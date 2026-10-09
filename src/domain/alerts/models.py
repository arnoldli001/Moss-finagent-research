"""事件告警领域模型（统一JSON契约，字段全部 snake_case）。

所有告警输出与前端视图必须附 DISCLAIMER 免责声明；
事件/告警均带 tenant_id（环境固定 tenant_001，接口预留多租户）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

DISCLAIMER = "事件告警由AI基于公开信息自动生成，仅供参考，不构成投资建议。"
DEFAULT_TENANT = "tenant_001"


class EventType(str, Enum):
    """事件类型：政策/板块/个股/投资日历。"""

    POLICY = "policy"
    SECTOR = "sector"
    STOCK = "stock"
    CALENDAR = "calendar"


class AlertType(str, Enum):
    """告警方向：风险/机会。"""

    RISK = "risk"
    OPPORTUNITY = "opportunity"


class AlertLevel(str, Enum):
    """告警级别。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertStatus(str, Enum):
    """告警状态机：未读/已读/过期。"""

    ACTIVE = "active"
    READ = "read"
    EXPIRED = "expired"


class EventEntities(BaseModel):
    """事件关联实体。"""

    industries: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)


class Event(BaseModel):
    """标准化事件（G2溯源：source_name/source_url/publish_time 必填可空值）。"""

    event_id: str
    event_key: str  # 来源级去重键：source_name + 规范标题 + 发布日期（保留溯源）
    content_key: str = ""  # 跨源内容键：规范标题 + 发布日期（不含来源，跨源防刷屏）
    event_type: EventType
    title: str
    content: str = ""
    source_name: str
    source_url: str = ""
    publish_time: str = ""
    fetch_time: str
    entities: EventEntities = Field(default_factory=EventEntities)
    raw_data: dict = Field(default_factory=dict)
    tenant_id: str = DEFAULT_TENANT


class AffectedStock(BaseModel):
    """受影响个股（受益/风险）。"""

    code: str = ""  # 尽力解析为6位代码，无法解析时留空仅展示名称
    name: str
    impact: str  # positive | negative | mixed
    reason: str = ""


class EventAssessment(BaseModel):
    """LLM两阶段分析结果（一个事件一条评估）。"""

    event_id: str
    event_type: EventType | None = None
    sentiment: str = "neutral"  # positive | negative | neutral
    risk_score: float = 0.0
    opportunity_score: float = 0.0
    confidence: float = 0.0
    affected_stocks: list[AffectedStock] = Field(default_factory=list)
    affected_industries: list[str] = Field(default_factory=list)
    impact_path: str = ""
    summary: str = ""
    model_used: str = ""


class Alert(BaseModel):
    """阈值引擎生成的告警。"""

    alert_id: str
    alert_key: str  # event_key + alert_type（同源24h冷却唯一）
    content_key: str = ""  # 跨源内容键 + alert_type（异源同文24h抑制）
    event_id: str
    alert_type: AlertType
    alert_level: AlertLevel
    title: str
    description: str
    risk_score: float
    opportunity_score: float
    confidence: float
    affected_stocks: list[AffectedStock] = Field(default_factory=list)
    affected_industries: list[str] = Field(default_factory=list)
    impact_path: str = ""
    source_name: str = ""
    source_url: str = ""
    event_publish_time: str = ""
    trigger_time: str
    expire_time: str
    status: AlertStatus = AlertStatus.ACTIVE
    tenant_id: str = DEFAULT_TENANT
    disclaimer: str = DISCLAIMER


class EmailSendResult(BaseModel):
    """单封告警邮件发送结果。"""

    alert_id: str
    status: str  # sent | failed | unconfigured | suppressed
    detail: str = ""


class ScanResult(BaseModel):
    """一次扫描的汇总结果（作业记录/前端轮询共用）。"""

    trigger: str = "manual"  # manual | schedule
    status: str = "success"  # success | partial | failed
    scanned: int = 0        # 标准化后事件总数
    new_events: int = 0     # 新入库事件数
    alerts_created: int = 0
    by_level: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    per_source: dict[str, int] = Field(default_factory=dict)
    email_results: list[EmailSendResult] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
