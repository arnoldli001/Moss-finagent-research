"""事件告警领域模型与配置测试（T1）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings, get_settings
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
    """★ 配置漂移哨兵：断言的是**真实生效值**，不是"设计值"。

    这 9 个阈值长期本地红/CI 绿（`.env` 标定过、CI 读不到 `.env`），被当成
    "已知噪音"写进文档，两天后同一个漂移以"客户抱怨邮件太多"的形式爆出来。
    现在权威值收敛进 `config.py`，**有没有 `.env` 都是这一组**，所以这条断言
    在哪里都能跑、也真的会拦住漂移。

    改阈值时必须同时改这里 + `config.py` 的说明 + `docs/ALERT_THRESHOLDS.md`；
    如果只是想让红灯变绿而放宽这里的数字，等于把哨兵绑起来。
    """
    s = get_settings()
    # 事件阈值（2026-09-24 标定，依据：24 条真快讯实测打分分布）
    assert s.alert_confidence_min == 0.60
    assert (s.alert_risk_high, s.alert_risk_medium, s.alert_risk_low) == (70, 50, 35)
    assert (s.alert_opp_high, s.alert_opp_medium, s.alert_opp_low) == (65, 50, 35)
    # 邮件分数门槛：设计口径 risk>69 / opp>=85（曾被 .env 压到 60/60）
    assert s.alert_email_risk_min_score == 69.0
    assert s.alert_email_opp_min_score == 85.0
    assert s.alert_cooldown_hours == 24
    assert s.alert_smtp_host == "smtp.qq.com"
    # 收件人是**按环境配置**的（.env 里就是真实邮箱），断生效值必然本地红/CI 绿 ——
    # 这里要守的是另一条不变量：**代码里出厂默认必须是占位符，不能把真实邮箱写进版本库**。
    assert Settings.model_fields["alert_email_to"].default == "your_qq_number@qq.com"


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
