"""Redis查询缓存装饰器：为任意DataPointRepository增加读缓存。

策略（Cache-Aside）：
- query_points：先查Redis，未命中穿透到底层仓储并回填；
- save_points：先写底层仓储，成功后失效该indicator的全部查询缓存；
- count_by_indicator：不缓存（写频高、廉价聚合）。

可用性：Redis全程fail-open——连接/读写异常只告警不阻断数据通路；
懒连接，构造时不连库，无Redis环境不影响应用启动。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.core.schemas import DataPoint
from src.infrastructure.repositories.base import DataPointRepository

logger = logging.getLogger(__name__)

_KEY_PREFIX = "finagent:dp"
_SCAN_BATCH = 200


def _cache_key(indicator: str, start_date: str | None, end_date: str | None) -> str:
    return f"{_KEY_PREFIX}:{indicator}:{start_date or '-'}:{end_date or '-'}"


class CachedRepository(DataPointRepository):
    """Redis缓存装饰器（包装底层SQLite/PostgreSQL仓储）。"""

    def __init__(
        self,
        inner: DataPointRepository,
        redis_url: str,
        *,
        ttl_seconds: int = 300,
        socket_timeout: float = 1.5,
    ) -> None:
        self._inner = inner
        self._redis_url = redis_url
        self._ttl = ttl_seconds
        self._socket_timeout = socket_timeout
        self._client: Any = None
        self._degraded = False  # 连续失败后置位，减少无谓重试（长生命周期内仍允许恢复）

    @property
    def inner(self) -> DataPointRepository:
        """被包装的底层仓储（测试/运维直接访问用）。"""
        return self._inner

    async def _get_client(self) -> Any | None:
        if self._degraded:
            return None
        if self._client is None:
            try:
                import redis.asyncio as aioredis
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Redis后端需要redis-py：uv add redis") from exc
            client = aioredis.from_url(
                self._redis_url, decode_responses=True,
                socket_connect_timeout=self._socket_timeout,
                socket_timeout=self._socket_timeout,
            )
            try:
                await client.ping()
            except Exception as exc:  # noqa: BLE001 连接失败即降级
                logger.warning("Redis不可达，查询缓存降级为直连：%s", exc)
                self._degraded = True
                await client.aclose()
                return None
            self._client = client
        return self._client

    async def ensure_schema(self) -> None:
        await self._inner.ensure_schema()

    async def save_points(self, points: list[DataPoint], task_id: str) -> dict[str, int]:
        stats = await self._inner.save_points(points, task_id)
        if stats["inserted"] > 0:
            indicators = {p.indicator for p in points}
            for indicator in indicators:
                await self._invalidate(indicator)
        return stats

    async def query_points(
        self, indicator: str, start_date: str | None = None, end_date: str | None = None
    ) -> list[DataPoint]:
        client = await self._get_client()
        key = _cache_key(indicator, start_date, end_date)
        if client is not None:
            try:
                hit = await client.get(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis读取失败，降级直连：%s", exc)
                self._degraded = True
            else:
                if hit is not None:
                    try:
                        return [DataPoint(**item) for item in json.loads(hit)]
                    except (json.JSONDecodeError, TypeError, ValueError):
                        logger.warning("Redis缓存条目反序列化失败，穿透重查：%s", key)

        points = await self._inner.query_points(indicator, start_date, end_date)

        if client is not None and points:
            try:
                await client.set(
                    key,
                    json.dumps([p.model_dump(mode="json") for p in points],
                               ensure_ascii=False, default=str),
                    ex=self._ttl,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis回填失败（不影响结果）：%s", exc)
                self._degraded = True
        return points

    async def count_by_indicator(self) -> dict[str, int]:
        return await self._inner.count_by_indicator()

    async def _invalidate(self, indicator: str) -> None:
        """删除某indicator下的全部查询缓存。"""
        client = await self._get_client()
        if client is None:
            return
        pattern = f"{_KEY_PREFIX}:{indicator}:*"
        try:
            async for batch in _scan_keys(client, pattern):
                if batch:
                    await client.delete(*batch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis缓存失效失败（下次查询自动纠正）：%s", exc)
            self._degraded = True

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        await self._inner.close()


async def _scan_keys(client: Any, pattern: str):
    """分批SCAN（避免KEYS阻塞），yield每批key列表。"""
    cursor = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=pattern, count=_SCAN_BATCH)
        yield keys
        if cursor == 0:
            break
