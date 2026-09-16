"""进程内Cron调度器单测（不联网、不起真实事件循环等待）。"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from src.scheduler.service import CronScheduler, cron_due

# ---------------- cron表达式匹配 ----------------

@pytest.mark.parametrize(("cron", "dt", "expected"), [
    # 周一10:05 每5分钟盘中作业：分钟%5==0、小时在9-11或13-14、工作日
    ("*/5 9-11,13-14 * * 1-5", datetime(2026, 9, 14, 10, 5), True),   # 周一
    ("*/5 9-11,13-14 * * 1-5", datetime(2026, 9, 14, 10, 7), False),  # 非5的倍数
    ("*/5 9-11,13-14 * * 1-5", datetime(2026, 9, 14, 12, 5), False),  # 午休时段
    ("*/5 9-11,13-14 * * 1-5", datetime(2026, 9, 14, 14, 55), True),  # 尾盘
    ("*/5 9-11,13-14 * * 1-5", datetime(2026, 9, 19, 10, 5), False),  # 周六
    ("*/5 9-11,13-14 * * 1-5", datetime(2026, 9, 20, 10, 5), False),  # 周日
    # 工作日16:10日频
    ("10 16 * * 1-5", datetime(2026, 9, 14, 16, 10), True),
    ("10 16 * * 1-5", datetime(2026, 9, 14, 16, 11), False),
    # 每天09:30
    ("30 9 * * *", datetime(2026, 9, 19, 9, 30), True),  # 周六也触发
    # 每月1日03:00
    ("0 3 1 * *", datetime(2026, 10, 1, 3, 0), True),
    ("0 3 1 * *", datetime(2026, 10, 2, 3, 0), False),
])
def test_cron_due(cron, dt, expected):
    assert cron_due(cron, dt) is expected


# ---------------- 调度行为 ----------------

class _FakeRunLog:
    def __init__(self, paused: set[str] | None = None):
        self.paused = paused or set()
        self.appends: list[dict] = []

    def is_paused(self, name: str) -> bool:
        return name in self.paused


@pytest.mark.asyncio
async def test_tick_fires_due_job_once(monkeypatch):
    calls: list[str] = []

    async def fake_execute(runtime, name, run_log, trigger):
        calls.append((name, trigger))
        return {"status": "success", "records_processed": 1}

    log = _FakeRunLog()
    sched = CronScheduler(object(), log, execute_fn=fake_execute,
                          now_fn=lambda: datetime(2026, 9, 14, 16, 10))
    await sched._tick()  # 16:10 工作日 → market_daily_snapshot到期
    await asyncio_flush()
    fired = {c[0] for c in calls}
    assert "market_daily_snapshot" in fired
    assert all(c[1] == "schedule" for c in calls)
    # 同一分钟重复tick不重复触发
    await sched._tick()
    await asyncio_flush()
    assert sum(1 for c in calls if c[0] == "market_daily_snapshot") == 1


@pytest.mark.asyncio
async def test_tick_skips_paused_and_nondue():
    log = _FakeRunLog(paused={"market_daily_snapshot"})
    calls: list[str] = []

    async def fake_execute(runtime, name, run_log, trigger):
        calls.append(name)
        return {"status": "success", "records_processed": 0}

    sched = CronScheduler(object(), log, execute_fn=fake_execute,
                          now_fn=lambda: datetime(2026, 9, 14, 16, 10))
    await sched._tick()
    await asyncio_flush()
    assert "market_daily_snapshot" not in calls


@pytest.mark.asyncio
async def test_tick_skips_overlapping_run():
    log = _FakeRunLog()

    async def slow_execute(runtime, name, run_log, trigger):
        return {"status": "success", "records_processed": 0}

    sched = CronScheduler(object(), log, execute_fn=slow_execute,
                          now_fn=lambda: datetime(2026, 9, 14, 16, 10))
    # 手工置为运行中（模拟上一轮未结束），到期也应跳过
    sched._running.add("market_daily_snapshot")
    await sched._tick()
    await asyncio_flush()
    assert log.appends == []


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    sched = CronScheduler(object(), _FakeRunLog())
    await sched.start()
    assert isinstance(sched._task, asyncio.Task) and not sched._task.done()
    # 幂等start不重复建任务
    await sched.start()
    assert isinstance(sched._task, asyncio.Task)
    await sched.stop()
    assert sched._task is None
    # stop幂等
    await sched.stop()


async def asyncio_flush():
    """让出事件循环，使create_task创建的作业协程执行完毕。"""
    for _ in range(5):
        await asyncio.sleep(0)
