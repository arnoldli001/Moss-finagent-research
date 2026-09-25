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
    "scheduler": "scheduler",
    "metrics": "metrics",
    "backtest": "backtest",
    "mainline": "mainline",
    "fundflow": "fundflow",
    # 量化交易页：三个子功能**任意一个**开启即显示整页
    # （用户买了做T就该能进这一页；至于页内哪些子面板可用，看子权限）
    "intraday": "quant.intraday",
    # 舆情情报页：`intel.radar` 是主入口 —— 开它才显示「情报雷达」页签。
    # 另外两项（`intel.brief` / `intel.alerts`）是**页内**能力，
    # 由 `require_feature` 在各自路由上单独把关，不各占一个顶层页签。
    "intel": "intel.radar",
}
#: 量化交易页的"任一开启即显示"集合
QUANT_ANY: tuple[str, ...] = ("quant.intraday", "quant.select", "quant.auction")


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
    visible: list[str] = []
    for view, feature in VIEW_FEATURE.items():
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
        "quant_views": {k: enabled.get(k, False) for k in QUANT_ANY},
        "resources": plan.resources,
        # 加购单价随套餐一起下发（前端暂不展示，但账单/商务后台要用）。
        # ⚠️ **不含套餐月费**（`monthly_price`）：该字段已下线
        # （用户口径 2026-09-23，定价不由本系统维护）。
        "pricing": plan.pricing,
        "note": "visible_views 只决定**页签是否渲染**；真正的权限校验在"
                "各业务端点（本接口不构成安全边界）。",
    }


__all__ = ["QUANT_ANY", "VIEW_FEATURE", "router"]
