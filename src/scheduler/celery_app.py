"""Celery应用与Beat调度（生产/分布式执行入口）。

beat_schedule由registry.JOB_REGISTRY单一事实源生成，禁止在此硬编码cron。
无Redis环境下无需导入本模块：API手动触发直接进程内执行jobs.execute_job。

运行（本机Windows演示需solo池）：
  uv run celery -A src.scheduler.celery_app.celery_app worker -B --pool=solo -l info
生产：redis作为broker，worker与beat可分进程部署。
"""

from __future__ import annotations

import asyncio
import functools

from celery import Celery
from celery.schedules import crontab

from src.core.config import get_settings
from src.scheduler.jobs import execute_job
from src.scheduler.registry import (
    JOB_REGISTRY,
    MAX_RETRIES,
    RETRY_COUNTDOWNS,
)
from src.scheduler.run_log import RunLog

settings = get_settings()

celery_app = Celery(
    "moss_finagent_scheduler",
    broker=settings.celery_broker_url,
    backend=settings.celery_broker_url,
)
celery_app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    task_soft_time_limit=900,   # 显式超时（设计文档三），15分钟软上限
    task_time_limit=930,
)


def _parse_cron(expr: str) -> crontab:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron必须为5字段: {expr}")
    minute, hour, dom, month, dow = parts
    return crontab(minute=minute, hour=hour, day_of_month=dom,
                   month_of_year=month, day_of_week=dow)


celery_app.conf.beat_schedule = {
    name: {
        "task": "moss_finagent.scheduler.dispatch",
        "schedule": _parse_cron(spec.cron),
        "args": (name,),
    }
    for name, spec in JOB_REGISTRY.items()
}


@functools.lru_cache(maxsize=1)
def _get_runtime():
    """Worker进程内复用一个Runtime（LLM网关连接池/仓储单例）。"""
    from src.api.runtime import build_runtime

    return build_runtime()


@celery_app.task(name="moss_finagent.scheduler.dispatch", bind=True)
def dispatch_job(self, job_name: str, trigger: str = "schedule") -> dict:
    log = RunLog(f"{settings.scheduler_dir}/runs.jsonl")
    runtime = _get_runtime()
    record = asyncio.run(execute_job(runtime, job_name, log, trigger))

    # 失败按1/5/15分钟指数退避重试，最多3次（skipped为人工介入不重试）
    if record["status"] == "failed" and trigger == "schedule":
        attempt = self.request.retries
        if attempt < MAX_RETRIES:
            raise self.retry(countdown=RETRY_COUNTDOWNS[attempt])
    return record
