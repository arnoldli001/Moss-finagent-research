"""稀疏信号的正确检验：**有信号的板块 vs 池内其余**，而不是算 IC。

## 为什么不能对它算 IC

`leader.seat`（龙虎榜席位确认）在候选池里只有 **11~17 个/天**有值
（62 个候选里约 1/5），而 `factor_ic_report.rank_ic` 要求**每天 ≥20 个共有
样本**，否则整天跳过。于是：

- 旧窗口（2023-10~2024-08）204 天里只有 **14 天**能算 IC；
- 新窗口 211 天里有 134 天能算。

而且**这不是数据缺口能解决的**：2024 年几个月 `ml_seat` 是齐的，
席位确认数仍然只有中位 11~17/天。所以这是**因子本身稀疏**，
补数据也只能把"0 天"变成"11 天"，过不了 20 的门槛。

**稀疏信号不该用 IC 检验。** IC 问的是"能不能给整个横截面排序"，
而一个只有 1/5 板块有值的信号**本来就不该**干这件事。
它该回答的是另一个问题：

    拿到席位确认的那 11~17 个板块，未来 20/60 日收益是否**高于池内其余**？

这是一个**条件均值 / 命中率**问题，样本量按"天"算（每天一个对比），
不受"每天要有 20 个样本"的限制 —— 只要每天有 ≥`--min-per-day` 个确认板块
就能进统计。

## 口径

- 对照组 = **当天候选池里没有席位确认的板块**（同时给"整个候选池"做参考）；
- 每个交易日算一个差值（信号组均值 − 对照组均值），再对**天**做统计；
- 日报酬重叠（H=20）会让 t 值虚高，所以**同时报胜率与天数**，
  并以"两个窗口同号"为采用判据。
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

from scripts.factor_ic_report import CACHE_DB  # noqa: E402

DB = ROOT / "data" / "moss_finagent.db"
WINDOWS = (("20231009", "20240806", "旧"), ("20251001", "20260918", "新"))
HORIZONS = (20, 60)


def panel(table: str, factor: str, start: str, end: str) -> pd.DataFrame:
    """候选池长表：`[day, code, signal]`（signal 只有在因子可用时才 > 0）。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
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
        layer_key, _, dim_key = factor.partition(".")
        value = None
        for layer in (payload.get("layers") or []):
            if str(layer.get("key")) != layer_key:
                continue
            for dim in (layer.get("dimensions") or []):
                if str(dim.get("key")) == dim_key and dim.get("available"):
                    value = float(dim.get("score") or 0.0)
        records.append({"day": str(row["trade_date"]),
                        "code": str(row["board_code"]),
                        "signal": value})
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
    parser = argparse.ArgumentParser(description="稀疏信号检验")
    parser.add_argument("--table", default="mainline_score_bak_20260921_2015")
    parser.add_argument("--factor", default="leader.seat")
    parser.add_argument("--min-per-day", type=int, default=3,
                        help="当天至少要有几个信号板块才进统计")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit(f"# 稀疏信号检验：`{args.factor}`")
    emit()
    emit(f"> 由 `scripts/sparse_signal_test.py` 生成，表 `{args.table}`，"
         f"每天至少 {args.min_per_day} 个信号板块。")
    emit("> **为什么不用 IC**：该因子每天只有约 1/5 的候选板块有值，"
         "过不了 `rank_ic` 的『每天 ≥20 个共有样本』门槛"
         "（旧窗口只有 14/204 天能算）。稀疏信号该检验的是"
         "『有信号的板块是否强于池内其余』，样本量按**天**算。")
    emit()

    for start, end, label in WINDOWS:
        data = panel(args.table, args.factor, start, end)
        close = closes(start, end)
        if data.empty:
            emit(f"- {label} 窗口无数据")
            continue
        emit(f"## {label} 窗口 {start}~{end}")
        emit()
        emit("| 持有期 | 信号组收益 | 对照组收益 | 候选池收益 | "
             "差值(信号−对照) | 差值t(上界) | 胜率 | 天数 | 日均信号数 |")
        emit("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for horizon in HORIZONS:
            ret = close.shift(-horizon) / close - 1.0
            sig: list[float] = []
            ctrl: list[float] = []
            pool: list[float] = []
            sizes: list[int] = []
            for day, group in data.groupby("day", sort=True):
                if day not in ret.index:
                    continue
                row = ret.loc[day]
                codes = group["code"].tolist()
                values = row.reindex(codes)
                mask = group["signal"].notna().to_numpy()
                hit = values[mask].dropna()
                miss = values[~mask].dropna()
                if len(hit) < args.min_per_day or len(miss) < args.min_per_day:
                    continue
                sig.append(float(hit.mean()))
                ctrl.append(float(miss.mean()))
                pool.append(float(values.dropna().mean()))
                sizes.append(len(hit))
            if not sig:
                emit(f"| {horizon} | — | — | — | — | — | — | 0 | — |")
                continue
            diff = np.array(sig) - np.array(ctrl)
            se = diff.std(ddof=1) / np.sqrt(len(diff)) if len(diff) > 1 else np.nan
            emit(f"| {horizon} | {np.mean(sig) * 100:+.2f}% "
                 f"| {np.mean(ctrl) * 100:+.2f}% | {np.mean(pool) * 100:+.2f}% "
                 f"| **{diff.mean() * 100:+.2f}pp** | {se * 100:.2f}pp "
                 f"| {(diff > 0).mean() * 100:.0f}% | {len(diff)} "
                 f"| {np.mean(sizes):.1f} |")
        emit()

    emit("判读：两个窗口、两个持有期的『差值』**同号且量级相近**才可采用。"
         "日度差值不独立（持有期重叠），`t` 只是上界参考，以胜率与天数为主。")
    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
