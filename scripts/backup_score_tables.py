"""重打分之前先备份 `mainline_score` / `mainline_alert`（带时间戳的表副本）。

## 为什么必须先备份

1. `rescore_mainline.py --force` 会**先清空区间再逐日重写**。中途崩溃/断网会
   留下一张"部分写入"的表 —— 而 §16.39 记过：部分写入状态下
   `score_total_history_sync` 会静默取到错误时期的历史。
2. 本轮改的是**景气度低分压制**，它会影响每一个低景气板块的六维分 →
   候选池成员 → 全部下游。没有备份就无法做"改前 vs 改后"的对照，
   也无法回退。
3. 备份是**表副本**（`CREATE TABLE ... AS SELECT`），不是文件拷贝：
   15 GiB 的仓库文件拷不动，而这两张表只有几十万行。

用法：
    .venv\\Scripts\\python.exe scripts/backup_score_tables.py
    .venv\\Scripts\\python\\python.exe scripts/backup_score_tables.py --list
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
TABLES = ("mainline_score", "mainline_alert")


def main() -> int:
    parser = argparse.ArgumentParser(description="评分/告警表备份")
    parser.add_argument("--list", action="store_true", help="只列出已有备份")
    parser.add_argument("--tag", default="", help="备份标签（默认时间戳）")
    args = parser.parse_args()

    stamp = args.tag or datetime.now().astimezone().strftime("%Y%m%d_%H%M")
    conn = sqlite3.connect(str(MAIN_DB), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    existing = [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    if args.list:
        for name in existing:
            if name.startswith("mainline_score_bak") or \
                    name.startswith("mainline_alert_bak"):
                count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                print(f"   {name}: {count} 行")
        conn.close()
        return 0

    for table in TABLES:
        source_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        target = f"{table}_bak_{stamp}"
        if target in existing:
            print(f"   {target} 已存在，跳过")
            continue
        index = f"CREATE INDEX idx_{target}_date ON {target}(trade_date)"
        conn.execute(f"CREATE TABLE {target} AS SELECT * FROM {table}")
        conn.execute(index)
        conn.commit()
        copied = conn.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0]
        flag = "✅" if copied == source_rows else "⚠️"
        print(f"   {flag} {table} → {target}（{copied} / {source_rows} 行）")
    conn.close()
    print(f"\n备份标签：{stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
