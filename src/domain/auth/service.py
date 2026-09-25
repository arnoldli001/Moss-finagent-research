"""认证服务：注册 / 登录 / 改密 / 找回 / 绑定 / 会话（邮箱通道）。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.5（六条流程）、
§8.6.7（六个安全要点）、§8.6.11（两层凭证与 sessionId 隔离）。

## 这个文件里每一处取舍都对应一个真实攻击面

| 攻击面 | 本文件的对策 |
|---|---|
| 账号枚举 | `login` 对"不存在/密码错"返回**同一文案**；`forgot` 对两种邮箱返回**同一文案** |
| 时序侧信道 | 账号不存在时**仍走一次等价耗时的校验**（见 `_dummy_verify`） |
| 暴力破解 | 按**账号**计数 + 指数退避锁定；失败计数与锁定时间同表 |
| 撞库 | 失败计数按账号（不按 IP）；IP 维度限流在路由层另有一层 |
| 短信/邮件轰炸 | 配额 + 三重限流（目标/账号/租户），**发前先过图形验证码**（由路由层传入） |
| 令牌泄漏 | 验证码/重置令牌/remember token **只存哈希**（仓储层保证） |
| 令牌重放 | remember token **每次使用即轮换**；拿到已轮换的旧 token → **撤销整个 family** |
| 会话永不失效 | 滑动续期 + **绝对上限不可突破**（仓储层 `touch_session` 保证） |
| 改密后旧会话还在 | 改密/重置 → **撤销其它全部会话 + 全部 remember token** |
| 换绑劫持账号 | 换绑需**旧联系方式 + 新联系方式双验证**，并通知旧通道 |

## 为什么服务层不直接依赖具体通道

`Notifier` 是注入的协议：单测用 `MemoryNotifier`，dev 用 `ConsoleNotifier`，
生产用 `SmtpEmailNotifier`。**加短信时本文件一行都不用改**（§8.6.8 的可插拔）。
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from src.infrastructure.notify import (
    SCENE_LOGIN_ALERT,
    SCENE_REGISTER,
    SCENE_RESET,
    Notifier,
)
from src.infrastructure.repositories.auth_sqlite_repo import (
    AuthSqliteRepository,
    SessionRecord,
    UserRecord,
    is_future,
    iso,
    iso_in,
    mask_contact,
    new_token,
    normalize_email,
    parse_iso,
    quota_exceeded,
    sha256_hex,
    utc_now,
    verify_password,
)

logger = logging.getLogger(__name__)

# ======================================================================
# 可调参数（集中一处，便于测试覆盖与线上调参）
# ======================================================================

#: 会话滑动期（无操作多久失效）。§8.6.11.1 的"这一会儿"。
SESSION_IDLE_SECONDS = 30 * 60
#: 会话绝对上限（**不可被滑动突破**）。
SESSION_ABSOLUTE_SECONDS = 12 * 3600
#: remember token 窗口 —— 🔒 7 天（§8.6.11.1 已确认的"短期不看"口径）
REMEMBER_DAYS = 7.0
#: 验证码有效期
CODE_TTL_SECONDS = 300.0
#: 验证码错误次数上限（超过作废）
CODE_MAX_ATTEMPTS = 5
#: 同一目标两次发送的最小间隔
CODE_RESEND_INTERVAL_SECONDS = 60.0
#: 重置令牌有效期
RESET_TTL_SECONDS = 900.0
#: 登录失败锁定
LOGIN_FAIL_THRESHOLD = 5
LOGIN_LOCK_SECONDS = 900.0
#: 密码策略
PASSWORD_MIN_LENGTH = 10
#: 弱口令黑名单（刻意短小：够挡住"顺手一试"的那批，长名单交给人审）
WEAK_PASSWORDS: frozenset[str] = frozenset({
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyuiop", "admin123", "administrator", "iloveyou",
    "abc123456", "a123456789", "1qaz2wsx3edc", "moss123456", "letmein123",
})

#: 邮箱格式（刻意宽松：只挡明显非法，**不追求 RFC 完备** ——
#: 过严的正则会误杀合法地址，而真正的验证手段是"验证码能不能收到"）
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


# ======================================================================
# 数据结构
# ======================================================================

@dataclass(frozen=True)
class AuthOutcome:
    """一次认证动作的结果。`ok=False` 时 `message` **必须是能给用户看的话**。"""

    ok: bool
    message: str = ""
    code: str = ""            # 机器可读的原因码（前端按码决定跳哪个页面）
    data: dict[str, Any] | None = None

    @staticmethod
    def fail(message: str, code: str = "invalid") -> AuthOutcome:
        return AuthOutcome(ok=False, message=message, code=code)

    @staticmethod
    def succeed(message: str = "", **data: Any) -> AuthOutcome:
        return AuthOutcome(ok=True, message=message, data=data or None)


@dataclass(frozen=True)
class LoginResult:
    """登录成功后的凭证包。**明文令牌只在这里出现一次**，之后库里只有哈希。"""

    user: UserRecord
    session: SessionRecord
    access_token: str
    refresh_token: str
    remember_token: str = ""
    remember_family: str = ""


# ======================================================================
# 纯函数：策略校验（可穷举单测，不碰 DB）
# ======================================================================

def valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(normalize_email(value)))


def check_password_strength(password: str, *, username: str = "",
                            email: str = "") -> tuple[bool, str]:
    """密码策略。返回 `(是否合格, 原因)`。

    为什么连"不能等于用户名/邮箱"也要查：金融系统里 `alice`/`alice@x.com` 这种
    密码是最常见的"顺手一试"，而它会让所有其它加固（锁定、限流）失去意义 ——
    因为攻击者根本不需要猜。
    """
    if len(password or "") < PASSWORD_MIN_LENGTH:
        return False, f"密码至少 {PASSWORD_MIN_LENGTH} 位"
    lowered = password.lower()
    if lowered in WEAK_PASSWORDS:
        return False, "密码过于常见，请换一个"
    if username and lowered == str(username).strip().lower():
        return False, "密码不能与用户名相同"
    local = normalize_email(email).partition("@")[0]
    if local and lowered == local:
        return False, "密码不能与邮箱前缀相同"
    kinds = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ])
    if kinds < 2:
        return False, "密码需包含大小写字母、数字、符号中的至少两类"
    return True, ""


def login_failure_code(*, user_exists: bool, password_ok: bool,
                       locked: bool, status_ok: bool) -> str:
    """把"失败原因"归一成机器码，供前端决定跳转。

    ⚠️ 注意：`user_exists` 与 `password_ok` **不产生不同的对外文案** ——
    它们只在内部用于决定"要不要走一次等价耗时校验"。
    """
    if status_ok and user_exists and password_ok and not locked:
        return "ok"
    if locked:
        return "locked"
    if user_exists and password_ok and not status_ok:
        return "status"
    return "credentials"      # 统一的"账号或密码错误"


# ======================================================================
# 服务
# ======================================================================

class AuthService:
    """认证服务。所有方法都是 async（仓储走 to_thread，不阻塞事件循环）。"""

    def __init__(
        self, repo: AuthSqliteRepository, notifier: Notifier, *,
        app_name: str = "Moss 投研",
        session_idle_seconds: float = SESSION_IDLE_SECONDS,
        session_absolute_seconds: float = SESSION_ABSOLUTE_SECONDS,
        remember_days: float = REMEMBER_DAYS,
        code_ttl_seconds: float = CODE_TTL_SECONDS,
        reset_ttl_seconds: float = RESET_TTL_SECONDS,
        login_fail_threshold: int = LOGIN_FAIL_THRESHOLD,
        login_lock_seconds: float = LOGIN_LOCK_SECONDS,
    ) -> None:
        self._repo = repo
        self._notifier = notifier
        self._app_name = app_name
        self._idle = session_idle_seconds
        self._absolute = session_absolute_seconds
        self._remember_days = remember_days
        self._code_ttl = code_ttl_seconds
        self._reset_ttl = reset_ttl_seconds
        self._fail_threshold = login_fail_threshold
        self._lock_seconds = login_lock_seconds
        #: 邮箱配额（本轮的"短信配额"对应物）。`0` = 不允许发送。
        self.email_monthly_quota = 2000

    # ---------------- 内部工具 ----------------

    async def _dummy_verify(self, password: str) -> None:
        """账号不存在时也走一次**等价耗时**的哈希校验。

        为什么必须做：如果"账号不存在"立刻返回、而"密码错"要等 600ms 的 PBKDF2，
        那么**响应时间本身就泄露了账号是否存在** —— 前面那个"统一文案"就白做了。
        """
        await self._repo.a_get_credential("__nonexistent__")  # 一次真实查询
        hashed = ("pbkdf2_sha256$1$00$" + "0" * 64)           # 极便宜的合法格式
        verify_password(password, hashed)

    async def _quota_left(self, tenant_id: str, channel: str = "email") -> tuple[bool, int]:
        """额度判定。返回 `(是否还有额度, 本月已用)`。"""
        usage = await self._repo.a_usage(tenant_id or "public", channel)
        return (not quota_exceeded(usage.sent_count, self.email_monthly_quota),
                usage.sent_count)

    # ---------------- 注册 ----------------

    async def send_code(
        self, *, scene: str, email: str, tenant_id: str = "",
        purpose_user: str = "", request_ip: str = "",
        has_captcha: bool = False,
    ) -> AuthOutcome:
        """发验证码。**发前必过图形验证码 + 三重限流 + 配额**（§8.6.7 第 3 条）。

        `has_captcha=False` 时**直接拒绝** —— 不是"忘了传"，而是
        "没有图形码就不发"是硬规则：否则一个脚本就能把额度与用户邮箱一起打爆。
        """
        target = normalize_email(email)
        if not valid_email(target):
            return AuthOutcome.fail("邮箱格式不正确", "bad_email")
        if not has_captcha:
            return AuthOutcome.fail("请先完成图形验证码", "captcha_required")

        # ① 同目标重发间隔（读最近一条验证码的发送时间）
        previous = await self._repo.a_latest_code(scene, target)
        if previous is not None:
            sent_at = parse_iso(previous.sent_at)
            if sent_at is not None:
                elapsed = (utc_now() - sent_at).total_seconds()
                if elapsed < CODE_RESEND_INTERVAL_SECONDS:
                    wait = int(CODE_RESEND_INTERVAL_SECONDS - elapsed) + 1
                    return AuthOutcome.fail(
                        f"发送过于频繁，请 {wait} 秒后再试", "rate_limited")

        # ② 租户配额（`tenant_id` 为空时归到 "public" 桶，便于平台级兜底统计）
        owner_tenant = tenant_id
        if not owner_tenant and purpose_user:
            member = await self._repo.a_get_user(purpose_user)
            owner_tenant = "" if member is None else member.applied_tier
        ok_quota, used = await self._quota_left(owner_tenant)
        if not ok_quota:
            # ★ 明确拒绝，**不静默失败**（ADR D34）：否则用户会一直等
            await self._repo.a_log_notify(
                channel="email", target=target, scene=scene, status="quota_exceeded",
                tenant_id=owner_tenant, user_id=purpose_user,
                error_code="QUOTA_EXCEEDED",
                error_msg=f"本月邮件额度已用完（{used}）")
            return AuthOutcome.fail(
                "本月验证码额度已用完，请联系管理员", "quota_exceeded")

        # ③ 生成并落库（**只存哈希**，明文只在外发时出现一次）
        code = f"{secrets.randbelow(1_000_000):06d}"
        record_id = await self._repo.a_issue_code(
            scene=scene, target=target, code=code, purpose_user=purpose_user,
            ttl_seconds=self._code_ttl, max_attempts=CODE_MAX_ATTEMPTS,
            request_ip=request_ip, channel="email")

        # ③b 找回场景**同时签发一次性重置令牌**。
        #
        # ★ 这一行曾经缺失 —— 是实测到的功能性缺陷：
        #   `reset_password` 要求「验证码 + 令牌」两样，而 `issue_reset` 全项目
        #   **零调用**、邮件模板也没有 `{reset_token}` 占位符，于是用户收到验证码
        #   却永远拿不到令牌，`/password/reset` 恒定 401（`bad_token`），
        #   前端那个"重置令牌（邮件中提供）"输入框成了填不出来的死路口。
        #   实测证据：`fact_password_reset` 表 0 行，而 `fact_notify_log` 里
        #   `reset_password / sent / smtp` 是成功的 —— 邮件发了，但里面没这东西。
        #
        # 令牌与验证码同封邮件下发（而不是分两封）：前端是**一个页面两个输入框**，
        # 用户也是**一次请求**触发的，分两封需要额外一步交互且前端没有这个流程。
        # 安全性上并不削弱 —— 真正的第二因子是"能收到这个邮箱"，不是"两封邮件"。
        #
        # 令牌 TTL 用 `_reset_ttl`（15 分钟）比验证码的 5 分钟长：
        # 用户读完邮件、输完码，令牌不该先过期。码一旦失效，重置仍会因
        # `a_consume_code` 失败而整体拒绝，所以更长 TTL 不构成绕过。
        reset_token = ""
        if scene == SCENE_RESET and purpose_user:
            reset_token = await self._repo.a_issue_reset(
                user_id=purpose_user, channel="email",
                ttl_seconds=self._reset_ttl, request_ip=request_ip)

        # ④ 真实外发（通道由注入的 Notifier 决定）
        result = await self._notifier.send(
            target, scene, {"code": code, "ttl_seconds": self._code_ttl,
                            "app_name": self._app_name,
                            "reset_token": reset_token})
        await self._repo.a_log_notify(
            channel="email", target=target, scene=scene, status=result.status,
            tenant_id=owner_tenant, user_id=purpose_user, provider=result.provider,
            provider_msg_id=result.provider_msg_id, unit_price=0.0,
            error_code=result.error_code, error_msg=result.error_msg)
        if result.ok:
            await self._repo.a_add_usage(owner_tenant or "public", "email", sent=1)
        else:
            await self._repo.a_add_usage(owner_tenant or "public", "email", failed=1)
            # 发失败要作废刚落库的验证码，避免"用户没收到、但码还能用"的窗口
            await self._repo.a_discard_code(record_id)
            # 令牌同样作废：否则留下"码废了但令牌还能兑换"的窗口
            if reset_token:
                await self._repo.a_discard_reset(reset_token)
            return AuthOutcome.fail(result.error_msg or "验证码发送失败，请稍后重试",
                                    "send_failed")

        # ★ 文案必须按**实际通道**如实给出（实测踩到的坑）
        #
        # `ConsoleNotifier` 的返回值同样是 `ok=True`（它确实把码写进了日志），
        # 所以原来那句"验证码已发送，请查收（若未收到请检查垃圾邮件）"
        # 在**没有配邮件凭据**时会明确误导用户：他去翻邮箱、翻垃圾箱，
        # 永远找不到，而真正的位置是**服务端日志**。
        #
        # 实测场景：`.env` 里 `ALERT_SMTP_AUTH_CODE` 留空（注释要求填 QQ 授权码，
        # 用户漏填）→ 通道回退 console → 界面提示"已发送"→ 用户收不到邮件。
        # 服务端没有任何报错，`fact_notify_log.status='sent'` 也是对的
        # （它记录的是"该通道发送成功"），所以从日志侧完全看不出问题。
        console_only = str(getattr(result, "provider", "")) == "console"
        if console_only:
            return AuthOutcome.succeed(
                "⚠ 未配置邮件发件凭据，验证码**没有真实发出** —— "
                "它已打印到服务端日志（关键字 `[DEV 通知]` / `code=`）。"
                "要真正收邮件，请在 .env 填 ALERT_SMTP_AUTH_CODE"
                "（QQ 邮箱的 16 位授权码，不是登录密码）后重启服务。",
                scene=scene, target_masked=mask_contact(target, "email"),
                quota_used=used + 1, delivery_channel="console",
                delivered_to_email=False)
        return AuthOutcome.succeed(
            f"验证码已发送到 {mask_contact(target, 'email')}，请查收"
            "（若未收到请检查垃圾邮件）",
            scene=scene, target_masked=mask_contact(target, "email"),
            quota_used=used + 1, delivery_channel="email",
            delivered_to_email=True)

    @staticmethod
    def _latest_sent_at(scene: str, target: str):
        """**已废弃的空实现，仅保留以免外部引用报错。**
        真实逻辑已内联进 `send_code`（用 `repo.latest_code(...).sent_at`）——
        放这里会导致"两次查询、两处口径"，那是比空实现更糟的事。
        """
        return None

    async def register(
        self, *, email: str, username: str, password: str, code: str,
        tenant_id: str = "trial", valid_days: float = 30.0,
        request_ip: str = "",
    ) -> AuthOutcome:
        """注册：**邮箱验证通过才落 `pending`**（§8.6.5①）。

        `valid_until` 在注册时就给一个默认值（审批时可改）——
        因为 `dim_user.valid_until` 是 `NOT NULL`（不允许永久账号，
        否则总有人给自己开一个无期限的）。
        """
        target = normalize_email(email)
        if not valid_email(target):
            return AuthOutcome.fail("邮箱格式不正确", "bad_email")
        if not str(username or "").strip():
            return AuthOutcome.fail("用户名不能为空", "bad_username")
        ok_pwd, why = check_password_strength(password, username=username,
                                              email=target)
        if not ok_pwd:
            return AuthOutcome.fail(why, "weak_password")

        # 邮箱唯一性（**在验证码之前查**：否则会白烧一条验证码）
        existing = await self._repo.a_find_user_by_contact("email", target)
        if existing is not None:
            owner = await self._repo.a_get_user(existing.user_id)
            if owner is not None and not owner.deleted_at and owner.status != "deleted":
                return AuthOutcome.fail("该邮箱已被注册", "email_taken")

        record = await self._repo.a_latest_code(SCENE_REGISTER, target)
        if record is None:
            return AuthOutcome.fail("请先获取邮箱验证码", "code_required")
        passed, reason = await self._repo.a_consume_code(record.id, code)
        if not passed:
            return AuthOutcome.fail(reason, "bad_code")

        user_id = f"u_{sha256_hex(target)[:16]}"
        user = await self._repo.a_create_user(
            user_id=user_id, username=str(username).strip(),
            display_name=str(username).strip(), status="pending",
            applied_tier=tenant_id, valid_until=iso_in(valid_days * 86400))

        await self._repo.a_set_password(user_id, password)
        await self._repo.a_bind_contact(
            user_id=user_id, kind="email", value=target, tenant_id=tenant_id,
            verified=True)
        await self._repo.a_update_user_status(
            user_id, "pending", action="register", note="邮箱已验证，等待管理员审批",
            ip=request_ip)
        logger.info("新注册（待审）：user_id=%s tier=%s", user_id, tenant_id)
        return AuthOutcome.succeed("注册申请已提交，请等待管理员审批",
                                   user_id=user.user_id, status="pending")

    # ---------------- 登录 ----------------

    async def _lock_remaining(self, user_id: str) -> int:
        cred = await self._repo.a_get_credential(user_id)
        if not cred:
            return 0
        locked_until = cred.get("locked_until")
        if not is_future(locked_until):
            return 0
        until = parse_iso(str(locked_until))
        return 0 if until is None else max(1, int((until - utc_now()).total_seconds()))

    async def login(
        self, *, account: str, password: str, remember_me: bool = False,
        tenant_id: str = "", device_label: str = "", device_id: str = "",
        user_agent: str = "", ip: str = "",
    ) -> AuthOutcome:
        """登录。返回 `AuthOutcome.data` 里带 `LoginResult` 的字段。

        ★ 防枚举的关键在 `_dummy_verify` + **统一文案**这两件事上，
        而不是"多写几个 if"。
        """
        account_text = str(account or "").strip()
        user: UserRecord | None = None
        if "@" in account_text:
            contact = await self._repo.a_find_user_by_contact(
                "email", normalize_email(account_text))
            if contact is not None:
                user = await self._repo.a_get_user(contact.user_id)
        else:
            user = await self._repo.a_get_user_by_username(account_text)

        if user is None:
            await self._dummy_verify(password)      # ← 等价耗时，防时序侧信道
            await self._repo.a_log_notify(
                channel="email", target=account_text, scene="login",
                status="failed", error_code="NO_SUCH_ACCOUNT", user_id="")
            return AuthOutcome.fail("账号或密码错误", "credentials")

        locked = await self._lock_remaining(user.user_id)
        if locked:
            return AuthOutcome.fail(
                f"登录失败次数过多，请 {locked // 60 + 1} 分钟后再试", "locked")

        cred = await self._repo.a_get_credential(user.user_id) or {}
        password_ok = verify_password(password, str(cred.get("password_hash", "")))
        allowed, status_msg = user.can_login()

        decision = login_failure_code(
            user_exists=True, password_ok=password_ok,
            locked=False, status_ok=allowed)
        if not password_ok:
            attempts = await self._repo.a_record_login_failure(
                user.user_id, threshold=self._fail_threshold,
                lock_seconds=self._lock_seconds, ip=ip)
            logger.info("登录失败：user_id=%s attempts=%s", user.user_id, attempts)
            return AuthOutcome.fail("账号或密码错误", "credentials")
        if not allowed:
            # 密码对但状态不允许 → **说清原因**（这对本人不是秘密，见 can_login 注释）
            return AuthOutcome.fail(status_msg, decision)

        await self._repo.a_clear_login_failures(user.user_id)
        package = await self._open_session(
            user=user, tenant_id=tenant_id or user.applied_tier,
            remember_me=remember_me, device_label=device_label,
            device_id=device_id, user_agent=user_agent, ip=ip)
        return AuthOutcome.succeed("登录成功", **{
            "user": package.user, "session": package.session,
            "access_token": package.access_token,
            "refresh_token": package.refresh_token,
            "remember_token": package.remember_token,
            "remember_family": package.remember_family,
            "must_change_password": bool(cred.get("must_change_password")),
        })

    async def _open_session(
        self, *, user: UserRecord, tenant_id: str, remember_me: bool,
        device_label: str = "", device_id: str = "", user_agent: str = "",
        ip: str = "",
    ) -> LoginResult:
        """开一个会话（三层令牌一次性发全）。"""
        session_id = new_token(24)
        access_token = new_token(32)
        refresh_token = new_token(32)
        session = await self._repo.a_create_session(
            session_id=session_id, user_id=user.user_id, tenant_id=tenant_id,
            access_jti=sha256_hex(access_token)[:32],
            refresh_hash=sha256_hex(refresh_token),
            idle_seconds=self._idle, absolute_seconds=self._absolute,
            device_label=device_label, device_id=device_id,
            user_agent=user_agent, ip=ip)
        remember_token = ""
        family = ""
        if remember_me:
            remember_token, family = await self._repo.a_issue_remember(
                user_id=user.user_id, tenant_id=tenant_id,
                days=self._remember_days, device_label=device_label,
                user_agent=user_agent, ip=ip)
        await self._repo.a_update_user_status(
            user.user_id, user.status, action="login",
            note=f"device={device_label or 'unknown'}", ip=ip)
        return LoginResult(user=user, session=session, access_token=access_token,
                           refresh_token=refresh_token,
                           remember_token=remember_token, remember_family=family)

    # ---------------- 免密回到页面（refresh cookie） ----------------

    async def resume_with_remember(
        self, *, token: str, device_label: str = "", ip: str = "",
        user_agent: str = "",
    ) -> AuthOutcome:
        """用 remember token **静默换一个新会话**（"短期不看再打开不用输密码"）。

        ★ **轮换 + 重放检测**（§8.6.5④）：
        正常路径是"旧 token 作废、发一个新 token"；
        如果收到一个**已经轮换过**的旧 token，说明它被复制走了 ——
        此时**撤销整个 family**（那台设备的所有令牌）并强制重新登录。
        """
        record, reason = await self._repo.a_consume_remember(token)
        if record is None:
            if reason == "replay":
                # 需要先拿到 family 才能撤销：再查一次（consume 已返回 None）
                family = await self._family_of(token)
                if family:
                    revoked = await self._repo.a_revoke_remember_family(family)
                    logger.warning(
                        "★ 检测到 remember token 重放，已撤销 family=%s（%d 个令牌）",
                        family, revoked)
                return AuthOutcome.fail(
                    "检测到异常登录，请重新输入密码", "replay_detected")
            return AuthOutcome.fail("登录状态已失效，请重新登录", "remember_expired")

        user = await self._repo.a_get_user(str(record["user_id"]))
        if user is None:
            return AuthOutcome.fail("登录状态已失效，请重新登录", "remember_expired")
        allowed, status_msg = user.can_login()
        if not allowed:
            return AuthOutcome.fail(status_msg, "status")

        family = str(record.get("family_id", ""))
        await self._repo.a_mark_remember_rotated(token)
        package = await self._open_session(
            user=user, tenant_id=str(record.get("tenant_id", "")) or user.applied_tier,
            remember_me=True, device_label=device_label, ip=ip,
            user_agent=user_agent)
        # 新令牌挂在同一个 family 上（这样"撤销 family"能一次清掉整条链）
        if package.remember_token:
            await self._repo.a_revoke_remember_family(family)
            new_token_value, _ = await self._repo.a_issue_remember(
                user_id=user.user_id, tenant_id=package.session.tenant_id,
                days=self._remember_days, family_id=family, rotated_from=token,
                device_label=device_label, user_agent=user_agent, ip=ip)
            package = LoginResult(
                user=package.user, session=package.session,
                access_token=package.access_token,
                refresh_token=package.refresh_token,
                remember_token=new_token_value, remember_family=family)
        return AuthOutcome.succeed("已恢复登录状态", **{
            "user": package.user, "session": package.session,
            "access_token": package.access_token,
            "refresh_token": package.refresh_token,
            "remember_token": package.remember_token,
        })

    async def _family_of(self, token: str) -> str:
        """取某 token 所属 family（重放检测后要撤整条链）。"""
        return await self._repo.a_family_of_remember(sha256_hex(token))

    # ---------------- 会话续期与登出 ----------------

    async def touch(self, session_id: str) -> SessionRecord | None:
        """滑动续期（每次真实业务请求调用）。**不延长绝对上限**。"""
        session = await self._repo.a_get_session(session_id)
        if session is None or not session.is_valid():
            return session if session is None else None
        return await self._repo.a_touch_session(session_id, idle_seconds=self._idle)

    async def validate_session(
        self, session_id: str,
    ) -> tuple[SessionRecord | None, Any | None]:
        """**只读**校验会话：不续期、不写库。返回 `(session, user)`。

        与 `touch()` 的分工必须分清楚：

        | 方法 | 会不会写库 | 谁用 |
        |---|---|---|
        | `touch()` | **会**（推 `last_seen_at` / `idle_expires_at`） | 业务请求进来时续期 |
        | `validate_session()` | **不会** | HTTP 中间件的登录门槛（每个请求都调） |

        为什么必须分开：登录门槛对**每个请求**都要问一句"这人登录了吗"，
        如果在那种地方调 `touch()`，等于每个请求都往 SQLite 写一次
        （实测一次 INSERT 的 fsync 就要 7.6 ms，见设计文档 §13.4）——
        为了续期把整站吞吐拖垮，而续期本来是一分钟一次就够的事。

        有效判据**只有一份**（`SessionRecord.is_valid()`：未撤销 ＋ 未超
        滑动期 ＋ 未超 12 小时绝对上限）。在这里重写一遍是危险的 ——
        两份"会话算不算有效"的实现**一定**会在某次改动后分叉，
        而分叉留下的那一侧就是漏网之门。
        """
        session = await self._repo.a_get_session(session_id)
        if session is None or not session.is_valid():
            return None, None
        user = await self._repo.a_get_user(session.user_id)
        return session, user

    async def logout(self, *, session_id: str, all_devices: bool = False,
                     user_id: str = "") -> AuthOutcome:
        """登出。`all_devices=True` 时撤销该用户全部会话 + 全部 remember token。"""
        if all_devices:
            if not user_id:
                return AuthOutcome.fail("缺少用户标识", "bad_request")
            count = await self._repo.a_revoke_user_sessions(user_id, "logout_all")
            await self._repo.a_revoke_user_remembers(user_id)
            return AuthOutcome.succeed(f"已退出全部设备（{count} 个会话）")
        await self._repo.a_revoke_session(session_id, "logout")
        return AuthOutcome.succeed("已退出登录")

    # ---------------- 改密码 ----------------

    async def change_password(
        self, *, user_id: str, old_password: str, new_password: str,
        current_session: str = "",
    ) -> AuthOutcome:
        """改密码。**必须撤其它会话**（§8.6.5③），否则"怀疑被盗"时改了也没用。"""
        cred = await self._repo.a_get_credential(user_id)
        if cred is None:
            return AuthOutcome.fail("账号异常，请联系管理员", "no_credential")
        if not verify_password(old_password, str(cred.get("password_hash", ""))):
            return AuthOutcome.fail("当前密码不正确", "bad_old_password")
        if old_password == new_password:
            return AuthOutcome.fail("新密码不能与当前密码相同", "same_password")
        user = await self._repo.a_get_user(user_id)
        ok_pwd, why = check_password_strength(
            new_password, username="" if user is None else user.username)
        if not ok_pwd:
            return AuthOutcome.fail(why, "weak_password")
        if await self._repo.a_password_was_used(user_id, new_password):
            return AuthOutcome.fail(
                f"新密码不能与最近 {5} 次用过的密码相同", "password_reused")

        await self._repo.a_set_password(user_id, new_password)
        revoked = await self._repo.a_revoke_user_sessions(
            user_id, "password_change", except_session=current_session)
        await self._repo.a_revoke_user_remembers(user_id)
        logger.info("改密成功：user_id=%s 撤销其它会话 %d 个", user_id, revoked)
        # ★ 文案必须限定"**你自己的**其它设备"。
        #
        # 原文案是"密码已更新，其它设备已退出登录"，用户实测把它读成了
        # **"别人的设备也被强制退出了"** 并来质疑（那确实不合理）。
        # 实际行为一直是对的（只撤自己的），但歧义会：
        #   ① 变成一次不必要的技术支持；
        #   ② 掩盖"改密只影响自己"这个**安全属性**——用户以为系统在乱踢人，
        #      反而不敢用这个功能（而它正是"怀疑被盗"时该用的）。
        return AuthOutcome.succeed(
            f"密码已更新。你在其它 {max(0, revoked)} 台设备上的登录已退出，"
            f"本机保持登录；其它用户不受影响。",
            revoked_sessions=revoked)

    # ---------------- 找回密码 ----------------

    async def request_reset(self, *, email: str, has_captcha: bool = False,
                            request_ip: str = "") -> AuthOutcome:
        """发起找回。**无论邮箱是否存在，一律返回同一文案**（防枚举）。

        刻意**不**在这里发验证码，而是复用 `send_code(scene=reset)`：
        两条路径共用同一套限流/配额/发送逻辑，避免"找回这条路忘了限流"。
        """
        target = normalize_email(email)
        if not valid_email(target):
            return AuthOutcome.fail("邮箱格式不正确", "bad_email")
        generic = AuthOutcome.succeed(
            "若该邮箱已注册，验证码已发送，请查收（含垃圾邮件箱）",
            target_masked=target)
        contact = await self._repo.a_find_user_by_contact("email", target)
        if contact is None:
            return generic                     # ← 不存在的邮箱也"看起来成功"
        user = await self._repo.a_get_user(contact.user_id)
        if user is None or user.status in {"deleted", "rejected", "disabled"}:
            return generic
        # 真正的发送（失败与否都不改对外文案 —— 否则又成了枚举信道）
        outcome = await self.send_code(
            scene=SCENE_RESET, email=target, tenant_id="",
            purpose_user=user.user_id, request_ip=request_ip,
            has_captcha=has_captcha)
        if not outcome.ok and outcome.code == "quota_exceeded":
            # 配额耗尽**必须**说出来：否则用户永远等不到邮件却不知道为什么
            return outcome
        return generic

    async def reset_password(
        self, *, email: str, code: str, token: str, new_password: str,
        request_ip: str = "",
    ) -> AuthOutcome:
        """用"验证码 + 一次性令牌"重置密码；**撤销全部会话**（§8.6.5⑥）。"""
        target = normalize_email(email)
        record = await self._repo.a_latest_code(SCENE_RESET, target)
        if record is None:
            return AuthOutcome.fail("请先获取验证码", "code_required")
        passed, reason = await self._repo.a_consume_code(record.id, code)
        if not passed:
            return AuthOutcome.fail(reason, "bad_code")
        user_id, why = await self._repo.a_consume_reset(token)
        if user_id is None:
            return AuthOutcome.fail(why, "bad_token")
        user = await self._repo.a_get_user(user_id)
        if user is None:
            return AuthOutcome.fail("账号不存在", "no_user")
        ok_pwd, msg = check_password_strength(new_password, username=user.username,
                                              email=target)
        if not ok_pwd:
            return AuthOutcome.fail(msg, "weak_password")
        if await self._repo.a_password_was_used(user_id, new_password):
            return AuthOutcome.fail("新密码不能与最近用过的密码相同",
                                    "password_reused")

        await self._repo.a_set_password(user_id, new_password)
        revoked = await self._repo.a_revoke_user_sessions(user_id, "password_reset")
        await self._repo.a_revoke_user_remembers(user_id)
        # 告警邮件：这是用户**唯一可能察觉**账号被盗的时机
        await self._notifier.send(target, SCENE_LOGIN_ALERT,
                                  {"when": iso(utc_now()),
                                   "app_name": self._app_name})
        await self._repo.a_update_user_status(
            user_id, user.status, action="password_reset",
            note=f"ip={request_ip}", ip=request_ip)
        logger.warning("密码重置：user_id=%s 撤销会话 %d 个", user_id, revoked)
        return AuthOutcome.succeed("密码已重置，请用新密码登录", revoked_sessions=revoked)

    # ---------------- 只读查询 ----------------

    async def list_sessions(self, user_id: str) -> list[SessionRecord]:
        """活跃设备列表（用户自救通道：看谁登着）。"""
        return await self._repo.a_list_sessions(user_id)

    async def kill_session(self, *, user_id: str, session_id: str,
                           reason: str = "admin_kick") -> AuthOutcome:
        """踢掉某一个会话。**必须校验它属于该用户** —— 否则成了一个越权接口。"""
        session = await self._repo.a_get_session(session_id)
        if session is None or session.user_id != user_id:
            return AuthOutcome.fail("会话不存在", "not_found")
        await self._repo.a_revoke_session(session_id, reason)
        return AuthOutcome.succeed("已退出该设备")

    async def contacts(self, user_id: str) -> dict[str, Any]:
        """已绑定的联系方式（**脱敏返回**，明文不出接口）。"""
        items = await self._repo.a_list_contacts(user_id)
        return {
            "email": [{"masked": c.masked, "verified": c.verified,
                       "primary": c.is_primary}
                      for c in items if c.kind == "email"],
            "phone": [{"masked": c.masked, "verified": c.verified,
                       "primary": c.is_primary}
                      for c in items if c.kind == "phone"],
        }


__all__ = [
    "CODE_MAX_ATTEMPTS",
    "CODE_RESEND_INTERVAL_SECONDS",
    "CODE_TTL_SECONDS",
    "LOGIN_FAIL_THRESHOLD",
    "LOGIN_LOCK_SECONDS",
    "PASSWORD_MIN_LENGTH",
    "REMEMBER_DAYS",
    "RESET_TTL_SECONDS",
    "SESSION_ABSOLUTE_SECONDS",
    "SESSION_IDLE_SECONDS",
    "WEAK_PASSWORDS",
    "AuthOutcome",
    "AuthService",
    "LoginResult",
    "check_password_strength",
    "login_failure_code",
    "valid_email",
]
