"""量化选股 API：3 档模型定时选股结果 + 自定义板块管理。

接口一览：
  GET    /api/v1/quant/select/status                 模块状态（模型/上一轮/是否在跑）
  POST   /api/v1/quant/select/train                  模型缺失时手动补训（自动训练的重试入口）
  POST   /api/v1/quant/select/run                    手动跑一轮（可选限定板块/条数）
  GET    /api/v1/quant/select/runs?limit=20          历史选股记录（含明细）
  GET    /api/v1/quant/select/latest?window=         最新一轮结果
  GET    /api/v1/quant/select/news?codes=&limit=     个股消息面（结果表"个股消息"列）
  POST   /api/v1/quant/select/add-to-watchlist       把某只票加进做T自选（手动确认）
  POST   /api/v1/quant/select/add-to-watchlist-batch 整批加进做T自选（一键全部）
  GET    /api/v1/quant/sectors                       自定义板块列表（含成分）
  POST   /api/v1/quant/sectors                       新建/更新板块（同名即更新）
  DELETE /api/v1/quant/sectors/{sector_id}           删除板块
  PUT    /api/v1/quant/sectors/{sector_id}/members   整表替换成分股
  POST   /api/v1/quant/sectors/{sector_id}/members   增量加成分股（幂等）
  DELETE /api/v1/quant/sectors/{sector_id}/members/{code}  移除单只成分

设计取舍（用户口径 2026-09-18）：

- **选股结果不自动写自选池** —— 只落在模块里，用户点「加自选」才写
  `configs/intraday.yaml`。模型选错时不会污染做T主链路；
- **自定义板块既是选股范围也是归类标签**：`POST /select/run` 传
  `sector_filter` 时把候选池换成这些板块的成分（而不是选完再筛，原因见
  `QuantSelectService._run_sync` 的注释）；结果里每只票也带命中的板块用于归类。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_LOG,
    BRIEF_TIGHT,
    brief,
)
from src.quant.quant_select_service import QuantSelectService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["quant-select"])


def _service(request: Request) -> QuantSelectService:
    service = getattr(request.app.state.runtime, "quant_select", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="量化选股模块未装配（quant_select service unavailable）")
    return service


# ================================================================
# 请求模型
# ================================================================


class RunRequest(BaseModel):
    sector_filter: list[str] = Field(
        default_factory=list,
        description="限定在这些自定义板块里选股（空=全市场）")
    top_n: int | None = Field(default=None, ge=1, le=200, description="最多选几只")
    trade_date: str | None = Field(
        default=None, description="指定交易日（YYYYMMDD）；空=数据里最新一天")
    max_stocks: int | None = Field(
        default=None, ge=1, le=8000, description="候选池上限（调试用）")


class SectorMember(BaseModel):
    code: str
    name: str = ""


class SectorRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    kind: str = Field(default="manual", description="manual=手工成分 / dynamic=规则")
    note: str = ""
    rule: dict[str, Any] = Field(
        default_factory=dict,
        description="dynamic 板块条件：min_circ_mv/max_circ_mv/industries/exclude_st")
    color: str = ""
    sort_order: int = 0
    members: list[SectorMember] = Field(default_factory=list)


class MembersRequest(BaseModel):
    members: list[SectorMember] = Field(default_factory=list)


class AddToWatchlistRequest(BaseModel):
    code: str
    name: str = ""


class BatchAddToWatchlistRequest(BaseModel):
    """「一键全部加自选」的入参。

    `max_length=500` 是**刻意**的：这是人在前端点一下触发的动作，
    正常一轮选股几十只；给到几千就等于开了一个"批量改配置文件"的口子。
    """

    items: list[SectorMember] = Field(default_factory=list, max_length=500)


# ================================================================
# 状态 / 运行 / 结果
# ================================================================


def _with_freshness(payload: dict) -> dict:
    """给选股结果补上「数据新鲜度」字段（**读时计算，不落库**）。

    为什么读时算而不是写时存：`quant_select_runs` 表不需要加列，
    而且**老记录也能被正确重判** —— 2026-09-15 那条记录今天读出来就是"滞后 2 天"，
    存成字段的话它永远是当时的结论（当时确实不算滞后）就看不出来了。
    """
    from src.quant.freshness import freshness

    payload.update(freshness(str(payload.get("trade_date") or "")))
    return payload


@router.get("/quant/select/status")
async def select_status(request: Request) -> dict:
    return (await _service(request).status()).to_dict()


@router.post("/quant/select/train")
async def select_train(request: Request) -> dict:
    """手动补训模型（前端「立即训练模型」）。

    模型缺失时 `/status` 会**自动**起一次训练；这个接口是给"自动那次失败/等不及"
    留的重试入口：只跳过失败冷却，仍然不会并发起第二个训练进程。
    """
    service = _service(request)
    message = await service.start_training_if_missing(force=True)
    return {
        "started": bool(message),
        "training": service.training(),
        "message": message or "模型已可用，无需训练",
    }


@router.post("/quant/select/run")
async def select_run(request: Request, body: RunRequest) -> dict:
    """手动跑一轮选股（同步等待结果；一轮几十秒到几分钟）。

    前端用 `status.running` 判断是否已有轮次在跑：本接口内部有锁，
    重复点击只会排队，不会真的并跑两轮。
    """
    service = _service(request)
    try:
        run = await service.run(
            window="manual", sector_filter=body.sector_filter,
            top_n=body.top_n, trade_date=body.trade_date,
            triggered_by="manual", max_stocks=body.max_stocks)
    except Exception as exc:  # noqa: BLE001 选股失败要给出可读原因
        logger.warning("手动量化选股失败：%s", brief(exc, BRIEF_DEFAULT))
        raise HTTPException(status_code=500,
                            detail=f"选股失败：{brief(exc, BRIEF_LOG)}") from exc
    return run.to_dict()


@router.get("/quant/select/news")
async def select_news(
    codes: str = Query(default="", description="逗号分隔的 6 位代码，最多 40 个"),
    limit: int = Query(default=3, ge=1, le=10, description="每只票最多几条"),
) -> dict:
    """选股结果用的**个股消息面**（新闻/公告），供结果表"个股消息"列展示。

    数据源：东方财富搜索接口直连（为什么不用 akshare 封装见 `src/quant/stock_news.py`）。
    **这是增强信息**：取不到就返回空列表 + 原因，绝不因此让选股结果接口失败 ——
    界面上显示"暂无"比显示一条假新闻好。
    """
    wanted = [part.strip() for part in str(codes or "").split(",") if part.strip()]
    # 只收 6 位数字代码：这是"拿选股结果来问消息"的接口，不该被当成任意搜索入口
    valid = [code for code in wanted if code.isdigit() and len(code) == 6][:40]
    if not valid:
        return {"news": {}, "count": 0, "reason": "没有有效的 6 位代码"}
    try:
        from src.quant.stock_news import fetch_many

        news = await fetch_many(valid, limit=limit)
    except Exception as exc:  # noqa: BLE001 消息面失败不影响选股结果本身
        logger.warning("个股消息面批量取数失败：%s", brief(exc, BRIEF_DEFAULT))
        return {"news": {}, "count": 0, "reason": f"消息面不可用：{brief(exc, BRIEF_TIGHT)}"}
    return {"news": news, "count": len(news), "reason": ""}


@router.get("/quant/select/runs")
async def select_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    window: str = Query(default=""),
) -> dict:
    service = _service(request)
    runs = await service.recent_runs(limit=limit, window=window)
    return {"runs": [_with_freshness(run.to_dict()) for run in runs],
            "count": len(runs)}


@router.get("/quant/select/latest")
async def select_latest(
    request: Request, window: str = Query(default=""),
) -> dict:
    service = _service(request)
    run = await service.latest_run(window=window)
    if run is None:
        return {"available": False, "reason": "还没有选股记录（等定时任务或手动跑一轮）"}
    payload = _with_freshness(run.to_dict())
    payload["available"] = True
    return payload


@router.post("/quant/select/add-to-watchlist")
async def add_to_watchlist(request: Request, body: AddToWatchlistRequest) -> dict:
    """把选出的票加进做T自选池（**人工确认**才写，选股结果本身不自动写）。

    写自选走做T模块既有入口（原子替换 + 注释保留 + 校验），并顺手把该代码在
    所有历史选股结果里标成 `added`，前端据此显示"已在自选"。
    """
    service = _service(request)
    runtime = request.app.state.runtime
    intraday = getattr(runtime, "intraday", None)
    if intraday is None:
        raise HTTPException(status_code=503, detail="做T辅助模块未装配")
    code = str(body.code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="code 不能为空")
    try:
        await asyncio.to_thread(intraday.add_watch, code, name=body.name or "")
    except Exception as exc:  # noqa: BLE001 交给前端显示原因
        raise HTTPException(status_code=400,
                            detail=f"加入自选失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    marked = await service.mark_added(code, added=True)
    return {"code": code, "added": True, "marked_runs": marked}


@router.post("/quant/select/add-to-watchlist-batch")
async def add_to_watchlist_batch(request: Request,
                                 body: BatchAddToWatchlistRequest) -> dict:
    """把一轮选股结果**整批**加进做T自选池（前端「＋ 全部加自选」）。

    与单只版本的区别只在**落盘次数**：这里一次写完、一次重建做T子组件。
    逐只循环调 `add_watch` 会让 20 次写入各自作废一次自选概览缓存，
    随后的整表重算是这段时间里最贵的一步。

    **不谎报成功**：`added`/`repaired`/`existing`/`failed` 分开返回，
    代码格式错的进 `failed`，名称解析不出来的进 `missing_name` ——
    前端据此给出"20 只里加了 18 只、2 只代码有问题"这种可核对的回执。
    `existing` 是**本来就在自选池里、本次没动**的（避免覆盖用户手配的板块/海外映射）。
    """
    service = _service(request)
    runtime = request.app.state.runtime
    intraday = getattr(runtime, "intraday", None)
    if intraday is None:
        raise HTTPException(status_code=503, detail="做T辅助模块未装配")
    items = [{"code": member.code, "name": member.name} for member in body.items]
    if not items:
        raise HTTPException(status_code=422, detail="items 不能为空")
    try:
        result = await asyncio.to_thread(intraday.add_watch_many, items)
    except Exception as exc:  # noqa: BLE001 交给前端显示原因
        raise HTTPException(status_code=400,
                            detail=f"批量加入自选失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    touched = list(result.get("added") or []) + list(result.get("repaired") or [])
    marked = await service.mark_added_many(touched) if touched else 0
    return {**result, "marked_runs": marked}


# ================================================================
# 自定义板块
# ================================================================


@router.get("/quant/sectors")
async def list_sectors(
    request: Request, with_members: bool = Query(default=True),
) -> dict:
    sectors = await _service(request).list_sectors(with_members=with_members)
    return {"sectors": [sector.to_dict() for sector in sectors],
            "count": len(sectors)}


@router.post("/quant/sectors")
async def save_sector(request: Request, body: SectorRequest) -> dict:
    service = _service(request)
    try:
        sector = await service.create_sector(
            name=body.name, kind=body.kind, note=body.note, rule=body.rule,
            color=body.color, sort_order=body.sort_order,
            members=[member.model_dump() for member in body.members])
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=brief(exc, BRIEF_DEFAULT)) from exc
    return sector.to_dict()


@router.delete("/quant/sectors/{sector_id}")
async def delete_sector(request: Request, sector_id: int) -> dict:
    ok = await _service(request).delete_sector(sector_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"板块不存在：id={sector_id}")
    return {"deleted": True, "sector_id": sector_id}


@router.put("/quant/sectors/{sector_id}/members")
async def replace_members(request: Request, sector_id: int,
                          body: MembersRequest) -> dict:
    service = _service(request)
    try:
        members = await service.set_sector_members(
            sector_id, [member.model_dump() for member in body.members])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=brief(exc, BRIEF_DEFAULT)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=brief(exc, BRIEF_DEFAULT)) from exc
    return {"sector_id": sector_id, "members": members, "count": len(members)}


@router.post("/quant/sectors/{sector_id}/members")
async def add_members(request: Request, sector_id: int,
                      body: MembersRequest) -> dict:
    """增量加成分（幂等：已在板块里的不算新增）。返回**实际新增**条数。"""
    service = _service(request)
    try:
        added = await service.add_sector_members(
            sector_id, [member.model_dump() for member in body.members])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=brief(exc, BRIEF_DEFAULT)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=brief(exc, BRIEF_DEFAULT)) from exc
    return {"sector_id": sector_id, "added": added}


@router.delete("/quant/sectors/{sector_id}/members/{code}")
async def remove_member(request: Request, sector_id: int, code: str) -> dict:
    ok = await _service(request).remove_sector_member(sector_id, code)
    if not ok:
        raise HTTPException(
            status_code=404, detail=f"板块 {sector_id} 里没有 {code}")
    return {"removed": True, "sector_id": sector_id, "code": code}
