"""验收：2026-06~07 农业 ETF 放量能否触发一级/二级异动并拿到加分。

## 这条脚本钉住的是哪次事故

2026 年 6 月底到 7 月，「农业ETF易方达」`562900.SH` 出现历史级成交额：

    日期        成交额(手)   近 60 日中位   倍数    份额变化
    2026-06-26   309,549      177,262      1.75     —
    2026-06-29   393,273      177,262      2.22    -3.0%
    2026-07-01   359,732      177,262      2.03    -3.1%
    2026-07-06   403,393      177,262      2.28    +6.31%

而主线模块对农业板块**完全没有反应**。两条独立的原因，本脚本逐条验收：

  ① 映射表把 ETF 映射到申万一级行业名（`农林牧渔`），不在 324 个概念板块池里
     → `etf` 维度对 97.8% 的板块永久不可用；
  ② 旧口径要求"放量 **且** 净申购同时成立" → 06-29 / 07-01 两天份额是
     净赎回（折价套利），被整条丢掉，只剩 07-06 一天。

## 用法

    .venv\\Scripts\\python.exe scripts\\verify_etf_breakout_case.py
    .venv\\Scripts\\python.exe scripts\\verify_etf_breakout_case.py --board 885812.TI
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402
from src.mainline.etf import (  # noqa: E402
    LEVEL_LABELS,
    board_etf_signals,
    breakout_bonus,
    load_mapping,
)

#: 真值事件窗口：农业启动的第一周。06-26 是放量的第一天。
CASE_DATES = ("20260626", "20260629", "20260630", "20260701",
              "20260702", "20260703", "20260706", "20260707")
DEFAULT_BOARD = "885812.TI"          # 农业种植


def main() -> int:
    parser = argparse.ArgumentParser(description="验收农业 ETF 放量事件")
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--dates", default=",".join(CASE_DATES))
    args = parser.parse_args()
    dates = [item.strip() for item in args.dates.split(",") if item.strip()]

    cfg = load_config()
    store = MainlineDataStore(config=cfg)
    mapping = load_mapping(cfg)
    print(f"映射表：keywords={len(mapping.keywords)} "
          f"overrides={len(mapping.overrides)} gap={mapping.gap!r}")

    boards = store._read(  # noqa: SLF001 运维脚本，直接读本地仓
        "SELECT code, name FROM ml_board WHERE source = 'sector_crowding:list'")
    board_by_name = {str(r["name"]): str(r["code"]) for r in boards}
    name_of = {str(r["code"]): str(r["name"]) for r in boards}
    if args.board not in name_of:
        print(f"❌ 板块 {args.board} 不在池内")
        return 2
    print(f"目标板块：{args.board} {name_of[args.board]}")

    codes = store.etf_codes()
    start = str(int(dates[0]) - 20000)     # 多取一段用于 60 日窗口
    bars = store.etf_bars(codes, start=start, end=dates[-1])
    print(f"本地 ETF {len(codes)} 只，区间内有行情 {len(bars)} 只")

    print()
    print(f"{'日期':<10}{'级别':<22}{'加分':>6}  "
          f"{'放大':>6}{'分位':>7}{'份额':>9}  ETF")
    print("-" * 96)
    hits = 0
    for date in dates:
        sliced = {code: [r for r in rows if str(r["trade_date"]) <= date]
                  for code, rows in bars.items()}
        sliced = {code: rows for code, rows in sliced.items() if rows}
        signals = board_etf_signals(
            sliced, mapping, board_by_name=board_by_name,
            window=int(cfg.etf.window),
            breakout_amount_ratio=float(cfg.etf.breakout_amount_ratio),
            breakout_percentile=float(cfg.etf.breakout_percentile))
        signal = signals.get(args.board)
        if signal is None:
            print(f"{date:<10}{'（无信号）':<22}")
            continue
        level = signal.level_of()
        bonus = breakout_bonus(signal, config=cfg)
        if level > 0:
            hits += 1
        share = ("—" if signal.share_change is None
                 else f"{signal.share_change * 100:+.2f}%")
        print(f"{date:<10}{LEVEL_LABELS[level]:<22}{bonus:>6.1f}  "
              f"{(signal.amount_ratio or 0):>6.2f}"
              f"{(signal.amount_percentile or 0) * 100:>6.0f}%"
              f"{share:>9}  {signal.etf_count} 只")
        for reason in signal.reasons[:2]:
            print(f"{'':<18}· {reason}")

    print()
    print("=" * 96)
    print(f"命中（级别 > 0）的交易日：{hits}/{len(dates)}")
    if hits == 0:
        print("❌ 一次都没触发 —— 映射或阈值仍然不对")
        return 1
    if hits < len(dates):
        print(f"⚠️ 有 {len(dates) - hits} 天没触发，逐日核对上面的行")
    print("✅ 农业启动窗口至少有一天能触发 ETF 异动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
