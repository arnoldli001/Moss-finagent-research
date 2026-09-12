"""多数据源连接器路由：按指标把fetch分发到首个命中的连接器。

A01采集Agent只依赖单一FetchBackend协议，本路由对其透明；
新增数据源（未来的付费产业接口）只需追加一条(connector, supports)路由。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint
from src.infrastructure.connectors.base import BaseConnector


class ConnectorRouter(BaseConnector):
    """有序路由：supports谓词首个命中者处理该指标。"""

    source_name = "router"
    source_url = ""

    def __init__(
        self, routes: list[tuple[BaseConnector, Callable[[str], bool]]]
    ) -> None:
        self._routes = routes

    def _resolve(self, indicator: str) -> BaseConnector:
        for connector, supports in self._routes:
            if supports(indicator):
                return connector
        known = [
            ind
            for connector, _ in self._routes
            for ind in connector.get_capabilities().get("indicators", [])
        ]
        raise DataFetchError(
            f"无连接器支持指标 {indicator}；已注册: {', '.join(map(str, known))}"
        )

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        return await self._resolve(indicator).fetch(indicator, start_date, end_date)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": "ConnectorRouter",
            "routes": [
                connector.get_capabilities() for connector, _ in self._routes
            ],
        }
