"""数据查询 / Trace / 报告路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from src.api.tasks import TaskStore
from src.core.config import get_settings
from src.infrastructure.repositories.audit_chain import ChainVerifier

router = APIRouter(prefix="/api/v1", tags=["data"])


def _store(request: Request) -> TaskStore:
    return request.app.state.store


@router.get("/data/macro")
async def query_macro(
    request: Request,
    indicator: str = Query(..., min_length=1),
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """查询已入库的宏观数据点（含溯源12字段）。"""
    from src.api.runtime import Runtime

    runtime: Runtime = request.app.state.runtime
    points = await runtime.repo.query_points(indicator, start_date, end_date)
    return {
        "indicator": indicator,
        "count": len(points),
        "data_points": [p.model_dump(mode="json") for p in points],
    }


@router.get("/trace/{trace_id}")
async def get_trace(trace_id: str, request: Request) -> dict:
    """推理路径查询：Agent输出 + LLM审计记录 + 哈希链头。"""
    record = _store(request).get(trace_id)
    if record is None:
        raise HTTPException(status_code=404, detail="trace not found")

    from src.api.runtime import Runtime

    runtime: Runtime = request.app.state.runtime
    audit_entries = [
        e for e in runtime.gateway.audit_log.read_all()
        if e.get("trace_id") == trace_id
    ]
    settings = get_settings()
    chain = ChainVerifier(f"{settings.llm_audit_dir}/audit_chain.jsonl").verify()
    return {
        "trace_id": trace_id,
        "status": record.status,
        "query": record.query,
        "agent_outputs": record.agent_outputs,
        "errors": record.errors,
        "llm_calls": audit_entries,
        "audit_chain": {"valid": chain["valid"], "head": chain["head"],
                        "records": chain["count"]},
    }


@router.post("/report/generate")
async def generate_report(request: Request, body: dict) -> dict:
    """返回已完成的最终报告（markdown）。"""
    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id required")
    record = _store(request).get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    if record.status != "completed" or not record.final_report:
        raise HTTPException(status_code=409, detail=f"report not ready, status={record.status}")
    return {
        "task_id": record.task_id,
        "format": "markdown",
        "report": record.final_report,
        "disclaimer": "以上信息来自互联网公开资料，仅供研究参考，不构成投资建议。"
                      "投资有风险，入市需谨慎，盈亏自负。",
    }
