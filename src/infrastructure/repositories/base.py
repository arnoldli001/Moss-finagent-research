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

    @abstractmethod
    async def count_by_indicator(self) -> dict[str, int]:
        """各指标行数统计（健康检查/冒烟用）。"""

    async def close(self) -> None:
        """释放连接资源（默认无操作，连接池后端覆盖）。"""
