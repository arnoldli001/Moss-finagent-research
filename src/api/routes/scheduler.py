"""定时调度管理：作业清单/手动触发/运行记录/日报。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.scheduler.jobs import execute_job
from src.scheduler.registry import JOB_REGISTRY

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])


def _run_log(request: Request):
    return request.app.state.run_log


@router.get("/jobs")
async def list_jobs(request: Request) -> dict:
    log = _run_log(request)
    jobs = []
    for name, spec in JOB_REGISTRY.items():
        last = log.last_run(name)
        jobs.append({
            "name": name,
            "cron": spec.cron,
            "kind": spec.kind,
            "description": spec.description,
            "params": spec.params,
            "paused": log.is_paused(name),
            "last_run": (
                {"status": last["status"], "start_time": last["start_time"],
                 "duration_ms": last["duration_ms"],
                 "records_processed": last["records_processed"],
                 "error_message": last["error_message"]}
                if last else None
            ),
        })
    return {"jobs": jobs}


@router.post("/jobs/{name}/run", status_code=202)
async def trigger_job(name: str, request: Request) -> dict:
    """手动触发作业：API进程内直接执行（无需Redis/Worker），同样落运行记录。"""
    if name not in JOB_REGISTRY:
        raise HTTPException(status_code=404, detail=f"未注册作业: {name}")
    record = await execute_job(
        request.app.state.runtime, name, _run_log(request), trigger="manual"
    )
    return {"run": record}


@router.get("/runs")
async def list_runs(
    request: Request, job: str | None = None, limit: int = 100
) -> dict:
    limit = max(1, min(limit, 1000))
    return {"runs": _run_log(request).read_all(job, limit=limit)}


@router.get("/runs/summary")
async def runs_summary(request: Request, date: str | None = None) -> dict:
    return _run_log(request).daily_summary(date)
