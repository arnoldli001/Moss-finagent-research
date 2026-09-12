"""PostgreSQL数据点仓储（asyncpg异步驱动，连接池懒初始化）。

与SQLite后端共享DataPointRepository端口与行映射，schema同构；
幂等写入依赖等价唯一约束 + ON CONFLICT DO NOTHING。

设计约束：
- 懒连接：构造时不连库，首次IO才建池，避免无PG环境下整个应用无法启动；
- DSN兼容SQLAlchemy形式（postgresql+asyncpg://）与asyncpg原生形式；
- 连接失败抛出带排障提示的RepositoryError，不静默降级到错误的库。
"""

from __future__ import annotations

from typing import Any

from src.core.exceptions import ConfigError
from src.core.schemas import DataPoint
from src.infrastructure.repositories._mapping import COLUMNS, point_to_row, row_to_point
from src.infrastructure.repositories.base import DataPointRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_data_points (
    data_id TEXT PRIMARY KEY,
    indicator TEXT NOT NULL,
    value DOUBLE PRECISION,
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
    confidence DOUBLE PRECISION NOT NULL,
    verified SMALLINT NOT NULL DEFAULT 0,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(indicator, period_date, raw_content_hash)
);
CREATE INDEX IF NOT EXISTS idx_points_indicator_period
    ON fact_data_points(indicator, period_date);
"""

_INSERT_SQL = (
    "INSERT INTO fact_data_points (" + ", ".join(COLUMNS) + ") VALUES ("
    + ", ".join(f"${i + 1}" for i in range(len(COLUMNS)))
    + ") ON CONFLICT (indicator, period_date, raw_content_hash) DO NOTHING"
)


def normalize_dsn(dsn: str) -> str:
    """postgresql+asyncpg://user:pw@host:port/db → postgresql://...（asyncpg原生DSN）。"""
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


class PostgresRepository(DataPointRepository):
    """PostgreSQL后端（单连接池，Demo规模足够；高并发可换create_pool分池）。"""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5) -> None:
        self._dsn = normalize_dsn(dsn)
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover - 依赖已声明，兜底提示
                raise ConfigError(
                    "PostgreSQL后端需要asyncpg：uv add asyncpg（或 uv sync --extra data）"
                ) from exc
            try:
                self._pool = await asyncpg.create_pool(
                    self._dsn, min_size=self._min_size, max_size=self._max_size
                )
            except OSError as exc:
                raise ConfigError(
                    f"无法连接PostgreSQL（{self._dsn}）：{exc}。"
                    "检查 docker compose up postgres 是否就绪，或设置 DATA_BACKEND=sqlite。"
                ) from exc
        return self._pool

    async def ensure_schema(self) -> None:
        pool = await self._get_pool()
        await pool.execute(_SCHEMA)

    async def save_points(self, points: list[DataPoint], task_id: str) -> dict[str, int]:
        if not points:
            return {"inserted": 0, "skipped": 0, "total": 0}
        pool = await self._get_pool()
        rows = [tuple(point_to_row(p, task_id)[c] for c in COLUMNS) for p in points]
        async with pool.acquire() as conn:
            await conn.execute(_SCHEMA)  # 首写兜底建表（正常路径ensure_schema已建）
            inserted = 0
            # executemany不回传逐行影响数，逐条写入换取幂等计数（Demo数据量可接受）
            for row in rows:
                tag = await conn.execute(_INSERT_SQL, *row)
                if tag == "INSERT 0 1":
                    inserted += 1
        return {"inserted": inserted, "skipped": len(points) - inserted, "total": len(points)}

    async def query_points(
        self, indicator: str, start_date: str | None = None, end_date: str | None = None
    ) -> list[DataPoint]:
        sql = "SELECT * FROM fact_data_points WHERE indicator = $1"
        params: list[Any] = [indicator]
        if start_date:
            params.append(start_date)
            sql += f" AND period_date >= ${len(params)}"
        if end_date:
            params.append(end_date)
            sql += f" AND period_date <= ${len(params)}"
        sql += " ORDER BY period_date"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(sql, *params)
        return [row_to_point(dict(r)) for r in records]

    async def count_by_indicator(self) -> dict[str, int]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT indicator, COUNT(*) AS n FROM fact_data_points GROUP BY indicator"
            )
        return {r["indicator"]: int(r["n"]) for r in records}

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
