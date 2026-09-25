"""认证体系仓储（SQLite 原生实现）—— 五张核心表 + 通知用量/日志。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.4（数据模型）、
§8.6.8⑤（通知配额与对账）、§8.6.11（会话与两层凭证）。

## 三条贯穿全文件的硬纪律

1. **一切秘密只存哈希**：验证码、重置令牌、remember token、会话 refresh token
   **一律存 SHA-256 摘要**，库里拿不到明文。密码另走慢哈希（见 `hash_password`）。
2. **手机号/邮箱存"密文 + 盲索引"**：`value` 作展示（可脱敏），
   `value_hash` 作查询键 —— 既能"按邮箱查用户"，又不必为了查询而存明文。
3. **用户级表的每个查询都带 `(tenant_id, user_id)`**：这是 §5.4 的行级隔离在
   仓储层的落地。**没有"只按 id 查"的公开方法** —— 那正是越权的入口。

## 为什么不用 ORM

与项目既有仓储一致（`intraday_profile_sqlite_repo.py`）：原生 sqlite3 + 内联 DDL +
`PRAGMA` 补列。理由是不引入额外依赖、且建表语句就是文档本身。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

#: 表名常量（集中定义，便于测试与迁移脚本引用）
TABLE_USER = "dim_user"
TABLE_CREDENTIAL = "dim_user_credential"
TABLE_CONTACT = "dim_user_contact"
TABLE_SESSION = "fact_session"
TABLE_VCODE = "fact_verification_code"
TABLE_REMEMBER = "fact_remember_token"
TABLE_RESET = "fact_password_reset"
TABLE_REVIEW = "fact_registration_review"
TABLE_NOTIFY_USAGE = "fact_notify_usage"
TABLE_NOTIFY_LOG = "fact_notify_log"

#: 账号生命周期状态（§8.6.1）
UserStatus = Literal["pending", "active", "rejected", "disabled", "expired", "deleted"]

#: 允许登录的状态（**只有 active**）。其余各自的文案在前端区分。
LOGIN_ALLOWED_STATUS: frozenset[str] = frozenset({"active"})

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_USER} (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    applied_tier  TEXT NOT NULL DEFAULT 'trial',
    reviewed_by   TEXT,
    reviewed_at   TEXT,
    review_note   TEXT NOT NULL DEFAULT '',
    valid_from    TEXT,
    valid_until   TEXT NOT NULL,
    expire_warned_at TEXT,
    renewed_count INTEGER NOT NULL DEFAULT 0,
    deleted_at    TEXT,
    deleted_by    TEXT,
    delete_reason TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
-- 邮箱唯一：**排除已软删除行**，否则"删了就不能再用同一邮箱注册"（§8.6.1.3）
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_username
    ON {TABLE_USER}(username) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS {TABLE_CREDENTIAL} (
    user_id        TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL,
    password_algo  TEXT NOT NULL DEFAULT 'pbkdf2_sha256',
    password_updated_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until   TEXT,
    last_failed_at TEXT,
    last_failed_ip TEXT,
    password_history_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS {TABLE_CONTACT} (
    tenant_id   TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    value       TEXT NOT NULL,
    value_hash  TEXT NOT NULL,
    is_primary  INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT,
    created_at  TEXT NOT NULL,
    -- ★ 软删时间。**必须存在**，否则下面的"部分唯一索引"无从判断。
    --
    -- 为什么联系方式也要软删：账号被删除/注销后，那个邮箱必须能被**重新注册**。
    -- 实测踩到：`uq_contact_value` 原来没有 `WHERE deleted_at IS NULL` 守卫，
    -- 而 `uq_user_username` 有 —— 于是"用户名能复用、邮箱永久占死"，
    -- 用户注销后再也无法用同一邮箱注册，且报错是 `UNIQUE constraint failed`
    -- （说的是约束名，用户根本看不懂自己在撞什么）。
    deleted_at  TEXT,
    PRIMARY KEY (user_id, kind, value_hash)
);
-- 一个邮箱/手机号只能绑到**一个未删除的**账号（防一号多号）。
-- ⚠️ `WHERE deleted_at IS NULL` 不能少，理由见上面的列注释。
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_value_live
    ON {TABLE_CONTACT}(kind, value_hash) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS {TABLE_SESSION} (
    session_id     TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    console        TEXT NOT NULL DEFAULT 'default',
    access_jti     TEXT NOT NULL UNIQUE,
    refresh_hash   TEXT,
    created_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at     TEXT,
    revoke_reason  TEXT NOT NULL DEFAULT '',
    device_label   TEXT NOT NULL DEFAULT '',
    device_id      TEXT NOT NULL DEFAULT '',
    user_agent     TEXT NOT NULL DEFAULT '',
    ip             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sess_user ON {TABLE_SESSION}(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_sess_expire
    ON {TABLE_SESSION}(idle_expires_at, absolute_expires_at);

CREATE TABLE IF NOT EXISTS {TABLE_VCODE} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scene        TEXT NOT NULL,
    target_hash  TEXT NOT NULL,
    code_hash    TEXT NOT NULL,
    purpose_user TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    sent_at      TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    request_ip   TEXT NOT NULL DEFAULT '',
    send_channel TEXT NOT NULL DEFAULT 'email',
    provider_msg_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_vcode_lookup
    ON {TABLE_VCODE}(scene, target_hash, expires_at);

CREATE TABLE IF NOT EXISTS {TABLE_REMEMBER} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id    TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    user_id      TEXT NOT NULL,
    tenant_id    TEXT NOT NULL DEFAULT '',
    issued_at    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    rotated_from TEXT,
    revoked_at   TEXT,
    device_label TEXT NOT NULL DEFAULT '',
    user_agent   TEXT NOT NULL DEFAULT '',
    ip           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_remember_family ON {TABLE_REMEMBER}(family_id);

CREATE TABLE IF NOT EXISTS {TABLE_RESET} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    channel     TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    request_ip  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS {TABLE_REVIEW} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    action       TEXT NOT NULL,
    from_status  TEXT NOT NULL DEFAULT '',
    to_status    TEXT NOT NULL DEFAULT '',
    tier_code    TEXT NOT NULL DEFAULT '',
    valid_until  TEXT,
    reviewer_id  TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    ip           TEXT NOT NULL DEFAULT '',
    chain_hash   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_user ON {TABLE_REVIEW}(user_id, at);

CREATE TABLE IF NOT EXISTS {TABLE_NOTIFY_USAGE} (
    tenant_id    TEXT NOT NULL,
    channel      TEXT NOT NULL,
    period       TEXT NOT NULL,
    sent_count   INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    cost_yuan    REAL NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, channel, period)
);

CREATE TABLE IF NOT EXISTS {TABLE_NOTIFY_LOG} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    scene       TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT '',
    provider_msg_id TEXT NOT NULL DEFAULT '',
    unit_price  REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    error_code  TEXT NOT NULL DEFAULT '',
    error_msg   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notify_log
    ON {TABLE_NOTIFY_LOG}(tenant_id, channel, at);
"""


# ======================================================================
# 纯函数工具（可单测，无副作用）
# ======================================================================

def sha256_hex(text: str) -> str:
    """秘密值的摘要。**验证码/令牌一律经它入库**，库里不留明文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_email(value: str) -> str:
    """邮箱归一化：去空白 + 小写。**入库与查询必须走同一个函数**，
    否则 `A@x.com` 与 `a@x.com` 会被当成两个账号。"""
    return str(value or "").strip().lower()


def normalize_phone(value: str) -> str:
    """手机号归一化：只留数字（容忍 `+86`/`-`/空格等写法）。"""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def mask_contact(value: str, kind: str) -> str:
    """脱敏展示：邮箱 `ab***@x.com`、手机号 `138****1234`（§8.6.6 的展示口径）。"""
    text = str(value or "")
    if kind == "phone":
        digits = normalize_phone(text)
        if len(digits) >= 7:
            return f"{digits[:3]}****{digits[-4:]}"
        return digits
    if "@" not in text:
        return "***"
    local, _, domain = text.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}***@{domain}"


def new_token(nbytes: int = 32) -> str:
    """密码学随机令牌（urlsafe，无歧义字符问题）。"""
    return secrets.token_urlsafe(nbytes)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    """统一的时间序列化（带时区，秒精度）—— 全表只此一种写法。"""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def iso_in(seconds: float) -> str:
    return iso(utc_now() + timedelta(seconds=seconds))


def parse_iso(value: str | None) -> datetime | None:
    """解析 `iso()` 写出的时间；脏数据返回 None（不抛，避免一行坏数据打断整表）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_future(value: str | None) -> bool:
    """该时间是否仍在未来（None/不可解析一律 False = 已过期，保守取向）。"""
    parsed = parse_iso(value)
    return parsed is not None and parsed > utc_now()


def verify_code_hash(candidate: str, stored_hash: str) -> bool:
    """验证码/令牌比对：**常量时间**，避免时序侧信道泄露"猜对了几位"。"""
    return hmac.compare_digest(sha256_hex(candidate), str(stored_hash or ""))


# ======================================================================
# 密码哈希（慢哈希；算法可插拔）
# ======================================================================

#: PBKDF2 迭代次数。取 60 万（OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议下限）。
#: ⚠️ 这是**在没有 argon2/bcrypt 时的最优 stdlib 选择**，不是首选方案：
#: 首选 Argon2id。装了 `argon2-cffi` 或 `bcrypt` 会自动切过去（见 `_pick_algo`），
#: 所以升级路径是"装包即可"，不需要改数据 —— 每个哈希自带算法前缀。
PBKDF2_ITERATIONS = 600_000


def _pick_algo() -> str:
    """选可用的最强算法：argon2id > bcrypt > pbkdf2_sha256。"""
    try:
        import argon2  # noqa: F401
        return "argon2id"
    except ImportError:
        pass
    try:
        import bcrypt  # noqa: F401
        return "bcrypt"
    except ImportError:
        pass
    return "pbkdf2_sha256"


def hash_password(password: str, *, algo: str | None = None) -> tuple[str, str]:
    """返回 `(哈希串, 算法名)`。哈希串自带算法前缀，便于将来平滑升级。

    拒绝空密码（配置/调用错误应当**立刻可见**，而不是存一个能过校验的空哈希）。
    """
    if not password:
        raise ValueError("密码不能为空")
    name = algo or _pick_algo()
    if name == "argon2id":
        from argon2 import PasswordHasher  # type: ignore[import-not-found]

        encoded = PasswordHasher().hash(password)
        return encoded, "argon2id"
    if name == "bcrypt":
        import bcrypt  # type: ignore[import-not-found]

        return bcrypt.hashpw(password.encode("utf-8"),
                             bcrypt.gensalt()).decode("ascii"), "bcrypt"
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, PBKDF2_ITERATIONS)
    return (f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}",
            "pbkdf2_sha256")


def verify_password(password: str, stored: str, algo: str = "") -> bool:
    """校验密码。**任何异常都返回 False**（坏哈希不能变成"校验通过"）。"""
    if not password or not stored:
        return False
    try:
        if stored.startswith("$argon2"):
            from argon2 import PasswordHasher  # type: ignore[import-not-found]
            from argon2.exceptions import VerifyMismatchError  # type: ignore

            try:
                PasswordHasher().verify(stored, password)
                return True
            except VerifyMismatchError:
                return False
        if stored.startswith("$2"):
            import bcrypt  # type: ignore[import-not-found]

            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("ascii"))
        if stored.startswith("pbkdf2_sha256$"):
            _, raw_iter, salt_hex, digest_hex = stored.split("$", 3)
            computed = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"),
                bytes.fromhex(salt_hex), int(raw_iter))
            return hmac.compare_digest(computed.hex(), digest_hex)
    except Exception:  # noqa: BLE001 校验路径绝不抛（否则会变成 500 而不是"密码错"）
        logger.warning("密码校验异常（按失败处理），algo=%s", algo or "?", exc_info=True)
    return False


# ======================================================================
# 纯函数：配额/限流判定（不碰 DB，便于单测穷举边界）
# ======================================================================

def quota_exceeded(used: int, limit: int) -> bool:
    """额度是否已耗尽。`limit <= 0` 视为**不允许发送**（这是刻意的：
    "没配额度"和"无限额度"是两件事，默认应当是前者）。"""
    if limit <= 0:
        return True
    return used >= limit


def period_of(when: datetime | None = None) -> str:
    """计费月键 `YYYY-MM`（按 UTC）。配额按自然月归零。"""
    return (when or utc_now()).strftime("%Y-%m")


#: 表名 → 本版本新增的列（老库升级用）。
#:
#: ⚠️ **加列必须登记在这里**，否则老库上的 `CREATE INDEX`/`SELECT` 会引用到
#: 不存在的列而整段失败。这不是可有可无的整洁度问题 ——
#: `deleted_at` 漏登记时，`ensure_schema` 在老库上直接抛
#: `no such column: deleted_at`，**认证服务装配不起来、整个登录不可用**，
#: 而新装的库上跑测试完全看不出来。
_ADDABLE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    TABLE_CONTACT: (("deleted_at", "TEXT"),),
}


# ======================================================================
# 数据对象
# ======================================================================

@dataclass(frozen=True)
class UserRecord:
    user_id: str
    username: str
    display_name: str
    status: str
    applied_tier: str
    valid_until: str
    created_at: str
    reviewed_by: str = ""
    reviewed_at: str = ""
    review_note: str = ""
    valid_from: str = ""
    deleted_at: str = ""
    delete_reason: str = ""

    @property
    def is_expired(self) -> bool:
        """有效期是否已过。**注意：到期 ≠ 禁用**（§8.6.11.6 的三种路径）。"""
        return not is_future(self.valid_until)

    def can_login(self) -> tuple[bool, str]:
        """能否登录，附**可给用户看的原因**（§8.6.5② 的文案口径）。

        刻意返回"具体原因"而不是统一的"登录失败"：
        账号状态对**本人**不是秘密，说清楚能省掉大量客服成本。
        （而"密码错 / 账号不存在"必须统一文案 —— 那才是防枚举。）
        """
        if self.status == "pending":
            return False, "账号正在等待管理员审批"
        if self.status == "rejected":
            reason = self.review_note or "未通过审批"
            return False, f"注册申请未通过：{reason}"
        if self.status == "disabled":
            return False, "账号已被禁用，请联系管理员"
        if self.status == "deleted":
            return False, "账号不存在"
        if self.status == "expired" or self.is_expired:
            return False, f"账号已于 {self.valid_until[:10]} 到期，请联系管理员续期"
        if self.status not in LOGIN_ALLOWED_STATUS:
            return False, f"账号状态异常（{self.status}）"
        return True, ""


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    tenant_id: str
    access_jti: str
    created_at: str
    last_seen_at: str
    idle_expires_at: str
    absolute_expires_at: str
    console: str = "default"
    revoked_at: str = ""
    revoke_reason: str = ""
    device_label: str = ""
    ip: str = ""

    def is_valid(self) -> bool:
        """是否仍可用：未撤销 **且** 未超滑动期 **且** 未超绝对上限。

        ⚠️ 三条缺一不可。只判滑动期会让"持续活跃的会话永不失效"（§8.6.11.3）。
        """
        if self.revoked_at:
            return False
        return is_future(self.idle_expires_at) and is_future(self.absolute_expires_at)


@dataclass(frozen=True)
class ContactRecord:
    user_id: str
    kind: str
    value: str
    value_hash: str
    verified_at: str = ""
    is_primary: bool = True

    @property
    def verified(self) -> bool:
        return bool(self.verified_at)

    @property
    def masked(self) -> str:
        return mask_contact(self.value, self.kind)


@dataclass(frozen=True)
class VerifyCodeRecord:
    id: int
    scene: str
    target_hash: str
    code_hash: str
    attempts: int
    max_attempts: int
    sent_at: str
    expires_at: str
    used_at: str = ""

    def is_usable(self) -> bool:
        """未用过、未过期、且未超尝试次数。"""
        if self.used_at:
            return False
        if self.attempts >= self.max_attempts:
            return False
        return is_future(self.expires_at)


@dataclass(frozen=True)
class ReviewRecord:
    at: str
    user_id: str
    action: str
    from_status: str = ""
    to_status: str = ""
    tier_code: str = ""
    valid_until: str = ""
    reviewer_id: str = ""
    note: str = ""
    chain_hash: str = ""


@dataclass(frozen=True)
class NotifyUsage:
    tenant_id: str
    channel: str
    period: str
    sent_count: int = 0
    failed_count: int = 0
    cost_yuan: float = 0.0


@dataclass
class _AuditChain:
    """审批流水的哈希链（与 `repositories/audit_chain.py` 同思路，本地维护游标）。"""

    last_hash: str = ""

    def next_hash(self, payload: dict[str, Any]) -> str:
        raw = (self.last_hash + json.dumps(payload, sort_keys=True,
                                           ensure_ascii=False)).encode("utf-8")
        self.last_hash = hashlib.sha256(raw).hexdigest()
        return self.last_hash


@dataclass
class _Cache:
    """进程内小缓存（避免每个请求都扫表结构）。"""

    schema_done: bool = False
    review_chain: _AuditChain = field(default_factory=_AuditChain)


# ======================================================================
# 主仓储
# ======================================================================

class AuthSqliteRepository:
    """认证仓储：同步实现 + `asyncio.to_thread` 异步端口。

    线程安全说明：每次调用**新建连接**（与项目其它 sqlite 仓储一致），
    因此不需要跨线程共享连接；WAL 由主库统一开启。
    """

    def __init__(self, db_path: str = "data/moss_finagent.db") -> None:
        self._db_path = db_path
        self._cache = _Cache()

    # ---------------- 连接与建表 ----------------

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """老库补列（`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列）。

        ★ **必须在 `executescript(_SCHEMA)` 之前跑**：schema 里那条
        `CREATE UNIQUE INDEX ... WHERE deleted_at IS NULL` 引用了本列，
        老库上若还没这一列，会直接 `no such column: deleted_at`
        —— **整个 `ensure_schema` 失败，认证服务根本装配不起来**
        （实测踩到：新库测试全绿，老库一跑就炸。所以一定要拿真实的老库验一次）。
        """
        for table, columns in _ADDABLE_COLUMNS.items():
            present = {str(r[1]) for r in
                       conn.execute(f"PRAGMA table_info({table})")}
            if not present:
                continue          # 表还不存在，等 executescript 建
            for name, ddl in columns:
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    logger.info("%s 补列：%s（老库升级）", table, name)

    def ensure_schema(self) -> None:
        """建表（幂等）。**只建表、不删列** —— 迁移永远是向后兼容的加列。"""
        with self._connect() as conn:
            # 先补列，再建索引/表 —— 顺序不能反（见 `_add_missing_columns`）
            self._add_missing_columns(conn)
            conn.executescript(_SCHEMA)
            self._migrate_contact_index(conn)
        self._cache.schema_done = True
        # 哈希链游标：从最后一条续上，保证跨进程重启后链仍然连续
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT chain_hash FROM {TABLE_REVIEW} ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self._cache.review_chain.last_hash = "" if row is None else str(row[0])

    @staticmethod
    def _migrate_contact_index(conn: sqlite3.Connection) -> None:
        """老库迁移：把全量唯一索引 `uq_contact_value` 换成带软删守卫的那版。

        ## 顺序刻意是"先建新、再删旧"

        反过来（先删后建）会留下一个**无约束窗口**：万一建新索引失败，
        库里就再也没有"一个邮箱只能绑一个账号"的保护了，而且**没有任何报错**
        （约束只是消失，写入照样成功）。先建新的，则在最坏情况下
        只是"新旧索引并存"（多占一点空间），保护始终在。

        旧索引名与新索引名**必须不同** —— SQLite 的
        `CREATE ... IF NOT EXISTS` 遇到同名索引会直接跳过，
        于是"老库永远用不上新定义"这件事故意不会发生。
        """
        names = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name=? ", (TABLE_CONTACT,))}
        if "uq_contact_value" in names:
            conn.execute("DROP INDEX IF EXISTS uq_contact_value")

    def _ready(self) -> None:
        if not self._cache.schema_done:
            self.ensure_schema()

    def clear_all(self) -> None:
        """清空所有认证表的数据（**仅测试用**）。

        为什么需要它：认证服务是**进程内单例**（`get_auth_service()`），
        多个用例共用同一个库时会互相污染 —— 实测表现为"第二个用例登录时
        发现 12 个会话""重发限流莫名不生效"这类难以归因的失败。
        让测试显式清库，比让每个用例猜自己的前置状态可靠得多。

        ⚠️ **不在生产路径上调用**：它不做任何权限校验，是给夹具用的。
        """
        self.ensure_schema()
        tables = (
            TABLE_USER, TABLE_CREDENTIAL, TABLE_CONTACT, TABLE_SESSION,
            TABLE_VCODE, TABLE_REMEMBER, TABLE_RESET, TABLE_REVIEW,
            TABLE_NOTIFY_USAGE, TABLE_NOTIFY_LOG,
        )
        with self._connect() as conn:
            for table in tables:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 表名来自本模块常量
        # 哈希链游标也要重置，否则下一个用例的链从上一个用例续着
        self._cache.review_chain.last_hash = ""

    # ---------------- 用户 ----------------

    def create_user(
        self, *, user_id: str, username: str, display_name: str,
        status: str, valid_until: str, applied_tier: str = "trial",
        tenant_id: str = "",
    ) -> UserRecord:
        """建号。`valid_until` 必填 —— **不允许永久账号**（§8.6.1.2 纪律 1）。"""
        if not str(valid_until or "").strip():
            raise ValueError("valid_until 必填：不允许创建无有效期的账号")
        now = iso(utc_now())
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_USER} "
                f"(user_id, username, display_name, status, applied_tier, "
                f" valid_until, created_at, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, display_name, status, applied_tier,
                 valid_until, now, now))
        return UserRecord(
            user_id=user_id, username=username, display_name=display_name,
            status=status, applied_tier=applied_tier, valid_until=valid_until,
            created_at=now)

    def get_user(self, user_id: str) -> UserRecord | None:
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_USER} WHERE user_id = ?", (user_id,)
            ).fetchone()
        return None if row is None else self._to_user(row)

    def get_user_by_username(self, username: str) -> UserRecord | None:
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_USER} "
                f"WHERE username = ? AND deleted_at IS NULL",
                (str(username or "").strip(),)).fetchone()
        return None if row is None else self._to_user(row)

    def set_user_tier_and_validity(
        self, user_id: str, *, tier: str = "", valid_until: str = "",
        display_name: str = "", operator: str = "", ip: str = "",
        note: str = "",
    ) -> UserRecord | None:
        """改**套餐等级**与**有效期**（管理台核心动作）+ 写审批流水。

        ## 为什么单独开一个方法而不是塞进 `update_user_status`

        两者语义不同、留痕动作名也不同：
          - `update_user_status` 管**状态机**（pending→active→disabled→deleted）；
          - 本方法管**授权参数**（给哪个等级、到什么时候）。
        合成一个方法后，"谁在什么时候把某人从试用升成 VIP"这件事在审批流水里
        会和"启用了账号"混成同一条记录，审计时分辨不出来。
        """
        self._ready()
        before = self.get_user(user_id)
        if before is None:
            return None
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [iso(utc_now())]
        changes: list[str] = []
        if tier:
            fields.append("applied_tier = ?")
            params.append(tier)
            changes.append(f"tier:{before.applied_tier or '-'}→{tier}")
        if valid_until:
            fields.append("valid_until = ?")
            params.append(valid_until)
            changes.append(f"valid_until:{before.valid_until[:10] or '-'}"
                           f"→{valid_until[:10]}")
        if display_name:
            fields.append("display_name = ?")
            params.append(display_name)
            changes.append("display_name")
        if len(fields) == 1:      # 只有 updated_at，说明没有任何改动
            return before
        params.append(user_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_USER} SET {', '.join(fields)} "
                f"WHERE user_id = ?", params)
        self._record_review(
            user_id=user_id,
            action="admin:tier" if tier else "admin:validity",
            from_status=before.status, to_status=before.status,
            reviewer_id=operator, tier_code=tier,
            valid_until=valid_until or "",
            note=note or "；".join(changes), ip=ip)
        return self.get_user(user_id)

    def count_users_by_status(self) -> dict[str, int]:
        """各状态的用户数（管理台顶部"待审批 N 人"徽标用）。

        单独做一次聚合，而不是让前端拉全表自己数：待审批人数要在
        **每次进入管理台**时立刻显示，为显示一个数字而拉 500 行太浪费。
        """
        self._ready()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT status, COUNT(*) AS n FROM {TABLE_USER} "
                f"WHERE deleted_at IS NULL GROUP BY status").fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def count_active_sessions_by_user(self) -> dict[str, int]:
        """每个用户当前的活跃会话数（管理台列表里"几台设备在线"那一列）。

        **一次聚合查完整张列表**，而不是每行一次查询：管理台一屏 200 行，
        逐行查就是 200 次 SQLite 往返（在 6.8 GB 库上这种 N+1 很明显）。
        """
        self._ready()
        now = iso(utc_now())
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT user_id, COUNT(*) AS n FROM {TABLE_SESSION} "
                f"WHERE revoked_at IS NULL AND absolute_expires_at > ? "
                f"GROUP BY user_id", (now,)).fetchall()
        return {str(r["user_id"]): int(r["n"]) for r in rows}

    def update_user_status(
        self, user_id: str, status: str, *, valid_until: str | None = None,
        reviewed_by: str = "", review_note: str = "", operator: str = "",
        action: str = "", note: str = "", ip: str = "",
    ) -> UserRecord | None:
        """改状态（可选同时改有效期）+ **写审批流水**（一份动作两处留痕）。

        `action` 缺省时按状态推导，保证流水一定有可读的动作名。
        """
        self._ready()
        before = self.get_user(user_id)
        if before is None:
            return None
        fields = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, iso(utc_now())]
        if valid_until is not None:
            fields.append("valid_until = ?")
            params.append(valid_until)
        if reviewed_by:
            fields.extend(["reviewed_by = ?", "reviewed_at = ?"])
            params.extend([reviewed_by, iso(utc_now())])
        if review_note:
            fields.append("review_note = ?")
            params.append(review_note)
        if status == "deleted":
            fields.extend(["deleted_at = ?", "deleted_by = ?", "delete_reason = ?"])
            params.extend([iso(utc_now()), operator or reviewed_by, note])
        params.append(user_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_USER} SET {', '.join(fields)} WHERE user_id = ?",
                params)
            if status == "deleted":
                # ★ 注销账号时**必须同时软删联系方式**，否则那个邮箱被永久占死：
                #   唯一索引 `uq_contact_value_live ... WHERE deleted_at IS NULL`
                #   只看 contact 这一侧，用户表被删了它并不知道 ——
                #   表现为"注销后再也无法用同一邮箱注册"，报错还是
                #   `UNIQUE constraint failed`（用户看不懂自己在撞什么）。
                conn.execute(
                    f"UPDATE {TABLE_CONTACT} SET deleted_at = ?, is_primary = 0 "
                    f"WHERE user_id = ? AND deleted_at IS NULL",
                    (iso(utc_now()), user_id))
        self._record_review(
            user_id=user_id,
            action=action or f"status:{status}",
            from_status=before.status, to_status=status,
            reviewer_id=reviewed_by or operator, note=note or review_note,
            valid_until=valid_until or "", ip=ip)
        return self.get_user(user_id)

    def list_users(
        self, *, tenant_id: str = "", status: str = "", keyword: str = "",
        include_deleted: bool = False, limit: int = 200,
    ) -> list[UserRecord]:
        """用户列表。

        ⚠️ **默认排除已软删除**（§8.6.10.3 规则 5）：否则删掉的用户永远混在列表里。
        """
        self._ready()
        where = ["1=1"]
        params: list[Any] = []
        if not include_deleted:
            where.append("deleted_at IS NULL")
        if status:
            where.append("status = ?")
            params.append(status)
        if keyword:
            where.append("(username LIKE ? OR display_name LIKE ? OR user_id LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {TABLE_USER} WHERE {' AND '.join(where)} "
                f"ORDER BY valid_until ASC, created_at DESC LIMIT ?",
                params).fetchall()
        return [self._to_user(row) for row in rows]

    # ---------------- 凭证（密码） ----------------

    def set_password(
        self, user_id: str, password: str, *, keep_history: int = 5,
        must_change: bool = False,
    ) -> None:
        """设置密码并**记入历史**（防改回旧密码）。

        `keep_history` 默认 5 —— 与 §8.6.5③ 的口径一致。
        """
        hashed, algo = hash_password(password)
        now = iso(utc_now())
        self._ready()
        history = self._history(user_id)
        history.append(hashed)
        history = history[-max(0, keep_history):]
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_CREDENTIAL} "
                f"(user_id, password_hash, password_algo, password_updated_at, "
                f" must_change_password, failed_attempts, locked_until, "
                f" password_history_json) "
                f"VALUES (?, ?, ?, ?, ?, 0, NULL, ?) "
                f"ON CONFLICT(user_id) DO UPDATE SET "
                f"password_hash=excluded.password_hash, "
                f"password_algo=excluded.password_algo, "
                f"password_updated_at=excluded.password_updated_at, "
                f"must_change_password=excluded.must_change_password, "
                f"failed_attempts=0, locked_until=NULL, "
                f"password_history_json=excluded.password_history_json",
                (user_id, hashed, algo, now, 1 if must_change else 0,
                 json.dumps(history, ensure_ascii=False)))

    def password_was_used(self, user_id: str, password: str) -> bool:
        """该密码是否在最近的历史里（含当前密码）—— 用于"禁止改回旧密码"。"""
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT password_hash, password_history_json "
                f"FROM {TABLE_CREDENTIAL} WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return False
        candidates = [str(row["password_hash"])] + [
            str(x) for x in _loads(row["password_history_json"], [])]
        return any(verify_password(password, item) for item in candidates if item)

    def _history(self, user_id: str) -> list[str]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT password_history_json FROM {TABLE_CREDENTIAL} "
                f"WHERE user_id = ?", (user_id,)).fetchone()
        return [str(x) for x in _loads(row["password_history_json"], [])] \
            if row is not None else []

    def get_credential(self, user_id: str) -> dict[str, Any] | None:
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_CREDENTIAL} WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["password_history"] = _loads(data.pop("password_history_json", "[]"), [])
        return data

    def record_login_failure(
        self, user_id: str, *, threshold: int = 5, lock_seconds: float = 900,
        ip: str = "",
    ) -> int:
        """记一次失败；达到阈值则**临时锁定**（按账号记，不按 IP）。

        返回当前失败次数。按账号记的理由：按 IP 会被"换 IP 继续撞"绕过
        （IP 维度的限流另有一层，两者互补）。
        """
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT failed_attempts FROM {TABLE_CREDENTIAL} WHERE user_id = ?",
                (user_id,)).fetchone()
            current = int(row["failed_attempts"]) + 1 if row is not None else 1
            locked = iso_in(lock_seconds) if current >= threshold else None
            if row is None:
                conn.execute(
                    f"INSERT INTO {TABLE_CREDENTIAL} "
                    f"(user_id, password_hash, password_updated_at, failed_attempts,"
                    f" locked_until, last_failed_at, last_failed_ip) "
                    f"VALUES (?, '', ?, ?, ?, ?, ?)",
                    (user_id, iso(utc_now()), current, locked,
                     iso(utc_now()), ip))
            else:
                conn.execute(
                    f"UPDATE {TABLE_CREDENTIAL} SET failed_attempts = ?, "
                    f"locked_until = ?, last_failed_at = ?, last_failed_ip = ? "
                    f"WHERE user_id = ?",
                    (current, locked, iso(utc_now()), ip, user_id))
        return current

    def clear_login_failures(self, user_id: str) -> None:
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_CREDENTIAL} SET failed_attempts = 0, "
                f"locked_until = NULL WHERE user_id = ?", (user_id,))

    # ---------------- 联系方式（邮箱/手机号） ----------------

    def bind_contact(
        self, *, user_id: str, kind: str, value: str, tenant_id: str = "",
        verified: bool = True, replace_primary: bool = True,
    ) -> ContactRecord:
        """绑定/换绑联系方式。

        `replace_primary=True` 时会先把同类的主联系方式降级 —— 保证
        "一个 kind 只有一条主记录"，避免"邮箱换了但找回还发旧邮箱"。
        """
        normalized = (normalize_email(value) if kind == "email"
                      else normalize_phone(value))
        if not normalized:
            raise ValueError(f"{kind} 不能为空")
        digest = sha256_hex(f"{kind}:{normalized}")
        self._ready()
        with self._connect() as conn:
            if replace_primary:
                conn.execute(
                    f"UPDATE {TABLE_CONTACT} SET is_primary = 0 "
                    f"WHERE user_id = ? AND kind = ?", (user_id, kind))
            # ★ ON CONFLICT 分支里必须把 `deleted_at` 清回 NULL（"复活"）：
            #   同一行若曾被软删，部分唯一索引 `WHERE deleted_at IS NULL` 会认为
            #   它仍然"不存在"，于是同一邮箱能被绑到第二个账号上 —— 那正是要防的事。
            conn.execute(
                f"INSERT INTO {TABLE_CONTACT} "
                f"(tenant_id, user_id, kind, value, value_hash, is_primary, "
                f" verified_at, created_at, deleted_at) "
                f"VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL) "
                f"ON CONFLICT(user_id, kind, value_hash) DO UPDATE SET "
                f"value=excluded.value, is_primary=1, deleted_at=NULL, "
                f"verified_at=COALESCE(excluded.verified_at, "
                f"                      {TABLE_CONTACT}.verified_at)",
                (tenant_id, user_id, kind, normalized, digest,
                 iso(utc_now()) if verified else None, iso(utc_now())))
        return ContactRecord(user_id=user_id, kind=kind, value=normalized,
                             value_hash=digest,
                             verified_at=iso(utc_now()) if verified else "")

    def unbind_contact(self, *, user_id: str, kind: str, value: str) -> bool:
        """解绑（**软删**）：释放该邮箱/手机号，允许它重新注册。

        为什么不物理删除：注册审批链与通知日志要能追溯"这个邮箱曾属于谁"，
        物理删除会让审计链断在这里（与 `dim_user` 的软删口径一致）。
        """
        normalized = (normalize_email(value) if kind == "email"
                      else normalize_phone(value))
        if not normalized:
            return False
        digest = sha256_hex(f"{kind}:{normalized}")
        self._ready()
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE {TABLE_CONTACT} SET deleted_at = ?, is_primary = 0 "
                f"WHERE user_id = ? AND kind = ? AND value_hash = ? "
                f"AND deleted_at IS NULL",
                (iso(utc_now()), user_id, kind, digest))
            return bool(cur.rowcount)

    def find_user_by_contact(self, kind: str, value: str) -> ContactRecord | None:
        """按联系方式反查（**走盲索引**，不解密、不扫描）。

        ⚠️ 必须带 `deleted_at IS NULL`：软删的联系方式属于"历史"，
        拿它反查到已注销账号会让"注销后重新注册"变成"登进了老账号"。
        """
        normalized = (normalize_email(value) if kind == "email"
                      else normalize_phone(value))
        if not normalized:
            return None
        digest = sha256_hex(f"{kind}:{normalized}")
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_CONTACT} "
                f"WHERE kind = ? AND value_hash = ? "
                f"AND deleted_at IS NULL", (kind, digest)).fetchone()
        return None if row is None else self._to_contact(row)

    def list_contacts(self, user_id: str, kind: str = "") -> list[ContactRecord]:
        self._ready()
        sql = (f"SELECT * FROM {TABLE_CONTACT} WHERE user_id = ? "
               f"AND deleted_at IS NULL")
        params: list[Any] = [user_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY is_primary DESC, created_at ASC",
                                params).fetchall()
        return [self._to_contact(row) for row in rows]

    def primary_contact(self, user_id: str, kind: str) -> ContactRecord | None:
        """取已验证的主联系方式 —— **发验证码前必须用它**，不能拿用户输入的值直接发。"""
        for item in self.list_contacts(user_id, kind):
            if item.is_primary and item.verified:
                return item
        for item in self.list_contacts(user_id, kind):   # 退化：任一已验证
            if item.verified:
                return item
        return None

    # ---------------- 会话 ----------------

    def create_session(
        self, *, session_id: str, user_id: str, tenant_id: str,
        access_jti: str, refresh_hash: str, idle_seconds: float,
        absolute_seconds: float, console: str = "default",
        device_label: str = "", device_id: str = "", user_agent: str = "",
        ip: str = "",
    ) -> SessionRecord:
        now = iso(utc_now())
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_SESSION} "
                f"(session_id, user_id, tenant_id, console, access_jti, "
                f" refresh_hash, created_at, last_seen_at, idle_expires_at, "
                f" absolute_expires_at, device_label, device_id, user_agent, ip) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, tenant_id, console, access_jti,
                 refresh_hash, now, now, iso_in(idle_seconds),
                 iso_in(absolute_seconds), device_label, device_id,
                 user_agent, ip))
        return SessionRecord(
            session_id=session_id, user_id=user_id, tenant_id=tenant_id,
            access_jti=access_jti, created_at=now, last_seen_at=now,
            idle_expires_at=iso_in(idle_seconds),
            absolute_expires_at=iso_in(absolute_seconds),
            console=console, device_label=device_label, ip=ip)

    def get_session(self, session_id: str) -> SessionRecord | None:
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_SESSION} WHERE session_id = ?",
                (session_id,)).fetchone()
        return None if row is None else self._to_session(row)

    def touch_session(
        self, session_id: str, *, idle_seconds: float,
    ) -> SessionRecord | None:
        """滑动续期：**只推 `idle_expires_at`，绝不延长 `absolute_expires_at`**。

        这一条是"会话永不失效"与"用户每 30 分钟被踢"之间的分界线（§8.6.11.3）。
        """
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_SESSION} SET last_seen_at = ?, "
                f"idle_expires_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                (iso(utc_now()), iso_in(idle_seconds), session_id))
        return self.get_session(session_id)

    def revoke_session(self, session_id: str, reason: str) -> bool:
        self._ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {TABLE_SESSION} SET revoked_at = ?, revoke_reason = ? "
                f"WHERE session_id = ? AND revoked_at IS NULL",
                (iso(utc_now()), reason, session_id))
            return bool(cursor.rowcount)

    def revoke_user_sessions(
        self, user_id: str, reason: str, *, except_session: str = "",
    ) -> int:
        """撤销该用户全部会话（可保留当前这一个）。

        **改密码/重置密码后必须调用**（§8.6.5③⑥）：
        用户改密的第一动机就是"怀疑被盗"，不踢会话等于没改。
        """
        self._ready()
        now = iso(utc_now())
        sql = (f"UPDATE {TABLE_SESSION} SET revoked_at = ?, revoke_reason = ? "
               f"WHERE user_id = ? AND revoked_at IS NULL")
        params: list[Any] = [now, reason, user_id]
        if except_session:
            sql += " AND session_id <> ?"
            params.append(except_session)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.rowcount)

    def list_sessions(
        self, user_id: str, *, include_revoked: bool = False,
    ) -> list[SessionRecord]:
        """活跃设备列表（**用户自救通道**：看清楚谁登着，然后一键踢）。"""
        self._ready()
        sql = f"SELECT * FROM {TABLE_SESSION} WHERE user_id = ?"
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        with self._connect() as conn:
            rows = conn.execute(
                sql + " ORDER BY last_seen_at DESC", (user_id,)).fetchall()
        return [self._to_session(row) for row in rows]

    def count_active_sessions(self, user_id: str) -> int:
        return sum(1 for s in self.list_sessions(user_id) if s.is_valid())

    # ---------------- 验证码 ----------------

    def issue_code(
        self, *, scene: str, target: str, code: str, purpose_user: str = "",
        ttl_seconds: float = 300, max_attempts: int = 5, request_ip: str = "",
        channel: str = "email",
    ) -> int:
        """落一条验证码（**存哈希**）。同一 (scene,target) 的旧码立即作废。"""
        digest = sha256_hex(f"{scene}:{target}")
        self._ready()
        now = iso(utc_now())
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_VCODE} SET used_at = ? "
                f"WHERE scene = ? AND target_hash = ? AND used_at IS NULL",
                (now, scene, digest))
            cursor = conn.execute(
                f"INSERT INTO {TABLE_VCODE} "
                f"(scene, target_hash, code_hash, purpose_user, attempts, "
                f" max_attempts, sent_at, expires_at, request_ip, send_channel) "
                f"VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (scene, digest, sha256_hex(code), purpose_user, max_attempts,
                 now, iso_in(ttl_seconds), request_ip, channel))
            return int(cursor.lastrowid or 0)

    def latest_code(self, scene: str, target: str) -> VerifyCodeRecord | None:
        digest = sha256_hex(f"{scene}:{target}")
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_VCODE} "
                f"WHERE scene = ? AND target_hash = ? ORDER BY id DESC LIMIT 1",
                (scene, digest)).fetchone()
        return None if row is None else self._to_vcode(row)

    def discard_code(self, record_id: int) -> None:
        """直接作废一条验证码（外发失败时调用）。

        为什么需要它：发送失败后若把码留在库，就出现
        **"用户没收到、但码仍然有效"** 的窗口 —— 那段时间里
        任何拿到日志/泄漏的人都可能用它完成注册或重置。
        """
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_VCODE} SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (iso(utc_now()), int(record_id)))

    def consume_code(
        self, record_id: int, code: str, *, now_used: bool = True,
    ) -> tuple[bool, str]:
        """校验并消费验证码。返回 `(是否通过, 原因)`。

        ⚠️ **失败也要 `attempts += 1`** —— 否则"错 5 次作废"形同虚设，
        攻击者可以无限次猜（6 位数字只有 100 万种，不限次就能暴力枚举）。
        """
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_VCODE} WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return False, "验证码不存在"
            record = self._to_vcode(row)
            if record.used_at:
                return False, "验证码已被使用"
            if not is_future(record.expires_at):
                return False, "验证码已过期"
            if record.attempts >= record.max_attempts:
                return False, "验证码尝试次数过多，请重新获取"
            if not verify_code_hash(code, record.code_hash):
                conn.execute(
                    f"UPDATE {TABLE_VCODE} SET attempts = attempts + 1 WHERE id = ?",
                    (record_id,))
                return False, "验证码不正确"
            if now_used:
                conn.execute(
                    f"UPDATE {TABLE_VCODE} SET used_at = ? WHERE id = ?",
                    (iso(utc_now()), record_id))
        return True, ""

    # ---------------- 记住我（记住密码） ----------------

    def issue_remember(
        self, *, user_id: str, tenant_id: str, days: float = 7.0,
        family_id: str = "", rotated_from: str = "", device_label: str = "",
        user_agent: str = "", ip: str = "",
    ) -> tuple[str, str]:
        """发 remember token。返回 `(明文令牌, family_id)` —— **明文只此一次**。

        窗口默认 **7 天**（§8.6.11.1 已确认的"短期不看"口径）。
        """
        token = new_token()
        family = family_id or new_token(8)
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_REMEMBER} "
                f"(family_id, token_hash, user_id, tenant_id, issued_at, "
                f" expires_at, rotated_from, device_label, user_agent, ip) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (family, sha256_hex(token), user_id, tenant_id, iso(utc_now()),
                 iso_in(days * 86400), rotated_from or None, device_label,
                 user_agent, ip))
        return token, family

    def consume_remember(self, token: str) -> tuple[dict[str, Any] | None, str]:
        """兑换 remember token。返回 `(记录, 原因)`；记录为 None 表示失败。

        ★ **重放检测**：令牌被用过（`rotated_from` 指向它 / `revoked_at` 有值）
        却再次出现 → 判定为"令牌被窃取后重放" → 调用方应**撤销整个 family**
        （§8.6.5④）。这里只负责识别，撤销动作由服务层做（保持仓储无业务策略）。
        """
        self._ready()
        digest = sha256_hex(token)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_REMEMBER} WHERE token_hash = ?",
                (digest,)).fetchone()
        if row is None:
            return None, "令牌不存在"
        record = dict(row)
        if record.get("revoked_at"):
            return None, "replay"          # 已撤销却再次出现 = 重放信号
        if not is_future(record.get("expires_at")):
            return None, "令牌已过期"
        return record, ""

    def mark_remember_rotated(self, token: str) -> None:
        """把旧令牌标记为已轮换（作废）。新令牌由 `issue_remember(rotated_from=)` 发出。"""
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_REMEMBER} SET revoked_at = ? "
                f"WHERE token_hash = ? AND revoked_at IS NULL",
                (iso(utc_now()), sha256_hex(token)))

    def revoke_remember_family(self, family_id: str) -> int:
        self._ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {TABLE_REMEMBER} SET revoked_at = ? "
                f"WHERE family_id = ? AND revoked_at IS NULL",
                (iso(utc_now()), family_id))
            return int(cursor.rowcount)

    def family_of_remember(self, token_hash: str) -> str:
        """按 **token 哈希** 反查它所属的 family。

        为什么需要独立方法：`consume_remember` 在识别出重放时会返回 `None`
        （它不该替调用方决定"撤谁"），而调用方要撤整条 family ——
        所以需要一个"即使令牌已失效也能查归属"的只读入口。
        """
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT family_id FROM {TABLE_REMEMBER} WHERE token_hash = ?",
                (str(token_hash),)).fetchone()
        return "" if row is None else str(row["family_id"])

    def revoke_user_remembers(self, user_id: str) -> int:
        """撤销该用户全部 remember token（改密/重置后必做，§8.6.5③⑤）。"""
        self._ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {TABLE_REMEMBER} SET revoked_at = ? "
                f"WHERE user_id = ? AND revoked_at IS NULL",
                (iso(utc_now()), user_id))
            return int(cursor.rowcount)

    # ---------------- 密码重置 ----------------

    def issue_reset(
        self, *, user_id: str, channel: str, ttl_seconds: float = 900,
        request_ip: str = "",
    ) -> str:
        """发一次性重置令牌（**必须先过验证码** —— `verified_at` 是必填语义）。"""
        token = new_token()
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_RESET} SET used_at = ? "
                f"WHERE user_id = ? AND used_at IS NULL",
                (iso(utc_now()), user_id))
            conn.execute(
                f"INSERT INTO {TABLE_RESET} "
                f"(user_id, token_hash, channel, verified_at, expires_at, "
                f" request_ip) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, sha256_hex(token), channel, iso(utc_now()),
                 iso_in(ttl_seconds), request_ip))
        return token

    def consume_reset(self, token: str) -> tuple[str | None, str]:
        """兑换重置令牌（一次性 + 15 分钟）。返回 `(user_id, 原因)`。"""
        self._ready()
        digest = sha256_hex(token)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_RESET} WHERE token_hash = ?",
                (digest,)).fetchone()
            if row is None:
                return None, "令牌不存在"
            if row["used_at"]:
                return None, "令牌已被使用"
            if not is_future(row["expires_at"]):
                return None, "令牌已过期"
            conn.execute(
                f"UPDATE {TABLE_RESET} SET used_at = ? WHERE id = ?",
                (iso(utc_now()), row["id"]))
            return str(row["user_id"]), ""

    def discard_reset(self, token: str) -> None:
        """作废一条重置令牌（外发失败时调用）—— `discard_code` 的重置版。

        为什么需要它：找回邮件里验证码与令牌**同封下发**，发送失败时若只作废
        验证码、把令牌留在库，就出现 **"用户没收到邮件、但令牌仍可兑换"** 的窗口
        —— 与 `discard_code` 要防的完全同类，只是对象换成了令牌。

        按 `token_hash` 定位（而不是 user_id）：调用点只有明文令牌，
        且同一用户可能有多条历史记录，按 user_id 会误伤其它批次。
        """
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"UPDATE {TABLE_RESET} SET used_at = ? "
                f"WHERE token_hash = ? AND used_at IS NULL",
                (iso(utc_now()), sha256_hex(token)))

    # ---------------- 审批流水 ----------------

    def _record_review(
        self, *, user_id: str, action: str, from_status: str, to_status: str,
        reviewer_id: str = "", note: str = "", tier_code: str = "",
        valid_until: str = "", ip: str = "",
    ) -> ReviewRecord:
        """写一条审批/生命周期流水（哈希链，与访问审计同思路）。"""
        payload = {
            "at": iso(utc_now()), "user_id": user_id, "action": action,
            "from": from_status, "to": to_status, "tier": tier_code,
            "reviewer": reviewer_id, "note": note,
        }
        chain_hash = self._cache.review_chain.next_hash(payload)
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_REVIEW} "
                f"(at, user_id, action, from_status, to_status, tier_code, "
                f" valid_until, reviewer_id, note, ip, chain_hash) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (payload["at"], user_id, action, from_status, to_status,
                 tier_code, valid_until or None, reviewer_id, note, ip,
                 chain_hash))
        return ReviewRecord(at=payload["at"], user_id=user_id, action=action,
                            from_status=from_status, to_status=to_status,
                            tier_code=tier_code, valid_until=valid_until,
                            reviewer_id=reviewer_id, note=note,
                            chain_hash=chain_hash)

    def list_reviews(self, user_id: str = "", *, limit: int = 100) -> list[ReviewRecord]:
        self._ready()
        sql = f"SELECT * FROM {TABLE_REVIEW}"
        params: list[Any] = []
        if user_id:
            sql += " WHERE user_id = ?"
            params.append(user_id)
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", params).fetchall()
        return [ReviewRecord(
            at=str(r["at"]), user_id=str(r["user_id"]), action=str(r["action"]),
            from_status=str(r["from_status"]), to_status=str(r["to_status"]),
            tier_code=str(r["tier_code"]), valid_until=str(r["valid_until"] or ""),
            reviewer_id=str(r["reviewer_id"]), note=str(r["note"]),
            chain_hash=str(r["chain_hash"])) for r in rows]

    # ---------------- 通知用量与日志 ----------------

    def usage(self, tenant_id: str, channel: str,
              period: str | None = None) -> NotifyUsage:
        key = period or period_of()
        self._ready()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_NOTIFY_USAGE} "
                f"WHERE tenant_id = ? AND channel = ? AND period = ?",
                (tenant_id, channel, key)).fetchone()
        if row is None:
            return NotifyUsage(tenant_id=tenant_id, channel=channel, period=key)
        return NotifyUsage(tenant_id=tenant_id, channel=channel, period=key,
                           sent_count=int(row["sent_count"]),
                           failed_count=int(row["failed_count"]),
                           cost_yuan=float(row["cost_yuan"]))

    def add_usage(
        self, tenant_id: str, channel: str, *, sent: int = 0, failed: int = 0,
        cost_yuan: float = 0.0, period: str | None = None,
    ) -> NotifyUsage:
        key = period or period_of()
        self._ready()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_NOTIFY_USAGE} "
                f"(tenant_id, channel, period, sent_count, failed_count, "
                f" cost_yuan, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(tenant_id, channel, period) DO UPDATE SET "
                f"sent_count = {TABLE_NOTIFY_USAGE}.sent_count + excluded.sent_count, "
                f"failed_count = {TABLE_NOTIFY_USAGE}.failed_count + excluded.failed_count, "
                f"cost_yuan = {TABLE_NOTIFY_USAGE}.cost_yuan + excluded.cost_yuan, "
                f"updated_at = excluded.updated_at",
                (tenant_id, channel, key, sent, failed, cost_yuan,
                 iso(utc_now())))
        return self.usage(tenant_id, channel, key)

    def log_notify(
        self, *, channel: str, target: str, scene: str, status: str,
        tenant_id: str = "", user_id: str = "", session_id: str = "",
        provider: str = "", provider_msg_id: str = "", unit_price: float = 0.0,
        error_code: str = "", error_msg: str = "",
    ) -> int:
        """逐条外发记录。`target` 只存哈希（**不落明文地址**）。"""
        self._ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO {TABLE_NOTIFY_LOG} "
                f"(at, tenant_id, user_id, session_id, channel, target_hash, "
                f" scene, provider, provider_msg_id, unit_price, status, "
                f" error_code, error_msg) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (iso(utc_now()), tenant_id, user_id, session_id, channel,
                 sha256_hex(target), scene, provider, provider_msg_id,
                 unit_price, status, error_code, error_msg))
            return int(cursor.lastrowid or 0)

    def notify_log(self, *, tenant_id: str = "", channel: str = "",
                   limit: int = 200) -> list[dict[str, Any]]:
        self._ready()
        where, params = ["1=1"], []
        if tenant_id:
            where.append("tenant_id = ?")
            params.append(tenant_id)
        if channel:
            where.append("channel = ?")
            params.append(channel)
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {TABLE_NOTIFY_LOG} WHERE {' AND '.join(where)} "
                f"ORDER BY id DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 行映射 ----------------

    @staticmethod
    def _to_user(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            user_id=str(row["user_id"]), username=str(row["username"]),
            display_name=str(row["display_name"]), status=str(row["status"]),
            applied_tier=str(row["applied_tier"]),
            valid_until=str(row["valid_until"]),
            created_at=str(row["created_at"]),
            reviewed_by=str(row["reviewed_by"] or ""),
            reviewed_at=str(row["reviewed_at"] or ""),
            review_note=str(row["review_note"] or ""),
            valid_from=str(row["valid_from"] or ""),
            deleted_at=str(row["deleted_at"] or ""),
            delete_reason=str(row["delete_reason"] or ""))

    @staticmethod
    def _to_session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=str(row["session_id"]), user_id=str(row["user_id"]),
            tenant_id=str(row["tenant_id"]), access_jti=str(row["access_jti"]),
            created_at=str(row["created_at"]),
            last_seen_at=str(row["last_seen_at"]),
            idle_expires_at=str(row["idle_expires_at"]),
            absolute_expires_at=str(row["absolute_expires_at"]),
            console=str(row["console"] or "default"),
            revoked_at=str(row["revoked_at"] or ""),
            revoke_reason=str(row["revoke_reason"] or ""),
            device_label=str(row["device_label"] or ""),
            ip=str(row["ip"] or ""))

    @staticmethod
    def _to_contact(row: sqlite3.Row) -> ContactRecord:
        return ContactRecord(
            user_id=str(row["user_id"]), kind=str(row["kind"]),
            value=str(row["value"]), value_hash=str(row["value_hash"]),
            verified_at=str(row["verified_at"] or ""),
            is_primary=bool(row["is_primary"]))

    @staticmethod
    def _to_vcode(row: sqlite3.Row) -> VerifyCodeRecord:
        return VerifyCodeRecord(
            id=int(row["id"]), scene=str(row["scene"]),
            target_hash=str(row["target_hash"]), code_hash=str(row["code_hash"]),
            attempts=int(row["attempts"]), max_attempts=int(row["max_attempts"]),
            sent_at=str(row["sent_at"]), expires_at=str(row["expires_at"]),
            used_at=str(row["used_at"] or ""))

    # ---------------- 异步端口 ----------------
    #
    # 与项目既有仓储一致：同步实现 + to_thread 包装。
    # sqlite3 调用是阻塞的，直接在事件循环里跑会把并发压成串行（§7.1 的教训）。

    async def a_ensure_schema(self) -> None:
        await asyncio.to_thread(self.ensure_schema)

    async def a_create_user(self, **kwargs: Any) -> UserRecord:
        return await asyncio.to_thread(lambda: self.create_user(**kwargs))

    async def a_get_user(self, user_id: str) -> UserRecord | None:
        return await asyncio.to_thread(self.get_user, user_id)

    async def a_get_user_by_username(self, username: str) -> UserRecord | None:
        return await asyncio.to_thread(self.get_user_by_username, username)

    async def a_update_user_status(self, user_id: str, status: str,
                                   **kwargs: Any) -> UserRecord | None:
        return await asyncio.to_thread(
            lambda: self.update_user_status(user_id, status, **kwargs))

    async def a_list_users(self, **kwargs: Any) -> list[UserRecord]:
        return await asyncio.to_thread(lambda: self.list_users(**kwargs))

    async def a_set_password(self, user_id: str, password: str,
                             **kwargs: Any) -> None:
        await asyncio.to_thread(
            lambda: self.set_password(user_id, password, **kwargs))

    async def a_password_was_used(self, user_id: str, password: str) -> bool:
        return await asyncio.to_thread(self.password_was_used, user_id, password)

    async def a_get_credential(self, user_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_credential, user_id)

    async def a_record_login_failure(self, user_id: str, **kwargs: Any) -> int:
        return await asyncio.to_thread(
            lambda: self.record_login_failure(user_id, **kwargs))

    async def a_clear_login_failures(self, user_id: str) -> None:
        await asyncio.to_thread(self.clear_login_failures, user_id)

    async def a_bind_contact(self, **kwargs: Any) -> ContactRecord:
        return await asyncio.to_thread(lambda: self.bind_contact(**kwargs))

    async def a_find_user_by_contact(self, kind: str,
                                     value: str) -> ContactRecord | None:
        return await asyncio.to_thread(self.find_user_by_contact, kind, value)

    async def a_list_contacts(self, user_id: str,
                              kind: str = "") -> list[ContactRecord]:
        return await asyncio.to_thread(self.list_contacts, user_id, kind)

    async def a_primary_contact(self, user_id: str,
                               kind: str) -> ContactRecord | None:
        return await asyncio.to_thread(self.primary_contact, user_id, kind)

    async def a_create_session(self, **kwargs: Any) -> SessionRecord:
        return await asyncio.to_thread(lambda: self.create_session(**kwargs))

    async def a_get_session(self, session_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self.get_session, session_id)

    async def a_touch_session(self, session_id: str, **kwargs: Any
                              ) -> SessionRecord | None:
        return await asyncio.to_thread(
            lambda: self.touch_session(session_id, **kwargs))

    async def a_revoke_session(self, session_id: str, reason: str) -> bool:
        return await asyncio.to_thread(self.revoke_session, session_id, reason)

    async def a_revoke_user_sessions(self, user_id: str, reason: str,
                                     **kwargs: Any) -> int:
        return await asyncio.to_thread(
            lambda: self.revoke_user_sessions(user_id, reason, **kwargs))

    async def a_list_sessions(self, user_id: str, **kwargs: Any
                              ) -> list[SessionRecord]:
        return await asyncio.to_thread(
            lambda: self.list_sessions(user_id, **kwargs))

    async def a_issue_code(self, **kwargs: Any) -> int:
        return await asyncio.to_thread(lambda: self.issue_code(**kwargs))

    async def a_consume_code(self, record_id: int, code: str
                             ) -> tuple[bool, str]:
        return await asyncio.to_thread(self.consume_code, record_id, code)

    async def a_discard_code(self, record_id: int) -> None:
        await asyncio.to_thread(self.discard_code, record_id)

    async def a_latest_code(self, scene: str, target: str
                            ) -> VerifyCodeRecord | None:
        return await asyncio.to_thread(self.latest_code, scene, target)

    async def a_issue_remember(self, **kwargs: Any) -> tuple[str, str]:
        return await asyncio.to_thread(lambda: self.issue_remember(**kwargs))

    async def a_consume_remember(self, token: str
                                 ) -> tuple[dict[str, Any] | None, str]:
        return await asyncio.to_thread(self.consume_remember, token)

    async def a_mark_remember_rotated(self, token: str) -> None:
        await asyncio.to_thread(self.mark_remember_rotated, token)

    async def a_revoke_remember_family(self, family_id: str) -> int:
        return await asyncio.to_thread(self.revoke_remember_family, family_id)

    async def a_family_of_remember(self, token_hash: str) -> str:
        return await asyncio.to_thread(self.family_of_remember, token_hash)

    async def a_revoke_user_remembers(self, user_id: str) -> int:
        return await asyncio.to_thread(self.revoke_user_remembers, user_id)

    async def a_issue_reset(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(lambda: self.issue_reset(**kwargs))

    async def a_consume_reset(self, token: str) -> tuple[str | None, str]:
        return await asyncio.to_thread(self.consume_reset, token)

    async def a_discard_reset(self, token: str) -> None:
        return await asyncio.to_thread(self.discard_reset, token)

    async def a_list_reviews(self, user_id: str = "", **kwargs: Any
                             ) -> list[ReviewRecord]:
        return await asyncio.to_thread(
            lambda: self.list_reviews(user_id, **kwargs))

    async def a_usage(self, tenant_id: str, channel: str,
                      period: str | None = None) -> NotifyUsage:
        return await asyncio.to_thread(self.usage, tenant_id, channel, period)

    async def a_add_usage(self, tenant_id: str, channel: str,
                          **kwargs: Any) -> NotifyUsage:
        return await asyncio.to_thread(
            lambda: self.add_usage(tenant_id, channel, **kwargs))

    async def a_log_notify(self, **kwargs: Any) -> int:
        return await asyncio.to_thread(lambda: self.log_notify(**kwargs))

    async def a_notify_log(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(lambda: self.notify_log(**kwargs))


def _loads(raw: object, default: Any) -> Any:
    """JSON 列解析（脏数据返回默认值并告警，不让一行坏数据打断整表）。"""
    if not raw:
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        logger.warning("认证表含无法解析的 JSON 列，已按默认值处理：%s", str(raw)[:120])
        return default
    return value


__all__ = [
    "LOGIN_ALLOWED_STATUS",
    "TABLE_CONTACT",
    "TABLE_CREDENTIAL",
    "TABLE_NOTIFY_LOG",
    "TABLE_NOTIFY_USAGE",
    "TABLE_REMEMBER",
    "TABLE_RESET",
    "TABLE_REVIEW",
    "TABLE_SESSION",
    "TABLE_USER",
    "TABLE_VCODE",
    "AuthSqliteRepository",
    "ContactRecord",
    "NotifyUsage",
    "ReviewRecord",
    "SessionRecord",
    "UserRecord",
    "VerifyCodeRecord",
    "hash_password",
    "is_future",
    "iso",
    "iso_in",
    "mask_contact",
    "new_token",
    "normalize_email",
    "normalize_phone",
    "parse_iso",
    "period_of",
    "quota_exceeded",
    "sha256_hex",
    "utc_now",
    "verify_code_hash",
    "verify_password",
]
