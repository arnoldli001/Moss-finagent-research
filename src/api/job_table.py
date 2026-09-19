"""进程内异步任务表的统一淘汰策略。

回测、因子统计等长请求都走「提交 → 轮询」模式，任务状态放在进程内 dict。
任务结束后必须按时间（TTL）和数量（上限）两个维度淘汰，否则内存只增不减。

此前每个路由各写一份 purge，而且常量同名不同值（都叫 `_JOB_TTL_SECONDS`，
一处 900s 一处 1800s）。同名不同义是维护陷阱：改动方以为在调全局策略，
实际只影响一个路由。这里把算法收成一份，把差异显式建模为 `JobRetention`，
每类任务必须写明自己的保留理由。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: 任务状态标记：运行中的任务不参与淘汰。
RUNNING = "running"


@dataclass(frozen=True)
class JobRetention:
    """一类任务的保留策略。"""

    ttl_seconds: float
    max_jobs: int
    reason: str


#: 回测结果含完整净值曲线，单体体积大，靠较短 TTL 控制内存。
BACKTEST_RETENTION = JobRetention(
    ttl_seconds=900.0,
    max_jobs=50,
    reason="回测产物含逐日净值曲线，体积大；15 分钟内完成取回即可",
)

#: 因子统计耗时长，用户提交后常隔一段时间才回看，TTL 放宽但压低并发条数。
QUANT_RETENTION = JobRetention(
    ttl_seconds=1800.0,
    max_jobs=20,
    reason="因子统计耗时数分钟，用户回看间隔长；放宽 TTL，用更小的条数上限兜内存",
)


def purge_jobs(jobs: dict[str, dict], retention: JobRetention) -> None:
    """就地淘汰已结束任务：先按 TTL 清过期，再按数量上限清最旧的。

    运行中的任务（``status == RUNNING``）永不淘汰。缺少 ``finished_at``
    的异常任务按「刚结束」处理，避免因缺字段被误删或永久滞留。
    """
    now = time.monotonic()
    for jid in [
        jid for jid, job in jobs.items()
        if job.get("status") != RUNNING
        and now - job.get("finished_at", now) > retention.ttl_seconds
    ]:
        jobs.pop(jid, None)

    finished = sorted(
        (job.get("finished_at", 0.0), jid)
        for jid, job in jobs.items()
        if job.get("status") != RUNNING
    )
    overflow = len(jobs) - retention.max_jobs
    for _, jid in finished[: max(overflow, 0)]:
        jobs.pop(jid, None)
