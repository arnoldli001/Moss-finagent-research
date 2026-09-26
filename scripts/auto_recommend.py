"""自动建议引擎：从回测结果推导**可执行**的改进项，并按证据强度排序。

## 为什么需要"跨窗口复现"这个判据

本模块在调参上已经栽过两次，都是**单窗口假象**：

    第一次  "边缘只在尾部"（≥25% 涨幅提升 3.10x）→ 旧窗口同一判据 0.69x
    第二次  "把 six_dim 阈值提到 70 得 2.32x"     → 旧窗口 1.08x

所以本引擎对每条建议都**在两个互不重叠的窗口上各算一遍**，只在
`复现` 时才给「强建议」，否则标为「待观察」并写明它是单窗口结果。

## 建议是怎么产生的

规则表（`RULES`）把"指标状态"映射到"动作 + 预估影响"。每条建议都必须带：

    - 证据：具体数值（不是形容词）
    - 动作：改哪个配置项、改成什么
    - 预估影响：改动后指标大概怎么变（基于离线合成，不需要重打分）
    - 复现性：两个窗口是否一致

产出 `docs/MAINLINE_AUTO_ADVICE.md`，供人（或下一轮自动化）直接执行。

用法：
    .venv\\Scripts\\python.exe scripts\\auto_recommend.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.factor_ic_report import (  # noqa: E402
    combo_plan,
    composite,
    load_returns,
    load_scores,
    rank_ic,
    summarize,
)

#: ⚠️ **三个**窗口（与 `pool_score_scan` / `iterate_mainline` 保持一致）。
#: 2024-09~2025-09 早期没有打分，全量重打分补上后是独立行情；
#: 两个窗口同号很容易是巧合 —— §16.38 就是在加入第三个窗口后推翻了
#: "层权重 100/0 已被确认"。
WINDOWS = (("20231009", "20240806", "旧区间"),
           ("20240901", "20250930", "中间区间"),
           ("20251001", "20260918", "新区间"))
#: 评估用的持有期
HORIZONS = (20, 60)
#: `factor_ic_report.load_scores` 额外给出的**派生列**（不是可调维度）。
#: `selection.rank_key` = 真实排序键（`base_total + bonus_potential + etf_bonus`）。
#: 规则 R1/R3 是针对"可调权重"的，必须跳过它 —— 否则会给出
#: "把 `rank_key` 反向或剔除"这种照做不了的假建议。
DERIVED = ("selection.",)


def config_facts() -> dict:
    """读当前配置，避免给出**已经实施过**的建议。

    ## 为什么必须读配置（冒烟测试抓到的真 bug）

    `DimensionScore.score` 存的是**自然分**：`six_dim.reverse_dims` 里的维度
    在层合成时才取 `100 - score`（见 `six_dim.py` 的说明）。
    所以即使已经反向，`rank_ic(该维度分, 未来收益)` **仍然是负的** ——
    建议引擎只看 IC 符号，就会建议"把这个维度反向"，
    而它**已经反了** ⇒ 照着做会把 V2.2 抵消掉，指标反而回退。

    同理：层（`total` / `six_dim` / `accumulation`）的权重来自
    `synthesis.layer_weights`，不是 `six_dim.weights` —— 对层提"加权重"毫无意义；
    权重为 0 的维度（如 `accumulation.etf`）也不该被告知"权重是空的"。
    """
    from src.mainline.config import load_config
    cfg = load_config(force=True)
    return {
        "reversed": set(cfg.six_dim.reverse_dims),
        "weights": {
            "six_dim": dict(cfg.six_dim.weights),
            "accumulation": dict(cfg.accumulation.weights),
        },
        "layers": set(cfg.synthesis.layer_weights),
        # 报告头要记录这两项：P2/R4 的"现行"基线由层权重决定，
        # 而层权重的尺度又决定了告警阈值的含义（见 §16.34）。
        "layer_weights": dict(cfg.synthesis.layer_weights),
        "medium_score": cfg.alert.medium_score,
        "strong_score": cfg.alert.strong_score,
    }


def leaf_weight(name: str, facts: dict) -> float | None:
    """`six_dim.trading` → 该维度在所属层里的权重；不是叶子维度返回 None。"""
    layer, _, leaf = name.partition(".")
    if not leaf:
        return None
    table = facts["weights"].get(layer)
    if not table:
        return None
    return float(table.get(leaf, 0.0))


def window_metrics(start: str, end: str) -> dict:
    """一个窗口上的全部指标。"""
    scores, dims = load_scores(start, end)
    if scores.empty:
        return {}
    returns = load_returns(start, end)
    out: dict = {"days": len(scores), "boards": scores.shape[1],
                 "total": {}, "dims": {}, "combos": {}, "dim_sd": {}}
    for horizon in HORIZONS:
        series, _ = rank_ic(scores, returns[horizon])
        out["total"][horizon] = summarize(series, horizon)
        for name, frame in dims.items():
            if frame.empty:
                continue
            s, _ = rank_ic(frame, returns[horizon])
            stat = summarize(s, horizon)
            if stat:
                out["dims"].setdefault(name, {})[horizon] = stat
    # 每个因子的**逐日横截面标准差**（均值）。用途见 R1：
    # 一个"每天所有板块都是同一个值"的因子（实测 `six_dim.macro` 在新窗口
    # 池内 sd = 0.000）**没有横截面信息**，给它建议"反向"或"加权"都是徒劳 ——
    # 反一个常数列不会改变任何排序。不把这条拦住，建议清单里就会躺着一个
    # 照做也没用的动作。
    for name, frame in dims.items():
        if frame.empty:
            continue
        out["dim_sd"][name] = float(frame.std(axis=1, ddof=0).mean())
    # 离线权重实验（不需要重打分）
    six = {n.split(".", 1)[1]: f for n, f in dims.items()
           if n.startswith("six_dim.")}
    acc = {n.split(".", 1)[1]: f for n, f in dims.items()
           if n.startswith("accumulation.")}
    # 离线权重实验（不需要重打分）。方案与基线**共用** `combo_plan()` ——
    # 本文件曾自己硬编码一份全正的"现行六维"，而库里存的是自然分
    # （反向 = 负权重），那份"现行"实际是**不反向的对照组**。
    for item in combo_plan():
        source = acc if item["source"] == "accumulation" else six
        frame = composite(source, item["weights"])
        if frame.empty:
            continue
        bucket = out["combos"].setdefault(item["label"], {})
        bucket["baseline"] = item["baseline"]
        for horizon in HORIZONS:
            s, _ = rank_ic(frame, returns[horizon])
            stat = summarize(s, horizon)
            if stat:
                bucket[horizon] = stat
    out["scale"] = layer_scale(start, end)
    return out


def effective_share(nominal_six: float, sd_six: float,
                    sd_acc: float) -> float | None:
    """名义权重下，六维层**实际**推动分数的份额（按 `w·sd` 归一）。

    两层原始分相加 `w·x + (1−w)·y`，每一项的典型波动幅度是 `w_i·sd_i`，
    所以"这一层在多大程度上决定排序"就是 `w_i·sd_i / Σ w_j·sd_j`。

    **尺度相同时它精确等于名义权重**（任意 w），这正是它比"方差占比"更适合
    当"有效权重"的原因：方差占比会把尺度差**平方**放大 —— 均衡尺度下
    `w=0.3` 的方差占比只有 0.155，看起来像"权重没生效"，其实生效了。
    """
    left = nominal_six * sd_six
    right = (1 - nominal_six) * sd_acc
    if left + right <= 0:
        return None
    return left / (left + right)


def variance_share(nominal_six: float, var_six: float,
                   var_acc: float) -> float | None:
    """名义权重下，六维层贡献的**合成方差**占比。

    `w·x + (1−w)·y` 在 x、y 独立时，方差是 `w²·var(x) + (1−w)²·var(y)`。
    这个量回答的是"合成分的波动主要来自哪一层"，比 `effective_share` 更极端
    （尺度差被平方），所以**只用来说明问题有多严重**，不用来当有效权重。

    这是 V2.3 改动的核心算式，单独抽出来是为了能脱离数据库做单测 ——
    算错了会让整条 R6 规则静默失效。
    """
    denominator = (nominal_six ** 2) * var_six + ((1 - nominal_six) ** 2) * var_acc
    if denominator <= 0:
        return None
    return (nominal_six ** 2) * var_six / denominator


def layer_scale(start: str, end: str) -> dict:
    """诊断「层权重是名义的还是实际的」。

    ## 为什么必须自动检查这一条

    V2.2 的 `layer_weights` 写着 50/50，但候选池内两层分数的**尺度差约 3 倍**
    （`six_dim` sd≈4.5，`accumulation` sd≈13.1）。两层原始分直接相加时，
    方差贡献是 `w²·var`，所以六维层只占 **约 10%** 的方差 ——
    "50/50"实际约等于"10/90"，排序由第二层主导，而第二层的 IC 恰好更差。

    这个错误**不会报任何错**：配置里写着 50/50，日志里写着 50/50，
    只有把两层的方差算出来才看得见。所以把它做成一条规则（R6），
    而不是靠人记得去看。

    返回 `{"nominal_six", "share_six", "var_share_six", "sd_six", "sd_acc"}`；
    候选池为空时 `{}`。`share_six` 是**按 `w·sd` 归一的实际推动份额**
    （尺度相同时精确等于名义权重），`var_share_six` 是合成方差占比
    （尺度差被平方，用来说明问题有多严重）。
    """
    from scripts.layer_weight_sim import load_panel

    panel = load_panel(start, end)
    if panel.empty:
        return {}
    by_day = panel.groupby("trade_date")
    v_six = float(by_day["six_dim"].var(ddof=0).mean())
    v_acc = float(by_day["accumulation"].var(ddof=0).mean())
    from src.mainline.config import load_config
    nominal = float(load_config(force=True).synthesis.normalized_layers()
                    .get("six_dim", 0.0)) / 100.0
    sd_six, sd_acc = v_six ** 0.5, v_acc ** 0.5
    return {"nominal_six": nominal,
            "share_six": effective_share(nominal, sd_six, sd_acc),
            "var_share_six": variance_share(nominal, v_six, v_acc),
            "sd_six": sd_six, "sd_acc": sd_acc,
            "boards": int(len(panel))}


def replicated(old: float | None, new: float | None, *,
               kind: str) -> str:
    """判定跨窗口是否复现。`kind` = improve / degrade。"""
    if old is None or new is None:
        return "数据不足"
    if kind == "improve":
        return "复现" if (old > 0 and new > 0) else "单窗口"
    return "复现" if (old < 0 and new < 0) else "单窗口"


def main() -> int:
    parser = argparse.ArgumentParser(description="自动建议引擎")
    parser.add_argument("--out", default="docs/MAINLINE_AUTO_ADVICE.md")
    parser.add_argument("--json", default="docs/MAINLINE_AUTO_ADVICE.json")
    args = parser.parse_args()

    results: dict[str, dict] = {}
    for start, end, label in WINDOWS:
        print(f"计算 {label}（{start}~{end}）…")
        results[label] = window_metrics(start, end)
        if not results[label]:
            print("   ⚠️ 该窗口没有评分数据，跳过")
    labels = [label for _, _, label in WINDOWS if results.get(label)]
    if not labels:
        print("❌ 两个窗口都没有数据")
        return 2

    advice: list[dict] = []
    facts = config_facts()
    print(f"当前配置：反向维度={sorted(facts['reversed'])}；"
          f"六维权重={facts['weights']['six_dim']}；"
          f"第二层权重={facts['weights']['accumulation']}")

    def add(priority: int, title: str, evidence: str, action: str,
            impact: str, repro: str) -> None:
        advice.append({"优先级": priority, "改进项": title, "证据": evidence,
                       "动作": action, "预估影响": impact, "复现性": repro})

    # ---------- R1：维度符号 ----------
    for name in sorted({n for label in labels for n in results[label]["dims"]}):
        # 派生列（如 `selection.rank_key`）不是可调权重的维度，跳过
        if name.startswith(DERIVED):
            continue
        # 🩸 「死因子」：逐日横截面几乎恒定的维度**没有横截面信息**，
        # 反向/加权都是徒劳（反一个常数列不改变排序）。
        # 实测 `six_dim.macro` 在新窗口候选池内 sd = 0.000（211/211 天）。
        sds = [results[label].get("dim_sd", {}).get(name) for label in labels]
        sds = [v for v in sds if v is not None]
        if sds and max(sds) < 0.5:
            add(9, f"维度 `{name}` 在候选池内**几乎没有横截面差异**（无需动作）",
                "；".join(f"{label} 逐日截面 sd 均值 {v:.3f}"
                          for label, v in zip(labels, sds, strict=False)),
                "无需动作 —— 反向或调权重都不会改变排序",
                "一个「所有板块同分」的维度对精选毫无贡献；"
                "要修得先查它的数据覆盖，而不是调权重",
                "—")
            continue
        signs: list[str] = []
        detail: list[str] = []
        for label in labels:
            stat = results[label]["dims"].get(name, {}).get(20)
            if not stat:
                continue
            signs.append("负" if stat["ic"] < 0 else "正")
            detail.append(f"{label} IC={stat['ic']:+.4f}（IC>0 "
                          f"{stat['win'] * 100:.0f}%）")
        if not signs:
            continue
        # ⚠️ 已经反向过的维度：它的**自然分** IC 必然还是负的，
        # 但层合成里用的是 `100 - score`，再反向一次就把 V2.2 抵消了。
        if name.split(".", 1)[-1] in facts["reversed"]:
            add(9, f"维度 `{name}` 已配置反向（无需动作）",
                "；".join(detail) + "；注意：存的是**自然分**，IC 为负是预期的",
                "无需动作 —— 它已在 `six_dim.reverse_dims` 里，"
                "层合成时取 `100 - score`",
                "若误按 IC 符号再反向一次，会把 V2.2 抵消、指标回退",
                "—")
            continue
        if all(s == "负" for s in signs) and len(signs) > 1:
            layer, dot, leaf = name.partition(".")
            if not dot:
                # 层（total / six_dim / accumulation / leader）不能"反向"：
                # `reverse_dims` 只作用于 six_dim 的维度。层的 IC 为负说明
                # **它的产物在给反向信号加权**，可执行的动作是不要用它加权。
                if name == "leader":
                    add(1, "`leader` 层两个窗口 IC 都为负 → "
                           "门控加分在给反向信号加权",
                        "；".join(detail) + "（`leader` 层只用于门控加分，"
                        "不占权重）",
                        "把 `leader.gate_bonus_max` 设为 0（或先重审"
                        "`concentration` / `seat` 两个子维度）",
                        "门控加分最高 20 分；16.9 实测提名通道提升 "
                        "0.73x（低于基准），与这里的负 IC 一致",
                        "复现")
                else:
                    add(2, f"层 `{name}` 两个窗口 IC 都为负 → 检查其子维度",
                        "；".join(detail),
                        "逐个子维度看 IC，把负的剔除/反向（见同表的维度行）",
                        "层是子维度的加权和，先定位是哪一项拖累",
                        "复现")
                continue
            add(1, f"维度 `{name}` 两个窗口 IC 都为负 → 反向或剔除",
                "；".join(detail),
                "在 `six_dim.reverse_dims` 里加入该维度（或从 "
                "`weights` 里删掉并让其余维度重新归一）",
                "参考 16.14 的实测：`trading`/`technical` 反向使六维 "
                "IC 从 −0.0375 变成 +0.0800（H=20）",
                "复现")
        elif all(s == "正" for s in signs) and len(signs) > 1:
            weight = leaf_weight(name, facts)
            if weight is None:
                continue          # 层不是"可加权的维度"
            add(3, f"维度 `{name}` 两个窗口 IC 都为正 → 可加权",
                "；".join(detail) + f"；当前权重 {weight:g}",
                "提高它在 `six_dim.weights` 里的权重",
                "权重提高后需重跑 IC 确认（离线合成可先估）", "复现")

    # ---------- R2：合成是否在稀释 ----------
    # ⚠️ 用什么跟"最强单维度"比很关键：`total` 里含**兑现后**的 `gate_bonus`
    # （只有约 12% 的候选板块拿到那 15~20 分），像"平滑分数 + 稀疏跳变"，
    # 它的 IC **天然**低于任何平滑因子，拿它比会一直报"权重在稀释"——
    # 而那不是权重问题，是列选错了。所以优先用真实排序键
    # `selection.rank_key = base_total + bonus_potential + etf_bonus`。
    for label in labels:
        dims = results[label]["dims"]
        best = max(((name, dims[name][20]["ic"])
                    for name in dims
                    if 20 in dims[name] and not name.startswith(DERIVED)),
                   default=None, key=lambda kv: kv[1])
        if best is None:
            continue
        key_name = next((n for n in dims if n.startswith("selection.")), None)
        if key_name is not None:
            head = dims[key_name].get(20, {}).get("ic")
            head_label = "排序键 `selection.rank_key`"
        else:
            head = results[label]["total"].get(20, {}).get("ic")
            head_label = "`total`（无 `bonus_potential`，退化为兑现后分）"
        if head is None:
            continue
        if head < best[1]:
            add(2, f"{label}：合成排序的 IC 低于单维度最强项 → 权重在稀释",
                f"{head_label} IC={head:+.4f} < `{best[0]}` "
                f"IC={best[1]:+.4f}",
                "重新分配层内权重：把权重挪向正 IC 的维度，"
                "负 IC 的维度反向或清零",
                "见下方「离线权重实验」表，直接读各方案 IC",
                replicated(
                    results[labels[0]]["total"].get(20, {}).get("ic"),
                    results[labels[-1]]["total"].get(20, {}).get("ic"),
                    kind="degrade"))

    # ---------- R3：不可用率高的维度 ----------
    for label in labels:
        total_rows = results[label]["days"] * results[label]["boards"]
        for name, series in results[label]["dims"].items():
            if name.startswith(DERIVED):      # 派生列无权重可调
                continue
            days = series.get(20, {}).get("days", 0)
            if not days or not total_rows:
                continue
            weight = leaf_weight(name, facts)
            # 权重为 0 的维度不参与加权，报"它占的权重是空的"会误导
            if weight is None or weight <= 0:
                continue
            ratio = days / results[label]["days"]
            if ratio < 0.5:
                add(4, f"维度 `{name}` 在 {label} 只有 {ratio * 100:.0f}% 的"
                       "交易日可用 → 它占的权重多数时候是空的",
                    f"可用交易日 {days}/{results[label]['days']}；"
                    f"名义权重 {weight:g}",
                    "降低其权重（空缺会让其余维度被动归一化，"
                    "分数不可比）",
                    "权重重新分配后可离线合成验证", "复现" if ratio < 0.3 else "待观察")

    # ---------- R6：层权重是"名义"的还是"实际"的 ----------
    # 这一条来自 V2.3 的教训：配置写着 50/50，但两层的分数量纲差约 3 倍，
    # 于是六维层实际只推动约 1/4 的分数、合成方差只占约 1/10，排序由第二层
    # 主导 —— 而配置和日志都写着 50/50，**不会报任何错**。
    # 触发用「按 w·sd 归一的实际份额」（尺度相同时它精确等于名义权重），
    # 方差占比只作为"问题有多严重"的补充说明。
    scale: dict[str, dict] = {label: (results[label].get("scale") or {})
                              for label in labels}
    gaps = {label: (item["share_six"] - item["nominal_six"])
            for label, item in scale.items()
            if item.get("share_six") is not None
            and item.get("nominal_six") is not None}
    if gaps:
        values = list(gaps.values())
        # 复现判据：两个窗口的偏离**同号**（都偏六维 或 都偏第二层）
        same_sign = len(values) > 1 and (all(v > 0 for v in values)
                                        or all(v < 0 for v in values))
        if any(abs(value) > 0.15 for value in values):
            item = scale[max(gaps, key=lambda key: abs(gaps[key]))]
            detail = "；".join(
                f"{label} 实际 {scale[label]['share_six'] * 100:.0f}%（方差占比 "
                f"{scale[label]['var_share_six'] * 100:.0f}%）"
                f" vs 名义 {scale[label]['nominal_six'] * 100:.0f}%"
                for label in gaps)
            add(1, "层权重是**名义**的 —— 实际推动份额 ≠ 配置里的数字",
                f"{detail}。候选池内 sd：`six_dim` {item['sd_six']:.1f} / "
                f"`accumulation` {item['sd_acc']:.1f}（{item['boards']} 行）；"
                "两层分数不在同一尺度上时，`Σ w·sd` 才是真实影响",
                "把 `synthesis.layer_weights` 向 IC 更好的那层倾斜，"
                "或先对两层做日内标准化再等权"
                "（`scripts/layer_weight_sim.py`）",
                "用 `layer_weight_sim.py` 直接比「取头部的超额收益」，"
                "不要只看 IC 或权重数字",
                "复现" if same_sign else "单窗口")

    # ---------- R4：权重实验里更好的方案 ----------
    # 基线用 `combo_plan()` 标出来的 `baseline` 字段，**不再按标签名找** ——
    # 老代码写死 `combos["现行六维"]`，一旦改名就静默跳过整条建议；
    # 更糟的是那个标签本身指向的是"不反向对照组"（见 combo_plan 的说明）。
    for label in labels:
        base_ic: float | None = None
        for _name, series in results[label]["combos"].items():
            if series.get("baseline") and 20 in series:
                base_ic = series[20].get("ic")
                break
        if base_ic is None:
            continue
        best_label, best_ic = None, base_ic
        for name, series in results[label]["combos"].items():
            if series.get("baseline") or 20 not in series:
                continue
            ic = series[20].get("ic")
            if ic is not None and ic > best_ic:
                best_label, best_ic = name, ic
        if best_label:
            add(2, f"{label}：离线实验里有比现行更好的权重方案",
                f"现行 IC={base_ic:+.4f} → `{best_label}` IC={best_ic:+.4f}",
                f"按 `{best_label}` 改 `configs/mainline.yaml` 的权重",
                f"IC 提升 {best_ic - base_ic:+.4f}；改完需重打分验证",
                "待观察（离线合成不等于实盘）")

    # ---------- R5：告警层与交易层（读已有报告） ----------
    precision = ROOT / "docs" / "MAINLINE_ALERT_TRADES.xlsx"
    if precision.exists():
        try:
            summary = pd.read_excel(precision, sheet_name="汇总")
            weak = pd.read_excel(precision, sheet_name="分档统计")
            row20 = summary[summary["持有期(交易日)"] == 20]
            if not row20.empty:
                exp20 = float(row20["期望收益"].iloc[0])
                if exp20 < 0:
                    add(1, "20 日持有期望为负 → 告警层在负期望上发信号",
                        f"期望收益 {exp20:+.2f}%、胜率 "
                        f"{float(row20['胜率'].iloc[0]):.1f}%、"
                        f"基准胜率 {float(row20['基准胜率'].iloc[0]):.1f}%",
                        "提高告警门槛把日均条数压到个位数；"
                        "或先修合成权重（R2）再看",
                        "见 `docs/MAINLINE_ALERT_TRADES.xlsx` 的"
                        "「命中概率」表：60 日 ≥7% 比例 29.6%（收在）/ "
                        "52.0%（触及）", "待观察")
                if "等级" in weak.columns and "期望收益" in weak.columns:
                    for _, item in weak.iterrows():
                        if float(item["期望收益"]) < 0:
                            add(int(item["期望收益"] < -1) + 4,
                                f"等级 `{item['等级']}` 期望为负 → 考虑砍掉",
                                f"期望 {float(item['期望收益']):+.2f}%、"
                                f"样本 {int(item['样本数'])}",
                                "从告警分档里移除该等级",
                                "减少低质量告警条数", "待观察")
        except Exception as exc:  # noqa: BLE001
            print(f"（读交易统计失败：{type(exc).__name__}: {exc}）")

    advice.sort(key=lambda item: item["优先级"])
    # 把**当时生效的合成口径**写进报告头：本文件是"改配置"的依据，
    # 如果不记录层权重与告警阈值，事后无法判断这份建议对应哪一版配置
    # （P2/R4 的基线完全由它们决定）。
    facts = config_facts()
    lines = ["# 主线挖掘 自动改进建议（机器生成）\n",
             f"- 评估窗口：{'、'.join(labels)}\n",
             "- 当前口径："
             f"层权重 {facts.get('layer_weights')}；"
             f"反向维度 {sorted(facts.get('reversed') or [])}；"
             f"中/强信号线 {facts.get('medium_score')}/{facts.get('strong_score')}\n",
             "- **复现性**：`复现` = 两个窗口结论一致（可信）；"
             "`单窗口`/`待观察` = 只在部分窗口成立，不要当规律\n",
             "> 本文件由 `scripts/auto_recommend.py` 生成；"
             "每条建议都带可核对的数值，不要只读标题。\n"]
    lines.append("\n## 一、离线权重实验（不需要重打分，直接读 IC）\n")
    for label in labels:
        combos = results[label]["combos"]
        if not combos:
            continue
        lines.append(f"\n### {label}\n")
        lines.append("| 方案 | H=20 IC | IC>0 | H=60 IC | IC>0 |")
        lines.append("|---|---:|---:|---:|---:|")
        for name, series in combos.items():
            s20 = series.get(20, {})
            s60 = series.get(60, {})
            mark = " **← 现行**" if series.get("baseline") else ""
            lines.append(
                f"| {name}{mark} | {s20.get('ic', float('nan')):+.4f} "
                f"| {s20.get('win', 0) * 100:.0f}% "
                f"| {s60.get('ic', float('nan')):+.4f} "
                f"| {s60.get('win', 0) * 100:.0f}% |")
    lines.append("\n## 二、建议清单（按优先级）\n")
    lines.append("| 优先级 | 改进项 | 证据 | 动作 | 预估影响 | 复现性 |")
    lines.append("|---:|---|---|---|---|---|")
    for item in advice:
        lines.append(f"| {item['优先级']} | {item['改进项']} | {item['证据']} "
                     f"| {item['动作']} | {item['预估影响']} "
                     f"| {item['复现性']} |")
    if not advice:
        lines.append("| — | 没有触发任何规则 | — | — | — | — |")

    (ROOT / args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ROOT / args.json).write_text(
        json.dumps({"windows": results, "advice": advice},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n建议 → {args.out}")
    print(f"机器可读 → {args.json}")
    print(f"\n共 {len(advice)} 条建议：")
    for item in advice:
        print(f"  [P{item['优先级']}][{item['复现性']}] {item['改进项']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
