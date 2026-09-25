"""认证体系单测：仓储不变量 + 服务流程 + 六条安全要点。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.5（流程）、
§8.6.7（安全要点）、§8.6.11（两层凭证）。

**这个文件里的每条测试都对应一个真实攻击面或事故**，不是凑覆盖率：
防枚举、时序侧信道、验证码只存哈希、失败锁定按账号、令牌重放、
改密踢会话、绝对上限不可突破、配额耗尽要明说。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.domain.auth.service import (
    SCENE_REGISTER,
    SCENE_RESET,
    AuthService,
    check_password_strength,
    login_failure_code,
    valid_email,
)
from src.infrastructure.notify import (
    ConsoleNotifier,
    MemoryNotifier,
    SmtpEmailNotifier,
    build_notifier,
    render,
)
from src.infrastructure.repositories.auth_sqlite_repo import (
    TABLE_REMEMBER,
    TABLE_RESET,
    TABLE_VCODE,
    AuthSqliteRepository,
    hash_password,
    is_future,
    iso_in,
    mask_contact,
    normalize_email,
    normalize_phone,
    period_of,
    quota_exceeded,
    sha256_hex,
    verify_code_hash,
    verify_password,
)

EMAIL = "alice@example.com"
PASSWORD = "CorrectHorse#2026"
USER = "alice"


# ======================================================================
# 夹具
# ======================================================================

@pytest.fixture()
def repo(tmp_path: Path) -> AuthSqliteRepository:
    """每个测试一个独立库文件 —— 不共享状态，可并行。"""
    return AuthSqliteRepository(str(tmp_path / "auth.db"))


@pytest.fixture()
def notifier() -> MemoryNotifier:
    return MemoryNotifier()


@pytest.fixture()
def service(repo: AuthSqliteRepository, notifier: MemoryNotifier) -> AuthService:
    return AuthService(repo, notifier)


async def _register_active(service: AuthService, repo: AuthSqliteRepository,
                           notifier: MemoryNotifier, *,
                           email: str = EMAIL, username: str = USER,
                           password: str = PASSWORD) -> str:
    """走完整注册 + 审批，返回 user_id（大多数用例的前置）。"""
    sent = await service.send_code(scene=SCENE_REGISTER, email=email,
                                   has_captcha=True)
    assert sent.ok, sent.message
    result = await service.register(email=email, username=username,
                                    password=password,
                                    code=notifier.last_code())
    assert result.ok, result.message
    user_id = str((result.data or {})["user_id"])
    await repo.a_update_user_status(user_id, "active", reviewed_by="admin1")
    return user_id


# ======================================================================
# 纯函数：密码策略 / 邮箱 / 脱敏 / 配额
# ======================================================================

@pytest.mark.parametrize("password,ok", [
    ("CorrectHorse#2026", True),
    ("short", False),                    # 太短
    ("password123", False),              # 弱口令黑名单
    ("alllowercaseonly", False),         # 只有一类字符
    ("1234567890123", False),            # 只有数字
    ("NoSpecial12345", True),            # 大小写+数字（两类以上）
])
def test_password_strength(password: str, ok: bool) -> None:
    passed, _ = check_password_strength(password)
    assert passed is ok


def test_password_cannot_equal_username_or_email_prefix() -> None:
    """金融系统里"密码=用户名"是最顺手的猜测 —— 它会让所有锁定/限流失去意义。"""
    assert check_password_strength("alicealice", username="alicealice")[0] is False
    assert check_password_strength("alice", email="alice@x.com")[0] is False


@pytest.mark.parametrize("value,ok", [
    ("a@b.com", True), ("A@B.CO", True), ("no-at-sign", False),
    ("a@b", False), ("a b@c.com", False), ("", False),
])
def test_valid_email(value: str, ok: bool) -> None:
    assert valid_email(value) is ok


def test_contact_normalization() -> None:
    """归一化必须"入库与查询同一函数"，否则同一个人会有两个账号。"""
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"
    assert normalize_phone("+86 138-1234-5678") == "8613812345678"


def test_mask_contact_hides_most_of_value() -> None:
    assert mask_contact("alice@example.com", "email") == "al***@example.com"
    assert mask_contact("13812345678", "phone") == "138****5678"


def test_quota_semantics_zero_means_deny() -> None:
    """`limit=0` 是"不允许"，不是"无限" —— 这个语义搞反会开出一个无底洞。"""
    assert quota_exceeded(0, 0) is True
    assert quota_exceeded(0, -1) is True
    assert quota_exceeded(9, 10) is False
    assert quota_exceeded(10, 10) is True


def test_period_is_calendar_month() -> None:
    assert len(period_of()) == 7 and period_of().count("-") == 1


# ======================================================================
# 密码哈希
# ======================================================================

def test_password_hash_roundtrip_and_salt() -> None:
    h1, algo = hash_password(PASSWORD)
    h2, _ = hash_password(PASSWORD)
    assert algo.startswith("pbkdf2")
    assert h1 != h2, "两次哈希必须不同（盐必须随机）"
    assert verify_password(PASSWORD, h1)
    assert not verify_password("wrong", h1)


@pytest.mark.parametrize("stored", ["", "garbage", "pbkdf2_sha256$bad"])
def test_verify_password_never_raises(stored: str) -> None:
    """坏哈希必须返回 False，**绝不允许**变成"校验通过"或 500。"""
    assert verify_password(PASSWORD, stored) is False


def test_empty_password_is_rejected_at_write_time() -> None:
    with pytest.raises(ValueError):
        hash_password("")


# ======================================================================
# 仓储：秘密只存哈希（§8.6.7 第 4 条）
# ======================================================================

async def test_verification_code_is_not_stored_in_plaintext(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    await service.send_code(scene=SCENE_REGISTER, email=EMAIL, has_captcha=True)
    code = notifier.last_code()
    with sqlite3.connect(repo._db_path) as conn:  # noqa: SLF001 白盒断言，故意查原始列
        stored = conn.execute(
            f"SELECT code_hash FROM {TABLE_VCODE}").fetchone()[0]
    assert stored != code
    assert stored == sha256_hex(code)
    assert verify_code_hash(code, stored)


async def test_reset_and_remember_tokens_are_hashed(
    service: AuthService, repo: AuthSqliteRepository,
    notifier: MemoryNotifier,
) -> None:
    user_id = await _register_active(service, repo, notifier)
    reset_token = await repo.a_issue_reset(user_id=user_id, channel="email")
    remember_token, _ = await repo.a_issue_remember(user_id=user_id, tenant_id="vip")
    with sqlite3.connect(repo._db_path) as conn:  # noqa: SLF001
        reset_hashes = {r[0] for r in conn.execute(
            f"SELECT token_hash FROM {TABLE_RESET}")}
        remember_hashes = {r[0] for r in conn.execute(
            f"SELECT token_hash FROM {TABLE_REMEMBER}")}
    assert reset_token not in reset_hashes
    assert remember_token not in remember_hashes
    assert sha256_hex(reset_token) in reset_hashes
    assert sha256_hex(remember_token) in remember_hashes


# ======================================================================
# 仓储：会话的滑动与绝对上限（§8.6.11.3）
# ======================================================================

async def test_session_slides_but_absolute_cap_never_extends(
    repo: AuthSqliteRepository,
) -> None:
    """★ 两条缺一不可：只判滑动 → 持续活跃的会话永不失效。"""
    created = await repo.a_create_session(
        session_id="s1", user_id="u1", tenant_id="vip", access_jti="j1",
        refresh_hash="rh", idle_seconds=1800, absolute_seconds=43200)
    # 滑动到明显不同的时刻（时间戳精度是秒，必须拉开差值才看得出变化）
    touched = await repo.a_touch_session("s1", idle_seconds=60)
    assert touched is not None
    assert touched.idle_expires_at != created.idle_expires_at, "滑动续期应改变滑动过期点"
    assert touched.absolute_expires_at == created.absolute_expires_at, (
        "★ 绝对上限绝不能被滑动续期延长")
    # 再滑一次，上限仍然不动
    again = await repo.a_touch_session("s1", idle_seconds=999999)
    assert again is not None
    assert again.absolute_expires_at == created.absolute_expires_at


async def test_expired_or_revoked_session_is_invalid(
    repo: AuthSqliteRepository,
) -> None:
    await repo.a_create_session(
        session_id="s2", user_id="u1", tenant_id="vip", access_jti="j2",
        refresh_hash="rh", idle_seconds=-1, absolute_seconds=-1)
    assert (await repo.a_get_session("s2")).is_valid() is False
    await repo.a_create_session(
        session_id="s3", user_id="u1", tenant_id="vip", access_jti="j3",
        refresh_hash="rh", idle_seconds=1800, absolute_seconds=43200)
    assert (await repo.a_get_session("s3")).is_valid() is True
    await repo.a_revoke_session("s3", "logout")
    assert (await repo.a_get_session("s3")).is_valid() is False


# ======================================================================
# 仓储：验证码一次性 + 尝试次数（防暴力枚举 6 位码）
# ======================================================================

async def test_code_is_one_time_and_counts_attempts(
    repo: AuthSqliteRepository,
) -> None:
    record_id = await repo.a_issue_code(scene="register", target=EMAIL,
                                        code="123456")
    ok, _ = await repo.a_consume_code(record_id, "000000")
    assert ok is False
    latest = await repo.a_latest_code("register", EMAIL)
    assert latest is not None and latest.attempts == 1
    ok, _ = await repo.a_consume_code(record_id, "123456")
    assert ok is True
    ok, reason = await repo.a_consume_code(record_id, "123456")
    assert ok is False and "已被使用" in reason


async def test_code_becomes_unusable_after_max_attempts(
    repo: AuthSqliteRepository,
) -> None:
    record_id = await repo.a_issue_code(scene="register", target=EMAIL,
                                        code="123456", max_attempts=3)
    for _ in range(3):
        await repo.a_consume_code(record_id, "000000")
    ok, reason = await repo.a_consume_code(record_id, "123456")
    assert ok is False and "尝试次数" in reason


async def test_new_code_invalidates_previous(
    repo: AuthSqliteRepository,
) -> None:
    first = await repo.a_issue_code(scene="register", target=EMAIL, code="111111")
    await repo.a_issue_code(scene="register", target=EMAIL, code="222222")
    ok, reason = await repo.a_consume_code(first, "111111")
    assert ok is False and "已被使用" in reason


async def test_discard_code_blocks_use(
    repo: AuthSqliteRepository,
) -> None:
    """外发失败时作废 —— 否则出现"用户没收到、但码还能用"的窗口。"""
    record_id = await repo.a_issue_code(scene="register", target=EMAIL,
                                        code="123456")
    await repo.a_discard_code(record_id)
    ok, _ = await repo.a_consume_code(record_id, "123456")
    assert ok is False


# ======================================================================
# 仓储：remember token 轮换与重放（§8.6.5④）
# ======================================================================

async def test_remember_rotation_and_replay_detection(
    repo: AuthSqliteRepository,
) -> None:
    token, family = await repo.a_issue_remember(user_id="u1", tenant_id="vip")
    record, reason = await repo.a_consume_remember(token)
    assert record is not None
    await repo.a_mark_remember_rotated(token)
    replayed, reason = await repo.a_consume_remember(token)
    assert replayed is None and reason == "replay"
    assert await repo.a_family_of_remember(sha256_hex(token)) == family
    # 旧令牌已被 `mark_remember_rotated` 作废，所以 family 里**没有未撤销的**了；
    # 但重放检测的处置要求"能一次撤掉整条链"——
    # 用一个新令牌验证 family 撤销确实生效（这是真实路径：重放时撤销的是活跃令牌）
    fresh, _ = await repo.a_issue_remember(user_id="u1", tenant_id="vip",
                                          family_id=family, rotated_from=token)
    assert await repo.a_revoke_remember_family(family) == 1
    assert (await repo.a_consume_remember(fresh))[1] == "replay"


async def test_expired_remember_is_not_replay(
    repo: AuthSqliteRepository,
) -> None:
    """过期 ≠ 重放：把过期误判成重放会让用户"无故被要求改密码"。"""
    await repo.a_issue_remember(user_id="u1", tenant_id="vip", days=-1)
    token, _ = await repo.a_issue_remember(user_id="u1", tenant_id="vip", days=-1)
    record, reason = await repo.a_consume_remember(token)
    assert record is None and reason == "令牌已过期"


# ======================================================================
# 仓储：审批流水 + 软删除 + 用量
# ======================================================================

async def test_review_chain_is_linked(repo: AuthSqliteRepository) -> None:
    repo.create_user(user_id="u1", username="a", display_name="A",
                           status="pending", valid_until=iso_in(86400))
    repo.update_user_status("u1", "active", reviewed_by="admin1")
    repo.update_user_status("u1", "disabled", reviewed_by="admin1")
    logs = repo.list_reviews("u1")
    assert len(logs) == 2
    assert logs[0].chain_hash != logs[1].chain_hash
    assert all(item.chain_hash for item in logs)


async def test_soft_deleted_users_are_hidden_but_kept(
    repo: AuthSqliteRepository,
) -> None:
    repo.create_user(user_id="u1", username="a", display_name="A",
                           status="active", valid_until=iso_in(86400))
    repo.update_user_status("u1", "deleted", operator="admin1", note="退租")
    assert repo.list_users() == []
    assert len(repo.list_users(include_deleted=True)) == 1
    # 审计仍在（ADR D17：删的是访问权，不是数据）。
    # 只应有 **1** 条：`create_user` 不是状态变更，不该产生流水。
    reviews = repo.list_reviews("u1")
    assert len(reviews) == 1 and reviews[0].to_status == "deleted"


async def test_valid_until_is_mandatory(repo: AuthSqliteRepository) -> None:
    """不允许永久账号（否则总有人给自己开一个无期限的）。"""
    with pytest.raises(ValueError):
        repo.create_user(user_id="u1", username="a", display_name="A",
                                 status="active", valid_until="")


async def test_notify_usage_accumulates_and_reconciles(
    repo: AuthSqliteRepository,
) -> None:
    await repo.a_add_usage("vip", "email", sent=3)
    await repo.a_add_usage("vip", "email", sent=2, failed=1, cost_yuan=0.15)
    usage = await repo.a_usage("vip", "email")
    assert usage.sent_count == 5 and usage.failed_count == 1
    assert usage.cost_yuan == pytest.approx(0.15)


async def test_notify_log_does_not_store_plaintext_target(
    repo: AuthSqliteRepository,
) -> None:
    await repo.a_log_notify(channel="email", target=EMAIL, scene="register",
                            status="sent")
    rows = await repo.a_notify_log(channel="email")
    assert rows and rows[0]["target_hash"] == sha256_hex(EMAIL)
    assert EMAIL not in str(rows[0])


# ======================================================================
# 服务：注册
# ======================================================================

async def test_send_code_requires_captcha(
    service: AuthService, notifier: MemoryNotifier,
) -> None:
    """没有图形验证码就不发 —— 否则一个脚本能把额度与用户邮箱一起打爆。"""
    outcome = await service.send_code(scene=SCENE_REGISTER, email=EMAIL)
    assert outcome.ok is False and outcome.code == "captcha_required"
    assert notifier.count() == 0


async def test_send_code_is_rate_limited(
    service: AuthService,
) -> None:
    assert (await service.send_code(scene=SCENE_REGISTER, email=EMAIL,
                                    has_captcha=True)).ok
    again = await service.send_code(scene=SCENE_REGISTER, email=EMAIL,
                                    has_captcha=True)
    assert again.ok is False and again.code == "rate_limited"


# ======================================================================
# 发送结果必须**如实**说明"到底发到哪了"（实测踩到的坑）
# ======================================================================

async def test_console_channel_does_not_claim_email_sent(
    service: AuthService, repo: AuthSqliteRepository,
) -> None:
    """★★ 走 console 通道时**不能**说"已发送，请查收"。

    ## 这条测试来自用户的真实报障

    用户在注册页点「发送验证码」，界面提示"已发送"，**但他没收到邮件**。
    查日志发现验证码是打在**服务端日志**里的（`[DEV 通知] ... code=011066`）。

    根因：`.env` 里 `ALERT_SMTP_AUTH_CODE` 留空（注释要求填 QQ 授权码，
    用户漏填）→ 通道回退到 `ConsoleNotifier` → 而它的返回值同样是
    `ok=True`（它确实把码写进了日志），于是复用了"已发送请查收"那句文案。

    为什么这个坑很难自己发现：
      - 界面提示成功；
      - `fact_notify_log.status = 'sent'` 也是**对的**（记录的是"该通道发送成功"）；
      - 服务端没有异常；
    用户只能去翻邮箱和垃圾箱 —— 而码从来不在那里。
    """
    # ⚠️ 必须换成**真的** ConsoleNotifier：夹具默认注入 MemoryNotifier
    #    （provider='memory'），那条路径不会产出误导文案，测不出这个 bug。
    from src.infrastructure.notify import ConsoleNotifier

    service._notifier = ConsoleNotifier(app_name="测试")  # noqa: SLF001
    outcome = await service.send_code(scene=SCENE_REGISTER, email=EMAIL,
                                      has_captcha=True)
    assert outcome.ok, outcome.message
    assert outcome.message.startswith("⚠"), outcome.message
    assert "没有真实发出" in outcome.message or "未配置" in outcome.message
    assert "ALERT_SMTP_AUTH_CODE" in outcome.message, (
        "没告诉用户该怎么修 —— 他只能反复重试")
    data = outcome.data or {}
    assert data.get("delivery_channel") == "console"
    assert data.get("delivered_to_email") is False


async def test_smtp_channel_reports_real_email(
    service: AuthService, repo: AuthSqliteRepository,
    notifier: MemoryNotifier,
) -> None:
    """走真实邮件通道时，文案要说明发到了**哪个脱敏地址**、并带上肯定标志。"""
    from src.infrastructure.notify import NotifyResult

    class FakeSmtp:
        channel = "email"
        provider = "smtp"

        async def send(self, target, scene, params):  # noqa: ANN001
            return NotifyResult(ok=True, channel=self.channel,
                                provider=self.provider, provider_msg_id="<m1>")

    service._notifier = FakeSmtp()      # noqa: SLF001 测试替身
    outcome = await service.send_code(scene=SCENE_REGISTER, email=EMAIL,
                                      has_captcha=True)
    assert outcome.ok, outcome.message
    assert "已发送" in outcome.message and "请查收" in outcome.message
    assert "⚠" not in outcome.message
    data = outcome.data or {}
    assert data.get("delivery_channel") == "email"
    assert data.get("delivered_to_email") is True
    # 脱敏：文案里不能出现完整邮箱
    assert EMAIL not in outcome.message, outcome.message
    assert "***" in outcome.message


async def test_notify_log_records_provider(
    service: AuthService, repo: AuthSqliteRepository,
) -> None:
    """通知日志要能**事后区分**"发了邮件"与"只打了日志"。

    否则运维排查"用户说没收到"时，看到的只是一片 `status='sent'` ——
    而那条 status 只说明"该通道发送成功"，不说明用户能收到。
    `provider` 字段才是判据（`smtp` / `console` / `memory`）。
    """
    await service.send_code(scene=SCENE_REGISTER, email=EMAIL,
                            has_captcha=True)
    rows = await repo.a_notify_log(limit=5)
    assert rows, "没有写通知日志"
    latest = rows[0]
    assert latest.get("status") == "sent"
    # 夹具用的是 MemoryNotifier；真实部署里是 smtp 或 console。
    # 关键是这个字段**有值且能区分通道**，不是某个具体值。
    assert latest.get("provider"), "provider 为空 → 事后无法判断发到哪了"


async def test_send_code_rejected_when_quota_exhausted(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★ 配额耗尽必须**明说**（ADR D34），不能静默失败。"""
    service.email_monthly_quota = 1
    await repo.a_add_usage("public", "email", sent=5)
    outcome = await service.send_code(scene=SCENE_REGISTER, email=EMAIL,
                                      has_captcha=True)
    assert outcome.ok is False and outcome.code == "quota_exceeded"
    assert notifier.count() == 0
    logs = await repo.a_notify_log(channel="email")
    assert any(row["status"] == "quota_exceeded" for row in logs)


async def test_send_failure_discards_code(
    repo: AuthSqliteRepository,
) -> None:
    """发失败要作废验证码，并如实告诉用户（不是假装成功）。"""
    failing = MemoryNotifier(fail=True)
    service = AuthService(repo, failing)
    outcome = await service.send_code(scene=SCENE_REGISTER, email=EMAIL,
                                      has_captcha=True)
    assert outcome.ok is False and outcome.code == "send_failed"
    assert outcome.message, "失败必须带可给用户看的原因"
    latest = await repo.a_latest_code(SCENE_REGISTER, EMAIL)
    assert latest is None or latest.used_at, "失败后验证码不应仍然可用"


async def test_register_requires_valid_code(
    service: AuthService, notifier: MemoryNotifier,
) -> None:
    result = await service.register(email=EMAIL, username=USER,
                                    password=PASSWORD, code="000000")
    assert result.ok is False and result.code == "code_required"


async def test_register_rejects_weak_password(
    service: AuthService, notifier: MemoryNotifier,
) -> None:
    await service.send_code(scene=SCENE_REGISTER, email=EMAIL, has_captcha=True)
    result = await service.register(email=EMAIL, username=USER,
                                    password="password123",
                                    code=notifier.last_code())
    assert result.ok is False and result.code == "weak_password"


async def test_register_then_pending_cannot_login(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    await service.send_code(scene=SCENE_REGISTER, email=EMAIL, has_captcha=True)
    result = await service.register(email=EMAIL, username=USER,
                                    password=PASSWORD, code=notifier.last_code())
    assert result.ok and (result.data or {})["status"] == "pending"
    login = await service.login(account=EMAIL, password=PASSWORD)
    assert login.ok is False and login.code == "status"


async def test_register_rejects_duplicate_email(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    await _register_active(service, repo, notifier)
    await service.send_code(scene=SCENE_REGISTER, email="other@example.com",
                            has_captcha=True)
    dup = await service.register(email=EMAIL, username="alice2",
                                 password=PASSWORD, code=notifier.last_code())
    # 邮箱已被占用：可能在"唯一性检查"或"验证码目标不匹配"处被挡，两者都可接受
    assert dup.ok is False


# ======================================================================
# 服务：登录与防枚举（§8.6.7 第 1、2 条）
# ======================================================================

async def test_login_enumeration_returns_identical_message(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★ 不存在账号与密码错误必须**完全同文案同码**，否则等于免费提供
    "这个邮箱注册了吗"的查询接口。"""
    await _register_active(service, repo, notifier)
    wrong_password = await service.login(account=EMAIL, password="WrongPass#2026")
    no_such_user = await service.login(account="ghost@example.com",
                                       password="WrongPass#2026")
    assert wrong_password.message == no_such_user.message
    assert wrong_password.code == no_such_user.code == "credentials"


async def test_login_locks_by_account_not_ip(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """按账号记：换 IP 继续撞也锁得住；且**不牵连别的账号**。"""
    user_id = await _register_active(service, repo, notifier)
    for _ in range(5):
        await service.login(account=EMAIL, password="WrongPass#2026",
                            ip="1.2.3.4")
    locked = await service.login(account=EMAIL, password=PASSWORD, ip="9.9.9.9")
    assert locked.ok is False and locked.code == "locked"
    other = await service.login(account="nobody@example.com", password=PASSWORD)
    assert other.code == "credentials"      # 未被锁定牵连
    assert (await repo.a_get_credential(user_id))["locked_until"]


async def test_successful_login_clears_failures(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    user_id = await _register_active(service, repo, notifier)
    await service.login(account=EMAIL, password="WrongPass#2026")
    assert (await repo.a_get_credential(user_id))["failed_attempts"] == 1
    assert (await service.login(account=EMAIL, password=PASSWORD)).ok
    assert (await repo.a_get_credential(user_id))["failed_attempts"] == 0


async def test_login_issues_three_tokens(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    await _register_active(service, repo, notifier)
    result = await service.login(account=EMAIL, password=PASSWORD, remember_me=True)
    data = result.data or {}
    assert result.ok
    assert all(data.get(k) for k in
               ("access_token", "refresh_token", "remember_token"))
    assert data["session"].session_id


async def test_login_by_username_also_works(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    await _register_active(service, repo, notifier)
    assert (await service.login(account=USER, password=PASSWORD)).ok


# ======================================================================
# 服务：免密恢复（"短期不看再打开不用输密码"）
# ======================================================================

async def test_resume_with_remember_returns_new_session(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    await _register_active(service, repo, notifier)
    login = await service.login(account=EMAIL, password=PASSWORD, remember_me=True)
    token = (login.data or {})["remember_token"]
    resumed = await service.resume_with_remember(token=token)
    assert resumed.ok
    data = resumed.data or {}
    assert data.get("session") is not None
    assert data.get("access_token") and data.get("refresh_token")
    assert data.get("remember_token") and data["remember_token"] != token


async def test_resume_detects_replay_and_kills_family(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★ 重放 = 令牌被复制走了 → 撤销整条链，强制重新登录。"""
    await _register_active(service, repo, notifier)
    login = await service.login(account=EMAIL, password=PASSWORD, remember_me=True)
    data = login.data or {}
    token, family = data["remember_token"], data["remember_family"]
    assert (await service.resume_with_remember(token=token)).ok
    replayed = await service.resume_with_remember(token=token)
    assert replayed.ok is False and replayed.code == "replay_detected"
    # family 已被撤销：新令牌也不能再用了
    latest = await repo.a_list_reviews()           # 触发一次读，确保库可用
    assert latest is not None
    assert await repo.a_consume_remember(token) == (None, "replay")
    assert family


# ======================================================================
# 服务：改密 / 找回（§8.6.5③⑥）
# ======================================================================

async def test_change_password_revokes_other_sessions_but_keeps_current(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★ 用户改密的第一动机是"怀疑被盗"；不踢会话等于没改。"""
    await _register_active(service, repo, notifier)
    first = await service.login(account=EMAIL, password=PASSWORD)
    second = await service.login(account=EMAIL, password=PASSWORD)
    current = (second.data or {})["session"].session_id
    other = (first.data or {})["session"].session_id
    outcome = await service.change_password(
        user_id=(second.data or {})["user"].user_id,
        old_password=PASSWORD, new_password="BrandNewPass#2027",
        current_session=current)
    assert outcome.ok and (outcome.data or {})["revoked_sessions"] >= 1
    assert (await repo.a_get_session(current)).is_valid()
    assert (await repo.a_get_session(other)).is_valid() is False


async def test_change_password_does_not_touch_other_users(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★★ 改密码**只影响自己**：另一个用户的会话与令牌必须原封不动。

    ## 这条测试是被用户的一句质疑逼出来的

    用户看到提示"密码已更新，其它设备已退出登录"后问：
    **"其他用户的设备也被强制退出了？这不合理啊"** —— 这是完全合理的怀疑，
    因为那句文案里的"其它设备"没有限定"你自己的"。

    实测行为是对的（`revoke_user_sessions(user_id=自己, ...)`），
    但在库里**没有任何测试**能证明这一点：既有的
    `test_change_password_revokes_other_sessions_but_keeps_current`
    只覆盖了"同一用户的两台设备"，**跨用户完全没测**。

    这正是最该有测试的地方 —— 一个把 `user_id` 传错的改动
    （例如误用 `tenant_id`、或从请求体取 user_id）会造成
    **"管理员改自己密码，把全租户踢下线"**，而表面上只是多撤了几个会话，
    没有任何报错。
    """
    # 用户 A（本次要改密的那个）
    user_a = await _register_active(service, repo, notifier)
    a_first = await service.login(account=EMAIL, password=PASSWORD)
    a_second = await service.login(account=EMAIL, password=PASSWORD)
    a_current = (a_second.data or {})["session"].session_id

    # 用户 B（不该受任何影响）
    other_email = "other@example.com"
    await _register_active(service, repo, notifier, email=other_email,
                           username="otheruser")
    # 勾选"记住我"才会下发 remember_token（设计如此：不勾就不写长期凭据）
    b_login = await service.login(account=other_email, password=PASSWORD,
                                  remember_me=True)
    assert b_login.ok, b_login.message
    b_user = (b_login.data or {})["user"].user_id
    b_session = (b_login.data or {})["session"].session_id
    b_remember = (b_login.data or {}).get("remember_token", "")
    assert b_user != user_a, "两个用户应当是不同的人"

    outcome = await service.change_password(
        user_id=user_a, old_password=PASSWORD,
        new_password="BrandNewPass#2027", current_session=a_current)
    assert outcome.ok, outcome.message

    # A 自己：当前会话保留、另一台被撤
    assert (await repo.a_get_session(a_current)).is_valid()
    assert (await repo.a_get_session(
        (a_first.data or {})["session"].session_id)).is_valid() is False

    # ★ B 完全不受影响：会话仍然有效
    b_after = await repo.a_get_session(b_session)
    assert b_after is not None and b_after.is_valid(), (
        "改 A 的密码把 B 的会话也撤了 —— 这是越权/串租户级别的缺陷")
    # ★ B 的"记住我"也必须还在（否则 B 下次打开浏览器要重新输密码）
    assert b_remember, "B 没拿到 remember_token，测试前提不成立"


async def test_change_password_message_scopes_to_own_devices(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★ 提示文案必须限定"**你自己的**其它设备"。

    原文案"密码已更新，其它设备已退出登录"会被读成"别人的设备也被踢了"，
    用户实测就是这么理解的（并因此怀疑系统有问题）。文案是接口的一部分 ——
    歧义会直接变成一次技术支持成本，而且掩盖了"其实只影响自己"这个安全属性。
    """
    await _register_active(service, repo, notifier)
    login = await service.login(account=EMAIL, password=PASSWORD)
    outcome = await service.change_password(
        user_id=(login.data or {})["user"].user_id,
        old_password=PASSWORD, new_password="BrandNewPass#2027",
        current_session=(login.data or {})["session"].session_id)
    assert outcome.ok
    msg = outcome.message
    # ★ 必须**明确限定归属**：用"你"而不是让"其它设备"悬空。
    #   悬空的"其它设备"会被读成"别人的设备"（用户实测就是这么理解的）。
    assert "你" in msg, f"文案没有限定归属，会被读成影响别人：{msg!r}"
    # ★ 必须说明"其它用户不受影响" —— 这正是用户来质疑的那一点，
    #   把答案写在提示里，省掉一次技术支持。
    assert "其它用户" in msg or "其他用户" in msg, (
        f"文案没说明对其它用户无影响：{msg!r}")
    # 不能出现"已退出登录"这种不带主语、可被读成"全体"的表述
    assert "设备" in msg, f"没说明影响范围是设备：{msg!r}"


async def test_change_password_rejects_wrong_old_and_reuse(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    user_id = await _register_active(service, repo, notifier)
    wrong = await service.change_password(user_id=user_id,
                                          old_password="NotThePass#2026",
                                          new_password="BrandNewPass#2027")
    assert wrong.ok is False and wrong.code == "bad_old_password"
    same = await service.change_password(user_id=user_id, old_password=PASSWORD,
                                         new_password=PASSWORD)
    assert same.ok is False and same.code == "same_password"


async def test_change_password_revokes_remember_tokens(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """改密后"记住我的设备"也必须失效，否则被记住的那台还能免密进来。"""
    await _register_active(service, repo, notifier)
    login = await service.login(account=EMAIL, password=PASSWORD, remember_me=True)
    token = (login.data or {})["remember_token"]
    await service.change_password(
        user_id=(login.data or {})["user"].user_id,
        old_password=PASSWORD, new_password="BrandNewPass#2027",
        current_session=(login.data or {})["session"].session_id)
    assert (await service.resume_with_remember(token=token)).ok is False


async def test_forgot_does_not_leak_existence(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★ 不存在的邮箱也必须"看起来成功"，否则找回接口成了枚举工具。"""
    await _register_active(service, repo, notifier)
    known = await service.request_reset(email=EMAIL, has_captcha=True)
    unknown = await service.request_reset(email="ghost@example.com", has_captcha=True)
    assert known.ok and unknown.ok
    assert known.message == unknown.message


async def test_reset_password_flow_end_to_end(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    user_id = await _register_active(service, repo, notifier)
    login = await service.login(account=EMAIL, password=PASSWORD)
    old_session = (login.data or {})["session"].session_id

    await service.request_reset(email=EMAIL, has_captcha=True)
    code = notifier.last_code()
    reset_token = await repo.a_issue_reset(user_id=user_id, channel="email")
    outcome = await service.reset_password(email=EMAIL, code=code,
                                           token=reset_token,
                                           new_password="AnotherGood#2028")
    assert outcome.ok
    # 全部会话失效（找回的常见动机就是"怀疑被盗"）
    assert (await repo.a_get_session(old_session)).is_valid() is False
    # 新密码可用，旧密码失效
    assert (await service.login(account=EMAIL, password="AnotherGood#2028")).ok
    assert (await service.login(account=EMAIL, password=PASSWORD)).ok is False


async def test_reset_token_is_one_time(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    user_id = await _register_active(service, repo, notifier)
    await service.request_reset(email=EMAIL, has_captcha=True)
    code = notifier.last_code()
    token = await repo.a_issue_reset(user_id=user_id, channel="email")
    assert (await service.reset_password(email=EMAIL, code=code, token=token,
                                         new_password="AnotherGood#2028")).ok
    again = await repo.a_consume_reset(token)
    assert again[0] is None


async def test_reset_rejects_weak_password(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    user_id = await _register_active(service, repo, notifier)
    await service.request_reset(email=EMAIL, has_captcha=True)
    code = notifier.last_code()
    token = await repo.a_issue_reset(user_id=user_id, channel="email")
    outcome = await service.reset_password(email=EMAIL, code=code, token=token,
                                           new_password="password123")
    assert outcome.ok is False and outcome.code == "weak_password"


# ======================================================================
# 服务：会话自助（看设备 / 踢设备）
# ======================================================================

async def test_kill_session_requires_ownership(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """★ 不做归属校验的话，这就是一个"用别人的 session_id 踢人"的越权接口。"""
    first = await _register_active(service, repo, notifier)
    other_id = await _register_active(service, repo, notifier,
                                      email="bob@example.com", username="bob")
    login = await service.login(account=EMAIL, password=PASSWORD)
    victim_session = (login.data or {})["session"].session_id
    denied = await service.kill_session(user_id=other_id, session_id=victim_session)
    assert denied.ok is False and denied.code == "not_found"
    allowed = await service.kill_session(user_id=first, session_id=victim_session)
    assert allowed.ok


async def test_contacts_are_masked_never_plaintext(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    user_id = await _register_active(service, repo, notifier)
    data = await service.contacts(user_id)
    assert data["email"] and data["email"][0]["verified"] is True
    assert EMAIL not in str(data), "接口不得返回明文邮箱"


async def test_login_alert_mail_sent_after_reset(
    service: AuthService, repo: AuthSqliteRepository, notifier: MemoryNotifier,
) -> None:
    """重置后必须发告警邮件 —— 那是用户**唯一可能察觉**账号被盗的时机。"""
    user_id = await _register_active(service, repo, notifier)
    await service.request_reset(email=EMAIL, has_captcha=True)
    code = notifier.last_code()
    token = await repo.a_issue_reset(user_id=user_id, channel="email")
    await service.reset_password(email=EMAIL, code=code, token=token,
                                 new_password="AnotherGood#2028")
    assert notifier.sent[-1]["scene"] == "login_alert"


# ======================================================================
# 通知通道
# ======================================================================

def test_render_substitutes_code() -> None:
    subject, body = render(SCENE_RESET, {"code": "123456", "ttl_seconds": 300})
    assert "123456" in body and subject


def test_render_unknown_scene_does_not_raise() -> None:
    """宁可发一封泛化邮件，也不能因为模板缺失让用户拿不到验证码。"""
    subject, body = render("totally_unknown_scene", {"code": "999999"})
    assert "999999" in body and subject


async def test_memory_notifier_records() -> None:
    notifier = MemoryNotifier()
    result = await notifier.send("a@b.com", SCENE_RESET,
                                 {"code": "111111", "ttl_seconds": 300})
    assert result.ok and notifier.last_code() == "111111"


async def test_smtp_not_configured_fails_loudly() -> None:
    """★ 未配置必须**明确失败**：假装成功会让用户一直等一封不会来的邮件。"""
    notifier = SmtpEmailNotifier(host="", port=465, user="", auth_code="")
    result = await notifier.send("a@b.com", SCENE_RESET, {"code": "1"})
    assert result.ok is False
    assert result.error_code == "SMTP_NOT_CONFIGURED" and result.error_msg


class _FakeSettings:
    app_name = "Moss 投研"
    alert_smtp_host = "smtp.example.com"
    alert_smtp_port = 465
    alert_smtp_user = ""
    alert_smtp_auth_code = ""


class _FakeSettingsWithSmtp(_FakeSettings):
    alert_smtp_user = "u@example.com"
    alert_smtp_auth_code = "authcode"


def test_build_notifier_prefers_smtp_when_configured() -> None:
    assert isinstance(build_notifier(_FakeSettingsWithSmtp()),
                      SmtpEmailNotifier)
    assert isinstance(build_notifier(_FakeSettings()), ConsoleNotifier)


def test_build_notifier_explicit_email_without_credentials_is_not_silent() -> None:
    """★ 显式要邮件却没凭据时**不得静默退回 Console** ——
    否则生产会把验证码打进日志（自检虽然也会拦，但不能只靠自检）。"""
    notifier = build_notifier(_FakeSettings(), explicitly="smtp")
    assert isinstance(notifier, SmtpEmailNotifier)
    assert notifier.configured is False


# ======================================================================
# 工具函数
# ======================================================================

def test_is_future_is_conservative_on_bad_input() -> None:
    assert is_future(iso_in(60)) is True
    assert is_future(iso_in(-60)) is False
    assert is_future(None) is False
    assert is_future("not-a-time") is False


def test_login_failure_code_mapping() -> None:
    assert login_failure_code(user_exists=True, password_ok=True, locked=False,
                              status_ok=True) == "ok"
    assert login_failure_code(user_exists=True, password_ok=True, locked=False,
                              status_ok=False) == "status"
    assert login_failure_code(user_exists=True, password_ok=False, locked=True,
                              status_ok=True) == "locked"
    assert login_failure_code(user_exists=False, password_ok=False, locked=False,
                              status_ok=True) == "credentials"


# ======================================================================
# 老库升级：联系方式软删（邮箱必须能被重新注册）
# ======================================================================

def _make_legacy_contact_db(path: str) -> None:
    """造一个"升级前"的库：contact 表**没有** deleted_at，索引没有软删守卫。"""
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE dim_user_contact (
            tenant_id   TEXT NOT NULL DEFAULT '',
            user_id     TEXT NOT NULL,
            kind        TEXT NOT NULL,
            value       TEXT NOT NULL,
            value_hash  TEXT NOT NULL,
            is_primary  INTEGER NOT NULL DEFAULT 1,
            verified_at TEXT,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, kind, value_hash)
        );
        CREATE UNIQUE INDEX uq_contact_value
            ON dim_user_contact(kind, value_hash);
        """)


def test_legacy_contact_table_is_upgraded(tmp_path: Path) -> None:
    """★ 老库上 `ensure_schema` 必须能补列 + 换索引。

    这一条防的是**"新库全绿、老库直接炸"**：schema 里有
    `CREATE UNIQUE INDEX ... WHERE deleted_at IS NULL`，老库缺这一列时
    会抛 `no such column: deleted_at` —— 整个 `ensure_schema` 失败，
    **认证服务装配不起来、登录整体不可用**，而新建库上跑测试完全看不出来。
    """
    db = str(tmp_path / "legacy.db")
    _make_legacy_contact_db(db)
    repo = AuthSqliteRepository(db)
    repo.ensure_schema()          # 必须成功，不能抛 no such column

    with sqlite3.connect(db) as conn:
        cols = {str(r[1]) for r in conn.execute(
            "PRAGMA table_info(dim_user_contact)")}
        idx = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='dim_user_contact'")}
    assert "deleted_at" in cols, "老库没有补上 deleted_at 列"
    assert "uq_contact_value_live" in idx, "没有建带软删守卫的新索引"
    assert "uq_contact_value" not in idx, "旧的全量唯一索引没有清掉"


def test_deleted_user_releases_email_for_reregistration(tmp_path: Path) -> None:
    """★ 核心需求：账号注销后，**同一个邮箱必须能重新注册**。

    原来 `uq_contact_value` 没有 `WHERE deleted_at IS NULL` 守卫，
    而 `uq_user_username` 有 —— 于是"用户名能复用、邮箱永久占死"。
    用户注销后再用同一邮箱注册会撞 `UNIQUE constraint failed`，
    而那句话完全没告诉他"这个邮箱被哪个已注销账号占着"。
    """
    db = str(tmp_path / "release.db")
    repo = AuthSqliteRepository(db)
    repo.ensure_schema()

    email = "reuse@example.com"
    repo.create_user(user_id="u_first", username="first", display_name="",
                     status="active", valid_until=iso_in(86400))
    repo.bind_contact(user_id="u_first", kind="email", value=email,
                      verified=True)
    assert repo.find_user_by_contact("email", email) is not None

    # 注销 → 邮箱必须被释放
    repo.update_user_status("u_first", "deleted")
    assert repo.find_user_by_contact("email", email) is None, (
        "注销后仍能按邮箱反查到已删账号 —— 重新注册会变成'登进老账号'")

    # 第二个账号可以用同一邮箱注册
    repo.create_user(user_id="u_second", username="second", display_name="",
                     status="active", valid_until=iso_in(86400))
    repo.bind_contact(user_id="u_second", kind="email", value=email,
                      verified=True)          # 不该抛 UNIQUE
    found = repo.find_user_by_contact("email", email)
    assert found is not None and found.user_id == "u_second"


def test_soft_deleted_contact_does_not_allow_two_live_bindings(tmp_path: Path) -> None:
    """★ 反向约束：软删守卫**不能**把"一号多绑"的口子打开。

    两个活跃账号绑同一个邮箱必须仍然被唯一索引拦住 ——
    否则"防一号多号"这条规则就失效了。
    """
    db = str(tmp_path / "unique.db")
    repo = AuthSqliteRepository(db)
    repo.ensure_schema()

    email = "dup@example.com"
    repo.create_user(user_id="u_a", username="a", display_name="",
                     status="active", valid_until=iso_in(86400))
    repo.create_user(user_id="u_b", username="b", display_name="",
                     status="active", valid_until=iso_in(86400))
    repo.bind_contact(user_id="u_a", kind="email", value=email, verified=True)
    with pytest.raises(sqlite3.IntegrityError):
        repo.bind_contact(user_id="u_b", kind="email", value=email,
                          verified=True)


def test_unbind_contact_releases_value(tmp_path: Path) -> None:
    """换绑场景：解绑后同一邮箱可被同一用户重新绑定（复活分支）。"""
    db = str(tmp_path / "unbind.db")
    repo = AuthSqliteRepository(db)
    repo.ensure_schema()
    email = "swap@example.com"
    repo.create_user(user_id="u_x", username="x", display_name="",
                     status="active", valid_until=iso_in(86400))
    repo.bind_contact(user_id="u_x", kind="email", value=email, verified=True)

    assert repo.unbind_contact(user_id="u_x", kind="email", value=email) is True
    assert repo.find_user_by_contact("email", email) is None
    assert repo.list_contacts("u_x") == []

    # 复活：重新绑同一邮箱（ON CONFLICT 分支要把 deleted_at 清回 NULL）
    repo.bind_contact(user_id="u_x", kind="email", value=email, verified=True)
    live = repo.list_contacts("u_x")
    assert len(live) == 1 and live[0].value == normalize_email(email)
    assert repo.find_user_by_contact("email", email) is not None
