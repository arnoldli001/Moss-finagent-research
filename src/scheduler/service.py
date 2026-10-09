"""进程内Cron调度器（零依赖模式）。

生产/分布式环境使用 Celery Beat（见 celery_app.py，broker=Redis）；
本地SQLite时由FastAPI lifespan启动本服务，按JOB_REGISTRY的cron表达式
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

from src.scheduler.registry import (
    JOB_REGISTRY,
    SCHEDULER_DENY_ENV,
    schedulable_jobs,
    scheduler_scope_report,
)
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
            # ★ 启动横幅与 `_tick()` **同源**（`schedulable_jobs()`），
            #   所以"打印 24 个"与"真正遍历 24 个"不可能不一致。
            #
            # ⚠️ 但**别指望这几行 logger.info 被人看到**（`CHG-0087` 实测）：
            #   全仓库没有 `logging.basicConfig()` / `dictConfig()`，root logger
            #   没有 handler ⇒ INFO 被直接丢弃（last-resort handler 只兜 WARNING）。
            #   逐字节搜 `data/run/*.log` 里的「调度器已启动」= **0 处**。
            #   真正可见的面有两个，都已在本条改动里落实：
            #     · `manage.py` 启动横幅的 `_scheduler_scope_line()`（stderr，给人看）；
            #     · `/health` → `data_sources.schedule`（给机器/前端看，20 秒轮询）。
            #   保留 logger.info 是为了**测试与前台运行**（有人配了 logging 时可见）。
            scope = scheduler_scope_report()
            logger.info("进程内Cron调度器已启动（%d/%d个作业，环境=%s）",
                        scope["active"], scope["total"], scope["env"])
            for item in scope["pruned"]:
                logger.info("调度裁剪：跳过作业%s —— %s",
                            item["job"], item["reason"])
            if scope["unknown_denied"]:
                logger.warning(
                    "%s 里有拼错的作业名 %s（不会禁用任何东西），可用名字见 "
                    "JOB_REGISTRY",
                    SCHEDULER_DENY_ENV, "、".join(scope["unknown_denied"]))

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def trigger(self, name: str, *, source: str = "startup") -> dict[str, Any]:
        """立刻执行一个已注册作业（启动自检的补偿路径用），返回运行记录。

        ## 与定时触发的两处差别

        1. `trigger` 字段不同 —— 运行记录里能看出这次是启动自检补的，
           而不是 16:40 那班定时；
        2. **不受"连续失败自动暂停"拦截**。暂停的用意是防"每 30 分钟撞同一堵墙"，
           而启动自检是一次性的：重启本身可能已经消除了失败原因
           （比如 token 刚配好、磁盘刚腾出空间），这时应当再试一次。

        ⚠️ 并发安全靠 `execute_job` 内部的 `_lock_for(job_name)`：
        自检与定时撞上时后者会串行等待，不会双跑（下载/灌库都是幂等的，
        但串行能避免两份下载互相抢带宽）。

        ## ★ 也必须过写权限那一关（`CHG-0087`，堵一条**绕过 `_tick` 的路**）

        本函数是**不在 `_tick()` 里**的第二条触发路径（启动自检的补偿、
        事件告警的盘中补扫）。如果它不过同一道判据，就会出现：
        定时路径已按写权限裁掉了 `quant_data_sync`，而启动自检发现"行情有缺口"
        又把它**直接拉起来** → 撞写闸门 → 记一条 `failed`。
        运维看到的是故障，实际是"这台机器不负责写"。

        所以这里返回一条 `skipped` 记录（**不写 failed、不执行**），
        并在理由里说清为什么、去哪改 —— 与 `_tick()` **同源同判据**。

        ## ★★ 同一道门在 `CHG-0139` 又来了一次（**进程角色**）

        把 4 个重作业挪出在线进程时，第一次只改了 `_tick()` 走的
        `schedulable_jobs()` —— 于是**这条旁路原样绕过了整次拆分**：
        `_check_quant_sync_at_startup()` 正是用 `trigger("quant_data_sync")`
        补缺口的，它会在**在线 API 进程**里把那个重作业跑起来。
        症状与拆分前**一模一样**（前端又报不可达），而排查的人会以为
        "已经挪出去了"，于是往别处找。

        所以这里加的是**同一个** `job_out_of_role()`，而不是把角色判断再写一遍。
        教训（本条的第二次出现）：**新增一道"谁能跑"的判据时，必须把
        "所有触发路径"列出来逐条接上** —— 判据写在一条路径上，
        等于给另一条路径开了后门。
        """
        from src.scheduler.jobs import execute_job
        from src.scheduler.registry import job_deny_reason, job_out_of_role

        reason = job_deny_reason(name)
        if reason:
            logger.warning("作业%s被跳过（非本实例职责）：%s", name, reason)
            return {"status": "skipped", "job_name": name, "trigger": source,
                    "records_processed": 0, "reason": reason,
                    "detail": "本实例没有该作业的写权限，未执行（不是失败）"}
        role_reason = job_out_of_role(name)
        if role_reason:
            # 与上面同理：这是**分工**，不是故障 ⇒ `skipped`，理由指向谁来跑。
            logger.info("作业%s不在本进程角色内，交由对方进程执行：%s", name, role_reason)
            return {"status": "skipped", "job_name": name, "trigger": source,
                    "records_processed": 0, "reason": role_reason,
                    "detail": "本进程角色不含该作业，未执行（不是失败）"}

        fn = self._execute or execute_job
        self._running.add(name)
        try:
            return await fn(self._runtime, name, self._run_log, source)
        finally:
            self._running.discard(name)

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
        # ★ 遍历 `schedulable_jobs()` 而**不是** `JOB_REGISTRY`：更新作业在
        #   只读实例上不该被触发（理由与派生方式见 `registry.job_deny_reason`）。
        for name, spec in schedulable_jobs().items():
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
