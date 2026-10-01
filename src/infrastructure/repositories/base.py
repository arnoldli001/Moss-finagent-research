"""数据点仓储抽象（端口）：SQLite/PostgreSQL双后端实现同一契约。

应用层与Agent只依赖本抽象，具体后端由工厂按配置注入
（storage factory，见repository_factory.py）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.schemas import DataPoint


class DataPointRepository(ABC):
    """统一数据点仓储端口（A04存储Agent依赖，不感知底层数据库）。"""

    @abstractmethod
    async def ensure_schema(self) -> None:
        """幂等建表/建索引（后端首次写入前自动调用）。"""

    @abstractmethod
    async def save_points(self, points: list[DataPoint], task_id: str) -> dict[str, int]:
        """批量入库；同键(indicator, period_date, raw_content_hash)重复幂等跳过。

        返回 {"inserted": n, "skipped": n, "total": n}。
        """

    @abstractmethod
    async def query_points(
        self, indicator: str, start_date: str | None = None, end_date: str | None = None
    ) -> list[DataPoint]:
        """按指标+期间区间查询，按period_date升序。"""

    async def query_points_batch(
        self, indicators: list[str], *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit_per_indicator: int | None = None,
    ) -> dict[str, list[DataPoint]]:
        """批量查询多指标，一次 SELECT 返回 `{indicator: [DataPoint...]}`。

        ★ 设计目的（2026-09-28）：SmartFetcher 一次性拿全 freshness，
        避免 N 次 query_points() 的 N 次 SELECT+fetch+反序列化。

        实现：默认回退到多次 query_points（各后端无一致 SQL 模板）。
        子类可在有更高效原生批量查询时覆盖（如 `IN (...)`）。

        Args:
            indicators: 指标 id 列表
            limit_per_indicator: 单指标返回条数上限（None=全量；用于"只看最新 N 条"）
        """
        result: dict[str, list[DataPoint]] = {}
        for ind in indicators:
            rows = await self.query_points(ind, start_date, end_date)
            if limit_per_indicator is not None and len(rows) > limit_per_indicator:
                rows = rows[-limit_per_indicator:]
            result[ind] = rows
        return result

    @abstractmethod
    async def count_by_indicator(self) -> dict[str, int]:
        """各指标行数统计（健康检查/冒烟用）。"""

    @abstractmethod
    async def delete_points(
        self, indicator: str, start_date: str | None = None, end_date: str | None = None
    ) -> int:
        """按指标（可选期间区间）删除数据点，返回删除行数。

        仅用于数据修正/坏点清理（如源口径错误污染本地缓存）；
        调用方必须通过统一数据层，禁止直接操作数据库。
        """

    @abstractmethod
    async def delete_points_by_source(self, source_name: str) -> int:
        """按**来源**删除数据点，返回删除行数。

        用于「某个源整条链路作废」的清理（占位/模拟源退役、源口径错误污染本地
        缓存）。这类清理的判据是来源而不是指标 —— `delete_points` 的指标口径做
        不到：同一个 indicator id 下往往既有旧源的坏点、也有新源的好点，
        按指标删会把新源的好数据一起删掉。
        调用方必须通过统一数据层，禁止直接操作数据库。
        """

    @abstractmethod
    async def prune_before(self, cutoff_date: str) -> int:
        """保留策略：删除所有 period_date 早于截止线的数据点，返回删除行数。

        用于「最多保留最近 N 年」。仅删除可定期间（period_date 非空）且早于
        cutoff_date（`YYYY-MM-DD`）的行；period_date 为空/无法定期间的行不在
        日期型保留范围内，避免误删无期间快照。
        """

    async def close(self) -> None:
        """释放连接资源（默认无操作，连接池后端覆盖）。"""
        return None
