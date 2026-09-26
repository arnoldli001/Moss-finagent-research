"""补全板块指数日线（`ml_board_bar`）。

## 为什么需要它

实测（2026-09-20）`ml_board_bar` 的数据**严重残缺**：在最近 12 个月
（2025-09-01~2026-09-18，255 个交易日）里，

    K线 200-260 根（完整）      仅  48 个板块
    K线  50-119 根             1305 个板块
    K线   1-49 根               631 个板块
    完全没有数据                 243 个板块

板块数据要么止于 2025-11-28，要么从 2026-09-15 才开始。这直接废掉了任何
"个股 × 板块"走势相关性计算：两条序列的时间段不相交，取末尾 240 个交易日
得到的是两个不相交的窗口（实测 14 万个组合里只有 1035 个能算出相关性）。

**根因不是接口限制**（已实测确认）：`ths_daily` 对 `885517.TI` 请求
2026-01-01~2026-09-18 能正常返回 174 行。是上一次同步 `partial`
（`511 项未取到`）后没有续跑。

## 用法

    python scripts/mainline_sync_bars.py --start 20250101 --end 20260918
    python scripts/mainline_sync_bars.py --codes 885517.TI,886069.TI   # 定点补
    python scripts/mainline_sync_bars.py --retry 3                     # 失败重试轮数
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.errors import BRIEF_DEFAULT, brief  # noqa: E402
from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402


def missing_report(store: MainlineDataStore, start: str, end: str) -> list[str]:
    """列出在区间内 K 线根数明显不足的板块（按缺口从大到小）。"""
    rows = store._read(  # noqa: SLF001 运维脚本，直接读本地仓
        "SELECT b.code, b.name,"
        " (SELECT COUNT(*) FROM ml_board_bar x"
        "  WHERE x.board_code = b.code AND x.trade_date BETWEEN ? AND ?) n"
        " FROM ml_board b ORDER BY n", (start, end))
    days = len(store.calendar(start, end))
    return [f"{r['code']} {r['name']} {r['n']}/{days}"
            for r in rows if int(r["n"] or 0) < days * 0.8]


def main() -> int:
    parser = argparse.ArgumentParser(description="补全板块指数日线")
    parser.add_argument("--start", default="20250101")
    parser.add_argument("--end", default="")
    parser.add_argument("--codes", default="", help="逗号分隔，只补这些板块")
    parser.add_argument("--retry", type=int, default=3,
                        help="失败板块的重试轮数")
    parser.add_argument("--report", action="store_true",
                        help="只报告缺口，不同步")
    args = parser.parse_args()

    cfg = load_config()
    store = MainlineDataStore(config=cfg)
    end = args.end
    if not end:
        end = str(store._read(  # noqa: SLF001
            "SELECT MAX(trade_date) d FROM ml_board_bar")[0]["d"] or "")

    if args.report:
        gaps = missing_report(store, args.start, end)
        print(f"{args.start}~{end} 缺口板块 {len(gaps)} 个：")
        for line in gaps[:30]:
            print("   ", line)
        return 0

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    print(f"同步板块日线 {args.start}~{end}"
          + (f"（{len(codes)} 个指定板块）" if codes else "（全部板块）"))
    started = time.perf_counter()
    result = store.sync_board_bars(start=args.start, end=end, codes=codes)
    print(f"  第 1 轮：{result.status}  {result.rows} 行  "
          f"{len(result.missing)} 项未取到  耗时 {result.seconds:.0f}s")
    missing = list(result.missing)
    for attempt in range(2, max(args.retry, 1) + 1):
        if not missing:
            break
        print(f"  第 {attempt} 轮重试 {len(missing)} 个板块 …")
        retry = store.sync_board_bars(start=args.start, end=end, codes=missing)
        print(f"    取到 {retry.rows} 行，仍缺 {len(retry.missing)} 项")
        missing = list(retry.missing)

    gaps = missing_report(store, args.start, end)
    print(f"\n仍有缺口 {len(gaps)} 个板块，总耗时 {time.perf_counter() - started:.0f}s")
    for line in gaps[:20]:
        print("   ", line)
    if missing:
        print(f"未取到的板块（{len(missing)}）：{','.join(missing[:20])}"
              + (" …" if len(missing) > 20 else ""))
    return 0 if not gaps else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{brief(exc, BRIEF_DEFAULT)}")
        raise SystemExit(1) from exc
