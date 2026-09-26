"""因子 IC / ICIR 报告：连续分到底有没有信息。

## 为什么必须先做这一步

告警层的提升倍数只有 0.93~1.14x（见 `scripts/alert_precision_report.py`）。
但那只说明**二值化之后**没信息。连续分本身可能仍有排序能力 ——
两者的修法完全不同：

    分数没信息（IC ≈ 0）      → 因子要重做，调阈值没用
    分数有信息、转换丢了        → 调阈值/分档就能救回来

**这个报告就是分辨这两种情况的那把尺子。**

## 口径（每一条都要写清，否则数字无法解释）

- **IC**：横截面 **Spearman 秩相关**（每日：当日全部板块的分数 vs 未来收益）。
  用秩相关而不是 Pearson：分数的分布是偏斜的，且我们只关心排序。
- **收益**：板块指数 `ml_board_bar.close`，`(close[D+H] / close[D] - 1)`。
  D 日收盘买入口径（与 `alert_precision_report.py` 一致）。
- **ICIR** = `mean(IC) / std(IC)`。
  ⚠️ **重叠窗口会把 t 值吹大**：H=20 时相邻 20 天的 IC 高度自相关，
  `t = mean/std*sqrt(N)` 严重高估。所以本脚本**同时**给
  `N_非重叠`（每 H 个交易日取一个点）与对应的 t 值，
  **以非重叠那个为准**。
- **分位价差**：每日按分数分 5 组，看第 1 组与第 5 组的未来收益差。
  它比 IC 更贴近"能不能赚钱"，也能暴露"只有极端组有效"这种情况。

用法：
    .venv\\Scripts\\python.exe scripts\\factor_ic_report.py
    .venv\\Scripts\\python.exe scripts\\factor_ic_report.py --start 20251001
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE_DB = ROOT / "data" / "mainline_cache.db"
MAIN_DB = ROOT / "data" / "moss_finagent.db"
HORIZONS = (5, 10, 20, 60)
#: 要算 IC 的"因子"：整体分 + 两层分 + 逐维度
LAYERS = ("six_dim", "accumulation", "leader")


def load_scores(start: str, end: str) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """读评分。返回 `(总分表, {因子名: 分数表})`，索引=日期、列=板块代码。

    除了 `total` 与各层/各维度，还会（在有数据时）额外给出
    `selection.rank_key` = `base_total + bonus_potential + etf_bonus`，
    也就是**真正的排序键**。为什么需要它单独一列：`total` 用的是**兑现后**的
    `gate_bonus`（只有约 12% 的候选板块拿到那 15~20 分），于是它像
    "平滑分数 + 稀疏跳变"，用它算秩相关会**系统性低估**排序质量 ——
    实测 `base_total` IC +0.075/+0.085 对 `total` +0.052/+0.040。
    详见 `BoardScore.bonus_potential` 与 §16.35。

    老数据没有 `bonus_potential` 字段时**不生成**这一列（宁缺勿假：
    用 `gate_bonus` 兜底会让它退化成 `total`、"看起来有数据其实没测到"）。
    """
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    totals: dict[str, dict[str, float]] = {}
    dims: dict[str, dict[str, dict[str, float]]] = {}
    rank_key: dict[str, dict[str, float]] = {}
    rows = conn.execute(
        "SELECT trade_date, board_code, total, payload FROM mainline_score"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end)).fetchall()
    for row in rows:
        day, code = str(row["trade_date"]), str(row["board_code"])
        totals.setdefault(day, {})[code] = float(row["total"] or 0.0)
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if "bonus_potential" in payload:
            rank_key.setdefault(day, {})[code] = (
                float(payload.get("base_total") or 0.0)
                + float(payload.get("bonus_potential") or 0.0)
                + float(payload.get("etf_bonus") or 0.0))
        for layer in (payload.get("layers") or []):
            key = str(layer.get("key") or "")
            if key in LAYERS:
                dims.setdefault(key, {}).setdefault(day, {})[code] = \
                    float(layer.get("score") or 0.0)
            for dim in (layer.get("dimensions") or []):
                if not dim.get("available"):
                    continue
                name = f"{key}.{dim.get('key')}"
                dims.setdefault(name, {}).setdefault(day, {})[code] = \
                    float(dim.get("score") or 0.0)
    conn.close()
    if rank_key:
        dims["selection.rank_key"] = rank_key
    return pd.DataFrame(totals).T, {k: pd.DataFrame(v).T for k, v in dims.items()}


def load_returns(start: str, end: str) -> dict[int, pd.DataFrame]:
    """`{H: 未来 H 日收益表}`，索引=日期、列=板块代码。"""
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY board_code, trade_date",
        (start, end)).fetchall()
    conn.close()
    frame = pd.DataFrame(
        [{"board_code": str(r["board_code"]), "trade_date": str(r["trade_date"]),
          "close": float(r["close"] or 0)} for r in rows])
    out: dict[int, pd.DataFrame] = {}
    for horizon in HORIZONS:
        wide = frame.pivot(index="trade_date", columns="board_code",
                           values="close").sort_index()
        # 用 shift(-H) 而不是逐日循环：一次性、向量化
        out[horizon] = wide.shift(-horizon) / wide - 1.0
    return out


def rank_ic(scores: pd.DataFrame, returns: pd.DataFrame
            ) -> tuple[pd.Series, int]:
    """逐日横截面 Spearman IC。返回 `(每日IC, 参与计算的板块数中位数)`。"""
    common_days = scores.index.intersection(returns.index)
    out: dict[str, float] = {}
    counts: list[int] = []
    score_rank = scores.loc[common_days].rank(axis=1)
    ret_rank = returns.loc[common_days].rank(axis=1)
    for day in common_days:
        left = score_rank.loc[day].dropna()
        right = ret_rank.loc[day].dropna()
        shared = left.index.intersection(right.index)
        if len(shared) < 20:              # 少于 20 个样本算不出有意义的横截面
            continue
        a, b = left[shared].to_numpy(), right[shared].to_numpy()
        if a.std() == 0 or b.std() == 0:
            continue
        value = float(np.corrcoef(a, b)[0, 1])
        if math.isfinite(value):
            out[day] = value
            counts.append(len(shared))
    return pd.Series(out).sort_index(), (int(np.median(counts)) if counts else 0)


def summarize(series: pd.Series, step: int) -> dict[str, float]:
    """IC 汇总。`step` 用于取**非重叠**子样本。"""
    if series.empty:
        return {}
    mean = float(series.mean())
    std = float(series.std(ddof=1)) if len(series) > 1 else 0.0
    thin = series.iloc[::max(1, step)]
    t_mean = float(thin.mean())
    t_std = float(thin.std(ddof=1)) if len(thin) > 1 else 0.0
    return {
        "days": len(series), "ic": mean,
        "icir": (mean / std) if std else 0.0,
        "win": float((series > 0).mean()),
        "t": (mean / std * math.sqrt(len(series))) if std else 0.0,
        "days_ne": len(thin), "ic_ne": t_mean,
        "icir_ne": (t_mean / t_std) if t_std else 0.0,
        "t_ne": (t_mean / t_std * math.sqrt(len(thin))) if t_std else 0.0,
    }


def decile_spread(scores: pd.DataFrame, returns: pd.DataFrame,
                  *, groups: int = 5) -> tuple[float, list[float]]:
    """按分数分组的未来收益（升序组 → 降序组），返回 `(最高-最低, 各组均值)`。"""
    common_days = scores.index.intersection(returns.index)
    buckets: list[list[float]] = [[] for _ in range(groups)]
    for day in common_days:
        left = scores.loc[day].dropna()
        right = returns.loc[day].dropna()
        shared = left.index.intersection(right.index)
        if len(shared) < groups * 5:
            continue
        ordered = left[shared].sort_values()
        # 等分；`qcut` 对并列值会报错，所以按序号切
        edges = np.linspace(0, len(ordered), groups + 1).astype(int)
        for index in range(groups):
            chunk = ordered.iloc[edges[index]:edges[index + 1]].index
            buckets[index].append(float(right[chunk].mean()))
    means = [float(np.mean(b)) if b else float("nan") for b in buckets]
    return (means[-1] - means[0]), means


def composite(parts: dict[str, pd.DataFrame],
              weights: dict[str, float]) -> pd.DataFrame:
    """按权重合成（**可用维度归一化**，与线上 `weighted_score` 同口径）。

    缺失维度从分母剔除而不是当 0 分 —— 否则"某个维度没数据"会直接压低分数，
    与线上行为不一致，模拟出来的结论就不可迁移。
    """
    index = None
    columns = None
    for name in weights:
        frame = parts.get(name)
        if frame is None:
            continue
        index = frame.index if index is None else index.union(frame.index)
        columns = (frame.columns if columns is None
                   else columns.union(frame.columns))
    if index is None or columns is None:
        return pd.DataFrame()
    total = pd.DataFrame(0.0, index=index, columns=columns)
    usable = pd.DataFrame(0.0, index=index, columns=columns)
    for name, weight in weights.items():
        frame = parts.get(name)
        if frame is None or weight == 0:
            continue
        aligned = frame.reindex(index=index, columns=columns)
        mask = aligned.notna()
        total = total.add(aligned.fillna(0.0) * weight, fill_value=0.0)
        usable = usable.add(mask.astype(float) * abs(weight), fill_value=0.0)
    out = total / usable.replace(0.0, np.nan)
    return out


def current_six_weights() -> dict[str, float]:
    """把**线上生效**的六维配置翻译成「自然分空间」的权重。

    ⚠️ 这是本项目最容易搞错的一处语义，必须写清楚：

    `mainline_score.payload` 里存的是 `DimensionScore.score`，也就是**自然分**；
    反向信息在 `reversed` 字段里，**不体现在分数上**。层合成用的是
    `effective_score = 100 - score`，展开就是

        Σ (100 - s_i) · w_i  =  100·Σw_i  -  Σ s_i·w_i

    常数项不影响排序（IC 是秩相关），所以线上那一层**等价于**给反向维度
    一个**负权重**。

    因此硬编码一份全正权重当"现行"得到的是**不反向的对照组**，
    据此"优化权重"会把 V2.2 的反向直接抵消掉。返回负权重才是现行。
    """
    from src.mainline.config import load_config

    cfg = load_config()
    reversed_dims = {str(d) for d in (cfg.six_dim.reverse_dims or [])}
    out: dict[str, float] = {}
    for name, weight in (cfg.six_dim.weights or {}).items():
        value = float(weight)
        out[str(name)] = -value if str(name) in reversed_dims else value
    return out


def six_dim_combos() -> tuple[str, dict[str, dict[str, float]]]:
    """返回 `(基线方案名, {方案名: 自然分空间权重})`。

    基线**由配置算出**，不再硬编码 —— 硬编码的"现行六维"曾与实际口径
    相反（见 `current_six_weights` 的说明），会让自动建议给出
    "把反向取消掉"这种自毁结论。

    `iterate_mainline.py` 与 `auto_recommend.py` 共用本函数，
    避免两处口径漂移（这正是本项目已经踩过的坑）。
    """
    current = current_six_weights()
    baseline = "现行（自然分空间：反向维度为负权重）"
    combos: dict[str, dict[str, float]] = {baseline: current}
    if current:
        combos["不反向对照组（诊断用）"] = {k: abs(v) for k, v in current.items()}
        combos["六维等权（含反向）"] = {
            k: (1.0 if v >= 0 else -1.0) for k, v in current.items()}
    combos["只留正 IC 两项"] = {"prosperity": 1.0, "moneyflow": 1.0}
    combos["砍动量（去掉反向维度）"] = {
        k: v for k, v in current.items() if v > 0}
    return baseline, combos


def accumulation_combos() -> tuple[str, dict[str, dict[str, float]]]:
    """第二层同理：返回 `(基线方案名, {方案名: 权重})`。

    第二层不做反向，所以基线就是配置里的权重（`etf` 权重为 0，剔除）。
    """
    from src.mainline.config import load_config

    cfg = load_config()
    current = {str(k): float(v)
               for k, v in (cfg.accumulation.weights or {}).items()
               if float(v) > 0}
    baseline = "现行第二层"
    combos: dict[str, dict[str, float]] = {baseline: current}
    if current:
        combos["第二层等权"] = {k: 1.0 for k in current}
    combos["第二层只用 northbound"] = {"northbound": 1.0}
    return baseline, combos


def combo_plan() -> list[dict]:
    """所有权重实验方案，带显式来源标注。

    返回 `[{label, source, weights, baseline}]`，`source` ∈
    `{"six_dim", "accumulation"}`。

    **为什么不用字符串匹配来源**：老代码靠 `"第二层" in label` 判断这组权重
    是给哪一层的，改个中文名就会静默把第二层的权重套到六维上。这里用显式
    字段，名字随便改。
    """
    six_base, six = six_dim_combos()
    acc_base, acc = accumulation_combos()
    plan = [{"label": label, "source": "six_dim", "weights": weights,
             "baseline": label == six_base} for label, weights in six.items()]
    plan += [{"label": label, "source": "accumulation", "weights": weights,
              "baseline": label == acc_base} for label, weights in acc.items()]
    return plan


def weight_experiments(dims: dict[str, pd.DataFrame],
                       returns: dict[int, pd.DataFrame]) -> list[str]:
    """在**已落库的维度分**上试几种权重方案，看 IC 能不能被救回来。

    这一步**不需要重新打分**：层的分是各维度分的加权平均，而维度分都在
    `payload` 里。所以任何权重方案都能直接离线评估 —— 这是零成本的。
    """
    six = {name.split(".", 1)[1]: frame
           for name, frame in dims.items() if name.startswith("six_dim.")}
    acc = {name.split(".", 1)[1]: frame
           for name, frame in dims.items() if name.startswith("accumulation.")}
    if not six:
        return []
    # ⚠️ 全部方案都从**当前配置**推导，不再硬编码。
    # 老代码把"现行六维"写成 15/20/20/15/15/15（V2.2 之前的权重），
    # 于是报告里"现行"和"已落地 V2.2"同时出现、两个都标着"现行"；
    # 改配置之后报告不会自己跟上 —— 这类漂移是自动化建议最容易骗到自己的地方。
    baseline_name, combos = six_dim_combos()
    acc_baseline, acc_combos = accumulation_combos()
    schemes: dict[str, dict[str, float]] = dict(combos)
    # 把"基线"显示成带括号的说明，方便在长表里一眼找到
    schemes = {(f"{k}  ← 现行" if k == baseline_name else k): v
               for k, v in schemes.items()}

    print()
    print("=" * 96)
    print("权重方案实验（在已落库的维度分上离线合成，**无需重新打分**）")
    print("-" * 96)
    header = (f"{'方案':<44}{'H':>4}{'IC':>9}{'ICIR':>7}{'IC>0':>7}"
              f"{'t(非重叠)':>10}")
    print(header)
    lines: list[str] = []
    for label, weights in schemes.items():
        frame = composite(six, weights)
        for horizon in (10, 20, 60):
            series, _ = rank_ic(frame, returns[horizon])
            stat = summarize(series, horizon)
            if not stat:
                continue
            print(f"{label:<44}{horizon:>4}{stat['ic']:>9.4f}"
                  f"{stat['icir']:>7.3f}{stat['win'] * 100:>6.0f}%"
                  f"{stat['t_ne']:>10.2f}")
            lines.append(f"| {label} | {horizon} | {stat['ic']:.4f} | "
                         f"{stat['icir']:.3f} | {stat['win'] * 100:.0f}% | "
                         f"{stat['t_ne']:.2f} |")
        print()

    print("  第二层权重：")
    for label, weights in acc_combos.items():
        shown = f"{label}  ← 现行" if label == acc_baseline else label
        frame = composite(acc, weights)
        for horizon in (20, 60):
            series, _ = rank_ic(frame, returns[horizon])
            stat = summarize(series, horizon)
            if not stat:
                continue
            print(f"    {shown:<28}{horizon:>4}{stat['ic']:>9.4f}"
                  f"{stat['icir']:>7.3f}{stat['win'] * 100:>6.0f}%"
                  f"{stat['t_ne']:>10.2f}")
            lines.append(f"| 第二层·{shown} | {horizon} | {stat['ic']:.4f} | "
                         f"{stat['icir']:.3f} | {stat['win'] * 100:.0f}% | "
                         f"{stat['t_ne']:.2f} |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="因子 IC / ICIR")
    parser.add_argument("--start", default="20231009")
    parser.add_argument("--end", default="20260918")
    parser.add_argument("--report", default="docs/MAINLINE_IC_REPORT.md")
    args = parser.parse_args()

    scores, dims = load_scores(args.start, args.end)
    print(f"评分：{len(scores)} 个交易日 × {scores.shape[1]} 个板块"
          f"（{scores.index.min()} ~ {scores.index.max()}）")
    returns = load_returns(args.start, args.end)

    factors: dict[str, pd.DataFrame] = {"total": scores}
    for name in LAYERS:
        if name in dims and not dims[name].empty:
            factors[name] = dims[name]
    for name in sorted(dims):
        if name not in LAYERS and not dims[name].empty:
            factors[name] = dims[name]

    lines: list[str] = []
    print()
    print("=" * 96)
    print("逐因子 IC（Spearman 横截面）—— `t_ne` 用非重叠子样本，以它为准")
    print("-" * 96)
    header = (f"{'因子':<30}{'H':>4}{'交易日':>7}{'IC':>8}{'ICIR':>7}"
              f"{'IC>0':>7}{'t(重叠)':>9}{'t(非重叠)':>10}")
    print(header)
    lines.append("| 因子 | 持有期 | 交易日 | IC 均值 | ICIR | IC>0 占比 "
                 "| t(重叠) | t(非重叠) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, frame in factors.items():
        for horizon in HORIZONS:
            if horizon not in returns:
                continue
            series, _ = rank_ic(frame, returns[horizon])
            stat = summarize(series, horizon)
            if not stat:
                continue
            print(f"{name:<30}{horizon:>4}{stat['days']:>7}{stat['ic']:>8.4f}"
                  f"{stat['icir']:>7.3f}{stat['win'] * 100:>6.0f}%"
                  f"{stat['t']:>9.2f}{stat['t_ne']:>10.2f}")
            lines.append(
                f"| {name} | {horizon} | {stat['days']} | {stat['ic']:.4f} "
                f"| {stat['icir']:.3f} | {stat['win'] * 100:.0f}% "
                f"| {stat['t']:.2f} | {stat['t_ne']:.2f} |")
        print()

    # ---------- 分组价差（比 IC 更贴近"能不能用"） ----------
    print("=" * 96)
    print("按分数分 5 组的未来收益（第 5 组=最高分）")
    print("-" * 96)
    print(f"{'因子':<30}{'H':>4}{'最低分组':>10}{'第2组':>9}{'第3组':>9}"
          f"{'第4组':>9}{'最高分组':>10}{'高-低':>9}")
    spread_lines: list[str] = []
    for name in ("total", "six_dim", "accumulation"):
        if name not in factors:
            continue
        for horizon in (10, 20):
            spread, means = decile_spread(factors[name], returns[horizon])
            if any(math.isnan(v) for v in means):
                continue
            cells = "".join(f"{v * 100:>9.2f}" for v in means)
            print(f"{name:<30}{horizon:>4}{cells}{spread * 100:>8.2f}%")
            spread_lines.append(
                f"| {name} | {horizon} | "
                + " | ".join(f"{v * 100:.2f}%" for v in means)
                + f" | {spread * 100:.2f}% |")
        print()

    # ---------- 权重方案实验 ----------
    experiment_lines = weight_experiments(dims, returns)

    out = [f"# 主线因子 IC / ICIR 报告（{args.start} ~ {args.end}）\n",
           f"- 评分 {len(scores)} 个交易日 × {scores.shape[1]} 个板块\n",
           "- 收益：板块指数收盘到收盘；IC 为横截面 Spearman 秩相关\n",
           "- **`t(非重叠)` 才是可信的显著性**：H=20 时相邻日的 IC 高度自相关，\n"
           "  重叠样本会把 t 值吹大好几倍\n\n",
           "## 一、逐因子 IC\n\n"]
    out.extend(lines)
    out.append("\n## 二、按分数分 5 组的未来收益\n\n")
    out.append("| 因子 | 持有期 | 最低分组 | 第2组 | 第3组 | 第4组 | 最高分组 | 高-低 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    out.extend(spread_lines)
    if experiment_lines:
        out.append("\n## 三、权重方案实验（离线合成，无需重新打分）\n\n")
        out.append("| 方案 | 持有期 | IC | ICIR | IC>0 | t(非重叠) |")
        out.append("|---|---:|---:|---:|---:|---:|")
        out.extend(experiment_lines)
    target = ROOT / args.report
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"报告 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
