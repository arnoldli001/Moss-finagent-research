"""管理员控制台 API：注册审批 / 用户增删 / 套餐等级 / 有效期。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.10（管理员控制台）。

## 为什么必须有这一层（不是"锦上添花"）

本系统的注册流是**审批制**：新注册用户落 `pending`，**不能登录**。
没有管理台，这个流程就是**断的** —— 用户注册完永远卡在"等待审批"，
而管理员没有任何入口去放行。所以管理台不是附加功能，是注册流的前置条件。

## 权限门槛：为什么用 `applied_tier == 'admin'`

本项目有两套授权概念，**不要混用**：

| 体系 | 落点 | 服务对象 |
|---|---|---|
| `Principal` + `Role` + `_POLICY` | 研究链路（Agent/数据分级/隔离墙） | 进程内分析任务 |
| `applied_tier`（会话身份） | **人**（登录后的 Web 用户） | 管理台、配额 |

管理台管的是"**人**"，属于第二套。硬把 `Principal` 拉进来会需要一整套
`Role` 映射与数据分级，而这里判断的只是"这个人是不是管理员"。
所以本模块用 `applied_tier == 'admin'` 作为门槛，并在**每个**端点上强制，
而不是只在页面上藏入口 —— 前端隐藏只是体验，不是权限。

## 留痕

每个改状态的端点都写审批流水（哈希链，见仓储 `_record_review`），
记录**谁**（operator=管理员的 user_id）、**对谁**、**改了什么**、**从什么改成什么**。
四眼原则要求"操作可归因到人"，匿名管理员在合规上等于没有管理。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.routes.auth import get_auth_service
from src.core.errors import brief
from src.domain.auth.service import check_password_strength

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

#: 系统只认这一个管理员等级（与 `dim_user.applied_tier` 取值一致）
ADMIN_TIER = "admin"
#: 可分配的套餐等级（顺序即管理台上下拉框的顺序：权限从大到小）
ASSIGNABLE_TIERS = ("admin", "vip", "trial")
#: 可设置的用户状态（`pending` 由注册产生，不在这里手动设置）
ASSIGNABLE_STATUS = ("active", "disabled", "expired", "rejected")


# ======================================================================
# 请求模型
# ======================================================================

class ApproveBody(BaseModel):
    """审批通过：**必须同时给出等级与有效期**。

    为什么两者都必填而不是给默认值：审批是"授予访问权"这个动作本身，
    默认给 `trial` + 30 天会让管理员在没想清楚的情况下就放行了 ——
    而"这个人能用多久、能用多少"恰恰是本系统唯一需要人工决策的事。
    """

    tier: str = Field(description="admin | vip | trial")
    days: int = Field(default=30, ge=1, le=3650,
                      description="有效天数（从今天起）")
    display_name: str = ""
    note: str = ""


class UpdateUserBody(BaseModel):
    """改用户：所有字段可选，只改传了的。"""

    status: str = Field(default="", description="active|disabled|expired|rejected")
    tier: str = Field(default="", description="admin|vip|trial")
    days: int = Field(default=0, ge=0, le=3650,
                      description="把有效期设成「今天 + N 天」；0 = 不改")
    valid_until: str = Field(default="", description="直接指定到期时间（ISO）")
    display_name: str = ""
    note: str = ""


class CreateUserBody(BaseModel):
    """管理员直接开号（用于第一批用户/内部同事，跳过邮箱验证）。"""

    username: str
    email: str = ""
    password: str
    tier: str = Field(default="trial")
    days: int = Field(default=30, ge=1, le=3650)
    display_name: str = ""
    must_change_password: bool = Field(
        default=True, description="首次登录强制改密（管理员知道初始密码）")


class ResetPasswordBody(BaseModel):
    new_password: str
    must_change_password: bool = True


# ======================================================================
# 管理员门槛
# ======================================================================

async def require_admin(request: Request) -> str:
    """校验当前会话属于管理员，返回 `user_id`；否则 401/403。

    ⚠️ **401 与 403 的区别必须守住**：

    | 情况 | 状态码 | 前端应该做什么 |
    |---|---|---|
    | 没登录 / 会话失效 | **401** | 跳登录页（或先静默续期） |
    | 登录了但不是管理员 | **403** | 提示"无权限"，**不要跳转** |

    把"不是管理员"也报成 401 会让前端跳登录页 —— 而"登录一百次也不是管理员"，
    那是个死循环。所以这里对**会话无效**单独捕获并转成 401
    （`service.touch()` 在会话失效时是**抛** `AccessDenied` 而不是返回 None，
    直接调会把异常漏成 500）。
    """
    from src.api.routes.auth import COOKIE_SESSION
    from src.core.tenancy import AccessDenied

    session_id = request.cookies.get(COOKIE_SESSION, "")
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    service = get_auth_service()
    try:
        session = await service.touch(session_id)
    except AccessDenied as exc:
        # 会话过期/被踢/重放检测撤销 —— 这些都是"重新登录"，属 401
        raise HTTPException(status_code=401,
                            detail=brief(exc) or "登录状态已失效") from exc
    if session is None:
        raise HTTPException(status_code=401, detail="登录状态已失效")
    user = await service._repo.a_get_user(session.user_id)  # noqa: SLF001
    if user is None or user.status != "active":
        raise HTTPException(status_code=403, detail="账号状态不允许管理操作")
    if user.applied_tier != ADMIN_TIER:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return str(user.user_id)


def _client_ip(request: Request) -> str:
    """真实客户端 IP。只认 Cloudflare 覆盖的 `CF-Connecting-IP`。"""
    return (request.headers.get("CF-Connecting-IP")
            or (request.client.host if request.client else "") or "")


def _iso_days_from_now(days: int) -> str:
    """`今天 + N 天` 的 ISO 时间（本地时区，与库里其它时间一致）。"""
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() + timedelta(days=days)).isoformat(
        timespec="seconds")


#: 本题幂等键的命名空间（一个接口一个，避免不同接口互相命中）。
_IDEM_SCOPE_CREATE_USER = "POST /api/v1/admin/users"


async def _idempotent(
    request: Request,
    *,
    scope: str,
    principal: str,
    execute: Callable[[], Awaitable[dict]],
) -> dict:
    """按 `X-Idempotency-Key` 执行**最多一次**，重复请求回放上次结果。

    为什么开号必须走这里：浏览器对 `POST` **不会**自动重试（非幂等），
    客户端只能自己重试；而"客户端重试"与"服务端其实已经执行成功、
    只是响应丢在回程"这两件事，客户端**无法区分**。不做幂等键，
    自动重试就会开出双份账号。

    三种走向：

    | 情况 | 行为 |
    |---|---|
    | 没有键 / 键非法 | 直接执行（退化成改动前的行为，保持兼容） |
    | 键已存在 | 回放缓存响应，**不重新执行** |
    | 键正在执行中 | 轮询等待首个请求的结果（这就是自动重试的路径） |

    ⚠️ 失败路径**必须** `release`：否则一次失败的请求会让该键在 TTL 内
    一直显示"执行中"，用户的每次重试都卡在等待里 —— 比重复执行更糟。
    """
    from src.core.idempotency import (
        DEFAULT_WAIT_SECONDS,
        HEADER_NAME,
        build_key,
        get_idempotency_store,
        sanitize,
    )

    client_key = sanitize(request.headers.get(HEADER_NAME, ""))
    if not client_key:
        return await execute()

    store = get_idempotency_store()
    key = build_key(scope, principal, client_key)

    found, cached = store.get(key)
    if found:
        logger.info("幂等回放：%s", scope)
        return cached

    if not store.claim(key):
        # 另一个请求正在执行同一个键 —— 大概率就是客户端的自动重试。
        deadline = time.monotonic() + DEFAULT_WAIT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            found, cached = store.get(key)
            if found:
                logger.info("幂等回放（等待后命中）：%s", scope)
                return cached
        # 等到超时仍无结果：宁可重复执行也不把用户永久挂在这里。
        logger.warning("幂等键 %s 等待超时，改为自行执行", key)

    try:
        result = await execute()
    except BaseException:
        store.release(key)
        raise
    store.put(key, result)
    return result


def _validate_tier(tier: str) -> str:
    value = str(tier or "").strip().lower()
    if value not in ASSIGNABLE_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"套餐等级只能是 {'/'.join(ASSIGNABLE_TIERS)}，收到 {tier!r}")
    return value


def _validate_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value not in ASSIGNABLE_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"状态只能是 {'/'.join(ASSIGNABLE_STATUS)}，收到 {status!r}")
    return value


def _user_json(u: Any, *, sessions: int = 0) -> dict[str, Any]:
    """用户 → 管理台展示结构（**不含**密码哈希等任何凭据）。"""
    return {
        "user_id": u.user_id,
        "username": u.username,
        "display_name": u.display_name,
        "status": u.status,
        "tier": u.applied_tier,
        "valid_until": u.valid_until,
        "valid_from": u.valid_from,
        "created_at": u.created_at,
        "reviewed_by": u.reviewed_by,
        "reviewed_at": u.reviewed_at,
        "review_note": u.review_note,
        "expired": u.is_expired,
        "can_login": u.can_login()[0],
        "login_block_reason": u.can_login()[1],
        "active_sessions": sessions,
    }


def _contact_json(c: Any) -> dict[str, Any]:
    """联系方式**脱敏**后展示 —— 管理员也不需要看全量邮箱。"""
    value = str(c.value or "")
    if "@" in value:
        name, _, domain = value.partition("@")
        masked = f"{name[:2]}***@{domain}"
    elif len(value) >= 7:
        masked = f"{value[:3]}****{value[-4:]}"
    else:
        masked = "***"
    return {"kind": c.kind, "masked": masked, "verified": bool(c.verified_at),
            "is_primary": bool(c.is_primary)}


# ======================================================================
# 概览
# ======================================================================

@router.get("/overview")
async def overview(request: Request) -> dict:
    """管理台首屏：各状态人数 + 待审批列表。

    **把待审批单独返回**而不是让前端过滤：管理员进管理台的第一件事
    永远是"有没有人等我批"，这个数字要在首屏就出现。
    """
    await require_admin(request)
    from src.core.config import get_settings

    settings = get_settings()
    repo = get_auth_service()._repo  # noqa: SLF001
    counts = await _to_thread(repo.count_users_by_status)
    pending = await _to_thread(
        lambda: repo.list_users(status="pending", limit=200))
    return {
        "counts": counts,
        "pending_count": counts.get("pending", 0),
        "pending": [_user_json(u) for u in pending],
        "tiers": list(ASSIGNABLE_TIERS),
        "statuses": list(ASSIGNABLE_STATUS),
        # ★ 这个管理台**管的是哪个实例的账号库** —— 必须让管理员一眼看见。
        #
        # 2026-09-23 实测踩到：客户在**试点实例**（8110 / data/pilot/）注册并提示
        # "等待管理员审批"，而管理员打开的是**调试实例**（8100 / data/dev/）的
        # 用户管理 —— 两边账号库是分开的，于是"看不到申请记录"，看起来像注册没落库。
        # 排查方向会被完全带偏（去查注册逻辑、查通知、查审批状态），
        # 而真相只是"看错了实例"。
        #
        # 所以把 env 与库路径一起下发（管理台本身就是管理员专用，不涉及泄露）。
        "instance": {
            "env": settings.env,
            "db": settings.sqlite_path,
        },
    }


@router.get("/users")
async def list_users(request: Request, status: str = "", keyword: str = "",
                     limit: int = 200) -> dict:
    """用户列表（可按状态/关键词筛选）。"""
    await require_admin(request)
    repo = get_auth_service()._repo  # noqa: SLF001
    users = await _to_thread(lambda: repo.list_users(
        status=status, keyword=keyword, limit=min(max(1, limit), 500)))
    session_counts = await _to_thread(repo.count_active_sessions_by_user)
    return {"users": [_user_json(u, sessions=session_counts.get(u.user_id, 0))
                      for u in users],
            "total": len(users)}


@router.get("/users/{user_id}")
async def user_detail(user_id: str, request: Request) -> dict:
    """单个用户详情：身份 + 脱敏联系方式 + 活跃设备 + 审批流水。"""
    await require_admin(request)
    repo = get_auth_service()._repo  # noqa: SLF001
    user = await _to_thread(lambda: repo.get_user(user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    contacts = await _to_thread(lambda: repo.list_contacts(user_id))
    sessions = await _to_thread(lambda: repo.list_sessions(user_id))
    reviews = await _to_thread(lambda: repo.list_reviews(user_id, limit=50))
    # ⚠️ `active_sessions` 必须是**仍然有效**的会话数（`is_valid()`：未撤销
    #    ＋ 未过滑动窗口 ＋ 未过绝对上限），**不是** `list_sessions()` 的长度 ——
    #    后者只过滤了"未撤销"，里面还含**滑动窗口已过期**的旧会话。
    #    这里曾与列表列各错一处：列表那次漏判 `idle_expires_at`，这次直接把
    #    "未撤销条数"当在线数，于是详情里的数字与它自己下面那行
    #    「在线设备（N）」当场对不上（一个 4、一个 1）。
    #    界面仍然拿到**全部**未撤销会话，好把已失效的那几条如实列出来。
    active = [s for s in sessions if s.is_valid()]
    return {
        "user": _user_json(user, sessions=len(active)),
        "contacts": [_contact_json(c) for c in contacts],
        "sessions": [
            {"session_id": s.session_id, "device_label": s.device_label,
             "ip": s.ip, "created_at": s.created_at,
             "last_seen_at": s.last_seen_at, "valid": s.is_valid()}
            for s in sessions],
        "reviews": [
            {"action": r.action, "from_status": r.from_status,
             "to_status": r.to_status, "reviewer_id": r.reviewer_id,
             "note": r.note, "tier_code": r.tier_code,
             "valid_until": r.valid_until,
             # 流水的字段名是 `at`，对外统一叫 `created_at`
             "created_at": r.at}
            for r in reviews],
    }


# ======================================================================
# 审批与增删
# ======================================================================

@router.post("/users/{user_id}/approve")
async def approve_user(user_id: str, body: ApproveBody,
                       request: Request) -> dict:
    """**审批通过**：状态置 `active` + 指定等级与有效期。"""
    admin_id = await require_admin(request)
    tier = _validate_tier(body.tier)
    repo = get_auth_service()._repo  # noqa: SLF001
    user = await _to_thread(lambda: repo.get_user(user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.status == "active" and tier == user.applied_tier:
        raise HTTPException(status_code=409, detail="该用户已是此状态与等级，无需重复审批")

    valid_until = _iso_days_from_now(body.days)
    updated = await _to_thread(lambda: repo.update_user_status(
        user_id, "active", valid_until=valid_until,
        reviewed_by=admin_id, review_note=body.note or f"审批通过（{tier}）",
        operator=admin_id, action="approve", ip=_client_ip(request)))
    if updated is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    await _to_thread(lambda: repo.set_user_tier_and_validity(
        user_id, tier=tier, valid_until=valid_until,
        display_name=body.display_name, operator=admin_id,
        ip=_client_ip(request), note=f"审批授予 {tier} / {body.days} 天"))
    final = await _to_thread(lambda: repo.get_user(user_id))
    logger.warning("管理员审批通过：admin=%s user=%s tier=%s days=%d",
                   admin_id, user_id, tier, body.days)
    return {"ok": True, "message": f"已通过，等级 {tier}，有效期 {body.days} 天",
            "user": _user_json(final)}


@router.post("/users/{user_id}/reject")
async def reject_user(user_id: str, request: Request, note: str = "") -> dict:
    """**驳回注册**：状态置 `rejected`，用户看到驳回原因。"""
    admin_id = await require_admin(request)
    repo = get_auth_service()._repo  # noqa: SLF001
    updated = await _to_thread(lambda: repo.update_user_status(
        user_id, "rejected", reviewed_by=admin_id,
        review_note=note or "未通过审批", operator=admin_id,
        action="reject", note=note or "未通过审批", ip=_client_ip(request)))
    if updated is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "message": "已驳回", "user": _user_json(updated)}


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UpdateUserBody,
                      request: Request) -> dict:
    """改状态 / 等级 / 有效期（管理台最常用的一个接口）。"""
    admin_id = await require_admin(request)
    repo = get_auth_service()._repo  # noqa: SLF001
    user = await _to_thread(lambda: repo.get_user(user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    # ★ 防止管理员把自己降级/停用后**再也进不来**：
    #   系统只认 `applied_tier == 'admin'`，一旦唯一的 admin 被改成 trial，
    #   就没有任何界面能把权限加回来了（只能改库）。这个坑必须在服务端堵。
    if user_id == admin_id:
        if body.tier and body.tier != ADMIN_TIER:
            raise HTTPException(
                status_code=400,
                detail="不能修改自己的套餐等级（否则将失去管理入口）")
        if body.status and body.status != "active":
            raise HTTPException(
                status_code=400, detail="不能停用/删除自己的账号")

    messages: list[str] = []
    if body.status:
        status = _validate_status(body.status)
        await _to_thread(lambda: repo.update_user_status(
            user_id, status, reviewed_by=admin_id, review_note=body.note,
            operator=admin_id, action=f"admin:{status}", ip=_client_ip(request)))
        messages.append(f"状态→{status}")

    tier = _validate_tier(body.tier) if body.tier else ""
    valid_until = body.valid_until or (
        _iso_days_from_now(body.days) if body.days else "")
    if tier or valid_until or body.display_name:
        await _to_thread(lambda: repo.set_user_tier_and_validity(
            user_id, tier=tier, valid_until=valid_until,
            display_name=body.display_name, operator=admin_id,
            ip=_client_ip(request), note=body.note))
        if tier:
            messages.append(f"等级→{tier}")
        if valid_until:
            messages.append(f"有效期→{valid_until[:10]}")

    final = await _to_thread(lambda: repo.get_user(user_id))
    return {"ok": True,
            "message": "已更新：" + ("、".join(messages) or "无变化"),
            "user": _user_json(final)}


@router.post("/users")
async def create_user(body: CreateUserBody, request: Request) -> dict:
    """管理员直接开号（**跳过邮箱验证**，用于第一批用户/内部同事）。

    与自助注册的区别：自助注册落 `pending` 等审批；这里直接 `active`。
    但仍然强制"首次登录改密"（默认开启）—— 因为初始密码是管理员设定的，
    管理员知道它，不改密等于两个人共用一把钥匙。

    **带 `X-Idempotency-Key` 时最多执行一次**：网络层报错后客户端可以安全
    重试，不会开出两个账号。键按管理员 user_id 再命名一次空间，防止
    跨管理员读到别人的响应体（响应里含初始密码）。
    """
    admin_id = await require_admin(request)
    return await _idempotent(
        request,
        scope=_IDEM_SCOPE_CREATE_USER,
        principal=admin_id,
        execute=lambda: _create_user_inner(body, request, admin_id),
    )


async def _create_user_inner(
    body: CreateUserBody, request: Request, admin_id: str) -> dict:
    tier = _validate_tier(body.tier)
    username = str(body.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    ok, why = check_password_strength(body.password, username=username)
    if not ok:
        raise HTTPException(status_code=400, detail=f"密码不符合要求：{why}")

    repo = get_auth_service()._repo  # noqa: SLF001
    if await _to_thread(lambda: repo.get_user_by_username(username)) is not None:
        raise HTTPException(status_code=409, detail=f"用户名 {username} 已存在")

    user_id = f"u_{secrets.token_hex(8)}"
    valid_until = _iso_days_from_now(body.days)
    try:
        await _to_thread(lambda: repo.create_user(
            user_id=user_id, username=username,
            display_name=body.display_name or username, status="active",
            valid_until=valid_until, applied_tier=tier))
        await _to_thread(lambda: repo.set_password(
            user_id, body.password, must_change=body.must_change_password))
        if body.email:
            await _to_thread(lambda: repo.bind_contact(
                user_id=user_id, kind="email", value=body.email, verified=True))
    except Exception as exc:  # noqa: BLE001
        # 半成品账号比"创建失败"更难处理：没有密码就无法登录、又占着用户名。
        # 所以失败时把已插入的行清掉，保证"要么建成、要么什么都没有"。
        reason = f"创建失败回滚：{exc}"
        try:
            await _to_thread(lambda msg=reason: repo.update_user_status(
                user_id, "deleted", operator=admin_id,
                note=msg, ip=_client_ip(request)))
        except Exception:  # noqa: BLE001
            logger.exception("创建失败后回滚用户 %s 也失败，需人工清理", user_id)
        raise HTTPException(status_code=400,
                            detail=f"创建失败：{brief(exc)}") from exc

    await _to_thread(lambda: repo._record_review(  # noqa: SLF001
        user_id=user_id, action="admin:create", from_status="", to_status="active",
        reviewer_id=admin_id, tier_code=tier, valid_until=valid_until,
        note=f"管理员开号（{tier}/{body.days}天）", ip=_client_ip(request)))
    final = await _to_thread(lambda: repo.get_user(user_id))
    logger.warning("管理员开号：admin=%s user=%s tier=%s", admin_id, user_id, tier)
    return {"ok": True, "message": f"已创建 {username}（{tier}）",
            "user": _user_json(final),
            "initial_password": body.password}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request, note: str = "") -> dict:
    """**软删**用户（保留审计链，见设计 §8.6.10.3）。

    为什么不做物理删除：审批链与访问审计必须保持可验证
    （《证券法》第二百一十四条要求资料真实完整）；物理删除会让链断在这里。
    软删同时会**释放该用户的邮箱**，允许重新注册。
    """
    admin_id = await require_admin(request)
    if user_id == admin_id:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    repo = get_auth_service()._repo  # noqa: SLF001
    if await _to_thread(lambda: repo.get_user(user_id)) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    await _to_thread(lambda: repo.update_user_status(
        user_id, "deleted", reviewed_by=admin_id,
        review_note=note or "管理员删除", operator=admin_id,
        action="admin:delete", note=note or "管理员删除",
        ip=_client_ip(request)))
    revoked = await _to_thread(
        lambda: repo.revoke_user_sessions(user_id, "admin:delete"))
    await _to_thread(lambda: repo.revoke_user_remembers(user_id))
    logger.warning("管理员删除用户：admin=%s user=%s 吊销会话=%s",
                   admin_id, user_id, revoked)
    return {"ok": True, "message": "已删除（软删，审计链保留）",
            "revoked_sessions": revoked}


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: str, body: ResetPasswordBody,
                         request: Request) -> dict:
    """管理员重置某用户的密码（**用于用户忘记邮箱/收不到验证码**）。

    重置后**吊销该用户全部会话**：管理员改的密码若立即生效而旧会话还在，
    等于"换了锁但旧钥匙还能开门"。
    """
    admin_id = await require_admin(request)
    repo = get_auth_service()._repo  # noqa: SLF001
    user = await _to_thread(lambda: repo.get_user(user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    ok, why = check_password_strength(body.new_password, username=user.username)
    if not ok:
        raise HTTPException(status_code=400, detail=f"密码不符合要求：{why}")
    await _to_thread(lambda: repo.set_password(
        user_id, body.new_password, must_change=body.must_change_password))
    await _to_thread(
        lambda: repo.revoke_user_sessions(user_id, "admin:reset_password"))
    await _to_thread(lambda: repo.revoke_user_remembers(user_id))
    await _to_thread(lambda: repo._record_review(  # noqa: SLF001
        user_id=user_id, action="admin:reset_password",
        from_status=user.status, to_status=user.status,
        reviewer_id=admin_id, note="管理员重置密码",
        ip=_client_ip(request)))
    logger.warning("管理员重置密码：admin=%s user=%s", admin_id, user_id)
    # 文案同样要限定"**该用户的**设备" —— 管理员看到"其它设备已退出"
    # 会怀疑自己把别人也踢了（与用户改密那条是同一类歧义，实测踩到过）。
    return {"ok": True,
            "message": f"已重置 {user.username} 的密码；该用户自己的全部设备"
                       f"已退出登录，其它用户不受影响"}


@router.delete("/users/{user_id}/sessions")
async def revoke_sessions(user_id: str, request: Request) -> dict:
    """踢掉某用户的**全部**登录状态（怀疑账号被盗时的处置动作）。"""
    admin_id = await require_admin(request)
    repo = get_auth_service()._repo  # noqa: SLF001
    revoked = await _to_thread(
        lambda: repo.revoke_user_sessions(user_id, "admin:kick_all"))
    await _to_thread(lambda: repo.revoke_user_remembers(user_id))
    await _to_thread(lambda: repo._record_review(  # noqa: SLF001
        user_id=user_id, action="admin:kick_all", from_status="", to_status="",
        reviewer_id=admin_id, note="管理员强制下线全部设备",
        ip=_client_ip(request)))
    logger.warning("管理员强制下线：admin=%s user=%s 会话=%s",
                   admin_id, user_id, revoked)
    return {"ok": True, "message": f"已吊销 {revoked} 个会话",
            "revoked_sessions": revoked}


@router.get("/reviews")
async def list_reviews(request: Request, user_id: str = "",
                       limit: int = 100) -> dict:
    """审批/管理流水（**倒序**）。四眼原则要求操作可归因到人。"""
    await require_admin(request)
    repo = get_auth_service()._repo  # noqa: SLF001
    rows = await _to_thread(lambda: repo.list_reviews(
        user_id, limit=min(max(1, limit), 500)))
    return {"reviews": [
        {"user_id": r.user_id, "action": r.action,
         "from_status": r.from_status, "to_status": r.to_status,
         "reviewer_id": r.reviewer_id, "note": r.note,
         "tier_code": r.tier_code, "valid_until": r.valid_until,
         # 字段名是 `at`（不是 `created_at`）：流水的时间戳列叫 `at`，
         # 对外统一成 `created_at` 以免前端要区分两套命名。
         "created_at": r.at}
        for r in rows]}


async def _to_thread(fn: Any) -> Any:
    """把同步仓储调用挪到线程（路由是 async，SQLite 调用会阻塞事件循环）。"""
    import asyncio

    return await asyncio.to_thread(fn)


__all__ = ["ASSIGNABLE_TIERS", "ADMIN_TIER", "require_admin", "router"]
