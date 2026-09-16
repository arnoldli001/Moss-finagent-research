"""事件告警子系统装配（composition root，仅API启动时调用）。

组装：事件仓储→采集器组（快讯多源+投资日历）→LLM分析器→阈值引擎→
WebSocket Hub→邮件通道→扫描服务。DATA_BACKEND=postgres 时工厂抛ConfigError，
由lifespan捕获降级（告警中心标注不可用，不拖垮主应用）。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.api.alert_hub import AlertHub
from src.core.config import Settings
from src.domain.alerts.analyzer import EventAnalyzer
from src.domain.alerts.repository import EventRepository
from src.domain.alerts.service import AlertScanService
from src.domain.alerts.thresholds import AlertEngine
from src.infrastructure.connectors.event_collectors import (
    CalendarCollector,
    NewsFlashCollector,
)
from src.infrastructure.connectors.security_resolver import resolve_stock
from src.infrastructure.llm import LLMGateway
from src.infrastructure.notifiers.email_notifier import EmailNotifier
from src.infrastructure.repositories.repository_factory import (
    build_event_repository,
)


@dataclass
class EventStack:
    """事件告警子系统句柄集合。"""

    service: AlertScanService
    hub: AlertHub
    repo: EventRepository
    emailer: EmailNotifier


def build_event_stack(settings: Settings, gateway: LLMGateway) -> EventStack:
    """同步装配（仓储建表由调用方await ensure_schema）。"""
    repo = build_event_repository(settings)
    collectors = [NewsFlashCollector(), CalendarCollector()]
    analyzer = EventAnalyzer(gateway, resolver=resolve_stock)
    engine = AlertEngine(settings)
    hub = AlertHub()
    emailer = EmailNotifier(settings)
    service = AlertScanService(
        collectors=collectors, repo=repo, analyzer=analyzer, engine=engine,
        hub=hub, emailer=emailer, settings=settings)
    return EventStack(service=service, hub=hub, repo=repo, emailer=emailer)
