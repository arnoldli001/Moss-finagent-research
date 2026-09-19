"""告警表 SQLite CRUD 与过期状态机（fact_alerts，EventSqliteRepository混入）。

过期处理（FR-8/AC-6）：读路径懒迁移——list/count/已读前把
expire_time<now 的 active/read 告警统一置为 expired；默认列表隐藏
expired，status='expired' 可显式查询，未读红点到期自动消退。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.domain.alerts.models import DEFAULT_TENANT, Alert
from src.domain.alerts.normalize import now_iso
from src.infrastructure.repositories.event_sqlite_base import (
    _ALERT_COLS,
    EXPIRE_TIMEOUT_S,
    alert_to_row,
    connect_sqlite,
    insert_sql,
    is_locked_error,
    retry_on_locked,
    row_to_alert,
)

logger = logging.getLogger(__name__)


class AlertSqliteMixin:
    """告警CRUD（依赖 EventSqliteStore 的 _connect/_sync_once）。"""

    def _upsert_alerts_sync(self, alerts: list[Alert]) -> dict[str, int]:
        self._sync_once()
        sql = insert_sql("fact_alerts", _ALERT_COLS)

        def _write() -> int:
            inserted = 0
            with self._connect() as conn:
                for alert in alerts:
                    inserted += conn.execute(sql, alert_to_row(alert)).rowcount
            return inserted

        # 单条告警入库失败会丢掉一次推送/邮件，这里对锁竞争做兜底重试。
        inserted = retry_on_locked(_write)
        return {"inserted": inserted, "skipped": len(alerts) - inserted}

    async def upsert_alerts(self, alerts: list[Alert]) -> dict[str, int]:
        return await asyncio.to_thread(self._upsert_alerts_sync, alerts)

    def _expire_due(self, tenant_id: str, now_text: str) -> bool:
        """懒过期：到期的活动/已读告警统一置expired（参数化SQL）。

        这是**读路径里唯一的一次写**，也是告警列表 500 的直接触发点：
        主库被做T/量化写入者占用时 `UPDATE` 抛
        `sqlite3.OperationalError: database is locked`，整个
        GET /api/v1/alerts 就 500（2026-09-18 backend.log traceback）。

        两条纪律，缺一不可：
        1. 过期是"懒迁移+幂等"的清理动作，不是读结果的一部分 —— 抢不到
           写锁就跳过并返回 False，让列表照常返回；
        2. 抢锁用**独立短超时连接**（EXPIRE_TIMEOUT_S）且**只试一次**，
           绝不复用读连接的长排队参数、也不在这里重试：服务是单事件循环 +
           to_thread 默认线程池，一次 20s 的清理等待会占死一个线程，主库
           繁忙时把线程池打满（实测占锁 12s 时连续 4 次请求全部超时），
           比 500 更糟。实测：占锁时单次 list_alerts 总耗时 ≈0.5s
           （全部是这一次抢锁），SELECT 本身不受 WAL 写事务影响。
        """
        try:
            with connect_sqlite(
                    self._db_path, timeout_s=EXPIRE_TIMEOUT_S,
                    busy_timeout_ms=int(EXPIRE_TIMEOUT_S * 1000)) as conn:
                conn.execute(
                    "UPDATE fact_alerts SET status = 'expired' "
                    "WHERE tenant_id = ? AND status IN ('active', 'read') "
                    "AND expire_time != '' AND expire_time < ?",
                    (tenant_id, now_text),
                )
            return True
        except Exception as exc:  # noqa: BLE001 库繁忙/只读等一律降级
            if is_locked_error(exc):
                logger.warning(
                    "告警懒过期跳过（主库写锁繁忙，不影响本次读取）: %s",
                    brief(exc, BRIEF_TIGHT))
                return False
            raise

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
        # 读路径一律短超时：读本身在 WAL 下几乎不阻塞，但不该为写锁排队 20s。
        with self._connect_fast() as conn:
            self._expire_due(tenant_id, now_iso())
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
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
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
            # 用户点击的接口用短超时连接：宁可快速失败让前端重试，
            # 也不要把一个请求钉在写锁上 20s。
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
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
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
                cur = conn.execute(
                    "UPDATE fact_alerts SET status = 'read' "
                    "WHERE tenant_id = ? AND status = 'active'", (tenant_id,))
                return cur.rowcount
        return await asyncio.to_thread(_update)

    async def count_unread(self, tenant_id: str = DEFAULT_TENANT) -> int:
        def _count() -> int:
            self._sync_once()
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
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
