"""拥挤度用的**成分股来源**：优先用主线挖掘的提纯名单。

## 为什么需要这个模块

拥挤度的第 4 列是「窗口内主力净流入 ÷ 基准日流通市值」，分母分子都靠**成分股**。
在接入提纯名单之前，它用的是 `sector_member`（来自东财 `ths_member` 的**原始**
概念名单），而主线挖掘那边早就换成了 `ml_member_pure`（按「业务相关性 +
与板块指数相关性」双门限提纯，`relevant=1` 才算）。

两套名单差多少（实测 2026-09-21）：

    提纯压缩比中位数 0.51（提纯后大约是原始的一半）
    培育钻石 885937：原始 20 只 → 提纯 11 只
    玻璃基板 886111：原始 57 只 → 提纯 56 只
    压得最狠的 885699：原始 256 只 → 提纯 1 只

原始名单里有大量「沾边」个股（蹭概念的），把它们算进资金流会把真实的主力
集中度**稀释**掉 —— 一个板块的拥挤度会被几十只不相干的股票摊薄。所以两套
子系统必须用同一份成分股，否则同一时刻两边讲的是不同的故事。

## 回退策略

提纯表只覆盖 324 个板块，而拥挤度跟踪 1878 个。所以：

    relevant 只数 ≥ `min_purified_members`  → 用提纯名单
    否则（未评估 / 提纯退化）             → 回退 `sector_member`（原始）

`min_purified_members` 的默认值是 3：实测全池只有 3 个板块的 `relevant` 少于 5
（885699 只有 1 只 —— 原始 256 只压到 1 只显然是提纯没有真正跑完，不是"这个
板块只有一只相关股"）。少于 3 只时用提纯名单算出来的资金流是**单只股票的
资金流**，噪声远大于信号，所以宁可回退。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 主线挖掘的提纯成员表（在 `mainline_cache.db`）
PURE_TABLE = "ml_member_pure"

#: 判定口径：`relevant=1` 才算提纯后保留的成分股
PURE_FLAG_COLUMN = "relevant"


def open_pure_db(path: str | Path) -> sqlite3.Connection | None:
    """只读打开主线缓存库；文件不存在或不是 SQLite 时返回 `None`。

    只读（`mode=ro`）是刻意的：拥挤度**绝不能**写主线挖掘的库，
    它只是消费方。库不存在时安静回退到原始名单，不该让整个周频任务失败。
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn
    except sqlite3.Error:
        return None


def pure_member_codes(conn: sqlite3.Connection, sector_code: str, *,
                      min_members: int = 3) -> list[str] | None:
    """提纯后的成分股代码；不足 `min_members` 只时返回 `None`（= 回退原始）。

    返回 `None` 而不是空列表很重要：调用方据此区分「提纯不可用，该回退」
    与「提纯可用但确实没有成分股」。
    """
    if conn is None or not sector_code:
        return None
    try:
        rows = conn.execute(
            f"SELECT code FROM {PURE_TABLE}"  # noqa: S608 表名是模块常量
            f" WHERE board_code = ? AND {PURE_FLAG_COLUMN} = 1", (sector_code,)
        ).fetchall()
    except sqlite3.Error:
        return None
    codes = [str(row[0]) for row in rows if row[0]]
    if len(codes) < max(1, int(min_members)):
        return None
    return codes


__all__ = ["PURE_FLAG_COLUMN", "PURE_TABLE", "open_pure_db", "pure_member_codes"]
