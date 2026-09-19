"""板块概念拥挤度：REST 接口。

路由前缀 `/api/v1/sector_crowding`。任务书写的是 `/api/sector_crowding`，
但本项目**所有**路由都自带 `/api/v1` 前缀、`api_router` 本身不加前缀
（见 `src/api/routes/fundflow.py` / `quant_select.py` 的同一写法），
所以这里也必须把 `/api/v1` 写进 prefix —— 否则真机路径会少这一层，前端全 404。

刷新接口是"立即返回 task_id + 后台线程"：一轮全量刷新要几分钟，
同步等待会顶到反代/浏览器超时（项目里已踩过 300 秒超时的坑）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.sector_crowding import db, refresh
from src.sector_crowding.config import load_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sector_crowding", tags=["sector-crowding"])


def _conn():
    """短连接（读接口用完即关；WAL 下与刷新写互不阻塞）。"""
    return db.get_db_connection(load_config())


def _ensure_tables() -> None:
    db.init_tables()


# ================================================================
# 刷新
# ================================================================

class RefreshRequest(BaseModel):
    concepts_only: bool = Field(
        default=False,
        description="只刷概念板块（默认 False=全刷；数据先全量落库，告警再按概念过滤）")
    max_sectors: int = Field(
        default=0, ge=0, le=5000,
        description="限制刷新板块数（0=全部；调试用）")


@router.post("/refresh_all")
async def refresh_all(request: Request, body: RefreshRequest | None = None) -> dict:
    """一键刷新全部板块拥挤度（立即返回 task_id，后台执行）。"""
    _ensure_tables()
    payload = body or RefreshRequest()
    outcome = refresh.start_refresh_all(
        concepts_only=payload.concepts_only, max_sectors=payload.max_sectors)
    return {"ok": True, **outcome}


@router.get("/refresh_status")
async def refresh_status(task_id: str = Query(default="")) -> dict:
    """查询刷新进度（task_id 留空 → 返回最近一次任务）。"""
    return refresh.get_refresh_progress(task_id)


@router.post("/recompute")
async def recompute() -> dict:
    """**不联网**重算已入库数据的原始拥挤度 / MA5 / 水位。

    用于改了 `sector_crowding/config.yaml` 的 `ma_window` /
    `max_lookback_years` / `min_bars_for_water_level` 之后让新口径立即生效，
    而不必重新抓一遍 2517 个板块（约十分钟）。
    """
    _ensure_tables()
    outcome = await asyncio.to_thread(refresh.recompute_stored_water_levels)
    return {"ok": True, **outcome}


# ================================================================
# 查询
# ================================================================

@router.get("/latest")
async def latest(
    concepts_only: bool = Query(default=False),
    trade_date: str = Query(default=""),
) -> dict:
    """全板块最新交易日的水位（散点总览）。"""
    _ensure_tables()
    conn = _conn()
    try:
        rows = db.query_all_latest_water_level(
            conn, concepts_only=concepts_only, trade_date=trade_date)
        return {
            "trade_date": db.latest_trade_date(conn),
            "count": len(rows),
            "threshold": load_config().window.alert_threshold,
            "high_threshold": load_config().window.high_alert_threshold,
            "sectors": rows,
        }
    finally:
        conn.close()


@router.get("/alerts")
async def alerts(
    threshold: float = Query(default=0.0, ge=0.0, le=1.0,
                             description="水位阈值（0=用配置默认 0.8）"),
    concepts_only: bool = Query(default=True),
    trade_date: str = Query(default=""),
) -> dict:
    """水位 ≥ 阈值的板块列表（按水位降序）。"""
    _ensure_tables()
    config = load_config()
    cut = float(threshold) if threshold > 0 else config.window.alert_threshold
    conn = _conn()
    try:
        rows = db.query_alerts(conn, threshold=cut, concepts_only=concepts_only,
                              trade_date=trade_date)
        return {
            "threshold": cut,
            "high_threshold": config.window.high_alert_threshold,
            "trade_date": db.latest_trade_date(conn),
            "updated_at": refresh.get_refresh_progress().get("finished_at", ""),
            "count": len(rows),
            "alerts": rows,
        }
    finally:
        conn.close()


@router.get("/sectors")
async def sectors(keyword: str = Query(default=""),
                  limit: int = Query(default=20, ge=1, le=200)) -> dict:
    """板块搜索（前端"输入板块名称查询"）。"""
    _ensure_tables()
    conn = _conn()
    try:
        return {"sectors": db.search_sectors(conn, keyword, limit=limit)}
    finally:
        conn.close()


@router.get("/members/{sector_code}")
async def members(sector_code: str) -> dict:
    """板块成分股（参考数据，不参与拥挤度计算）。"""
    _ensure_tables()
    conn = _conn()
    try:
        return {"sector_code": sector_code,
                "members": db.query_sector_members(conn, sector_code)}
    finally:
        conn.close()


@router.get("/watchlist")
async def watchlist() -> dict:
    _ensure_tables()
    conn = _conn()
    try:
        rows = db.list_watchlist(conn)
        return {"items": rows, "count": len(rows),
                "threshold": load_config().window.alert_threshold}
    finally:
        conn.close()


class WatchRequest(BaseModel):
    sector_code: str
    sector_name: str = ""
    note: str = ""


@router.post("/watchlist")
async def watchlist_add(body: WatchRequest) -> dict:
    _ensure_tables()
    conn = _conn()
    try:
        created = db.add_to_watchlist(
            conn, body.sector_code, sector_name=body.sector_name, note=body.note)
        rows = db.list_watchlist(conn)
        return {"ok": True, "created": created, "items": rows, "count": len(rows)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=brief(exc, BRIEF_DEFAULT)) from exc
    finally:
        conn.close()


@router.delete("/watchlist/{sector_code}")
async def watchlist_remove(sector_code: str) -> dict:
    _ensure_tables()
    conn = _conn()
    try:
        removed = db.remove_from_watchlist(conn, sector_code)
        if not removed:
            raise HTTPException(
                status_code=404, detail=f"自选池里没有 {sector_code}")
        rows = db.list_watchlist(conn)
        return {"ok": True, "removed": True, "items": rows, "count": len(rows)}
    finally:
        conn.close()


@router.get("/{sector_code}")
async def sector_detail(sector_code: str,
                        limit: int = Query(default=0, ge=0, le=5000,
                                           description="只取最近 N 个交易日（0=全部）")) -> dict:
    """单板块历史曲线（含水位）。放在最后注册，避免吃掉上面的固定路径。"""
    _ensure_tables()
    conn = _conn()
    try:
        rows = db.query_sector_crowding(conn, sector_code)
        meta = next((item for item in db.query_sector_meta(conn)
                     if item["sector_code"] == sector_code), None)
        if limit > 0:
            rows = rows[-limit:]
        values = [item["water_level"] for item in rows
                  if item.get("water_level") is not None]
        return {
            "sector_code": sector_code,
            "sector_name": (meta or {}).get("sector_name", ""),
            "is_concept": bool((meta or {}).get("is_concept", 1)),
            "bars": len(rows),
            "first_trade_date": rows[0]["trade_date"] if rows else "",
            "last_trade_date": rows[-1]["trade_date"] if rows else "",
            "water_level": values[-1] if values else None,
            "max_water_level": max(values) if values else None,
            "threshold": load_config().window.alert_threshold,
            "series": rows,
        }
    finally:
        conn.close()


@router.get("")
async def root() -> dict:
    """模块自检：表是否建好、有多少数据、上次刷新状态。"""
    _ensure_tables()
    conn = _conn()
    try:
        config = load_config()
        return {
            "module": "sector_crowding",
            "db_path": str(config.db_path),
            "tables": {
                "daily": db.count_rows(conn),
                "meta": len(db.query_sector_meta(conn)),
                "watchlist": len(db.list_watchlist(conn)),
            },
            "latest_trade_date": db.latest_trade_date(conn),
            "window": config.window.__dict__,
            "refresh": refresh.get_refresh_progress(),
        }
    finally:
        conn.close()
