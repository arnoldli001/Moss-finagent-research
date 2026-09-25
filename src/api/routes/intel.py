"""舆情情报：REST 接口（前缀 `/api/v1/intel`）。

## 三道门各管一件事

| 层 | 管什么 | 在哪 |
|---|---|---|
| `LoginGateMiddleware` | 登录了没有 | 自动 —— 新路由默认需登录，不必在此重复 |
| `require_feature` | **这个等级买了没有** | 本文件每个端点显式调用 |
| `core.policy` | 角色能不能做这个动作 | 写操作（事件处置） |

## 数据源保密的实现位置（重要）

**接口响应里根本不存在**以下字段（不是"脱敏"，是不构造）：
`source_url` / `report_url` / `group_id` / `author_id` / `topic_id` / token。

三层保证，缺一不可：
  1. 连接器 `IntelItem.to_public()` 白名单构造（新字段默认不出）
  2. 本路由只回 `feed.to_public()`，**不直接序列化内部对象**
  3. `tests/unit/test_intel_source_privacy.py` 断言键集合与值形态

## 为什么接口不做 LLM 分析

采集要快（秒级），模型慢（本地 8B ~30s/批）。分析放调度任务，
结果存库给接口读 —— 在线延迟稳定，模型升级/重跑不影响用户。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.routes.my_features import require_feature
from src.core.errors import brief
from src.domain.intel.service import DEFAULT_LIMIT, build_feed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/intel", tags=["intel"])

#: 功能 key（与 `platform/config.py` 的 FEATURES 一致）
FEATURE_RADAR = "intel.radar"
FEATURE_BRIEF = "intel.brief"
FEATURE_ALERTS = "intel.alerts"


# ======================================================================
# 情报流（情报雷达）
# ======================================================================

@router.get("/feed")
async def intel_feed(
    request: Request,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200,
                       description="返回条数上限"),
    codes: str = Query(default="",
                       description="关注标的（逗号分隔，用于拉取对应研报）"),
    sort: str = Query(default="credibility",
                      pattern="^(credibility|time)$",
                      description="取样顺序：credibility 优先纳入高可信条目｜"
                                  "time 纯时间"),
    filter: str = Query(default="all",
                        description="可信度筛选档：all/high/mid_up/low/"
                                    "official/broker（只作用于**返回结果**，"
                                    "计数 counts 始终是全量口径）"),
) -> dict[str, Any]:
    """聚合六源情报流。

    **单源失败不影响整体** —— 失败进 `gaps`，并置 `degraded=true`，
    前端据此显示"数据不完整"（**不显示具体是哪个源坏了**）。

    ## 筛选为什么在服务端做，而不是前端过滤

    前端过滤只能过滤"已经取回来的这一页"（默认 60 条），于是
    "高可信 ≥80" 可能只有 6 条、而全量里其实有 30 条 —— 用户看到的是
    **取样的结果**，不是**数据的真相**。所以筛选必须在下发前做：
    服务端按档位重新取样，保证这一页就是"符合该档位的最近 N 条"。
    """
    await require_feature(request, FEATURE_RADAR)

    watch = [c.strip() for c in codes.split(",") if c.strip()]
    try:
        feed = await build_feed(watch_codes=watch, limit=limit, sort=sort)
    except Exception as exc:  # noqa: BLE001 聚合层不该把 500 抛给用户
        logger.exception("情报聚合失败")
        raise HTTPException(
            status_code=503,
            detail={"code": "intel_unavailable",
                    "message": brief(exc) or "情报聚合暂时不可用，请稍后重试"}) from exc

    payload = feed.to_public()
    # 用户侧只看到"某类来源暂无更新"；管理员提示单独放 admin_hints，
    # 由前端按 `applied_tier == 'admin'` 决定是否展示。
    admin_hints = [g["admin_hint"] for g in feed.gaps if g.get("admin_hint")]
    public_gaps = [{k: v for k, v in g.items() if k != "admin_hint"}
                   for g in feed.gaps]
    payload["gaps"] = public_gaps
    payload["admin_hints"] = admin_hints

    # ── 可信度筛选（下发前做）──
    from src.domain.intel.credibility import (
        FILTERS, level_of, matches_filter,
    )

    if filter not in FILTERS:
        raise HTTPException(
            status_code=422,
            detail={"code": "bad_filter",
                    "message": f"filter 必须是 {'/'.join(FILTERS)} 之一"})

    all_items = payload.get("items") or []
    kept = []
    for it in all_items:
        raw = it.get("credibility") or {}
        try:
            score = int(raw.get("score"))
        except (TypeError, ValueError):
            score = 0
        # 复用 `credibility.Credibility` 的判据，避免前后端两套口径漂移
        from src.domain.intel.credibility import Credibility

        cred = Credibility(
            score=score,
            source_base=int(raw.get("source_base") or 0),
            content_base=int(raw.get("content_base") or 0),
            source_reason=str(raw.get("source_reason") or ""),
            content_reason=str(raw.get("content_reason") or ""))
        if matches_filter(cred, str(it.get("kind") or ""), filter):
            kept.append(it)
    payload["items"] = kept
    payload["filter"] = filter
    payload["sort"] = sort
    # 分层计数（面向筛选 tab 的角标）。**基于全量 counts 的同批条目**，
    # 不随当前 filter 变化 —— 否则切一次 tab 角标就全变了，用户会以为数据在动。
    dist: dict[str, int] = {}
    for it in all_items:
        raw = it.get("credibility") or {}
        try:
            lv = level_of(int(raw.get("score")))[0]
        except (TypeError, ValueError):
            lv = "doubt"
        dist[lv] = dist.get(lv, 0) + 1
    payload["credibility_dist"] = dist
    payload["filters"] = FILTERS
    return payload


@router.get("/calendar")
async def intel_calendar(
    request: Request,
    horizon_days: int = Query(default=30, ge=1, le=180,
                              description="展望天数"),
) -> dict[str, Any]:
    """投资日历：预约披露 / 限售解禁 / 宏观发布 / 交易日。

    每类**主备互用**（见 `src/domain/intel/calendar.py`）：
    主源失败自动走备源，全失败则进 `gaps` 并置 `degraded=true`。

    ⚠️ **交易日历刻意无备源** —— 交易日是交易所规则，不存在"第二个可信来源"，
    用不可信的日历会让整个调度在错误的日子跑，后果比"日历不可用"严重。

    合规：只呈现**已公布的日程**与**覆盖范围统计**，
    不含方向判断、不给目标价、不给买卖时点。
    """
    await require_feature(request, FEATURE_RADAR)

    from src.domain.intel.calendar import build_calendar

    try:
        res = await build_calendar(horizon_days=horizon_days)
    except Exception as exc:  # noqa: BLE001
        logger.exception("投资日历聚合失败")
        raise HTTPException(
            status_code=503,
            detail={"code": "calendar_unavailable",
                    "message": brief(exc) or "投资日历暂时不可用"}) from exc

    payload = res.to_public()
    payload["disclaimer"] = ("本日历只呈现已公布的日程安排与覆盖范围统计，"
                             "不含方向判断，不构成投资建议。"
                             "日程可能变更，请以交易所与公司公告为准。")
    return payload


@router.get("/sources/health")
async def intel_sources_health(request: Request) -> dict[str, Any]:
    """各源最近一次采集健康度 —— **只给聚合状态，不按源名细分**。

    为什么聚合而不细分：`{"zsxq_48848484411448": {...}}` 这种形状
    等于把群组 ID 直接印在响应里。前端只需要知道"采集是否正常"。
    """
    await require_feature(request, FEATURE_RADAR)

    from src.infrastructure.connectors.intel_sources import health

    h = health()
    total = len(h)
    ok = sum(1 for v in h.values() if v.get("ok"))
    return {
        # 聚合口径：不暴露有几个源、分别叫什么
        "state": "healthy" if total == 0 or ok == total else "degraded",
        "sources_total": total,
        "sources_ok": ok,
        # 最近一次的耗时区间（性能用，不含来源标识）
        "last_ok_ms": max((v.get("ms", 0) for v in h.values()
                           if v.get("ok")), default=0),
    }


# ======================================================================
# 盘前简报
# ======================================================================

@router.get("/brief")
async def intel_brief(request: Request) -> dict[str, Any]:
    """盘前简报：盘前新闻 / 研报热度 / 盘前预测 三项的**汇总视图**。

    ⚠️ 当前返回的是"素材"（聚合后的条目），**不含方向判断**。
    四项定时任务产物（08:30 前）由调度器写入后由此读取 ——
    见 `docs/INTEL_CENTER_REDESIGN.md` §2。
    """
    await require_feature(request, FEATURE_BRIEF)

    feed = await build_feed(limit=40)
    items = feed.to_public()["items"]

    # 按类型分组（简报的三段结构）
    grouped: dict[str, list[dict[str, Any]]] = {
        "newswire": [], "broker_report": [], "policy": [], "research_note": [],
    }
    for it in items:
        grouped.setdefault(str(it.get("kind")), []).append(it)

    return {
        "fetched_at": feed.fetched_at,
        "degraded": feed.degraded,
        "sections": {
            "premarket_news": grouped.get("newswire", [])[:15],
            "broker_heat": grouped.get("broker_report", [])[:15],
            "policy": grouped.get("policy", [])[:10],
            "research_notes": grouped.get("research_note", [])[:15],
        },
        "counts": feed.counts,
        # 合规边界固定下发，前端原样展示
        "disclaimer": ("本简报为公开信息的聚合与统计，不含方向判断，"
                       "不构成投资建议。"),
    }


# ======================================================================
# 事件处置（告警中心）
# ======================================================================

class AlertStateBody(BaseModel):
    state: str = Field(description="ack（已确认）| ignore（已忽略）| open（重新打开）")
    note: str = Field(default="", max_length=200, description="处置备注")


@router.post("/alerts/{alert_id}/state")
async def set_alert_state(alert_id: str, body: AlertStateBody,
                          request: Request) -> dict[str, Any]:
    """处置一个事件（确认 / 忽略 / 重新打开）。

    只改**状态与备注**，不删数据 —— 审计链要能回溯"谁在什么时候处置了什么"。
    """
    user_id, _ = await require_feature(request, FEATURE_ALERTS)

    allowed = {"ack", "ignore", "open"}
    if body.state not in allowed:
        raise HTTPException(
            status_code=422,
            detail={"code": "bad_state",
                    "message": f"state 必须是 {'/'.join(sorted(allowed))} 之一"})

    # 角色维度：处置动作走 policy（合规/审计只读）
    from src.core.policy import WRITE_ALERT_STATE, authorize

    decision = authorize(action=WRITE_ALERT_STATE, user_id=user_id)
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden",
                    "message": decision.reason or "当前角色无权处置事件"})

    # 事件存储由 alerts 模块负责；此处只做状态变更与留痕。
    from src.domain.alerts import service as alerts_service

    try:
        await asyncio.to_thread(
            alerts_service.set_state, alert_id, body.state,
            operator=user_id, note=body.note)
    except AttributeError:
        # alerts 模块尚未提供该入口 → 明确报"未实现"，不要假装成功。
        # （假装成功会让前端显示"已处置"而库里没变，是最坏的一类静默故障）
        raise HTTPException(
            status_code=501,
            detail={"code": "not_implemented",
                    "message": "事件处置存储尚未接入，请联系开发者"}) from None
    except Exception as exc:  # noqa: BLE001
        logger.exception("事件处置失败 alert_id=%s", alert_id)
        raise HTTPException(
            status_code=400,
            detail={"code": "alert_state_failed",
                    "message": brief(exc) or "处置失败"}) from exc

    return {"ok": True, "alert_id": alert_id, "state": body.state,
            "operator": user_id}
