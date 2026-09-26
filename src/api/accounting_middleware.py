"""记账归属中间件：把**会话身份**与**请求路径**绑进记账上下文。

对应模块说明见 `src/core/accounting.py`（为什么刻意不合并进 `Principal`）。

## 它做什么

每个 HTTP 请求进入时：

    moss_sid Cookie ──validate_session──▶ (user_id, applied_tier)
                                          │
    request.url.path ─────────────────────┴─▶ AccountingContext
                                              （contextvars，只存活于本次请求）

之后该请求内发生的一切 LLM 调用（含它 `create_task` 出来的后台任务 ——
`asyncio` 在建任务时会复制当前上下文）都会把 `path / tenant_id / user_id`
随行写进 `data/audit/llm_audit.jsonl`。

## 三个刻意的取舍

1. **永不拦请求、永不抛异常**。这是记账层，不是权限层：会话无效就当作
   "没有身份"（留空），请求照常往下走 —— 该拦人的是
   `LoginGateMiddleware` 与各端点的 `require_admin`/`require_feature`。
   在记账层 fail-closed 会把"身份查不到"变成"接口 500"，方向完全错了。
2. **只在真的有 Cookie 时才查库**。匿名请求（登录页、静态资源、探针）
   不产生任何查询；有 Cookie 时是一次索引查询（`validate_session` 是
   **只读**的，不续期、不写库）。
3. **不做 TTL 缓存**。与登录门槛同一条理由：缓存能省一次索引查询，代价是
   "用户被停用后还能再记几秒钟的账"。对一个**审计**字段来说这个代价划不来。
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.core.accounting import AccountingContext, use

logger = logging.getLogger(__name__)


async def session_identity(request: Request) -> tuple[str, str]:
    """从会话 Cookie 取 `(tenant_id, user_id)`；取不到返回 `("", "")`。

    `tenant_id` 取用户的 `applied_tier`（= 套餐等级），与
    `configs/platform_tiers.json`、`/me/features`、运维页的租户列**同一口径** ——
    这个项目的"租户"在人这一侧就是套餐等级。
    """
    from src.api.routes.auth import COOKIE_SESSION, get_auth_service

    session_id = request.cookies.get(COOKIE_SESSION, "")
    if not session_id:
        return "", ""
    try:
        session, user = await get_auth_service().validate_session(session_id)
    except Exception:  # noqa: BLE001 记账层绝不因为查身份失败而拦住请求
        logger.debug("记账身份解析异常（按无身份处理）", exc_info=True)
        return "", ""
    if session is None or user is None:
        return "", ""
    if str(getattr(user, "status", "")) != "active":
        # 停用/过期账号的旧会话：**不记到该用户名下**（钱不是他花的，
        # 而是"一个已失效的会话"花的 —— 归到 (未归属) 更诚实）
        return "", ""
    return (str(getattr(user, "applied_tier", "") or ""),
            str(user.user_id))


class AccountingMiddleware(BaseHTTPMiddleware):
    """把 `(path, tenant, user)` 绑进记账上下文（不拦任何请求）。"""

    #: 这些路径**不查会话**：它们不可能触发 LLM 调用，查一次库纯属浪费
    #: （前端每页都要拉几十个静态资源，而每个请求都是一次 SQLite 查询）。
    SKIP_PREFIXES: tuple[str, ...] = ("/assets/", "/static/")
    SKIP_EXACT: frozenset[str] = frozenset({"/favicon.ico"})

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        tenant_id, user_id = "", ""
        if path not in self.SKIP_EXACT and not path.startswith(self.SKIP_PREFIXES):
            try:
                tenant_id, user_id = await session_identity(request)
            except Exception:  # noqa: BLE001 双保险：任何意外都不影响请求
                logger.debug("记账上下文装配失败（按无身份处理）", exc_info=True)
                tenant_id, user_id = "", ""
        ctx = AccountingContext(path=path, tenant_id=tenant_id,
                                user_id=user_id)
        # ⚠️ 必须在 `await call_next(...)` **之前**进入 `with`：
        #   Starlette 的 `BaseHTTPMiddleware` 在下游新起一个任务，
        #   而任务创建时会**复制当前上下文** —— 先设后调用，下游才看得到。
        with use(ctx):
            return await call_next(request)


__all__ = ["AccountingMiddleware", "session_identity"]
