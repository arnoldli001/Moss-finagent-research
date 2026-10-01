"""缓存型新闻读取层：包装 NewsFetcher，TTL 内重复分析不重复网络。

读取顺序（与连接器三级短路同思路）：
  1. 进程内 TTL（内存 dict，毫秒级）
  2. 新闻缓存仓储（SQLite，fetch_time 在 TTL 内有效）——进程重启后仍命中
  3. 底层网络 fetcher（个股新闻/全球主题快讯）——成功后回填内存与仓储

安全：网络返回空或异常时不写缓存（防空缓存固化）；新闻为增强链路，
仓储/网络异常都不向上抛，返回 []。键：stock:{code} / topic:{关键词哈希}。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime
from typing import Any

from src.infrastructure.connectors.news_fetcher import NewsFetcher
from src.infrastructure.repositories.news_cache_sqlite_repo import (
    NewsCacheRepository,
)

logger = logging.getLogger(__name__)


class CachedNewsFetcher:
    """新闻读取层：TTL → 仓储 → 网络；对外契约与 NewsFetcher 完全一致。"""

    def __init__(
        self,
        primary: NewsFetcher,
        repo: NewsCacheRepository | None = None,
        ttl_seconds: int = 600,
    ) -> None:
        self._primary = primary
        self._repo = repo
        self._ttl = max(1, int(ttl_seconds))
        self._mem: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def stock_key(code: str) -> str:
        return f"stock:{code}"

    @staticmethod
    def topic_key(keywords: list[str]) -> str:
        """规范关键词集合的稳定哈希：顺序无关、去重、大小写不敏感。"""
        norm = sorted({k.strip().lower() for k in keywords if k.strip()})
        digest = hashlib.sha1(",".join(norm).encode("utf-8")).hexdigest()[:16]
        return f"topic:{digest}"

    def _mem_hit(self, key: str) -> list[dict[str, Any]] | None:
        hit = self._mem.get(key)
        if hit and hit[0] > time.monotonic():
            return list(hit[1])
        if hit:
            self._mem.pop(key, None)
        return None

    def _repo_age(self, rec: dict[str, Any]) -> float | None:
        """仓储记录的年龄（秒）；fetch_time 无法解析时返回 None。"""
        try:
            fetched = datetime.fromisoformat(str(rec["fetch_time"]))
        except (ValueError, TypeError):
            return None
        return (datetime.now() - fetched).total_seconds()

    @staticmethod
    def _latest_publish(items: list[dict[str, Any]]) -> str:
        times = [str(i.get("publish_time") or "") for i in items]
        return max(times, default="")

    async def _read_cached(self, key: str) -> list[dict[str, Any]] | None:
        """依次查内存与仓储；返回命中条目，未命中/超期返回 None。"""
        mem = self._mem_hit(key)
        if mem is not None:
            return mem
        if self._repo is None:
            return None
        try:
            rec = await self._repo.get(key)
        except Exception as exc:  # noqa: BLE001 仓储 fail-open
            logger.debug("新闻缓存仓储读取失败(%s): %s", key, exc)
            return None
        if not rec:
            return None
        age = self._repo_age(rec)
        if age is None or age > self._ttl:
            return None
        items = list(rec.get("items") or [])
        if not items:
            return None
        # 回填内存，剩余存活对齐仓储 fetch_time（而非整段 TTL）
        remaining = max(1.0, self._ttl - age)
        self._mem[key] = (time.monotonic() + remaining, list(items))
        return items

    async def _store(self, key: str, scope: str,
                     items: list[dict[str, Any]]) -> None:
        from src.infrastructure.repositories.news_cache_sqlite_repo import now_iso

        self._mem[key] = (time.monotonic() + self._ttl, list(items))
        if self._repo is None:
            return
        try:
            await self._repo.upsert(
                key, scope, items, now_iso(),
                latest_publish_time=self._latest_publish(items))
        except Exception as exc:  # noqa: BLE001 落库失败不影响本次返回
            logger.debug("新闻缓存写入失败(%s): %s", key, exc)

    async def _fetch(self, key: str, scope: str,
                     network: Any) -> list[dict[str, Any]]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self._read_cached(key)
            if cached is not None:
                return cached
            try:
                items = await network()
            except Exception as exc:  # noqa: BLE001 新闻增强链路不抛
                logger.warning("新闻网络获取异常(%s): %s", key, exc)
                return []
            if items:  # 仅非空才缓存，防空缓存固化
                await self._store(key, scope, items)
            return list(items)

    async def fetch_news(self, code: str, limit: int | None = None
                         ) -> list[dict[str, Any]]:
        """`limit=None` = **用被包装者的默认值**，**不要**把 None 透传下去。

        ★ 2026-09-30（`CHG-0135`）实测线上：
        `TypeError: '>' not supported between instances of 'NoneType' and 'int'`
        —— 就在这条透传上。`None` 一路传到 `stock_news._build_url` 的
        `max(1, limit)`，而 `max(1, None)` 在 Python 里是**比较**，直接抛；
        表现是"个股新闻整条取不到"，而根因只是**一个参数的默认值语义**。
        所以这里显式区分"没给"与"给了具体值"（`**kwargs` 省略 vs 传入）。
        """
        key = self.stock_key(code)
        kwargs = {} if limit is None else {"limit": limit}
        return await self._fetch(
            key, "stock",
            lambda: self._primary.fetch_news(code, **kwargs))

    async def fetch_topic_news(
        self, keywords: list[str], limit: int | None = None
    ) -> list[dict[str, Any]]:
        """同上（`CHG-0135`）：`None` 不透传。"""
        key = self.topic_key(keywords)
        kwargs = {} if limit is None else {"limit": limit}
        return await self._fetch(
            key, "topic",
            lambda: self._primary.fetch_topic_news(keywords, **kwargs))
