"""事件告警领域模型与配置测试（T1）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import get_settings
from src.domain.alerts import (
    DISCLAIMER,
    Alert,
    AlertLevel,
    AlertStatus,
    AlertType,
    Event,
    EventType,
)


def test_settings_alert_defaults():
    s = get_settings()
    assert s.alert_confidence_min == 0.70
    assert s.alert_risk_high == 75
    assert s.alert_opp_high == 80
    assert s.alert_email_to == "1027312283@qq.com"
    assert s.alert_cooldown_hours == 24
    assert s.alert_smtp_host == "smtp.qq.com"


def test_event_defaults_tenant_and_required_fields():
    event = Event(
        event_id="evt_1", event_key="k", event_type=EventType.POLICY,
        title="政策标题", source_name="test", fetch_time="2026-09-14T10:00:00")
    dumped = event.model_dump()
    assert dumped["tenant_id"] == "tenant_001"
    assert dumped["entities"] == {"industries": [], "companies": [], "regions": []}
    assert dumped["source_url"] == ""


def test_event_missing_required_raises():
    with pytest.raises(ValidationError):
        Event(event_id="x", event_key="k")  # type: ignore[call-arg]


def test_alert_carries_disclaimer_and_status_flow():
    alert = Alert(
        alert_id="a1", alert_key="k:risk", event_id="evt_1",
        alert_type=AlertType.RISK, alert_level=AlertLevel.HIGH,
        title="t", description="d", risk_score=80.0, opportunity_score=10.0,
        confidence=0.9, trigger_time="2026-09-14T17:30:00",
        expire_time="2026-09-21T17:30:00")
    assert alert.status == AlertStatus.ACTIVE
    assert alert.disclaimer == DISCLAIMER
    assert "不构成投资建议" in alert.disclaimer
    dumped = alert.model_dump()
    assert "alertId" not in dumped  # 全snake_case，不允许camelCase
    assert dumped["alert_type"] == "risk"
