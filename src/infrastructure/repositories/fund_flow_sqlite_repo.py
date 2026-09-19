"""资金流监控的用户选择（板块 / 个股）持久化。

与 `dim_intraday_profile` 同一套范式（原生 sqlite3 + `CREATE TABLE IF NOT EXISTS`
+ PRAGMA 补列，落在 `settings.sqlite_path`），理由也一样：这是**用户偏好**，
不该混进 14 GiB 的行情仓库；换个仓库不该把选择弄丢。

表结构刻意做成"一张表放两类实体"（`kind` 区分 sector/stock）：
选择列表永远是一起读、一起写的小集合，拆两张表只会让读写路径翻倍。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "dim_fund_flow_watch"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    added_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, code)
);
CREATE INDEX IF NOT EXISTS idx_fund_flow_kind ON {TABLE}(kind, added_at);
"""


@dataclass
class FlowWatchEntry:
    """一条被监控的实体（板块或个股）。"""

    kind: str
    code: str
    name: str = ""
    # manual=用户手动加入；default=系统给的默认热门（可被用户删掉，删了就不再自动补）
    source: str = "manual"
    added_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "code": self.code, "name": self.name,
                "source": self.source, "added_at": self.added_at}


@dataclass
class FlowWatchRepository:
    """资金流监控选择的 SQLite 仓储（`path` 为库文件路径）。"""

    path: str
    _ready: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    # ---------- 建表 ----------

    def _connect(self) -> sqlite3.Connection:
        # timeout 与 WAL 一起决定"和服务进程里其它 SQLite 写入者共存"的能力：
        # 实测（2026-09-17）与做T面板同时刷新时 `CREATE TABLE IF NOT EXISTS` 撞上
        # "database is locked"——因为服务进程里还有采集/告警两条链在写同一个库。
        # 解决：① 建表只在装配时做一次（见 `ensure_schema` 的调用点）；
        #       ② WAL + busy_timeout 让并发写排队而不是立刻报错。
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.Error):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _ensure_schema_sync(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        self._ready = True

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await asyncio.to_thread(self._ensure_schema_sync)

    # ---------- 读 ----------

    def _list_sync(self, kind: str) -> list[FlowWatchEntry]:
        self._ensure_schema_sync()
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT kind, code, name, source, added_at FROM {TABLE} "
                "WHERE kind = ? ORDER BY added_at, code", (kind,)).fetchall()
        return [FlowWatchEntry(kind=str(row["kind"]), code=str(row["code"]),
                               name=str(row["name"] or ""),
                               source=str(row["source"] or "manual"),
                               added_at=str(row["added_at"] or ""))
                for row in rows]

    async def list(self, kind: str) -> list[FlowWatchEntry]:
        """按 kind（sector / stock）列出选择，按加入时间排序。"""
        return await asyncio.to_thread(self._list_sync, kind)

    async def all(self) -> list[FlowWatchEntry]:
        async with self._lock:
            sectors = await asyncio.to_thread(self._list_sync, "sector")
            stocks = await asyncio.to_thread(self._list_sync, "stock")
        return sectors + stocks

    # ---------- 写 ----------

    def _add_sync(self, entry: FlowWatchEntry) -> None:
        self._ensure_schema_sync()
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {TABLE}(kind, code, name, source, added_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(kind, code) DO UPDATE SET "
                "name = excluded.name, source = excluded.source",
                (entry.kind, entry.code, entry.name, entry.source,
                 entry.added_at or datetime.now().astimezone().isoformat(
                     timespec="seconds")))

    async def add(self, entry: FlowWatchEntry) -> list[FlowWatchEntry]:
        """加入监控（幂等：重复加入只更新名字，不改加入时间）。"""
        async with self._lock:
            await asyncio.to_thread(self._add_sync, entry)
        return await self.list(entry.kind)

    def _remove_sync(self, kind: str, code: str) -> bool:
        self._ensure_schema_sync()
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM {TABLE} WHERE kind = ? AND code = ?", (kind, code))
            return cursor.rowcount > 0

    async def remove(self, kind: str, code: str) -> tuple[bool, list[FlowWatchEntry]]:
        async with self._lock:
            removed = await asyncio.to_thread(self._remove_sync, kind, code)
        return removed, await self.list(kind)

    def _seed_sync(self, entries: list[FlowWatchEntry]) -> int:
        """批量写默认热门（只在**该 kind 一条都没有**时调用，见 service）。"""
        self._ensure_schema_sync()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.executemany(
                f"INSERT INTO {TABLE}(kind, code, name, source, added_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(kind, code) DO NOTHING",
                [(item.kind, item.code, item.name, item.source,
                  item.added_at or now) for item in entries])
        return len(entries)

    async def seed(self, entries: list[FlowWatchEntry]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._seed_sync, entries)


def build_fund_flow_repository(settings: Any) -> FlowWatchRepository | None:
    """按配置组装仓储（与 `build_intraday_profile_repository` 同口径）。"""
    path = getattr(settings, "sqlite_path", "") or ""
    if not path:
        logger.warning("未配置 sqlite_path，资金流监控选择无法持久化（仅内存）")
        return None
    return FlowWatchRepository(path=path)
