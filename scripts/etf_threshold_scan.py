"""标定用：ETF 放量门槛的 召回-精度 扫描。

## 为什么需要它

`breakout_amount_ratio` 目前是 **1.6**，但这个数是**按一个具体事件拍的**
（农业 562900 在启动窗口的 `amount` 倍数 1.54 / 1.98 / 1.87 / 2.17，
2.0 会把整段挡在外面），不是标定结果。它直接决定：

    门槛 ↓  → 召回的启动日更多（好），但日常波动也更容易越线（坏）

本脚本在**全历史上**逐日扫描，给出每个门槛下：

  - 有 ETF 信号的板块-日数量（信号量 / 告警压力的代理指标）
  - 其中二级（放量 + 净申购）的占比（更强的确认）
  - 与 1.6 相比的信号量倍数（"放宽到 X 要多看多少条"）
  - **农业真值窗口的召回**（2026-06-26 / 06-29 / 07-01 / 07-06 能否命中）

这不能替代真值事件驱动的标定（`configs/mainline_ground_truth.yaml`），
但它能把"放宽门槛的代价"从一个形容词变成一个数字。

## 用法

    .venv\\Scripts\\python.exe scripts\\etf_threshold_scan.py
    .venv\\Scripts\\python.exe scripts\\etf_threshold_scan.py --start 20240101
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402
from src.mainline.etf import board_etf_signals, load_mapping  # noqa: E402

#: 待比较的 (倍数门槛, 分位门槛)。`倍数=0` 表示**关掉倍数条件**，
#: 也就是"只看分位"的旁路口径 —— 现有代码里 `ratio >= 0` 恒真，
#: 于是 `level1` 退化成 `percentile >= 分位门槛`，正好等于旁路。
#:
#: 这样就能在**不改代码**的前提下量出旁路的代价：
#:     1.6:0.90  当前线上口径（合取）
#:     1.5:0.90  单纯放宽倍数
#:     0.0:0.95  纯旁路（只看分位 ≥95%）
#:     1.2:0.95  旁路 + 倍数地板
DEFAULT_GRID: tuple[tuple[float, float], ...] = (
    (1.6, 0.90), (1.5, 0.90), (0.0, 0.95), (1.2, 0.95),
)
#: 农业真值窗口（启动第一周）
TRUTH_DATES = ("20260626", "20260629", "20260701", "20260706")


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 放量门槛扫描")
    parser.add_argument("--start", default="20231001")
    parser.add_argument("--end", default="")
    parser.add_argument("--percentile", type=float, default=0.90,
                        help="分位门槛（默认与线上一致）")
    parser.add_argument("--truth-board", default="885812.TI",
                        help="真值事件归属的板块（默认农业种植）")
    parser.add_argument("--grid", default="",
                        help="自定义口径，形如 `1.6:0.90,0.0:0.95`"
                             "（倍数:分位，逗号分隔）")
    args = parser.parse_args()

    grid: list[tuple[float, float]] = list(DEFAULT_GRID)
    if args.grid:
        grid = []
        for item in args.grid.split(","):
            left, _, right = item.strip().partition(":")
            grid.append((float(left), float(right or 0.90)))

    cfg = load_config()
    store = MainlineDataStore(config=cfg)
    mapping = load_mapping(cfg)
    end = args.end or str(store._read(  # noqa: SLF001
        "SELECT MAX(trade_date) d FROM ml_etf")[0]["d"] or "")
    boards = store._read(  # noqa: SLF001
        "SELECT code, name FROM ml_board WHERE source = 'sector_crowding:list'")
    board_by_name = {str(r["name"]): str(r["code"]) for r in boards}

    # 只加载被映射到的 ETF：全量 700+ 只里绝大多数与任何板块都无关
    wanted = {str(short) for short in mapping.overrides}
    codes = [code for code in store.etf_codes()
             if code.split(".")[0] in wanted]
    bars = store.etf_bars(codes, start=args.start, end=end)
    # 每个代码 → 升序日期列表 + 日期→下标，避免逐日重建
    keys_of: dict[str, list[str]] = {}
    index_of: dict[str, dict[str, int]] = {}
    for code, rows in bars.items():
        marks = [str(row["trade_date"]) for row in rows]
        keys_of[code] = marks
        index_of[code] = {mark: i for i, mark in enumerate(marks)}
    dates = sorted({mark for marks in keys_of.values() for mark in marks})
    dates = [day for day in dates if args.start <= day <= end]
    print(f"区间 {args.start} ~ {end}：{len(dates)} 个交易日，"
          f"映射内 ETF {len(bars)} 只，映射 {len(mapping.overrides)} 条")

    tally: dict[tuple[float, float], dict[str, int]] = {
        key: {"days": 0, "l1": 0, "l2": 0} for key in grid}
    truth_hits: dict[tuple[float, float], list[str]] = {key: [] for key in grid}

    for date in dates:
        sliced: dict[str, list[dict]] = {}
        for code, rows in bars.items():
            i = index_of[code].get(date)
            if i is None:
                continue          # 该日这只 ETF 没有行情，就不参与当天聚合
            sliced[code] = rows[: i + 1]
        if not sliced:
            continue
        for ratio, percentile in grid:
            signals = board_etf_signals(
                sliced, mapping, board_by_name=board_by_name,
                window=int(cfg.etf.window), breakout_amount_ratio=ratio,
                breakout_percentile=percentile)
            hit = [s for s in signals.values() if s.level_of() > 0]
            if not hit:
                continue
            tally[(ratio, percentile)]["days"] += 1
            tally[(ratio, percentile)]["l1"] += len(hit)
            tally[(ratio, percentile)]["l2"] += sum(
                1 for s in hit if s.level_of() >= 2)
            # ⚠️ 真值命中必须**按板块判定**，不能"当天有任意板块有信号就算命中"
            # —— 那样每个门槛都会显示全中，把门槛差异完全掩盖掉（这正是本脚本
            # 第一版写错的地方，差点报出一个假的"门槛无关"结论）。
            if date in TRUTH_DATES:
                target = signals.get(args.truth_board)
                if target is not None and target.level_of() > 0:
                    truth_hits[(ratio, percentile)].append(date)

    print()
    print(f"{'倍数':>6}{'分位':>7}{'有信号日':>9}{'板块-日':>9}{'二级占比':>9}"
          f"{'相对基线':>10}   农业真值命中")
    print("-" * 96)
    base = tally[grid[0]]["l1"] or 1
    for key in grid:
        ratio, percentile = key
        row = tally[key]
        share = row["l2"] / row["l1"] * 100 if row["l1"] else 0.0
        label = "纯旁路" if ratio <= 0 else ""
        print(f"{ratio:>6.2f}{percentile:>7.2f}{row['days']:>9}{row['l1']:>9}"
              f"{share:>8.0f}%{row['l1'] / base:>9.2f}x   "
              f"{'、'.join(truth_hits[key]) or '（无）'} {label}")
    print()
    print("读法：`倍数=0` 表示关掉倍数条件（只剩分位），即「分位 ≥ 阈值 即算」的旁路。")
    print("      `相对基线` 的第一行（`--grid` 的第一项）是分母，默认 1.6:0.90 当前口径。")
    print("      `二级占比` 越高说明信号质量越好（放量 + 净申购才算二级）。")
    print("      农业真值窗口只有 4 天，命中数只是**召回下界**，不是全部真值事件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
