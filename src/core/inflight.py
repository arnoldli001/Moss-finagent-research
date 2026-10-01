"""**在飞登记簿**：每个请求处理期间"谁正在事件循环上跑"（`CHG-0146`）。

## 它回答哪一个问题

`loop_lag` 已经能量"循环卡了多久"，但它**说不出是谁卡的**。而这个区分决定了修法：

| 观测 | 真相 | 动作 |
|---|---|---|
| 某接口 `latency_ms` 很大、但**卡顿期间它不在飞** | **受害者**（排队） | 别改它 |
| 某接口 `latency_ms` 很大、且**卡顿期间它正在飞** | **嫌疑人** | 去读它的代码 |

实测教训（2026-09-30）：`/research/{task_id}` 的 p50 是 **8.3 秒**、184 次，
看着像头号犯人 —— 读代码发现它只是**内存字典查表**（`TaskStore.get`），
本身微秒级；它之所以"慢"，是因为它是研究任务期间**前端轮询最频繁的那个**，
于是它总是排在队列里，把**别人的阻塞**记在了自己账上。
⇒ **按 `latency_ms` 排序会把人带偏**，必须区分"排队"与"占着循环"。

## 为什么用"登记簿"而不是逐请求计时的中间件

中间件只能给出"这个请求自己花了多久"，仍然分不清它是在**算**还是在**等**。
登记簿换个角度：把"此刻正在执行的处理器"记下来，**在循环卡顿的那一刻**去读它 ——
卡顿时刻仍在飞的那些请求，就是占着循环的人（一个或多个）。

## 代价与边界（诚实登记）

* 只在**异步依赖**里登记：`O(1)` 两次字典写，无锁、无 IO、不进线程池；
* 用 `asyncio.current_task()` 当键 ⇒ **同一任务重入**（嵌套调用）只记一次最外层；
* 它只覆盖**被登记的路径**（本仓库是在汇总 router 上挂一个全局依赖 ⇒ 全部 API 都登记）；
* 卡顿时"在飞"的可能**不止一个**（并发请求共享同一次阻塞），所以它是**嫌疑人名单**
  而不是判决书 —— 真正的判决仍要看代码里有没有同步重活。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

#: task → (标签, 进入时刻 monotonic)
_INFLIGHT: dict[Any, tuple[str, float]] = {}

#: 只保留最近这么多个"在飞"样本，避免日志里出现几百行
MAX_REPORT = 6


def enter(label: str) -> None:
    """登记"本任务正在处理 `label`"。幂等：已在册则只更新时刻不动标签。"""
    try:
        task = asyncio.current_task()
    except RuntimeError:      # 无事件循环（同步上下文/测试）
        return
    if task is None:
        return
    _INFLIGHT[task] = (label, time.monotonic())


def leave() -> None:
    """注销本任务（请求结束时调用）。"""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return
    if task is not None:
        _INFLIGHT.pop(task, None)


def snapshot(*, now: float | None = None, limit: int = MAX_REPORT) -> list[dict[str, Any]]:
    """此刻在飞的处理器（按已持续时间降序）—— 卡顿时刻读它。"""
    t = time.monotonic() if now is None else now
    rows = [
        {"label": label, "elapsed_ms": round((t - started) * 1000.0, 1)}
        for label, started in _INFLIGHT.values()
    ]
    rows.sort(key=lambda r: -r["elapsed_ms"])
    return rows[:limit]


def describe(*, now: float | None = None, limit: int = MAX_REPORT) -> str:
    """一行摘要（给日志用）；空时返回空串 —— **"没在飞"也是一个结论**。"""
    rows = snapshot(now=now, limit=limit)
    if not rows:
        return ""
    return "、".join(f"{r['label']}({r['elapsed_ms']:.0f}ms)" for r in rows)


def inflight_count() -> int:
    return len(_INFLIGHT)


def reset() -> None:
    """清空（测试用；生产不需要 —— 任务结束时自己注销）。"""
    _INFLIGHT.clear()
