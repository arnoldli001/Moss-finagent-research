"""把主库里的**主线挖掘结果表**同步到对外试点库（`data/pilot/moss_pilot.db`）。

## 为什么需要它

试点的数据是**隔离**的（`manage.py` 的 `pilot_isolation_env` 把
`MOSS_SQLITE_PATH` 指到 `data/pilot/moss_pilot.db`），而且试点的**定时任务被
刻意关闭**（两个调度器同写 31GB 行情仓会重复下载）。两条设计叠加的结果是：

    试点库的主线结果表**永远停在建库那一天**，不会自己长。

实测（2026-09-25）：

| 表 | 主库 | 试点库 |
| --- | --- | --- |
| `mainline_score` | 94,219（20231009~20260922） | **138**（只有 20260923 一天） |
| `mainline_alert` | 2,865 | **11** |
| `mainline_backtest` | 6（导入的历史回测） | **0** |

于是客户在 `https://moss.wujiaitool.cn` 上打开「回测报告」看到的是
**「缺数据说明：还没有回测记录」**，打开「回测收益展示」几乎一片空白 ——
不是功能坏了，是**这份库里没有数据**。

## 口径

* **只增不改业务语义**：按主键 `INSERT OR REPLACE`，可重复执行、幂等；
  主库删过的行不会在这里被删（试点的历史留痕比"和主库逐字节一致"更重要）。
* **只搬结果表**，不搬账号/会话/审计（那些是试点自己的，搬过去等于越权）。
* 写的是**试点库**，会短暂持有写锁（几秒）。分块提交，避免长时间独占。

用法：

    python scripts/sync_mainline_to_pilot.py --dry-run   # 只看会搬多少
    python scripts/sync_mainline_to_pilot.py             # 正式同步
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "moss_finagent.db"
TARGET = ROOT / "data" / "pilot" / "moss_pilot.db"

#: 搬运顺序 = 依赖无关的"结果表"。刻意**不含**账号/会话/审计/配额表：
#: 那些属于试点自己的库，覆盖过去会改掉客户的登录与权限状态。
TABLES = (
    "mainline_score",
    "mainline_alert",
    "mainline_backtest",
    "mainline_etf_flow_signal",
    "mainline_etf_flow_backtest",
    "mainline_future_signal",
    "mainline_mapping_calibration",
)

BATCH = 2000


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def count(connection: sqlite3.Connection, table: str) -> int:
    try:
        return connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description="主库 → 试点库 主线结果表同步")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for path in (SOURCE, TARGET):
        if not path.exists():
            print(f"❌ 缺少 {path}", file=sys.stderr)
            return 1

    src = sqlite3.connect(f"file:{SOURCE.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(TARGET, timeout=60.0)

    total = 0
    for table in TABLES:
        src_cols = columns(src, table)
        dst_cols = columns(dst, table)
        if not src_cols:
            print(f"  · {table:32s} 主库没有这张表，跳过")
            continue
        if not dst_cols:
            print(f"  · {table:32s} ⚠️ 试点库没有这张表，跳过"
                  f"（先在试点跑一次启动让它建表）")
            continue
        if src_cols != dst_cols:
            print(f"  · {table:32s} ❌ 列不一致，跳过\n      主库={src_cols}\n"
                  f"      试点={dst_cols}")
            continue

        before = count(dst, table)
        rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
        if args.dry_run:
            print(f"  · {table:32s} 试点 {before} → 将写入 {len(rows)} 行（dry-run）")
            continue
        marks = ",".join("?" for _ in src_cols)
        names = ",".join(f'"{name}"' for name in src_cols)
        written = 0
        dst.execute("BEGIN")
        try:
            for start in range(0, len(rows), BATCH):
                chunk = rows[start:start + BATCH]
                dst.executemany(
                    f'INSERT OR REPLACE INTO "{table}"({names}) VALUES({marks})',
                    chunk)
                written += len(chunk)
            dst.execute("COMMIT")
        except sqlite3.Error as exc:
            dst.execute("ROLLBACK")
            print(f"  · {table:32s} ❌ 写入失败并已回滚：{exc}", file=sys.stderr)
            continue
        after = count(dst, table)
        total += written
        print(f"  · {table:32s} 试点 {before} → {after}"
              f"（写入 {written} 行）")

    src.close()
    dst.close()
    print(f"\n完成：共写入 {total} 行。"
          + ("（dry-run，未改动试点库）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
