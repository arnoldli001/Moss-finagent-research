"""事件/告警仓储端口（domain层抽象，依赖倒置）。

infrastructure提供具体实现（SQLite），service与api仅依赖此抽象，
禁止在领域层直接连接数据库（项目编码规范）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.domain.alerts.models import DEFAULT_TENANT, Alert, Event


class EventRepository(ABC):
    """事件与告警持久化端口。所有方法按租户隔离。"""

    @abstractmethod
    async def ensure_schema(self) -> None:
        """幂等建表。"""

    @abstractmethod
    async def upsert_events(self, events: list[Event]) -> dict[str, int]:
        """按event_key去重写入；返回 {inserted, skipped}。"""

    @abstractmethod
    async def list_events(
        self, event_type: str | None = None, limit: int = 100,
        tenant_id: str = DEFAULT_TENANT,
    ) -> list[Event]:
        """按入库时间倒序取事件。"""

    @abstractmethod
    async def existing_event_keys(
        self, event_keys: list[str], tenant_id: str = DEFAULT_TENANT,
    ) -> set[str]:
        """返回入参中已落库的event_key集合（判定新事件用，按租户隔离）。"""

    @abstractmethod
    async def list_unanalyzed_events(
        self, limit: int = 100, tenant_id: str = DEFAULT_TENANT,
    ) -> list[Event]:
        """取尚未完成LLM评估的事件（发布时间倒序），支撑手工导入闭环。"""

    @abstractmethod
    async def mark_events_analyzed(
        self, event_ids: list[str], tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """把已成功评估的事件置位analyzed；返回更新行数（按租户隔离）。"""

    @abstractmethod
    async def upsert_alerts(self, alerts: list[Alert]) -> dict[str, int]:
        """按alert_key幂等写入（INSERT OR IGNORE）；返回 {inserted, skipped}。"""

    @abstractmethod
    async def list_alerts(
        self, alert_type: str | None = None, alert_level: str | None = None,
        status: str | None = None, limit: int = 100,
        include_expired: bool = False, tenant_id: str = DEFAULT_TENANT,
    ) -> list[Alert]:
        """按触发时间倒序；默认懒过期并隐藏expired，支持类型/级别/状态过滤。

        include_expired=True且status=None时返回含过期在内的全部告警；
        status='expired'显式查询已过期告警。
        """

    @abstractmethod
    async def get_alert(
        self, alert_id: str, tenant_id: str = DEFAULT_TENANT,
    ) -> Alert | None:
        """按主键取单条告警（跨租户不可见）。"""

    @abstractmethod
    async def mark_read(
        self, alert_id: str, tenant_id: str = DEFAULT_TENANT,
    ) -> bool:
        """标记已读；命中行返回True。"""

    @abstractmethod
    async def mark_all_read(self, tenant_id: str = DEFAULT_TENANT) -> int:
        """全部活动告警置为已读；返回更新行数。"""

    @abstractmethod
    async def count_unread(self, tenant_id: str = DEFAULT_TENANT) -> int:
        """未读（status=active）告警数。"""

    @abstractmethod
    async def last_alert_time(
        self, alert_key: str, tenant_id: str = DEFAULT_TENANT,
    ) -> str | None:
        """查询某alert_key最近一次告警触发时间（同源冷却判定，按租户）。"""

    @abstractmethod
    async def last_content_alert_time(
        self, content_key: str, tenant_id: str = DEFAULT_TENANT,
    ) -> str | None:
        """查询跨源内容抑制键最近一次告警时间（异源同文冷却，按租户）。"""

    async def close(self) -> None:
        """释放资源（默认无操作；SQLite连接每操作即关，无需覆写）。"""
        return None
