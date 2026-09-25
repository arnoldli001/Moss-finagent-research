"""事件告警API集成测试（T10/FR-10/FR-12，ASGI内存传输，不联网）。"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.alert_hub import AlertHub
from src.api.routes.alerts import router as alerts_router
from src.core.config import get_settings
from src.domain.alerts.models import EventAssessment
from src.domain.alerts.service import AlertScanService
from src.domain.alerts.thresholds import AlertEngine
from src.infrastructure.repositories.event_sqlite_repo import (
    EventSqliteRepository,
)


class FakeCollector:
    source_name = "fake_collector"

    def __init__(self, items):
        self.items = items

    async def collect(self):
        return self.items, []


class FakeAnalyzer:
    async def analyze(self, events, force: bool = False):
        out = []
        for e in events:
            if "重大" in e.title:
                out.append(EventAssessment(
                    event_id=e.event_id, event_type=e.event_type,
                    sentiment="positive", risk_score=12,
                    opportunity_score=90, confidence=0.91,
                    affected_industries=["固态电池"], impact_path="政策→需求→业绩",
                    summary="重大产业利好"))
            else:
                out.append(EventAssessment(
                    event_id=e.event_id, event_type=e.event_type,
                    risk_score=20, opportunity_score=30, confidence=0.8))
        return out


class FakeEmailer:
    def is_configured(self):
        return False

    async def send(self, alert):
        from src.domain.alerts.models import EmailSendResult
        return EmailSendResult(
            alert_id=alert.alert_id, status="unconfigured",
            detail="未配置SMTP账号或授权码")


def _raw(title, **over):
    item = {"title": title, "content": "事件正文内容",
            "source_name": "测试快讯", "source_url": "https://x/1",
            "publish_time": "2026-09-14 08:00:00"}
    item.update(over)
    return item


@pytest.fixture
def client(tmp_path):
    repo = EventSqliteRepository(os.path.join(str(tmp_path), "evt.db"))
    items = [
        _raw("国务院发布固态电池产业重大扶持政策"),
        _raw("行业日常动态：市场成交平稳"),
    ]
    service = AlertScanService(
        collectors=[FakeCollector(items)], repo=repo,
        analyzer=FakeAnalyzer(), engine=AlertEngine(get_settings()),
        hub=AlertHub(), emailer=FakeEmailer(), settings=get_settings())
    # service与路由共用同一AlertHub（AC-8：实时推送可端到端断言）
    runtime = SimpleNamespace(
        event_repo=repo, event_service=service,
        alert_hub=service._hub, email_notifier=FakeEmailer())
    app = FastAPI()
    app.include_router(alerts_router)
    app.state.runtime = runtime
    # 仓储建表（独立事件循环写DDL，SQLite文件schema对TestClient循环同样生效）
    import asyncio
    asyncio.run(repo.ensure_schema())
    with TestClient(app) as c:
        yield c, repo, service, runtime


def _wait_scan(c, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get("/api/v1/alerts/scan/latest")
        body = r.json()
        if not body["running"] and body["result"]:
            return body["result"]
        time.sleep(0.1)
    raise AssertionError("扫描后台任务超时未完成")


def test_settings_endpoint_reports_thresholds_and_email(client):
    c, *_ = client
    data = c.get("/api/v1/alerts/settings").json()
    assert data["available"] is True
    assert data["thresholds"]["risk"]["high"] == 75
    assert data["email"]["configured"] is False
    assert data["email"]["to"] == "2693888583@qq.com"
    assert data["email"]["risk_min_score"] == 69.0
    assert data["email"]["opp_min_score"] == 85.0
    assert data["schedule"]["cron"] == "30 17 * * 1-5"
    assert "不构成投资建议" in data["disclaimer"]


def test_manual_import_validation(client):
    c, *_ = client
    ok = c.post("/api/v1/events/import", json={"events": [
        {"title": "手工政策事件", "content": "正文", "event_type": "policy"}]})
    assert ok.status_code == 200 and ok.json()["inserted"] == 1
    bad = c.post("/api/v1/events/import", json={"events": [
        {"title": "", "event_type": "bomb"}]})
    assert bad.status_code == 422


def test_full_scan_creates_alert_and_ws_snapshot(client):
    c, repo, service, runtime = client

    r = c.post("/api/v1/alerts/scan")
    assert r.status_code == 202 and r.json()["status"] == "accepted"
    result = _wait_scan(c)
    assert result["status"] == "success"
    assert result["scanned"] == 2 and result["new_events"] == 2
    assert result["alerts_created"] == 1
    assert result["by_level"] == {"high": 1}
    assert result["email_results"][0]["status"] == "unconfigured"

    alerts = c.get("/api/v1/alerts").json()
    assert alerts["total"] == 1 and alerts["unread"] == 1
    alert = alerts["alerts"][0]
    assert alert["alert_type"] == "opportunity"
    # 来源脱敏：用户响应里**不存在** source_url / source_name，
    # 只有稳定假名 source_alias；管理员视图才保留真名与链接。
    # （这个测试客户端未登录，走的是非管理员路径）
    assert "source_url" not in alert and "source_name" not in alert
    assert alert["source_alias"]
    assert c.get("/api/v1/alerts/unread-count").json()["unread"] == 1
    assert c.get("/api/v1/alerts", params={"level": "high"}).json()["total"] == 1
    assert c.get("/api/v1/alerts", params={"level": "low"}).json()["total"] == 0

    # WebSocket：连上即收到未读快照
    with c.websocket_connect("/api/v1/ws/alerts?tenant_id=tenant_001") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "snapshot" and msg["unread"] == 1
    assert msg["data"][0]["alert_id"] == alert["alert_id"]

    # 已读流
    assert c.post(f"/api/v1/alerts/{alert['alert_id']}/read").status_code == 200
    assert c.get("/api/v1/alerts/unread-count").json()["unread"] == 0
    # ⚠️ 这个测试客户端**未登录**，所以走的是**旧的全局行为**
    # （_viewer_user_id() 拿不到身份 → 退回按行的 status 字段），
    # 因此重复标记仍是 404。
    #
    # 按用户隔离那条路径需要真实会话，在**仓储层**测更干净：
    # 见 	ests/unit/test_alert_user_read.py —— 那里直接拿两个 user_id
    # 验证"A 标记不影响 B"，不需要把认证栈塞进这个 fixture。
    assert c.post(f"/api/v1/alerts/{alert['alert_id']}/read").status_code == 404


def test_ws_receives_alert_pushed_during_scan(client):
    """AC-8：WS连接保持期间触发扫描，实时收到新告警全字段帧。"""
    c, *_ = client
    with c.websocket_connect("/api/v1/ws/alerts?tenant_id=tenant_001") as ws:
        snapshot = ws.receive_json()  # 初始快照：无未读
        assert snapshot["type"] == "snapshot" and snapshot["unread"] == 0

        assert c.post("/api/v1/alerts/scan").status_code == 202
        pushed = ws.receive_json()

    assert pushed["type"] == "alert"
    data = pushed["data"]
    assert data["alert_type"] == "opportunity"
    assert data["alert_level"] == "high"
    assert data["opportunity_score"] == 90
    assert data["confidence"] == 0.91
    # 来源脱敏（这个客户端未登录 → 非管理员路径）：推送帧里同样不能有真名
    assert "source_name" not in data and "source_url" not in data
    assert data["source_alias"]
    assert "不构成投资建议" in data["disclaimer"]
    # 前端铃铛最终与REST一致
    assert c.get("/api/v1/alerts/unread-count").json()["unread"] == 1


def test_repeated_scan_no_new_events(client):
    c, *_ = client
    c.post("/api/v1/alerts/scan")
    _wait_scan(c)
    c.post("/api/v1/alerts/scan")
    second = _wait_scan(c)
    assert second["new_events"] == 0 and second["alerts_created"] == 0


def test_alert_detail_404(client):
    c, *_ = client
    assert c.get("/api/v1/alerts/al_missing").status_code == 404


def test_degraded_stack_returns_503():
    app = FastAPI()
    app.include_router(alerts_router)
    app.state.runtime = SimpleNamespace(
        event_repo=None, event_service=None, alert_hub=None,
        email_notifier=None)
    with TestClient(app) as c:
        assert c.get("/api/v1/alerts").status_code == 503
        settings = c.get("/api/v1/alerts/settings").json()
        assert settings["available"] is False
