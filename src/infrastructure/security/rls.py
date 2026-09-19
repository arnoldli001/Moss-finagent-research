"""行级安全（RLS）—— 租户隔离的**第二层**防线。

## 先读这一条，否则会误用

**应用层 RLS 不是安全边界。** 它能防"忘了写 `WHERE tenant_id=?`"这类疏漏，
防不住：拿到连接的任意代码、绕过本模块的裸 SQL、直接读库文件的运维。
真正的边界是**数据库原生 RLS 策略**（PostgreSQL `CREATE POLICY`，见
`postgres_rls_ddl()`）加网络层 mTLS。两层都要有，缺一不可。

⚠️ 本模块的前一版是个**假实现**：`_inject_tenant_filter` 取了租户、遍历了表，
然后 `return stmt` 原样返回 —— docstring 声称的 `WHERE tenant_id = ?`
根本不存在。那比没有更危险，因为它会让人以为已经隔离了。
现在的版本只做它真正能做到的事，并把边界写在文档里。

## 三层职责

1. `tenant_column_guard()` —— **查询期下推**。仓储层用它把租户条件拼进 SQL，
   而不是"查出全量再在内存里过滤"（后者数据已经进进程了，不叫隔离）。
2. `register_rls_listeners()` —— SQLAlchemy 兜底。只对**显式标记**的表生效，
   不做隐式改写（隐式改写会破坏迁移脚本、后台任务与审计查询）。
3. `postgres_rls_ddl()` —— 给出数据库层的策略 DDL，由运维执行。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from src.core.tenancy import TenantError, current_principal, is_authenticated

logger = logging.getLogger(__name__)

#: 参与租户隔离的业务表 → 租户列名。
#: **必须显式登记**：不做"所有表都加 tenant_id"的猜测 —— 审计链、迁移表、
#: 全局行情表没有租户列，猜错会让查询直接报 SQL 错。
TENANT_COLUMNS: dict[str, str] = {
    "watchlist": "tenant_id",
    "intraday_profile": "tenant_id",
    "strategy": "tenant_id",
    "backtest_run": "tenant_id",
    "research_task": "tenant_id",
    "screener_run": "tenant_id",
    "alert_rule": "tenant_id",
}

#: 明确**不参与**租户隔离的表，及其原因（写下来防止有人"顺手加上"）。
#: 每条原因都必须独立可读 —— 写"同上"会让后来者无法判断是否仍然成立。
GLOBAL_TABLES: dict[str, str] = {
    "audit_chain": "审计链跨租户，只由合规/审计角色经 READ_AUDIT 授权后读取",
    "llm_audit": "LLM 调用审计跨租户，同上，且已按租户写入 user_id/tenant_id 字段",
    "llm_cache": "内容缓存无租户语义；其键已由 tenancy.tenant_cache_key() 加租户前缀",
    "schema_migrations": "迁移元数据，不含任何业务数据",
    "auction_snapshot": "全市场公共行情，非租户产生，按 trade_date 全局共享",
    "auction_feature": "竞价特征由公共行情派生，同样按 trade_date 全局共享",
}


def tenant_column(table: str) -> str | None:
    """返回该表的租户列名；不参与隔离的表返回 `None`。"""
    return TENANT_COLUMNS.get(table)


def tenant_column_guard(table: str, *, alias: str = "") -> tuple[str, list[Any]]:
    """返回 `(SQL 片段, 参数)`，供仓储层拼进 `WHERE`。

    用法::

        where, params = tenant_column_guard("watchlist")
        conn.execute(f"SELECT * FROM watchlist WHERE {where}", params)

    未登记的表**直接抛错**而不是放行 —— "忘了登记"必须表现为失败，
    否则新表会静默地没有隔离。
    """
    column = TENANT_COLUMNS.get(table)
    if column is None:
        raise TenantError(
            f"表 {table!r} 未登记租户列。若它确有租户属性，请加进 "
            f"TENANT_COLUMNS；若它是全局表，请加进 GLOBAL_TABLES 并写明原因。")
    if not is_authenticated():
        raise TenantError(f"查询 {table!r} 需要已认证身份（当前上下文没有 Principal）")
    principal = current_principal()
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{column} = ?", [principal.tenant_id]


def filter_rows(rows: Iterable[dict[str, Any]], table: str) -> list[dict[str, Any]]:
    """内存兜底过滤。

    ⚠️ **只用于已经拿到小结果集的场景**（例如缓存反序列化）。
    主查询路径必须用 `tenant_column_guard()` 做**下推** —— 查出全量再过滤，
    数据已经进了进程，隔离已经失效了。
    """
    column = TENANT_COLUMNS.get(table)
    if column is None:
        raise TenantError(f"表 {table!r} 未登记租户列")
    tenant = current_principal().tenant_id
    return [row for row in rows if str(row.get(column) or "") == tenant]


@contextmanager
def tenant_scope(tenant_id: str):
    """**仅测试与运维脚本**使用：在指定租户下执行一段代码。

    ⚠️ 不对外暴露给 HTTP 层。请求路径的租户只能由
    `src/api/tenancy_middleware.py` 从凭证派生后注入。
    """
    from src.core.tenancy import Principal, Role, principal_scope

    if not tenant_id:
        raise TenantError("tenant_id 不能为空")
    with principal_scope(Principal(user_id=f"scope:{tenant_id}",
                                   tenant_id=tenant_id,
                                   roles=frozenset({Role.AUDITOR}),
                                   auth_source="scope")) as scoped:
        yield scoped


# ======================================================================
# SQLAlchemy 兜底 listener
# ======================================================================

def register_rls_listeners(engine: Any) -> None:
    """注册 SQLAlchemy 连接事件，在**执行前**校验租户上下文存在。

    刻意**不做**语句改写：自动往任意 SELECT 里插 `WHERE` 会破坏
    JOIN 语义、聚合、迁移脚本与后台任务，且很容易写出"看起来加了、
    实际被 UNION 绕过"的假隔离。这里只做一件事 ——
    **碰租户表却没有身份时，让查询失败**。
    """
    from sqlalchemy import event

    def _before_execute(conn, clauseelement, multiparams, params, execution_options):
        try:
            text = str(clauseelement)
        except Exception:  # noqa: BLE001
            return clauseelement, multiparams, params
        touched = [t for t in TENANT_COLUMNS if t in text]
        if touched and not is_authenticated():
            raise TenantError(
                f"查询涉及租户表 {touched}，但当前没有已认证身份。"
                "后台任务请显式使用 tenancy.system_scope(reason)。")
        return clauseelement, multiparams, params

    event.listen(engine, "before_execute", _before_execute, retval=True)
    logger.info("RLS 守卫已注册：engine=%s，租户表 %d 张，全局表 %d 张",
                getattr(engine, "url", "?"), len(TENANT_COLUMNS),
                len(GLOBAL_TABLES))


# ======================================================================
# 数据库层策略（真正的边界）
# ======================================================================

def postgres_rls_ddl(*, role: str = "moss_app") -> str:
    """PostgreSQL 原生 RLS 策略 DDL。**由运维执行**，不在应用启动时跑。

    应用层的 `tenant_column_guard()` 与这里的策略是**两层**：
    前者防疏漏，后者才是边界。缺任何一层都不算做完隔离。
    """
    if role not in {"moss_app", "moss_ro", "moss_audit"}:
        raise TenantError(f"未知数据库角色: {role!r}")
    lines = [
        "-- 由 src/infrastructure/security/rls.py:postgres_rls_ddl() 生成",
        "-- 前提：会话变量 app.tenant_id 由连接池在每次取连接时设置",
        "CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS text AS $$",
        "  SELECT current_setting('app.tenant_id', true)",
        "$$ LANGUAGE sql STABLE;",
        "",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role};",
        "",
    ]
    for table, column in sorted(TENANT_COLUMNS.items()):
        lines += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
            f"CREATE POLICY {table}_tenant_isolation ON {table}",
            f"  USING ({column} = current_tenant_id())",
            f"  WITH CHECK ({column} = current_tenant_id());",
            "",
        ]
    lines.append("-- 全局表（审计链等）不加策略，只授最小权限：")
    for table, why in sorted(GLOBAL_TABLES.items()):
        lines.append(f"--   {table}: {why}")
    return "\n".join(lines)


__all__ = [
    "GLOBAL_TABLES",
    "TENANT_COLUMNS",
    "filter_rows",
    "postgres_rls_ddl",
    "register_rls_listeners",
    "tenant_column",
    "tenant_column_guard",
    "tenant_scope",
]
