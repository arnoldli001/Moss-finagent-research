"""接入层的租户与审计中间件 —— 身份进入系统的**唯一入口**。

## 为什么身份必须在这里派生

业界最常见的越权写法是 `tenant_id = request.headers["X-Tenant-Id"]`：
客户端改一个字符串就能读别人的数据。本中间件因此：

1. **凭证优先**：`Authorization: Bearer <token>` → 校验 → 派生 Principal；
2. **请求头只做"降级提示"**：仅当 `MOSS_ALLOW_HEADER_IDENTITY=1`
   （**只允许在单机开发环境打开**）时，才接受 `X-Tenant-Id`，且强制打审计标记
   `auth_source=header-dev`；
3. **默认拒绝**：两条都拿不到 → 401，而不是回落到 default 租户。

## 审计埋点为什么放在这里

放在中间件而不是各路由里，才能保证**没有路由能绕过**：
每个进入的请求都留下 `(who, tenant, method, path, status, 耗时, 跨墙)`。
路由内部只补"业务动作"级的留痕（如"导出了 N 行"）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.core.errors import brief
from src.core.tenancy import (
    DataClass,
    Principal,
    Role,
    TenantError,
    WallGroup,
    principal_scope,
)

logger = logging.getLogger(__name__)

#: 免鉴权路径（健康检查与前端静态资源）。**只放确实不需要身份的。**
#:
#: `/api/v1/health/live` 必须在这里，两个理由：
#:   ① **登录页就要能用** —— 前端连接状态条在还没有会话时就要判断
#:      "后端在不在"，否则用户会在登录页看到一条假的"服务不可达"；
#:   ② 存活探针（k8s liveness / 负载均衡）本来就不该带凭证。
#: 它的返回体只有 `{"ok": true, "ts": ...}`（无 pid、无环境名、无版本号），
#: 匿名可读不泄露任何部署信息。
#:
#: ⚠️ 但**聚合健康度 `/api/v1/health` 故意不放进来**：它会返回 Ollama/
#: DeepSeek 配置状态、数据源健康度、库表行数 —— 那是内部拓扑，
#: 匿名可读等于给攻击者一份踩点清单。所以"存活免鉴权、就绪要鉴权"。
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/api/v1/metrics/health", "/healthz", "/favicon.ico",
    "/api/v1/metrics/ready", "/api/v1/health/live",
})

#: 仅开发环境可用：允许用请求头声明身份（`MOSS_ALLOW_HEADER_IDENTITY=1` 时才生效）
_HEADER_IDENTITY_ENV = "MOSS_ALLOW_HEADER_IDENTITY"

#: 强制鉴权开关。**未打开时**走"本地开发兜底身份"，并在响应头与启动日志里
#: **显式标注未强制** —— 让"没做鉴权"是一个看得见的状态，而不是静默降级。
ENFORCE_ENV = "MOSS_TENANCY_ENFORCE"

#: 本地开发兜底身份（仅在一个租户内，权限最小）
_LOCAL_DEV = Principal(user_id="local-dev", tenant_id="local",
                       roles=frozenset({Role.RESEARCHER}),
                       clearance=DataClass.INTERNAL,
                       wall_group=WallGroup.RESEARCH,
                       auth_source="dev-bypass")


def enforcement_enabled() -> bool:
    return os.environ.get(ENFORCE_ENV, "").strip() in {"1", "true", "yes"}


def describe_enforcement() -> str:
    """启动日志用：一句话说清当前鉴权状态。"""
    if enforcement_enabled():
        return f"多租户鉴权：**已强制**（{ENFORCE_ENV}=1）"
    return (f"多租户鉴权：**未强制** —— 未带凭证的请求将以本地开发身份 "
            f"({_LOCAL_DEV.tenant_id}) 放行。生产部署请设 {ENFORCE_ENV}=1；"
            f"仅本地联调可设 {_HEADER_IDENTITY_ENV}=1 用请求头声明身份。")


def _header_identity_allowed() -> bool:
    return os.environ.get(_HEADER_IDENTITY_ENV, "").strip() in {"1", "true", "yes"}


def _default_audit_dir() -> Path:
    return Path(os.environ.get("MOSS_AUDIT_DIR", "data/audit"))


class TenantAuditLog:
    """租户维度的访问审计（JSONL，追加写，含哈希链字段）。

    与业务库**分开存放**：审计与业务同库同权限等于没审计。
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = Path(directory) if directory else _default_audit_dir()
        self._path = self._dir / "access_audit.jsonl"
        self._last_hash = ""

    @property
    def path(self) -> Path:
        return self._path

    def _chain_hash(self, payload: dict) -> str:
        import hashlib

        raw = (self._last_hash + json.dumps(payload, sort_keys=True,
                                            ensure_ascii=False)).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        self._last_hash = digest
        return digest

    def record(self, *, principal: Principal | None, method: str, path: str,
               status: int, latency_ms: int, action: str = "",
               extra: dict | None = None) -> None:
        entry: dict = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": latency_ms,
            "action": action,
            **(principal.audit_fields() if principal else
               {"user_id": "", "tenant_id": "", "auth_source": "none"}),
            **(extra or {}),
        }
        entry["chain_hash"] = self._chain_hash(entry)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # 审计写失败**必须吵**，不能静默
            logger.error("访问审计写入失败（%s）：%s", self._path, exc)


def principal_from_token(token: str) -> Principal | None:
    """由 Bearer token 派生身份。

    当前实现是**无状态的签名载荷**（`base64(payload).signature`），
    足以表达"服务端派生、客户端不可伪造"的语义，且不引入额外依赖。
    生产环境应替换为机构 IdP 的 JWT 校验（OIDC / mTLS），
    只需保持本函数签名不变 —— 上层不感知。
    """
    import base64
    import hashlib
    import hmac

    secret = os.environ.get("MOSS_IDENTITY_SECRET", "")
    if not secret:
        return None
    try:
        payload_b64, signature = token.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("身份令牌签名校验失败")
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        return Principal(
            user_id=str(payload["sub"]),
            tenant_id=str(payload["tenant"]),
            roles=frozenset(Role.parse(r) for r in payload.get("roles", [])),
            clearance=DataClass[payload.get("clearance", "PUBLIC")],
            wall_group=WallGroup(payload.get("wall", "platform")),
            groups=frozenset(payload.get("groups", [])),
            auth_source="token",
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("身份令牌解析失败：%s", exc)
        return None


def _principal_from_headers(request: Request) -> Principal | None:
    """**仅开发环境**：用请求头声明身份，并打上显式标记以便审计区分。"""
    tenant = request.headers.get("X-Tenant-Id", "").strip()
    if not tenant or not _header_identity_allowed():
        return None
    user = request.headers.get("X-User-Id", "dev").strip() or "dev"
    roles = [r for r in request.headers.get("X-Roles", "").split(",") if r.strip()]
    try:
        parsed = frozenset(Role.parse(r) for r in roles) if roles else \
            frozenset({Role.RESEARCHER})
        clearance = DataClass[request.headers.get(
            "X-Clearance", "INTERNAL").strip().upper()]
        wall = WallGroup(request.headers.get("X-Wall", "research").strip().lower())
    except (ValueError, KeyError) as exc:
        logger.warning("开发身份头非法：%s", exc)
        return None
    logger.warning("使用开发用请求头身份 tenant=%s user=%s —— "
                   "生产环境必须关闭 %s", tenant, user, _HEADER_IDENTITY_ENV)
    return Principal(user_id=user, tenant_id=tenant, roles=parsed,
                     clearance=clearance, wall_group=wall,
                     auth_source="header-dev")


def resolve_principal(request: Request) -> Principal | None:
    """从请求派生身份。凭证优先，请求头仅在开发开关打开时兜底。"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        principal = principal_from_token(auth[7:].strip())
        if principal is not None:
            return principal
    return _principal_from_headers(request)


class TenancyMiddleware(BaseHTTPMiddleware):
    """注入 Principal + 写访问审计 + 默认拒绝。

    ⚠️ 中间件顺序：必须**在外层**（先于业务路由）注册，
    否则路由拿不到 Principal。
    """

    def __init__(self, app, *, audit: TenantAuditLog | None = None,
                 public_paths: frozenset[str] | None = None) -> None:
        super().__init__(app)
        self._audit = audit or TenantAuditLog()
        self._public = public_paths or _PUBLIC_PATHS

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in self._public or path.startswith(("/assets/", "/static/")):
            return await call_next(request)

        principal = resolve_principal(request)
        bypass = principal is None and not enforcement_enabled()
        if bypass:
            principal = _LOCAL_DEV
        if principal is None:
            self._audit.record(principal=None, method=request.method, path=path,
                               status=401, latency_ms=0,
                               extra={"reason": "missing_or_invalid_credential"})
            return JSONResponse(
                {"detail": "未认证：请提供 Authorization: Bearer <token>。"
                           "本服务不做匿名降级。"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"})

        began = time.perf_counter()
        try:
            with principal_scope(principal):
                response = await call_next(request)
        except TenantError as exc:
            self._audit.record(principal=principal, method=request.method,
                               path=path, status=403, latency_ms=0,
                               extra={"reason": brief(exc)})
            # 对外只发报错码 + 通用文案（异常原文可能含内部隔离规则，只进审计）
            return JSONResponse(
                {"detail": "权限不足", "code": "AUTH_4030"}, status_code=403)
        elapsed = int((time.perf_counter() - began) * 1000)
        self._audit.record(principal=principal, method=request.method, path=path,
                           status=response.status_code, latency_ms=elapsed)
        response.headers["X-Tenant-Id"] = principal.tenant_id
        if bypass:
            # 让"本次未鉴权"对调用方与日志都可见
            response.headers["X-Tenancy"] = "dev-bypass"
        return response


__all__ = [
    "ENFORCE_ENV",
    "TenancyMiddleware",
    "TenantAuditLog",
    "describe_enforcement",
    "enforcement_enabled",
    "principal_from_token",
    "resolve_principal",
]
