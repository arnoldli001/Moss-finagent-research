"""情报流 `/feed` 的**非阻塞**契约（用户口径 2026-10-01 第七轮）。

用户原话：

> "前端刷新触发后端任务，但是不应该等待后端输出才显示，后端采集信息和输出
>   需要时间的，容易触发等待时间，可以做监听回调，有信息了再触发自动刷新前端。"

## 这一版修的是什么（与"给缓存加个 TTL"的区别）

上一版加了 60/300 秒 TTL，但请求仍然 `await build_feed(...)` —— 实测冷
**2.18s** / 热 **1.01s**（并发拉六个源 + 知识星球增量）。所以
**第一次打开、以及每一次过期**仍然要盯着空屏看 1~2 秒。用户不接受的就是这一段。

现在的契约是"**请求永不等待采集**"：

    GET /feed ──► 立刻返回进程内缓存（没有就给一个形状正确的空壳）
                  └─► 若缺失/过期/显式 refresh → 起后台任务（**单飞**）
                      后台建完 → 落缓存 → WS 推 `intel_feed` → 前端自动补拉

## 用例的分工

本文件只测**新增的那一层**（缓存 / 单飞 / 通知 / 空壳形状），所以把
`build_feed` 与 `_build_heat` 换成可控的假实现 —— 真实的聚合逻辑由
`test_intel_label_and_fulltext.py` 等既有用例覆盖。两边的关注点不重叠，
混在一起写会让"缓存坏了"与"采集坏了"变成同一个红灯。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.alert_hub import AlertHub
from src.api.routes import intel as INTEL_ROUTE
from src.domain.intel.service import IntelFeed

#: 空壳（冷启动）与真实 payload 的键**必须逐键相同**。
#:
#: ⚠️ 这是本文件最重要的一条断言：少一个键不会有任何报错，只会让前端某块
#: 渲染不出来 —— 而"冷启动"是每个新进程第一次打开页面时**必然**走到的路径。
_FEED_KEYS = {
    "items", "gaps", "counts", "fetched_at", "degraded",
    "credibility_dist", "cluster_stats", "tone_dist", "filter_stats",
    "admin_hints", "filter", "sort", "filters", "heat",
    "cached", "refreshing", "built_at", "age_seconds", "seq",
    # `limit` 在真实 payload 里（启动热加载要靠它重算缓存键，
    # 见 `_feed_public_payload`），空壳也必须给 —— 否则"第一次请求"与
    # "第二次请求"的键集合不一致，而那正是本文件开头那条纪律要防的事。
    "limit",
    # 多/空筛选（用户口径 2026-09-26）。空壳也必须有这三个键 ——
    # 少了它们，前端在冷启动那一次读 `direction_dist.bull` 会炸在渲染期。
    "direction", "direction_dist", "direction_hidden",
}


class _RecordingSocket:
    """假的 WebSocket 连接（与 `test_notifiers._FakeWebSocket` 同口径）。

    用它配**真的** `AlertHub`：这样"注册 → 遍历 → 序列化 → 发送"整条链
    都真的跑到了，只有最外层的 socket 是假的。
    """

    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _reset_feed_cache():
    """每个用例都从"全新进程"开始（缓存 / 单飞标记 / 序号全部清空）。"""
    with INTEL_ROUTE._FEED_LOCK:
        INTEL_ROUTE._FEED_CACHE.clear()
        INTEL_ROUTE._FEED_BUILDING.clear()
        INTEL_ROUTE._FEED_TASKS.clear()
        INTEL_ROUTE._FEED_SEQ = 0
    yield
    with INTEL_ROUTE._FEED_LOCK:
        INTEL_ROUTE._FEED_CACHE.clear()
        INTEL_ROUTE._FEED_BUILDING.clear()
        INTEL_ROUTE._FEED_SEQ = 0


def _patch_feature_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_require_feature(request: object, feature: str):
        return ("u-test", "pro")

    monkeypatch.setattr(INTEL_ROUTE, "require_feature", _fake_require_feature)


def _patch_build(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0,
                 calls: list[dict] | None = None) -> None:
    """把真实的六源聚合换成可控的假实现（见模块 docstring）。"""
    seen = calls if calls is not None else []

    async def _fake_build(**kw):
        seen.append(kw)
        if delay:
            import asyncio
            await asyncio.sleep(delay)
        return IntelFeed(
            items=[{"title": "假条目", "content_hash": "h-fake",
                    "kind": "newswire", "summary": "假摘要"}],
            counts={"newswire": 1},
            fetched_at="2026-10-01T22:00:00+08:00",
            credibility_dist={"high": 0},
        )

    async def _fake_heat(feed, *, limit):
        return {"at": "", "stocks": [], "topics": [], "rank": [], "rank_at": "",
                "stocks_scanned": 0, "gaps": [], "note": "假热议"}

    monkeypatch.setattr(INTEL_ROUTE, "build_feed", _fake_build)
    monkeypatch.setattr(INTEL_ROUTE, "_build_heat", _fake_heat)


def _client(hub: AlertHub | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(INTEL_ROUTE.router)
    # `_feed_hub` 走的是 `request.app.state.runtime.alert_hub`（与真实装配一致）
    app.state.runtime = SimpleNamespace(alert_hub=hub)
    return TestClient(app)


def _wait_idle(timeout: float = 5.0) -> bool:
    """等后台重建结束（返回是否在超时前等到）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not INTEL_ROUTE._feed_status()["building"]:
            return True
        time.sleep(0.02)
    return False


# ======================================================================
# ① 请求永不阻塞
# ======================================================================

def test_cold_request_returns_immediately_with_a_shaped_placeholder(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 冷启动：**立刻**返回空壳，不等待采集。

    这是用户报障的核心："不应该等待后端输出才显示"。所以断言两件事：
      ① 耗时必须是**毫秒级**（后台那次假的 1.5 秒采集绝不能被等掉）；
      ② 空壳的键与真实 payload **逐键相同**（前端不必写第二套渲染分支）。
    """
    _patch_feature_gate(monkeypatch)
    _patch_build(monkeypatch, delay=1.5)

    with _client() as c:
        t0 = time.monotonic()
        r = c.get("/api/v1/intel/feed?limit=60")
        elapsed = time.monotonic() - t0

        assert r.status_code == 200
        assert elapsed < 0.3, f"冷启动等了 {elapsed:.2f}s（必须毫秒级返回）"
        body = r.json()
        assert set(body) == _FEED_KEYS, set(body) ^ _FEED_KEYS
        assert body["items"] == []
        assert body["refreshing"] is True, "前端靠它显示'正在采集…'"
        assert body["cached"] is False
        assert body["seq"] == 0, "还没有过任何一份数据"
        assert body["built_at"] == "" and body["age_seconds"] is None, \
            "空壳不许编一个时间（渲染出来是假信息）"
        # 空壳的 heat 也必须是完整形状（少键只会让前端某块渲染不出来）
        assert set(body["heat"]) == {"at", "stocks", "topics", "rank",
                                     "rank_at", "stocks_scanned", "gaps",
                                     "note"}
        assert _wait_idle(), "后台重建没有被调度起来"


def test_after_background_build_the_next_request_serves_real_data(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 后台建好之后：同一请求拿到真数据、`refreshing=false`、`seq` 前进。"""
    _patch_feature_gate(monkeypatch)
    _patch_build(monkeypatch, delay=0.05)

    with _client() as c:
        first = c.get("/api/v1/intel/feed?limit=60").json()
        assert first["items"] == []
        assert _wait_idle()

        second = c.get("/api/v1/intel/feed?limit=60").json()
        assert set(second) == _FEED_KEYS
        assert len(second["items"]) == 1, "缓存里那一份没被下发"
        assert second["refreshing"] is False
        assert second["cached"] is True
        assert second["seq"] == 1
        assert second["built_at"], "真实那一份必须有生成时刻"
        assert isinstance(second["age_seconds"], (int, float))


def test_warm_request_does_not_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """热命中：既不重建，也**不再等**（`build_feed` 只被调一次）。"""
    _patch_feature_gate(monkeypatch)
    calls: list[dict] = []
    _patch_build(monkeypatch, calls=calls)

    with _client() as c:
        c.get("/api/v1/intel/feed?limit=60")
        assert _wait_idle()
        for _ in range(5):
            body = c.get("/api/v1/intel/feed?limit=60").json()
            assert body["refreshing"] is False
        # 只有最初那一次后台重建
        assert len(calls) == 1, calls


# ======================================================================
# ② 单飞：并发请求不许各起一个采集
# ======================================================================

def test_concurrent_requests_start_only_one_build(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 单飞：同一份缓存键在重建期间**只允许一个**后台任务。

    没有这个守卫时，多个用户（或同一用户多个页签）同时打开页面会各自
    并发拉六个源 —— 本地单用户测不出来，只在试点表现为"上游偶尔超时"。
    """
    _patch_feature_gate(monkeypatch)
    calls: list[dict] = []
    _patch_build(monkeypatch, delay=0.3, calls=calls)

    with _client() as c:
        for _ in range(6):
            assert c.get("/api/v1/intel/feed?limit=60").status_code == 200
        assert INTEL_ROUTE._feed_status()["building"] is True
        assert len(calls) == 1, f"并发的 6 个请求起了 {len(calls)} 次采集"
        assert _wait_idle()
        assert len(calls) == 1


def test_different_cache_keys_build_independently(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """不同参数（filter 是缓存键的一部分）各自重建 —— 单飞**不能**跨键误拦。"""
    _patch_feature_gate(monkeypatch)
    calls: list[dict] = []
    _patch_build(monkeypatch, delay=0.2, calls=calls)

    with _client() as c:
        c.get("/api/v1/intel/feed?limit=60&filter=all")
        c.get("/api/v1/intel/feed?limit=60&filter=high")
        assert _wait_idle()
        assert len(calls) == 2, calls
        assert {kw["filter"] for kw in calls} == {"all", "high"}


# ======================================================================
# ③ 显式刷新：绕开 TTL，但受下限保护
# ======================================================================

def test_explicit_refresh_reuses_a_just_built_entry(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 刚建好的那份，`refresh=true` **不重建**（`FEED_REFRESH_FLOOR`）。

    用户会连点刷新；没有下限时每一次点击都重跑一遍六源采集 —— 那是自己打
    自己的上游。而请求本来就不阻塞，所以这里省的是上游，不是用户的时间。
    """
    _patch_feature_gate(monkeypatch)
    calls: list[dict] = []
    _patch_build(monkeypatch, calls=calls)

    with _client() as c:
        c.get("/api/v1/intel/feed?limit=60")
        assert _wait_idle()
        body = c.get("/api/v1/intel/feed?limit=60&refresh=true").json()
        assert body["seq"] == 1, "刚建好就重建了"
        assert body["refreshing"] is False
        assert len(calls) == 1, calls


def test_explicit_refresh_rebuilds_an_aged_entry(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """过期的缓存 + `refresh=true` → 起新的后台重建（序号前进）。"""
    _patch_feature_gate(monkeypatch)
    _patch_build(monkeypatch)

    with _client() as c:
        c.get("/api/v1/intel/feed?limit=60")
        assert _wait_idle()
        # 把这份人为"变老"到下限之外（不改 TTL，只改写入时刻）
        with INTEL_ROUTE._FEED_LOCK:
            key = next(iter(INTEL_ROUTE._FEED_CACHE))
            entry = INTEL_ROUTE._FEED_CACHE[key]
            entry.at -= INTEL_ROUTE.FEED_REFRESH_FLOOR + 1.0

        body = c.get("/api/v1/intel/feed?limit=60&refresh=true").json()
        assert body["refreshing"] is True, "显式刷新没有触发重建"
        assert _wait_idle()
        assert c.get("/api/v1/intel/feed?limit=60").json()["seq"] == 2


# ======================================================================
# ④ 就绪通知：复用既有告警 WebSocket 通道
# ======================================================================

def test_build_completion_pushes_on_the_existing_alert_hub(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 建完后通过**既有**的 `AlertHub` 推 `intel_feed` 通知。

    用户要的就是这一步："有信息了再触发自动刷新前端"。

    ⚠️ 载荷里**只有序号与时间戳** —— 这是它能跨租户广播的前提
    （`AlertHub.notify` 不做逐连接脱敏）。塞进任何业务字段都会变成
    "把管理员的视图发给所有人"那条老事故。
    """
    _patch_feature_gate(monkeypatch)
    _patch_build(monkeypatch)

    hub = AlertHub()
    sock = _RecordingSocket()

    with _client(hub) as c:
        # 连接登记要在请求之前（真实时序：App 层常驻连接先建好）
        asyncio.run(hub.connect(sock, "tenant_001", user_id="u1"))
        c.get("/api/v1/intel/feed?limit=60")
        assert _wait_idle()

    assert sock.sent, "建完之后没有任何通知，前端只能靠手动刷新"
    msg = sock.sent[0]
    assert msg["type"] == "intel_feed"
    assert set(msg["data"]) == {"seq", "built_at"}, msg
    assert msg["data"]["seq"] == 1


def test_missing_hub_does_not_break_the_build(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 hub（子系统未装配 / 单测无 lifespan）时**照常建缓存**。

    前端有 `/feed/status` 兜底，所以"通知发不出去"不该让数据也建不出来 ——
    少写这条兜底的表现是"试点实例上情报流永远是空的"，而日志里只有一条
    `AttributeError`。
    """
    _patch_feature_gate(monkeypatch)
    _patch_build(monkeypatch)

    with _client(None) as c:
        c.get("/api/v1/intel/feed?limit=60")
        assert _wait_idle()
        assert len(c.get("/api/v1/intel/feed?limit=60").json()["items"]) == 1


# ======================================================================
# ⑤ 廉价状态接口（WS 的兜底）
# ======================================================================

def test_status_endpoint_is_cheap_and_reports_progress(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`/feed/status` 只读进程内计数器：`building` / `seq` / `age_seconds`。"""
    _patch_feature_gate(monkeypatch)
    _patch_build(monkeypatch, delay=0.3)

    with _client() as c:
        c.get("/api/v1/intel/feed?limit=60")
        st = c.get("/api/v1/intel/feed/status").json()
        assert set(st) == {"building", "seq", "built_at", "age_seconds", "ttl"}
        assert st["building"] is True and st["seq"] == 0
        assert st["ttl"] == INTEL_ROUTE.FEED_CACHE_TTL

        assert _wait_idle()
        st = c.get("/api/v1/intel/feed/status").json()
        assert st["building"] is False and st["seq"] == 1
        assert st["built_at"] and st["age_seconds"] is not None


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存条目**有界**：超过 `FEED_CACHE_MAX` 整体清空（防参数枚举撑满内存）。"""
    _patch_feature_gate(monkeypatch)
    _patch_build(monkeypatch)
    monkeypatch.setattr(INTEL_ROUTE, "FEED_CACHE_MAX", 3)

    with _client() as c:
        for i in range(6):
            c.get(f"/api/v1/intel/feed?limit={i + 1}")
            assert _wait_idle()
        with INTEL_ROUTE._FEED_LOCK:
            assert len(INTEL_ROUTE._FEED_CACHE) <= 3, INTEL_ROUTE._FEED_CACHE


# ======================================================================
# ⑥ 响应形状与隐私（回归：新增字段不许破坏既有契约）
# ======================================================================

def test_feed_payload_keeps_hiding_full_text_and_channel_identity(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 情报流 payload 仍然**没有**全文（**顶层与收容组内两处都没有**）。

    这一层改动把 payload 从"当场组装"变成"缓存里那份"，所以必须重新钉一遍：
    缓存里如果混进内部字段，泄漏会**一直存在**（不像即时组装那样只在某些
    参数组合下出现）。

    ⚠️ 收容组里的子条目是 `_group_row` 生成的**另一个 dict**，
    `to_public()` 必须两层都剥 —— 只剥顶层时"实测 21 条研究笔记全在组内"
    就等于没剥。缓存这一层把 `to_public()` 从"每请求一次"变成"建的时候一次"，
    漏剥的后果因此从"偶发"变成"永久"。
    """
    _patch_feature_gate(monkeypatch)
    marker = "清洗后的全文——这一段绝不许出接口"

    async def _fake_build(**_kw):
        return IntelFeed(items=[{
            "title": "标题", "content_hash": "h1", "kind": "research_note",
            "summary": "摘要", "extract_text": marker,
            "market_terms": ["天岳先进"], "source_alias": "research-note-zsxq",
            "group_items": [{"title": "组内", "content_hash": "h2",
                             "summary": "组内摘要", "extract_text": marker,
                             "market_terms": ["第三代半导体"]}],
        }], counts={"research_note": 1})

    async def _fake_heat(feed, *, limit):
        return {"at": "", "stocks": [], "topics": [], "rank": [], "rank_at": "",
                "stocks_scanned": 0, "gaps": [], "note": ""}

    monkeypatch.setattr(INTEL_ROUTE, "build_feed", _fake_build)
    monkeypatch.setattr(INTEL_ROUTE, "_build_heat", _fake_heat)

    with _client() as c:
        c.get("/api/v1/intel/feed?limit=60")
        assert _wait_idle()
        item = c.get("/api/v1/intel/feed?limit=60").json()["items"][0]

    assert "extract_text" not in item, "全文泄漏到情报流 payload（顶层）"
    assert "market_terms" not in item, "内部字段泄漏到情报流 payload（顶层）"
    assert marker not in str(item), "全文内容本身泄漏（顶层）"
    inner = item["group_items"][0]
    assert "extract_text" not in inner, "全文泄漏到收容组子条目（只剥了顶层）"
    assert "market_terms" not in inner, "内部字段泄漏到收容组子条目"
    assert marker not in str(inner), "全文内容泄漏到收容组子条目"


# ======================================================================
# ⑦ 前端契约（源码级守卫，与 `test_intel_label_and_fulltext` 同一手法）
# ======================================================================

#: 仓库根（读前端源码用）
_ROOT = Path(__file__).resolve().parents[2]


def test_refresh_button_sends_the_explicit_refresh_flag() -> None:
    """★★ 手动刷新**必须**带 `refresh=true`，否则它是"假刷新"。

    服务端有 60 秒缓存。不带这个标志时"点刷新"会原样拿回同一份：按钮先显示
    一次"刷新中…"、然后什么都没变 —— 用户会以为"新内容真的还没有"，
    于是不再相信这个按钮。这类缺陷**不会报错**，只能靠这条断言钉住。

    ⚠️ 一条同样重要的反向纪律：**后台自动补拉不许带它**
    （补拉要的正是"吃缓存"）。所以这里同时断言 `IntelPanel` 里
    `refresh: true` 只出现在刷新按钮那一处。
    """
    panel = (_ROOT / "web" / "src" / "components" / "intel"
             / "IntelPanel.tsx").read_text(encoding="utf-8")
    assert "loadAll({ refresh: true })" in panel, \
        "刷新按钮没有走显式刷新路径（会变成假刷新）"
    assert panel.count("refresh: true") == panel.count(
        "loadAll({ refresh: true })"), \
        "后台自动补拉带上了 refresh=true（那会让自动刷新退化成每次重采集）"

    api = (_ROOT / "web" / "src" / "intelApi.ts").read_text(encoding="utf-8")
    assert 'q.set("refresh", "true")' in api, "refresh 参数没有拼进查询串"


def test_frontend_consumes_the_intel_ready_notification() -> None:
    """信息流就绪通知要走**既有**的告警 WebSocket，不是第二条通道。"""
    hook = (_ROOT / "web" / "src" / "hooks" / "useAlertsWs.ts").read_text(
        encoding="utf-8")
    # 复用的是同一个连接（`alertsWsUrl`），且处理了 `intel_feed`
    assert "alertsWsUrl" in hook and '"intel_feed"' in hook
    assert "intelSeq" in hook

    app = (_ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "intelSeq={intelSeq}" in app, "通知没有接到情报面板上"

    api = (_ROOT / "web" / "src" / "intelApi.ts").read_text(encoding="utf-8")
    assert "/feed/status" in api, "缺少 WS 断开时的兜底状态接口"


def test_panel_stops_polling_instead_of_spinning_forever() -> None:
    """兜底轮询必须**有上限**，且超时后如实提示（静默转圈是最坏的表现）。"""
    panel = (_ROOT / "web" / "src" / "components" / "intel"
             / "IntelPanel.tsx").read_text(encoding="utf-8")
    assert "READY_POLL_MAX_TICKS" in panel
    assert "setReadyTimeout(true)" in panel
    # 只在 refreshing 期间轮询：effect 的第一句必须是对它的早退
    assert "if (!refreshing) return;" in panel


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
