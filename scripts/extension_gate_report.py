"""波段顶门限：分数高 + 指数已经涨了很多 → 大概率是追高，该拦掉。

## 用户的判断

> 分数过高，指数又近 20 日涨幅大于 10% 的可能是波段顶，成交量大被误报了，
> 可以加个门限，近 10 日或 20 日概念指数涨幅大于 xx%，这个你可以根据实际
> 数据，分析给出个合理的门限值。

这个判断可以直接检验：**"已经涨了多少"与"后面还能不能涨"是什么关系。**
如果超过某个涨幅之后，未来 20/60 日收益转负（且胜率跌破 50%），
那这个涨幅就是该拦的门限 —— 而且是数据给的，不是我拍的。

## 口径

对每个（板块，交易日）：

    prior_gain_10 = close[D] / close[D-10] - 1
    prior_gain_20 = close[D] / close[D-20] - 1
    forward_20    = close[D+20] / close[D] - 1     ← 告警后才有意义的收益
    forward_60    = close[D+60] / close[D] - 1

再看：

1. **无条件**：按 prior_gain 分桶，未来收益随涨幅怎么变；
2. **有条件（高分段）**：只取 `total` 高于某个高分位的日子 ——
   因为门限是加在"分数高"的前提上的，无条件曲线会把低分样本混进来。

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
#: 分桶边界（近 20 日涨幅，%）。**高端要细**：门限若存在，一定在高端，
#: 而 >15% 的样本本来就少，粗桶会把它们并掉、看不出拐点。
BUCKETS = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 50.0, 1e9)
#: 一个桶里至少这么多样本才输出（高端样本天然少，阈值给 20 而不是 50）
MIN_SAMPLES = 20
#: "分数高"的定义：当日候选池内 `total` 的百分位
HIGH_PCT = 0.80


def board_closes(start: str, end: str) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    frame = pd.read_sql_query(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", conn, params=(start, end))
    conn.close()
    return frame.pivot(index="trade_date", columns="board_code",
                       values="close").sort_index()


def scores(table: str, start: str, end: str) -> pd.DataFrame:
    """候选池的 `total`（宽表）与当日百分位。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, total, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    data: dict[str, dict[str, float]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        data.setdefault(str(row["trade_date"]), {})[str(row["board_code"])] = \
            float(row["total"] or 0.0)
    frame = pd.DataFrame(data).T
    return frame.rank(axis=1, pct=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="波段顶门限分析")
    parser.add_argument("--table", default="mainline_score_bak_20260921_2015")
    parser.add_argument("--horizons", default="20,60")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 波段顶门限：分数高 + 指数已经涨很多 → 该拦还是该报？")
    emit()
    emit(f"> 由 `scripts/extension_gate_report.py` 生成，表 `{args.table}`。")
    emit(f"> 分桶按**近 20 日涨幅**；『高分段』= 当日候选池内 `total` 百分位"
         f" ≥ {HIGH_PCT:.0%}。")
    emit()

    for start, end, label in WINDOWS:
        close = board_closes(start, end)
        pct = scores(args.table, start, end)
        if close.empty or pct.empty:
            continue
        common_days = close.index.intersection(pct.index)
        gain20 = (close / close.shift(20) - 1.0) * 100.0
        gain10 = (close / close.shift(10) - 1.0) * 100.0
        fwd = {h: (close.shift(-h) / close - 1.0) * 100.0 for h in horizons}
        emit(f"## {label} 窗口 {start}~{end}")
        emit()
        for horizon in horizons:
            forward = fwd[horizon]
            emit(f"### 未来 {horizon} 日收益（按近 20 日涨幅分桶）")
            emit()
            emit("| 近20日涨幅桶 | 高分段样本 | 高分段未来收益 | 高分段胜率 |")
            emit("|---|---:|---:|---:|")
            cand = pd.DataFrame(False, index=pct.index, columns=pct.columns)
            cand.loc[common_days, :] = pct.loc[common_days, :].notna()
            for low, high in zip(BUCKETS[:-1], BUCKETS[1:], strict=True):
                mask = (gain20 >= low) & (gain20 < high)
                chosen = mask & cand & (pct >= HIGH_PCT)
                values = forward.where(chosen).stack().dropna()
                if len(values) < MIN_SAMPLES:
                    continue
                emit(f"| {low:g}~{high:g}% | {len(values)} "
                     f"| {values.mean():+.2f}% "
                     f"| {(values > 0).mean() * 100:.0f}% |")
            emit()
        # 近 10 日涨幅同样看一遍（用户提到 10 日或 20 日都可以）
        emit("### 近 10 日涨幅（高分段的未来 20 日收益）")
        emit()
        emit("| 近10日涨幅桶 | 样本 | 未来20日收益 | 胜率 |")
        emit("|---|---:|---:|---:|")
        cand = pd.DataFrame(False, index=pct.index, columns=pct.columns)
        cand.loc[common_days, :] = pct.loc[common_days, :].notna()
        for low, high in zip(BUCKETS[:-1], BUCKETS[1:], strict=True):
            mask = (gain10 >= low) & (gain10 < high)
            chosen = mask & cand & (pct >= HIGH_PCT)
            values = fwd[horizons[0]].where(chosen).stack().dropna()
            if len(values) < MIN_SAMPLES:
                continue
            emit(f"| {low:g}~{high:g}% | {len(values)} | {values.mean():+.2f}% "
                 f"| {(values > 0).mean() * 100:.0f}% |")
        emit()

    emit("## 判读")
    emit()
    emit("门限取自**未来收益均值转负**（或胜率跌破 50%）的那个涨幅桶的**下沿**。"
         "注意要三个窗口同向才算稳（本项目反复踩过单窗口假象）。")
    emit()

    # ---------- 三、动量回落：涨幅大 **且** 已经掉头 ----------
    emit("## 三、动量回落检验：该拦的不是「涨得多」，而是「涨得多**且已掉头**」")
    emit()
    emit("用户选的方向：不按涨幅硬拦，改成测「涨幅大 + 动量回落」。两个口径都测：")
    emit()
    emit("- **指数回落**：`close[D] / max(close[D-9..D]) - 1`（距近 10 日高点回撤）")
    emit("- **分数回落**：`total[D] - total[D-3]`（自身分数三日变化）")
    emit()
    for start, end, label in WINDOWS:
        close = board_closes(start, end)
        pct = scores(args.table, start, end)
        if close.empty or pct.empty:
            continue
        common_days = close.index.intersection(pct.index)
        gain20 = (close / close.shift(20) - 1.0) * 100.0
        drawdown = (close / close.rolling(10, min_periods=5).max() - 1.0) * 100.0
        fwd20 = (close.shift(-20) / close - 1.0) * 100.0
        score_chg = pct - pct.shift(3)
        cand = pd.DataFrame(False, index=pct.index, columns=pct.columns)
        cand.loc[common_days, :] = pct.loc[common_days, :].notna()
        base = cand & (pct >= HIGH_PCT) & (gain20 >= 10.0)
        emit(f"### {label}（样本 = 候选 ∩ 分数≥{HIGH_PCT:.0%} ∩ 近20日涨幅≥10%）")
        emit()
        emit("| 回落口径 | 分档 | 样本 | 未来20日收益 | 胜率 |")
        emit("|---|---|---:|---:|---:|")
        for name, frame, cuts in (
                ("指数回撤", drawdown,
                 ((-1e9, -5.0), (-5.0, -2.0), (-2.0, 1e9))),
                ("分数3日变化", score_chg,
                 ((-1e9, -3.0), (-3.0, 3.0), (3.0, 1e9)))):
            for low, high in cuts:
                chosen = base & (frame >= low) & (frame < high)
                values = fwd20.where(chosen).stack().dropna()
                if len(values) < MIN_SAMPLES:
                    continue
                emit(f"| {name} | {low:g}~{high:g} | {len(values)} "
                     f"| {values.mean():+.2f}% "
                     f"| {(values > 0).mean() * 100:.0f}% |")
        emit()
    emit("**判读**：若「回落」档的未来收益明显差于「仍在高位」档，"
         "说明该拦的是**动量转向**（涨得多且掉头），而不是涨幅本身；"
         "若两档差不多，说明连「掉头」也不该拦，就保持不加门限。")
    emit()

    # ---------- 四、换个问法：不是"最终涨不涨"，而是"中间会不会先痛" ----------
    emit("## 四、换个问法：最大不利偏移（MAE）——「追高会不会先挨一拳」")
    emit()
    emit("均值收益会掩盖体验：一个板块未来 20 日**最终**涨 2%，"
         "但中途先跌 15%，持仓体验就是「在顶部被套」。")
    emit()
    emit("所以再算一次 **MAE = min(close[D+1..D+20]) / close[D] - 1**"
         "（未来 20 日内最差的一次浮亏），按近 20 日涨幅分桶。")
    emit()
    for start, end, label in WINDOWS:
        close = board_closes(start, end)
        pct = scores(args.table, start, end)
        if close.empty or pct.empty:
            continue
        common_days = close.index.intersection(pct.index)
        gain20 = (close / close.shift(20) - 1.0) * 100.0
        # 未来 20 日内的最低收盘（这是**结果**，本来就该看未来）
        future_low = close.shift(-1).rolling(20, min_periods=5).min().shift(-19)
        mae = (future_low / close - 1.0) * 100.0
        cand = pd.DataFrame(False, index=pct.index, columns=pct.columns)
        cand.loc[common_days, :] = pct.loc[common_days, :].notna()
        base = cand & (pct >= HIGH_PCT)
        emit(f"### {label}")
        emit()
        emit("| 近20日涨幅 | 样本 | 最差浮亏(中位) | 最差浮亏(均值) "
             "| 浮亏 >10% 的比例 |")
        emit("|---|---:|---:|---:|---:|")
        for low, high in zip(BUCKETS[:-1], BUCKETS[1:], strict=True):
            chosen = base & (gain20 >= low) & (gain20 < high)
            values = mae.where(chosen).stack().dropna()
            if len(values) < MIN_SAMPLES:
                continue
            emit(f"| {low:g}~{high:g}% | {len(values)} "
                 f"| {values.median():.2f}% | {values.mean():.2f}% "
                 f"| {(values <= -10).mean() * 100:.0f}% |")
        emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
