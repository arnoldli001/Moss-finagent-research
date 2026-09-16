"""进程内Cron调度器（零依赖演示模式）。

生产/分布式环境使用 Celery Beat（见 celery_app.py，broker=Redis）；
本地SQLite演示时由FastAPI lifespan启动本服务，按JOB_REGISTRY的cron表达式
在API进程内直接执行 jobs.execute_job，同样落运行记录与重试/暂停判定。

特性：
- 5字段cron：支持 *、*/n、a-b、逗号列表（周字段1=周一…7/0=周日）；
- 同一作业上一轮未结束时跳过本轮（禁止重叠执行）；
- RunLog判定连续失败自动暂停的作业跳过；
- 整点分钟边界对齐触发，停止时优雅取消。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from src.scheduler.registry import JOB_REGISTRY
from src.scheduler.run_log import RunLog

logger = logging.getLogger(__name__)

ExecuteFn = Callable[[Any, str, RunLog, str], Awaitable[dict[str, Any]]]


def _match_field(expr: str, value: int) -> bool:
    """单字段匹配：* / */n / a-b / 逗号列表 / 纯数字。"""
    for part in expr.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            if value % int(part[2:]) == 0:
                return True
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if lo <= value <= hi:
                return True
            continue
        if int(part) == value:
            return True
    return False


def cron_due(expr: str, now: datetime) -> bool:
    """标准5字段cron（分 时 日 月 周）在给定时刻是否到期。"""
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # Python weekday(): 周一=0…周日=6 → cron: 周一=1…周日=0/7
    cron_dow = 0 if now.weekday() == 6 else now.weekday() + 1
    return (
        _match_field(minute, now.minute)
        and _match_field(hour, now.hour)
        and _match_field(month, now.month)
        and _match_field(dom, now.day)
        and _match_field(dow, cron_dow)
    )


class CronScheduler:
    """按注册表在进程内定时执行作业。"""

    def __init__(
        self,
        runtime: Any,
        run_log: RunLog,
        *,
        execute_fn: ExecuteFn | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._runtime = runtime
        self._run_log = run_log
        self._execute = execute_fn  # 延迟导入默认值，测试可注入假执行器
        self._sleep = sleep_fn
        self._now = now_fn
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._running: set[str] = set()  # 正在执行的作业（防重叠）
        self._fired: set[tuple[str, str]] = set()  # 已触发过的(作业,分钟)

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="cron-scheduler")
            logger.info("进程内Cron调度器已启动（%d个作业）", len(JOB_REGISTRY))

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # 先对齐到下一分钟边界后2秒，降低分钟内抖动导致的漏触发
        while not self._stop.is_set():
            now = self._now()
            wait_sec = 60 - now.second + 2
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_sec)
                return  # 收到停止信号
            except asyncio.TimeoutError:
                pass
            await self._tick()

    async def _tick(self) -> None:
        now = self._now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        for name, spec in JOB_REGISTRY.items():
            key = (name, minute_key)
            if key in self._fired:
                continue
            if not cron_due(spec.cron, now):
                continue
            self._fired.add(key)
            if name in self._running:
                logger.warning("作业%s上一轮未结束，跳过本轮", name)
                continue
            if self._run_log.is_paused(name):
                logger.info("作业%s已被自动暂停（连续失败），跳过", name)
                continue
            asyncio.create_task(self._run_one(name))

    async def _run_one(self, name: str) -> None:
        from src.scheduler.jobs import execute_job

        fn = self._execute or execute_job
        self._running.add(name)
        try:
            record = await fn(self._runtime, name, self._run_log, "schedule")
            logger.info("定时作业%s完成: %s（%s条）", name,
                        record.get("status"), record.get("records_processed"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 单作业异常不影响调度循环
            logger.exception("定时作业%s执行异常: %s", name, exc)
        finally:
            self._running.discard(name)
