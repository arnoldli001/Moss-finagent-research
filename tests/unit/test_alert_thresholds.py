"""阈值引擎边界测试（T6/FR-6，6组规则边界+风险优先）。"""

from __future__ import annotations

import pytest

from src.core.config import get_settings
from src.domain.alerts.models import (
    AlertLevel,
    AlertType,
    Event,
    EventAssessment,
    EventType,
)
from src.domain.alerts.thresholds import AlertEngine


def _event(key: str = "k1") -> Event:
    return Event(
        event_id=f"evt_{key}", event_key=key, event_type=EventType.POLICY,
        title="固态电池产业政策出台", content="支持锂电发展", source_name="测试源",
        source_url="https://x/1", publish_time="2026-09-14 08:00:00",
        fetch_time="2026-09-14T17:30:00+08:00")


def _a(risk: float = 0.0, opp: float = 0.0, conf: float = 0.9,
       summary: str = "") -> EventAssessment:
    return EventAssessment(
        event_id="evt_k1", risk_score=risk, opportunity_score=opp,
        confidence=conf, summary=summary or "AI摘要")


@pytest.fixture
def engine():
    return AlertEngine(get_settings())


def test_risk_high_boundary(engine):
    alert = engine.evaluate(_event(), _a(risk=75, conf=0.70))
    assert alert is not None
    assert alert.alert_type == AlertType.RISK and alert.alert_level == AlertLevel.HIGH
    assert alert.alert_key == "k1:risk"
    assert alert.expire_time > alert.trigger_time


def test_risk_just_below_low_no_alert(engine):
    assert engine.evaluate(_event(), _a(risk=59, opp=0)) is None


def test_opportunity_high_boundary(engine):
    alert = engine.evaluate(_event(), _a(risk=0, opp=80))
    assert alert is not None
    assert alert.alert_type == AlertType.OPPORTUNITY
    assert alert.alert_level == AlertLevel.HIGH
    assert alert.alert_key == "k1:opportunity"


def test_opportunity_below_medium_no_alert(engine):
    assert engine.evaluate(_event(), _a(risk=0, opp=64)) is None


def test_low_confidence_blocks_high_risk(engine):
    assert engine.evaluate(_event(), _a(risk=80, conf=0.69)) is None


def test_risk_below_low_threshold_no_alert(engine):
    assert engine.evaluate(_event(), _a(risk=44, conf=0.9)) is None


def test_risk_priority_over_opportunity(engine):
    alert = engine.evaluate(_event(), _a(risk=70, opp=90, conf=0.9))
    assert alert is not None
    assert alert.alert_type == AlertType.RISK
    assert alert.alert_level == AlertLevel.MEDIUM


def test_low_band_emits_when_min_level_lowed():
    """alert_min_level=low 时，[45,60) 风险档生成low告警（阈值可调）。"""
    settings = get_settings().model_copy(
        update={"alert_min_level": "low"})
    permissive = AlertEngine(settings)
    alert = permissive.evaluate(_event(), _a(risk=59, conf=0.8))
    assert alert is not None
    assert alert.alert_type == AlertType.RISK and alert.alert_level == AlertLevel.LOW
    opp_low = permissive.evaluate(_event("k2"), _a(risk=0, opp=50, conf=0.8))
    assert opp_low is not None and opp_low.alert_level == AlertLevel.LOW


def test_alert_carries_provenance_and_disclaimer(engine):
    alert = engine.evaluate(_event(), _a(risk=90, conf=0.95))
    assert alert is not None
    assert alert.source_url == "https://x/1" and alert.source_name == "测试源"
    assert alert.event_publish_time == "2026-09-14 08:00:00"
    assert alert.tenant_id == "tenant_001"
    assert "不构成投资建议" in alert.disclaimer
