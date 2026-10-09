"""做T自选的**按账号归属**（2026-10-08，`CHG-0222` / `CHG-0224` / `CHG-0227`）。

## 一句话

自选清单从"服务端一份 `configs/intraday.yaml`"改成"**每个账号一份**"
（存在 `dim_user_watchlist_v2`，见 `docs/PRD.md` §50.5/§50.6）。

## 为什么用 contextvar，而不是给每个方法加 `owner` 参数

`IntradayService` 里"当前是谁的自选"被十几处用到 —— 列表（`watchlist`）、
加/删/置顶、每只票的板块与同业（`config.watch(code)`）、快照、日K、回测、
板板块预热、报价快车道。逐个加参数意味着改十几个签名 + 几十个既有测试，
而"这个请求是谁"本来就是**请求级上下文**：本仓库已有同款范式
（`src/core/tenancy.principal_scope`、`src/api/accounting_middleware` 的
`AccountingContext`）。

所以：

- **HTTP**：路由入口 `with owner_scope(user_id, tenant_id):`（见 `routes/intraday.py`）；
- **WebSocket**：按**连接**包一层（`ws_identity` 拿到的人）；
- **后台作业**：不设 owner ⇒ `SYSTEM`。

## SYSTEM（无身份）看到什么 —— 这是**语义**，不是降级

后台作业（`intraday_t_scan`、`daily_warm`、报价快车道）没有"谁"这回事，
它们要的是"**所有人在看的票**"：`SYSTEM` = 全体账号自选代码的**并集**（去重）。

**并集为空时**回落到 `configs/intraday.yaml` 的 `watchlist`：那是"还没跑迁移的
老装机"的兼容路径（迁移见本模块 `migrate_legacy_watchlist`），会打一条 INFO。
**只有 SYSTEM 有这条回落** —— 登录用户永远只看自己的行，一条都不"继承"。

## 空身份的写路径必须**报错**，不许回落

`add_item/remove_item/pin_item` 一律要求 owner；空 owner 抛
`WatchOwnerRequired`。理由：`alerts._viewer_user_id` 那条"拿不到身份就退回旧的
全局行为"在**读**上勉强可接受，在**写**上等于隔离静默失效 —— 用户点一下
"加自选"，结果写进了大家共用的清单。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from src.infrastructure.repositories.user_pool_sqlite_repo import (
    PoolValidationError,
    WatchRecord,
    check_add_stock,
    get_profile_repo,
    normalize_code,
)

logger = logging.getLogger(__name__)

#: 每个账号的默认自选池 id。**必须带 user_id**：`dim_user_pool` 的主键是
#: `pool_id` 一列（全局唯一），用固定值会让第二个账号建池时撞主键。
POOL_PREFIX = "watch_"
POOL_NAME = "自选"

#: 与板块迁移同一套政策：存量归属可由运维覆盖（user_id 或 username）。
LEGACY_OWNER_ENV = "MOSS_LEGACY_WATCH_OWNER"


class WatchOwnerRequired(RuntimeError):
    """写自选时没有身份。**刻意用异常而不是静默写公共清单**（见模块文档）。"""


# ======================================================================
# 当前 owner（请求级上下文）
# ======================================================================

_owner: ContextVar[tuple[str, str] | None] = ContextVar(
    "intraday_watch_owner", default=None)

#: 无身份的语义值：后台作业。**不是**"身份丢了"。
SYSTEM: tuple[str, str] = ("", "")


@contextmanager
def owner_scope(user_id: str, tenant_id: str = "") -> Iterator[None]:
    """把"当前是谁的自选"作用到这一段上下文（HTTP 路由 / WS 连接用）。"""
    token = _owner.set((str(user_id or ""), str(tenant_id or "")))
    try:
        yield
    finally:
        _owner.reset(token)


def current_owner() -> tuple[str, str]:
    """当前的 `(user_id, tenant_id)`；没有就是 `SYSTEM`。"""
    return _owner.get() or SYSTEM


def has_owner() -> bool:
    return bool(current_owner()[0])


def owner_key(owner: tuple[str, str] | None = None) -> str:
    """缓存键：`""` = SYSTEM（并集），否则是 user_id。"""
    return (owner or current_owner())[0]


def pool_id_for(user_id: str) -> str:
    return f"{POOL_PREFIX}{user_id}"


# ======================================================================
# 存储（`dim_user_watchlist_v2`，按 user_id 定位）
# ======================================================================

def _repo() -> Any:
    return get_profile_repo()


def ensure_default_pool(user_id: str, tenant_id: str = "") -> str:
    """确保该账号有默认自选池（幂等）。

    为什么需要池行：`add_stock` 的配额校验要按池类型算（`watch` vs `sector`），
    而且"池数"本身就是套餐额度的一项。池 id 带 user_id（见 `POOL_PREFIX` 注释）。
    """
    clean = str(user_id or "")
    if not clean:
        raise WatchOwnerRequired("自选必须绑定账号：user_id 不能为空")
    pool_id = pool_id_for(clean)
    try:
        _repo().create_pool(pool_id=pool_id, tenant_id=str(tenant_id or ""),
                            user_id=clean, name=POOL_NAME, kind="watch")
    except sqlite3.IntegrityError:
        # 已存在（PK 冲突）—— 幂等路径，不是错误
        pass
    return pool_id


def list_items(user_id: str) -> list[WatchRecord]:
    """该账号的自选条目（只按 user_id 查；顺序=置顶在前、新加的在前）。"""
    clean = str(user_id or "")
    if not clean:
        raise WatchOwnerRequired("自选必须绑定账号：user_id 不能为空")
    return _repo().list_watch_by_owner(clean)


def add_item(user_id: str, tenant_id: str, *, code: str, name: str = "",
             boards: object = (), peers: object = (), overseas: object = (),
             industry: str = "", pinned: bool = False,
             quota: Any = None) -> WatchRecord:
    """加/更新一只自选（配额由调用方传入的 `quota` 决定，见 `QuotaService`）。"""
    clean = str(user_id or "")
    if not clean:
        raise WatchOwnerRequired("加自选必须绑定账号：user_id 不能为空")
    pool_id = ensure_default_pool(clean, tenant_id)
    clean_code = normalize_code(code)          # 非法代码在这里就抛（不用 zfill 兜底）
    if quota is not None:
        mine = _repo().list_watch_by_owner(clean)
        mine_codes = {r.code for r in mine}
        # ⚠️ **已在这只票上改配置**（补板块/改名）不算"新增额度"。
        # 不特判的话 `check_add_stock` 的单池校验（只看数量、不看"这只已在池里"）
        # 会在池满时把"改一下已有自选的关联板块"也拒掉 —— 用户看到的是
        # "我什么都没加，它却说满了"。这是本层自己的判据，所以在本层特判。
        if clean_code not in mine_codes:
            check = check_add_stock(
                quota=quota, pool_id=pool_id, code=clean_code,
                pool_counts={pool_id: sum(1 for r in mine
                                          if r.pool_id == pool_id)},
                distinct_codes=mine_codes, pool_kind="watch")
            if not check.ok:
                raise PoolValidationError(check.code, check.reason)
    return _repo().upsert_watch_by_owner(
        user_id=clean, tenant_id=tenant_id, pool_id=pool_id, code=clean_code,
        name=name, industry=industry, boards=boards, peers=peers,
        overseas=overseas, pinned=pinned)


def remove_item(user_id: str, code: str) -> bool:
    clean = str(user_id or "")
    if not clean:
        raise WatchOwnerRequired("删自选必须绑定账号：user_id 不能为空")
    # 幂等：不在池里也返回 False 而不是抛（前端会重复点）
    return _repo().remove_watch_by_owner(
        user_id=clean, pool_id=pool_id_for(clean), code=code)


def pin_item(user_id: str, code: str, pinned: bool) -> bool:
    clean = str(user_id or "")
    if not clean:
        raise WatchOwnerRequired("置顶必须绑定账号：user_id 不能为空")
    changed = _repo().pin_watch_by_owner(
        user_id=clean, pool_id=pool_id_for(clean), code=code, pinned=pinned)
    return bool(changed)


# ---------------------------------------------------------------- 异步口

async def a_list_items(user_id: str) -> list[WatchRecord]:
    return await asyncio.to_thread(list_items, user_id)


async def a_add_item(user_id: str, tenant_id: str, **kwargs: Any) -> WatchRecord:
    return await asyncio.to_thread(add_item, user_id, tenant_id, **kwargs)


async def a_remove_item(user_id: str, code: str) -> bool:
    return await asyncio.to_thread(remove_item, user_id, code)


async def a_pin_item(user_id: str, code: str, pinned: bool) -> bool:
    return await asyncio.to_thread(pin_item, user_id, code, pinned)


# ======================================================================
# SYSTEM（作业）视角：全体并集 + 老机器兼容
# ======================================================================

def system_codes() -> list[str]:
    """全体账号自选代码的并集（去重）。"""
    return _repo().owner_watch_codes()


def legacy_yaml_codes(config: Any) -> list[str]:
    """`configs/intraday.yaml` 里的 watchlist 代码（老机器兼容路径）。"""
    return [str(item.code) for item in (getattr(config, "watchlist", None) or [])]


def all_codes_for_system(config: Any) -> tuple[list[str], str]:
    """SYSTEM 视角的代码清单 + 来源说明（给日志/接口状态用）。

    返回 `(codes, source)`，`source ∈ {"owners", "legacy-yaml", "empty"}`。
    """
    codes = system_codes()
    if codes:
        return codes, "owners"
    legacy = legacy_yaml_codes(config)
    if legacy:
        logger.info("自选并集为空 ⇒ 回落到 configs/intraday.yaml 的 watchlist"
                    "（%d 只，老机器兼容路径；迁移见 user_watch.migrate_legacy_watchlist）",
                    len(legacy))
        return legacy, "legacy-yaml"
    return [], "empty"


# ======================================================================
# 存量迁移：共享 YAML → 归属最早的管理员
# ======================================================================

def _legacy_owner() -> tuple[str, str] | None:
    """存量自选归给谁：该环境**最早的管理员**（可用 `MOSS_LEGACY_WATCH_OWNER` 覆盖）。

    与板块迁移（`quant_select_repo.resolve_legacy_owner`）同一套政策：
    YAML 里没有"这行是谁写的"，归属只能政策裁定，判错可重跑。
    """
    from src.core.config import get_settings

    override = os.environ.get(LEGACY_OWNER_ENV, "").strip()
    db_path = get_settings().sqlite_path
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        if override:
            row = conn.execute(
                "SELECT user_id, applied_tier FROM dim_user"
                " WHERE user_id = ? OR username = ? LIMIT 1",
                (override, override)).fetchone()
            if row is not None:
                return str(row["user_id"]), str(row["applied_tier"] or "")
            logger.warning("%s=%r 查不到，回落到最早的管理员",
                           LEGACY_OWNER_ENV, override)
        row = conn.execute(
            "SELECT user_id, applied_tier FROM dim_user"
            " WHERE applied_tier = 'admin' AND status = 'active'"
            " ORDER BY created_at LIMIT 1").fetchone()
    except sqlite3.Error:
        return None                     # 该库没有 dim_user（单测临时库等）
    finally:
        conn.close()
    if row is None:
        return None
    return str(row["user_id"]), str(row["applied_tier"] or "")


def migrate_legacy_watchlist(config: Any, *,
                             owner: tuple[str, str] | None = None,
                             dry_run: bool = False) -> dict[str, Any]:
    """把共享 YAML 的自选清单落到**该环境最早的管理员**名下（幂等）。

    ## 幂等判据是"表里一个人都还没有"，不是"跑过几次"

    没有额外的迁移标记表：**只要 `dim_user_watchlist_v2` 已有任何行，就不迁移**
    （那说明按账号自选已经在用了）。这比"记一个 flag"更稳 —— 库被回滚或换环境时，
    flag 会撒谎，而"表里有没有行"永远是事实。

    ## 没有管理员 ⇒ 一行都不迁（与板块同一政策）

    绝不把运营者的清单随手分给某个 VIP 客户。
    """
    report: dict[str, Any] = {"migrated": False, "reason": "", "owner": "",
                              "rows": 0, "needs_owner": False}
    existing = _repo().owner_watch_codes()
    if existing:
        report["reason"] = f"已有按账号自选（{len(existing)} 只），跳过"
        return report
    items = list(getattr(config, "watchlist", None) or [])
    if not items:
        report["reason"] = "旧清单为空，无需迁移"
        return report
    if owner is None:
        owner = _legacy_owner()
    report["needs_owner"] = owner is None
    if owner is None:
        report["reason"] = "该环境没有 active 的管理员账号 → 不迁移（个人自选从空开始）"
        logger.warning("⚠️ 自选迁移：%s（旧清单 %d 只留在 configs/intraday.yaml，"
                       "可用 %s=<user_id|username> 指定归属后重启）",
                       report["reason"], len(items), LEGACY_OWNER_ENV)
        return report
    report["owner"] = f"{owner[0]}（{owner[1] or '未知档位'}）"
    if dry_run:
        report["rows"] = len(items)
        report["reason"] = "[dry-run] 迁移完成"
        return report
    ensure_default_pool(*owner)
    for item in items:
        add_item(owner[0], owner[1], code=item.code, name=item.name or "",
                 boards=list(getattr(item, "boards", []) or []),
                 peers=list(getattr(item, "peers", []) or []),
                 overseas=list(getattr(item, "overseas", []) or []),
                 industry=str(getattr(item, "industry", "") or ""),
                 pinned=bool(getattr(item, "pinned", False)))
        report["rows"] += 1
    report["migrated"] = True
    report["reason"] = "迁移完成"
    logger.warning("自选归属迁移：旧共享清单 %d 只 → %s 名下；"
                   "其他账号的自选从空开始", report["rows"], report["owner"])
    return report
