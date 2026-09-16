"""告警表 SQLite CRUD 与过期状态机（fact_alerts，EventSqliteRepository混入）。

过期处理（FR-8/AC-6）：读路径懒迁移——list/count/已读前把
expire_time<now 的 active/read 告警统一置为 expired；默认列表隐藏
expired，status='expired' 可显式查询，未读红点到期自动消退。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.domain.alerts.models import DEFAULT_TENANT, Alert
from src.domain.alerts.normalize import now_iso
from src.infrastructure.repositories.event_sqlite_base import (
    _ALERT_COLS,
    alert_to_row,
    insert_sql,
    row_to_alert,
)


class AlertSqliteMixin:
    """告警CRUD（依赖 EventSqliteStore 的 _connect/_sync_once）。"""

    def _upsert_alerts_sync(self, alerts: list[Alert]) -> dict[str, int]:
        self._sync_once()
        sql = insert_sql("fact_alerts", _ALERT_COLS)
        inserted = 0
        with self._connect() as conn:
            for alert in alerts:
                inserted += conn.execute(sql, alert_to_row(alert)).rowcount
        return {"inserted": inserted, "skipped": len(alerts) - inserted}

    async def upsert_alerts(self, alerts: list[Alert]) -> dict[str, int]:
        return await asyncio.to_thread(self._upsert_alerts_sync, alerts)

    @staticmethod
    def _expire_due(conn, now_text: str, tenant_id: str) -> None:
        """懒过期：到期的活动/已读告警统一置expired（参数化SQL）。"""
        conn.execute(
            "UPDATE fact_alerts SET status = 'expired' "
            "WHERE tenant_id = ? AND status IN ('active', 'read') "
            "AND expire_time != '' AND expire_time < ?",
            (tenant_id, now_text),
        )

    async def list_alerts(
        self, alert_type: str | None = None, alert_level: str | None = None,
        status: str | None = None, limit: int = 100,
        include_expired: bool = False, tenant_id: str = DEFAULT_TENANT,
    ) -> list[Alert]:
        return await asyncio.to_thread(
            self._list_alerts_sync, alert_type, alert_level, status,
            limit, include_expired, tenant_id)

    def _list_alerts_sync(
        self, alert_type: str | None, alert_level: str | None,
        status: str | None, limit: int, include_expired: bool,
        tenant_id: str,
    ) -> list[Alert]:
        self._sync_once()
        sql = "SELECT * FROM fact_alerts WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        with self._connect() as conn:
            self._expire_due(conn, now_iso(), tenant_id)
            if status:
                sql += " AND status = ?"
                params.append(status)
            elif not include_expired:
                sql += " AND status <> 'expired'"  # 默认隐藏过期
            for column, value in (
                ("alert_type", alert_type), ("alert_level", alert_level),
            ):
                if value:
                    sql += f" AND {column} = ?"
                    params.append(value)
            sql += " ORDER BY trigger_time DESC, alert_id DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
        return [row_to_alert(r) for r in rows]

    async def get_alert(
        self, alert_id: str, tenant_id: str = DEFAULT_TENANT,
    ) -> Alert | None:
        def _query() -> Alert | None:
            self._sync_once()
            with self._connect() as conn:
                self._expire_due(conn, now_iso(), tenant_id)
                row = conn.execute(
                    "SELECT * FROM fact_alerts WHERE alert_id = ? AND tenant_id = ?",
                    (alert_id, tenant_id),
                ).fetchone()
            return row_to_alert(row) if row else None
        return await asyncio.to_thread(_query)

    async def mark_read(
        self, alert_id: str, tenant_id: str = DEFAULT_TENANT,
    ) -> bool:
        def _update() -> bool:
            self._sync_once()
            with self._connect() as conn:
                self._expire_due(conn, now_iso(), tenant_id)
                cur = conn.execute(
                    "UPDATE fact_alerts SET status = 'read' "
                    "WHERE alert_id = ? AND tenant_id = ? AND status = 'active'",
                    (alert_id, tenant_id),
                )
                return cur.rowcount > 0
        return await asyncio.to_thread(_update)

    async def mark_all_read(self, tenant_id: str = DEFAULT_TENANT) -> int:
        def _update() -> int:
            self._sync_once()
            with self._connect() as conn:
                self._expire_due(conn, now_iso(), tenant_id)
                cur = conn.execute(
                    "UPDATE fact_alerts SET status = 'read' "
                    "WHERE tenant_id = ? AND status = 'active'", (tenant_id,))
                return cur.rowcount
        return await asyncio.to_thread(_update)

    async def count_unread(self, tenant_id: str = DEFAULT_TENANT) -> int:
        def _count() -> int:
            self._sync_once()
            with self._connect() as conn:
                self._expire_due(conn, now_iso(), tenant_id)
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM fact_alerts "
                    "WHERE tenant_id = ? AND status = 'active'", (tenant_id,),
                ).fetchone()
            return int(row["n"])
        return await asyncio.to_thread(_count)

    async def last_alert_time(
        self, alert_key: str, tenant_id: str = DEFAULT_TENANT,
    ) -> str | None:
        def _query() -> str | None:
            self._sync_once()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT trigger_time FROM fact_alerts "
                    "WHERE tenant_id = ? AND alert_key = ?",
                    (tenant_id, alert_key),
                ).fetchone()
            return row["trigger_time"] if row else None
        return await asyncio.to_thread(_query)

    async def last_content_alert_time(
        self, content_key: str, tenant_id: str = DEFAULT_TENANT,
    ) -> str | None:
        """跨源同文抑制：查该租户同content_key最近一次告警时间。"""
        if not content_key:
            return None

        def _query() -> str | None:
            self._sync_once()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT trigger_time FROM fact_alerts "
                    "WHERE tenant_id = ? AND content_key = ? "
                    "ORDER BY trigger_time DESC LIMIT 1",
                    (tenant_id, content_key),
                ).fetchone()
            return row["trigger_time"] if row else None
        return await asyncio.to_thread(_query)
