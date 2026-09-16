"""事件监控与自动告警领域模块。

数据流：采集器(infrastructure) → 标准化 → 去重 → EventAnalyzer(LLM)
→ AlertEngine(阈值) → EventRepository(存储) → AlertHub/邮件(通知)。
"""

from src.domain.alerts.models import (
    DISCLAIMER,
    AffectedStock,
    Alert,
    AlertLevel,
    AlertStatus,
    AlertType,
    Event,
    EventAssessment,
    EventEntities,
    EventType,
    ScanResult,
)

__all__ = [
    "DISCLAIMER",
    "AffectedStock",
    "Alert",
    "AlertLevel",
    "AlertStatus",
    "AlertType",
    "Event",
    "EventAssessment",
    "EventEntities",
    "EventType",
    "ScanResult",
]
