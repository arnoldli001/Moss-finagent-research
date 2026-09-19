"""事件/告警仓储 SQLite 实现（infrastructure，参数化SQL + 线程池化IO）。

组装 EventSqliteStore（连接/迁移）+ AlertSqliteMixin（fact_alerts）；
事件侧 fact_events 本模块实现。event_key/alert_key UNIQUE + INSERT OR IGNORE
保证采集与告警幂等；content_key 支撑跨源同文抑制（G3）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.domain.alerts.models import DEFAULT_TENANT, Event
from src.domain.alerts.repository import EventRepository
from src.infrastructure.repositories.alert_sqlite_repo import AlertSqliteMixin
from src.infrastructure.repositories.event_sqlite_base import (
    _EVENT_COLS,
    EventSqliteStore,
    event_to_row,
    insert_sql,
    retry_on_locked,
    row_to_event,
)


class EventSqliteRepository(AlertSqliteMixin, EventSqliteStore, EventRepository):
    """事件/告警 SQLite 仓储（阻塞IO经 asyncio.to_thread 卸载）。"""

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._schema_sync)

    def _upsert_events_sync(self, events: list[Event]) -> dict[str, int]:
        self._sync_once()
        sql = insert_sql("fact_events", _EVENT_COLS)

        def _write() -> int:
            inserted = 0
            with self._connect() as conn:
                for event in events:
                    inserted += conn.execute(sql, event_to_row(event)).rowcount
            return inserted

        # 采集入库撞上主库其它写入者时重试，避免整轮扫描的采集结果白丢。
        inserted = retry_on_locked(_write)
        return {"inserted": inserted, "skipped": len(events) - inserted}

    async def upsert_events(self, events: list[Event]) -> dict[str, int]:
        return await asyncio.to_thread(self._upsert_events_sync, events)

    async def list_events(
        self, event_type: str | None = None, limit: int = 100,
        tenant_id: str = DEFAULT_TENANT,
    ) -> list[Event]:
        return await asyncio.to_thread(
            self._list_events_sync, event_type, limit, tenant_id)

    def _list_events_sync(
        self, event_type: str | None, limit: int, tenant_id: str,
    ) -> list[Event]:
        self._sync_once()
        sql = "SELECT * FROM fact_events WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY created_at DESC, event_id DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_event(r) for r in rows]

    async def existing_event_keys(
        self, event_keys: list[str], tenant_id: str = DEFAULT_TENANT,
    ) -> set[str]:
        if not event_keys:
            return set()

        def _query() -> set[str]:
            self._sync_once()
            placeholders = ",".join("?" for _ in event_keys)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT event_key FROM fact_events "
                    f"WHERE tenant_id = ? AND event_key IN ({placeholders})",
                    [tenant_id, *event_keys],
                ).fetchall()
            return {row["event_key"] for row in rows}

        return await asyncio.to_thread(_query)

    def _list_unanalyzed_sync(
        self, limit: int, tenant_id: str,
    ) -> list[Event]:
        self._sync_once()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fact_events WHERE tenant_id = ? "
                "AND COALESCE(analyzed, 0) = 0 "
                "ORDER BY COALESCE(NULLIF(publish_time, ''), fetch_time) DESC, "
                "created_at DESC LIMIT ?",
                (tenant_id, int(limit)),
            ).fetchall()
        return [row_to_event(r) for r in rows]

    async def list_unanalyzed_events(
        self, limit: int = 100, tenant_id: str = DEFAULT_TENANT,
    ) -> list[Event]:
        return await asyncio.to_thread(
            self._list_unanalyzed_sync, limit, tenant_id)

    def _mark_analyzed_sync(
        self, event_ids: list[str], tenant_id: str,
    ) -> int:
        if not event_ids:
            return 0
        self._sync_once()
        placeholders = ",".join("?" for _ in event_ids)

        def _write() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    f"UPDATE fact_events SET analyzed = 1 "
                    f"WHERE tenant_id = ? AND event_id IN ({placeholders})",
                    [tenant_id, *event_ids],
                )
                return cur.rowcount

        # 标记失败会导致下轮重复LLM分析（浪费额度），因此同样做锁重试。
        return retry_on_locked(_write)

    async def mark_events_analyzed(
        self, event_ids: list[str], tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        return await asyncio.to_thread(
            self._mark_analyzed_sync, event_ids, tenant_id)
