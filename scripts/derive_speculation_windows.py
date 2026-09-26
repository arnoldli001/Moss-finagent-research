"""从历史行情里**推导每个板块的炒作窗口（月-日区间）**。

## 为什么要这个脚本

用户澄清了"左侧两周冗余"的正确含义：

> 「窗口日期往前两周，比如历史上高度炒作时间是 **2.6-4.15**，
>   那窗口就是 2.6 − 14 个自然日，也就是 **1.23 就要放开**告警限制。」

所以窗口不是"哪些月份"，而是**一段具体的月-日区间**（2 月 6 日 到 4 月 15 日），
允许告警的区间 = **[起点 − 14 天, 终点]**。

本脚本就用历史数据把这段区间**推出来**：对每个板块，找出所有
"滚动 20/30 日涨幅 > 15%" 的启动段，取每段的 **(起始月-日, 结束月-日)**，
再把跨年重复出现的段合并成少数几条窗口。

## 口径

- 启动段：`advance[t] = 滚动 20 或 30 日涨幅 > 15%`；连续的 `True` 是一段；
- 段的**起** = 该段第一天，**止** = 该段最后一天；
- 合并：把各年的段按"月-日"投影到一年内，重叠或间隔 ≤ `--merge-days` 的合并；
- 只保留**至少出现过 `--min-years` 次**的窗口（避免把一次性行情当规律）。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/derive_speculation_windows.py \\
        --boards 885914.TI,885936.TI,885497.TI --out docs/MAINLINE_SPEC_WINDOWS.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
BIG = 0.15


def to_day(month: int, day: int) -> int:
    """把 (月, 日) 映射成"一年中的第几天"，便于比较与合并。"""
    return (date(2025, month, day) - date(2025, 1, 1)).days


def main() -> int:
    parser = argparse.ArgumentParser(description="推导炒作窗口")
    parser.add_argument("--boards", required=True)
    parser.add_argument("--windows", default="20,30")
    parser.add_argument("--merge-days", type=int, default=20)
    parser.add_argument("--min-years", type=int, default=2)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    names = {str(r["code"]): str(r["name"]) for r in
             conn.execute("SELECT code, name FROM ml_board")}

    emit("# 从历史行情推导的炒作窗口（月-日区间）")
    emit()
    emit(f"> 由 `scripts/derive_speculation_windows.py` 生成（只读）。")
    emit(f"> 启动段定义：滚动 {'/'.join(str(w) for w in windows)} 日涨幅 > "
         f"{BIG:.0%}；把各年的段投影到一年内，间隔 ≤ {args.merge_days} 天的合并。")
    emit()
    emit("⚠️ 样本只有约 3.7 年，所以「至少出现 N 次」这个筛选很重要："
         "单次行情不构成季节性。")
    emit()

    for code in [x.strip() for x in args.boards.split(",") if x.strip()]:
        rows = conn.execute(
            "SELECT b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " WHERE b.board_code = ? ORDER BY b.trade_date", (code,)).fetchall()
        if len(rows) < 60:
            emit(f"## {names.get(code, code)}：行情不足")
            continue
        days = [str(r["trade_date"]) for r in rows]
        close = np.asarray([float(r["close"] or 0.0) for r in rows])
        advance = np.zeros(len(close), dtype=bool)
        for window in windows:
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            advance |= np.isfinite(rolling) & (rolling > BIG)

        episodes: list[tuple[int, int, int, int, str]] = []
        pos = 0
        while pos < len(advance):
            if not advance[pos]:
                pos += 1
                continue
            end = pos
            while end + 1 < len(advance) and advance[end + 1]:
                end += 1
            start_day, end_day = days[pos], days[end]
            episodes.append((int(start_day[4:6]), int(start_day[6:8]),
                             int(end_day[4:6]), int(end_day[6:8]),
                             start_day[:4]))
            pos = end + 1

        emit(f"## {names.get(code, code)}（{code}）")
        emit()
        emit(f"共 {len(episodes)} 段：")
        emit()
        emit("| # | 年份 | 起始 | 结束 | 起(第几天) | 止(第几天) |")
        emit("|---:|---|---|---|---:|---:|")
        for order, (sm, sd, em, ed, year) in enumerate(episodes, 1):
            emit(f"| {order} | {year} | {sm:02d}-{sd:02d} | {em:02d}-{ed:02d} "
                 f"| {to_day(sm, sd)} | {to_day(em, ed)} |")
        emit()

        # 投影到一年内合并
        spans = sorted((to_day(sm, sd), to_day(em, ed), year)
                       for sm, sd, em, ed, year in episodes)
        merged: list[dict] = []
        for start, end, year in spans:
            if merged and start - merged[-1]["end"] <= args.merge_days:
                merged[-1]["end"] = max(merged[-1]["end"], end)
                merged[-1]["years"].add(year)
            else:
                merged.append({"start": start, "end": end, "years": {year}})
        # 年界回绕：末段与首段若跨年相邻，合并
        if len(merged) > 1:
            gap = (365 - merged[-1]["end"]) + merged[0]["start"]
            if gap <= args.merge_days:
                merged[0]["start"] = merged[-1]["start"]
                merged[0]["years"] |= merged[-1]["years"]
                merged.pop()
        kept = [m for m in merged if len(m["years"]) >= args.min_years]
        emit(f"合并后得到 **{len(merged)}** 条窗口，其中至少在 "
             f"{args.min_years} 个不同年份出现过的 **{len(kept)}** 条：")
        emit()
        emit("| 窗口(月-日) | 出现年份数 | 年份 | 允许告警区间（起点 − 14 天） |")
        emit("|---|---:|---|---|")
        for item in merged:
            start = date(2025, 1, 1) + timedelta(days=item["start"])
            end = date(2025, 1, 1) + timedelta(days=item["end"])
            lead = start - timedelta(days=14)
            mark = "" if item in kept else "（次数不足，未采纳）"
            emit(f"| {start.strftime('%m-%d')} ~ {end.strftime('%m-%d')} "
                 f"| {len(item['years'])} | {'、'.join(sorted(item['years']))} "
                 f"| **{lead.strftime('%m-%d')} ~ {end.strftime('%m-%d')}** {mark} |")
        emit()
        if kept:
            parts = []
            for item in kept:
                start = date(2025, 1, 1) + timedelta(days=item["start"])
                end = date(2025, 1, 1) + timedelta(days=item["end"])
                parts.append(f'{{start: "{start.strftime("%m-%d")}", '
                             f'end: "{end.strftime("%m-%d")}"}}')
            emit("可直接粘进配置的 `windows`：")
            emit()
            emit("```yaml")
            emit(f'  windows: [{", ".join(parts)}]')
            emit("```")
        emit()

    conn.close()
    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
