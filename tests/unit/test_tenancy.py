"""多租户与合规的回归测试。

覆盖三块：
1. **身份上下文**（`src/core/tenancy.py`）—— 未认证必须是可检测状态，不静默降级
2. **授权策略**（`src/core/policy.py`）—— RBAC + 数据分级 + 信息隔离墙 + 默认拒绝
3. **RLS**（`src/infrastructure/security/rls.py`）—— 未登记的表必须报错而非放行
"""
from __future__ import annotations

import pytest

from src.core import policy as P
from src.core.tenancy import (
    DataClass,
    Principal,
    Role,
    TenantError,
    WallGroup,
    current_principal,
    current_tenant,
    is_authenticated,
    principal_scope,
    require_principal,
    system_scope,
    tenant_cache_key,
)
from src.infrastructure.security import rls


@pytest.fixture()
def researcher() -> Principal:
    return Principal(user_id="alice", tenant_id="quant-a",
                     roles=frozenset({Role.RESEARCHER}),
                     clearance=DataClass.CONFIDENTIAL,
                     wall_group=WallGroup.RESEARCH)


@pytest.fixture()
def pm() -> Principal:
    return Principal(user_id="bob", tenant_id="quant-a",
                     roles=frozenset({Role.PM}),
                     clearance=DataClass.CONFIDENTIAL,
                     wall_group=WallGroup.INVESTMENT)


@pytest.fixture()
def auditor() -> Principal:
    return Principal(user_id="aud", tenant_id="quant-a",
                     roles=frozenset({Role.AUDITOR}),
                     clearance=DataClass.RESTRICTED,
                     wall_group=WallGroup.CONTROL)


# ======================================================================
# 1. 身份上下文
# ======================================================================

def test_unauthenticated_is_detectable_not_silent() -> None:
    """**核心回归**：没有身份时必须是"未认证"这一可检测状态。

    最危险的写法是 `tenant_id = request.headers.get("X-Tenant-Id", "default")`
    —— 拿不到就降级到 default 租户，等于把越权变成默认行为。
    """
    assert is_authenticated() is False
    assert current_principal().authenticated is False
    with pytest.raises(TenantError, match="没有已认证身份"):
        require_principal()
    with pytest.raises(TenantError):
        current_tenant()


def test_principal_must_have_tenant(researcher: Principal) -> None:
    with pytest.raises(TenantError):
        Principal(user_id="x", tenant_id="")
    with pytest.raises(TenantError):
        Principal(user_id="", tenant_id="quant-a")


def test_principal_scope_restores_previous(researcher: Principal) -> None:
    with principal_scope(researcher):
        assert current_tenant() == "quant-a"
    assert is_authenticated() is False, "退出作用域后必须恢复未认证"


def test_system_scope_is_explicit_and_attributable() -> None:
    """后台任务必须有显式系统身份，且 `reason` 可归因（进审计）。"""
    with system_scope("nightly-rebuild") as scoped:
        assert scoped.tenant_id == "public"
        assert "nightly-rebuild" in scoped.user_id


def test_tenant_cache_key_isolates_tenants(researcher: Principal) -> None:
    """**缓存跨租户泄漏回归**：内容相同的缓存键在不同租户下必须不同。"""
    pm_other = Principal(user_id="bob", tenant_id="quant-b",
                         roles=frozenset({Role.PM}),
                         clearance=DataClass.CONFIDENTIAL,
                         wall_group=WallGroup.INVESTMENT)
    with principal_scope(researcher):
        key_a = tenant_cache_key("同一段 prompt")
    with principal_scope(pm_other):
        key_b = tenant_cache_key("同一段 prompt")
    assert key_a != key_b, "缓存键未按租户隔离 → A 会命中 B 的缓存"


def test_sensitive_cache_key_also_isolates_by_clearance() -> None:
    """**同租户内也要按清关等级隔离敏感缓存**。

    合规岗（RESTRICTED）与低清关用户（PUBLIC）在同一个部门，
    若共用策略结论缓存，低清关者等于间接读到了高等级内容。
    """
    high = Principal(user_id="c", tenant_id="quant-a",
                     roles=frozenset({Role.COMPLIANCE}),
                     clearance=DataClass.RESTRICTED,
                     wall_group=WallGroup.CONTROL)
    low = Principal(user_id="i", tenant_id="quant-a",
                    roles=frozenset({Role.RESEARCHER}),
                    clearance=DataClass.PUBLIC,
                    wall_group=WallGroup.RESEARCH)
    with principal_scope(high):
        sensitive_high = tenant_cache_key("策略结论",
                                          data_class=DataClass.CONFIDENTIAL)
        public_high = tenant_cache_key("行情")
    with principal_scope(low):
        sensitive_low = tenant_cache_key("策略结论",
                                         data_class=DataClass.CONFIDENTIAL)
        public_low = tenant_cache_key("行情")
    assert sensitive_high != sensitive_low, "敏感缓存未按清关等级隔离"
    # 非敏感内容不必按清关等级拆分（拆了只会白白降低命中率）
    assert public_high == public_low


# ======================================================================
# 2. 授权策略
# ======================================================================

def test_default_deny_for_undeclared_action(researcher: Principal) -> None:
    with principal_scope(researcher):
        assert P.authorize("delete:everything").allowed is False


def test_role_matrix_blocks_cross_desk_access(researcher: Principal,
                                             pm: Principal) -> None:
    """信息隔离墙在**角色层**的落地：投资端不得读研究端的在研策略。"""
    with principal_scope(researcher):
        assert P.authorize(P.READ_STRATEGY).allowed is True
    with principal_scope(pm):
        assert P.authorize(P.READ_STRATEGY).allowed is False


def test_clearance_is_independent_from_role() -> None:
    """**清关等级与角色是两个维度**：合规岗权限高但不该看核心策略数据。"""
    low = Principal(user_id="dave", tenant_id="quant-a",
                    roles=frozenset({Role.RESEARCHER}),
                    clearance=DataClass.PUBLIC, wall_group=WallGroup.RESEARCH)
    with principal_scope(low):
        decision = P.authorize(P.READ_STRATEGY)
    assert decision.allowed is False and "CONFIDENTIAL" in decision.reason


def test_wall_crossing_requires_explicit_credential(researcher: Principal) -> None:
    """跨墙必须带审批凭据 —— 使跨墙在代码里无法悄悄发生。"""
    other_wall = P.Resource(kind="strategy", tenant_id="quant-a",
                            data_class=DataClass.CONFIDENTIAL,
                            wall_group=WallGroup.INVESTMENT)
    with principal_scope(researcher):
        assert P.authorize(P.READ_STRATEGY, other_wall).allowed is False
        crossed = P.authorize(
            P.READ_STRATEGY, other_wall,
            wall_crossing=P.WallCrossing(approver="compliance-01",
                                         reason="监管核查"))
    assert crossed.allowed is True and crossed.crossed_wall is True


def test_wall_crossing_credential_must_be_complete() -> None:
    with pytest.raises(ValueError, match="approver"):
        P.WallCrossing(approver="", reason="x")
    with pytest.raises(ValueError, match="approver"):
        P.WallCrossing(approver="c", reason="")


def test_control_group_is_naturally_cross_wall(researcher: Principal,
                                               auditor: Principal) -> None:
    """风控/合规/审计天然跨墙（否则无法履职）。"""
    assert auditor.is_control is True
    assert researcher.is_control is False


def test_cross_tenant_denied_except_auditor(researcher: Principal,
                                            auditor: Principal) -> None:
    other = P.Resource(kind="quote", tenant_id="quant-b")
    with principal_scope(researcher):
        assert P.authorize(P.READ_PUBLIC_DATA, other).allowed is False
    with principal_scope(auditor):
        assert P.authorize(P.READ_PUBLIC_DATA, other).allowed is True


def test_export_and_admin_need_four_eyes(researcher: Principal) -> None:
    """批量导出是泄漏主通道 → 声明式标注需双人复核。"""
    with principal_scope(researcher):
        decision = P.authorize(P.EXPORT_DATA)
    assert decision.allowed is True
    assert decision.requires_four_eyes is True
    assert P.requires_four_eyes(P.MANAGE_TENANT) is True
    assert P.requires_four_eyes(P.READ_PUBLIC_DATA) is False


def test_visible_data_class_is_clearance(researcher: Principal) -> None:
    assert P.visible_data_class(researcher) is DataClass.CONFIDENTIAL


# ======================================================================
# 3. RLS
# ======================================================================

def test_undeclared_table_raises_not_silently_passes() -> None:
    """**"忘了登记"必须表现为失败**，否则新表会静默地没有隔离。"""
    with pytest.raises(TenantError, match="未登记租户列"):
        rls.tenant_column_guard("brand_new_table")


def test_tenant_guard_returns_pushdown_predicate(researcher: Principal) -> None:
    """隔离条件必须**下推进 SQL**，而不是查出全量再在内存里过滤。"""
    with principal_scope(researcher):
        where, params = rls.tenant_column_guard("watchlist")
    assert where == "tenant_id = ?"
    assert params == ["quant-a"]
    with principal_scope(researcher):
        aliased, _ = rls.tenant_column_guard("watchlist", alias="w")
    assert aliased == "w.tenant_id = ?"


def test_guard_requires_authenticated_principal() -> None:
    with pytest.raises(TenantError, match="已认证身份"):
        rls.tenant_column_guard("watchlist")


def test_global_tables_are_declared_with_reason() -> None:
    """不参与隔离的表必须写明原因 —— 防止有人"顺手加上"。"""
    for table, reason in rls.GLOBAL_TABLES.items():
        assert reason, f"{table} 未写明为什么不做租户隔离"
    assert rls.tenant_column("audit_chain") is None


def test_filter_rows_matches_tenant(researcher: Principal, pm: Principal) -> None:
    rows = [{"id": 1, "tenant_id": "quant-a"},
            {"id": 2, "tenant_id": "quant-b"}]
    with principal_scope(researcher):
        assert [r["id"] for r in rls.filter_rows(rows, "watchlist")] == [1]


def test_postgres_ddl_covers_every_tenant_table() -> None:
    """数据库层策略（真正的边界）必须覆盖所有登记表。"""
    ddl = rls.postgres_rls_ddl()
    for table in rls.TENANT_COLUMNS:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in ddl
    assert "current_tenant_id()" in ddl
    with pytest.raises(TenantError, match="未知数据库角色"):
        rls.postgres_rls_ddl(role="root")
