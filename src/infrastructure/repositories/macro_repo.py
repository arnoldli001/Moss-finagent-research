"""SQLite数据点仓储（infrastructure层，参数化查询）。

版本管理：UNIQUE(indicator, period_date, raw_content_hash)约束，
同键重复写入自动跳过，不同哈希同期数据共存形成版本序列；
血缘追踪：行级task_id/processed_by记录数据来源链路。

类名MacroRepository为历史命名（业务最初只存宏观指标），现已是通用
DataPointRepository端口的SQLite实现，新代码请按抽象类型依赖。
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.repositories._mapping import COLUMNS, point_to_row
from src.infrastructure.repositories.base import DataPointRepository
from src.infrastructure.repositories.event_sqlite_base import (
    connect_sqlite,
    retry_on_locked,
)

# ★ 2026-09-29 补：本模块原先**没有** logger，而 `query_many()` 的降级分支
#   （SQLite < 3.25 无窗口函数 → 退回全量拉取）里写着 `logger.warning(...)`
#   ⇒ 那条"优雅降级"路径一执行就抛 `NameError: name 'logger' is not defined`
#   （ruff `F821` 抓到的；在没有窗口函数的老 SQLite 上才会走到）。
#   **降级分支写错了等于没有降级** —— 而且报的是 NameError，
#   排查方向会被引到"查询为什么失败"而不是"日志器没定义"。
logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_data_points (
    data_id TEXT PRIMARY KEY,
    indicator TEXT NOT NULL,
    value REAL,
    unit TEXT,
    period_date TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}',
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    publish_time TEXT,
    fetch_time TEXT NOT NULL,
    fetch_method TEXT NOT NULL,
    raw_content_hash TEXT NOT NULL,
    processed_by TEXT NOT NULL,
    process_time TEXT NOT NULL,
    confidence REAL NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(indicator, period_date, raw_content_hash)
);
CREATE INDEX IF NOT EXISTS idx_points_indicator_period
    ON fact_data_points(indicator, period_date);
"""

_INSERT_SQL = (
    "INSERT OR IGNORE INTO fact_data_points (" + ", ".join(COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in COLUMNS) + ")"
)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class MacroRepository(DataPointRepository):
    """统一数据点仓储（SQLite，线程池化阻塞IO）。"""

    def __init__(self, db_path: str | None = None) -> None:
        # 默认取**本环境**的应用库 —— `AGENTS.md`：默认值即护栏，安全的一侧做成默认。
        # 原先写死 `data/moss_finagent.db`（三档隔离**共用**的遗留主库）：
        # 一次漏传 `db_path` 就让隔离档写到共享库上，而没有任何地方声明过（CHG-0069）。
        from src.infrastructure.catalog.data_stores import default_app_db

        self._db_path = db_path or default_app_db()

    def _connect(self) -> sqlite3.Connection:
        # 同一把范式：显式锁等待 + WAL。此前用裸 connect（5s 默认等待），
        # 做T/量化/资金流并发写主库时 `CREATE TABLE IF NOT EXISTS` 会直接抛
        # "database is locked"（见 2026-09-18 backend.log 的 85 处锁错误）。
        return connect_sqlite(self._db_path)

    def _ensure_schema_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._ensure_schema_sync)

    def _save_sync(self, points: list[DataPoint], task_id: str) -> dict[str, int]:
        """批量入库（幂等 upsert）—— **锁竞争重试**，别把数据丢掉。

        2026-10-05 实测事故的同类现场：pilot 启动期在重建索引（持写锁 >20s），
        此时任何写都会在 `busy_timeout` 之后抛 `database is locked`。
        索引写失败可以降级（`smart_fetch._safe_upsert_meta`），**数据写不行** ——
        丢了就是"采到了但没落库"，所以这里按项目既有范式重试（幂等 ⇒ 安全）。
        """
        def _write() -> dict[str, int]:
            inserted = 0
            with self._connect() as conn:
                for p in points:
                    row = point_to_row(p, task_id)
                    cursor = conn.execute(_INSERT_SQL, tuple(row[c] for c in COLUMNS))
                    inserted += cursor.rowcount
            return {"inserted": inserted, "skipped": len(points) - inserted,
                    "total": len(points)}

        return retry_on_locked(_write)

    async def save_points(self, points: list[DataPoint], task_id: str) -> dict[str, int]:
        """批量入库；同键(指标,期间,哈希)重复自动跳过（幂等）。"""
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._save_sync, points, task_id)

    def _query_sync(
        self, indicator: str, start_date: str | None, end_date: str | None
    ) -> list[DataPoint]:
        sql = "SELECT * FROM fact_data_points WHERE indicator = ?"
        params: list[Any] = [indicator]
        if start_date:
            sql += " AND period_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND period_date <= ?"
            params.append(end_date)
        sql += " ORDER BY period_date"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_point(row) for row in rows]

    async def query_points(
        self, indicator: str, start_date: str | None = None, end_date: str | None = None
    ) -> list[DataPoint]:
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._query_sync, indicator, start_date, end_date)

    def _query_batch_sync(
        self, indicators: list[str],
        start_date: str | None, end_date: str | None,
        limit_per_indicator: int | None,
    ) -> dict[str, list[DataPoint]]:
        """SQLite 优化版批量查询：单次 `WHERE indicator IN (...)` + Python 端分组。

        ## ★ 2026-09-28 第十三轮修正：limit 必须下推到 SQL

        原实现在 Python 端截断：

            rows = conn.execute(sql).fetchall()      # ← 拉**全部**行
            ...
            if limit_per_indicator is not None:
                grouped[ind] = pts[-limit:]          # ← 才截断

        后果：`fact_data_points` 有 **199 万行**，某指标若有 10 万行，
        就要 fetch 10 万行 + 构造 10 万个 DataPoint，再扔掉 99.94%。
        实测：批量查询 **1491ms**（审计 R7 判 FAIL）。

        修正：用窗口函数把 limit 下推到 SQL —— 只取每个指标的最近 N 条。
        这是"参数存在但没真正生效"的典型（AGENTS.md：
        「我改了」是意图，「它生效了」才是事实）。

        SQLite 窗口函数需 ≥3.25（2018-09 起）；旧版本降级为原行为。
        """
        if not indicators:
            return {}
        placeholders = ",".join("?" for _ in indicators)
        params: list[Any] = list(indicators)
        date_clause = ""
        if start_date:
            date_clause += " AND period_date >= ?"
            params.append(start_date)
        if end_date:
            date_clause += " AND period_date <= ?"
            params.append(end_date)

        if limit_per_indicator is not None and limit_per_indicator > 0:
            # ★ 下推：ROW_NUMBER 按指标分区、按 period_date 倒序编号，只取最近 N 条。
            #   外层再按升序排回来（契约要求升序返回）。
            sql = (
                "SELECT * FROM ("
                "  SELECT *, ROW_NUMBER() OVER ("
                "    PARTITION BY indicator ORDER BY period_date DESC"
                "  ) AS _rn FROM fact_data_points "
                f"  WHERE indicator IN ({placeholders}){date_clause}"
                f") WHERE _rn <= ? ORDER BY indicator, period_date"
            )
            params.append(int(limit_per_indicator))
        else:
            sql = (f"SELECT * FROM fact_data_points "
                   f"WHERE indicator IN ({placeholders}){date_clause} "
                   f"ORDER BY indicator, period_date")

        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # SQLite < 3.25 不支持窗口函数 → 降级为原行为（慢但对）
            if "ROW_NUMBER" not in str(exc).upper() and "syntax" not in str(exc).lower():
                raise
            logger.warning(
                "SQLite 不支持窗口函数（%s），批量查询降级为全量拉取", exc)
            fallback_sql = (
                f"SELECT * FROM fact_data_points "
                f"WHERE indicator IN ({placeholders}){date_clause} "
                f"ORDER BY indicator, period_date")
            with self._connect() as conn:
                rows = conn.execute(
                    fallback_sql, params[:-1]).fetchall()

        grouped: dict[str, list[DataPoint]] = {ind: [] for ind in indicators}
        for row in rows:
            ind = row["indicator"]
            grouped.setdefault(ind, []).append(self._row_to_point(row))
        return grouped

    async def query_points_batch(
        self, indicators: list[str], *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit_per_indicator: int | None = None,
    ) -> dict[str, list[DataPoint]]:
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(
            self._query_batch_sync, indicators,
            start_date, end_date, limit_per_indicator,
        )

    @staticmethod
    def _row_to_point(row: sqlite3.Row) -> DataPoint:
        raw = row["extra_json"]
        try:
            extra: Any = json.loads(raw)
        except json.JSONDecodeError:
            extra = ast.literal_eval(raw)  # 兼容历史str(dict)格式行
        return DataPoint(
            data_id=row["data_id"],
            indicator=row["indicator"],
            value=row["value"],
            unit=row["unit"],
            period_date=row["period_date"],
            extra=extra,
            source_name=row["source_name"],
            source_url=row["source_url"],
            source_type=DataSourceType(row["source_type"]),
            publish_time=_parse_dt(row["publish_time"]),
            fetch_time=_parse_dt(row["fetch_time"]) or datetime.now(),
            fetch_method=FetchMethod(row["fetch_method"]),
            raw_content_hash=row["raw_content_hash"],
            processed_by=row["processed_by"],
            process_time=_parse_dt(row["process_time"]) or datetime.now(),
            confidence=row["confidence"],
            verified=bool(row["verified"]),
        )

    def _delete_sync(
        self, indicator: str, start_date: str | None, end_date: str | None
    ) -> int:
        sql = "DELETE FROM fact_data_points WHERE indicator = ?"
        params: list[Any] = [indicator]
        if start_date:
            sql += " AND period_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND period_date <= ?"
            params.append(end_date)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount

    async def delete_points(
        self, indicator: str, start_date: str | None = None, end_date: str | None = None
    ) -> int:
        """数据修正/坏点清理：按指标（可选区间）删除，返回删除行数。"""
        return await asyncio.to_thread(
            self._delete_sync, indicator, start_date, end_date)

    def _delete_by_source_sync(self, source_name: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM fact_data_points WHERE source_name = ?",
                (source_name,),
            )
            return cursor.rowcount

    async def delete_points_by_source(self, source_name: str) -> int:
        """按来源删除数据点（源退役/坏点清理），返回删除行数。"""
        return await asyncio.to_thread(self._delete_by_source_sync, source_name)

    #: 保留期口径的 WHERE —— **删除与计数共用同一份**。
    #: 分成两份必然漂移，而漂移的症状是"dry-run 说 0 行、真跑删掉一堆"，
    #: 且不报错。见 `prune_before` 的 `dry_run` 说明。
    _PRUNE_WHERE = ("period_date IS NOT NULL AND period_date != '' "
                    "AND period_date < ?")

    def _prune_before_sync(self, cutoff_date: str, *,
                           dry_run: bool = False) -> int:
        # 仅删可定期间且早于截止线的行；空 period_date 不参与日期型保留。
        with self._connect() as conn:
            if dry_run:
                row = conn.execute(
                    "SELECT COUNT(*) FROM fact_data_points "
                    f"WHERE {self._PRUNE_WHERE}",
                    (cutoff_date,)).fetchone()
                return int(row[0] or 0) if row else 0
            cursor = conn.execute(
                f"DELETE FROM fact_data_points WHERE {self._PRUNE_WHERE}",
                (cutoff_date,),
            )
            return cursor.rowcount

    async def prune_before(self, cutoff_date: str, *,
                           dry_run: bool = False) -> int:
        """保留策略：删除所有早于 cutoff_date（YYYY-MM-DD）的数据点。

        `dry_run=True`：**只数不改**（与删除共用同一个 WHERE）。
        """
        return await asyncio.to_thread(self._prune_before_sync, cutoff_date,
                                       dry_run=dry_run)

    def _count_sync(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT indicator, COUNT(*) AS n FROM fact_data_points GROUP BY indicator"
            ).fetchall()
        return {row["indicator"]: row["n"] for row in rows}

    async def count_by_indicator(self) -> dict[str, int]:
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._count_sync)
