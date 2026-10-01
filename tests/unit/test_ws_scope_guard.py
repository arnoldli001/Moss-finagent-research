"""判据：**非 HTTP 作用域不许打到静态挂载上**（`CHG-0149`）。

## 现场（2026-09-30 公网链路体检当场复现）

SPA 的静态资源挂在 `/`（`Mount("/")` 匹配**任何**路径），而
`starlette/staticfiles.py:91` 第一行就是 `assert scope["type"] == "http"`。
于是任何落到这里的 **WebSocket 作用域**都会变成：

    AssertionError → uvicorn 记 `connection rejected (500 Internal Server Error)`
    → 客户端看到 **500**

两个触发面都是**正常误用**，不需要恶意：

1. 把 `/api/v1/health/live` 这类 **HTTP-only 路径**当 WS 地址连；
2. 连一个**不存在的** WS 路径（路径写错、老客户端残留）。

## 正确行为（按 ASGI 规范）

* 服务端支持「拒绝响应」扩展（uvicorn 支持）⇒ 回真正的 **404**（可读、可排障）；
* 不支持 ⇒ accept **之前** `websocket.close(1008)`（uvicorn 渲染成 403 握手拒绝）。

★ 注意：**登录门槛拦下的 403 与这里的 404/1008 是两件事** ——
前者是"这个 WS 路径存在、但你没登录"，后者是"这里根本没有 WS 路由"。
判据必须能区分，否则把 WS 路径真删了也照样绿。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

#: 三种"没有 WS 路由"的路径：不存在的路径 / HTTP-only 路径 / 另一个 HTTP-only 路径
NON_WS_PATHS = ("/definitely-not-a-ws", "/api/v1/health/live", "/api/v1/nope")


def _connect(path: str):
    """连一次 WS，返回 (结果类型, 状态/关闭码)；抛出的必须是**干净拒绝**。"""
    from src.api.main import app

    client = TestClient(app)
    try:
        with client.websocket_connect(path):
            return "accepted", None
    except WebSocketDenialResponse as exc:       # 有拒绝响应扩展：真 404
        return "denied_response", exc.status_code
    except WebSocketDisconnect as exc:           # 退回规范路径：1008
        return "closed", exc.code


@pytest.mark.parametrize("path", NON_WS_PATHS)
def test_ws_to_non_ws_path_is_rejected_not_500(path: str) -> None:
    """★ 必须是 404 / 1008，**不能**是 500（AssertionError 会以异常形式冒出来）。"""
    kind, code = _connect(path)
    assert kind != "accepted", f"{path} 竟然接受了 WebSocket 连接"
    if kind == "denied_response":
        assert code == 404, f"{path} 的拒绝响应码是 {code}，应为 404"
    else:
        assert code == 1008, f"{path} 的关闭码是 {code}，应为 1008（策略违规）"


def test_the_mount_still_serves_http(monkeypatch) -> None:
    """反向：包了一层之后 **HTTP 照常**（首页能出、静态资源能下）。

    只测"WS 被拒绝"会奖励"把静态挂载摘掉"—— 那会让前端整站打不开。
    """
    from src.api.main import app

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code in (200, 304), (
        f"首页状态 {resp.status_code} —— 静态挂载被改动坏了")


@pytest.mark.asyncio
async def test_shim_passes_http_and_blocks_every_other_scope() -> None:
    """★ shim 的**合同**：HTTP 透传、其它作用域一律不进内层应用。

    为什么不通过整站测这条（第一版就是这么写的，当场红了）：测试环境**没有装配
    runtime** ⇒ 存在的 WS 路径会在处理器里抛 `AttributeError: State has no
    attribute 'runtime'` ⇒ 客户端拿到的是 `ClosedResourceError`，
    与"路径不存在"混在一起 —— 判据变成了在测别的东西。

    所以这里直接对 shim 下断言（确定性、零依赖）。而
    「**存在的** WS 路径 + 未登录 ⇒ 403（登录门槛）」那一半由
    `tests/unit/test_login_gate.py::test_pilot_blocks_anonymous_websocket` 覆盖 ——
    两条判据合起来才把"路径不存在(404/1008)"与"没登录(403)"分开。
    """
    from src.api.main import _HttpOnlyStatic

    calls: list[str] = []

    async def _inner(scope, receive, send):  # noqa: ANN001
        calls.append(scope["type"])

    shim = _HttpOnlyStatic(_inner)

    # ① HTTP：必须原样交给内层（静态资源/首页靠它）
    await shim({"type": "http", "path": "/", "headers": [], "extensions": {}},
               None, None)
    assert calls == ["http"], "HTTP 作用域没有透传给内层应用"

    # ② WebSocket：**不许**进内层；必须发出一条干净的拒绝
    sent: list[dict] = []

    async def _send(message):  # noqa: ANN001
        sent.append(message)

    await shim({"type": "websocket", "path": "/nope", "headers": [],
                "extensions": {"websocket.http.response": {}}}, None, _send)
    assert calls == ["http"], "WebSocket 作用域竟然进到了静态挂载（那会 assert 崩）"
    assert sent and sent[0]["type"] == "websocket.http.response.start", sent
    assert sent[0]["status"] == 404, f"拒绝响应码应为 404，实际 {sent[0].get('status')}"

    # ③ 没有该扩展时：退回 accept 前 close(1008)
    sent.clear()
    await shim({"type": "websocket", "path": "/nope", "headers": [],
                "extensions": {}}, None, _send)
    assert sent == [{"type": "websocket.close", "code": 1008}], sent

    # ④ lifespan 等其它作用域：静态挂载不参与（也不许抛）
    await shim({"type": "lifespan"}, None, None)
    assert calls == ["http"], "非 HTTP 作用域被交给了静态挂载"
