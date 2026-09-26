"""补齐主线挖掘链路里落后的数据集（`ml_board_flow` 等）。

## 为什么需要它

`ml_board_flow` 由**个股口径聚合**而成（Σ 成分股 `net_mf_amount`，
见 `MainlineDataStore.sync_board_flows` 的口径说明），而个股数据来自本地
Tushare 仓库 `quant_moneyflow`。仓库水位一旦落后，板块资金流就整体停在
那一天 —— 2026-09-22 实测：`ml_board_bar` 已到 0922，`ml_board_flow` 却停在
0915，于是 0916~0922 的评分全部**用 0915 的资金流当最新值**，
界面上完全看不出来（有分数、有排名、有告警），只有「最新日期」不对。

⚠️ 这个脚本**只补齐数据、不重算分数**。补完必须再跑评分，
否则旧分数仍建立在过期资金流之上。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402

CACHE = ROOT / "data" / "mainline_cache.db"

#: 需要看水位的表：表名 → 日期列
WATCH = (
    ("ml_board_bar", "trade_date"),
    ("ml_board_flow", "trade_date"),
    ("ml_seat", "trade_date"),
    ("ml_margin", "trade_date"),
    ("ml_index", "trade_date"),
)


def watermarks() -> dict[str, str]:
    """各表最新交易日（读不到记空串）。"""
    conn = sqlite3.connect(f"file:{CACHE.as_posix()}?mode=ro", uri=True)
    out: dict[str, str] = {}
    for table, column in WATCH:
        try:
            row = conn.execute(
                f"SELECT MAX({column}) FROM {table}").fetchone()
            out[table] = str(row[0] or "")
        except sqlite3.Error:
            out[table] = ""
    conn.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐主线挖掘链路里落后的数据集")
    parser.add_argument("--start", default="", help="起始交易日 YYYYMMDD（缺省=水位次日起）")
    parser.add_argument("--end", default="", help="结束交易日 YYYYMMDD（缺省=板块日线水位）")
    parser.add_argument("--dataset", action="append", default=[],
                        help="可重复，缺省 board_flow")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    before = watermarks()
    print("补前水位：")
    for table, stamp in before.items():
        print(f"  {table:16s} {stamp or '—'}")

    target = args.end or before.get("ml_board_bar", "")
    if not target:
        print("❌ 读不到 ml_board_bar 水位，无法判断该补到哪天（不做猜测）")
        return 2

    datasets = args.dataset or ["board_flow"]
    for name in datasets:
        if name == "board_flow":
            floor = before.get("ml_board_flow", "")
        elif name == "board_bar":
            floor = before.get("ml_board_bar", "")
        else:
            floor = ""
        start = args.start or floor
        if not start:
            print(f"⚠️ {name}: 没有水位，需要显式给 --start")
            continue
        if start >= target:
            print(f"✅ {name}: {start} 已到 {target}，无需补")
            continue
        print(f"\n→ {name}: 补 {start} ~ {target}")
        if args.dry_run:
            continue
        store = MainlineDataStore(config=load_config())
        if name == "board_flow":
            result = store.sync_board_flows(start=start, end=target)
        elif name == "board_bar":
            result = store.sync_board_bars(start=start, end=target)
        else:
            print(f"⚠️ 未知数据集 {name}，跳过")
            continue
        print(f"  status={result.status} rows={result.rows} "
              f"{result.seconds:.1f}s {result.message}")

    after = watermarks()
    print("\n补后水位：")
    changed = []
    for table, stamp in after.items():
        mark = ""
        if stamp != before.get(table):
            mark = f"  ← {before.get(table) or '—'}"
            changed.append(table)
        print(f"  {table:16s} {stamp or '—'}{mark}")
    if not changed:
        print("\n⚠️ 没有任何水位变化。")
    print("\n⚠️ 数据补齐 ≠ 分数更新：板块资金流变了，历史评分必须重算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
