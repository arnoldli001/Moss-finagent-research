"""误报率 vs 分数：**提高门限到底能不能降误报？**

## 用户的立场（决定了这里该优化什么）

> 提前太早报可以提高点门限吗？早点不是问题，可以人工持续观测转折点再进，
> 重点是**误报影响大**。

所以目标不是"抓得早"，而是**降误报**。误报的定义沿用用户先前给的口径：

    命中 = 告警后 **20 日收益 > 2%**（`--hit-return` 可调）
    误报 = 20 日收益 ≤ 2%
    没报出来的只算**漏报**，不算失败

## 要回答的唯一问题

**"分数"与"命中率"是不是单调关系？**

- 若是：提高门限就能降误报，而且可以按目标误报率**反推**门限值；
- 若否（各分数档命中率差不多）：提高门限只会同时砍掉命中与误报，
  净收益为零甚至为负 —— 那就该换别的办法（换因子/加过滤条件）。

## 口径（避免版本混淆）

用 V2.2 备份的 payload **重算当前口径的 `total`**
（`aware` 80/20 + 兑现加分，即 V2.4/V2.5 的算法），
这样在**全量 696 天**上就能按现行尺度评估，不必等重打分。
（直接用备份里存的 `total` 会混入旧的 50/50 与"腰斩"缺陷。）

只读、不写库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
WINDOWS = (("20231009", "20240806", "旧"), ("20240901", "20250930", "中"),
           ("20251001", "20260918", "新"))
#: 分数分箱（当前口径 0-100）
BINS = (45.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0, 90.0, 100.0)


def load(table: str, start: str, end: str, six_weight: float) -> pd.DataFrame:
    """候选池 + **按当前口径重算** 的 `total`。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        six = float(payload.get("six_dim_score") or 0.0)
        acc = float(payload.get("accumulation_score") or 0.0)
        six_ok = acc_ok = False
        for layer in (payload.get("layers") or []):
            key = str(layer.get("key") or "")
            if key == "six_dim":
                six_ok = bool(layer.get("available"))
            elif key == "accumulation":
                acc_ok = bool(layer.get("available"))
        parts: list[tuple[float, float]] = []
        if six_ok:
            parts.append((six, six_weight))
        if acc_ok:
            parts.append((acc, 100.0 - six_weight))
        weight = sum(item[1] for item in parts)
        base = (sum(value * w for value, w in parts) / weight
                if weight > 0 else six)
        total = min(100.0, base + float(payload.get("etf_bonus") or 0.0)
                    + float(payload.get("gate_bonus") or 0.0))
        records.append({"day": str(row["trade_date"]),
                        "code": str(row["board_code"]),
                        "total": total,
                        "six": six})
    return pd.DataFrame(records)


def closes(start: str, end: str) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    frame = pd.read_sql_query(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", conn, params=(start, end))
    conn.close()
    return frame.pivot(index="trade_date", columns="board_code",
                       values="close").sort_index()


def main() -> int:
    parser = argparse.ArgumentParser(description="误报率 vs 分数")
    parser.add_argument("--table", default="mainline_score_bak_20260921_2015")
    parser.add_argument("--six-weight", type=float, default=80.0)
    parser.add_argument("--hit-return", type=float, default=2.0,
                        help="命中判据：20 日收益 > 该值（%%）")
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 误报率 vs 分数：提高门限能不能降误报？")
    emit()
    emit(f"> 由 `scripts/false_positive_report.py` 生成，表 `{args.table}`"
         f"（`total` 已按**当前口径**重算：aware {args.six_weight:g}/"
         f"{100 - args.six_weight:g} + 兑现加分）。")
    emit(f"> 命中 = 未来 {args.horizon} 日收益 > {args.hit_return:g}%；"
         "误报 = 未命中。**没报出来的算漏报，不计入误报。**")
    emit()

    pooled: dict[float, list[float]] = {}
    for start, end, label in WINDOWS:
        panel = load(args.table, start, end, args.six_weight)
        close = closes(start, end)
        if panel.empty or close.empty:
            continue
        forward = (close.shift(-args.horizon) / close - 1.0) * 100.0
        emit(f"## {label} 窗口 {start}~{end}")
        emit()
        emit("| 分数档 | 样本 | 命中率(>2%) | **误报率** | 未来收益均值 | 中位 |")
        emit("|---|---:|---:|---:|---:|---:|")
        for low, high in zip(BINS[:-1], BINS[1:], strict=True):
            chosen = panel[(panel["total"] >= low) & (panel["total"] < high)]
            if chosen.empty:
                continue
            values: list[float] = []
            for _, row in chosen.iterrows():
                day, code = row["day"], row["code"]
                if day not in forward.index:
                    continue
                value = forward.at[day, code] if code in forward.columns else np.nan
                if value == value:
                    values.append(float(value))
            if len(values) < 30:
                continue
            arr = np.array(values)
            hit = float((arr > args.hit_return).mean() * 100)
            emit(f"| {low:g}~{high:g} | {len(arr)} | {hit:.0f}% "
                 f"| **{100 - hit:.0f}%** | {arr.mean():+.2f}% "
                 f"| {np.median(arr):+.2f}% |")
            pooled.setdefault(low, []).extend(values)
        emit()

    # 汇总：把三个窗口合起来看单调性（单窗口容易看成巧合）
    emit("## 三窗口合并：分数与命中率是不是单调关系")
    emit()
    emit("| 分数档 | 样本 | 命中率 | 误报率 | 未来收益均值 |")
    emit("|---|---:|---:|---:|---:|")
    trend: list[tuple[float, int, float]] = []
    for low in sorted(pooled):
        arr = np.array(pooled[low])
        if len(arr) < 200:
            continue
        hit = float((arr > args.hit_return).mean() * 100)
        emit(f"| {low:g}~ | {len(arr)} | {hit:.0f}% | {100 - hit:.0f}% "
             f"| {arr.mean():+.2f}% |")
        trend.append((low, len(arr), hit))
    emit()
    if len(trend) >= 3:
        hits = [item[2] for item in trend]
        rising = sum(1 for a, b in zip(hits, hits[1:], strict=False) if b > a)
        emit(f"- 命中率随分数**上升**的相邻档数：{rising}/{len(hits) - 1}")
        emit(f"- 最低档命中 {hits[0]:.0f}% → 最高档命中 {hits[-1]:.0f}%"
             f"（差 {hits[-1] - hits[0]:+.0f} 个百分点）")
        if rising >= len(hits) - 1:
            emit("- ✅ **单调上升**：提高门限确实能降误报，可以按目标误报率反推门限")
        elif rising >= (len(hits) - 1) * 0.6:
            emit("- ⚠️ **大体上升但不单调**：提高门限能降误报，但边际递减")
        else:
            emit("- 🚨 **基本不随分数变化**：提高门限只会同时砍掉命中与误报，"
                 "净收益接近零 —— 该换因子/加别的过滤条件，而不是提门槛")
    emit()

    emit("## 四、误报主要是「选错板块」还是「市况不好」？")
    emit()
    emit("上面看到误报率随分数只从 58% 降到 40%（还丢掉 95% 样本），"
         "说明分数区分力弱。那就该问一个更基本的问题：")
    emit()
    emit("**同一个板块分数，在不同市况下的命中率差多少？**")
    emit()
    emit("市况代理：当日**全市场板块**近 20 日收益的横截面中位数"
         "（`breadth`）。它高 = 普涨，低 = 普跌。")
    emit()
    emit("若命中率随 `breadth` 大幅变化，说明该加的是**市况闸门**，"
         "而不是继续提分数门槛 —— 后者是在用板块选择去解决市况问题。")
    emit()
    pooled_state: dict[str, list[float]] = {}
    for start, end, label in WINDOWS:
        panel = load(args.table, start, end, args.six_weight)
        close = closes(start, end)
        if panel.empty or close.empty:
            continue
        forward = (close.shift(-args.horizon) / close - 1.0) * 100.0
        breadth = ((close / close.shift(args.horizon) - 1.0) * 100.0).median(
            axis=1)
        # 按该窗口内 breadth 的**分位**切成四档；用分位而不是绝对值，
        # 三个窗口才能合并（绝对值量纲不同，合并会把不同含义的桶混在一起
        # —— 第一版就是这么错的：同名桶跨窗口被 pandas 合并成一个 key，
        # 于是"最差市况 16% / 最好市况 69%"是假的）。
        rank = breadth.rank(pct=True)
        emit(f"### {label}")
        emit()
        emit("| 市况分位 | 样本 | 命中率 | 误报率 | 未来收益均值 |")
        emit("|---|---:|---:|---:|---:|")
        for name, low, high in (("最低25%", 0.0, 0.25), ("25~50%", 0.25, 0.5),
                                ("50~75%", 0.5, 0.75), ("最高25%", 0.75, 1.01)):
            days = [day for day in rank.index
                    if low <= rank[day] < high]
            values: list[float] = []
            for _, row in panel[panel["day"].isin(days)].iterrows():
                day, code = row["day"], row["code"]
                if day not in forward.index or code not in forward.columns:
                    continue
                value = forward.at[day, code]
                if value == value:
                    values.append(float(value))
            if len(values) < 100:
                continue
            arr = np.array(values)
            hit = float((arr > args.hit_return).mean() * 100)
            emit(f"| {name} | {len(arr)} | {hit:.0f}% | {100 - hit:.0f}% "
                 f"| {arr.mean():+.2f}% |")
            pooled_state.setdefault(name, []).extend(values)
        emit()
    if pooled_state:
        emit("### 三窗口合并")
        emit()
        emit("| 市况 | 样本 | 命中率 | 误报率 | 未来收益均值 |")
        emit("|---|---:|---:|---:|---:|")
        rates: list[float] = []
        for name, values in pooled_state.items():
            arr = np.array(values)
            hit = float((arr > args.hit_return).mean() * 100)
            rates.append(hit)
            emit(f"| {name} | {len(arr)} | {hit:.0f}% | {100 - hit:.0f}% "
                 f"| {arr.mean():+.2f}% |")
        if len(rates) >= 2:
            emit()
            emit(f"- 最差市况命中 **{min(rates):.0f}%**，最好市况命中 "
                 f"**{max(rates):.0f}%**（差 **{max(rates) - min(rates):+.0f}** "
                 "个百分点）")
        emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
