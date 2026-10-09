"""双凭证桥的判据（`CHG-0192` / 债 #10）。

## 这条判据防的是什么

本项目有**两套凭证**，各自只认自己那套：

| 层 | 认什么 |
|---|---|
| `LoginGateMiddleware` | 会话 Cookie（`moss_sid`） |
| `TenancyMiddleware` | `Authorization: Bearer <签名令牌>` |

于是 `MOSS_TENANCY_ENFORCE=1` **不能直接打开**：浏览器带着合法会话 Cookie 进来，
租户中间件认不出身份 ⇒ 按"默认拒绝"回 **401** ⇒ 表现是"登录成功但每个接口都 401"，
且没有任何报错线索（债 #10 的原文）。

修法：`principal_from_session()` —— 把会话 Cookie 桥成 `Principal`。

## 判据强度

`test_admin_only_from_the_shared_admin_tier` —— ★ **权限判据**：
只有 `applied_tier == ADMIN_TIER` 才给 `Role.ADMIN`，**别的 tier 一律只给
`RESEARCHER`**（把 `vip` 也判成 ADMIN 就红）。这是"不许在这里按套餐猜角色"的机器版。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from starlette.requests import Request

from src.api import tenancy_middleware as tm
from src.api.routes.auth import COOKIE_SESSION
from src.core.tenancy import DataClass, Role, WallGroup


def _request(cookies: dict[str, str] | None = None,
             headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    cookie = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    if cookie:
        raw_headers.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "method": "GET", "path": "/api/v1/x",
                    "headers": raw_headers, "query_string": b""})


class _Session:
    def __init__(self, tenant_id: str, session_id: str = "sid-1") -> None:
        self.tenant_id = tenant_id
        self.session_id = session_id


class _User:
    def __init__(self, user_id: str = "u-1", *, tier: str = "trial",
                 status: str = "active") -> None:
        self.user_id = user_id
        self.applied_tier = tier
        self.status = status


@pytest.fixture
def patch_validate(monkeypatch: pytest.MonkeyPatch):
    """把会话校验换成替身（不在单测里起真库）。"""
    box: dict[str, Any] = {"session": _Session("dept-x"), "user": _User()}

    class _Svc:
        async def validate_session(self, session_id: str):
            return box["session"], box["user"]

    import src.api.routes.auth as auth_routes

    monkeypatch.setattr(auth_routes, "get_auth_service", lambda: _Svc())
    return box


# ======================================================================
# 1. 桥本身
# ======================================================================

def test_valid_session_cookie_yields_a_principal(patch_validate):
    """★ 核心：合法的会话 Cookie 必须能派生出 `Principal`（这就是"桥"）。"""
    req = _request({COOKIE_SESSION: "sid-1"})
    p = asyncio.run(tm.principal_from_session(req))
    assert p is not None, "会话 Cookie 没被认出来 ⇒ enforce 打开后浏览器全 401"
    assert p.user_id == "u-1"
    assert p.tenant_id == "dept-x", "租户必须取 `SessionRecord.tenant_id`"
    assert p.session_id == "sid-1", "session_id 要带上（会话级隔离的锚点）"
    assert p.auth_source == "session"
    assert p.authenticated is True


def test_no_cookie_yields_none(patch_validate):
    """没有 Cookie ⇒ `None`（不猜身份、不回落默认租户）。"""
    assert asyncio.run(tm.principal_from_session(_request())) is None


def test_disabled_account_session_is_refused(patch_validate):
    """★ 账号被停用后，手上那张未到期的会话票必须**立刻**失效。"""
    patch_validate["user"] = _User(status="disabled")
    req = _request({"moss_sid": "sid-1"})
    assert asyncio.run(tm.principal_from_session(req)) is None, (
        "停用账号的旧会话仍能派生身份 ⇒ 管理员停人之后对方还能用 30 分钟"
    )


def test_validate_exception_is_fail_closed(patch_validate, monkeypatch):
    """★ 校验抛异常时按**未认证**处理（fail-closed），不许放行。"""
    import src.api.routes.auth as auth_routes

    class _Boom:
        async def validate_session(self, session_id: str):
            raise RuntimeError("库挂了")

    monkeypatch.setattr(auth_routes, "get_auth_service", lambda: _Boom())
    assert asyncio.run(tm.principal_from_session(_request({"moss_sid": "s"}))) is None


def test_empty_session_record_yields_none(patch_validate):
    """会话/用户为空 ⇒ `None`。"""
    patch_validate["session"] = None
    assert asyncio.run(tm.principal_from_session(_request({"moss_sid": "s"}))) is None


# ======================================================================
# 2. ★ 权限映射（最小权限）
# ======================================================================

def test_admin_only_from_the_shared_admin_tier(patch_validate):
    """★ 只有 `applied_tier == ADMIN_TIER` 才给 `Role.ADMIN`。

    把别的 tier（vip / trial）也判成 ADMIN ⇒ 立刻红 —— 那是提权漏洞。
    """
    from src.api.routes.admin import ADMIN_TIER

    patch_validate["user"] = _User(tier=ADMIN_TIER)
    p = asyncio.run(tm.principal_from_session(_request({"moss_sid": "s"})))
    assert Role.ADMIN in p.roles, "管理员没拿到 ADMIN 角色 ⇒ 管理端全 403"

    for tier in ("vip", "trial", ""):
        patch_validate["user"] = _User(tier=tier)
        p = asyncio.run(tm.principal_from_session(_request({"moss_sid": "s"})))
        assert Role.ADMIN not in p.roles, (
            f"tier={tier!r} 被判成管理员 ⇒ 提权漏洞"
        )
        assert p.roles == frozenset({Role.RESEARCHER}), (
            "非管理员只应给最小角色 RESEARCHER（按套餐猜角色是提权）"
        )


def test_clearance_and_wall_are_the_conservative_ones(patch_validate):
    """`clearance` / `wall_group` 给保守值（更高档要显式授予，不在这里猜）。"""
    p = asyncio.run(tm.principal_from_session(_request({"moss_sid": "s"})))
    assert p.clearance is DataClass.INTERNAL
    assert p.wall_group is WallGroup.PLATFORM
    assert p.groups == frozenset()


# ======================================================================
# 3. 接线（三层顺序 + 唯一调用点）
# ======================================================================

def test_resolve_principal_tries_bearer_then_session_then_headers():
    """★ `resolve_principal` 必须依次尝试三层，且顺序是"令牌 → 会话 → 开发头"。"""
    src = inspect.getsource(tm.resolve_principal)
    assert "principal_from_token" in src
    assert "principal_from_session" in src
    assert "_principal_from_headers" in src
    order = [src.index(x) for x in
             ("principal_from_token", "principal_from_session",
              "_principal_from_headers")]
    assert order == sorted(order), "三层顺序错了（开发用请求头必须最后）"


def test_dispatch_awaits_the_principal_resolution():
    """★ `resolve_principal` 现在是协程 ⇒ 调用点必须 `await`。

    漏掉 `await` 的后果是**拿到一个 coroutine 对象**（恒真）⇒ 每个请求都
    被当成"有身份"，而且身份是错的 —— 静默提权。

    ## 为什么用 AST 而不是字符串匹配

    第一版写的是 `"await resolve_principal(request)" in getsource(dispatch)` ——
    它在一次**门禁与编辑同时进行**的运行里假红过（`getsource` 读到了正在被改的
    文件）。字符串判据对空白/换行/注释都敏感，而这条要钉的是**语法结构**
    （有没有 `Await` 包住那个调用），AST 恰好只表达结构。

    ⚠️ `inspect.getsource(方法)` 返回的是**带缩进的片段**，`ast.parse` 直接吃会
    `IndentationError` ⇒ 必须先 `textwrap.dedent`。
    """
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(tm.TenancyMiddleware.dispatch))
    tree = ast.parse(src)
    awaited = [getattr(node.value.func, "id", getattr(node.value.func, "attr", ""))
               for node in ast.walk(tree)
               if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)]
    assert "resolve_principal" in awaited, (
        f"dispatch 里没有 `await resolve_principal(...)` ⇒ principal 会是 coroutine "
        f"对象（恒真）⇒ 静默提权。实际 await 的调用：{awaited}"
    )


def test_bridge_is_exported():
    """公开出口：别的层（如 `/api/v1/auth/me` 之类）可能要问同一个映射。"""
    assert "principal_from_session" in tm.__all__
