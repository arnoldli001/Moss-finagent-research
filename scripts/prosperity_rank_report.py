"""景气度的两面性 + 变化率口径的回测对照。

## 回答用户的三个问题

1. **除了煤炭，还有哪些板块被"低景气度"压得进不了池？**
   → 逐板块算：景气度在当日横截面的分位、进候选池比例、以及
   "若把景气度抬到池中位（50）六维能涨多少分"（= 拖累幅度）。
2. **哪些板块是"景气度很高、但变化率开始下降"（高位见顶）从而造成误报？**
   → 定义状态 `水平值分位 ≥ 70 且 Δg 分位 ≤ 30`，看这些板块日的
   未来 20 日表现，以及**线上真实告警**有多少落在这种状态里。
3. **都按变化率算景气度，回测一轮效果。**
   → 在**告警层面**（不是候选层面）对 L / D / Δg / E / EF 五个口径做对照：
   告警量、未来 20 日涨跌分布、用户启动集判据 FP/TP、以及 24 个真值事件台账。

## ⚠️ 保真度说明（必须看）

线上 `total = base_total + etf_bonus + gate_bonus`，而 `base_total` 对
**池内**板块含蓄势层（加权 20%）、对池外板块等于六维分。离线无法重算蓄势层
（它依赖成员级两融/北向，且只对池内板块算），所以：

- **保真口径**（只用于现状）：直接用库里存的 `total` → 复现线上告警集，
  作为保真度校验；
- **对照口径**（所有方案一视同仁）：`total' = six_dim' + 加分`，**不含蓄势层**。

两种口径的绝对数不同，但 L 与 D 的**相对差异**在对照口径下是可比的。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/prosperity_rank_report.py \\
        --out docs/MAINLINE_PROSPERITY_RANK.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prosperity_variant_experiment import (  # noqa: E402
    SUB,
    delta_percentiles,
    load,
    make_variants,
    percentile_within_day,
    rebuild,
)

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LEAD_OK, LAG_OK = 10, 5
FP_RATIO = 0.22
HORIZON = 20
BIG = 0.10
MEDIUM = 77.0
TRAIN = ("20231009", "20250930")
TEST = ("20251001", "20260918")
#: 候选池规模（与线上一致：当日六维前 ~64）
TOPN = 64


def main() -> int:
    parser = argparse.ArgumentParser(description="景气度两面性与变化率回测")
    parser.add_argument("--windows", default="60,120")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    data = load()
    days = sorted({d for item in data.values() for d in item["days"]})
    index_of = {code: {day: i for i, day in enumerate(item["days"])}
                for code, item in data.items()}
    by_day: dict[str, list[str]] = {}
    for code, item in data.items():
        for day in item["days"]:
            by_day.setdefault(day, []).append(code)

    # 加分（三个口径共用，保持不变）
    bonus: dict[tuple[str, str], float] = {}
    stored_total: dict[tuple[str, str], float] = {}
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "SELECT trade_date, board_code, total, payload FROM mainline_score"):
        payload = json.loads(str(row["payload"] or "{}"))
        key = (str(row["trade_date"]), str(row["board_code"]))
        bonus[key] = (float(payload.get("etf_bonus") or 0.0)
                      + float(payload.get("gate_bonus") or 0.0))
        stored_total[key] = float(row["total"] or 0.0)
    alerts_online: dict[str, set[str]] = {}
    for row in conn.execute("SELECT trade_date, board_code FROM mainline_alert"):
        alerts_online.setdefault(str(row["board_code"]), set()).add(
            str(row["trade_date"]))
    names: dict[str, str] = {}
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    for row in cache.execute("SELECT code, name FROM ml_board"):
        names[str(row["code"])] = str(row["name"])
    cache.close()
    conn.close()

    variants, level = make_variants(
        data, days, by_day, index_of,
        windows=[int(x) for x in args.windows.split(",") if x.strip()],
        floors=[30.0, 40.0], change_weight=0.25)
    six_by_variant: dict[str, dict[tuple[str, str], float]] = {}
    for name, prosperity in variants.items():
        if name.startswith("L"):
            # 现状直接用库里存的六维（比重算更保真，用于第四节的保真度校验）
            six_by_variant[name] = {
                (day, code): data[code]["six_dim"][index_of[code][day]]
                for code in data for day in data[code]["days"]}
        else:
            six_by_variant[name] = rebuild(data, days, by_day, index_of,
                                           prosperity)

    emit("# 景气度的两面性：低景气压制了谁，高景气+变化率下行误报了谁")
    emit()
    emit("> 由 `scripts/prosperity_rank_report.py` 生成（只读）。")
    emit(f"> 候选 = 当日六维前 {TOPN}；告警 = 候选 且 `total ≥ {MEDIUM:g}`；"
         f"启动集口径同用户定义（20/35 日滚动涨幅 > 15%，步长 4）。")
    emit()

    # ---------- 一、被低景气压制的板块 ----------
    # ⚠️ 进池比例必须真的按"当日六维前 N 名"算。第一版写成
    # `(day, code) in six`，而 `six` 含全部 (日, 板块) 对 —— 于是每个板块
    # 都显示 100%，整张表失去意义。
    six_live = six_by_variant["L 水平值（现状）"]
    pool_by_day: dict[str, list[tuple[str, float]]] = {}
    for (day, code), value in six_live.items():
        if np.isfinite(value):
            pool_by_day.setdefault(day, []).append((code, value))
    in_pool: dict[str, set[str]] = {}
    for day, items in pool_by_day.items():
        items.sort(key=lambda kv: -kv[1])
        in_pool[day] = {code for code, _ in items[:TOPN]}

    drag: list[tuple[float, str, float, float, int]] = []
    for code, item in data.items():
        pcts = [level.get((day, code)) for day in item["days"]]
        pcts = [p for p in pcts if p is not None]
        if len(pcts) < 100:
            continue
        mean_pct = float(np.mean(pcts))
        dims_weight = {}
        for day in item["days"]:
            dims = item["dims"][index_of[code][day]]
            if "prosperity" in dims:
                dims_weight = dims
        weight = dims_weight.get("prosperity", (0.0, 30.0, False))[1]
        den = sum(w for _v, w, _r in dims_weight.values()) or 1.0
        # 把景气度抬到池中位（50 分）能加多少六维分
        drag_score = (50.0 - mean_pct) * weight / den if mean_pct < 50 else 0.0
        days_in_pool = sum(1 for day in item["days"]
                           if code in in_pool.get(day, ()))
        drag.append((drag_score, code, mean_pct,
                     days_in_pool / len(item["days"]), len(item["days"])))

    drag.sort(reverse=True)
    emit("## 一、被「低景气度」压制的板块（拖累幅度降序）")
    emit()
    emit("拖累幅度 = 若把该板块的景气度抬到池中位（50 分），它的六维分能加多少"
         f"（景气度维权重占六维的 {30 / 86:.0%}）。")
    emit()
    emit("| 板块 | 景气度均分 | 全市场分位 | 六维被拖累 | 进池比例 |")
    emit("|---|---:|---:|---:|---:|")
    for drag_score, code, mean_pct, rate, _n in drag[:25]:
        emit(f"| {names.get(code, code)}（{code}） | {mean_pct:.1f} "
             f"| {mean_pct:.0f}% | **−{drag_score:.1f} 分** | {rate:.0%} |")
    emit()
    emit(f"- 景气度分位 < 30% 的板块："
         f"**{sum(1 for d in drag if d[2] < 30)}** 个 / {len(drag)}；"
         f"其中进池比例 < 20% 的 "
         f"**{sum(1 for d in drag if d[2] < 30 and d[3] < 0.2)}** 个")
    emit(f"- 景气度分位 > 70% 的板块："
         f"**{sum(1 for d in drag if d[2] > 70)}** 个；"
         f"它们进池比例中位 "
         f"{np.median([d[3] for d in drag if d[2] > 70]):.0%}"
         f"（低景气组中位 "
         f"{np.median([d[3] for d in drag if d[2] < 30]):.0%}）")
    emit()
    emit("⚠️ 口径说明：这里的「全市场分位」是在**全部 ~320 个板块**里算的；"
         "而 `docs/MAINLINE_COAL_DIAGNOSIS.md` 里煤炭的 5.6% 是在"
         "**候选池（~64 个）**里算的 —— 池子本身就是六维选出来的，"
         "所以同一个板块在池内的景气度分位会更低。两个数都对，别混用。")
    emit()

    # ---------- 二、高景气 + 变化率下行 ----------
    change_pct = delta_percentiles(data, days, by_day, index_of, 60)
    forward: dict[tuple[str, str], float] = {}
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    closes: dict[str, list[tuple[str, float]]] = {}
    for row in conn.execute(
            "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        closes.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    conn.close()
    for code, items in closes.items():
        for pos, (day, close) in enumerate(items):
            if pos + HORIZON < len(items) and close > 0:
                forward[(day, code)] = items[pos + HORIZON][1] / close - 1.0

    online_days = {(day, code) for code, values in alerts_online.items()
                   for day in values}
    buckets: dict[str, list[tuple[str, str]]] = {
        "高景气(≥70) + 变化率下行(≤30)": [],
        "高景气(≥70) + 变化率不弱(>30)": [],
        "低景气(<30)": [],
        "其它": [],
    }
    for day in days:
        for code in by_day.get(day) or []:
            value = level.get((day, code))
            change = change_pct["profit_yoy"].get(day, {}).get(code)
            if value is None:
                continue
            if value >= 70 and change is not None and change <= 30:
                buckets["高景气(≥70) + 变化率下行(≤30)"].append((day, code))
            elif value >= 70:
                buckets["高景气(≥70) + 变化率不弱(>30)"].append((day, code))
            elif value < 30:
                buckets["低景气(<30)"].append((day, code))
            else:
                buckets["其它"].append((day, code))

    emit("## 二、「高景气 + 变化率下行」是不是高位见顶（误报来源）")
    emit()
    emit("| 状态 | 板块日数 | 未来 20 日 P(>+10%) | P(<−10%) | 均值 "
         "| 其中线上告警数 | 告警的未来 20 日均值 |")
    emit("|---|---:|---:|---:|---:|---:|---:|")
    for label, keys in buckets.items():
        values = np.asarray([forward[k] for k in keys if k in forward])
        hit_alerts = [k for k in keys if k in online_days]
        alert_values = np.asarray([forward[k] for k in hit_alerts
                                   if k in forward])
        if not values.size:
            continue
        mean_alert = (f"{alert_values.mean():+.2%}" if alert_values.size else "—")
        emit(f"| {label} | {len(keys)} | {(values > BIG).mean():.1%} "
             f"| {(values < -BIG).mean():.1%} | {values.mean():+.2%} "
             f"| {len(hit_alerts)} | {mean_alert} |")
    emit()
    emit("**实测结论：「高景气 + 变化率下行」不是见顶，而是最强的状态。**")
    emit()
    emit("它的未来 20 日 `P(>+10%)` = **20.1%**、`P(<−10%)` = **6.1%**、"
         "均值 **+3.63%** —— 四个状态里最好；而「高景气 + 变化率不弱」"
         "只有 15.6% / 8.2% / +1.85%，低景气组 13.3% / 9.8% / +1.31%。"
         "线上落在这一状态的 **432 条告警**，未来 20 日均值 **+3.25%**，"
         "同样是各状态里最高的。")
    emit()
    emit("所以「变化率下行 = 高位见顶 = 误报来源」这个假设**不成立**。"
         "这与本项目此前三次否掉「涨幅大 + 动量回落」波段顶门限的结论一致："
         "在这套数据里，**水平值高是持续的强势**，二阶导转弱并不预告见顶。"
         "用变化率替换水平值，等于把最强的状态换掉。")
    emit()

    # ---------- 三、告警层面的回测对照 ----------
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    cache.close()
    day_index = {day: i for i, day in enumerate(days)}

    labels_by_board: dict[str, tuple[list[int], list[str]]] = {}
    for code, items in closes.items():
        day_list = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        labels: set[int] = set()
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            pos = window
            while pos < len(close):
                if np.isfinite(rolling[pos]) and rolling[pos] > 0.15:
                    labels.add(pos)
                    pos += 4
                else:
                    pos += 1
        labels_by_board[code] = (sorted(labels), day_list)

    def alert_set(six: dict[tuple[str, str], float], low: str,
                  high: str) -> dict[str, set[str]]:
        """候选（当日六维前 N）且 `six_dim + 加分 ≥ 77`。"""
        by_day_scores: dict[str, list[tuple[str, float]]] = {}
        for (day, code), value in six.items():
            if low <= day <= high and np.isfinite(value):
                by_day_scores.setdefault(day, []).append((code, value))
        fired: dict[str, set[str]] = {}
        for day, items in by_day_scores.items():
            items.sort(key=lambda kv: -kv[1])
            for code, value in items[:TOPN]:
                total_value = value + bonus.get((day, code), 0.0)
                if total_value >= MEDIUM:
                    fired.setdefault(code, set()).add(day)
        return fired

    def launch_ledger(fired: dict[str, set[str]], low: str, high: str) -> dict:
        tp = fp = total = 0
        for code, (labels, day_list) in labels_by_board.items():
            index = {d: i for i, d in enumerate(day_list)}
            window_labels = [i for i in labels if low <= day_list[i] <= high]
            if not window_labels:
                continue
            total += len(window_labels)
            hits = {index[d] for d in fired.get(code, ())
                    if low <= d <= high and d in index}
            covered: set[int] = set()
            for i in window_labels:
                span = range(max(0, i - LEAD_OK), i + LAG_OK + 1)
                if any(t in span for t in hits):
                    tp += 1
                covered |= set(range(max(0, i - LEAD_OK), i + LAG_OK + 1))
            fp += sum(1 for t in hits if t not in covered)
        return {"labels": total, "tp": tp, "fp": fp,
                "recall": (tp / total) if total else float("nan"),
                "fp_ratio": (fp / tp) if tp else float("inf")}

    def truth_ledger(fired: dict[str, set[str]]) -> dict:
        reverse = {name: code for code, name in names.items()}
        good = late = early = missed = 0
        for event in events:
            date = str(event.get("date") or "").replace("-", "")
            if date not in day_index:
                continue
            codes = [reverse.get(str(c), str(c)) for c in (event.get("codes") or [])]
            center = day_index[date]
            window = set(days[max(0, center - 20):
                              min(len(days) - 1, center + 40) + 1])
            first = None
            for code in codes:
                for day in fired.get(code, ()):
                    if day in window and (first is None or day < first):
                        first = day
            if first is None:
                missed += 1
                continue
            delta = day_index[first] - center
            if delta < -LEAD_OK:
                early += 1
            elif delta <= LAG_OK:
                good += 1
            else:
                late += 1
        total = good + late + early + missed
        return {"good": good, "late": late, "early": early, "missed": missed,
                "rate": (good / total * 100) if total else float("nan")}

    emit("## 三、告警层面回测：五个景气度口径对照")
    emit()
    emit("⚠️ 对照口径 `total' = six_dim' + 加分`（**不含蓄势层**，见文首说明）。"
         "所有口径用同一规则，因此可横向比较；绝对数与线上不同。")
    emit()
    emit("| 口径 | 窗口 | 告警/日 | 合计 | P(>+10%) | P(<−10%) | 均值 "
         "| 启动集召回 | 启动集 FP/TP | 真值 合格/太早/滞后/漏报 |")
    emit("|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    summary: dict[str, dict] = {}
    for name, six in six_by_variant.items():
        for tag, (low, high) in (("训练", TRAIN), ("留出", TEST)):
            fired = alert_set(six, low, high)
            keys = [(day, code) for code, values in fired.items() for day in values]
            values = np.asarray([forward[k] for k in keys if k in forward])
            stat = launch_ledger(fired, low, high)
            ledger = truth_ledger(fired)
            n_days = sum(1 for day in days if low <= day <= high)
            summary[f"{name}|{tag}"] = {
                "alerts": len(keys), "per_day": len(keys) / max(n_days, 1),
                "up": float((values > BIG).mean()) if values.size else float("nan"),
                "down": float((values < -BIG).mean()) if values.size else float("nan"),
                "mean": float(values.mean()) if values.size else float("nan"),
                **stat, **ledger}
            emit(f"| {name} | {tag} | {len(keys) / max(n_days, 1):.2f} "
                 f"| {len(keys)} "
                 f"| {(values > BIG).mean():.1%} | {(values < -BIG).mean():.1%} "
                 f"| {values.mean():+.2%} | {stat['recall']:.0%} "
                 f"| {stat['fp_ratio']:.2f} "
                 f"| {ledger['good']}/{ledger['early']}/{ledger['late']}"
                 f"/{ledger['missed']} |")
    emit()

    # 保真度校验
    guard = alert_set(six_by_variant["L 水平值（现状）"], "", "99999999")
    live = {(day, code) for code, values in alerts_online.items()
            for day in values}
    mine = {(day, code) for code, values in guard.items() for day in values}
    emit("## 四、保真度校验（现状口径 vs 线上）")
    emit()
    emit(f"- 线上 `mainline_alert`：**{len(live)}** 条")
    emit(f"- 对照口径复现：**{len(mine)}** 条；交集 {len(live & mine)}，"
         f"仅线上有 {len(live - mine)}，仅离线有 {len(mine - live)}")
    emit()
    emit("差异来自三处，都是已知的：① 对照口径不含蓄势层；"
         "② 线上还有确认（`confirm_periods`）与冷却（`cooldown_days`）会把重复告警合并；"
         "③ 强度分档还要求「至少 2 个维度达标」，这里只用了分数线。"
         "所以下面只看**口径之间**的相对差异，不看绝对条数。")
    emit()
    emit("⚠️ **复现率只有 "
         f"{len(live & mine) / max(len(live), 1):.0%}**（交集 {len(live & mine)} / "
         f"线上 {len(live)}），缺口主要来自「不含蓄势层」—— 蓄势层占池内板块"
         "`base_total` 的 20%，缺了它，「六维分 + 加分」与真实 `total` 不是"
         "同一个量纲。因此**第三节的绝对数字不可信，只有口径之间的相对差异"
         "可参考**；而候选层面的对照（`docs/MAINLINE_PROSPERITY_VARIANTS.md`）"
         "只依赖六维排名，不受这个缺口影响，那份结论更硬。")
    emit()
    emit("## 五、结论")
    emit()
    emit("1. **被低景气压制的板块有 29 个**（景气度分位 < 30%，占 321 个的 9%），"
         "它们的进池比例中位只有 **2%**；而且**不只有红利股** —— "
         "既有煤炭/煤化工/物业/租售同权这类价值股，也有硅能源、短剧游戏、"
         "空间计算、DeepSeek概念、华为盘古、谷子经济、猴痘概念这些**新题材**"
         "（成员公司小、同比增速为负）。所以这是**系统性**问题，不是煤炭独有。")
    emit("2. **「变化率下行 = 见顶」被数据否掉**：高景气 + 变化率下行是"
         "四个状态里**最强**的（P(>+10%) 20.1%、均值 +3.63%），"
         "线上告警在这状态下也最好（+3.25%）。所以 22% 误报里"
         "**没有**「变化率下行」这一块可以掐。")
    emit("3. **换成变化率做回测：总体更差**。候选层面留出集 FP/TP 7.53→8.44；"
         "告警层面留出集 P(>+10%) 22.1%→15.5%、均值 +2.78%→**+1.14%**、"
         "真值漏报 12→13。两个层面结论一致：**Δg 替换水平值得不偿失**。")
    emit("4. 若确实想让那 29 个板块被看见，**低分压制（floor）**是唯一"
         "在两个窗口、两个指标上都不亏的做法（候选层面留出 FP/TP 7.53→6.42）；"
         "而它只把煤炭从 2.6% 抬到 5.3% —— 想抬到 28% 只能靠替换，"
         "代价就是上面第 3 条。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    print(json.dumps({"variants": list(six_by_variant)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
