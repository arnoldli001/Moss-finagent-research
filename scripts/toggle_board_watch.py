"""把某个板块移出「关注」（拥挤度看板清单）。

用户指示：「删除华为盘古概念的关注」。

## 「关注」在哪张表

拥挤度看板的状态在 `sector_crowding_list`（`moss_finagent.db`）：
`visible=1` 就是"在关注列表里"。`sector_crowding_watch` 是另一套
（告警用的自选），华为盘古不在其中。

## 为什么默认是「隐藏」而不是「删除行」

`visible=0` 保留了 `created_at` / 来源，随时可以恢复；直接 DELETE 会把
"用户曾经关注过它"这件事一起抹掉，而且下次全量种子化时又会被重新插回来
（`source='manual'` 的行会保留，但列表重建逻辑会重新种子化概念板块）。
所以默认用隐藏，加 `--purge` 才真删。

用法：
    .venv\\Scripts\\python.exe scripts/toggle_board_watch.py 886094.TI --hide
    .venv\\Scripts\\python.exe scripts/toggle_board_watch.py 886094.TI --show
    .venv\\Scripts\\python.exe scripts/toggle_board_watch.py 886094.TI --purge
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="关注列表开关")
    parser.add_argument("codes", nargs="+", help="板块代码，如 886094.TI")
    parser.add_argument("--hide", action="store_true", help="取消关注（visible=0）")
    parser.add_argument("--show", action="store_true", help="恢复关注（visible=1）")
    parser.add_argument("--purge", action="store_true", help="直接删除该行")
    args = parser.parse_args()

    if not (args.hide or args.show or args.purge):
        print("❌ 需要 --hide / --show / --purge 之一")
        return 2

    conn = sqlite3.connect(str(MAIN_DB), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for code in args.codes:
        row = conn.execute(
            "SELECT sector_code, sector_name, visible, pinned, source"
            " FROM sector_crowding_list WHERE sector_code = ?", (code,)).fetchone()
        if row is None:
            print(f"   {code}：不在关注清单里，跳过")
            continue
        if args.purge:
            conn.execute("DELETE FROM sector_crowding_list WHERE sector_code = ?",
                         (code,))
            print(f"   {row['sector_name']}（{code}）：已**删除**该行"
                  f"（原 visible={row['visible']}）")
            continue
        visible = 1 if args.show else 0
        pinned = 0 if args.hide else int(row["pinned"] or 0)
        conn.execute(
            "UPDATE sector_crowding_list SET visible=?, pinned=?, updated_at=?"
            " WHERE sector_code=?", (visible, pinned, stamp, code))
        verb = "恢复关注" if visible else "取消关注"
        print(f"   {row['sector_name']}（{code}）：{verb}"
              f"（visible {row['visible']} → {visible}）")
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM sector_crowding_list WHERE visible = 1").fetchone()[0]
    print(f"\n当前关注数（visible=1）：{remaining}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
