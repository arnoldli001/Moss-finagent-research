"""事件告警**列表缓存与载荷瘦身**的回归测试（2026-09-26 用户报障）。

## 那次报障与量出来的账

> "事件告警 首次打开也要加载 2-3 秒才出数据，切网站内界面再切回也要
>   2 秒显示……数据入库，首次取库，减少冷启动，用户登录成功就预加载进来。"

| 环节 | 实测 |
|---|---|
| 后端本机 `/alerts?limit=100` | **28~74 ms**，112,520 B |
| 同一条走公网域名 | **5,705 ms**（第二次直接读超时） |
| 隧道带宽 | **≈ 51 KB/s**（834 KB 静态包 16.3 秒） |
| `fact_alerts` 行数 | **95 行**，索引齐全 |

结论：**瓶颈既不是数据库也不是后端，而是把 112 KB 挪过隧道**。
所以这里钉住的是两条"少传/少往返"的契约，以及它们的**正确性代价**：

1. 列表结果按 (租户,用户,管理员,筛选) 缓存 —— 但**任何改内容的写操作
   必须立刻失效**，否则用户点完已读再切回来会看到旧状态（看起来像"点了没用"）。
2. 列表投影砍掉没人读的字段（每条重复的 disclaimer + 内部键）。
3. `/alerts/bootstrap` 必须真的能路由到（**不能被 `/alerts/{alert_id}` 吞掉**
   —— 那是"看起来像告警不存在"的一类假故障）。
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.alert_hub import AlertHub
from src.api.routes import alerts as alerts_route
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
                    affected_industries=["固态电池"],
                    impact_path="政策→需求→业绩", summary="重大产业利好"))
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

        return EmailSendResult(alert_id=alert.alert_id, status="skipped",
                               detail="未配置SMTP账号或授权码")


def _raw(title, **over):
    item = {"title": title, "content": "事件正文内容",
            "source_name": "测试快讯", "source_url": "https://x/1",
            "publish_time": "2026-09-14 08:00:00"}
    item.update(over)
    return item


@pytest.fixture(autouse=True)
def _clear_alerts_cache():
    """每个用例前后都清空列表缓存。

    它是**进程级**的（`_alerts_cache`），不清就会串用例 ——
    表现是"第二个用例拿不到自己刚插的告警"，看起来像排序/筛选坏了。
    """
    alerts_route._invalidate_alerts()          # noqa: SLF001
    yield
    alerts_route._invalidate_alerts()          # noqa: SLF001


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
    runtime = SimpleNamespace(
        event_repo=repo, event_service=service,
        alert_hub=service._hub, email_notifier=FakeEmailer())
    app = FastAPI()
    app.include_router(alerts_router)
    app.state.runtime = runtime
    asyncio.run(repo.ensure_schema())
    with TestClient(app) as c:
        yield c, repo, service, runtime


def _scan_and_wait(c, timeout=10.0):
    c.post("/api/v1/alerts/scan")
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = c.get("/api/v1/alerts/scan/latest").json()
        if not body["running"] and body["result"]:
            return body["result"]
        time.sleep(0.1)
    raise AssertionError("扫描后台任务超时未完成")


# ======================================================================
# 载荷瘦身
# ======================================================================

def test_list_omits_per_alert_disclaimer(client) -> None:
    """★ 每条重复 91 字节的 disclaimer 必须不再下发。

    95 条里它占 8,645 B（7.7%），而在 ~51 KB/s 的隧道上，
    重复 95 遍的同一句话要从用户等待时间里扣掉好几秒。
    """
    c, _repo, _svc, _rt = client
    _scan_and_wait(c)
    alerts = c.get("/api/v1/alerts?limit=100").json()["alerts"]
    assert alerts, "扫描后应至少有一条告警"
    assert all("disclaimer" not in a for a in alerts), (
        "列表里不该再有逐条 disclaimer（改由 settings.disclaimer 下发一次）")


def test_list_omits_internal_only_keys(client) -> None:
    """内部去重键与租户标识不进列表响应（前端零引用，纯占带宽）。"""
    c, _repo, _svc, _rt = client
    _scan_and_wait(c)
    alerts = c.get("/api/v1/alerts?limit=100").json()["alerts"]
    for a in alerts:
        for key in ("alert_key", "content_key", "tenant_id"):
            assert key not in a, f"内部字段 {key} 不该出现在列表响应里"


def test_list_keeps_fields_the_detail_drawer_needs(client) -> None:
    """★ 反向保护：**不能**顺手把 description / affected_stocks 也裁掉。

    `AlertsPanel.selectAlert` 是直接拿列表对象打开详情抽屉、**不重新请求**的。
    裁掉它们确实能再省 ~35 KB，代价却是"点一条先转两秒"。
    """
    c, _repo, _svc, _rt = client
    _scan_and_wait(c)
    alerts = c.get("/api/v1/alerts?limit=100").json()["alerts"]
    assert alerts
    for a in alerts:
        assert "description" in a
        assert "affected_stocks" in a
        assert "title" in a and "alert_id" in a


def test_settings_still_carries_the_disclaimer(client) -> None:
    """免责声明改成**响应级下发一次** —— 这条保证它没有整条链路消失。"""
    c, _repo, _svc, _rt = client
    body = c.get("/api/v1/alerts/settings").json()
    assert body.get("disclaimer"), "settings 必须仍然带免责声明文案"


# ======================================================================
# 响应缓存
# ======================================================================

def test_second_list_request_is_served_from_cache(client) -> None:
    """同一档第二次请求命中缓存（不再走仓库）。

    断言方式：拿到第一份后，**绕过端点直接改库**（把一条告警标成已读），
    再请求一次 —— 若仍然返回 `active`，说明它来自缓存而不是数据库。

    这样写同时钉住了两件事：
      · 缓存确实生效；
      · 失效只发生在**端点层**（`_invalidate_alerts`），直接改库不会
        "隔空"清掉缓存 —— 否则这条断言会假通过。
    """
    c, repo, _svc, _rt = client
    _scan_and_wait(c)
    first = c.get("/api/v1/alerts?limit=100").json()
    target = next(a for a in first["alerts"] if a["status"] == "active")

    # 直接改库（**不走** `/alerts/{id}/read`，所以不触发缓存失效）
    asyncio.run(repo.mark_read(target["alert_id"], "tenant_001", user_id=""))
    again = c.get("/api/v1/alerts?limit=100").json()
    got = next(a for a in again["alerts"] if a["alert_id"] == target["alert_id"])
    assert got["status"] == "active", (
        "第二次应命中缓存（库里已改成 read，但缓存里还是 active）")


def test_mark_read_invalidates_cache(client) -> None:
    """★ 点已读**必须立刻**失效缓存。

    不失效的话，用户点完已读再切回来会看到旧状态 —— 最坏的表现是
    "点了没用"，而那是用户会直接报障的那种 bug。
    """
    c, _repo, _svc, _rt = client
    _scan_and_wait(c)
    alerts = c.get("/api/v1/alerts?limit=100").json()["alerts"]
    target = next(a for a in alerts if a["status"] == "active")

    assert c.post(f"/api/v1/alerts/{target['alert_id']}/read").status_code == 200
    fresh = c.get("/api/v1/alerts?limit=100").json()["alerts"]
    got = next(a for a in fresh if a["alert_id"] == target["alert_id"])
    assert got["status"] == "read", "标记已读后列表必须反映新状态（缓存已失效）"


def test_mark_all_read_invalidates_cache(client) -> None:
    """全部已读同样要失效。"""
    c, _repo, _svc, _rt = client
    _scan_and_wait(c)
    before = c.get("/api/v1/alerts?limit=100").json()
    assert any(a["status"] == "active" for a in before["alerts"])

    c.post("/api/v1/alerts/read-all")
    after = c.get("/api/v1/alerts?limit=100").json()
    assert all(a["status"] != "active" for a in after["alerts"]), (
        "全部已读后不该还有 active（缓存已失效）")
    assert after["unread"] == 0


def test_cache_key_separates_filters(client) -> None:
    """★ 不同筛选档**不能**互相复用缓存。

    复用错的表现极隐蔽：切到"仅未读"却看到全部 —— 看着完全正常，
    只是筛选没用。
    """
    c, _repo, _svc, _rt = client
    _scan_and_wait(c)
    all_alerts = c.get("/api/v1/alerts?limit=100").json()["alerts"]
    active_only = c.get("/api/v1/alerts?limit=100&status=active").json()["alerts"]
    assert all(a["status"] == "active" for a in active_only)
    assert len(active_only) <= len(all_alerts)


def test_cache_key_separates_admin_from_user(client) -> None:
    """★ 管理员与普通用户**不是同一份内容**，绝不能共用缓存。

    管理员响应保留 `source_name`/`source_url`（排障用），普通用户没有。
    共用键会把管理员的响应发给普通用户 —— 那是**数据源泄漏**，
    不只是显示问题。
    """
    key_admin = alerts_route._alerts_cache_key(          # noqa: SLF001
        tenant_id="tenant_001", user_id="u1", alert_type=None,
        alert_level=None, status=None, limit=100,
        include_expired=False, is_admin=True)
    key_user = alerts_route._alerts_cache_key(           # noqa: SLF001
        tenant_id="tenant_001", user_id="u1", alert_type=None,
        alert_level=None, status=None, limit=100,
        include_expired=False, is_admin=False)
    assert key_admin != key_user


def test_cache_key_separates_users(client) -> None:
    """同一租户下不同用户不能共用缓存（未读状态是按人的）。"""
    a = alerts_route._alerts_cache_key(                  # noqa: SLF001
        tenant_id="tenant_001", user_id="u1", alert_type=None,
        alert_level=None, status=None, limit=100,
        include_expired=False, is_admin=False)
    b = alerts_route._alerts_cache_key(                  # noqa: SLF001
        tenant_id="tenant_001", user_id="u2", alert_type=None,
        alert_level=None, status=None, limit=100,
        include_expired=False, is_admin=False)
    assert a != b


# ======================================================================
# /alerts/bootstrap
# ======================================================================

def test_bootstrap_is_routable_and_returns_both_parts(client) -> None:
    """★ `/alerts/bootstrap` 必须真能路由到。

    它注册在 `/alerts/{alert_id}` **之前**是刻意的：FastAPI 按注册顺序匹配，
    否则 "bootstrap" 会被当成一个告警 ID 吞掉，端点永远 404 ——
    而那个症状看起来像"这条告警不存在"，与路由顺序完全联系不起来。
    """
    c, _repo, _svc, _rt = client
    _scan_and_wait(c)
    resp = c.get("/api/v1/alerts/bootstrap?limit=100")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("alerts"), "应带上列表"
    assert body.get("settings", {}).get("available") is not None, "应带上设置"
    assert "disclaimer" in body["settings"]
    assert isinstance(body.get("unread"), int)
