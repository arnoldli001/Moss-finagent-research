"""个股口径（权重/阈值）API —— 每用户各自一份。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §7.2.6③（copy-on-write）、
§6.1（做T模块多用户化）。

## 这一层解决什么问题

个股口径原先住在 `dim_intraday_profile`，而那张表的**主键是 `code` 一列**
（"一只票全系统一份权重"）。多用户下这是**结构性的错误**：
A 把 600519 的箱体权重调到 17，B 看到的也是 17 —— 而两个人对同一只票
本来就该有不同的参数。

新表 `dim_intraday_profile_v2` 的主键是
`(tenant_id, user_id, code, mode)`，本模块是它的 HTTP 出口。

## copy-on-write：为什么读接口要返回 `from_user`

生效口径按三级回退：**用户档案 → 租户模板 → 内置默认**。
读接口必须告诉前端"这条是**你自己调的**，还是跟着系统默认走的" ——
否则界面上两种情况长得一模一样，用户会以为"我明明没调过，为什么有值？"
（更糟的是他会去"改"，于是凭空物化出一份本来不需要存在的档案）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.session_ctx import current_user as _current
from src.core.errors import brief
from src.infrastructure.repositories.user_pool_sqlite_repo import (
    PoolValidationError,
    get_profile_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/me/profiles", tags=["user-profiles"])

#: 一次最多返回多少条（防止全量拉取把响应撑爆）
LIST_LIMIT = 500


class SaveProfileBody(BaseModel):
    code: str = Field(description="6 位标的代码")
    mode: str = Field(default="intraday", description="intraday（做T）| daily（日线）")
    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    levels: dict[str, float] = Field(default_factory=dict)
    #: 关联板块 / 海外映射：与权重一起存，但**不影响口径指纹**（见 `caliber_key`）
    boards: list[str] = Field(default_factory=list)
    overseas: list[str] = Field(default_factory=list)


def _repo() -> Any:
    """个股口径的存储（与自选池共用同一张库表文件，但**只剩这张表在用**）。

    自选池功能已删除，这里不再借 `QuotaService` 拿 repo —— 直接走专用工厂，
    少一层"用配额服务当仓储句柄"的间接。
    """
    return get_profile_repo()


def _profile_json(p: Any) -> dict[str, Any]:
    """档案 → 前端结构。

    ★ 一定带上 `from_user` 与 `source`：前端据此显示
    "已自定义" / "跟随系统默认"。少了它，界面无法区分两种来源。
    ★ 带 `caliber_key`：它是"口径指纹"（只由 mode/权重/阈值/档位决定）。
      相同指纹的两个用户可以共用一次计算 —— 这正是"计算可共享、
      参数不共享"的落点，前端也用它在界面上提示"与系统默认一致"。
    """
    return {
        "code": p.code,
        "mode": p.mode,
        "name": p.name,
        "weights": p.weights,
        "thresholds": p.thresholds,
        "levels": p.levels,
        "boards": list(p.boards),
        "overseas": list(p.overseas),
        "from_user": bool(p.from_user),
        "source": p.source,
        "caliber_key": p.caliber_key,
        "updated_at": p.updated_at,
    }


@router.get("")
async def list_profiles(request: Request, mode: str = "") -> dict:
    """我**自己调过**的口径（不含继承项）。

    只列自己调过的，而不是把"所有票 × 三级回退结果"都算出来 ——
    后者在 100 只自选 × 每人一份的情况下是纯浪费，而且没有任何意义
    （没调过的票本来就是"跟系统默认走"）。
    """
    user_id, tenant_id = await _current(request, write=False)
    rows = await _repo().a_list_profiles(tenant_id, user_id, mode=mode)
    return {"profiles": [_profile_json(p) for p in rows[:LIST_LIMIT]],
            "total": len(rows)}


@router.get("/{code}")
async def effective_profile(code: str, request: Request,
                            mode: str = "intraday") -> dict:
    """**生效口径**（三级回退后的结果）+ 说明它是从哪来的。

    这是前端编辑面板的初始值来源：用户打开某只票的"口径"页时，
    看到的应该是**他实际生效的那份**，而不是空表单。
    """
    user_id, tenant_id = await _current(request, write=False)
    try:
        p = await _repo().a_effective_profile(tenant_id=tenant_id,
                                              user_id=user_id, code=code,
                                              mode=mode)
    except PoolValidationError as exc:
        raise HTTPException(status_code=422,
                            detail={"code": exc.code,
                                    "message": exc.message}) from exc
    body = _profile_json(p)
    body["explain"] = _explain(p)
    return body


@router.put("/{code}")
async def save_profile(code: str, body: SaveProfileBody,
                       request: Request) -> dict:
    """保存**我自己的**口径（copy-on-write 的物化点）。

    ⚠️ 这里**只**写当前用户的档案 —— `user_id` 由会话决定，
    请求体里没有、也不接受 `user_id`。让前端传 user_id 是越权的第一步。
    """
    user_id, tenant_id = await _current(request)
    if body.mode not in {"intraday", "daily"}:
        raise HTTPException(status_code=400, detail="mode 只能是 intraday 或 daily")
    _validate_numbers(body.weights, "weights")
    _validate_numbers(body.thresholds, "thresholds")
    _validate_numbers(body.levels, "levels")
    try:
        saved = await _repo().a_save_profile(
            tenant_id=tenant_id, user_id=user_id, code=code, mode=body.mode,
            weights=body.weights, thresholds=body.thresholds,
            levels=body.levels, boards=body.boards, overseas=body.overseas)
    except PoolValidationError as exc:
        raise HTTPException(status_code=422,
                            detail={"code": exc.code,
                                    "message": exc.message}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=brief(exc)) from exc
    logger.info("口径已保存：user=%s code=%s mode=%s 权重=%d 项",
                user_id, code, body.mode, len(body.weights))
    return {"ok": True, "message": f"已保存 {saved.code} 的口径", "profile": _profile_json(saved)}


@router.delete("/{code}")
async def reset_profile(code: str, request: Request,
                        mode: str = "intraday") -> dict:
    """**还原系统默认** = 删掉我自己的那份，回退到模板/内置默认。

    刻意不是"把默认值写进我自己的档案"：那样做之后，
    将来系统默认值升级就再也到不了这个用户（他被钉在了旧默认值上）。
    """
    user_id, tenant_id = await _current(request)
    removed = await _repo().a_delete_profile(tenant_id=tenant_id,
                                             user_id=user_id, code=code,
                                             mode=mode)
    if not removed:
        return {"ok": True, "removed": False,
                "message": "这只票本来就没有自定义口径（已在跟随系统默认）"}
    after = await _repo().a_effective_profile(tenant_id=tenant_id,
                                              user_id=user_id, code=code,
                                              mode=mode)
    return {"ok": True, "removed": True,
            "message": "已还原为系统默认",
            "profile": _profile_json(after)}


def _validate_numbers(payload: dict[str, float], label: str) -> None:
    """数值必须有限且非负。

    为什么不让 NaN/Infinity 进库：它们会**静默污染后续所有计算**
    （总分变成 NaN，前端显示"NaN 分"而没有任何报错），
    而且 JSON 里的 `NaN` 不是合法 JSON，Python 却默认接受 ——
    必须在入口挡住。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{label} 必须是对象")
    for key, value in payload.items():
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{label}.{key} 不是数字：{value!r}") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise HTTPException(
                status_code=400,
                detail=f"{label}.{key} 不是有限数字（NaN/Infinity 会污染全部计算）")


def _explain(p: Any) -> str:
    """给用户看的一句话来源说明（前端直接显示，避免自己拼文案）。"""
    if p.from_user:
        return "这是你自己调整过的口径。"
    if p.source == "tenant_template":
        return "这是租户统一设定的口径（你没单独调过这只票）。"
    return "这是系统内置默认口径（你没单独调过这只票，也没有租户模板）。"


__all__ = ["router"]
