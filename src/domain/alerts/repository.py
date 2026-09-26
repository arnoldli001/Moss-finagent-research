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

    async def delete_alerts(
        self, alert_key_prefix: str, tenant_id: str = DEFAULT_TENANT,
    ) -> dict[str, int]:
        """按 alert_key 前缀删除告警（**只服务试验数据的回滚**）。

        ## 为什么是"具体方法"而不是 `@abstractmethod`

        这条能力只有**试验副本**（`scripts/intel_copy_to_alerts.py` 写进去的
        `intelcopy:` 数据）会用到，它不属于生产告警链路。写成抽象方法会把
        **所有**现有实现一起打挂：`tests/unit/test_alert_service.py` 里有手写的
        假仓储（`FakeRepo`，约 78 行起），以及将来可能有的 postgres 实现 ——
        它们不关心回滚，却会因为"没有实现一个用不到的接口"而无法实例化，
        表现是**大量与本次改动无关的测试突然报 TypeError**。
        默认实现抛 `NotImplementedError` 而不是静默返回 0：静默的
        `{"alerts": 0, "events": 0}` 会让回滚脚本报告"清理完成"，
        而数据一条没少 —— 那是本次改动最危险的失败方式。

        ## 契约

        - 只删 `tenant_id` 下 `alert_key` **以此前缀开头**的行；
        - 前缀按**字面**匹配，`%` / `_` 不是通配符（见 SQLite 覆写里的说明）；
        - 返回 `{"alerts": n, "events": m}`，即两侧**实际受影响的行数**；
        - `alert_key_prefix` 为空串时抛 `ValueError`（空前缀 = 删光该租户全部告警）。
        """
        raise NotImplementedError("该仓储不支持按前缀删除告警")

    async def prune_alerts_before(
        self, cutoff_text: str, tenant_id: str | None = None,
        *, delete_orphan_events: bool = True,
    ) -> dict[str, int]:
        """删除触发时间早于 `cutoff_text` 的告警（保留期清理 /"溢出删除"）。

        用户口径（2026-09-26）："事件告警的信息最多保留三天，超过3天的
        信息自动溢出删除。"

        ## 与 `expire_time` / `_expire_due` 的分工（**别混**）

        两者都要，缺一不可：

        | 机制 | 动作 | 何时 | 目的 |
        |---|---|---|---|
        | `expire_time` + 懒过期 | `status='expired'` | 读路径 | 到期**不再显示** |
        | **本方法** | `DELETE` | 保留作业 | 数据**真正释放** |

        只做懒过期的话 `fact_alerts` 会**无限增长**（过期行永远留着）——
        表小的时候看不出来，攒够了就是一次查询变慢 + 库文件膨胀。

        ## 契约

        - 只删 `trigger_time < cutoff_text` 的行；`tenant_id=None` 表示全部租户；
        - `cutoff_text` 为空串时抛 `ValueError`（空 = 删光全表，绝不该发生）；
        - `delete_orphan_events=True` 时，**顺带删掉因此不再被任何告警引用的
          事件行**（理由同 `delete_alerts`：孤儿事件会被
          `list_unanalyzed_events` 当"未评估积压"反复捞出来）；
        - 返回 `{"alerts": n, "events": m}`，即两侧**实际删除的行数**。

        ## 为什么默认实现抛 NotImplementedError

        与 `delete_alerts` 同一理由：写成 `@abstractmethod` 会把所有现有实现
        （含测试里手写的 `FakeRepo`）一起打挂，报错看起来与本次改动无关。
        抛错而不是静默返回 0 —— 静默的 `{"alerts": 0}` 会让保留作业报告
        "清理完成"而数据一条没少，那是这类改动最危险的失败方式。
        """
        raise NotImplementedError("该仓储不支持按保留期清理告警")

    async def close(self) -> None:
        """释放资源（默认无操作；SQLite连接每操作即关，无需覆写）。"""
        return None
