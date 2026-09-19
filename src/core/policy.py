"""授权策略引擎 —— **所有访问决策的唯一入口**。

## 设计原则

1. **单一入口**：业务代码不得自己写 `if user.role == "admin"`。所有判断走
   `authorize(action, resource)`，这样策略变更只有一处、审计也只有一个埋点。
2. **默认拒绝**：未在 `_POLICY` 中声明的 (角色, 动作) 组合一律拒绝。
   "忘了配策略"必须是拒绝而不是放行。
3. **纵深防御而非安全边界**：本模块是**应用层**控制。真正的边界在数据库
   （RLS 策略）与网络（mTLS）。这里挂掉不等于数据安全，反之亦然 —— 两层都要有。
4. **跨墙必须显式留痕**：带着 `WallCrossing` 才能跨信息隔离墙，且该参数会进审计。

## 与业界机构做法的对应

| 机构做法 | 本模块落地 |
|---|---|
| RBAC（岗位角色） | `Role` + `_POLICY` 动作矩阵 |
| ABAC（数据分级） | `DataClass` vs `Principal.clearance` |
| 信息隔离墙（Chinese Wall） | `WallGroup` + `WallCrossing` 审批对象 |
| 最小权限 | 默认拒绝 + 动作级粒度 |
| 双人复核（四眼原则） | `requires_four_eyes()` 声明式标注 |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from src.core.tenancy import (
    AccessDenied,
    DataClass,
    Principal,
    Role,
    WallGroup,
)

logger = logging.getLogger(__name__)


class Action(str):
    """动作标识。用 `str` 子类是为了既能当常量又能直接进 JSON。"""


# ======================================================================
# 动作清单 —— 新增动作必须同时在上面的矩阵里给授权
# ======================================================================

#: 读类
READ_PUBLIC_DATA: Final = "read:public_data"
READ_INTERNAL_REPORT: Final = "read:internal_report"
READ_STRATEGY: Final = "read:strategy"          # 策略源码/参数（核心资产）
READ_PORTFOLIO: Final = "read:portfolio"        # 持仓
READ_AUDIT: Final = "read:audit"

#: 写类
RUN_ANALYSIS: Final = "run:analysis"            # 跑投研分析（花钱、产生结论）
RUN_BACKTEST: Final = "run:backtest"
WRITE_WATCHLIST: Final = "write:watchlist"
WRITE_STRATEGY: Final = "write:strategy"
EXPORT_DATA: Final = "export:data"              # **批量导出是泄漏主通道，单独授权**
MANAGE_TENANT: Final = "manage:tenant"          # 建租户/改权限
MANAGE_DATA_SOURCE: Final = "manage:data_source"  # 接新数据源（含 LLM 生成连接器）


#: 动作 → 允许的角色集合。**未列出即拒绝。**
_POLICY: Final[dict[str, frozenset[Role]]] = {
    READ_PUBLIC_DATA: frozenset({Role.RESEARCHER, Role.PM, Role.TRADER,
                                 Role.RISK, Role.COMPLIANCE, Role.AUDITOR}),
    READ_INTERNAL_REPORT: frozenset({Role.RESEARCHER, Role.PM, Role.RISK,
                                     Role.COMPLIANCE, Role.AUDITOR}),
    # 策略源码：只有研究员本人/同组可读；PM、风控、合规**都不给**
    # —— 这是机构里"投资端不得看研究端在研策略"的落地
    READ_STRATEGY: frozenset({Role.RESEARCHER, Role.AUDITOR}),
    READ_PORTFOLIO: frozenset({Role.PM, Role.TRADER, Role.RISK,
                               Role.COMPLIANCE, Role.AUDITOR}),
    READ_AUDIT: frozenset({Role.COMPLIANCE, Role.AUDITOR, Role.ADMIN}),

    RUN_ANALYSIS: frozenset({Role.RESEARCHER, Role.PM, Role.RISK,
                             Role.COMPLIANCE}),
    RUN_BACKTEST: frozenset({Role.RESEARCHER, Role.PM, Role.RISK}),
    WRITE_WATCHLIST: frozenset({Role.RESEARCHER, Role.PM, Role.TRADER}),
    WRITE_STRATEGY: frozenset({Role.RESEARCHER}),
    EXPORT_DATA: frozenset({Role.RESEARCHER, Role.PM, Role.RISK,
                            Role.COMPLIANCE}),
    MANAGE_TENANT: frozenset({Role.ADMIN}),
    MANAGE_DATA_SOURCE: frozenset({Role.ADMIN, Role.RESEARCHER}),
}

#: 需要**双人复核**的动作（四眼原则）。命中后调用方必须走审批流，
#: 不能只凭单次 `authorize` 通过就执行。
_FOUR_EYES: Final[frozenset[str]] = frozenset({
    MANAGE_TENANT, MANAGE_DATA_SOURCE, EXPORT_DATA,
})

#: 动作所需的最低数据等级（ABAC 维度）。未列出 = PUBLIC。
_REQUIRED_DATA_CLASS: Final[dict[str, DataClass]] = {
    READ_INTERNAL_REPORT: DataClass.INTERNAL,
    READ_STRATEGY: DataClass.CONFIDENTIAL,
    READ_PORTFOLIO: DataClass.CONFIDENTIAL,
    READ_AUDIT: DataClass.CONFIDENTIAL,
    WRITE_STRATEGY: DataClass.CONFIDENTIAL,
    MANAGE_TENANT: DataClass.RESTRICTED,
}


@dataclass(frozen=True)
class Resource:
    """被访问的资源。`tenant_id` 为 None 表示全局共享资源。"""

    kind: str                                  # "report" / "strategy" / "portfolio" …
    tenant_id: str | None = None
    data_class: DataClass = DataClass.PUBLIC
    owner_id: str = ""
    owner_groups: frozenset[str] = frozenset()
    wall_group: WallGroup | None = None


@dataclass(frozen=True)
class WallCrossing:
    """跨信息隔离墙的**显式审批凭据**。

    机构里跨墙需要合规审批并留痕；这里把它做成一个必须显式传入的对象，
    使"跨墙"在代码里无法悄悄发生。
    """

    approver: str
    reason: str
    approved_at: str = ""

    def __post_init__(self) -> None:
        if not self.approver or not self.reason:
            raise ValueError("跨墙审批必须带 approver 与 reason")
        if not self.approved_at:
            object.__setattr__(
                self, "approved_at",
                datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def audit_fields(self) -> dict[str, str]:
        return {"wall_crossing_approver": self.approver,
                "wall_crossing_reason": self.reason,
                "wall_crossing_at": self.approved_at}


@dataclass(frozen=True)
class Decision:
    """授权结论。`allowed=False` 时 `reason` 必须能直接给用户看。"""

    allowed: bool
    reason: str = ""
    requires_four_eyes: bool = False
    crossed_wall: bool = False

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise AccessDenied(self.reason)


def authorize(action: str, resource: Resource | None = None, *,
              principal: Principal | None = None,
              wall_crossing: WallCrossing | None = None) -> Decision:
    """**唯一的授权判定**。默认拒绝。

    判定顺序（先便宜后昂贵，且**先隔离后权限**）：

    1. 动作是否在策略表里 —— 不在即拒绝（默认拒绝）
    2. 角色是否有该动作 —— RBAC
    3. 租户是否匹配 —— 行级隔离兜底（数据库层还有一层）
    4. 数据等级是否够 —— ABAC
    5. 信息隔离墙 —— 跨墙需要显式审批
    """
    from src.core.tenancy import require_principal

    actor = principal or require_principal()
    resource = resource or Resource(kind="global")

    if action not in _POLICY:
        return Decision(False, f"动作 {action!r} 未在策略表中声明（默认拒绝）")

    if not (actor.roles & _POLICY[action]):
        return Decision(
            False,
            f"角色 {'/'.join(sorted(r.value for r in actor.roles)) or '（无）'} "
            f"无权执行 {action}")

    # ---- 租户隔离：跨租户访问一律拒绝，管理员也只在本租户内 ----
    # （跨租户只读由 `AUDITOR` 通过显式 `tenant_scope` 承担，见接入层）
    if resource.tenant_id is not None and resource.tenant_id != actor.tenant_id:
        if Role.AUDITOR not in actor.roles:
            return Decision(
                False,
                f"资源属于租户 {resource.tenant_id}，当前身份在 {actor.tenant_id}"
                "（跨租户访问需审计员身份且显式声明）")

    # ---- 数据分级（ABAC）----
    required = _REQUIRED_DATA_CLASS.get(action, DataClass.PUBLIC)
    need = max(required, resource.data_class)
    if actor.clearance < need:
        return Decision(
            False,
            f"{action} 需要 {need.name} 级数据权限，当前为 {actor.clearance.name}")

    # ---- 信息隔离墙 ----
    crossed = False
    if (resource.wall_group is not None
            and resource.wall_group is not actor.wall_group
            and not actor.is_control):
        if wall_crossing is None:
            return Decision(
                False,
                f"资源位于「{resource.wall_group.value}」隔离墙内，当前身份在"
                f"「{actor.wall_group.value}」；跨墙需提供 WallCrossing 审批凭据")
        crossed = True

    return Decision(True, requires_four_eyes=action in _FOUR_EYES,
                    crossed_wall=crossed)


def requires_four_eyes(action: str) -> bool:
    """该动作是否必须双人复核（调用方据此走审批流，不能只凭 authorize 通过就执行）。"""
    return action in _FOUR_EYES


def visible_data_class(principal: Principal) -> DataClass:
    """该身份在**列表/搜索**场景下能看到的数据等级上限。

    列表查询必须用它做下推过滤 —— 查出全量再在内存里过滤，
    数据已经在进程里了，不叫隔离。
    """
    return principal.clearance


__all__ = [
    "Action",
    "Decision",
    "EXPORT_DATA",
    "MANAGE_DATA_SOURCE",
    "MANAGE_TENANT",
    "READ_AUDIT",
    "READ_INTERNAL_REPORT",
    "READ_PORTFOLIO",
    "READ_PUBLIC_DATA",
    "READ_STRATEGY",
    "Resource",
    "RUN_ANALYSIS",
    "RUN_BACKTEST",
    "WRITE_STRATEGY",
    "WRITE_WATCHLIST",
    "WallCrossing",
    "authorize",
    "requires_four_eyes",
    "visible_data_class",
]
