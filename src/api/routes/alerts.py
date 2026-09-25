"""事件告警REST与WebSocket路由（FR-10/FR-12，全snake_case）。

- 列表/详情/已读/事件查询/手工导入/手动扫描(后台执行,2秒内返回)/阈值配置；
- /ws/alerts 实时推送，连上先推未读快照，离线不补发；
- 事件子系统未装配(SQLite外后端)时返回503降级标注。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from src.core.config import get_settings
from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.domain.alerts.dedup import dedupe_events
from src.domain.alerts.models import DEFAULT_TENANT, DISCLAIMER, ScanResult
from src.domain.alerts.normalize import normalize_events, now_iso
from src.domain.alerts.service import ScanInProgressError
from src.scheduler.registry import JOB_REGISTRY, alert_scan_schedule

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["alerts"])

_MAX_IMPORT = 50
_TITLE_LIMIT = 200
_CONTENT_LIMIT = 2000
_VALID_TYPES = {"policy", "sector", "stock", "calendar", ""}


def _runtime(request: Request):
    return request.app.state.runtime


def _require_stack(request: Request):
    runtime = _runtime(request)
    if runtime.event_repo is None or runtime.event_service is None:
        raise HTTPException(
            status_code=503,
            detail="事件告警子系统不可用：当前DATA_BACKEND暂仅支持sqlite")
    return runtime


def _scan_state(request: Request) -> dict:
    state = request.app.state
    if getattr(state, "alert_scan_state", None) is None:
        state.alert_scan_state = {"running": False, "started_at": "", "result": None}
    return state.alert_scan_state


# ======================================================================
# 来源脱敏（**这是数据源保密的关键一环，别绕过它直接 model_dump**）
# ======================================================================

#: 告警/事件输出里**只对管理员**保留的字段。
_ADMIN_ONLY_FIELDS = ("source_name", "source_url")
#: 用户可见字段里**可能夹带上游 URL 的文本**
_TEXT_FIELDS = ("description", "impact_path", "content")

#: `raw_data` 里**允许出接口**的键（白名单）。
#:
#: ⚠️ 必须白名单，不能黑名单。实测 `raw_data` 里躺着 `source_tag: "sina"` ——
#: 那是**内部来源标识**，等于把渠道名直接印在事件里，前面给 `source_name`
#: 做的假名化在这里全白费。黑名单式（"剔除 source_tag"）挡不住下次
#: 有人再往里塞一个 `channel` / `feed` / `vendor`。
_RAW_DATA_PUBLIC_KEYS = frozenset({"importance"})


def _safe_raw_data(raw: object) -> dict:
    """只保留白名单键的 `raw_data`（新键默认**不出**）。"""
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in _RAW_DATA_PUBLIC_KEYS}


def _strip_urls(text: object) -> str:
    """抹掉文本里的 URL，保留其余内容。

    为什么不能只删 `source_url` 字段就算完：`description` 与 `impact_path`
    是**分析层生成的自然语言**，实测里它们会把上游链接整段抄进来；`content`
    更是事件原文。渗透工具只要读这几个字段就能拿到域名。
    """
    import re

    s = "" if text is None else str(text)
    if not s:
        return s
    # 只匹配 http(s):// 开头的串，遇到空白/引号/中文标点即止
    return re.sub(r"https?://[^\s\"'<>）】，。；]+", "（链接已隐藏）", s)


async def _viewer_is_admin(request: Request) -> bool:
    """当前请求是不是管理员。

    **fail-closed**：解析不出来时按"非管理员"处理 —— 脱敏失败只是少给
    一点信息，反过来则是把数据源交出去。所以默认值必须是"更严的那一边"。
    """
    try:
        from src.api.session_ctx import current_user

        _, tier = await current_user(request, write=False)
        return str(tier) == "admin"
    except Exception:  # noqa: BLE001 拿不到身份就按非管理员
        return False


async def _viewer_user_id(request: Request) -> str:
    """当前请求的**用户** ID，拿不到返回空串。

    ## 为什么每个读/写已读的端点都必须拿它

    已读原来是写在 `fact_alerts.status` 上的**行级单值**，而告警行按
    `tenant_id` 共享 —— 实测 pilot 库 47 条告警全是 `tenant_001`，
    而系统里有 13 个 vip 用户。于是**任何一个人点已读，全体一起清零**。
    按用户隔离的前提就是"知道是谁"。

    返回空串时调用方退回旧的全局行为（不假装隔离成功）——
    这在只读展示上是可接受的降级，但**不该出现在写路径上**。
    """
    try:
        from src.api.session_ctx import current_user

        user_id, _ = await current_user(request, write=False)
        return str(user_id)
    except Exception:  # noqa: BLE001
        return ""


def alert_to_public(alert, *, is_admin: bool) -> dict:
    """告警 → 可出接口的 dict。

    ## 为什么必须有这一层

    原来直接 `alert.model_dump()`，于是响应里带着：

        "source_name": "东方财富-全球财经"
        "source_url":  "https://finance.eastmoney.com/a/202609243883509677.html"

    任何登录用户按一下 F12 就能看到**我们用了哪几个免费渠道**，
    而"渠道组合 + 采集节奏"正是本项目的壁垒。前端**根本没用到**
    这两个字段（`AlertsPanel` 只渲染了 `source_name` 做署名），
    所以把它们从用户响应里去掉不损失任何功能。

    ## 三层处理

      · `source_name` → 用户侧换成**稳定假名** `source_alias`
        （不是打码成 `***`：那样所有来源塌成同一个值，按源分组/去重全失效）
      · `source_url`  → **一律不给用户**（管理员保留，他需要排查）
      · 文本字段里的裸 URL → 抹成"（链接已隐藏）"
    """
    data = alert.model_dump()
    for f in _TEXT_FIELDS:
        if f in data:
            data[f] = _strip_urls(data.get(f))
    # `raw_data` 走白名单（实测里面有内部 `source_tag`）
    if "raw_data" in data:
        data["raw_data"] = _safe_raw_data(data.get("raw_data"))

    if is_admin:
        # 管理员要能溯源排障：保留真名与链接
        return data

    from src.core.redaction import source_pseudonym

    raw_name = str(data.get("source_name") or "")
    data.pop("source_url", None)
    data.pop("source_name", None)
    data["source_alias"] = source_pseudonym(raw_name)
    return data


def event_to_public(event, *, is_admin: bool) -> dict:
    """事件 → 可出接口的 dict。规则同 `alert_to_public`。"""
    data = event.model_dump()
    for f in _TEXT_FIELDS:
        if f in data:
            data[f] = _strip_urls(data.get(f))
    # ★ 这是实测抓到的泄漏点：`raw_data.source_tag = "sina"` 原样出接口，
    #   用户直接看到渠道名，而 `source_alias` 的假名化在这里全白费。
    if "raw_data" in data:
        data["raw_data"] = _safe_raw_data(data.get("raw_data"))

    if is_admin:
        return data

    from src.core.redaction import source_pseudonym

    raw_name = str(data.get("source_name") or "")
    data.pop("source_url", None)
    data.pop("source_name", None)
    data["source_alias"] = source_pseudonym(raw_name)
    return data


# ---------- 告警查询/操作 ----------

@router.get("/alerts/settings")
async def alert_settings(request: Request) -> dict:
    runtime = _runtime(request)
    settings = get_settings()
    emailer = getattr(runtime, "email_notifier", None)
    spec = JOB_REGISTRY.get("event_alert_daily")
    return {
        "available": runtime.event_repo is not None,
        "confidence_min": settings.alert_confidence_min,
        "min_level": settings.alert_min_level,
        "cooldown_hours": settings.alert_cooldown_hours,
        "candidate_limit": settings.alert_scan_candidate_limit,
        "thresholds": {
            "risk": {"high": settings.alert_risk_high,
                     "medium": settings.alert_risk_medium,
                     "low": settings.alert_risk_low},
            "opportunity": {"high": settings.alert_opp_high,
                            "medium": settings.alert_opp_medium,
                            "low": settings.alert_opp_low},
        },
        "email": {
            "configured": bool(emailer and emailer.is_configured()),
            "smtp_host": settings.alert_smtp_host,
            "risk_min_score": settings.alert_email_risk_min_score,
            "opp_min_score": settings.alert_email_opp_min_score,
            "to": settings.alert_email_to,
        },
        # 扫描时机：定时班次有三班（盘中/午盘/收盘后），时刻表从注册表的 cron
        # 现算（`alert_scan_schedule`），前端直接展示、不再自己解析 cron。
        # `job`/`cron` 两个旧字段保留给老前端，指向收盘后那班全量扫描。
        "schedule": {
            "job": "event_alert_daily",
            "cron": spec.cron if spec else None,
            **alert_scan_schedule(),
        },
        "disclaimer": DISCLAIMER,
    }


@router.get("/alerts/unread-count")
async def unread_count(request: Request, tenant_id: str = DEFAULT_TENANT) -> dict:
    runtime = _require_stack(request)
    # 按**用户**统计 —— 不是全局。见 _viewer_user_id 的说明。
    return {"unread": await runtime.event_repo.count_unread(
        tenant_id, user_id=await _viewer_user_id(request))}


@router.post("/alerts/scan", status_code=202)
async def trigger_scan(
    request: Request,
    force: bool = Query(
        default=False,
        description="true=强制重判（绕过 LLM 缓存），false=复用上次对同一批事件的评估"),
) -> dict:
    """手动触发扫描：后台执行，2秒内返回accepted，前端轮询scan/latest。

    `force=true` 给"我就是要重新判一次"的场景 —— 它会把 `use_cache=False`
    一路传到 LLM 网关。不传则复用缓存（同一批事件不会重复计费）。
    """
    runtime = _require_stack(request)
    scan = _scan_state(request)
    if scan["running"] or runtime.event_service.is_running:
        raise HTTPException(status_code=409, detail="上一次扫描仍在执行中")
    scan["running"] = True
    scan["started_at"] = now_iso()
    scan["result"] = None

    async def _job() -> None:
        try:
            scan["result"] = (
                await runtime.event_service.run("manual", force=force)).model_dump()
        except ScanInProgressError as exc:
            # 与定时作业/调度器手动入口的共享锁冲突：跳过而非失败
            scan["result"] = ScanResult(
                trigger="manual", status="success",
                data_gaps=[f"扫描已在执行中，本次跳过：{brief(exc, BRIEF_TIGHT)}"],
                started_at=scan["started_at"], finished_at=now_iso()).model_dump()
        except Exception as exc:  # noqa: BLE001 服务总异常也要让轮询可见
            logger.exception("手动事件扫描失败")
            scan["result"] = ScanResult(
                trigger="manual", status="failed",
                errors=[f"扫描异常: {brief(exc, BRIEF_DEFAULT)}"],
                started_at=scan["started_at"], finished_at=now_iso()).model_dump()
        finally:
            scan["running"] = False

    asyncio.create_task(_job(), name="manual-event-scan")
    return {"status": "accepted", "started_at": scan["started_at"],
            "poll": "/api/v1/alerts/scan/latest"}


@router.get("/alerts/scan/latest")
async def scan_latest(request: Request) -> dict:
    return _scan_state(request)


@router.post("/alerts/read-all")
async def mark_all_read(request: Request, tenant_id: str = DEFAULT_TENANT) -> dict:
    runtime = _require_stack(request)
    # 只把**这个用户**的告警标成已读。原来是一条 UPDATE 把整个租户的
    # 告警全置 read —— 一个人点"全部已读"，13 个人的角标一起清零。
    return {"updated": await runtime.event_repo.mark_all_read(
        tenant_id, user_id=await _viewer_user_id(request))}


@router.get("/alerts/{alert_id}")
async def get_alert(
    request: Request, alert_id: str, tenant_id: str = DEFAULT_TENANT,
) -> dict:
    runtime = _require_stack(request)
    alert = await runtime.event_repo.get_alert(alert_id, tenant_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在或已过期")
    return {"alert": alert_to_public(alert,
                                     is_admin=await _viewer_is_admin(request))}


@router.post("/alerts/{alert_id}/read")
async def mark_read(
    request: Request, alert_id: str, tenant_id: str = DEFAULT_TENANT,
) -> dict:
    runtime = _require_stack(request)
    updated = await runtime.event_repo.mark_read(
        alert_id, tenant_id, user_id=await _viewer_user_id(request))
    if not updated:
        raise HTTPException(status_code=404, detail="告警不存在或已读")
    return {"alert_id": alert_id, "status": "read"}


@router.get("/alerts")
async def list_alerts(
    request: Request, type: str | None = None, level: str | None = None,
    status: str | None = None, limit: int = 100,
    include_expired: bool = False, tenant_id: str = DEFAULT_TENANT,
) -> dict:
    runtime = _require_stack(request)
    limit = max(1, min(limit, 200))
    uid = await _viewer_user_id(request)
    alerts = await runtime.event_repo.list_alerts(
        alert_type=type, alert_level=level, status=status,
        limit=limit, include_expired=include_expired, tenant_id=tenant_id,
        user_id=uid)
    # 未读数也要按用户 —— 否则列表是我的未读、角标是全体的未读，
    # 两个数对不上，用户会以为系统坏了
    unread = await runtime.event_repo.count_unread(tenant_id, user_id=uid)
    is_admin = await _viewer_is_admin(request)
    return {"alerts": [alert_to_public(a, is_admin=is_admin) for a in alerts],
            "total": len(alerts), "unread": unread}


# ---------- 事件 ----------

@router.get("/events")
async def list_events(
    request: Request, type: str | None = None, limit: int = 100,
    tenant_id: str = DEFAULT_TENANT,
) -> dict:
    runtime = _require_stack(request)
    limit = max(1, min(limit, 200))
    events = await runtime.event_repo.list_events(
        event_type=type, limit=limit, tenant_id=tenant_id)
    is_admin = await _viewer_is_admin(request)
    return {"events": [event_to_public(e, is_admin=is_admin) for e in events],
            "total": len(events)}


@router.post("/events/import")
async def import_events(request: Request, body: dict) -> dict:
    """手工导入事件（仅标准化入库；下次扫描或手动扫描时参与分析）。"""
    runtime = _require_stack(request)
    items = body.get("events")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=422, detail="events必须为非空数组")
    if len(items) > _MAX_IMPORT:
        raise HTTPException(status_code=422,
                            detail=f"单次最多导入{_MAX_IMPORT}条")
    raws = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="事件项必须为对象")
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        etype = str(item.get("event_type", "")).strip()
        if not title or len(title) > _TITLE_LIMIT:
            raise HTTPException(status_code=422,
                                detail=f"title必填且≤{_TITLE_LIMIT}字")
        if len(content) > _CONTENT_LIMIT:
            raise HTTPException(status_code=422,
                                detail=f"content≤{_CONTENT_LIMIT}字")
        if etype not in _VALID_TYPES:
            raise HTTPException(status_code=422,
                                detail="event_type仅支持policy/sector/stock/calendar")
        raws.append({
            "title": title, "content": content, "type_hint": etype,
            "source_name": str(item.get("source_name") or "manual_import")[:60],
            "source_url": str(item.get("source_url") or "")[:300],
            "publish_time": str(item.get("publish_time") or "")[:40],
            "extra": {"source_tag": "manual"},
        })
    events = dedupe_events(normalize_events(raws, fetch_time=now_iso()))
    if not events:
        raise HTTPException(status_code=422, detail="无有效事件可导入")
    saved = await runtime.event_repo.upsert_events(events)
    return {"received": len(items), "inserted": saved["inserted"],
            "skipped": saved["skipped"],
            "event_ids": [e.event_id for e in events]}


# ---------- WebSocket ----------

@router.websocket("/ws/alerts")
async def alerts_ws(websocket: WebSocket, tenant_id: str = DEFAULT_TENANT) -> None:
    # ★ 同上：WS 不走 HTTP 登录门槛，必须自己校验会话
    from src.api.session_ctx import ws_allow

    if not await ws_allow(websocket):
        return
    runtime = websocket.app.state.runtime
    hub = runtime.alert_hub
    if hub is None or runtime.event_repo is None:
        await websocket.close(code=1013)  # 子系统不可用
        return

    # 该连接是不是管理员 —— 决定它能不能看到来源真名与原文链接。
    # 脱敏必须**按连接**（同一 vip 租户下既有管理员也有普通用户），
    # 所以身份在这里解析一次、登记到 hub 上，由 hub 逐连接序列化。
    from src.api.session_ctx import ws_identity

    ident = await ws_identity(websocket)
    is_admin = bool(ident and ident[1] == "admin")
    # 用户 ID 同样按连接取 —— 快照里的"未读"必须是**这个人**的未读，
    # 否则他推上来会看到别人的已读状态（或漏掉自己的未读）
    ws_user_id = str(ident[0]) if ident else ""

    await hub.connect(websocket, tenant_id, is_admin=is_admin,
                      user_id=ws_user_id)
    try:
        unread = await runtime.event_repo.list_alerts(
            status="active", limit=10, tenant_id=tenant_id,
            user_id=ws_user_id)
        unread_count = await runtime.event_repo.count_unread(
            tenant_id, user_id=ws_user_id)
        await websocket.send_json({
            "type": "snapshot", "unread": unread_count,
            "data": [alert_to_public(a, is_admin=is_admin) for a in unread]})
        while True:
            await websocket.receive_text()  # 仅保活/探断连
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 任何WS异常都只做清理
        pass
    finally:
        hub.disconnect(websocket, tenant_id)
