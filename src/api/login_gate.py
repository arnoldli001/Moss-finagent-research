"""登录门槛中间件：公网环境下，**除白名单外一律要求有效会话**。

## 为什么必须单独做这一层（实测数据，不是假设）

2026-09-23 在 dev 实例上实测"不带任何 Cookie"直接打接口：

| 路径 | 未登录结果 |
|---|---|
| `/api/v1/intraday/watchlist` | **200**（整份自选清单） |
| `/api/v1/intraday/snapshot?code=600036` | **200**（整套做T取数/计算入口） |
| `/api/v1/scheduler/jobs` | **200**（内部任务配置） |
| `/api/v1/metrics` | **200** |
| `/docs`、`/openapi.json` | **200**（完整接口面） |
| `/api/v1/auth/_debug/whoami` | **200**（**库路径 + 用户名列表**） |
| `/api/v1/admin/*`、`/api/v1/me/*` | 401 ✅（这些路由自己查了会话） |

原因很直接：`TenancyMiddleware` 在 `MOSS_TENANCY_ENFORCE` 未打开时，会把
**没有凭证的请求**当成"本地开发身份"放行（`_LOCAL_DEV`），
而它的凭证解析只认 `Authorization: Bearer` 与开发用请求头 ——
**不认本项目的会话 Cookie**。所以"每个路由自己查会话"成了唯一防线，
而只做了一半的接口（量化/调度/指标/文档）就裸露在公网上。

## 这一层与"多租户鉴权"的分工

| 层 | 认什么凭证 | 判什么 | 现状 |
|---|---|---|---|
| `LoginGateMiddleware`（本模块） | **会话 Cookie** `moss_sid` | **登录了没有** | 公网环境自动生效 |
| `TenancyMiddleware` | Bearer 令牌 / 开发头 | **能看哪一类数据** [注1] | 待补"会话→Principal"桥 |

[注1] 三个维度：DataClass（数据密级）/ 租户 / 中国墙（利益冲突隔离）。

之所以不直接打开 `MOSS_TENANCY_ENFORCE=1` 一步到位：那要求把会话翻译成
`Principal`，而这条桥还没建 —— 强行打开会让**每个浏览器请求**都 401
（连登录页都打不开）。所以先落地"登录门槛"这一半，它就能独立把上面
那张表里的 200 全部变成 401；DataClass 平面随后接管。

## 三条设计约束

1. **fail-closed**：判据是"路径在白名单里"而不是"路径不在黑名单里"。
   新加的路由默认**需要登录** —— 新功能忘记加鉴权时，默认是安全的。
2. **只在公网环境生效**（`is_public`：prod / pilot）。dev/test 保持原样：
   本地调试不该被登录拦住，而 dev 的隔离性由"只绑 127.0.0.1 + 独立库"
   与启动横幅负责。
3. **不缓存会话结论**。缓存能省一次索引查询，代价是"用户被踢之后还能
   再用几秒"——对一个**安全**判断来说这个代价划不来。等真测出延迟瓶颈
   再加 TTL 缓存，且必须带上"登出/踢人即刻失效"的钩子。
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

#: 免登录路径（**精确匹配**）。
#:
#: `"/"` 必须在里面：前端是单页应用，登录页本身就是 `/` 这一份 HTML
#: —— 把它拦掉会变成"要登录才能打开登录页"的死锁。
#: `/index.html` 同理（有些浏览器会显式请求它）。
PUBLIC_EXACT: frozenset[str] = frozenset({
    "/",
    "/index.html",
    "/favicon.ico",
    "/healthz",
    "/api/v1/health/live",      # 0 I/O 存活探针：登录页的连接状态条要用
    "/api/v1/metrics/health",   # 既有公开探针，保持兼容
    "/api/v1/metrics/ready",
})

#: 免登录路径前缀。
#:
#: ⚠️ `/api/v1/auth/` 整段放行是**刻意的**：这一族的职责就是"在还没有会话时
#: 工作"（登录、验证码、注册、找回密码、以及**开机静默续期用的 `/auth/refresh`**
#: —— 它只带"记住我"Cookie，不带会话 Cookie，最容易漏掉）。
#: 需要会话的那几个（`/auth/me`、`/auth/sessions`、`/auth/password/change`、
#: `/auth/_debug/whoami`）**自己会查**，返回它们自己的 401/404，
#: 比在这一层统一拦截给出的错误信息更准确。
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/api/v1/auth/",
    "/assets/",
    "/static/",
)

#: 交互式文档与 schema：公网环境**直接 404**。
#:
#: 它们在 FastAPI 构造时已经按环境关掉了（见 `main.py` 的 `docs_url=None`），
#: 这里**再兜一层**，因为那一次判断发生在**进程启动那一刻**：
#: 配置热改、测试里重载模块、或有人把 env 写错顺序，都可能让构造时的判断
#: 与实际运行环境不一致。两处都写，才有"无论怎么启动都不对外暴露接口面"。
#:
#: 用 404 而不是 401：401 等于承认"这里确实有个东西，只是你没权限"，
#: 而 404 不给任何可用来踩点的信息。
DOC_PATHS: frozenset[str] = frozenset({"/docs", "/redoc", "/openapi.json"})


def is_public_path(path: str) -> bool:
    """该路径是否**不需要登录**即可访问。"""
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def docs_kwargs(settings: object) -> dict[str, object]:
    """`FastAPI(...)` 的文档相关参数：公网环境**根本不注册**这些路由。

    抽成函数是为了**可测**：写在 `FastAPI(...)` 的调用里就没法单测，
    而这里正是"接口面要不要对外"的开关。测试直接断言两个环境的返回值，
    不依赖"哪个环境先 import 了 main"这种顺序耦合。
    """
    public = bool(getattr(settings, "is_public", False))
    if public:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc",
            "openapi_url": "/openapi.json"}


class LoginGateMiddleware(BaseHTTPMiddleware):
    """公网环境下强制登录；dev/test 完全放行（保持本地开发体验）。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        from src.core.config import get_settings

        public = get_settings().is_public
        path = request.url.path

        # 文档/schema：公网 404（dev/test 照常可用，本地联调要看文档）
        if path in DOC_PATHS and public:
            logger.info("公网环境隐藏接口文档：path=%s ip=%s",
                        path, _client_ip(request))
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        if not public:
            return await call_next(request)

        # OPTIONS 一律放行：它是 CORS 预检，本身不携带业务数据；
        # 拦掉它只会让浏览器报一个与鉴权无关的 CORS 错误，误导排障。
        if request.method == "OPTIONS" or is_public_path(path):
            return await call_next(request)

        session_id = request.cookies.get(_session_cookie_name(), "")
        if session_id and await _session_is_alive(session_id):
            return await call_next(request)

        # 审计：被挡下来的请求必须留痕（谁在什么路径上被拒），
        # 否则"公网上有人扫接口"这件事在日志里看不见。
        logger.warning("登录门槛拦截：path=%s ip=%s", path, _client_ip(request))
        return JSONResponse(
            {"detail": "需要登录：请先登录后再访问该接口。"},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )


def _session_cookie_name() -> str:
    """会话 Cookie 名。延迟导入避免与路由模块形成循环依赖。

    （`require_admin` 里也是同样的写法 —— 保持一致的既有风格。）
    """
    from src.api.routes.auth import COOKIE_SESSION

    return COOKIE_SESSION


def _client_ip(request: Request) -> str:
    """真实客户端 IP。**只认 Cloudflare 覆盖的 `CF-Connecting-IP`**。

    ⚠️ 不要退回 `request.client.host`：接上 Cloudflare Tunnel 之后，
    所有公网请求在 TCP 层都来自本机的 `cloudflared`（127.0.0.1），
    日志里会变成"全网访问都来自本机"，等于没有来源信息。
    """
    return (request.headers.get("CF-Connecting-IP")
            or (request.client.host if request.client else "") or "")


async def _session_is_alive(session_id: str) -> bool:
    """会话是否仍然有效（**只读**，不续期、不写库）。

    复用领域层已经写好的那套判断（`SessionRecord.is_valid()`：未撤销
    ＋ 未超滑动期 ＋ 未超 12 小时绝对上限），而不是在这里重写一遍 ——
    "会话算不算有效"如果有两份实现，它们**一定**会在某次改动后分叉，
    而分叉的那一侧就是漏网之门。

    用户状态也要一起判：管理员把人停用（`disabled`）或到期之后，
    手上那张还没到期的会话票据必须立刻失效，不能等 30 分钟。
    """
    from src.api.routes.auth import get_auth_service

    try:
        session, user = await get_auth_service().validate_session(session_id)
    except Exception:  # noqa: BLE001 鉴权层不能把 500 抛成"看起来能继续"
        logger.exception("会话校验异常，按**拒绝**处理（fail-closed）")
        return False
    if session is None or user is None:
        return False
    if str(getattr(user, "status", "")) != "active":
        return False
    return True


__all__ = [
    "DOC_PATHS",
    "PUBLIC_EXACT",
    "PUBLIC_PREFIXES",
    "LoginGateMiddleware",
    "docs_kwargs",
    "is_public_path",
]
