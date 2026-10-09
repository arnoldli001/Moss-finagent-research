"""新闻/快讯缓存仓储：抽象端口 + SQLite 实现。

用途：投研分析的新闻（个股新闻/全球主题快讯）被获取后落库，TTL 内重复
分析直接读缓存，避免每次都走网络。新闻属增强链路，仓储失败一律 fail-open。

表 news_cache：cache_key 为「stock:{code}」或「topic:{关键词哈希}」，
payload_json 存该键本次获取的条目数组；fetch_time 用于 TTL 判定与 prune。
阻塞 SQLite IO 一律 asyncio.to_thread，值参数全部占位。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from src.infrastructure.repositories.event_sqlite_base import connect_sqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_cache (
    cache_key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    latest_publish_time TEXT NOT NULL DEFAULT '',
    fetch_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_cache_fetch ON news_cache(fetch_time);
"""

_UPSERT_SQL = (
    "INSERT INTO news_cache (cache_key, scope, payload_json, "
    "latest_publish_time, fetch_time) VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(cache_key) DO UPDATE SET "
    "scope=excluded.scope, payload_json=excluded.payload_json, "
    "latest_publish_time=excluded.latest_publish_time, "
    "fetch_time=excluded.fetch_time"
)


class NewsCacheRepository(ABC):
    """新闻缓存仓储端口（读取层只依赖本抽象，不感知底层数据库）。"""

    @abstractmethod
    async def ensure_schema(self) -> None:
        """幂等建表/索引。"""

    @abstractmethod
    async def get(self, cache_key: str) -> dict[str, Any] | None:
        """返回 {"items": [...], "fetch_time": iso}；未命中返回 None。"""

    @abstractmethod
    async def upsert(
        self,
        cache_key: str,
        scope: str,
        items: list[dict[str, Any]],
        fetch_time: str,
        latest_publish_time: str = "",
    ) -> None:
        """写入/更新某键缓存。"""

    @abstractmethod
    async def prune(self, before_fetch_time: str, *,
                    dry_run: bool = False) -> int:
        """删除 fetch_time 早于给定 ISO 时间的行，返回行数。

        `dry_run=True`：**只数不改**（与删除共用同一 WHERE）。
        """


class NewsCacheSqliteRepository(NewsCacheRepository):
    """新闻缓存仓储 SQLite 实现（线程池化阻塞 IO）。"""

    def __init__(self, db_path: str | None = None) -> None:
        # 默认取**本环境**的应用库 —— `AGENTS.md`：默认值即护栏，安全的一侧做成默认。
        # 原先写死 `data/moss_finagent.db`（三档隔离**共用**的遗留主库）：
        # 一次漏传 `db_path` 就让隔离档写到共享库上，而没有任何地方声明过（CHG-0069）。
        from src.infrastructure.catalog.data_stores import default_app_db

        self._db_path = db_path or default_app_db()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path)

    def _ensure_schema_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        self._ready = True

    def _ready_once(self) -> None:
        """惰性建表（build_runtime 为同步，无法 await；幂等且并发安全）。"""
        if not self._ready:
            self._ensure_schema_sync()

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._ensure_schema_sync)

    def _get_sync(self, cache_key: str) -> dict[str, Any] | None:
        self._ready_once()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, fetch_time FROM news_cache "
                "WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            items: Any = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return {"items": items, "fetch_time": row["fetch_time"]}

    async def get(self, cache_key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, cache_key)

    def _upsert_sync(
        self,
        cache_key: str,
        scope: str,
        items: list[dict[str, Any]],
        fetch_time: str,
        latest_publish_time: str,
    ) -> None:
        self._ready_once()
        payload = json.dumps(items, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                _UPSERT_SQL,
                (cache_key, scope, payload, latest_publish_time, fetch_time),
            )

    async def upsert(
        self,
        cache_key: str,
        scope: str,
        items: list[dict[str, Any]],
        fetch_time: str,
        latest_publish_time: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._upsert_sync, cache_key, scope, items,
            fetch_time, latest_publish_time)

    #: 保留期口径的 WHERE —— **删除与计数共用同一份**（理由见 ABC 的 `prune`）。
    _PRUNE_WHERE = "fetch_time < ?"

    def _prune_sync(self, before_fetch_time: str, *,
                    dry_run: bool = False) -> int:
        self._ready_once()
        with self._connect() as conn:
            if dry_run:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM news_cache WHERE {self._PRUNE_WHERE}",
                    (before_fetch_time,)).fetchone()
                return int(row[0] or 0) if row else 0
            cursor = conn.execute(
                f"DELETE FROM news_cache WHERE {self._PRUNE_WHERE}",
                (before_fetch_time,),
            )
            return cursor.rowcount

    async def prune(self, before_fetch_time: str, *,
                    dry_run: bool = False) -> int:
        return await asyncio.to_thread(self._prune_sync, before_fetch_time,
                                       dry_run=dry_run)


def now_iso() -> str:
    """当前本地时间 ISO（缓存 fetch_time 用）。"""
    return datetime.now().isoformat(timespec="seconds")
