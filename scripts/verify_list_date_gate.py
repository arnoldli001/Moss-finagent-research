"""验证：上市日期闸门。

## 钉住什么

需求："股票池成分股回测前要记录上市时间，回测起始时间没上市的，不算在内。"

数据事实（`scripts/_diag_list_date.py` 实测）：

    quant_stock_basic          5562 只，list_date 100% 非空
    池内 324 板块的成分股       5510 只，99.86% 能查到上市日期
    需剔除比例   2023-10 起点   4.46%
                 2025-10 起点   1.38%
                 2026-01 起点   0.95%

用法：.venv\\Scripts\\python.exe scripts\\verify_list_date_gate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402

STARTS = ("20231001", "20250101", "20251001", "20260101")


def main() -> int:
    store = MainlineDataStore(config=load_config())
    listing = store.stock_list_dates()
    print(f"上市日期记录：{len(listing)} 只")
    if not listing:
        print("❌ 拿不到上市日期 —— 闸门不会生效（回测会有前视偏差）")
        return 1
    filled = sum(1 for value in listing.values() if value)
    print(f"   其中非空 {filled}（{filled / len(listing) * 100:.2f}%）")

    boards = store._read(  # noqa: SLF001 验收脚本，直接读本地仓
        "SELECT code FROM ml_board WHERE source = 'sector_crowding:list'")
    pool = {str(r["code"]) for r in boards}
    member_map = store.member_map()
    inside = {code: codes for code, codes in member_map.items()
              if code in pool}
    total = sum(len(v) for v in inside.values())
    stocks = {code for codes in inside.values() for code in codes}
    known = {code for code in stocks if listing.get(code)}
    print(f"\n池内成分股：{total} 个 (板块,股票) 对 / {len(stocks)} 只股票")
    print(f"   能查到上市日期 {len(known)} 只"
          f"（{len(known) / max(1, len(stocks)) * 100:.2f}%）")

    print(f"\n{'回测起点':<12}{'剔除未上市':>12}{'剔除日期缺失':>14}"
          f"{'合计占比':>10}")
    print("-" * 50)
    ok = True
    for start in STARTS:
        unlisted = 0
        unknown = 0
        for codes in inside.values():
            for stock in codes:
                value = listing.get(str(stock))
                if not value:
                    unknown += 1
                elif value > start:
                    unlisted += 1
        share = (unlisted + unknown) / max(1, total) * 100
        print(f"{start:<12}{unlisted:>12}{unknown:>14}{share:>9.2f}%")
        if unlisted == 0:
            print("   ⚠️ 一个都没剔除 —— 闸门可能没接上")
            ok = False

    print()
    print("=" * 62)
    print("反例检查：2023-10 起点必须比 2026-01 起点剔得多（新股越多）")
    def cut(start: str) -> int:
        return sum(1 for codes in inside.values() for stock in codes
                   if (listing.get(str(stock)) or "") > start)

    if cut("20231001") > cut("20260101"):
        print(f"   ✅ {cut('20231001')} > {cut('20260101')}，方向正确")
    else:
        print(f"   ❌ {cut('20231001')} <= {cut('20260101')}，方向不对")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
