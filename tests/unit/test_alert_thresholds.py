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


@pytest.fixture
def settings():
    return get_settings()


# ⚠️ 本文件的断言一律**从生效设置推导边界**，不硬编码档位数字。
#
# 原因（真实代价）：这些用例原先写死旧设计档位（risk 75/60/45、opp 80/65/50、
# conf 0.70），而 `.env` 在 2026-09-24 按实测分布重标定成 70/50/35、65/50/35、0.60，
# 于是 5 条长期红灯被当成"已知噪音"写进文档 —— 而它们其实是唯一在喊
# "配置和设计不一致"的哨兵，两天后同一个漂移以"客户抱怨邮件太多"重现。
#
# 现在分工明确：
#   · **值**由 tests/unit/test_alert_models.py::test_settings_alert_defaults 守（哨兵）；
#   · **语义**由本文件守：边界含不含等号、风险是否优先、min_level 是否拦 LOW。
# 这样再来一次重标定，只该改哨兵那一处，本文件继续绿 —— 红灯只代表语义真的坏了。


def test_risk_high_boundary(engine, settings):
    """达到高挡下沿（含等号）→ 高挡风险告警。"""
    alert = engine.evaluate(
        _event(), _a(risk=settings.alert_risk_high,
                     conf=settings.alert_confidence_min))
    assert alert is not None
    assert alert.alert_type == AlertType.RISK and alert.alert_level == AlertLevel.HIGH
    assert alert.alert_key == "k1:risk"
    assert alert.expire_time > alert.trigger_time


def test_risk_low_band_suppressed_by_default_min_level(engine, settings):
    """低挡（命中最低分段）在默认 alert_min_level=medium 下不产生告警。"""
    assert settings.alert_min_level == "medium"
    assert engine.evaluate(
        _event(), _a(risk=settings.alert_risk_low, opp=0)) is None


def test_opportunity_high_boundary(engine, settings):
    alert = engine.evaluate(_event(), _a(risk=0, opp=settings.alert_opp_high))
    assert alert is not None
    assert alert.alert_type == AlertType.OPPORTUNITY
    assert alert.alert_level == AlertLevel.HIGH
    assert alert.alert_key == "k1:opportunity"


def test_opportunity_below_medium_no_alert(engine, settings):
    """低于中挡下沿一格 → 落进低挡 → 默认被 min_level 拦掉。"""
    assert engine.evaluate(
        _event(), _a(risk=0, opp=settings.alert_opp_medium - 1)) is None


def test_low_confidence_blocks_high_risk(engine, settings):
    """置信度低于门槛直接不告警（宁漏勿滥），哪怕风险分拉满。"""
    assert engine.evaluate(
        _event(), _a(risk=100, conf=settings.alert_confidence_min - 0.01)) is None


def test_risk_below_low_threshold_no_alert(engine, settings):
    """低于最低分段下沿 → 不告警。"""
    assert engine.evaluate(
        _event(), _a(risk=settings.alert_risk_low - 1, conf=0.9)) is None


def test_risk_priority_over_opportunity(engine, settings):
    """风险优先：风险分命中中挡、机会分命中高挡时，仍判风险。"""
    alert = engine.evaluate(_event(), _a(
        risk=settings.alert_risk_medium, opp=settings.alert_opp_high, conf=0.9))
    assert alert is not None
    assert alert.alert_type == AlertType.RISK
    assert alert.alert_level == AlertLevel.MEDIUM


def test_low_band_emits_when_min_level_lowed(settings):
    """alert_min_level=low 时，最低分段也生成 low 告警（阈值可调）。"""
    permissive = AlertEngine(settings.model_copy(
        update={"alert_min_level": "low"}))
    alert = permissive.evaluate(
        _event(), _a(risk=settings.alert_risk_low, conf=0.8))
    assert alert is not None
    assert alert.alert_type == AlertType.RISK and alert.alert_level == AlertLevel.LOW
    opp_low = permissive.evaluate(_event("k2"), _a(
        risk=0, opp=settings.alert_opp_low, conf=0.8))
    assert opp_low is not None and opp_low.alert_level == AlertLevel.LOW


def test_alert_carries_provenance_and_disclaimer(engine):
    alert = engine.evaluate(_event(), _a(risk=90, conf=0.95))
    assert alert is not None
    assert alert.source_url == "https://x/1" and alert.source_name == "测试源"
    assert alert.event_publish_time == "2026-09-14 08:00:00"
    assert alert.tenant_id == "tenant_001"
    assert "不构成投资建议" in alert.disclaimer
