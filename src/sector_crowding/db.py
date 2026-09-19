"""板块概念拥挤度：SQL 存储层。

## 表结构（建在**主库** `data/moss_finagent.db`，与做T/量化选股同库）

    sector_crowding_daily   每日每板块一行（UNIQUE(trade_date, sector_code)）
    sector_meta             板块元数据 + 增量刷新的水位线（last_update_date）
    sector_member           板块成分股（参考数据，不参与拥挤度计算）

## 为什么 `sector_meta.last_update_date` 是增量刷新的水位线

手动"一键刷新"要回答的问题是"从哪天开始补"。答案必须是**幂等且可恢复**的：

- 该板块为空 → 从近 6 年起始日全量回填；
- 非空 → 从 `last_update_date` 的**下一个交易日**拉到最新交易日；
- 按 `UNIQUE(trade_date, sector_code)` UPSERT，重复触发不产生重复行；
- 全部成功后才推进 `last_update_date`（否则失败那天的数据永远补不回来）。

最后一条是关键：如果拉取成功但写入失败，或中途异常早退，
水位线**不能**前进 —— 否则下次刷新会从错误的日期开始，那段时间成为永久空洞。

## 成交额口径

板块成交额取 `ths_daily` 的 `vol × avg_price`（同花顺板块指数日线里没有 amount 列，
而这两列的乘积就是成交额）。实测与"成分股成交额求和"同量级，且**不受成分股
历史变更影响**（成分股接口 `ths_member` 只给当前快照，用它回算 6 年历史会有
前视偏差 —— 见 docs/SECTOR_CROWDING.md 的已知限制）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from src.sector_crowding.config import SectorCrowdingConfig, load_config

logger = logging.getLogger(__name__)

DAILY_TABLE = "sector_crowding_daily"
META_TABLE = "sector_meta"
MEMBER_TABLE = "sector_member"
WATCH_TABLE = "sector_crowding_watch"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {DAILY_TABLE} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT    NOT NULL,
    sector_code  TEXT    NOT NULL,
    sector_name  TEXT    NOT NULL DEFAULT '',
    sector_amount REAL,
    market_amount REAL,
    raw_crowding REAL,
    ma5_crowding REAL,
    water_level  REAL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE(trade_date, sector_code)
);
CREATE INDEX IF NOT EXISTS idx_crowding_sector_date
    ON {DAILY_TABLE}(sector_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_crowding_date
    ON {DAILY_TABLE}(trade_date);

CREATE TABLE IF NOT EXISTS {META_TABLE} (
    sector_code       TEXT PRIMARY KEY,
    sector_name       TEXT NOT NULL DEFAULT '',
    is_concept        INTEGER NOT NULL DEFAULT 1,
    board_type        TEXT NOT NULL DEFAULT '',
    first_trade_date  TEXT NOT NULL DEFAULT '',
    last_update_date  TEXT NOT NULL DEFAULT '',
    bars              INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS {MEMBER_TABLE} (
    sector_code TEXT NOT NULL,
    stock_code  TEXT NOT NULL,
    stock_name  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (sector_code, stock_code)
);

CREATE TABLE IF NOT EXISTS {WATCH_TABLE} (
    sector_code TEXT PRIMARY KEY,
    sector_name TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);
"""

_ADDABLE: dict[str, dict[str, str]] = {
    DAILY_TABLE: {},
    META_TABLE: {
        "board_type": "TEXT NOT NULL DEFAULT ''",
        "bars": "INTEGER NOT NULL DEFAULT 0",
    },
    MEMBER_TABLE: {},
    WATCH_TABLE: {},
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ======================================================================
# 连接
# ======================================================================

def get_db_connection(config: SectorCrowdingConfig | None = None,
                      *, path: str | Path | None = None) -> sqlite3.Connection:
    """打开拥挤度库连接（WAL + busy_timeout，与项目其它仓储同范式）。

    为什么每次新建连接而不是长连接：一键刷新在**后台线程**里跑，
    SQLite 连接不能跨线程共享；短连接 + WAL 让"刷新写"与"前端读"互不阻塞。
    """
    config = config or load_config()
    target = Path(path) if path is not None else config.db_path
    os.makedirs(target.parent, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_tables(conn: sqlite3.Connection | None = None,
                config: SectorCrowdingConfig | None = None) -> None:
    """建表 + PRAGMA 补列（幂等）。"""
    own = conn is None
    conn = conn or get_db_connection(config)
    try:
        conn.executescript(_SCHEMA)
        for table, columns in _ADDABLE.items():
            if not columns:
                continue
            existing = {row["name"] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()
    finally:
        if own:
            conn.close()


# ======================================================================
# 写入
# ======================================================================

def upsert_sector_crowding(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]],
                           *, commit: bool = True) -> int:
    """UPSERT 每日拥挤度（幂等）。返回写入行数。

    `UNIQUE(trade_date, sector_code)` 冲突时更新数值列与 `updated_at`，
    `created_at` 保持不变（保留"这行最早是什么时候写进来的"）。

    `commit=False`：提交交给调用方（批量重算时用）。实测差异极大 ——
    逐板块提交 217 万行要 **656 秒**，整批一次提交降到 **80 秒**
    （每个 commit 都是一次 WAL 落盘）。
    """
    payload = list(rows)
    if not payload:
        return 0
    stamp = _now()
    sql = f"""
    INSERT INTO {DAILY_TABLE}
        (trade_date, sector_code, sector_name, sector_amount, market_amount,
         raw_crowding, ma5_crowding, water_level, created_at, updated_at)
    VALUES (:trade_date, :sector_code, :sector_name, :sector_amount, :market_amount,
            :raw_crowding, :ma5_crowding, :water_level, :created_at, :updated_at)
    ON CONFLICT(trade_date, sector_code) DO UPDATE SET
        sector_name   = excluded.sector_name,
        sector_amount = excluded.sector_amount,
        market_amount = excluded.market_amount,
        raw_crowding  = excluded.raw_crowding,
        ma5_crowding  = excluded.ma5_crowding,
        water_level   = excluded.water_level,
        updated_at    = excluded.updated_at
    """
    prepared = []
    for row in payload:
        prepared.append({
            "trade_date": str(row.get("trade_date") or ""),
            "sector_code": str(row.get("sector_code") or ""),
            "sector_name": str(row.get("sector_name") or ""),
            "sector_amount": _num(row.get("sector_amount")),
            "market_amount": _num(row.get("market_amount")),
            "raw_crowding": _num(row.get("raw_crowding")),
            "ma5_crowding": _num(row.get("ma5_crowding")),
            "water_level": _num(row.get("water_level")),
            "created_at": stamp,
            "updated_at": stamp,
        })
    conn.executemany(sql, prepared)
    if commit:
        conn.commit()
    return len(prepared)


def update_sector_meta(conn: sqlite3.Connection, *, sector_code: str,
                       sector_name: str = "", is_concept: bool = True,
                       board_type: str = "", first_trade_date: str = "",
                       last_update_date: str = "", bars: int | None = None,
                       advance_watermark: bool = True) -> None:
    """写板块元数据。

    `advance_watermark=False` 时**不动** `last_update_date` —— 只有整段成功
    才允许推进水位线（中途失败还推进会让那段日期成为永久空洞）。
    """
    stamp = _now()
    code = str(sector_code).strip()
    if not code:
        return
    conn.execute(
        f"""
        INSERT INTO {META_TABLE}
            (sector_code, sector_name, is_concept, board_type, first_trade_date,
             last_update_date, bars, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sector_code) DO UPDATE SET
            sector_name = CASE WHEN excluded.sector_name <> ''
                               THEN excluded.sector_name
                               ELSE {META_TABLE}.sector_name END,
            is_concept  = excluded.is_concept,
            board_type  = CASE WHEN excluded.board_type <> ''
                               THEN excluded.board_type
                               ELSE {META_TABLE}.board_type END,
            first_trade_date = CASE WHEN excluded.first_trade_date <> ''
                                    THEN excluded.first_trade_date
                                    ELSE {META_TABLE}.first_trade_date END,
            last_update_date = CASE WHEN ? = 1 AND excluded.last_update_date <> ''
                                    THEN excluded.last_update_date
                                    ELSE {META_TABLE}.last_update_date END,
            bars        = COALESCE(?, {META_TABLE}.bars),
            updated_at  = excluded.updated_at
        """,
        (code, str(sector_name or ""), 1 if is_concept else 0, str(board_type or ""),
         str(first_trade_date or ""), str(last_update_date or ""),
         int(bars) if bars is not None else 0, stamp,
         1 if advance_watermark else 0,
         int(bars) if bars is not None else None),
    )
    conn.commit()


def upsert_members(conn: sqlite3.Connection, sector_code: str,
                   members: Iterable[dict[str, Any]]) -> int:
    """覆盖写板块成分股（参考数据）。返回写入条数。"""
    rows = [(str(sector_code), str(item.get("code") or ""),
             str(item.get("name") or ""), _now())
            for item in members if str(item.get("code") or "").strip()]
    if not rows:
        return 0
    conn.executemany(
        f"INSERT OR REPLACE INTO {MEMBER_TABLE}"
        "(sector_code, stock_code, stock_name, updated_at) VALUES (?,?,?,?)", rows)
    conn.commit()
    return len(rows)


# ======================================================================
# 读取
# ======================================================================

def get_last_update_date(conn: sqlite3.Connection, sector_code: str) -> str:
    """该板块已入库到的最后交易日（'' = 从未入库 → 需要全量回填）。"""
    row = conn.execute(
        f"SELECT last_update_date FROM {META_TABLE} WHERE sector_code = ?",
        (str(sector_code),)).fetchone()
    return str(row["last_update_date"] or "") if row else ""


def query_sector_crowding(conn: sqlite3.Connection, sector_code: str, *,
                          start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
    """单板块历史序列（按交易日升序）。"""
    sql = f"SELECT * FROM {DAILY_TABLE} WHERE sector_code = ?"
    params: list[Any] = [str(sector_code)]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(str(start_date))
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(str(end_date))
    sql += " ORDER BY trade_date"
    return [dict(row) for row in conn.execute(sql, params)]


def query_all_latest_water_level(conn: sqlite3.Connection, *,
                                 concepts_only: bool = False,
                                 trade_date: str = "") -> list[dict[str, Any]]:
    """全板块**最新交易日**的水位（前端散点总览用）。

    `trade_date` 留空时取全库最大交易日 —— 不能让每个板块各取自己的最新日：
    停牌/退市的板块最新日会更早，混在一起画散点会出现"今天的图里混着上周的点"。
    """
    target = str(trade_date or "").strip()
    if not target:
        row = conn.execute(
            f"SELECT MAX(trade_date) AS d FROM {DAILY_TABLE}").fetchone()
        target = str(row["d"] or "") if row else ""
    if not target:
        return []
    sql = f"""
    SELECT d.sector_code, d.sector_name, d.trade_date, d.sector_amount,
           d.market_amount, d.raw_crowding, d.ma5_crowding, d.water_level,
           COALESCE(m.is_concept, 1) AS is_concept, m.bars
    FROM {DAILY_TABLE} d
    LEFT JOIN {META_TABLE} m ON m.sector_code = d.sector_code
    WHERE d.trade_date = ?
    """
    params: list[Any] = [target]
    if concepts_only:
        sql += " AND COALESCE(m.is_concept, 1) = 1"
    sql += " ORDER BY d.water_level IS NULL, d.water_level DESC"
    return [dict(row) for row in conn.execute(sql, params)]


def query_alerts(conn: sqlite3.Connection, *, threshold: float = 0.8,
                 concepts_only: bool = True,
                 trade_date: str = "") -> list[dict[str, Any]]:
    """水位 ≥ 阈值的板块（按水位降序）。

    水位为 NULL（数据不足）的**不参与告警** —— "不知道"和"不拥挤"是两件事，
    把 NULL 当成 0 或当成触发都是错的。
    """
    target = str(trade_date or "").strip()
    if not target:
        row = conn.execute(
            f"SELECT MAX(trade_date) AS d FROM {DAILY_TABLE}").fetchone()
        target = str(row["d"] or "") if row else ""
    if not target:
        return []
    sql = f"""
    SELECT d.sector_code, d.sector_name, d.trade_date, d.sector_amount,
           d.market_amount, d.raw_crowding, d.ma5_crowding, d.water_level,
           COALESCE(m.is_concept, 1) AS is_concept,
           (SELECT MAX(x.ma5_crowding) FROM {DAILY_TABLE} x
             WHERE x.sector_code = d.sector_code) AS max_ma5_crowding
    FROM {DAILY_TABLE} d
    LEFT JOIN {META_TABLE} m ON m.sector_code = d.sector_code
    WHERE d.trade_date = ? AND d.water_level IS NOT NULL AND d.water_level >= ?
    """
    params: list[Any] = [target, float(threshold)]
    if concepts_only:
        sql += " AND COALESCE(m.is_concept, 1) = 1"
    sql += " ORDER BY d.water_level DESC"
    return [dict(row) for row in conn.execute(sql, params)]


def query_sector_meta(conn: sqlite3.Connection, *,
                      concepts_only: bool = False) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {META_TABLE}"
    if concepts_only:
        sql += " WHERE is_concept = 1"
    sql += " ORDER BY sector_code"
    return [dict(row) for row in conn.execute(sql)]


# ======================================================================
# 自选池
# ======================================================================

def list_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """自选池板块（带上各自最新水位，前端一屏就能看全）。"""
    latest = latest_trade_date(conn)
    rows = conn.execute(
        f"SELECT w.sector_code, w.sector_name, w.note, w.added_at, "
        f"       d.water_level, d.ma5_crowding, d.raw_crowding, "
        f"       d.sector_amount, d.market_amount, d.trade_date "
        f"FROM {WATCH_TABLE} w "
        f"LEFT JOIN {DAILY_TABLE} d ON d.sector_code = w.sector_code "
        f"     AND d.trade_date = ? "
        f"ORDER BY w.added_at DESC", (latest,)).fetchall()
    return [dict(row) for row in rows]


def add_to_watchlist(conn: sqlite3.Connection, sector_code: str, *,
                     sector_name: str = "", note: str = "") -> bool:
    """加入自选（幂等）。返回 True 表示新增，False 表示已在池里（仍更新名称）。"""
    code = str(sector_code).strip()
    if not code:
        raise ValueError("sector_code 不能为空")
    existed = conn.execute(
        f"SELECT 1 FROM {WATCH_TABLE} WHERE sector_code = ?", (code,)).fetchone()
    conn.execute(
        f"INSERT INTO {WATCH_TABLE}(sector_code, sector_name, note, added_at) "
        f"VALUES (?,?,?,?) ON CONFLICT(sector_code) DO UPDATE SET "
        f"sector_name = CASE WHEN excluded.sector_name <> '' "
        f"                    THEN excluded.sector_name "
        f"                    ELSE {WATCH_TABLE}.sector_name END, "
        f"note = CASE WHEN excluded.note <> '' THEN excluded.note "
        f"             ELSE {WATCH_TABLE}.note END",
        (code, str(sector_name or ""), str(note or ""), _now()))
    conn.commit()
    return existed is None


def remove_from_watchlist(conn: sqlite3.Connection, sector_code: str) -> bool:
    cursor = conn.execute(f"DELETE FROM {WATCH_TABLE} WHERE sector_code = ?",
                          (str(sector_code).strip(),))
    conn.commit()
    return bool(cursor.rowcount)


def watchlist_codes(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute(
        f"SELECT sector_code FROM {WATCH_TABLE}")}


def query_sector_members(conn: sqlite3.Connection,
                         sector_code: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        f"SELECT stock_code, stock_name FROM {MEMBER_TABLE} "
        f"WHERE sector_code = ? ORDER BY stock_code", (str(sector_code),))]


def search_sectors(conn: sqlite3.Connection, keyword: str, *,
                   limit: int = 20) -> list[dict[str, Any]]:
    """按名称/代码搜板块（前端"输入板块名称查询"用）。"""
    text = str(keyword or "").strip()
    if not text:
        return []
    rows = conn.execute(
        f"SELECT sector_code, sector_name, is_concept, board_type, bars, "
        f"       last_update_date FROM {META_TABLE} "
        f"WHERE sector_name LIKE ? OR sector_code LIKE ? "
        f"ORDER BY bars DESC, sector_code LIMIT ?",
        (f"%{text}%", f"%{text}%", max(1, int(limit)))).fetchall()
    return [dict(row) for row in rows]


def list_sectors_needing_refresh(conn: sqlite3.Connection, *,
                                 concepts_only: bool = False) -> list[dict[str, Any]]:
    """所有可刷新的板块（含 `last_update_date`，供增量起点计算）。"""
    return query_sector_meta(conn, concepts_only=concepts_only)


def latest_trade_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        f"SELECT MAX(trade_date) AS d FROM {DAILY_TABLE}").fetchone()
    return str(row["d"] or "") if row else ""


def count_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {DAILY_TABLE}").fetchone()[0])


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


__all__ = [
    "DAILY_TABLE",
    "MEMBER_TABLE",
    "META_TABLE",
    "WATCH_TABLE",
    "add_to_watchlist",
    "count_rows",
    "get_db_connection",
    "get_last_update_date",
    "init_tables",
    "latest_trade_date",
    "list_sectors_needing_refresh",
    "list_watchlist",
    "query_alerts",
    "query_all_latest_water_level",
    "query_sector_crowding",
    "query_sector_members",
    "query_sector_meta",
    "remove_from_watchlist",
    "search_sectors",
    "update_sector_meta",
    "upsert_members",
    "upsert_sector_crowding",
    "watchlist_codes",
]
