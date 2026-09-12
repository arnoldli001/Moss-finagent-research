"""定时作业执行器：作业函数同时服务Celery Beat（分布式）与API手动触发（进程内）。

幂等性：快照作业复用研究图，A04存储按(指标,期别,内容哈希)去重，重复执行零新增；
进程内对同作业加asyncio锁，防止Beat/手动触发重叠执行。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from src.core.config import get_settings
from src.scheduler.registry import JobSpec, get_job
from src.scheduler.run_log import RunLog

_job_locks: dict[str, asyncio.Lock] = {}


def _lock_for(job_name: str) -> asyncio.Lock:
    if job_name not in _job_locks:
        _job_locks[job_name] = asyncio.Lock()
    return _job_locks[job_name]


def build_initial_state(
    analysis_type: str, target: str, query: str
) -> dict[str, Any]:
    task_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    return {
        "task_id": task_id, "tenant_id": "tenant_001",
        "user_query": query, "analysis_type": analysis_type,
        "target": target, "plan": [], "raw_points": [], "cleaned_points": [],
        "validated_points": [], "validation_report": {}, "storage_stats": {},
        "info_items": [], "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }


async def _graph_snapshot(runtime: Any, spec: JobSpec) -> tuple[int, list[str]]:
    """跑一次或多次研究图，返回(处理数据点数, 错误列表)。"""
    params = spec.params
    targets = params.get("targets") or [params.get("target", "")]
    total_points, errors = 0, []
    for target in targets:
        query = f"[定时]{spec.description}：{target}" if target else f"[定时]{spec.description}"
        state = build_initial_state(params["analysis_type"], target, query)
        final = await runtime.graph.ainvoke(state)
        stats = final.get("storage_stats") or {}
        total_points += int(stats.get("total", len(final.get("raw_points", []))))
        errors.extend(final.get("errors", []))
    return total_points, errors


async def execute_job(
    runtime: Any, job_name: str, run_log: RunLog, trigger: str = "schedule"
) -> dict[str, Any]:
    """执行一个注册作业并落运行记录；任何异常记为failed（不向调用方抛出）。"""
    spec = get_job(job_name)
    if trigger == "schedule" and run_log.is_paused(job_name):
        record = run_log.start(job_name, trigger)
        return run_log.finish(
            record, status="skipped",
            error_message="连续失败已达阈值，作业暂停等待人工介入",
        )

    async with _lock_for(job_name):
        record = run_log.start(job_name, trigger)
        try:
            if spec.kind == "graph_snapshot":
                processed, errors = await _graph_snapshot(runtime, spec)
                if errors:
                    return run_log.finish(
                        record, status="failed", records_processed=processed,
                        error_message="; ".join(errors)[:500])
                return run_log.finish(
                    record, status="success", records_processed=processed)
            if spec.kind == "run_log_cleanup":
                removed = run_log.prune(get_settings().scheduler_run_log_ttl_days)
                return run_log.finish(
                    record, status="success", records_processed=removed)
        except Exception as exc:  # noqa: BLE001 作业级兜底：失败入记录供告警/重试
            return run_log.finish(record, status="failed", error_message=str(exc)[:500])

        return run_log.finish(
            record, status="failed", error_message=f"未知作业类型 {spec.kind}")
