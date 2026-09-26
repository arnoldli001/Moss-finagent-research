"""租户与身份上下文 —— 多租户隔离的**唯一可信来源**。

## 一条不能被打破的规则

**租户与身份只能由服务端从凭证派生，绝不能从请求体或裸请求头取。**

业界最常见的越权来源就是 `tenant_id = request.headers["X-Tenant-Id"]`：
客户端改一个字符串就能读到别人的数据。本模块因此**不提供**任何
"从请求头直接设置租户"的公开接口；接入层（`src/api/tenancy_middleware.py`）
必须先用凭证校验身份，再把**已校验的**结果交给 `principal_scope()`。

## 面向"量化部门内部系统"的模型

| 概念 | 取值 | 说明 |
|---|---|---|
| `tenant` | 部门 / 团队 | 行级隔离边界。共享表 + `tenant_id` 列 |
| `role` | 见 `Role` | RBAC 主体，决定能做什么动作 |
| `clearance` | 见 `DataClass` | ABAC 属性，决定能看什么等级的数据 |
| `wall_group` | 见 `WallGroup` | 信息隔离墙分组（研究 ↔ 投资 ↔ 风控） |

## 为什么 `clearance` 与 `role` 分开

"是不是合规岗"和"能看多机密的数据"是两件事：合规岗权限高但**不需要**看
未公开的策略参数；PM 权限低但需要看持仓。混成一个维度会出现"越权即升权"。
"""

from __future__ import annotations

import contextvars
import enum
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace


class DataClass(enum.IntEnum):
    """数据分级（与 `docs/SECURITY_COMPLIANCE.md` 的四级制一一对应）。

    用 `IntEnum` 是为了"比较"：`principal.clearance >= resource.data_class`。
    """

    PUBLIC = 0        # 常规一般数据：公开行情、已发布研报摘要
    INTERNAL = 1      # 敏感一般数据：个股分析报告、财务数据
    CONFIDENTIAL = 2  # 重要数据：产业链图谱、内部研报、策略参数
    RESTRICTED = 3    # 核心数据：未公开政策预判、核心策略逻辑、MNPI


class Role(str, enum.Enum):
    """岗位角色。命名与机构内部投研平台的常见划分对齐。"""

    RESEARCHER = "researcher"      # 研究员：跑分析、看自己与共享池
    PM = "pm"                      # 投资经理：看组合、看研究结论
    TRADER = "trader"              # 交易员：只看执行所需
    RISK = "risk"                  # 风控：看敞口与限额，不看策略源码
    COMPLIANCE = "compliance"      # 合规：看全部审计，不看策略源码
    AUDITOR = "auditor"            # 审计：只读，全租户可见
    ADMIN = "admin"                # 系统管理员：运维动作，不含业务数据

    @classmethod
    def parse(cls, value: object) -> Role:
        text = str(value or "").strip().lower()
        try:
            return cls(text)
        except ValueError as exc:
            raise TenantError(f"未知角色: {value!r}") from exc


class WallGroup(str, enum.Enum):
    """信息隔离墙分组（Chinese Wall）。

    《证券法》第 54 条禁止利用未公开信息交易；机构必须做到
    **研究部门与投资部门的信息隔离**。这里把它落成标签：
    同墙内可见对方的**在研结论**，跨墙只能看到**已发布**结论。
    """

    RESEARCH = "research"          # 研究（含行业研究、策略研究）
    INVESTMENT = "investment"      # 投资（PM / 交易）
    CONTROL = "control"            # 控制（风控 / 合规 / 审计）—— 天然跨墙
    PLATFORM = "platform"          # 平台（运维 / 数据工程）


class TenantError(PermissionError):
    """租户/身份上下文缺失或不合法。"""


class AccessDenied(TenantError):
    """已识别身份但权限不足。"""


@dataclass(frozen=True)
class Principal:
    """一次请求的身份快照。**不可变** —— 防止中途被提权。"""

    user_id: str
    tenant_id: str
    #: 会话标识（**sessionId 级隔离的锚点**，见
    #: `docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.11.4）。
    #:
    #: 为什么是一等字段而不是"塞进 groups"：
    #: 会话态（当前查看的池 / 未保存的草稿 / WS 订阅集合）必须按会话隔离，
    #: 而"同一用户的另一个会话该看不到它"这条规则要在仓储与缓存层可判。
    #: 默认空串 = 未经过会话认证的身份（如 `system_scope` 的后台任务）。
    session_id: str = ""
    roles: frozenset[Role] = field(default_factory=frozenset)
    clearance: DataClass = DataClass.PUBLIC
    wall_group: WallGroup = WallGroup.PLATFORM
    #: 分组/团队标签，用于 ABAC 的"同组可见"
    groups: frozenset[str] = field(default_factory=frozenset)
    #: 凭证来源（`jwt` / `mtls` / `local-dev`），进审计
    auth_source: str = "unknown"
    #: 是否为**已认证**身份。只有未认证哨兵为 False —— 让"忘了鉴权"
    #: 成为一个可检测的状态，而不是静默降级成某个真实租户。
    authenticated: bool = True

    def __post_init__(self) -> None:
        if self.authenticated and (not self.user_id or not self.tenant_id):
            raise TenantError("已认证的 Principal 必须带 user_id 与 tenant_id")

    @property
    def is_control(self) -> bool:
        """风控 / 合规 / 审计 —— 天然跨墙只读。"""
        return self.wall_group is WallGroup.CONTROL or bool(
            self.roles & {Role.RISK, Role.COMPLIANCE, Role.AUDITOR})

    @property
    def is_privileged(self) -> bool:
        return Role.ADMIN in self.roles or Role.AUDITOR in self.roles

    def has_role(self, *roles: Role) -> bool:
        return bool(self.roles & set(roles))

    def fiscal(self) -> Principal:
        """降权副本：给后台任务/子进程用，避免把高权限身份带出去。"""
        return replace(self, roles=frozenset(self.roles),
                       clearance=DataClass.PUBLIC)

    def with_session(self, session_id: str) -> Principal:
        """派生一个带会话标识的副本（**不可变**，不改原对象）。

        由接入层在验证 `session_id` 有效后调用 —— 这样"会话已校验"这件事
        在类型上就带着走，业务代码不需要也不应该自己拼。
        """
        return replace(self, session_id=str(session_id or ""))

    def session_audit_fields(self) -> dict[str, str]:
        """带会话的审计字段（在 `audit_fields()` 之外**单独**提供）。

        为什么不让 `audit_fields()` 直接加 `session_id`：
        那是审计链的哈希输入，改动会让**历史链的复算口径变化**。
        需要会话维度的调用点（登录/改密/找回）显式调这个，兼顾兼容与可追溯。
        """
        return {**self.audit_fields(), "session_id": self.session_id}

    def audit_fields(self) -> dict[str, str]:
        """进审计日志的最小字段集。**不含任何业务数据**。"""
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "roles": ",".join(sorted(r.value for r in self.roles)),
            "clearance": self.clearance.name,
            "wall": self.wall_group.value,
            "auth_source": self.auth_source,
        }


#: 系统身份 —— 只用于**明确标注**的无认证场景（本地开发、单测、迁移脚本）。
#: 任何进入 HTTP 请求路径的代码都不该主动使用它。
ANONYMOUS = Principal(user_id="anonymous", tenant_id="public",
                      roles=frozenset({Role.AUDITOR}),
                      clearance=DataClass.PUBLIC,
                      wall_group=WallGroup.PLATFORM,
                      auth_source="none")

#: 未认证标记：`current_principal()` 拿不到身份时返回它，
#: 让"忘了鉴权"变成**显式可检测**的状态，而不是静默降级成某个真实租户。
_UNAUTHENTICATED = Principal(user_id="__unauthenticated__", tenant_id="",
                              wall_group=WallGroup.PLATFORM,
                              auth_source="missing", authenticated=False)

_current: contextvars.ContextVar[Principal] = contextvars.ContextVar(
    "principal", default=_UNAUTHENTICATED)


def current_principal() -> Principal:
    """当前上下文身份。**未认证**时返回哨兵，`require_principal()` 会拒绝。"""
    return _current.get()


def is_authenticated() -> bool:
    return _current.get() is not _UNAUTHENTICATED


def require_principal() -> Principal:
    """取身份，未认证直接抛 —— 业务代码应优先用这个而非 `current_principal()`。"""
    principal = _current.get()
    if principal is _UNAUTHENTICATED:
        raise TenantError("当前上下文没有已认证身份（接入层未注入 Principal）")
    return principal


def current_tenant() -> str:
    """当前租户 ID。用于仓储层拼 `WHERE tenant_id = ?`。"""
    return require_principal().tenant_id


@contextmanager
def principal_scope(principal: Principal) -> Iterator[Principal]:
    """在指定身份下执行一段代码。**只能由接入层或测试调用。**

    例外：带 `wall_crossing` 的显式跨墙访问必须走
    `policy.authorize(..., wall_crossing=WallCrossing(...))`，而不是
    直接换 Principal —— 那样会绕过留痕。
    """
    token = _current.set(principal)
    try:
        yield principal
    finally:
        _current.reset(token)


@contextmanager
def system_scope(reason: str) -> Iterator[Principal]:
    """后台任务/迁移脚本的显式系统上下文。

    `reason` 会进审计，让"谁在无用户身份下动了数据"可追溯。
    """
    principal = replace(ANONYMOUS, user_id=f"system:{reason or 'unspecified'}")
    with principal_scope(principal) as scoped:
        yield scoped


def tenant_cache_key(parts: str, *,
                     data_class: DataClass = DataClass.INTERNAL) -> str:
    """给缓存键加隔离前缀。

    ⚠️ 缓存是跨租户泄漏最常见的通道之一：LLM 响应缓存、行情缓存、研报缓存
    若只按内容做 key，A 的请求会命中 B 的缓存条目。所有**内容型缓存**
    必须经过本函数。

    ## 隔离维度不止租户

    同一租户内不同清关等级的用户**也不该共用敏感缓存**：
    合规岗（`RESTRICTED`）与实习生（`PUBLIC`）在同一个部门，
    若共用策略结论缓存，低清关者等于间接读到了高等级内容。

    所以当 `data_class >= CONFIDENTIAL` 时，把清关等级也并入 key。
    低于该等级的内容（行情、公开研报）按租户隔离即可 —— 加进去只会
    白白降低命中率。
    """
    principal = current_principal()
    scope = principal.tenant_id or "public"
    if data_class >= DataClass.CONFIDENTIAL:
        scope = f"{scope}|c{int(principal.clearance)}"
    digest = hashlib.sha256(
        f"{scope}\x00{parts}".encode()).hexdigest()
    return digest


__all__ = [
    "ANONYMOUS",
    "AccessDenied",
    "DataClass",
    "Principal",
    "Role",
    "TenantError",
    "WallGroup",
    "current_principal",
    "current_tenant",
    "is_authenticated",
    "principal_scope",
    "require_principal",
    "system_scope",
    "tenant_cache_key",
]
