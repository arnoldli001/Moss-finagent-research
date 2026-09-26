"""AlertHub 与 EmailNotifier 测试（T7/FR-10/FR-11）。"""

from __future__ import annotations

import pytest

from src.api.alert_hub import AlertHub
from src.core.config import get_settings
from src.domain.alerts.models import (
    AffectedStock,
    Alert,
    AlertLevel,
    AlertType,
)
from src.infrastructure.notifiers.email_notifier import EmailNotifier


def _alert(level: AlertLevel = AlertLevel.HIGH,
           atype: AlertType = AlertType.RISK,
           risk_score: float = 80.0,
           opportunity_score: float = 20.0) -> Alert:
    return Alert(
        alert_id="al_1", alert_key=f"k:{atype.value}", event_id="evt_1",
        alert_type=atype, alert_level=level, title="固态电池政策落地",
        description="锂电板块受益", risk_score=risk_score,
        opportunity_score=opportunity_score,
        confidence=0.9,
        affected_stocks=[AffectedStock(code="300308", name="示例科技",
                                       impact="positive", reason="需求放量")],
        affected_industries=["固态电池"], impact_path="政策→需求→业绩",
        source_name="测试源", source_url="https://x/1",
        event_publish_time="2026-09-14 08:00:00",
        trigger_time="2026-09-14T17:30:00+08:00",
        expire_time="2026-09-21T17:30:00+08:00")


class _FakeWebSocket:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.accepted = False
        self.sent: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict) -> None:
        if self.fail:
            raise ConnectionError("对端已断开")
        self.sent.append(message)


@pytest.mark.asyncio
async def test_hub_connect_broadcast_disconnect():
    hub = AlertHub()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket(fail=True)
    await hub.connect(ws1)
    await hub.connect(ws2)
    assert ws1.accepted and hub.client_count() == 2
    delivered = await hub.broadcast(_alert())
    assert delivered == 1  # 死连接被清理
    assert ws1.sent[0]["type"] == "alert"
    assert ws1.sent[0]["data"]["alert_id"] == "al_1"
    await hub.broadcast(_alert())
    assert hub.client_count() == 1
    hub.disconnect(ws1)
    assert hub.client_count() == 0


@pytest.mark.asyncio
async def test_hub_notify_broadcasts_a_signalling_message_across_tenants():
    """`notify` 是**信令**通道：跨租户广播，且照样清理死连接。

    它服务的是"情报流后台建好了，前端可以来拉了"这类通知（见
    `src/api/routes/intel.py`）。与 `broadcast` 的分工是硬的：

      · `broadcast`  推**告警正文** ⇒ 必须逐连接脱敏（管理员才看得到来源真名）
      · `notify`     推**只有序号/时间戳的信令** ⇒ 跨租户广播不泄漏任何东西

    ⚠️ 这条边界只能靠纪律守：往 `notify` 里塞任何业务字段，它就退化成
    "把管理员的视图发给所有人"那条老事故，而代码评审里看不出来。
    所以本用例顺带钉住"两个租户都收到了同一条**无内容**消息"。
    """
    hub = AlertHub()
    a, b, dead = _FakeWebSocket(), _FakeWebSocket(), _FakeWebSocket(fail=True)
    await hub.connect(a, "tenant_a")
    await hub.connect(b, "tenant_b")
    await hub.connect(dead, "tenant_a")

    msg = {"type": "intel_feed", "data": {"seq": 3, "built_at": "2026-10-01"}}
    assert await hub.notify(msg) == 2        # 死连接不算送达
    assert a.sent == [msg] and b.sent == [msg]
    assert hub.client_count("tenant_a") == 1, "死连接没被清理"
    # 载荷里不许出现任何业务内容（这条断言就是上面那句纪律的落点）
    assert set(msg["data"]) == {"seq", "built_at"}

    # `heartbeat` 复用了同一条出口 —— 清理逻辑只有一份
    before = len(a.sent)
    await hub.heartbeat()
    assert a.sent[before]["type"] == "heartbeat"


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


@pytest.mark.asyncio
async def test_email_unconfigured_without_credentials():
    notifier = EmailNotifier(_settings(
        alert_smtp_user="", alert_smtp_auth_code="", alert_email_to="x@qq.com"))
    result = await notifier.send(_alert())
    assert result.status == "unconfigured"
    assert notifier.is_configured() is False


@pytest.mark.asyncio
async def test_email_score_gate_boundaries(monkeypatch):
    """邮件分数门控：风险严格>69、机会≥85；未达门槛suppressed且不触网。"""
    sent_calls: list = []

    class _SMTP:
        def __init__(self, *args, **kwargs):
            sent_calls.append(args)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, *a):
            pass

        def sendmail(self, *a):
            sent_calls.append(a)

    monkeypatch.setattr(
        "src.infrastructure.notifiers.email_notifier.smtplib.SMTP_SSL", _SMTP)
    notifier = EmailNotifier(_settings(
        alert_smtp_user="sender@qq.com", alert_smtp_auth_code="code",
        alert_email_to="2693888583@qq.com"))
    assert notifier.is_configured()

    # 风险类：69不发（严格大于），70发；与站内级别无关（medium也发）
    blocked = await notifier.send(_alert(
        AlertLevel.MEDIUM, AlertType.RISK, risk_score=69.0))
    assert blocked.status == "suppressed" and not sent_calls
    sent_risk = await notifier.send(_alert(
        AlertLevel.MEDIUM, AlertType.RISK, risk_score=70.0))
    assert sent_risk.status == "sent" and sent_calls

    # 机会类：84不发，85发（含边界）
    sent_calls.clear()
    blocked_opp = await notifier.send(_alert(
        AlertLevel.HIGH, AlertType.OPPORTUNITY, risk_score=10.0,
        opportunity_score=84.0))
    assert blocked_opp.status == "suppressed" and not sent_calls
    sent_opp = await notifier.send(_alert(
        AlertLevel.HIGH, AlertType.OPPORTUNITY, risk_score=10.0,
        opportunity_score=85.0))
    assert sent_opp.status == "sent" and sent_calls
    # 收件人固定为需求邮箱
    assert sent_calls[-1][1] == ["2693888583@qq.com"]


@pytest.mark.asyncio
async def test_email_gate_thresholds_configurable(monkeypatch):
    """门槛可经配置调整：风险>79、机会≥90。"""
    attempted: list = []

    class _SMTP:
        def __init__(self, *args, **kwargs):
            attempted.append(args)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, *a):
            pass

        def sendmail(self, *a):
            attempted.append(a)

    monkeypatch.setattr(
        "src.infrastructure.notifiers.email_notifier.smtplib.SMTP_SSL", _SMTP)
    notifier = EmailNotifier(_settings(
        alert_smtp_user="sender@qq.com", alert_smtp_auth_code="code",
        alert_email_to="2693888583@qq.com",
        alert_email_risk_min_score=79.0, alert_email_opp_min_score=90.0))
    assert (await notifier.send(_alert(risk_score=75.0))).status == "suppressed"
    assert (await notifier.send(_alert(risk_score=80.0))).status == "sent"
    assert (await notifier.send(_alert(
        atype=AlertType.OPPORTUNITY, risk_score=5.0,
        opportunity_score=85.0))).status == "suppressed"
    assert (await notifier.send(_alert(
        atype=AlertType.OPPORTUNITY, risk_score=5.0,
        opportunity_score=90.0))).status == "sent"


@pytest.mark.asyncio
async def test_email_failure_does_not_raise(monkeypatch):
    class _BrokenSMTP:
        def __init__(self, *args, **kwargs):
            raise OSError("连接被拒绝")

    monkeypatch.setattr(
        "src.infrastructure.notifiers.email_notifier.smtplib.SMTP_SSL",
        _BrokenSMTP)
    notifier = EmailNotifier(_settings(
        alert_smtp_user="sender@qq.com", alert_smtp_auth_code="code",
        alert_email_to="2693888583@qq.com"))
    result = await notifier.send(_alert())
    assert result.status == "failed" and "连接被拒绝" in result.detail


@pytest.mark.asyncio
async def test_hub_broadcast_isolated_per_tenant():
    """D4：A租户连接收不到B租户广播。"""
    hub = AlertHub()
    ws_a, ws_b = _FakeWebSocket(), _FakeWebSocket()
    await hub.connect(ws_a, tenant_id="tenant_a")
    await hub.connect(ws_b, tenant_id="tenant_b")
    sent = await hub.broadcast(_alert(), tenant_id="tenant_a")
    assert sent == 1
    assert len(ws_a.sent) == 1 and ws_b.sent == []
    sent = await hub.broadcast(_alert(), tenant_id="tenant_b")
    assert sent == 1 and len(ws_b.sent) == 1
    assert hub.client_count("tenant_a") == 1
    assert hub.client_count("tenant_missing") == 0


def test_email_body_has_disclaimer_and_html_escape():
    """TR-7.1/D12：邮件正文必带免责声明，外部字段HTML转义防注入。"""
    notifier = EmailNotifier(_settings(
        alert_smtp_user="sender@qq.com", alert_smtp_auth_code="code",
        alert_email_to="2693888583@qq.com"))
    alert = _alert()
    alert.title = '<script>alert("x")</script>固态电池政策'
    text = notifier._render_text(alert)
    html_body = notifier._render_html(alert)
    assert "不构成投资建议" in text and "不构成投资建议" in html_body
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "javascript:" not in html_body
