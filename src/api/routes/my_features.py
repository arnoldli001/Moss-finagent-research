"""当前用户可见的功能与限额（`GET /api/v1/me/features`）。

对应设计：§8.6.10（管理员控制台配置权限）、§4.6（tiers）。

## 前端为什么必须有这个端点

权限是**管理员可配置**的：同一个 VIP 租户，今天可能开着"策略回测"、
明天被关掉。前端如果自己硬编码页签，就会出现两种情况：

  - 配了权限但页面还在（用户点进去全是 403，像系统坏了）；
  - 取消了权限但管理员忘了改前端（等于权限没生效）。

所以**页签列表由服务端下发**：`visible_views` 直接就是该用户能看到的
页签 key 列表，前端照着渲染即可。

## ⚠️ 这只是**体验层**，不是权限边界

隐藏页签 ≠ 禁止访问。真正的门槛在每个业务端点自己身上
（本项目的做法是 `MOSS_TENANCY_ENFORCE` + 能力码，见设计 §4）。
这一点必须写清楚，否则将来有人会以为"前端藏了就安全了"。

例外是 `ADMIN_ONLY_VIEWS`（目前只有 `scheduler`）：那几项**多一道**
"非管理员不下发"的规则，因为对应的路由本身已经挂了 `require_admin`
（两道门，见该常量的说明）。它们不是"体验层"，而是规则层。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from src.api.session_ctx import current_user as _current
from src.domain.platform.config import (
    FEATURES,
    get_platform_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/me", tags=["me"])

#: 页签 key → 决定它可见性的功能 key。
#:
#: 与 `platform/config.py` 的 `FEATURES` **刻意分开**：那边是"可售卖的项"，
#: 这里是"前端有哪些页签"。两者目前一一对应，但将来可能一个页签对应多个
#: 售卖项（例如"量化交易"页签由三个 quant.* 中任意一个开启即可见）——
#: 到那时只改这张表，不动售卖口径。
VIEW_FEATURE: dict[str, str] = {
    "research": "research",
    # ⚠️ `scheduler` / `metrics` 在**本表**（页签清单）里，但**不在**
    # `platform/config.py` 的 `FEATURES`（可售卖项）里。两张表刻意分开 ——
    # 这正是分开的理由：它们是**页签**，但不是能勾选的**套餐项**。
    # 可见性由 `ADMIN_ONLY_VIEWS` 写死（见下）。
    "scheduler": "scheduler",
    "metrics": "metrics",
    "backtest": "backtest",
    "mainline": "mainline",
    "fundflow": "fundflow",
    # 量化交易页：三个子功能**任意一个**开启即显示整页
    # （用户买了做T就该能进这一页；至于页内哪些子面板可用，看子权限）
    "intraday": "quant.intraday",
    # 舆情情报：**两个顶级页签共用一个售卖项**（`intel.hot`）。
    # 用户口径（2026-09-25）："删除一级目录'情报中心'，把二级页签
    # '热点&研报小作文'、'投资日历'改为一级目录"，权限矩阵里
    # "新增对'热点&研报小作文'的权限管理"（同时删掉原「情报雷达」
    # 与「盘前简报」）。
    # 两者本来就同源 —— 同一份公开信息，"现在在说什么"与
    # "接下来会发生什么"两种看法；拆成两个售卖项只会让矩阵变长。
    "intel-hot": "intel.hot",
    "intel-calendar": "intel.hot",
    # 事件告警：**页内能力**，不占顶级页签（前端由铃铛 / Toast / 独立页承载）
    "alerts": "intel.alerts",
}
#: 量化交易页的"任一开启即显示"集合
QUANT_ANY: tuple[str, ...] = ("quant.intraday", "quant.select", "quant.auction")

#: **管理员专属页签**：无论功能权限矩阵怎么配，都不下发给非管理员。
#:
#: ## 为什么是"写死"而不是"矩阵里的默认值"
#:
#: 用户口径（2026-09-25）：
#:
#:     "默认只有管理员有运行指标、调度管理的权限，
#:      不用加在功能权限设置的选项里"
#:     "写死默认管理员有这两个权限，其他用户都没这个权限且不可选择"
#:
#: 所以这两项**不在** `FEATURES` 里（权限矩阵根本渲染不出来，也就无从勾选），
#: 可见性只由本集合决定。为什么不只靠 `configs/platform_tiers.json` 的默认值
#: （那里 vip/trial 的 `scheduler` / `metrics` 本来也都是 `false`）：
#: 那是**可改的默认值**，不是一条规则 —— 只要它们在矩阵里露过面，
#: 管理员勾一下就发出去了，而「调度管理」能手动触发作业
#: （`snapshot_industry_watchlist` 一点 = 4 次完整研究图，
#: 见 `src/scheduler/registry.py`）。这类"能触发内部作业"的能力属于
#: **运维面**，不该作为可售卖套餐项下发给客户。
#:
#: ⚠️ 用户在 2026-09-25 明确说了"可以不删除"—— 所以
#: `configs/platform_tiers.json` 里的这两个键**保留**，
#: 只是已不被任何判据读取（删了反而要在多处解释"为什么没有"）。
#:
#: 与 `src/api/routes/scheduler.py` 的 `require_admin` 是**两道**门：
#: 这里管"看不看得见"，那里管"调不调得动" —— 缺了后者，藏起来的页签
#: 用一条 `curl` 就能绕过（本文件开头「只是体验层」那段说的就是这件事）。
ADMIN_ONLY_VIEWS: frozenset[str] = frozenset({"scheduler", "metrics"})


def feature_enabled_for_tier(tier: str, feature: str) -> bool:
    """纯函数：某等级是否开启某功能。便于单测，不依赖请求上下文。"""
    try:
        plan = get_platform_config().plan(tier)
    except Exception:  # noqa: BLE001 配置读取失败按"未开启"处理（fail-closed）
        return False
    return bool(plan.features.get(feature, False))


async def require_feature(request: Request, feature: str) -> tuple[str, str]:
    """**套餐功能门**：未开启则 403。返回 `(user_id, tier)`。

    与既有的两道门配合（**三层各管一件事**）：

    | 层 | 管什么 | 在哪 |
    |---|---|---|
    | `LoginGateMiddleware` | 登录了没有 | `src/api/login_gate.py`（自动，新路由默认需登录） |
    | **本函数** | **这个等级买了没有** | 各路由显式调用 |
    | `core.policy` | 这个角色能不能做这个动作 | 写操作 / 敏感读 |

    为什么用 403 而不是 404：前端要能区分"没权限"与"路径不存在"，
    后者会让用户以为链接失效。**不过情报接口整体建议不暴露存在性** ——
    见 `docs/INTEL_PERMISSION_DESIGN.md`，若要隐藏存在性，
    调用方可在 `HIDE_EXISTENCE` 打开时改抛 404。
    """
    user_id, tier = await _current(request, write=False)
    if not feature_enabled_for_tier(tier, feature):
        raise HTTPException(
            status_code=403,
            detail={"code": "feature_disabled",
                    "message": f"当前套餐未开通该功能（{FEATURES.get(feature, feature)}），"
                               f"请联系管理员开通"})
    return user_id, tier


@router.get("/features")
async def my_features(request: Request) -> dict:
    """我当前等级能看哪些页签 + 我的资源上限 + 功能定价。"""
    user_id, tenant_id = await _current(request, write=False)
    store = get_platform_config()
    plan = store.plan(tenant_id)

    enabled = {k: bool(plan.features.get(k, False)) for k in FEATURES}
    # 管理员身份用既有的那一个判据（`admin.py` 的 `ADMIN_TIER`），
    # 不在这里再写一遍字面量：两份判据迟早会分叉，而分叉的那一侧就是漏网之门。
    # 延迟导入的写法与 `login_gate._session_cookie_name` 一致（避免路由模块互相 import）。
    from src.api.routes.admin import ADMIN_TIER

    is_admin = str(tenant_id).strip().lower() == ADMIN_TIER
    visible: list[str] = []
    for view, feature in VIEW_FEATURE.items():
        if view in ADMIN_ONLY_VIEWS:
            # ⚠️ **不能**走下面的 `enabled.get(feature)` 判据。
            # `enabled` 是按 `FEATURES` 构造的，而管理员专属的两项
            # （scheduler / metrics）**已经不在 `FEATURES` 里** ——
            # 一查就是 False，结果连管理员都看不到「运行指标」和「调度管理」。
            # 这正是把它们移出 `FEATURES` 时必须一起改的地方：
            # 少改这一处，表现是"管理员自己少了两个页签"。
            if is_admin:
                visible.append(view)
            continue
        if view == "intraday":
            if any(enabled.get(f, False) for f in QUANT_ANY):
                visible.append(view)
        elif enabled.get(feature, False):
            visible.append(view)

    return {
        "user_id": user_id,
        "tier": tenant_id,
        "tier_label": plan.label,
        "features": enabled,
        "feature_labels": dict(FEATURES),
        # ★ 前端**照着这个渲染页签**，而不是自己判断哪个 features 对应哪个页签
        "visible_views": visible,
        # 管理员专属页签（供前端标注"这里为什么少了一页"，而不是静默消失）
        "admin_only_views": sorted(ADMIN_ONLY_VIEWS),
        "is_admin": is_admin,
        "quant_views": {k: enabled.get(k, False) for k in QUANT_ANY},
        "resources": plan.resources,
        # 加购单价随套餐一起下发（前端暂不展示，但账单/商务后台要用）。
        # ⚠️ **不含套餐月费**（`monthly_price`）：该字段已下线
        # （用户口径 2026-09-23，定价不由本系统维护）。
        "pricing": plan.pricing,
        "note": "visible_views 只决定**页签是否渲染**；真正的权限校验在"
                "各业务端点（本接口不构成安全边界）。",
    }


__all__ = ["ADMIN_ONLY_VIEWS", "QUANT_ANY", "VIEW_FEATURE", "router"]
