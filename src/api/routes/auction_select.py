"""集合竞价选股：REST 接口。

前缀 `/api/v1/auction_select`（与项目其它模块一致：`/api/v1` 写进 prefix，
`api_router` 本身不加前缀 —— 见 `src/api/routes/fundflow.py` 同一写法）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.auction_select import db, scheduler, service
from src.auction_select.config import load_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auction_select", tags=["auction-select"])


def _conn():
    """短连接（读接口用完即关；WAL 下与写互不阻塞）。"""
    config = load_config()
    conn = db.get_db_connection(config=config)
    db.init_tables(conn)
    return conn


def _loads(text: Any, fallback: Any) -> Any:
    """宽松 JSON 解析（库里存的是 TEXT）。坏数据不抛，返回 fallback。"""
    if text is None or text == "":
        return fallback
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


def _pick_to_api(row: dict[str, Any]) -> dict[str, Any]:
    """库行 → 接口结构（把 JSON 文本列解开）。"""
    item = dict(row)
    item["dim_scores"] = _loads(row.get("dim_scores"), {})
    item["dim_notes"] = _loads(row.get("dim_notes"), {})
    item["veto_reasons"] = _loads(row.get("veto_reasons"), [])
    item["features"] = _loads(row.get("features"), {})
    item["vetoed"] = bool(row.get("vetoed"))
    return item


#: `auction_feature` 表里存的是 JSON 文本的列。新增这类列时**必须**加进来。
_FEATURE_JSON_COLS: dict[str, Any] = {
    "theme_names": [],      # list[str]
    "theme_all": [],        # list[str]
    "degraded": [],         # list[str]
    "rush_labels": [],      # list[str]
    "pattern_meta": {},     # dict
    "mode_fits": [],        # list[dict]
}

#: `auction_feature` 表里存 0/1 的布尔列（SQLite 没有真 bool）。
_FEATURE_BOOL_COLS = ("is_one_word_board", "is_weak_to_strong", "is_strong_to_weak")


def _feature_to_api(row: dict[str, Any]) -> dict[str, Any]:
    """`auction_feature` 行 → 接口结构。

    ⚠️ **必须把表里所有 JSON 文本列都解开。** 踩过的坑（2026-09-21 用户报障
    "点详情整页崩掉"）：`/today` 的 `features` 走 `_pick_to_api` 解开了 JSON，
    而 `/detail` 的 `feature` 直接返回这张表的原始行 —— 于是**同一个字段在两个
    接口里类型不同**：`rush_labels` 在 `/today` 是 `[]`、在 `/detail` 是字符串
    `"[]"`。前端对后者调 `.join()` 抛
    `TypeError: feature.rush_labels.join is not a function`，
    而 React 没有错误边界 → **整棵树卸载 = 白屏**。

    所以这里统一解开，并顺手把 0/1 列转成真 bool（与 `/today` 对齐）。
    """
    item = dict(row)
    for col, fallback in _FEATURE_JSON_COLS.items():
        if col in row:
            item[col] = _loads(row.get(col), fallback)
    for col in _FEATURE_BOOL_COLS:
        if col in row:
            item[col] = bool(row.get(col))
    return item


class ManualRunRequest(BaseModel):
    """手动触发（调试/补跑用）。"""

    trade_date: str = Field(default="", description="交易日 YYYYMMDD，留空=今天")
    force: bool = Field(default=False, description="强制重跑（忽略当日已完成）")


class WatchAddRequest(BaseModel):
    """加一只到手动关注列表（用户口径 2026-09-22）。"""

    code: str = Field(default="", description="6 位股票代码，如 600325")
    name: str = Field(default="", description="名称（留空则由后端补全）")
    note: str = Field(default="", description="备注（只有用户自己看）")


class AddWatchRequest(BaseModel):
    code: str
    name: str = ""
    note: str = ""


@router.get("/sentiment_cycle")
async def sentiment_cycle(
    days: int = Query(default=30, ge=5, le=120,
                      description="回看多少个交易日"),
    refresh: bool = Query(default=False, description="true=绕过进程内缓存重算"),
) -> dict[str, Any]:
    """**情绪周期五线**（大肉 / 大面 / 连板数 / 小周期 / 大周期）—— 图表的唯一数据源。

    口径出自《情绪周期表的用法》（用户 2026-09-23 指定），并按用户同日口径
    **只统计主板（10CM）**：北证/创业板/科创板不计入。

    数据来自本地行情仓（日频、收盘后落库），所以最后一个点通常是**上一交易日**；
    盘中要今天的数，看面板里的「情绪周期阶段/温度」（那是 9:25 竞价链路算的）。
    详见 `src/auction_select/sentiment_cycle.py` 的模块文档（含文档锚点校验：
    2023-11-24 大面 23 vs 文档 24）。
    """
    from src.auction_select.sentiment_cycle import build_cycle

    return build_cycle(days=days, refresh=refresh)


@router.get("")
async def info() -> dict[str, Any]:
    """模块自检：表行数、最近一次运行、调度状态、配置阈值。"""
    config = load_config()
    conn = _conn()
    try:
        return {
            "module": "auction_select",
            "db_path": str(config.db_path),
            "tables": db.count_rows(conn),
            "latest_trade_date": db.latest_trade_date(conn),
            "latest_run": db.query_run(conn),
            "scheduler": scheduler.state_snapshot(),
            "schedule": {
                "enabled": config.schedule.enabled,
                "preheat_at": config.schedule.preheat_at,
                "trigger": config.schedule.trigger,
                "deadline": config.schedule.deadline,
            },
            "universe": {
                "min_market_cap": config.universe.min_market_cap,
                "max_market_cap": config.universe.max_market_cap,
                # 前置筛选（用户口径 2026-09-19 规则 1）：昨收价上限 + 站上均线。
                # 市值/均线门槛的口径都是「截至昨日」，不是 9:25。
                "max_prev_close_price": config.universe.max_prev_close_price,
                "require_above_ma": config.universe.require_above_ma,
                "ma_window": config.universe.ma_window,
                "cap_prefilter_basis": "流通股本 × 昨日收盘价（前置筛选）",
                "exclude_st": config.universe.exclude_st,
                "require_yesterday_limit_up": config.universe.require_yesterday_limit_up,
            },
            "score": {
                "min_total_score": config.score.min_total_score,
                "pool_size": config.score.pool_size,
                # 抢筹族分档加分（规则 2）：大量抢筹 5 分、抢筹 3 分
                "rush_bonus": config.score.rush_bonus,
                "rush_bonus_heavy": config.score.rush_bonus_heavy,
                "weights": config.score.weights,
            },
            "veto": {
                "max_auction_volume_ratio": config.veto.max_auction_volume_ratio,
                "min_open_gap_pct": config.veto.min_open_gap_pct,
                "main_board_max_open_gap_pct": config.veto.main_board_max_open_gap_pct,
                # 规则 6（2026-09-19）：主板 >6% 时「大量抢筹」的量比门槛
                "main_board_rush_volume_pct":
                    config.veto.main_board_rush_volume_pct,
                "max_broken_rate": config.veto.max_broken_rate,
                "min_limit_up_count": config.veto.min_limit_up_count,
            },
        }
    finally:
        conn.close()


@router.get("/today")
async def today(trade_date: str = Query(default="")) -> dict[str, Any]:
    """当日选股池（`trade_date` 留空取最近一天）。"""
    conn = _conn()
    try:
        day = trade_date or db.latest_trade_date(conn)
        if not day:
            return {"trade_date": "", "picked": [], "overflow": [],
                    "prefiltered": [], "near_miss": [], "run": None,
                    "message": "还没有任何选股记录"}
        # ⚠️ 必须带 `include_vetoed=True`：默认会过滤掉 `vetoed=1` 的行，
        # 于是下面的 `vetoed` 列表永远是空的、前端"落选区"什么都不显示
        # （用户看到的现象就是"被否决的票查不到分项"）。
        picks = [_pick_to_api(row)
                 for row in db.query_picks(conn, day, include_vetoed=True)]
        run = db.query_run(conn, day)
        threshold = load_config().score.min_total_score
        # ⚠️ 达门槛的票要再分一层：`rank > 0` 才是真正入池的。
        # `overflow`（够分、没被否决，但排在 pool_size 之外）现在也会落库，
        # 如果不按 rank 拆开，前端的"入选"区会凭空多出几只没进池的票。
        picked = [item for item in picks if not item["vetoed"]
                  and float(item.get("total_score") or 0) >= threshold
                  and int(item.get("rank") or 0) > 0]
        overflow = [item for item in picks if not item["vetoed"]
                    and float(item.get("total_score") or 0) >= threshold
                    and int(item.get("rank") or 0) <= 0]
        overflow.sort(key=lambda item: -float(item.get("total_score") or 0))
        near = [item for item in picks if not item["vetoed"]
                and float(item.get("total_score") or 0) < threshold]
        near.sort(key=lambda item: -float(item.get("total_score") or 0))
        # **前置筛选剔除**的票单独一组（用户口径 2026-09-19 规则 1）：
        # 它们在"做任何判断之前"就被拦掉，没有分数、没有特征 —— 和"打过分被否决"
        # 是两回事，混在一列里会让人以为"打过分但被否决了"。
        # 判据：`vetoed=1` 且 `dim_scores` 为空（前置筛选行不落维度分）。
        prefiltered = [item for item in picks
                       if item["vetoed"] and not item.get("dim_scores")]
        vetoed = [item for item in picks
                  if item["vetoed"] and item.get("dim_scores")]
        return {
            "trade_date": day,
            "picked": picked,
            "overflow": overflow,
            "near_miss": near[:5],
            "prefiltered": prefiltered,
            "vetoed": vetoed[:40],
            "counts": {"picked": len(picked), "near_miss": len(near),
                       "overflow": len(overflow),
                       "prefiltered": len(prefiltered),
                       "vetoed": len(vetoed)},
            "run": _run_to_api(run),
            "threshold": threshold,
        }
    finally:
        conn.close()


def _run_to_api(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    item = dict(run)
    gaps = _loads(run.get("gaps"), [])
    # 从 gaps 里摘出「发酵题材」的结构化行（2026-09-22 用户口径）。
    #
    # 为什么它藏在 gaps 里：`auction_run` 是每日一条的窄表，为**纯展示**信息加列
    # 要动 schema + 迁移老库；而 gaps 本来就是"运行说明"的自由通道、已经落库、
    # 已经透传到这里。摘出来之后**不要把那条 JSON 留在 gaps 里** —— 那是给人读的
    # 列表，混一条机器串进去会让人以为运行出错了。
    fermenting: dict[str, Any] = {}
    clean: list[Any] = []
    for gap in gaps if isinstance(gaps, list) else []:
        text = str(gap)
        if text.startswith(service._FERMENT_PREFIX):
            try:
                fermenting = json.loads(text[len(service._FERMENT_PREFIX):])
            except ValueError:
                clean.append(text)      # 解析不了就照旧当普通 gap 展示（不吞）
            continue
        clean.append(gap)
    item["gaps"] = clean
    item["fermenting_theme"] = fermenting
    item["on_time"] = bool(run.get("on_time"))
    return item


@router.get("/watch")
async def watch_list() -> dict[str, Any]:
    """手动关注列表 + 最近一轮的打分与操作建议（用户口径 2026-09-22）。

    列表本身**跨日保留**（`auction_watch`），分数是**每日一轮**的结果
    （`auction_watch_score`，重跑一次就重算一次）。两者分开的理由：
    用户加的票不该明天就消失，而分数昨天的对今天没有意义。
    """
    conn = _conn()
    try:
        items = db.query_watch(conn)
        scores = {row["code"]: _watch_score_to_api(row)
                  for row in db.query_watch_scores(conn)}
        day = str(conn.execute(
            f"SELECT MAX(trade_date) d FROM {db.WATCH_SCORE_TABLE}"
        ).fetchone()["d"] or "")
        # 列表里的票**一定**都要出现（哪怕还没打过分的），否则用户加了却看不到；
        # 没分数的给 `None` 由前端显示"待重跑一次后打分"。
        out = []
        for item in items:
            code = str(item.get("code") or "")
            out.append({
                "code": code, "name": item.get("name") or "", "note": item.get("note") or "",
                "added_at": item.get("added_at") or "",
                "score": scores.get(code),
            })
        return {"trade_date": day, "items": out, "count": len(out)}
    finally:
        conn.close()


@router.post("/watch")
async def watch_add(payload: WatchAddRequest) -> dict[str, Any]:
    """加一只到关注列表。已存在则只更新名称/备注（幂等）。"""
    code = "".join(ch for ch in str(payload.code or "") if ch.isdigit())
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="请给 6 位股票代码（如 600325）")
    conn = _conn()
    try:
        created = db.add_watch(conn, code, name=payload.name, note=payload.note)
        name = payload.name
        if not name:
            # 名称尽量补上：读一次本股票字典，省得前端显示一串数字。
            # ⚠️ `auto_enrich=False`：加票是**用户交互**路径，不能因为字典里没有
            # 就去连东财（那会让"加一只"卡十几秒）。查不到就留空，下次重跑补。
            try:
                from src.quant.stock_directory import stock_directory

                name = stock_directory().name_of(code, auto_enrich=False) or ""
                if name:
                    db.add_watch(conn, code, name=name)
            except Exception:  # noqa: BLE001 查不到名称不影响加票
                name = ""
        return {"ok": True, "code": code, "name": name,
                "created": created,
                "message": ("已加入关注列表" if created
                            else "该票已在关注列表（已更新名称/备注）")}
    finally:
        conn.close()


@router.delete("/watch/{code}")
async def watch_remove(code: str) -> dict[str, Any]:
    """从关注列表移除（连它的历史打分一起清掉，避免前端读到已删的票）。"""
    conn = _conn()
    try:
        removed = db.remove_watch(conn, code)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{code} 不在关注列表里")
        return {"ok": True, "code": code, "message": "已移出关注列表"}
    finally:
        conn.close()


def _watch_score_to_api(row: dict[str, Any]) -> dict[str, Any]:
    """关注票打分行 → 接口结构（JSON 列解开、去掉内部字段）。"""
    item = dict(row)
    for key in ("dim_scores", "dim_notes", "advice_basis", "degraded"):
        item[key] = _loads(row.get(key), [] if key in ("advice_basis", "degraded") else {})
    # `features` 太大（几十个键），前端那一格用不到 —— 只留"从哪个源来"的可读摘要
    item.pop("features", None)
    return item


@router.get("/detail/{code}")
async def detail(code: str, trade_date: str = Query(default="")) -> dict[str, Any]:
    """单只股票的竞价详情：快照 + 竞价序列 + 特征 + 打分（前端个股详情用）。"""
    conn = _conn()
    try:
        day = trade_date or db.latest_trade_date(conn)
        if not day:
            raise HTTPException(status_code=404, detail="还没有选股记录")
        snapshot = db.query_snapshot(conn, day, code)
        feature = db.query_feature(conn, day, code)
        picks = {row["code"]: row for row in db.query_picks(conn, day, include_vetoed=True)}
        pick = picks.get(code)
        if snapshot is None and feature is None and pick is None:
            raise HTTPException(status_code=404,
                                detail=f"{day} 没有 {code} 的竞价数据")
        payload: dict[str, Any] = {"trade_date": day, "code": code}
        if snapshot:
            payload["snapshot"] = {
                **{k: v for k, v in snapshot.items() if k not in ("series", "series_meta")},
                "series": _loads(snapshot.get("series"), []),
                "series_meta": _loads(snapshot.get("series_meta"), {}),
            }
        if feature:
            payload["feature"] = _feature_to_api(feature)
        if pick:
            payload["pick"] = _pick_to_api(pick)
        return payload
    finally:
        conn.close()


@router.get("/stage_guide")
async def stage_guide(
    stage: str = Query(default="", description="情绪周期阶段；留空则取最近一轮运行"),
) -> dict[str, Any]:
    """当前情绪周期阶段的 skill 操作指南：仓位管理 / 择股方向 / 风险。

    数据取自 `skills/jianmen-shortterm/references/`（`cycle.json` 的
    `cycle_stages[].action` + `indicators`，`modes.json` 的 `timing_matrix[]`
    的 `position_hint` / `best_modes` / `avoid`）—— **原样返回，不改写**。

    前端把这块显示在运行摘要的「情绪周期温度」后面、竞价选股池上方。
    `stage` 留空时回落到最近一轮运行记录的阶段，这样不依赖前端传参也能用。
    """
    from src.auction_select import skill_guide

    target = stage.strip()
    if not target:
        conn = _conn()
        try:
            recent = db.query_recent_runs(conn, 1)
        finally:
            conn.close()
        target = str((recent[0] if recent else {}).get("market_stage") or "")
    return skill_guide.build_stage_guide(target)


@router.get("/runs")
async def runs(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """历史运行记录（看每天是否按时、出池几只、有哪些缺口）。"""
    conn = _conn()
    try:
        rows = db.query_recent_runs(conn, limit)
        return {"runs": [_run_to_api(row) for row in rows], "count": len(rows)}
    finally:
        conn.close()


@router.post("/run")
async def manual_run(payload: ManualRunRequest) -> dict[str, Any]:
    """手动触发一次选股（**跑在后台**，避免请求超时）。

    ## 实际耗时（2026-09-22 实测，别再照抄"三分钟"）

    | 情形 | 整轮耗时 |
    |---|---|
    | 预热缓存命中（常态：09:15 的预热循环已经跑过） | **2~12 秒**（实测 0917~0922 为 7.6 / 2.2 / 7.2 / 12.1 秒） |
    | 预热缓存**完全冷**（服务刚起或跨日第一次） | **约 2 分钟**（实测冷预热 116s，其中 `theme_strength_rank` 一项 114s） |

    原来的写法是"一轮含预热要三分钟以上" —— 那是**冷预热**的上限，被当成了常态，
    于是前端跟着写「约 6 秒」+ 8 秒后补拉一次；冷预热时 8 秒时服务端还在跑、
    拉回来仍是旧结果，表现为"点了没反应"（用户口径 2026-09-22 报障）。

    所以这里起后台线程、立刻返回（同步等待会顶到反代/浏览器超时，
    项目已踩过 300 秒超时的坑），**耗时的预期由前端按"轮询到结果变化"处理**，
    不再写死一个秒数。
    """
    import threading

    config = load_config()
    # 开市日守卫：`force=True` 时放行。用途有两个 ——
    # ① 算法/参数升级后重算当日结果；
    # ② 跨零点复跑（实测踩到：00:48 时市场时钟仍报前一日会话，
    #    `is_trading_day()` 判 False，连一次手动重跑都发不出去）。
    # 非 force 的触发仍然严格守开市日，避免在休市日空跑。
    if not payload.force and not scheduler.is_trading_day():
        raise HTTPException(status_code=400, detail="今天不是开市日")

    outcome: dict[str, Any] = {"status": "running", "error": ""}

    def _worker() -> None:
        try:
            result = scheduler.run_once(trade_date=payload.trade_date,
                                        config=config, force=payload.force)
            outcome.update({"status": result.status, "picked": len(result.picked),
                            "seconds": round(result.seconds, 2),
                            "on_time": result.on_time, "gaps": result.gaps[:5]})
        except Exception as exc:  # noqa: BLE001 后台线程不能把异常抛给请求
            outcome.update({"status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"})
            logger.warning("手动竞价选股失败：%s", exc)

    thread = threading.Thread(target=_worker, name="auction-manual",
                              daemon=True)
    thread.start()
    # 给 3 秒看它是否立刻失败（例如"已在运行"），不长时间阻塞请求
    await asyncio.sleep(3)
    return {"ok": outcome["status"] != "failed", **outcome,
            "message": "已在后台开始，稍后刷新查看结果" if outcome["status"] == "running"
            else outcome.get("error") or "完成"}


@router.get("/preheat")
async def preheat(force: bool = Query(default=False)) -> dict[str, Any]:
    """预热数据摘要（前端展示"昨日涨停池/题材榜是否就绪"）。"""
    config = load_config()
    data = scheduler.get_preheat(force=force, config=config)
    if data is None:
        raise HTTPException(status_code=503, detail="预热不可用（eltdx 未连接？）")
    return {
        "summary": data.to_dict(),
        "themes": data.themes[:15],
        "limit_up_sample": data.limit_up[:10],
    }


@router.get("/state")
async def state() -> dict[str, Any]:
    """调度状态（前端轮询"今天跑了没有"）。"""
    return scheduler.state_snapshot()


__all__ = ["router"]
