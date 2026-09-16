"""崩溃隔离子进程执行器：取消路径必须收掉子进程。

## 事故记录（2026-09-16）

板块快照的冷取改成"放后台拉"之后，`test_light_snapshot_skips_slow_subsystems`
（用真实 IntradayService 跑冷启动快照）在测试进程里起了**真实子进程**；
用例结束时 `asyncio.run` 取消该后台任务，而 `run_json_subprocess` **只处理了
TimeoutError，没有处理 CancelledError** —— 子进程被留在那里，它的 stdout/stderr
管道让事件循环在关闭时一直等，整个 pytest 卡在 53% 十几分钟。

生产上同样致命：服务关停 / 后台刷新被取消时会留下孤儿子进程，关停流程挂住。
"""

from __future__ import annotations

import asyncio
import time

from src.intraday.subproc import run_json_subprocess

_SLEEP_BODY = """
import time
time.sleep(30)
__emit({"ok": True})
"""


def test_cancellation_kills_child_process() -> None:
    """取消子进程调用后必须**立刻**返回，而不是等子进程自己睡完 30 秒。"""
    async def _run() -> bool:
        task = asyncio.create_task(run_json_subprocess(
            _SLEEP_BODY, timeout=60.0, label="取消测试"))
        await asyncio.sleep(0.4)          # 确保子进程已经起来
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task.cancelled()

    started = time.perf_counter()
    cancelled = asyncio.run(_run())
    elapsed = time.perf_counter() - started
    assert cancelled is True
    assert elapsed < 5.0, (
        f"取消后耗时 {elapsed:.1f}s —— 子进程没被 kill，事件循环在等它的管道")


def test_cancellation_is_raised_not_swallowed() -> None:
    """取消必须继续往上抛（否则 asyncio 的正常关停协议会被破坏）。"""
    async def _run() -> str:
        task = asyncio.create_task(run_json_subprocess(
            _SLEEP_BODY, timeout=60.0, label="取消测试2"))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(_run()) == "cancelled"


def test_normal_result_still_works() -> None:
    """取消分支不能把正常路径搞坏。"""
    async def _run() -> object:
        return await run_json_subprocess(
            '__emit({"value": 42})', timeout=30.0, label="正常路径")

    assert asyncio.run(_run()) == {"value": 42}
