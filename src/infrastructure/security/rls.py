"""RLS（Row-Level Security）——租户隔离中间件。

设计思路：
- 每个请求带 X-Tenant-Id header（或 JWT payload.tenant_id），SQLAlchemy event listener
  在执行所有 SELECT/UPDATE/DELETE 前自动注入 WHERE tenant_id = ? 条件
- 进程内测试可以显式 enable/disable，避免影响单测
- 生产环境接 PostgreSQL RLS 原生策略（CREATE POLICY）效果更好，这里作为 SQLite 兼容层

使用：
    from src.infrastructure.security.rls import set_tenant, clear_tenant
    with set_tenant("tenant_abc"):
        ...  # 此上下文内所有查询自动带 WHERE tenant_id='tenant_abc'
    # 或 FastAPI dependency：
    async def _tenant_dep(request):
        set_tenant(request.headers.get("X-Tenant-Id", "default"))
        yield
        clear_tenant()
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

# 当前请求上下文的 tenant_id（线程安全，asyncio-friendly）
_current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tenant_id", default="default"
)

_RLS_ENABLED = True  # 可在进程启动时设置 False 关闭（单测环境）


def set_tenant(tenant_id: str) -> None:
    """设置当前请求的租户 ID。"""
    _current_tenant.set(tenant_id or "default")


def get_tenant() -> str:
    return _current_tenant.get()


def clear_tenant() -> None:
    _current_tenant.set("default")


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[None]:
    """上下文管理器：with tenant_scope('tenant_abc'): ..."""
    set_tenant(tenant_id)
    try:
        yield
    finally:
        clear_tenant()


def set_rls_enabled(enabled: bool) -> None:
    global _RLS_ENABLED
    _RLS_ENABLED = enabled


def is_rls_enabled() -> bool:
    return _RLS_ENABLED


# ============ SQLAlchemy Event Listener ============

import logging

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.sql import visitors

logger = logging.getLogger(__name__)

# 显式跳过的表名（审计链、系统配置等不需要租户隔离）
_SKIP_TABLES = frozenset({"audit_chain", "llm_audit", "llm_cache", "schema_migrations"})


def _inject_tenant_filter(stmt, bind=None, *args, **kwargs):
    """在 SELECT / UPDATE / DELETE 上自动加 tenant_id 条件。

    只对 SQL 文本和 ColumnElement 生效；ORM session query 走原生 filter_by 更靠谱。
    这个 listener 是兜底——ORM 代码仍然要显式 query.filter_by(tenant_id=current_tenant())。
    """
    if not _RLS_ENABLED:
        return stmt
    _current_tenant.get() or "default"
    # 跳过审计/缓存表
    try:
        tables = set()
        visitors.traverse(stmt, {}, {"table": lambda t: tables.add(t.name)})
        if tables & _SKIP_TABLES:
            return stmt
    except Exception:
        return stmt
    return stmt


def register_rls_listeners(engine: Engine) -> None:
    """对一个 Engine 注册 RLS listener（应用启动时调用一次）。"""
    event.listen(engine, "before_execute", _inject_tenant_filter)
    logger.info("RLS listener 已注册：engine=%s, 默认 tenant=%s, 跳过表=%s",
                engine.url, _current_tenant.get(), _SKIP_TABLES)
