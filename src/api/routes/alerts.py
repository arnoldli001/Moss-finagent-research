"""事件告警REST与WebSocket路由（FR-10/FR-12，全snake_case）。

- 列表/详情/已读/事件查询/手工导入/手动扫描(后台执行,2秒内返回)/阈值配置；
- /ws/alerts 实时推送，连上先推未读快照，离线不补发；
- 事件子系统未装配(SQLite外后端)时返回503降级标注。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from src.core.config import get_settings
from src.domain.alerts.dedup import dedupe_events
from src.domain.alerts.models import DEFAULT_TENANT, DISCLAIMER, ScanResult
from src.domain.alerts.normalize import normalize_events, now_iso
from src.domain.alerts.service import ScanInProgressError
from src.scheduler.registry import JOB_REGISTRY

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
        "schedule": {"job": "event_alert_daily",
                     "cron": spec.cron if spec else None},
        "disclaimer": DISCLAIMER,
    }


@router.get("/alerts/unread-count")
async def unread_count(request: Request, tenant_id: str = DEFAULT_TENANT) -> dict:
    runtime = _require_stack(request)
    return {"unread": await runtime.event_repo.count_unread(tenant_id)}


@router.post("/alerts/scan", status_code=202)
async def trigger_scan(request: Request) -> dict:
    """手动触发扫描：后台执行，2秒内返回accepted，前端轮询scan/latest。"""
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
                await runtime.event_service.run("manual")).model_dump()
        except ScanInProgressError as exc:
            # 与定时作业/调度器手动入口的共享锁冲突：跳过而非失败
            scan["result"] = ScanResult(
                trigger="manual", status="success",
                data_gaps=[f"扫描已在执行中，本次跳过：{str(exc)[:120]}"],
                started_at=scan["started_at"], finished_at=now_iso()).model_dump()
        except Exception as exc:  # noqa: BLE001 服务总异常也要让轮询可见
            logger.exception("手动事件扫描失败")
            scan["result"] = ScanResult(
                trigger="manual", status="failed",
                errors=[f"扫描异常: {str(exc)[:200]}"],
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
    return {"updated": await runtime.event_repo.mark_all_read(tenant_id)}


@router.get("/alerts/{alert_id}")
async def get_alert(
    request: Request, alert_id: str, tenant_id: str = DEFAULT_TENANT,
) -> dict:
    runtime = _require_stack(request)
    alert = await runtime.event_repo.get_alert(alert_id, tenant_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在或已过期")
    return {"alert": alert.model_dump()}


@router.post("/alerts/{alert_id}/read")
async def mark_read(
    request: Request, alert_id: str, tenant_id: str = DEFAULT_TENANT,
) -> dict:
    runtime = _require_stack(request)
    updated = await runtime.event_repo.mark_read(alert_id, tenant_id)
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
    alerts = await runtime.event_repo.list_alerts(
        alert_type=type, alert_level=level, status=status,
        limit=limit, include_expired=include_expired, tenant_id=tenant_id)
    unread = await runtime.event_repo.count_unread(tenant_id)
    return {"alerts": [a.model_dump() for a in alerts],
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
    return {"events": [e.model_dump() for e in events], "total": len(events)}


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
    runtime = websocket.app.state.runtime
    hub = runtime.alert_hub
    if hub is None or runtime.event_repo is None:
        await websocket.close(code=1013)  # 子系统不可用
        return
    await hub.connect(websocket, tenant_id)
    try:
        unread = await runtime.event_repo.list_alerts(
            status="active", limit=10, tenant_id=tenant_id)
        unread_count = await runtime.event_repo.count_unread(tenant_id)
        await websocket.send_json({
            "type": "snapshot", "unread": unread_count,
            "data": [a.model_dump() for a in unread]})
        while True:
            await websocket.receive_text()  # 仅保活/探断连
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 任何WS异常都只做清理
        pass
    finally:
        hub.disconnect(websocket, tenant_id)
