"""数据源连接器抽象基类（DATA_SOURCE_INTEGRATION.md 二）。

每个数据源实现一个连接器；fetch 返回携带溯源元数据的 DataPoint 列表。
domain层通过结构化Protocol使用连接器，不直接import本模块（依赖规则）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.core.schemas import DataPoint


class BaseConnector(ABC):
    """连接器基类：子类声明来源元数据并实现fetch。"""

    source_name: str = ""
    source_url: str = ""

    @abstractmethod
    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        """拉取指标数据，返回DataPoint列表（含溯源元数据）。"""

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]:
        """返回连接器能力描述（支持的指标/限制），用于路由与监控。"""
