"""套餐配额：把 `applied_tier` 映射成可执行的额度，并在服务端强制。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §4.6（tiers 表）、§7.2.6①（三重配额）。

## 为什么必须有这一层（不能让调用方手写 `quota=`）

`UserPoolSqliteRepository` 的 `add_stock(quota=...)` 是**可选**参数：
不传就完全不校验。这在单用户时代无所谓，多用户下就是**额度形同虚设** ——
只要有一个调用点忘了传，那个入口就是无限额度。

所以本模块的定位是：
  - 唯一一处**定义**"哪个等级对应多少额度"（不含在任何路由里）；
  - 唯一一处**读取**当前用户的等级并产出 `PoolQuota`；
  - 路由只做 `await quota_for(user_id)` → 传下去，**不再自己判断等级**。

## 一个刻意的设计：等级取不到时给"最小额度"而不是"不限制"

fail-open（拿不到等级就放行）在额度场景下是最糟的选择：
一个空等级的用户（比如刚审批完还没分配套餐）会获得**无限额度**。
这里 fail-closed 到试用档 —— 额度小但可用，管理员分配后自然放开。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from src.infrastructure.repositories.user_pool_sqlite_repo import (
    PoolQuota,
    UserPoolSqliteRepository,
)

logger = logging.getLogger(__name__)

#: 等级 → 额度。**只在这里定义**（路由、前端都从这里推导，不各自写一份）。
#:
#: 口径来自已确认的需求（§附录 D）：
#:   - 每用户 5 个自选池；
#:   - 管理员单池 ≤100 只、VIP 总数 ≤100 只、试用 5 池 × 5 只 = 25 只；
#:   - 自定义板块单板块 ≤200 只，且**不占**自选总数（数据侧不参与实时宇宙）。
TIER_QUOTAS: Final[dict[str, PoolQuota]] = {
    "admin": PoolQuota(pool_limit=5, pool_size_limit=100, watchlist_limit=100,
                       sector_limit=20, sector_size_limit=200),
    "vip": PoolQuota(pool_limit=5, pool_size_limit=100, watchlist_limit=100,
                     sector_limit=20, sector_size_limit=200),
    "trial": PoolQuota(pool_limit=5, pool_size_limit=5, watchlist_limit=25,
                       sector_limit=5, sector_size_limit=50),
}

#: 取不到等级时的兜底额度（**fail-closed**，理由见模块文档）。
FALLBACK_TIER: Final = "trial"


class QuotaService:
    """按用户产出配额。

    ## 为什么需要**两个**仓储（这是踩过的坑）

    | 需要什么 | 从哪读 |
    |---|---|
    | 用户的 `applied_tier` | **认证仓储**（`dim_user`） |
    | 池与条目 | 用户池仓储（`dim_user_pool` / `dim_user_watchlist_v2`） |

    第一版只注入了用户池仓储，结果 `quota_for()` 调 `a_get_user()` 直接
    `AttributeError` —— 用户池仓储里根本没有"用户"这个概念（它只管池）。
    表虽然同库，但**职责不同**，混用会在运行期才炸。

    ## 为什么每次都用仓储读等级，而不是从会话里取

    会话里的 `applied_tier` 是**登录那一刻**的快照。管理员中途给用户升级
    （或降级、禁用）之后，那个用户手里的旧会话仍带着老等级 ——
    于是"刚给你升成 VIP"要等他重新登录才生效，而"刚把你降级/禁用"
    同样要等重新登录，这在**降级**方向上是安全问题。
    直接读库则永远是最新值；这是一次极轻的主键查询（实测亚毫秒）。
    """

    def __init__(self, auth_repo: Any, pool_repo: Any) -> None:
        self._auth = auth_repo
        self._repo = pool_repo

    def quota_for_tier(self, tier: str) -> PoolQuota:
        """等级 → 额度（纯函数，便于单测穷举）。"""
        value = str(tier or "").strip().lower()
        if value in TIER_QUOTAS:
            return TIER_QUOTAS[value]
        if value:
            logger.warning("未知套餐等级 %r，按最小额度 %s 处理",
                           tier, FALLBACK_TIER)
        return TIER_QUOTAS[FALLBACK_TIER]

    async def quota_for(self, user_id: str) -> PoolQuota:
        """当前用户的额度（**读认证库取最新等级**）。"""
        if not str(user_id or "").strip():
            raise ValueError("user_id 必填：配额必须绑定到具体用户")
        user = await self._auth.a_get_user(user_id)
        if user is None:
            # 用户不存在/已删除 → 最小额度。**不要**抛异常打断请求：
            # 用户可能刚好在被删除的瞬间发来请求，报"额度不足"比 500 更合适。
            logger.info("配额查询：用户 %s 不存在，按最小额度处理", user_id)
            return TIER_QUOTAS[FALLBACK_TIER]
        return self.quota_for_tier(user.applied_tier)

    async def usage(self, *, tenant_id: str, user_id: str) -> dict[str, Any]:
        """额度 + 已用量（前端展示"3/5 个池 · 12/100 只"）。

        `tenant_id` 必须由调用方传入（从会话里取），不能在这里猜 ——
        库里的行按 `(tenant_id, user_id)` 联合定位，传空串会查不到任何行，
        表现为"明明有 12 只自选，界面显示 0/100"。
        """
        quota = await self.quota_for(user_id)
        pools = await self._repo.a_list_pools(tenant_id, user_id)
        counts = await _to_thread(self._repo.pool_counts, tenant_id, user_id)
        distinct = await self._repo.a_distinct_codes(tenant_id, user_id)
        watch_pools = [p for p in pools if p.kind == "watch"]
        sector_pools = [p for p in pools if p.kind == "sector"]
        return {
            "quota": _quota_json(quota),
            "pools_used": len(watch_pools),
            "pool_limit": quota.pool_limit,
            "stocks_used": len(distinct),
            "stock_limit": quota.watchlist_limit,
            "sectors_used": len(sector_pools),
            "sector_limit": quota.sector_limit,
            # 每个池的已用条数：前端要给每个池显示"12/100"
            "pool_sizes": counts,
            "pool_size_limit": quota.pool_size_limit,
        }


def _quota_json(quota: PoolQuota) -> dict[str, int]:
    return {
        "pool_limit": quota.pool_limit,
        "pool_size_limit": quota.pool_size_limit,
        "watchlist_limit": quota.watchlist_limit,
        "sector_limit": quota.sector_limit,
        "sector_size_limit": quota.sector_size_limit,
    }


async def _to_thread(fn: Any, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(fn, *args)


# ======================================================================
# 进程内单例（与 `get_auth_service()` 同风格）
# ======================================================================

_SERVICE: QuotaService | None = None


def get_quota_service() -> QuotaService:
    """取配额服务（惰性单例）。

    ★ 两个仓储**必须来自同一个库路径**，否则会出现
    "登录用的是 A 库、配额查的是 B 库" → 读不到用户 → 永远按试用档处理
    （而且不报错，只是额度悄悄变小）。
    所以这里直接复用 `get_auth_service()` 已装配好的**认证仓储**，
    再用同一路径建用户池仓储。
    """
    global _SERVICE  # noqa: PLW0603 进程内单例
    if _SERVICE is None:
        from src.api.routes.auth import get_auth_service
        from src.core.config import get_settings

        auth_repo = get_auth_service()._repo  # noqa: SLF001
        db_path = str(getattr(auth_repo, "_db_path", "")
                      or get_settings().sqlite_path)
        pool_repo = UserPoolSqliteRepository(db_path)
        pool_repo.ensure_schema()
        _SERVICE = QuotaService(auth_repo, pool_repo)
        logger.warning("配额服务已装配：db=%s", db_path)
    return _SERVICE


def reset_quota_service() -> None:
    """清掉单例（**仅测试用**）。"""
    global _SERVICE  # noqa: PLW0603
    _SERVICE = None


__all__ = [
    "FALLBACK_TIER",
    "TIER_QUOTAS",
    "QuotaService",
    "get_quota_service",
    "reset_quota_service",
]
