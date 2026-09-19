"""关键路径专用线程池：别让"慢业务线程"把运维/健康接口挤死。

## 现场（2026-09-17 实测）

`/api/v1/health` 在服务启动后要 **144.5 秒**才返回 —— 而把 `build_data_health()`
单独拿出来量只要 **3.74 秒**（第二次 0.00s，它有 TTL 缓存）。差别只在"当时
线程池里有没有空位"。

原因：`asyncio.to_thread()` 用的是事件循环的**默认执行器**
（`ThreadPoolExecutor`，`max_workers = min(32, cpu+4)`）。而自选池首屏是
**26 只票并发**，每只票的分钟线/日线/板块/估值都在往同一个池子里丢任务；
热加载后的后台整表重算、以及盘后自动选股（几十只票）叠在一起，默认池被占满。
于是 `/health` 虽然自己只要 3.7 秒，却要**排队**等前面几十个几百毫秒~几秒的
业务任务 —— 用户看到的就是"重启后运维页一直转圈"。

## 做法

给"必须随时可用"的调用一条**独立车道**：自己的 `ThreadPoolExecutor`，
固定少量 worker。业务线程再多也占不到它。

放这里而不是各模块自己建：一张表看清哪些调用被划成关键路径，
也便于统一在关停时收掉（`shutdown_infra_executors()`）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

#: 关键路径线程数：这些任务都很短（缓存命中 0.00s、未命中 3~5s），
#: 4 个足够并发服务健康检查/运行指标/数据源状态三类请求，
#: 又小到不会把机器 CPU 抢光。
INFRA_WORKERS = max(2, int(os.environ.get("MOSS_INFRA_THREADS", "4")))

_INFRA_EXECUTOR: ThreadPoolExecutor | None = None


def infra_executor() -> ThreadPoolExecutor:
    """关键路径专用执行器（懒建、进程内单例）。"""
    global _INFRA_EXECUTOR
    if _INFRA_EXECUTOR is None:
        _INFRA_EXECUTOR = ThreadPoolExecutor(
            max_workers=INFRA_WORKERS, thread_name_prefix="moss-infra")
        logger.debug("关键路径线程池已创建（%d workers）", INFRA_WORKERS)
    return _INFRA_EXECUTOR


async def run_infra(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在关键路径线程池里跑同步函数（用法同 `asyncio.to_thread`）。"""
    loop = asyncio.get_running_loop()
    if kwargs:
        from functools import partial

        return await loop.run_in_executor(
            infra_executor(), partial(func, *args, **kwargs))
    return await loop.run_in_executor(infra_executor(), func, *args)


def shutdown_infra_executors() -> None:
    """关停时收掉线程池（幂等；不等待在飞任务，避免拖住进程退出）。"""
    global _INFRA_EXECUTOR
    if _INFRA_EXECUTOR is None:
        return
    try:
        _INFRA_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001 关停尽力而为
        logger.debug("关键路径线程池关停异常（忽略）", exc_info=True)
    finally:
        _INFRA_EXECUTOR = None


__all__ = [
    "INFRA_WORKERS",
    "infra_executor",
    "run_infra",
    "shutdown_infra_executors",
]
