"""阈值扫描：告警线定在哪里最好（第 1 步「重建告警层」的决策工具）。

## 为什么不能拍一个阈值

"提高 medium 门槛"听起来对，但**提高门槛不一定提高准确率**：
分数与未来收益的关系可能只在极端分位才成立，也可能整体单调。两种情况
最优阈值完全不同。所以要在**已落库的分数**上扫一遍，看准确率与基准率
随阈值怎么变 —— 这一步零成本、不需要重新打分。

## 口径

对每个阈值 T 与每个持有期 H：

    选中集合 = { (板块, 日) : total >= T }
    准确率   = 选中集合里未来 H 日最大涨幅 >= gain 且 H 日收益 > 0 的比例
    基准率   = 同一批交易日 × 全池 324 板块随机取样的同一比例
    **提升** = 准确率 / 基准率     ← 唯一能判断"有没有用"的量
    日均条数 = 选中集合大小 / 交易日数   ← 决定会不会刷屏

同时给 `six_dim` / `accumulation` 的扫描，用于判断"第二层到底加不加分"。

用法：
    .venv\\Scripts\\python.exe scripts\\threshold_sweep.py --start 20251001
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
MAIN_DB = ROOT / "data" / "moss_finagent.db"
GRID = (35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0)


def load(start: str, end: str) -> tuple[dict[str, pd.DataFrame],
                                        dict[int, pd.DataFrame]]:
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    frames: dict[str, dict[str, dict[str, float]]] = {}
    for row in conn.execute(
            "SELECT trade_date, board_code, total, six_dim, accumulation"
            " FROM mainline_score WHERE trade_date BETWEEN ? AND ?",
            (start, end)):
        day, code = str(row["trade_date"]), str(row["board_code"])
        for key in ("total", "six_dim", "accumulation"):
            value = row[key]
            if value is None or float(value) == 0.0:
                continue
            frames.setdefault(key, {}).setdefault(day, {})[code] = float(value)
    conn.close()
    scores = {key: pd.DataFrame(value).T for key, value in frames.items()}

    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    wide = pd.DataFrame(
        [{"board_code": str(r["board_code"]), "trade_date": str(r["trade_date"]),
          "close": float(r["close"] or 0)} for r in rows]
    ).pivot(index="trade_date", columns="board_code", values="close").sort_index()
    returns = {h: wide.shift(-h) / wide - 1.0 for h in (10, 20)}
    return scores, returns


def main() -> int:
    parser = argparse.ArgumentParser(description="告警阈值扫描")
    parser.add_argument("--start", default="20231009")
    parser.add_argument("--end", default="20260918")
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--gain", type=float, default=8.0)
    args = parser.parse_args()

    scores, returns = load(args.start, args.end)
    ret = returns[args.horizon]
    print(f"窗口 {args.start} ~ {args.end}，持有期 {args.horizon} 日，"
          f"命中判据：最大涨幅 ≥{args.gain:g}% 且 {args.horizon} 日收益 >0")
    print()

    # 未来 H 日的「最大涨幅」：用逐日滚动最大值算
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", (args.start, args.end)).fetchall()
    conn.close()
    wide = pd.DataFrame(
        [{"board_code": str(r["board_code"]), "trade_date": str(r["trade_date"]),
          "close": float(r["close"] or 0)} for r in rows]
    ).pivot(index="trade_date", columns="board_code", values="close").sort_index()
    peak = wide.rolling(args.horizon, min_periods=1).max().shift(-args.horizon) \
        / wide - 1.0
    fwd_ret = wide.shift(-args.horizon) / wide - 1.0

    days = sorted(set(wide.index))
    print(f"交易日 {len(days)}")

    for key in ("total", "six_dim", "accumulation"):
        frame = scores.get(key)
        if frame is None or frame.empty:
            continue
        print()
        print("=" * 88)
        print(f"{key}：阈值 → 准确率 / 基准率 / 提升 / 日均条数")
        print("-" * 88)
        print(f"{'阈值':>6}{'选中条数':>10}{'日均':>7}{'准确率':>9}"
              f"{'基准率':>9}{'提升':>8}")
        for threshold in GRID:
            hit_n = 0
            picked_n = 0
            base_hit = 0
            base_n = 0
            used_days = 0
            for day in days:
                if day not in frame.index or day not in fwd_ret.index:
                    continue
                left = frame.loc[day].dropna()
                p = peak.loc[day].dropna() if day in peak.index else None
                r = fwd_ret.loc[day].dropna() if day in fwd_ret.index else None
                if p is None or r is None:
                    continue
                shared = left.index.intersection(p.index).intersection(r.index)
                if len(shared) < 20:
                    continue
                used_days += 1
                chosen = left[shared][left[shared] >= threshold].index
                if len(chosen):
                    picked_n += len(chosen)
                    hit_n += int(((p[chosen] >= args.gain / 100)
                                  & (r[chosen] > 0)).sum())
                base_n += len(shared)
                base_hit += int(((p[shared] >= args.gain / 100)
                                 & (r[shared] > 0)).sum())
            if not picked_n or not base_n:
                continue
            acc = hit_n / picked_n
            base = base_hit / base_n
            print(f"{threshold:>6.0f}{picked_n:>10}{picked_n / max(1, used_days):>7.1f}"
                  f"{acc * 100:>8.1f}%{base * 100:>8.1f}%"
                  f"{acc / base if base else 0:>7.2f}x")
    print()
    print("读法：**看「提升」列**。提升 ≈ 1.0 说明这个阈值只是把基准率换个说法；")
    print("      日均条数决定会不会刷屏（当前实测 11~16 条/天）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
