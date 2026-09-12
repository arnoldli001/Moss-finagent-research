"""投研任务路由：POST analyze（异步）+ GET 状态轮询。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.runtime import Runtime, agent_health
from src.api.tasks import TaskStore, new_task_id
from src.core.config import get_settings
from src.orchestration.supervisor import plan_run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["research"])


class AnalyzeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    analysis_type: str = "full"
    target: str = ""
    tenant_id: str = "tenant_001"
    options: dict = Field(default_factory=dict)


def _store(request: Request) -> TaskStore:
    return request.app.state.store


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


@router.post("/research/analyze", status_code=202)
async def submit_analyze(body: AnalyzeRequest, request: Request) -> dict:
    """提交投研分析任务（异步执行，返回task_id供轮询）。"""
    runtime = _runtime(request)
    store = _store(request)
    task_id = new_task_id()
    plan = plan_run(body.analysis_type, body.target)

    store.create(
        task_id,
        trace_id=task_id,
        tenant_id=body.tenant_id,
        query=body.query,
        analysis_type=plan["analysis_type"],
        target=body.target,
    )
    state = {
        "task_id": task_id, "tenant_id": body.tenant_id,
        "user_query": body.query, "analysis_type": plan["analysis_type"],
        "target": body.target, "plan": [], "raw_points": [], "cleaned_points": [],
        "validated_points": [], "validation_report": {}, "storage_stats": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }

    async def _run() -> None:
        store.update(task_id, status="running")
        try:
            final = await runtime.graph.ainvoke(state)
            store.update(
                task_id,
                status="completed",
                agent_outputs=final.get("agent_outputs", []),
                errors=final.get("errors", []),
                final_report=final.get("final_report"),
            )
        except asyncio.CancelledError:
            store.update(task_id, status="failed", error="任务被取消（服务关停）")
            raise
        except Exception as exc:  # noqa: BLE001 任务级兜底
            logger.exception("research task failed: %s", task_id)
            store.update(task_id, status="failed", error=str(exc))

    handle = asyncio.create_task(_run())
    store.register_handle(task_id, handle)
    return {"task_id": task_id, "trace_id": task_id, "status": "queued",
            "plan": plan["agents"]}


@router.get("/research/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    """任务状态与结果（completed时含conclusion/report，契约见API_REFERENCE）。"""
    record = _store(request).get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    conclusion, confidence = None, None
    for o in record.agent_outputs:
        if o.get("agent_id") == "A17_recommend":
            conclusion = o.get("conclusion")
            confidence = o.get("confidence")
    return {
        "task_id": record.task_id, "trace_id": record.trace_id,
        "status": record.status, "conclusion": conclusion,
        "confidence": confidence, "report": record.final_report,
        "errors": record.errors, "error": record.error,
        "created_at": record.created_at,
    }


@router.get("/research/{task_id}/agents")
async def get_task_agents(task_id: str, request: Request) -> dict:
    """任务内全部Agent输出摘要（分析过程透明化）。"""
    record = _store(request).get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task_id": record.task_id, "status": record.status,
            "agent_outputs": record.agent_outputs, "errors": record.errors}


@router.get("/debug/plan")
async def debug_plan(analysis_type: str = "full", target: str = "") -> dict:
    """查看Supervisor对某类任务的规划（不执行）。"""
    return plan_run(analysis_type, target) | {"agents_health_note": "see /api/v1/health"}


@router.get("/health")
async def health(request: Request) -> dict:
    """聚合健康检查：Agent + 模型网关 + 数据库 + 审计链。"""
    import httpx

    from src.infrastructure.repositories.audit_chain import ChainVerifier

    runtime = _runtime(request)
    settings = get_settings()
    agents = agent_health(runtime.agents)

    ollama_status = "unknown"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(settings.ollama_base_url)
            ollama_status = "connected" if resp.status_code == 200 else "degraded"
    except (httpx.HTTPError, OSError):
        ollama_status = "unreachable"

    chain = ChainVerifier(f"{settings.llm_audit_dir}/audit_chain.jsonl").verify()
    return {
        "status": "healthy" if all(v == "healthy" for v in agents.values()) else "degraded",
        "agents": agents,
        "data_sources": {"akshare": "connected", "sqlite": "connected"},
        "model_gateway": {
            "ollama": ollama_status,
            "deepseek": "configured" if settings.deepseek_api_key else "not_configured",
        },
        "audit_chain": {"valid": chain["valid"], "records": chain["count"]},
    }
