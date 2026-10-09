"""RLS 会话变量**前提**的判据（`CHG-0192` / 债 #1 的可做部分）。

## 这条判据防的是什么（"照文档做即故障"）

`postgres_rls_ddl()` 生成一份看起来完备的策略 DDL，注释写着
"前提：会话变量 app.tenant_id / app.user_id 由连接池在每次取连接时设置"。
而**全仓库没有任何一处写这两个变量**（零 `SET LOCAL` / `set_config`）。

于是照那份 DDL 建完策略之后：

    current_setting('app.tenant_id', true)  →  NULL（true = 缺失时返回 NULL）
    USING (tenant_id = current_tenant_id()) →  USING (tenant_id = NULL) → 恒不成立
    ⇒ ★ 应用**一行都查不到**

**不是"隔离生效了"，是"数据全部不可见"** —— 而且没有任何报错。
这类缺陷在 SQLite 默认部署下不会暴露（那边根本没有 RLS），
只在切到 PG 的那一天爆发。

## 判据强度

- `test_session_vars_are_not_wired_yet` —— 现状判据（接线后本文件要一起改）。
- `test_ddl_carries_a_loud_banner_while_unwired` —— ★ **DDL 自证**：
  未接线时生成的 DDL 必须带醒目警告块，且警告块里给出**正确顺序**。
  把 banner 删掉 ⇒ 立刻红（防止"警告被顺手清理"）。
- `test_ddl_says_wired_when_wired` —— 反事实：接线后 banner 必须消失、
  状态行必须写"已接线"（保证这个开关真的在起作用，不是死代码）。
- `test_startup_refuses_postgres_without_wiring` —— ★ **行为判据**：
  `DATA_BACKEND=postgres` + 未接线时，启动期检查必须**拒绝**。
- `test_startup_allows_sqlite` —— 反向：默认 SQLite 部署不受影响（不许误拦）。
"""

from __future__ import annotations

import inspect

import pytest

from src.infrastructure import security
from src.infrastructure.security import rls


# ======================================================================
# 1. 现状与开关
# ======================================================================

def test_session_vars_are_not_wired_yet():
    """★ 现状：会话变量**未接线**。

    这条会在真的接线后变红 —— 那是**预期**：届时请连同本文件与
    `rls._SESSION_VARS_WIRED` 一起更新，而不是把判据删掉了事。
    """
    wired, why = rls.session_vars_supported()
    assert wired is False, (
        "会话变量已接线？那请：① 置 rls._SESSION_VARS_WIRED=True；"
        "② 补「跨租户 403 + 并发不串数据」判据；③ 更新本用例"
    )
    assert "SET LOCAL" in why, "未接线的说明必须给出接线方法（SET LOCAL …）"
    assert "查不到" in why or "不可见" in why, (
        "说明必须写清后果是**数据不可见**（而不是含糊的「隔离未生效」）"
    )


def test_session_vars_supported_is_exported():
    """公开出口：启动自检与运维脚本都要能问这个问题。"""
    assert "session_vars_supported" in rls.__all__
    assert callable(getattr(security.rls, "session_vars_supported", None))


# ======================================================================
# 2. DDL 自证（未接线 → 醒目警告；接线 → 状态如实）
# ======================================================================

def test_ddl_carries_a_loud_banner_while_unwired():
    """★ 未接线时生成的 DDL 必须自带醒目警告，并给出**正确执行顺序**。"""
    ddl = rls.postgres_rls_ddl()
    assert "请勿执行" in ddl, "DDL 顶部没有「请勿执行」警告 ⇒ 运维会照做"
    assert "一行都查不到" in ddl, "警告必须写清后果（数据不可见）"
    assert "SET LOCAL app.tenant_id" in ddl, "警告必须给出接线方法"
    # 顺序必须写明（先接线、再判据、再翻开关、最后执行）
    assert "①" in ddl and "④" in ddl, "警告没有给出分步顺序"
    # 状态行必须如实
    assert "未接线" in ddl


def test_ddl_still_contains_the_real_policy_sql():
    """加警告**不许**把正文弄丢（既有 tenancy 测试也依赖这些子串）。"""
    ddl = rls.postgres_rls_ddl()
    for table in rls.TENANT_COLUMNS:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in ddl
    assert "current_tenant_id()" in ddl


def test_ddl_says_wired_when_wired(monkeypatch: pytest.MonkeyPatch):
    """★ 反事实：把开关置 True ⇒ 警告消失、状态行写"已接线"。

    没有这条，上面那份警告可能是**永远为真**的死代码（接线了也不消失 ⇒
    运维会以为它永远不能执行）。
    """
    monkeypatch.setattr(rls, "_SESSION_VARS_WIRED", True, raising=False)
    wired, why = rls.session_vars_supported()
    assert wired is True and why == ""

    ddl = rls.postgres_rls_ddl()
    assert "请勿执行" not in ddl, "已接线却仍在喊「请勿执行」⇒ 开关没起作用"
    assert "已接线" in ddl
    assert "current_tenant_id()" in ddl, "接线后正文必须照旧"


def test_unknown_role_still_rejected():
    """既有护栏不许被这次改动削弱。"""
    from src.core.tenancy import TenantError

    with pytest.raises(TenantError, match="未知数据库角色"):
        rls.postgres_rls_ddl(role="root")


# ======================================================================
# 3. 启动期守卫（行为判据）
# ======================================================================

def test_guard_is_a_single_implementation():
    """★ 守卫必须是**单一实现**，两个调用点都调它（不许各写一份判断）。

    本仓库为此付过代价：同一个判断两份实现 ⇒ 只改一处 ⇒ 行为静默分叉。
    """
    from src.infrastructure.security import rls as mod

    assert callable(getattr(mod, "assert_backend_can_start", None))
    assert "assert_backend_can_start" in mod.__all__


def test_guard_allows_sqlite_and_rejects_postgres():
    """★ 行为判据（不读源码，直接调）：sqlite / 空值放行，postgres 拒绝。"""
    from src.infrastructure.security.rls import assert_backend_can_start

    assert_backend_can_start("sqlite")
    assert_backend_can_start("")
    assert_backend_can_start(None)
    assert_backend_can_start("SQLITE")          # 大小写不敏感

    with pytest.raises(RuntimeError) as exc:
        assert_backend_can_start("postgres")
    msg = str(exc.value)
    assert "未接线" in msg
    assert "①" in msg and "②" in msg, "报错必须给出两条出路（而不是只报错）"


def test_guard_releases_after_wiring(monkeypatch: pytest.MonkeyPatch):
    """★ 反事实：接线后必须**放行**（否则守卫会永久拦住 PG 部署）。"""
    from src.infrastructure.security.rls import assert_backend_can_start

    monkeypatch.setattr(rls, "_SESSION_VARS_WIRED", True, raising=False)
    assert_backend_can_start("postgres")         # 不抛 = 放行


def test_startup_refuses_postgres_without_wiring():
    """★ `api/main.py` 的 lifespan 必须调那个单一实现（兜住绕过 manage 的启动）。"""
    src = inspect.getsource(__import__("src.api.main", fromlist=["x"]).lifespan)
    assert "assert_backend_can_start" in src, "lifespan 没有后端前提检查"
    assert "raise RuntimeError" not in src.split("assert_backend_can_start")[0][-200:], (
        "不要再内联一份判断 —— 走单一实现"
    )


def test_manage_py_env_selfcheck_also_calls_the_guard():
    """★ `manage.py::_prepare_environment` 也要调（更便宜的失败点）。

    覆盖 `status` / `doctor` / `retention` 这类**不启动 API、永远不跑 lifespan**
    的命令 —— 它们同样会受"PG 策略建了但应用读不到数据"的影响。
    """
    from pathlib import Path

    src = Path("manage.py").read_text(encoding="utf-8")
    assert "assert_backend_can_start" in src, (
        "manage.py 的环境自检没有接后端守卫 ⇒ 不起 API 的命令全绕过"
    )
    assert src.count("assert_backend_can_start") >= 2, (
        "manage.py 里应有两处：环境自检 + 删数据的 retention 命令（--env 可选，"
        "不传时 `_prepare_environment` 根本不跑）"
    )


def test_default_sqlite_deployment_is_not_blocked():
    """反向：默认 SQLite 部署**不许**被这条守卫拦住（那是绝大多数环境）。"""
    from src.core.config import Settings

    s = Settings()
    assert str(s.data_backend).strip().lower() == "sqlite", (
        "默认后端不再是 sqlite —— 请确认守卫不会拦住默认部署"
    )
