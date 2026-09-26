"""认证端点：注册 / 登录 / 免密恢复 / 改密 / 找回 / 会话自助。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.6（API 清单）、
§8.6.11（两层 Cookie 与 sessionId 隔离）。

## 三层 Cookie 的职责（设错任何一条都会出问题）

| Cookie | 内容 | 关键属性 |
|---|---|---|
| `moss_sid` | 不透明 session_id | `HttpOnly; Secure; SameSite=Lax`，**无 Max-Age** |
| `moss_rt` | refresh token | 同上 + `Max-Age=7d` + `Path=/api/v1/auth` |
| `moss_csrf` | CSRF token | **非 HttpOnly**（前端要读）+ `SameSite=Lax` |

- `moss_sid` 放**不透明 ID 而非 JWT**：会话要能**立刻被单独踢掉**
  （用户点"退出这台设备"/ 管理员踢人 / 重放检测），JWT 做不到即时吊销。
- `moss_rt` 收窄 `Path`：只在换新时发送，日常请求不带它，暴露面小一个数量级。

## 安全约定（与设计文档逐条对应）

- **不返回明文联系方式**：一律脱敏（§8.6.6 的展示口径）；
- **失败响应不区分"账号不存在"与"密码错"**（§8.6.7 第 2 条）；
- **发验证码前必须过图形验证码**（`has_captcha`，§8.6.7 第 3 条）；
- **配额耗尽明确拒绝**，不静默失败（ADR D34）；
- **dev 环境**下 Cookie 的 `Secure` 必须关掉，否则 http://127.0.0.1 下前端拿不到 Cookie
  （这是"开发能跑通"与"生产必须安全"的唯一分歧点，用 `settings.env` 区分）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.core.errors import brief
from src.domain.auth.service import AuthService
from src.infrastructure.notify import build_notifier
from src.infrastructure.repositories.auth_sqlite_repo import AuthSqliteRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: Cookie 名（集中定义，前端与本文件共用同一份口径）
COOKIE_SESSION = "moss_sid"
COOKIE_REFRESH = "moss_rt"
COOKIE_CSRF = "moss_csrf"

#: refresh cookie 的 Path：**只在 /api/v1/auth 下发送**（收窄暴露面）
REFRESH_COOKIE_PATH = "/api/v1/auth"


# ======================================================================
# 服务装配（进程内单例）
# ======================================================================

_SERVICE: AuthService | None = None


def get_auth_service() -> AuthService:
    """取认证服务（进程内单例）。

    为什么在这里惰性装配而不是塞进 `build_runtime()`：
    认证是**前置能力** —— 它不该依赖 runtime 是否装配成功
    （研究链路装配失败时，用户仍然要能登录、改密、找回）。
    数据库路径与通知通道都直接来自 `Settings`，没有额外依赖。
    """
    global _SERVICE  # noqa: PLW0603 进程内单例，与项目其它 get_xxx() 同风格
    if _SERVICE is None:
        settings = get_settings()
        repo = AuthSqliteRepository(settings.sqlite_path)
        repo.ensure_schema()
        _SERVICE = AuthService(
            repo, build_notifier(settings),
            app_name=settings.app_name)
        # 装配信息打到 WARNING 级而不是 INFO：
        # 排查"登录莫名 401"时，第一件要确认的事就是**服务进程到底连了哪个库**。
        # 实测踩过：脚本建的用户在 A 库，服务读的是 B 库，症状只是 401，
        # 日志里什么都看不出来。装配是**进程级一次性**事件，用 WARNING 不算吵。
        logger.warning(
            "认证服务已装配：env=%s db=%s notifier=%s",
            settings.env, settings.sqlite_path,
            type(_SERVICE._notifier).__name__)
    return _SERVICE


def reset_auth_service() -> None:
    """清掉单例（**仅测试用**：让每个用例换一个隔离库）。"""
    global _SERVICE  # noqa: PLW0603
    _SERVICE = None


# ======================================================================
# 请求模型
# ======================================================================

class SendCodeBody(BaseModel):
    scene: str = Field(default="register", description="register | reset_password")
    email: str = Field(description="目标邮箱")
    #: 图形验证码**令牌**（由 `/auth/captcha` 下发，一次性）
    captcha_token: str = Field(default="", description="图形验证码令牌")
    #: 图形验证码**答案**（用户看图输入）。服务端按答案哈希校验。
    captcha_answer: str = Field(default="", description="图形验证码答案")


class RegisterBody(BaseModel):
    email: str
    username: str
    password: str
    code: str = Field(description="邮箱验证码")
    captcha_token: str = ""
    captcha_answer: str = ""


class LoginBody(BaseModel):
    account: str = Field(description="邮箱或用户名")
    password: str
    remember_me: bool = Field(default=False, description="保持登录（7 天）")
    #: 仅在"该 IP 已被要求图形码"时才需要填（见 `login` 的防护说明）。
    #: 正常用户第一次登录不需要 —— 所以默认空串，服务端按需校验。
    captcha_token: str = ""
    captcha_answer: str = ""


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


class ForgotBody(BaseModel):
    email: str
    captcha_token: str = ""


class ResetBody(BaseModel):
    email: str
    code: str
    token: str = Field(description="验证码通过后下发的一次性重置令牌")
    new_password: str


# ======================================================================
# Cookie 工具
# ======================================================================

def _cookie_kwargs(request: Request) -> dict[str, Any]:
    """Cookie 的公共属性。

    ⚠️ `Secure` 在 dev 必须关：http://127.0.0.1 下浏览器会**丢弃** Secure Cookie，
    表现为"登录成功但立刻又是未登录"——这个坑很隐蔽（没有报错，只有行为不对）。
    生产/对外试点打开，且由 §8.7.2 的自检保证它们一定跑在 HTTPS 之后
    （Cloudflare Tunnel）。

    ⚠️ 判据是 `is_public`（prod **或** pilot）而不是 `is_prod`：
    试点实例同样在公网上跑 HTTPS，漏掉它就会让凭据在加密隧道上**明文回发**
    —— 隧道的意义正是保护这一段，Cookie 不带 Secure 等于把它白做了。
    """
    settings = get_settings()
    return {
        "httponly": True,
        "secure": settings.is_public,
        "samesite": "lax",
    }


def _set_auth_cookies(response: Response, request: Request, data: dict[str, Any]) -> None:
    """写三个 Cookie（会话 / 记住我 / CSRF）。

    ⚠️ **`moss_rt` 放的是 `remember_token`（记住我令牌），不是 `refresh_token`** ——
    这两个是不同的东西，混用会**直接破坏"短期不看免密"**：
      - `remember_token`：7 天窗口，用于"关掉浏览器再打开时静默换新会话"；
      - `refresh_token`：30 分钟会话窗口内的换新凭据（库里存哈希）。
    实测踩过这个坑：把 refresh_token 写进 cookie 后，`/auth/refresh` 拿它去查
    `fact_remember_token` 必然查不到 → 一律返回"登录状态已失效"，
    表现为**用户每次关浏览器回来都要重新输密码**（需求的核心场景失效）。
    """
    session = data.get("session")
    if session is not None:
        response.set_cookie(COOKIE_SESSION, session.session_id,
                            path="/", **_cookie_kwargs(request))
    # ★ 记住我（长窗口）。只有勾了"记住我"才会有值；没勾则不写这个 Cookie。
    if data.get("remember_token"):
        response.set_cookie(COOKIE_REFRESH, str(data["remember_token"]),
                            path=REFRESH_COOKIE_PATH, max_age=7 * 24 * 3600,
                            **_cookie_kwargs(request))
    if data.get("csrf_token"):
        # CSRF token 必须让前端能读 → httponly=False
        response.set_cookie(COOKIE_CSRF, str(data["csrf_token"]),
                            path="/", httponly=False,
                            secure=_cookie_kwargs(request)["secure"],
                            samesite="lax")


def _clear_auth_cookies(response: Response) -> None:
    for name, path in ((COOKIE_SESSION, "/"), (COOKIE_REFRESH, REFRESH_COOKIE_PATH),
                       (COOKIE_CSRF, "/")):
        response.delete_cookie(name, path=path)


def _client_ip(request: Request) -> str:
    """真实客户端 IP。

    ⚠️ 只认 `CF-Connecting-IP`（Cloudflare 会覆盖并由边缘保证真实性），
    **不取 `X-Forwarded-For` 首段** —— 那是客户端可伪造的（§7.5 坑 2）。
    """
    return (request.headers.get("CF-Connecting-IP")
            or (request.client.host if request.client else "")
            or "")


def _device_label(request: Request) -> str:
    """设备标签（给用户认设备用，不做指纹追踪）。"""
    ua = request.headers.get("User-Agent", "")
    for token, label in (("Edg", "Edge"), ("Chrome", "Chrome"),
                         ("Firefox", "Firefox"), ("Safari", "Safari")):
        if token in ua:
            os_name = ("Windows" if "Windows" in ua else
                       "macOS" if "Macintosh" in ua else
                       "iPhone" if "iPhone" in ua else
                       "Android" if "Android" in ua else "其他")
            return f"{label} / {os_name}"
    return ua[:40] or "unknown"


def _require_session(request: Request) -> str:
    """取当前会话 id；没有有效会话则 401。"""
    session_id = request.cookies.get(COOKIE_SESSION, "")
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    return session_id


async def _current_user(request: Request) -> tuple[str, str]:
    """取 `(user_id, session_id)`，并**滑动续期**。

    这里做续期而不是让每个业务路由自己做：
    漏一处就会出现"某个接口不续期，用户在那页上待久了被踢"。
    """
    session_id = _require_session(request)
    service = get_auth_service()
    session = await service.touch(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    return session.user_id, session_id


# ======================================================================
# 公开端点（全部要限流；图形验证码是发验证码的前置）
# ======================================================================

@router.get("/captcha")
async def captcha(request: Request) -> dict:
    """图形验证码：下发**图片 + 一次性令牌**，服务端真实校验。

    ## 与上一版的区别（这是个安全修复）

    上一版只返回一个随机串（占位实现），**服务端根本不校验** ——
    "发验证码前必须过图形码"这条规则因此**没有实际防护**：
    脚本带任意非空令牌就能通过。

    现在返回真实挑战：
      - `image_png` 为 `data:image/png;base64,...`，前端直接 `<img src>`；
      - `token` 必须随后续请求带回，服务端按**答案哈希**校验；
      - 令牌**一次性**、**绑定客户端 IP**、**3 分钟过期**、
        **试错 N 次即作废**（细节见 `src/domain/auth/human_check.py`）。
    """
    from src.domain.auth.human_check import (
        ChallengeRateLimited,
        get_challenge_service,
        png_data_uri,
    )

    try:
        view = get_challenge_service().issue(
            ip=_client_ip(request),
            ua=request.headers.get("User-Agent", ""),
            debug=True)
    except ChallengeRateLimited as exc:
        # 429：语义是"太频繁"，不是"参数错" —— 前端据此提示等待而非重填
        raise HTTPException(status_code=429, detail=brief(exc)) from exc
    return {
        "captcha_token": view.token,
        "image_png": png_data_uri(view.image_png),
        "expires_in": view.expires_in,
        "meta": view.meta,
        # 仅 dev/test 有值（本地调试不必费劲看图）；生产恒为空
        "debug_answer": view.debug_answer,
    }


def _verify_captcha(request: Request, token: str, answer: str) -> bool:
    """校验图形码（答案 + 令牌），**成功即作废**。"""
    from src.domain.auth.human_check import get_challenge_service

    return get_challenge_service().verify(
        token=str(token or ""), answer=str(answer or ""),
        ip=_client_ip(request))


@router.get("/_debug/whoami")
async def debug_whoami() -> dict:
    """**仅 dev**：报告服务进程实际连的库与可见用户数。

    ## 为什么需要这个端点（实测踩到的排查死角）

    出现"脚本建的用户，HTTP 登录一律 401"时，能想到的原因有七八个
    （密码错、状态不对、库不同、连接池缓存、事务未提交…），而**外部没有任何手段
    能区分它们** —— 库里的 `last_failed_at` 为空，既可能是"没找到用户"，
    也可能是"找到了但没记失败"。加了这个端点，一句话就能定位：
    服务连的库路径对不对、能看见几个用户。

    ⚠️ **公网环境（prod / pilot）直接 404**：它会泄露库路径与用户规模，
    不属于对外接口。
    """
    if get_settings().is_public:
        raise HTTPException(status_code=404, detail="Not Found")
    service = get_auth_service()
    repo = service._repo  # noqa: SLF001 诊断端点，只读
    users = await repo.a_list_users()
    return {
        "env": get_settings().env,
        "settings_sqlite_path": get_settings().sqlite_path,
        "repo_db_path": str(getattr(repo, "_db_path", "(未知)")),
        "cwd": str(__import__("pathlib").Path.cwd()),
        "visible_user_count": len(users),
        "visible_usernames": [u.username for u in users[-10:]],
        "notifier": type(service._notifier).__name__,  # noqa: SLF001
    }


@router.post("/verify-code")
async def verify_code(body: SendCodeBody, request: Request) -> dict:
    """发送验证码（注册 / 找回）。

    ★ **图形码现在是真校验**（以前只检查"令牌非空"）：
    先验答案，再决定是否发码。顺序不能反 —— 否则脚本可以带一个
    假令牌把邮件额度打爆（那正是这条规则要防的事）。
    """
    from src.infrastructure.notify import SCENE_REGISTER

    service = get_auth_service()
    scene = body.scene or SCENE_REGISTER
    if not _verify_captcha(request, body.captcha_token, body.captcha_answer):
        # 400 而不是 401：这是"表单里某一项不对"，前端应让用户重填图形码
        # （401 会被前端理解成"未登录"而去跳登录页 —— 那没有意义）。
        raise HTTPException(
            status_code=400,
            detail={"code": "captcha_failed",
                    "message": "图形验证码不正确或已过期，请重新输入",
                    "refresh_captcha": True})
    outcome = await service.send_code(
        scene=scene, email=body.email,
        has_captcha=True,
        request_ip=_client_ip(request))
    return _to_response(outcome)


@router.post("/register")
async def register(body: RegisterBody, request: Request) -> dict:
    """注册（邮箱验证码通过后落 `pending`，等管理员审批）。"""
    service = get_auth_service()
    outcome = await service.register(
        email=body.email, username=body.username, password=body.password,
        code=body.code, request_ip=_client_ip(request))
    return _to_response(outcome)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict:
    """登录。成功则下发三层 Cookie。

    ## 机器人防护（三层，按"用户无感"优先）

    1. **IP 总量限流**：单 IP 在窗口内请求数超限 → 429。拦高频脚本。
    2. **失败后要求图形码**：该 IP 失败过若干次 → 必须过图形码才继续。
       正常用户第一次登录**不会**看到图形码。
    3. **按账号锁定**（已有，在 `AuthService.login` 里）：拦盯单一账号猛试。

    为什么不是"每次登录都要图形码"：那会让每天登录的人烦，
    而收益有限（脚本过一次图就能继续）。**按失败升级**才是对症的。

    ⚠️ 限流必须在**验密之前**：argon2/pbkdf2 是刻意设计成慢的，
    否则攻击者能用错误的密码白刷 CPU（那是 DoS 面）。
    """
    from src.domain.auth.ratelimit import get_ip_rate_limiter

    ip = _client_ip(request)
    limiter = get_ip_rate_limiter()
    decision = limiter.check(ip)
    if not decision.allowed:
        # 429 + Retry-After：标准做法，客户端/网关都能识别
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": decision.reason},
            headers={"Retry-After": str(max(1, decision.retry_after))})
    limiter.note_request(ip)

    # 已进入"要求图形码"状态 → 先验图再验密
    if decision.require_captcha and not _verify_captcha(
            request, body.captcha_token, body.captcha_answer):
        raise HTTPException(
            status_code=400,
            detail={"code": "captcha_required",
                    "message": "为确认是你本人操作，请先完成图形验证码",
                    "require_captcha": True, "refresh_captcha": True})

    service = get_auth_service()
    outcome = await service.login(
        account=body.account, password=body.password,
        remember_me=body.remember_me,
        device_label=_device_label(request), user_agent=request.headers.get(
            "User-Agent", ""), ip=ip)
    if outcome.ok and outcome.data:
        limiter.note_success(ip)
        data = dict(outcome.data)
        import secrets as _secrets

        data["csrf_token"] = _secrets.token_urlsafe(16)
        _set_auth_cookies(response, request, data)
        # 响应体里**不回显任何令牌** —— 令牌只走 HttpOnly Cookie，
        # 前端拿不到也就无法泄漏到 localStorage / 日志
        return {
            "ok": True,
            "message": outcome.message,
            "user": _public_user(data.get("user")),
            "must_change_password": data.get("must_change_password", False),
        }
    # 认证失败：记账（用于"下一次要不要图形码"的判断）
    limiter.note_failure(ip)
    _fail(outcome)
    return {}


@router.get("/login-mode")
async def login_mode(request: Request) -> dict:
    """当前 IP 登录时**是否需要图形码**（前端据此决定要不要渲染它）。

    单独一个只读端点，而不是让前端"先试一次登录看 400"：
    后者会让正常用户白填一次表单。
    """
    return _login_mode_payload(request)


def _login_mode_payload(request: Request) -> dict:
    """图形码判定（`/auth/login-mode` 与 `/auth/bootstrap` 共用一份）。

    同一份判定的两份拷贝一定会分叉，而分叉的一侧就是"登录页不显示
    图形码、提交时却被要求填"这种自相矛盾的表现。
    """
    from src.domain.auth.ratelimit import get_ip_rate_limiter

    decision = get_ip_rate_limiter().check(_client_ip(request))
    return {
        "require_captcha": bool(decision.require_captcha),
        "allowed": bool(decision.allowed),
        "reason": decision.reason,
        "retry_after": decision.retry_after,
    }


@router.post("/refresh")
async def refresh(request: Request, response: Response) -> dict:
    """用 refresh cookie **静默换新会话**（"短期不看再打开不用输密码"）。

    正常路径会**轮换**令牌；若收到已轮换过的旧令牌 → 服务层判定重放并撤销
    整个 family（§8.6.5④）。
    """
    token = request.cookies.get(COOKIE_REFRESH, "")
    if not token:
        raise HTTPException(status_code=401, detail="没有可续期的登录状态")
    service = get_auth_service()
    outcome = await service.resume_with_remember(
        token=token, device_label=_device_label(request),
        ip=_client_ip(request), user_agent=request.headers.get("User-Agent", ""))
    if outcome.ok and outcome.data:
        data = dict(outcome.data)
        import secrets as _secrets

        data["csrf_token"] = _secrets.token_urlsafe(16)
        _set_auth_cookies(response, request, data)
        return {"ok": True, "user": _public_user(data.get("user"))}
    # 重放检测：清 Cookie，强制重新登录（**不要**让前端无限重试 refresh）
    _clear_auth_cookies(response)
    _fail(outcome)
    return {}


@router.post("/password/forgot")
async def forgot(body: ForgotBody, request: Request) -> dict:
    """发起找回。**无论邮箱是否存在，一律返回同一文案**（防枚举）。"""
    service = get_auth_service()
    outcome = await service.request_reset(
        email=body.email, has_captcha=bool(body.captcha_token),
        request_ip=_client_ip(request))
    return _to_response(outcome)


@router.post("/password/reset")
async def reset(body: ResetBody, request: Request) -> dict:
    """用「验证码 + 一次性令牌」重置密码；成功后**撤销全部会话**。"""
    service = get_auth_service()
    outcome = await service.reset_password(
        email=body.email, code=body.code, token=body.token,
        new_password=body.new_password, request_ip=_client_ip(request))
    return _to_response(outcome)


# ======================================================================
# 需登录端点
# ======================================================================

@router.post("/logout")
async def logout(request: Request, response: Response,
                 all_devices: bool = False) -> dict:
    """登出（`all_devices=true` 时退出全部设备 —— 用户自救通道）。"""
    session_id = _require_session(request)
    service = get_auth_service()
    user_id = ""
    session = await service._repo.a_get_session(session_id)  # noqa: SLF001 取归属
    if session is not None:
        user_id = session.user_id
    outcome = await service.logout(session_id=session_id, all_devices=all_devices,
                                   user_id=user_id)
    _clear_auth_cookies(response)
    return _to_response(outcome)


@router.get("/sessions")
async def list_sessions(request: Request) -> dict:
    """活跃设备列表（看清谁登着）。"""
    user_id, current = await _current_user(request)
    service = get_auth_service()
    sessions = await service.list_sessions(user_id)
    return {"sessions": [
        {"session_id": s.session_id, "current": s.session_id == current,
         "device_label": s.device_label, "ip": s.ip,
         "created_at": s.created_at, "last_seen_at": s.last_seen_at,
         "valid": s.is_valid()}
        for s in sessions]}


@router.delete("/sessions/{session_id}")
async def kill_session(session_id: str, request: Request) -> dict:
    """踢掉某一台设备（**校验归属**，否则就是越权接口）。"""
    user_id, _ = await _current_user(request)
    service = get_auth_service()
    outcome = await service.kill_session(user_id=user_id, session_id=session_id)
    return _to_response(outcome)


@router.post("/password/change")
async def change_password(body: ChangePasswordBody, request: Request) -> dict:
    """改密码。**撤销其它全部会话 + 全部 remember token**（§8.6.5③）。"""
    user_id, session_id = await _current_user(request)
    service = get_auth_service()
    outcome = await service.change_password(
        user_id=user_id, old_password=body.old_password,
        new_password=body.new_password, current_session=session_id)
    return _to_response(outcome)


@router.get("/me")
async def me(request: Request) -> dict:
    """当前身份 + 脱敏后的联系方式（**不返回明文**）。"""
    user_id, session_id = await _current_user(request)
    return await _me_payload(user_id, session_id)


@router.get("/bootstrap")
async def bootstrap(request: Request, response: Response,
                    refresh: bool = True) -> dict:
    """开机探测：**一次往返**回答"我是谁 + 要不要图形码"。

    ## 为什么必须有这个端点（2026-09-26 用户报障）

    原来前端要串三次：`/me`（401）→ `/refresh`（200）→ `/me`（200）。
    本机每次 1~62 ms 看着无所谓，但用户走的是**公网隧道**：实测单次
    0.4~2.0 秒、还会偶发 502，于是"正在检查登录状态"要停 10 秒左右。

    三次往返里有一次是**纯浪费**：`/refresh` 的响应里本来就已经带了
    `user`（见下面 `_public_user(data.get("user"))`），前端却还是重打了一遍
    `/me` 才敢建状态。另一次是协议噪声：没有会话时先吃一个 401 再续期。

    现在合并成一次：
      ① 会话 Cookie 有效 → 直接返回身份（**不轮换任何令牌**）；
      ② 否则带 refresh Cookie 且 `refresh=true` → 服务端续期，
         并把新身份一起返回（客户端不必也不应再问一次）。

    ## 为什么是 GET 而不是 POST

    客户端语义是"我只想读当前状态"。GET 会被浏览器缓存/预取这件事由
    响应头 `no-store` 兜住；更重要的是：**没有会话时它才写库**（换新会话），
    有会话时它是纯只读的 —— 用一个 POST 去表达"通常什么也不改"会让
    中间层和浏览器把它当非幂等请求排队处理。

    续期失败（没有 refresh Cookie / 令牌已轮换过）**不报错**，而是回
    `authenticated: false` —— 这不是异常，是"该登录了"的正常结论。
    前端因此不必用异常控制流来判断登录态。
    """
    response.headers["Cache-Control"] = "no-store"
    login_mode = _login_mode_payload(request)
    session_id = request.cookies.get(COOKIE_SESSION, "")
    service = get_auth_service()

    if session_id:
        session = await service.touch(session_id)
        if session is not None:
            # 已登录：绝不重写 Cookie（重写会换掉 CSRF 令牌，
            # 让其它标签页里"已读到的旧令牌"立刻失效）。
            payload = await _me_payload(session.user_id, session_id)
            payload.update({"authenticated": True, "renewed": False,
                            "login_mode": login_mode})
            return payload

    if not refresh:
        return {"authenticated": False, "renewed": False, "user": {},
                "login_mode": login_mode}

    token = request.cookies.get(COOKIE_REFRESH, "")
    if not token:
        return {"authenticated": False, "renewed": False, "user": {},
                "login_mode": login_mode}

    outcome = await service.resume_with_remember(
        token=token, device_label=_device_label(request),
        ip=_client_ip(request), user_agent=request.headers.get("User-Agent", ""))
    if not (outcome.ok and outcome.data):
        # 重放/过期：清 Cookie，让浏览器别再拿着废令牌反复试。
        _clear_auth_cookies(response)
        return {"authenticated": False, "renewed": False, "user": {},
                "login_mode": login_mode}

    data = dict(outcome.data)
    import secrets as _secrets

    data["csrf_token"] = _secrets.token_urlsafe(16)
    _set_auth_cookies(response, request, data)
    user = _public_user(data.get("user"))
    user_id = str(user.get("user_id", ""))
    new_session = data.get("session")
    payload = await _me_payload(user_id, str(getattr(new_session, "session_id", "")))
    payload.update({"authenticated": True, "renewed": True,
                    "login_mode": login_mode})
    return payload


async def _me_payload(user_id: str, session_id: str) -> dict:
    """`/me` 与 `/bootstrap` 共用的载荷。

    抽出来是因为两者**必须逐字段一致** —— `/me` 是登录后各页面
    "重新读取自己的身份"用的，`/bootstrap` 是开机探测用的，一旦
    字段分叉，会出现"刚打开时头像/套餐正常、点一下刷新就变空"这种怪 bug。
    """
    service = get_auth_service()
    user = await service._repo.a_get_user(user_id)  # noqa: SLF001 只读当前用户
    contacts = await service.contacts(user_id)
    return {
        "user_id": user_id,
        "username": "" if user is None else user.username,
        "display_name": "" if user is None else user.display_name,
        "status": "" if user is None else user.status,
        "applied_tier": "" if user is None else user.applied_tier,
        "valid_until": "" if user is None else user.valid_until,
        "session_id": session_id,
        "contacts": contacts,
    }


# ======================================================================
# 响应辅助
# ======================================================================

def _public_user(user: Any) -> dict[str, Any]:
    """用户对象 → 可外发的字段（**绝不带哈希、令牌、明文联系方式**）。"""
    if user is None:
        return {}
    return {
        "user_id": getattr(user, "user_id", ""),
        "username": getattr(user, "username", ""),
        "display_name": getattr(user, "display_name", ""),
        "status": getattr(user, "status", ""),
        "applied_tier": getattr(user, "applied_tier", ""),
        "valid_until": getattr(user, "valid_until", ""),
    }


def _to_response(outcome: Any) -> dict[str, Any]:
    """统一出参：`ok` + `message` + `code` + 可选 `data`。

    ⚠️ **失败时直接抛 `HTTPException`**（而不是 200 返回 `ok:false`）。

    实测踩过：早先只有 `login` 走 `_fail()`、其余处理器直接 `return _to_response(...)`，
    结果"图形码未通过"这类失败**返回 200 + 错误体** —— 前端按状态码判断时
    会把它当成成功（表现为"点了发送验证码没反应"），而服务端日志里一切正常。
    **状态码属于协议层，不能靠 body 里的 `ok` 字段替代。**
    """
    if not outcome.ok:
        _fail(outcome)
    payload: dict[str, Any] = {
        "ok": True,
        "message": outcome.message or "",
        "code": outcome.code or "",
    }
    if outcome.data:
        # 过滤掉不能外发的字段（会话对象 / 令牌 / 用户 ORM 对象）
        safe = {k: v for k, v in outcome.data.items()
                if k not in {"session", "access_token", "refresh_token",
                             "remember_token", "remember_family"}}
        if "user" in safe:
            safe["user"] = _public_user(safe["user"])
        payload.update(safe)
    return payload


def _fail(outcome: Any) -> None:
    """失败 → 抛 HTTPException（**保留可给用户看的文案**）。

    状态码映射（前端据此决定跳哪个页面）：
      401 → 会话/凭据问题（跳登录页）
      403 → 账号状态问题（跳提示页：待审批 / 已到期 / 已禁用）
      429 → 限流 / 配额
    """
    code = outcome.code or "invalid"
    status = 400
    if code in {"credentials", "locked", "replay_detected", "remember_expired",
                "bad_code", "bad_token", "bad_old_password", "code_required"}:
        status = 401
    elif code in {"status", "locked"}:
        status = 403
    elif code in {"rate_limited", "quota_exceeded"}:
        status = 429
    elif code == "send_failed":
        status = 502
    raise HTTPException(status_code=status,
                        detail={"code": code, "message": outcome.message})


__all__ = [
    "COOKIE_CSRF",
    "COOKIE_REFRESH",
    "COOKIE_SESSION",
    "REFRESH_COOKIE_PATH",
    "get_auth_service",
    "reset_auth_service",
    "router",
]
