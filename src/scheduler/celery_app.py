"""Celery应用与Beat调度（生产/分布式执行入口）。

beat_schedule由registry.JOB_REGISTRY单一事实源生成，禁止在此硬编码cron。
无Redis环境下无需导入本模块：API手动触发直接进程内执行jobs.execute_job。

运行（本机Windows需solo池）：
  uv run celery -A src.scheduler.celery_app.celery_app worker -B --pool=solo -l info
生产：redis作为broker，worker与beat可分进程部署。
"""

from __future__ import annotations

import asyncio
import functools
import logging

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

logger = logging.getLogger(__name__)

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


def _register_catalog_jobs_before_schedule() -> None:
    """★ 在生成 beat_schedule **之前**把 catalog 作业注册进 `JOB_REGISTRY`。

    ## 修的缺陷（2026-09-28，全量测试红灯暴露）

    `catalog_calendar` / `catalog_gap_drain` / 全部 `catalog_*` 采集作业由
    `catalog_jobs.install_catalog_jobs()` **函数调用时**写进 `JOB_REGISTRY`，
    而调用点原先只有一处：**API 进程的 lifespan**（`api/main.py`）。

    两个后果，第二个更严重：

      · 测试：`test_beat_schedule_built_from_registry` 遍历 `JOB_REGISTRY`、
        逐个断言 beat_schedule 里有它 —— 取决于"谁先跑"，会随测试顺序红/绿
        （`test_calendar_index.py` 特意快照并**恢复**了 `JOB_REGISTRY`，
        恢复之后注册就没了）；
      · **生产**：Celery 的 **worker / beat 进程根本不执行 FastAPI 的 lifespan**
        → 这些作业从未进入 beat_schedule → **投资日历同步、缺口补取、
        catalog 批量采集在真实调度里一次都不会被触发**。
        而且不报错：beat 只是没有这些条目，日志里看不出少了什么。

    修法（最小改动）：在 `celery_app` 导入期注册一次，再生成 beat_schedule。
    幂等（同名覆盖），且 `plan_jobs()` 只读配置与索引表，不联网。
    """
    try:
        from src.scheduler.catalog_jobs import install_catalog_jobs

        install_catalog_jobs()
    except Exception:  # noqa: BLE001 调度表生成不能因为索引库读不到而整个挂掉
        logger.warning(
            "catalog 作业注册失败（beat_schedule 将不含 catalog_* 作业）；"
            "调度仍可用，但这些作业**不会被触发**", exc_info=True)


_register_catalog_jobs_before_schedule()

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
    # ★ 第四条触发路径（前三条：`_tick()`、`main.py` 启动自检、`/scheduler/jobs/{name}/run`）
    #   —— 四条必须同判据，否则"按写权限裁掉更新作业"会被 Worker 一键绕过（CHG-0087）。
    #   Worker 只在与 Redis 一起用时才存在（`MOSS_SCHEDULER_BACKEND=celery`），
    #   但判据不该因为"当前没启用"就少一处 —— 那正是"设了但不生效"的成因。
    from src.scheduler.registry import job_deny_reason

    denied = job_deny_reason(job_name)
    if denied:
        logger.warning("Worker 跳过作业%s（非本实例职责）：%s", job_name, denied)
        return {"status": "skipped", "job_name": job_name, "trigger": trigger,
                "records_processed": 0, "reason": denied,
                "detail": "本实例没有该作业的写权限，未执行（不是失败）"}

    log = RunLog(f"{settings.scheduler_dir}/runs.jsonl")
    runtime = _get_runtime()
    record = asyncio.run(execute_job(runtime, job_name, log, trigger))

    # 失败按1/5/15分钟指数退避重试，最多3次（skipped为人工介入不重试）
    if record["status"] == "failed" and trigger == "schedule":
        attempt = self.request.retries
        if attempt < MAX_RETRIES:
            raise self.retry(countdown=RETRY_COUNTDOWNS[attempt])
    return record
