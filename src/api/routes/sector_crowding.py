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
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.sector_crowding import db, metrics, refresh
from src.sector_crowding.config import load_config, remove_crowding_exclusions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sector_crowding", tags=["sector-crowding"])


async def _warm_metrics(codes: list[str]) -> dict:
    """用户新增/恢复板块后，**立刻临时算**这些板块的周频 4 列。

    ## 用户 2026-09-27 定的产品规则

    > 前端已配置数据要提前算，用户打开前端直接就加载算好的数据，而不是等结果。
    > 对于非前端配置数据，在用户**新增**概念板块到前端时，**再临时算**。

    所以范围切成两段：
    * **预计算** = 清单里已配置且可见的（`visible = 1`）→ 打开页面就有数
    * **按需**   = 用户新增的那个 → 就是这里

    ## 两个实现约束

    1. **必须放线程池**：`compute_metrics_for_codes` 是同步阻塞的（要读 SQLite +
       仓库库），直接在 async 处理函数里跑会堵住事件循环，连带拖慢所有请求。
    2. **失败不能反过来让"新增"失败**：补算是一次便利性优化，
       它挂了只该记日志 —— 否则用户加板块会因为一次指标计算失败而加不进去。
    """
    if not codes:
        return {"computed": 0, "failed": []}
    try:
        return await asyncio.to_thread(metrics.compute_metrics_for_codes, codes)
    except Exception as exc:  # noqa: BLE001 补算失败不该拖垮新增
        logger.warning("新增板块后临时算失败：%s", brief(exc, BRIEF_DEFAULT))
        return {"computed": 0, "failed": list(codes)}


def _conn():
    """短连接（读接口用完即关；WAL 下与刷新写互不阻塞）。"""
    return db.get_db_connection(load_config())


#: DDL 只在进程内跑一次（★ 2026-09-27 性能修复）。
#: 原先每个请求都 `executescript(_SCHEMA)` + `PRAGMA table_info` + `commit()`：
#: 幂等但每请求白付 2~5ms，且 executescript 的隐式事务与后台刷新线程的
#: 写事务存在锁交互窗口（读路径不该碰写锁 —— 事件告警 500 故障的同款教训）。
#: 后台刷新线程/定时任务自己也会调 `db.init_tables()`，双保险仍在。
_TABLES_READY = threading.Event()


def _ensure_tables() -> None:
    if _TABLES_READY.is_set():
        return
    # ★ `CHG-0143`：这里只建**本环境用户配置库**那一半，**不碰共享参考库**。
    #   共享库点名了写者（`writer: dev`）⇒ pilot 上只读；对只读库执行 DDL
    #   会报错/挂锁，而且会把"读实例写共享库"变成正常动作。
    #   共享库的建表由**写者实例**在刷新前调 `db.init_tables()` 完成。
    db.ensure_config_tables()
    _TABLES_READY.set()


def _run_db(fn, /, *args, **kwargs):
    """同步 SQLite 工作 → 线程池（★ 2026-09-27 性能修复，不让事件循环阻塞）。

    本路由的 handler 此前全是 `async def` 里直接跑同步 SQLite —— 单进程下
    事件循环是全局稀缺资源，一条 5ms~1.7s 的查询会冻结**全站所有请求**
    （含 /health 与其它页面的轮询）。与告警仓储修复
    （`event_sqlite_base.py` 的 `asyncio.to_thread` 范式）对齐。
    """
    return asyncio.to_thread(fn, *args, **kwargs)


# ================================================================
# 刷新
# ================================================================

class RefreshRequest(BaseModel):
    pool_only: bool = Field(
        default=True,
        description=("只刷**关注板块池**（`sector_crowding_list` 里可见的板块，"
                     "也就是主线挖掘复用的那份池子）。默认 True —— "
                     "2026-09-24 用户口径：一键刷新不该扫全市场 1850 个板块。"
                     "False = 全量（含行业/地区，约 1850 个），想看行业拥挤度时用"))
    concepts_only: bool = Field(
        default=False,
        description="在池子之上再过滤：只保留概念板块（默认 False=池内全部）")
    max_sectors: int = Field(
        default=0, ge=0, le=5000,
        description="限制刷新板块数（0=全部；调试用）")


@router.post("/refresh_all")
async def refresh_all(request: Request, body: RefreshRequest | None = None) -> dict:
    """一键刷新拥挤度（立即返回 task_id，后台执行）。

    默认只刷**关注板块池**：`sector_crowding_list` 里可见的板块（当前约 554 个），
    与主线挖掘的板块池同源。需要行业/地区口径时显式传 `pool_only=false`。
    """
    _ensure_tables()
    _invalidate_max_cache()
    payload = body or RefreshRequest()
    outcome = refresh.start_refresh_all(
        concepts_only=payload.concepts_only, max_sectors=payload.max_sectors,
        pool_only=payload.pool_only)
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
    _invalidate_max_cache()
    outcome = await asyncio.to_thread(refresh.recompute_stored_water_levels)
    return {"ok": True, **outcome}


# ================================================================
# 查询
# ================================================================

#: "近 6 年最高平滑拥挤度"按板块一次 GROUP BY 要扫 217 万行（实测 1.7s），
#: 不能每个请求都算。清单/告警面板整表渲染要用它，所以在进程内缓存；
#: 数据只可能被刷新/重算改动，那两处会主动清掉它（见 `_invalidate_max_cache`）。
#:
#: ★★★ 2026-09-27 第八轮：物化表 `sector_crowding_max_ma5` 上线后，
#: db.max_ma5_map() 已是 O(N) 主键读（< 5ms），进程内缓存的边际收益已很小；
#: 保留以应对"前端 60s 轮询"高频场景，仍按 300s TTL 收敛。
_MAX_MA5_CACHE: dict[str, Any] = {"at": 0.0, "map": {}}
_MAX_MA5_TTL = 300.0


def _invalidate_max_cache() -> None:
    _MAX_MA5_CACHE["at"] = 0.0
    _MAX_MA5_CACHE["map"] = {}


def _cached_max_ma5(conn) -> dict[str, float]:
    now = time.monotonic()
    if _MAX_MA5_CACHE["map"] and now - float(_MAX_MA5_CACHE["at"]) < _MAX_MA5_TTL:
        return _MAX_MA5_CACHE["map"]  # type: ignore[return-value]
    # ★ 物化表查询 < 5ms（替代 0.4-1.7s GROUP BY）
    mapping = db.max_ma5_map(conn)
    _MAX_MA5_CACHE["at"] = now
    _MAX_MA5_CACHE["map"] = mapping
    return mapping


def _sectors_max_ma5_sync() -> dict:
    conn = _conn()
    try:
        mapping = _cached_max_ma5(conn)
        return {"count": len(mapping), "cached_at": _MAX_MA5_CACHE["at"],
                "max_ma5": mapping}
    finally:
        conn.close()


@router.get("/sectors_max_ma5")
async def sectors_max_ma5() -> dict:
    """各板块历史最高平滑拥挤度（面板"近6年最高"列）。

    单独一个接口 + 进程内缓存：这个值是**全历史聚合**，跟"最新交易日"无关，
    每次读清单都重算 1.7s 不划算。刷新/重算完成后缓存会被清掉。
    """
    _ensure_tables()
    return await _run_db(_sectors_max_ma5_sync)


@router.post("/metrics/compute")
async def metrics_compute(
    force: bool = Query(
        default=False,
        description="同一周已算过时是否强制重算（默认 False=跳过，避免每天白跑）"),
) -> dict:
    """立即重算周频异动指标（立即返回 task_id，后台线程执行）。

    正常节奏由调度器每周自动跑一次（`crowding_metrics_weekly`）；这个接口是
    给"刚刷完拥挤度、想立刻看到 4 列"用的。一轮只算**看板默认视图真正渲染**的
    板块（`MetricConfig.pool_only` + `pool_concepts_only`，即"可见 + 概念板块"，
    约 262 个；改之前是全量 1479 个）：热缓存下是本地 SQL 聚合，实测几秒；
    冷缓存要按板块抓成分股，可能 3~5 分钟。
    所以同样走"后台任务 + 进度轮询"，不要同步等。
    """
    _ensure_tables()
    outcome = metrics.start_metric_compute(force=bool(force))
    return {"ok": True, **outcome}


@router.get("/metrics/status")
async def metrics_status(task_id: str = Query(default="")) -> dict:
    """周频指标的计算进度 / 最近一次结果。"""
    _ensure_tables()
    return metrics.get_metric_progress(task_id)


@router.get("/metrics/summary")
async def metrics_summary() -> dict:
    """周频指标的元信息：最新周、各基准日、4 列各自的覆盖板块数。"""
    _ensure_tables()
    return await _run_db(metrics.metrics_summary)


def _latest_sync(*, concepts_only: bool, trade_date: str, use_list: bool) -> dict:
    conn = _conn()
    try:
        codes = db.list_visible_codes(conn, concepts_only=concepts_only) \
            if use_list else None
        rows = db.query_all_latest_water_level(
            conn, concepts_only=concepts_only, trade_date=trade_date,
            sector_codes=codes)
        return {
            "trade_date": db.latest_trade_date(conn),
            "count": len(rows),
            "threshold": load_config().window.alert_threshold,
            "high_threshold": load_config().window.high_alert_threshold,
            "sectors": rows,
        }
    finally:
        conn.close()


@router.get("/latest")
async def latest(
    concepts_only: bool = Query(default=False),
    trade_date: str = Query(default=""),
    use_list: bool = Query(
        default=False,
        description="只返回持久化清单里**可见**的板块（散点总览用；未配置过时=全部）"),
) -> dict:
    """全板块最新交易日的水位（散点总览）。"""
    _ensure_tables()
    return await _run_db(
        _latest_sync, concepts_only=concepts_only,
        trade_date=trade_date, use_list=use_list)


def _alerts_sync(*, threshold: float, concepts_only: bool, trade_date: str,
                 use_list: bool, all_: bool, cut: float,
                 high_threshold: float) -> dict:
    conn = _conn()
    try:
        codes = db.list_visible_codes(conn, concepts_only=concepts_only) \
            if use_list else None
        target = db.latest_trade_date(conn)
        if all_:
            # 全量清单：基准行来自"清单"本身（`sector_meta` 里有的板块），
            # 而不是当日 K 线 —— 否则刚加入、当天还没数据的板块会凭空消失，
            # 用户会以为"添加没生效"。
            view = db.query_list_view(conn, concepts_only=False)
            # 最新交易日的水位/成交额（按板块索引）
            snapshots = {row["sector_code"]: row for row in
                         db.query_all_latest_water_level(
                             conn, concepts_only=False, trade_date=target)}
            # ★ 2026-09-27：改走 `_cached_max_ma5`（300s 进程内缓存）——
            # 原来这里直连 `db.max_ma5_map(conn)`，物化表上线前每次请求
            # 都付 1.7s 全表 GROUP BY，是告警面板"绕过了缓存"的那条热路径。
            maxima = _cached_max_ma5(conn)
            # 本周的周频异动指标（4 列）
            weekly = db.query_metrics(conn)
            wanted = None if codes is None else set(codes)
            rows: list[dict] = []
            for item in view:
                code = str(item["sector_code"])
                if not int(item["visible"]):
                    continue
                if wanted is not None and code not in wanted:
                    continue
                if concepts_only and not int(item["is_concept"]):
                    continue
                merged = {**item, **{key: value for key, value in
                                     snapshots.get(code, {}).items()
                                     if key not in ("sector_name",)}}
                merged["max_ma5_crowding"] = maxima.get(code)
                for key in ("chg_5d", "chg_1m", "chg_2m", "flow_ratio",
                            "net_inflow", "circ_mv_base"):
                    merged[key] = (weekly.get(code) or {}).get(key)
                water = merged.get("water_level")
                merged["is_alert"] = bool(
                    water is not None and float(water) >= cut)
                rows.append(merged)
            rows.sort(key=lambda row: (
                -int(row.get("pinned") or 0),
                0 if row.get("is_alert") else 1,
                -(row["water_level"] if row.get("water_level") is not None else -1),
                str(row.get("sector_code") or "")))
            alert_count = sum(1 for row in rows if row["is_alert"])
        else:
            rows = db.query_alerts(conn, threshold=cut,
                                   concepts_only=concepts_only,
                                   trade_date=trade_date, sector_codes=codes)
            for row in rows:
                row["is_alert"] = True
            alert_count = len(rows)
        return {
            "threshold": cut,
            "high_threshold": high_threshold,
            "trade_date": target,
            "updated_at": refresh.get_refresh_progress().get("finished_at", ""),
            "count": len(rows),
            "alert_count": alert_count,
            "all": bool(all_),
            "alerts": rows,
        }
    finally:
        conn.close()


@router.get("/alerts")
async def alerts(
    threshold: float = Query(default=0.0, ge=0.0, le=1.0,
                             description="水位阈值（0=用配置默认 0.8）"),
    concepts_only: bool = Query(default=True),
    trade_date: str = Query(default=""),
    use_list: bool = Query(default=False,
                           description="按持久化清单里可见的板块过滤"),
    all: bool = Query(default=False,
                      description="返回清单**全部板块**（≥阈值标红、置顶在前），"
                                  "而不是只返回触发告警的"),
) -> dict:
    """水位 ≥ 阈值的板块列表；`all=1` 时返回清单全量并标记告警。"""
    _ensure_tables()
    config = load_config()
    cut = float(threshold) if threshold > 0 else config.window.alert_threshold
    return await _run_db(
        _alerts_sync, threshold=threshold, concepts_only=concepts_only,
        trade_date=trade_date, use_list=use_list, all_=bool(all), cut=cut,
        high_threshold=config.window.high_alert_threshold)


# ================================================================
# 看板清单（总览散点图 + 告警面板共用的持久化配置）
# ================================================================

class ListUpsertRequest(BaseModel):
    sector_code: str
    sector_name: str = ""
    pinned: bool | None = None


class ListDeleteRequest(BaseModel):
    sector_codes: list[str] = Field(default_factory=list)


def _list_view_row(row: dict, metric: dict | None = None) -> dict:
    """清单视图行 → 前端需要的最小字段集（不夹带内部结构）。

    `metric` 是本周的周频异动指标（`sector_crowding_metric`，见 metrics.py）。
    没有时 4 列一律为 **None**（前端显示"—"）—— 不填 0，否则"还没算"会被
    读成"没有变化"。
    """
    metric = metric or {}
    return {
        "sector_code": str(row.get("sector_code") or ""),
        "sector_name": str(row.get("sector_name") or ""),
        "is_concept": int(row.get("is_concept") or 0),
        "bars": int(row.get("bars") or 0),
        "visible": bool(row.get("visible", True)),
        "pinned": bool(row.get("pinned", False)),
        "source": str(row.get("source") or ""),
        "missing": bool(row.get("missing", False)),
        "in_watchlist": bool(row.get("in_watchlist", False)),
        "trade_date": row.get("trade_date") or "",
        "sector_amount": row.get("sector_amount"),
        "market_amount": row.get("market_amount"),
        "raw_crowding": row.get("raw_crowding"),
        "ma5_crowding": row.get("ma5_crowding"),
        "water_level": row.get("water_level"),
        # --- 自定义告警阈值（"告警"列）---
        "alert_mode": str(row.get("alert_mode") or ""),
        "alert_threshold": row.get("alert_threshold"),
        "alert_on": db.alert_triggered(
            str(row.get("alert_mode") or ""), row.get("alert_threshold"),
            row.get("water_level")),
        # --- 周频异动指标（4 列）---
        "chg_5d": metric.get("chg_5d"),
        "chg_1m": metric.get("chg_1m"),
        "chg_2m": metric.get("chg_2m"),
        "flow_ratio": metric.get("flow_ratio"),
        "net_inflow": metric.get("net_inflow"),
        "circ_mv_base": metric.get("circ_mv_base"),
        "metric_week": str(metric.get("compute_week") or ""),
        "base_date_5d": str(metric.get("base_date_5d") or ""),
        "base_date_1m": str(metric.get("base_date_1m") or ""),
        "base_date_2m": str(metric.get("base_date_2m") or ""),
        "flow_base_date": str(metric.get("flow_base_date") or ""),
        "flow_last_date": str(metric.get("flow_last_date") or ""),
    }


def _read_list(conn, *, concepts_only: bool = False,
               with_metrics: bool = True) -> list[dict]:
    metrics = db.query_metrics(conn) if with_metrics else {}
    return [_list_view_row(row, metrics.get(str(row.get("sector_code"))))
            for row in db.query_list_view(conn, concepts_only=concepts_only)]


def _config_list_sync(*, concepts_only: bool, config) -> dict:
    conn = _conn()
    try:
        seeded = db.seed_list(conn, concepts_only=True)
        # 顺带把**存量**的空壳板块（`bars=0`）软删一次。
        # 用 `force=False`：只标记从未标记过的行，所以跑多少次结果都一样，
        # 也不会把"用户手动恢复过"的空壳又自动藏回去 —— 自动清理只做一次，
        # 之后要不要再藏，由用户点「清理空壳板块」决定。
        #
        # ★ 2026-09-27 加 SELECT 守卫：UPDATE 即使 0 行命中也要开写事务，
        # 这条接口被前端 60s 轮询 —— 每次读都抢写锁正是"读路径藏写"反模式
        # （事件告警 500 故障同款）。先查有没有候选行，通常没有就纯读。
        hidden = (db.hide_dead_boards(conn, force=False)
                  if db.has_hideable_dead_boards(conn) else 0)
        rows = _read_list(conn, concepts_only=concepts_only)
        return {
            "items": rows,
            "count": len(rows),
            "visible_count": sum(1 for row in rows if row["visible"]),
            "pinned_count": sum(1 for row in rows if row["pinned"]),
            "hidden_count": sum(1 for row in rows if not row["visible"]),
            "dead_hidden_count": db.count_hidden_dead(conn),
            "seeded": seeded,
            "hidden_dead": hidden,
            "trade_date": db.latest_trade_date(conn),
            "threshold": config.window.alert_threshold,
            "high_threshold": config.window.high_alert_threshold,
            # 水位需要的最少日线根数。前端用它把"水位为空"显示成
            # 「数据不足（563/750）」而不是一个光秃秃的"—" ——
            # 空值看起来像故障，实际是**有意的样本量门槛**（见
            # `sector_crowding/config.yaml` 里 `min_bars_for_water_level`
            # 的说明：分母是"近 6 年最高值"，历史不足 3 年时板块的
            # "历史最高"就是最近几天，水位恒为 100%、一上线就误告警）。
            "min_bars_for_water_level": int(
                config.window.min_bars_for_water_level),
        }
    finally:
        conn.close()


@router.get("/config_list")
async def config_list(
    concepts_only: bool = Query(
        default=True,
        description="只返回概念板块（前端「只看概念板块」开关的默认口径）"),
) -> dict:
    """看板清单（可见/隐藏/置顶 + 最新水位）。

    "该看哪些板块"的**唯一真相来源**：散点总览与告警面板都读它。
    返回全量（含隐藏项），前端需要时自行过滤，便于做"已隐藏"回显。

    首次调用会把当前默认可见的板块**种子化落库**（幂等）：落库之后
    "用户删掉全部板块"（全 visible=0）与"从没配置过"才区分得开 ——
    否则用户清空清单后界面又会长回全量。
    """
    _ensure_tables()
    config = load_config()
    return await _run_db(_config_list_sync, concepts_only=concepts_only,
                         config=config)


class AlertUpsertRequest(BaseModel):
    sector_code: str
    #: above = 水位高于阈值告警；below = 低于阈值告警
    mode: str = Field(default="above", description="above | below")
    threshold: float = Field(ge=0.0, le=1.0,
                             description="告警水位阈值，取值 [0, 1]")
    note: str = ""


@router.post("/alert/clear_all")
async def alert_clear_all(
    concepts_only: bool = Query(default=True),
) -> dict:
    """清除全部自定义告警阈值。

    **必须注册在 `/alert/{sector_code}` 之前** —— 否则 `clear_all` 会被当成
    sector_code 匹配掉（FastAPI 按注册顺序匹配）。
    """
    _ensure_tables()
    conn = _conn()
    try:
        cleared = 0
        for code in list(db.query_alerts_config(conn)):
            db.delete_alert(conn, code)
            cleared += 1
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, "cleared": cleared, "items": rows,
                "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"])}
    finally:
        conn.close()


@router.post("/alert/{sector_code}")
async def alert_set(
    sector_code: str,
    body: AlertUpsertRequest,
    concepts_only: bool = Query(default=True),
) -> dict:
    """设置/更新某板块的自定义告警阈值。

    阈值范围 `[0, 1]`（水位本身就是 0~1 的比例量），前端支持 3 位小数。
    方向二选一：`above`（水位涨到阈值以上告警，看拥挤风险）/
    `below`（跌到阈值以下告警，看冷清下来的机会）。
    """
    _ensure_tables()
    conn = _conn()
    try:
        try:
            outcome = db.upsert_alert(
                conn, sector_code, mode=body.mode,
                threshold=body.threshold, note=body.note)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=brief(exc, BRIEF_DEFAULT)) from exc
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, **outcome, "items": rows, "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"])}
    finally:
        conn.close()


@router.delete("/alert/{sector_code}")
async def alert_clear(
    sector_code: str,
    concepts_only: bool = Query(default=True),
) -> dict:
    """清除某板块的自定义告警阈值（回到"不设告警"）。"""
    _ensure_tables()
    conn = _conn()
    try:
        removed = db.delete_alert(conn, sector_code)
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, "removed": removed, "items": rows,
                "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"])}
    finally:
        conn.close()


@router.get("/config_list/hidden")
async def config_list_hidden(
    keyword: str = Query(default="", description="按名称/代码过滤"),
    limit: int = Query(default=0, ge=0, le=2000, description="0=全部"),
) -> dict:
    """已隐藏的板块（系统隐藏的空壳 + 用户手动删除），供恢复列表用。

    单独一个接口而不是塞进 `/config_list`：后者是每 60 秒轮询的，
    把几百个不可见行一起带上会让每次轮询都白传一大截数据。

    `bars=0` 的是**空壳板块**（从未刷到过数据，同花顺 865xxx 段居多）；
    `bars>0` 的是用户自己删掉的。两者都列出来，恢复口径一致。
    """
    _ensure_tables()
    conn = _conn()
    try:
        rows = db.query_hidden_boards(conn, keyword=keyword, limit=limit)
        dead = sum(1 for row in rows if int(row.get("bars") or 0) == 0)
        return {
            "items": rows,
            "count": len(rows),
            "dead_count": dead,
            "revivable_count": len(rows) - dead,
        }
    finally:
        conn.close()


@router.post("/config_list/hidden/cleanup")
async def config_list_hidden_cleanup() -> dict:
    """把当前**可见**的空壳板块（`bars=0`）重新隐藏一次。

    启动时会自动跑一次（只标记从未标记过的行）；这个接口是 `force=True` 版本，
    用于"我手动恢复了一批空壳，想再藏回去"。
    """
    _ensure_tables()
    conn = _conn()
    try:
        hidden = db.hide_dead_boards(conn, force=True)
        rows = _read_list(conn)
        return {"ok": True, "hidden": hidden, "items": rows,
                "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"]),
                "pinned_count": sum(1 for row in rows if row["pinned"])}
    finally:
        conn.close()


@router.post("/config_list/restore")
async def config_list_restore(
    body: ListDeleteRequest,
    concepts_only: bool = Query(default=True),
) -> dict:
    """恢复被隐藏/删除的板块（批量）。与 `batch_delete` 正好相反。

    ⚠️ 必须同时把它们从**剔除清单**里摘出去（2026-09-27 修）：清单一旦收入
    某个板块，`db.query_list_view()` 就会把它过滤掉 —— 只写 `visible = 1`
    的话接口回 `{"restored": N}` 而板块**不出现**，静默失败。
    用户的显式恢复 = 撤销当初的剔除决定。
    """
    _ensure_tables()
    if not body.sector_codes:
        raise HTTPException(status_code=422, detail="sector_codes 不能为空")
    conn = _conn()
    try:
        restored = 0
        touched: list[str] = []
        for code in body.sector_codes:
            text = str(code or "").strip()
            if not text:
                continue
            db.upsert_list_item(conn, text, visible=True, source=db.SOURCE_MANUAL)
            touched.append(text)
            restored += 1
        if touched:
            remove_crowding_exclusions(touched)
        # 同 `config_list_add`：**先补算再读列表**，否则响应里那一行还是「—」
        fresh = await _warm_metrics(touched)
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, "restored": restored, "items": rows,
                "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"]),
                "pinned_count": sum(1 for row in rows if row["pinned"]),
                "metrics": fresh}
    finally:
        conn.close()


@router.post("/config_list")
async def config_list_add(
    body: ListUpsertRequest,
    concepts_only: bool = Query(default=True),
) -> dict:
    """新增/恢复一个板块到清单（幂等）。

    ⚠️ 同样要摘掉剔除清单（理由见 `config_list_restore`）：用户主动加一个板块，
    不该被一份历史剔除清单静默挡住。
    """
    _ensure_tables()
    conn = _conn()
    try:
        try:
            outcome = db.upsert_list_item(
                conn, body.sector_code, sector_name=body.sector_name,
                visible=True, pinned=body.pinned,
                source=db.SOURCE_MANUAL)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=brief(exc, BRIEF_DEFAULT)) from exc
        remove_crowding_exclusions([body.sector_code])
        # ⚠️ 顺序很重要：**先补算，再读列表**。
        # 反过来的话，`_read_list` 拿到的还是旧快照 → 响应里这一行仍是「—」，
        # 用户得等下一次 60 秒轮询才看到数 —— 而补算只要几百毫秒，
        # 那 60 秒全是白等（这是第一版写反的地方）。
        fresh = await _warm_metrics([body.sector_code])
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, **outcome, "items": rows, "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"]),
                "pinned_count": sum(1 for row in rows if row["pinned"]),
                "metrics": fresh}
    finally:
        conn.close()


@router.delete("/config_list/{sector_code}")
async def config_list_remove(
    sector_code: str,
    concepts_only: bool = Query(default=True),
) -> dict:
    """从清单移除（软删：`visible=0`，不删历史拥挤度数据）。"""
    _ensure_tables()
    conn = _conn()
    try:
        outcome = db.upsert_list_item(conn, sector_code, visible=False)
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, **outcome, "items": rows, "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"]),
                "pinned_count": sum(1 for row in rows if row["pinned"])}
    finally:
        conn.close()


@router.post("/config_list/batch_delete")
async def config_list_batch_delete(
    body: ListDeleteRequest,
    concepts_only: bool = Query(default=True),
) -> dict:
    """批量从清单移除（散点图框选/多选后一次删除）。"""
    _ensure_tables()
    if not body.sector_codes:
        raise HTTPException(status_code=422, detail="sector_codes 不能为空")
    conn = _conn()
    try:
        removed = 0
        for code in body.sector_codes:
            text = str(code or "").strip()
            if not text:
                continue
            db.upsert_list_item(conn, text, visible=False)
            removed += 1
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, "removed": removed, "items": rows,
                "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"]),
                "pinned_count": sum(1 for row in rows if row["pinned"])}
    finally:
        conn.close()


@router.post("/config_list/{sector_code}/pin")
async def config_list_pin(
    sector_code: str,
    pinned: bool = Query(default=True),
    concepts_only: bool = Query(default=True),
) -> dict:
    """置顶/取消置顶（置顶板块在两个面板里都排最前）。"""
    _ensure_tables()
    conn = _conn()
    try:
        outcome = db.upsert_list_item(conn, sector_code, pinned=bool(pinned))
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, **outcome, "items": rows, "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"]),
                "pinned_count": sum(1 for row in rows if row["pinned"])}
    finally:
        conn.close()


@router.post("/config_list/reset")
async def config_list_reset(
    concepts_only: bool = Query(default=True),
) -> dict:
    """清空清单配置 → 回到"默认显示全部板块"（用户的删除与置顶一起清掉）。"""
    _ensure_tables()
    conn = _conn()
    try:
        cleared = db.reset_list(conn)
        rows = _read_list(conn, concepts_only=concepts_only)
        return {"ok": True, "cleared": cleared, "items": rows,
                "count": len(rows),
                "visible_count": sum(1 for row in rows if row["visible"]),
                "pinned_count": sum(1 for row in rows if row["pinned"])}
    finally:
        conn.close()


def _sectors_sync(keyword: str, limit: int) -> dict:
    conn = _conn()
    try:
        return {"sectors": db.search_sectors(conn, keyword, limit=limit)}
    finally:
        conn.close()


@router.get("/sectors")
async def sectors(keyword: str = Query(default=""),
                  limit: int = Query(default=20, ge=1, le=200)) -> dict:
    """板块搜索（前端"输入板块名称查询"）。"""
    _ensure_tables()
    return await _run_db(_sectors_sync, keyword, limit)


def _members_sync(sector_code: str) -> dict:
    conn = _conn()
    try:
        return {"sector_code": sector_code,
                "members": db.query_sector_members(conn, sector_code)}
    finally:
        conn.close()


@router.get("/members/{sector_code}")
async def members(sector_code: str) -> dict:
    """板块成分股（参考数据，不参与拥挤度计算）。"""
    _ensure_tables()
    return await _run_db(_members_sync, sector_code)


def _watchlist_sync() -> dict:
    conn = _conn()
    try:
        rows = db.list_watchlist(conn)
        return {"items": rows, "count": len(rows),
                "threshold": load_config().window.alert_threshold}
    finally:
        conn.close()


@router.get("/watchlist")
async def watchlist() -> dict:
    _ensure_tables()
    return await _run_db(_watchlist_sync)


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


def _sector_detail_sync(sector_code: str, limit: int) -> dict:
    """单板块历史曲线（**展示路径** → `slim=True`）。

    ★ 2026-09-30 `CHG-0137`：这条是**展示**接口，必须走瘦身投影。
    原先 `SELECT *` 把 `id`/`created_at`/`updated_at` 一起发出去（28.9% 的明文
    白传），浮点又按完整 double 序列化（实测 2348 个值带 18~20 位小数 ⇒ gzip
    压不动）—— 明文 424 KB / gzip 66 KB，在 ~4.6 KB/s 的劣化隧道上要 **14 秒**，
    而**后端只花 13 毫秒**。加 `slim=True` 后 gzip 66,507 B → 33,491 B（砍半）。
    口径与实测见 `docs/PRD.md` §22。

    ⚠️ 元数据**不许**用 `db.query_sector_meta()` 全表再线性查找 —— 那是
    2517 行的整表读出只为了取一行。这里改传 `sector_code=` 走主键直查。
    """
    conn = _conn()
    try:
        rows = db.query_sector_crowding(conn, sector_code, slim=True)
        found = db.query_sector_meta(conn, sector_code=sector_code)
        meta = found[0] if found else None
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


@router.get("/{sector_code}")
async def sector_detail(sector_code: str,
                        limit: int = Query(default=0, ge=0, le=5000,
                                           description="只取最近 N 个交易日（0=全部）")) -> dict:
    """单板块历史曲线（含水位）。放在最后注册，避免吃掉上面的固定路径。"""
    _ensure_tables()
    return await _run_db(_sector_detail_sync, sector_code, limit)


def _root_sync() -> dict:
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


@router.get("")
async def root() -> dict:
    """模块自检：表是否建好、有多少数据、上次刷新状态。"""
    _ensure_tables()
    return await _run_db(_root_sync)
