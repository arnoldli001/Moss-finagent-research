"""会话身份解析：把 `moss_sid` Cookie 换成一个可信的 `(user_id, tier)`。

## 为什么单独抽一个模块

这三个函数原本长在 `src/api/routes/my_pools.py`（自选池路由）里。
**自选池功能已删除**（用户口径 2026-09-23："很鸡肋，不需要了"），
但"当前这个请求属于谁、是什么等级"是所有**个人化接口**都要问的问题
（个股口径 `/me/profiles`、可见页签 `/me/features`），
所以把它挪到中立位置，而不是跟着那个路由一起删掉。

## 三个必须守住的判断（都是踩出来的）

1. **读路径也要续期**（`touch()`）：否则用户长时间只读不写，
   滑动窗口到期后突然"数据看起来没了"。
2. **`status` 与"到期"是两条不同的路径**：到期后 `status` 仍是 `active`，
   只查 status 会留下真实缺口 —— 登录被挡住了，但**已经在里面的旧会话
   照样能改数据**。所以写路径（`write=True`）必须单独判 `is_expired`。
3. **`write=False` 时到期用户仍可读**："能看不能改"是刻意设计的中间态
   （设计 §8.6.11.6），不是漏判。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

__all__ = ["current_user", "ws_identity"]


async def ws_identity(websocket) -> tuple[str, str] | None:
    """WebSocket 版身份解析，返回 `(user_id, tier)`；拿不到返回 `None`。

    ## 为什么不能直接复用 `current_user`

    `current_user` 是给 `Request` 写的：它失败时**抛 `HTTPException`**。
    WebSocket 没有"401 响应体"这种东西，抛出去只会变成一条无意义的
    连接异常。所以这里返回 `None` 让调用方自己决定（关连接 / 降级）。

    ## 为什么需要它（不是"能连上就够了"）

    WS 的脱敏必须**按连接**做：同一个租户（= 套餐等级）下既有管理员也有
    普通用户，而只有管理员该看到来源真名与原文链接。没有身份就没法区分，
    只能要么全给（泄源）要么全不给（管理员看不到排障信息）。

    **fail-closed**：任何异常都返回 `None`，调用方按"非管理员"处理。
    """
    session_id = ""
    try:
        session_id = websocket.cookies.get("moss_sid", "")
    except Exception:  # noqa: BLE001 拿不到 cookie 就按未登录
        return None
    if not session_id:
        return None
    try:
        from src.api.routes.auth import get_auth_service

        session, user = await get_auth_service().validate_session(session_id)
    except Exception:  # noqa: BLE001 校验异常按拒绝处理（fail-closed）
        logger.exception("WS 身份解析异常，按未登录处理")
        return None
    if session is None or user is None:
        return None
    if str(getattr(user, "status", "")) != "active":
        return None
    return str(user.user_id), str(getattr(user, "applied_tier", "") or "public")


async def current_user(request: Request, *,
                       write: bool = True) -> tuple[str, str]:
    """解析当前会话，返回 `(user_id, tenant_id/tier)`；失败抛 401/403。

    `write=True`（默认）表示这是一次**改状态**的请求：
    已到期账号只读、禁用/删除账号直接 403。
    """
    from src.api.routes.auth import COOKIE_SESSION, get_auth_service
    from src.core.errors import brief
    from src.core.tenancy import AccessDenied

    session_id = request.cookies.get(COOKIE_SESSION, "")
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    service = get_auth_service()
    try:
        session = await service.touch(session_id)
    except AccessDenied as exc:
        raise HTTPException(
            status_code=401,
            detail=brief(exc) or "登录状态已失效") from exc
    if session is None:
        raise HTTPException(status_code=401, detail="登录状态已失效")
    user = await service._repo.a_get_user(session.user_id)  # noqa: SLF001
    if user is None or user.status in {"disabled", "deleted", "rejected"}:
        # 403：身份在，但状态不允许访问（禁用/删除/驳回都不该继续用）
        raise HTTPException(status_code=403,
                            detail="账号状态不允许访问（可能已被禁用或删除）")
    if user.is_expired and write:
        raise HTTPException(
            status_code=403,
            detail=f"账号已于 {user.valid_until[:10]} 到期，续期前不能修改"
                   f"（仍可查看）")
    return str(user.user_id), str(user.applied_tier or "public")


#: WebSocket 因"未登录"关闭时使用的应用自定义关闭码（4000-4999 区间保留给应用）。
WS_UNAUTHORIZED = 4401


async def ws_allow(websocket) -> bool:
    """WebSocket 的登录门槛。允许返回 `True`；否则**已关闭连接**并返回 `False`。

    ## 为什么 WebSocket 需要单独一道门槛（2026-09-23 实测发现）

    `LoginGateMiddleware` 是 `BaseHTTPMiddleware` —— 它只包装 `scope["type"] == "http"`，
    **WebSocket 连接根本不经过它**。而 `/api/v1/ws/intraday`、`/api/v1/ws/alerts`
    自身也没有任何会话校验。

    实测（公网隧道上匿名连接）：

        wss://<临时域名>/api/v1/ws/intraday?code=300308  →  握手成功
        首帧 snapshot: 中际旭创 922.50  ← 未登录就拿到了整套做T快照

    也就是说：**登录门槛在 HTTP 上生效、在 WS 上被绕过**。
    对一个公网可达的实例，这等于把量化信号流敞着。

    ## 为什么只在公网环境强制

    与 HTTP 门槛同一个判据（`is_public`）：dev/test 不拦，保持本地调试体验
    （本地连 WS 不需要先登录）。要改这个口径，改 `is_public` 一处即可。

    ## 为什么"失败即关闭"而不是继续跑

    WS 没有"401 响应体"这种东西，错误只能通过关闭码表达。所以这里
    **先关再返回 False**，调用方看到 False 直接 return —— 不能"记个日志然后照常推"。
    """
    from src.core.config import get_settings

    if not get_settings().is_public:
        return True

    session_id = ""
    try:
        session_id = websocket.cookies.get("moss_sid", "")
    except Exception:  # noqa: BLE001 拿不到 cookie 就按未登录处理
        session_id = ""
    if session_id:
        from src.api.routes.auth import get_auth_service

        try:
            session, user = await get_auth_service().validate_session(session_id)
        except Exception:  # noqa: BLE001 校验异常按拒绝处理（fail-closed）
            logger.exception("WS 会话校验异常，按拒绝处理")
            session, user = None, None
        if session is not None and user is not None \
                and str(getattr(user, "status", "")) == "active":
            return True

    logger.warning("WebSocket 拒绝未登录连接：path=%s ip=%s",
                   getattr(websocket.url, "path", "?"),
                   (websocket.headers.get("CF-Connecting-IP")
                    or (websocket.client.host if websocket.client else "")))
    try:
        await websocket.close(code=WS_UNAUTHORIZED, reason="需要登录")
    except Exception:  # noqa: BLE001 连接可能已经断了
        pass
    return False
