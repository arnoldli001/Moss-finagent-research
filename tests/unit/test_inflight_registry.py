"""判据：**在飞登记簿**必须能把"谁堵住循环"与"谁只是排队"分开（`CHG-0146`）。

这组判据守的是一个**观测口径**，不是业务功能。它的价值由一次实测定价：

* `/research/{task_id}` 的 `latency_ms` p50 = **8.3 秒**、当天 184 次，
  按耗时排序它是"头号犯人"；
* 读代码发现它只是**内存字典查表**（`TaskStore.get`）—— 它慢是因为
  **研究任务期间前端轮询它最频繁**，于是它总在队列里，把**别人的阻塞**
  记在了自己账上（实测：被堵时几十个路径会同时放行，出现一批一模一样的
  47708/47757/47754… ms）。

⇒ 判据要保证：卡顿**那一刻**我们能拿到"正在飞的是谁"，且这个登记
**零业务影响、零泄漏**（请求结束必须注销，否则登记簿会越涨越大、
把后来所有卡顿都算到早已结束的请求头上）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.routing import APIRouter
from fastapi.testclient import TestClient

from src.core import inflight


@pytest.fixture(autouse=True)
def _clean_registry():
    """每条判据前后清空登记簿（它是**进程级**状态，会跨用例泄漏）。"""
    inflight.reset()
    yield
    inflight.reset()


@pytest.mark.asyncio
async def test_enter_and_leave_are_paired() -> None:
    """登记 → 注销必须成对；请求结束后**不许**留痕。"""
    assert inflight.inflight_count() == 0
    inflight.enter("GET /api/v1/x")
    assert inflight.inflight_count() == 1
    inflight.leave()
    assert inflight.inflight_count() == 0, (
        "注销失败 ⇒ 登记簿只增不减，之后每一次卡顿都会把早已结束的请求算成嫌疑人")


@pytest.mark.asyncio
async def test_snapshot_is_ordered_by_elapsed() -> None:
    """按**已持续时间**降序 —— 卡顿时最该怀疑的就是飞得最久的那个。"""
    async def _long(label: str, sleep_s: float) -> None:
        inflight.enter(label)
        try:
            await asyncio.sleep(sleep_s)
        finally:
            inflight.leave()

    t1 = asyncio.create_task(_long("old", 0.30))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(_long("new", 0.30))
    await asyncio.sleep(0.05)
    snap = inflight.snapshot()
    assert [r["label"] for r in snap[:2]] == ["old", "new"], snap
    assert snap[0]["elapsed_ms"] > snap[1]["elapsed_ms"] >= 0
    await asyncio.gather(t1, t2)
    assert inflight.inflight_count() == 0


def test_describe_is_empty_when_nothing_in_flight() -> None:
    """**"没在飞"是一个结论**（说明卡顿不来自请求路径），不许印成空白或"None"。"""
    assert inflight.describe() == ""


def test_enter_outside_loop_does_not_raise() -> None:
    """没有事件循环（同步上下文/线程里）时登记必须静默跳过，绝不抛异常。

    观测器的故障不许变成接口的故障 —— 这条与本文件其它判据同一纪律。
    """
    inflight.enter("sync-context")
    inflight.leave()
    assert inflight.inflight_count() == 0


def test_router_registration_is_effective_not_just_attached(monkeypatch) -> None:
    """★★ **行为**判据：真发一个请求，登记必须被调用 —— 不看"依赖在不在列表里"。

    为什么必须是行为判据（这条判据的第一版就是**假绿**的活教材）：
    第一版把依赖 `api_router.dependencies.append(...)` 挂上去，并断言
    "它在列表里" —— 判据绿了。而 FastAPI 的 `include_router` 只应用
    **调用时传进来的** `dependencies=`，事后 append 到父 router 的列表
    **不生效**：请求跑完全程、登记簿里**一条都没有**，
    于是卡顿日志永远印「（无在飞请求）」—— 仪器装了个寂寞。

    所以：**判"接线"要看电流，不要看电线在不在。**
    """
    from fastapi.testclient import TestClient

    from src.api.main import app

    calls: list[tuple[str, str | None]] = []
    orig_enter, orig_leave = inflight.enter, inflight.leave

    def _enter(label: str) -> None:
        calls.append(("enter", label))
        orig_enter(label)

    def _leave() -> None:
        calls.append(("leave", None))
        orig_leave()

    monkeypatch.setattr(inflight, "enter", _enter)
    monkeypatch.setattr(inflight, "leave", _leave)
    with TestClient(app) as client:
        assert client.get("/api/v1/health/live").status_code == 200

    kinds = [k for k, _ in calls]
    assert kinds.count("enter") >= 1, (
        "请求跑了，登记却没被调用 ⇒ 依赖没生效（`include_router` 只认调用时传的 "
        "`dependencies=`，事后 append 到 `api_router.dependencies` 无效）")
    assert kinds.count("enter") == kinds.count("leave"), (
        f"登记与注销不成对 ⇒ 登记簿会越涨越大：{calls}")
    assert any("health/live" in (label or "") for _, label in calls), calls


def test_the_dependency_also_works_on_websocket_routes() -> None:
    """★★ 全局依赖必须对 **WebSocket** 也成立（实测：收 `Request` 会让 WS 直接炸）。

    ## 现场（2026-09-30 全量门禁当场抓到）

    登记依赖挂在**汇总 router** 上 ⇒ 它同时作用于 WS 路由
    （`/api/v1/ws/intraday`、`/api/v1/ws/alerts`）。而 WS 的 scope 里**没有**
    `request` ⇒ 依赖解析抛：

        TypeError: _mark_inflight() missing 1 required positional argument: 'request'

    后果不是「WS 少了个登记」，而是 **2 个 WS 判据 + 7 个 health 契约判据一起红**
    （它们共用同一条 app 装配路径）。修法：收 `HTTPConnection`
    （`Request` 与 `WebSocket` 的**共同基类**）。

    ## 为什么用**独立小应用**而不是整站

    整站的 WS 需要 runtime 装配、登录门槛、真实行情源 —— 那条路红/绿取决于太多东西。
    这里只问一件事：**把依赖挂上去，WS 还能不能正常收发**。
    这正是缺陷的形状（依赖解析，不是业务），所以最小复现最准。

    ## ⚠️ 另一个当场踩到的坑：注解必须在**模块级**可解析

    本文件有 `from __future__ import annotations` ⇒ 注解是**字符串**，
    FastAPI 用 `func.__globals__` 去解析它。第一版把 `WebSocket` 等 import 写在
    **测试函数内部**（局部名）⇒ 解析失败 ⇒ FastAPI 把 `websocket` 当成
    **查询参数**（症状是 `WebSocketDisconnect(1008)`，reason 里写着
    `loc=['query','websocket'] Field required`）—— 与"依赖挂错"长得**完全不一样**。
    所以这些 import 必须放**模块顶层**（见文件头）。
    """
    from src.api.routes import _mark_inflight

    app = FastAPI()
    router = APIRouter(dependencies=[Depends(_mark_inflight)])

    @router.websocket("/ws")
    async def _ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    app.include_router(router)
    inflight.reset()
    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_text() == "ok", (
            "WS 连接被全局依赖打挂了 —— 依赖必须收 `HTTPConnection` 而不是 `Request`")
    assert inflight.inflight_count() == 0, "WS 请求结束后没有注销"


@pytest.mark.asyncio
async def test_loop_lag_spike_names_the_inflight_handler(monkeypatch) -> None:
    """★ 行为判据：卡顿落盘的理由里**必须点名**在飞的处理器。

    只断言"登记簿有 API"是不够的 —— 本仪器的价值全在**loop_lag 那一刻去读它**，
    而那一步写错（比如读成了周期结束时的快照）时，判据仍是绿的、日志却是空的。

    ⚠️ 这条**必须是 async**：`inflight.enter()` 在没有事件循环时静默跳过
    （见 `test_enter_outside_loop_does_not_raise`），写成同步用例会得到
    「（无在飞请求）」的假红 —— 第一版就是这么红的，它顺带证明了
    "无循环时登记确实不生效"。
    """
    from src.core import loop_lag

    inflight.enter("GET /api/v1/quant/data-status")
    assert inflight.inflight_count() == 1, "登记没生效（本用例必须在事件循环里跑）"
    s = loop_lag.stats()
    s.samples, s.max_ms, s.over_warn, s.probe_n = 3, 1234.0, 1, 2
    s.spikes.append(("16:00:00", 1234.0))
    recorded: list[tuple[str, str, str]] = []

    def _capture(kind: str, indicator: str, reason: str) -> None:
        recorded.append((kind, indicator, reason))

    monkeypatch.setattr(loop_lag, "_record", _capture)
    loop_lag.flush(force=True)
    assert recorded, "撞阈值却没有落异常"
    _kind, _ind, reason = recorded[0]
    assert "data-status" in reason, (
        f"卡顿理由里没有点名在飞的处理器 ⇒ 仪器等于没装：{reason!r}")


# ======================================================================
# `interactive()`：**给后台任务让路**用的判据（`CHG-0221`）
#
# 背景：2026-10-08 用户报「自选股的分时图加载很慢」。量出来是日K预热占掉
# API 进程 70% 的墙钟，用户点票的 `/intraday/snapshot` p50 = 3.67 s。
# 修法是让预热**看见用户在等就让路** —— 而"用户在等"这个判据一旦写错，
# 后果是**静默**的：要么让路变成永不生效，要么变成把预热整个关掉。
# ======================================================================

@pytest.mark.asyncio
async def test_interactive_counts_http_requests() -> None:
    """HTTP 请求/响应周期算"用户在等"。"""
    inflight.enter("GET /api/v1/intraday/snapshot")
    rows = inflight.interactive()
    assert [r["label"] for r in rows] == ["GET /api/v1/intraday/snapshot"], rows


@pytest.mark.asyncio
async def test_interactive_excludes_websockets_and_tasks() -> None:
    """★★ **长连接与后台任务都不算"用户在等"** —— 这条是本判据的全部价值。

    实测过的两个反例（都来自生产日志）：

        websocket /api/v1/ws/alerts(2272813ms)   ← 一条挂过 **38 分钟**
        task:catalog-rebuild(30313ms)            ← 后台任务自己

    拿"登记簿非空"当判据，`websocket` 会让预热**永远**让路（等于把预热关掉，
    而界面上看不出任何异常，只是每只票都变冷）；`task:` 则让后台给后台让路。
    """
    inflight.enter("websocket /api/v1/ws/alerts")
    inflight.enter("task:catalog-rebuild")
    inflight.enter("task:intel-feed-prewarm")
    assert inflight.interactive() == [], (
        "长连接/后台任务被算成了交互请求 ⇒ 预热会永远让路（静默退化）")

    # 混进来一个真请求时，只有它被算进去
    inflight.enter("POST /api/v1/research/analyze")
    assert [r["label"] for r in inflight.interactive()] == [
        "POST /api/v1/research/analyze"]


@pytest.mark.asyncio
async def test_interactive_is_ordered_like_snapshot() -> None:
    """排序与 `snapshot()` 同口径（已持续最久的在前），便于日志直接读。"""
    async def _hold(label: str, sleep_s: float) -> None:
        inflight.enter(label)
        try:
            await asyncio.sleep(sleep_s)
        finally:
            inflight.leave()

    t1 = asyncio.create_task(_hold("GET /api/v1/older", 0.30))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(_hold("GET /api/v1/newer", 0.30))
    await asyncio.sleep(0.05)
    assert [r["label"] for r in inflight.interactive()] == [
        "GET /api/v1/older", "GET /api/v1/newer"]
    await asyncio.gather(t1, t2)


def test_interactive_outside_loop_is_empty_not_error() -> None:
    """没有事件循环时返回空表（不是异常）—— 后台线程里也要能安全问一句。"""
    assert inflight.interactive() == []
