"""`scripts/rescore_mainline.py` 的区间清理（`purge_range`）回归测试。

## 为什么要专门测这个

`--force` 会**不可逆地删数据**，而它存在的原因是一个曾经把结论搞错的缺陷：

- `mainline_alert` 按 `(trade_date, board_code, level)` upsert，
  所以旧配置下的 `medium` 行和新配置下的 `strong` 行会**同时存在**
  （实测 `20260918` 一天 20 行，`885908` 同时有 strong 与 medium）。
  `alert_trade_stats.py` 读全部行 → "告警胜率"变成两套配置的混合值，
  而报告上完全看不出来。
- `mainline_score` 同理：换池后消失的板块会永远留着旧分数、混进截面。

这类"静默混口径"是本项目最贵的一类 bug，所以清理逻辑必须有测试钉住：
**范围要准（区间外一行都不能动）、两张表都要清、`--keep-alerts` 要真的只清一张。**
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.rescore_mainline import purge_range  # noqa: E402


def _make_db(path: Path) -> None:
    """造一个只有两张表的最小库，区间内外各留几行。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE mainline_score"
                 "(trade_date TEXT, board_code TEXT, total REAL)")
    conn.execute("CREATE TABLE mainline_alert"
                 "(trade_date TEXT, board_code TEXT, level TEXT)")
    conn.executemany("INSERT INTO mainline_score VALUES(?,?,?)", [
        ("20240101", "A", 1.0), ("20240102", "A", 2.0),
        ("20240103", "A", 3.0),                     # 区间外（后面）
        ("20231201", "A", 4.0),                     # 区间外（前面）
    ])
    conn.executemany("INSERT INTO mainline_alert VALUES(?,?,?)", [
        ("20240101", "A", "medium"), ("20240101", "A", "strong"),
        ("20240102", "A", "medium"),
        ("20240103", "A", "weak"),
    ])
    conn.commit()
    conn.close()


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_purge_removes_only_the_range_and_both_tables(tmp_path: Path) -> None:
    db = tmp_path / "moss.db"
    _make_db(db)
    removed = purge_range(str(db), "20240101", "20240102", keep_alerts=False)
    assert removed == {"mainline_score": 2, "mainline_alert": 3}
    # 区间内清空、区间外（20231201 / 20240103）一行不动
    assert _count(db, "mainline_score") == 2
    assert _count(db, "mainline_alert") == 1


def test_keep_alerts_leaves_the_alert_table_alone(tmp_path: Path) -> None:
    db = tmp_path / "moss.db"
    _make_db(db)
    removed = purge_range(str(db), "20240101", "20240102", keep_alerts=True)
    assert removed == {"mainline_score": 2}
    assert _count(db, "mainline_alert") == 4          # 告警原样保留
    assert _count(db, "mainline_score") == 2


def test_purge_tolerates_a_missing_table(tmp_path: Path) -> None:
    """新库可能还没建过告警表 —— 记 0 而不是抛错中断整轮重打分。"""
    db = tmp_path / "half.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE mainline_score"
                 "(trade_date TEXT, board_code TEXT, total REAL)")
    conn.execute("INSERT INTO mainline_score VALUES('20240101','A',1.0)")
    conn.commit()
    conn.close()
    removed = purge_range(str(db), "20240101", "20240101", keep_alerts=False)
    assert removed["mainline_score"] == 1
    assert removed["mainline_alert"] == 0


def test_purge_on_missing_file_is_a_noop(tmp_path: Path) -> None:
    assert purge_range(str(tmp_path / "nope.db"), "20240101", "20240102",
                       keep_alerts=False) == {}
