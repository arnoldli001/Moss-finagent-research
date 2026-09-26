"""运维：把个股上市日期记录到 `ml_stock_meta`（回测可复现）。

为什么要有这一步：上市日期闸门默认直接读行情仓 `quant_stock_basic`，
但那是按 `list_status='L'` 同步的**当前上市**名录，会随退市/新上市变化。
回测要复现"当时按哪份上市日期做的剔除"，就需要在本地留一份快照。

用法：
    .venv\\Scripts\\python.exe scripts\\sync_stock_meta.py
    .venv\\Scripts\\python.exe scripts\\sync_stock_meta.py --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="记录个股上市日期")
    parser.add_argument("--check", action="store_true",
                        help="只报告现状，不写库")
    args = parser.parse_args()

    store = MainlineDataStore(config=load_config())
    listing = store.stock_list_dates()
    print(f"上市日期可读：{len(listing)} 只"
          f"（非空 {sum(1 for v in listing.values() if v)}）")
    if args.check:
        rows = store._read(  # noqa: SLF001 运维脚本，直接读本地仓
            "SELECT COUNT(*) n FROM ml_stock_meta")
        print(f"ml_stock_meta 现有 {rows[0]['n']} 行")
        return 0

    result = store.sync_stock_meta()
    print(f"写入 {result.rows} 行，状态 {result.status}，"
          f"耗时 {result.seconds:.2f}s"
          + (f"，消息 {result.message}" if result.message else ""))
    rows = store._read(  # noqa: SLF001
        "SELECT COUNT(*) n, MIN(list_date) a, MAX(list_date) b"
        " FROM ml_stock_meta")
    print(f"ml_stock_meta 现有 {rows[0]['n']} 行 / "
          f"上市日期 {rows[0]['a']} ~ {rows[0]['b']}")
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
