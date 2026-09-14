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
import os
import sqlite3
from datetime import datetime
from typing import Any

from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.repositories._mapping import COLUMNS, point_to_row
from src.infrastructure.repositories.base import DataPointRepository

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

    def __init__(self, db_path: str = "data/moss_finagent.db") -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._ensure_schema_sync)

    def _save_sync(self, points: list[DataPoint], task_id: str) -> dict[str, int]:
        inserted = 0
        with self._connect() as conn:
            for p in points:
                row = point_to_row(p, task_id)
                cursor = conn.execute(_INSERT_SQL, tuple(row[c] for c in COLUMNS))
                inserted += cursor.rowcount
        return {"inserted": inserted, "skipped": len(points) - inserted, "total": len(points)}

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

    def _count_sync(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT indicator, COUNT(*) AS n FROM fact_data_points GROUP BY indicator"
            ).fetchall()
        return {row["indicator"]: row["n"] for row in rows}

    async def count_by_indicator(self) -> dict[str, int]:
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._count_sync)
