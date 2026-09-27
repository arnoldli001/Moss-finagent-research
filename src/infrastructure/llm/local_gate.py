"""本地模型的**应用侧并发闸**：排队发生在应用里，而不是在 Ollama 里。

## 为什么需要它（2026-09-26 实测）

本机是 **RTX 4060 / 8 GB 显存 + 32 GB 内存**，Ollama 实际只有**一个计算槽位**：

    $ ollama server.log
    slot launch_slot_: id 0 | task 47744 | processing task
    ...
    srv  update_slots: all slots are idle       ← 全程只出现过 id 0
    slot print_timing: tg = 44.2 t/s            ← 单请求就吃满 GPU

也就是说：**同一个模型上的并发请求是严格串行的**（第二个请求在 Ollama 内部排队）。
在没有这层闸之前，多出来的请求会一直挂在 HTTP 上等 —— 而
`settings.llm_timeout_seconds` 是 **120 秒**，于是"排队排到 120 秒"会表现成
`Ollama 调用失败`（随后要么降级云端花钱、要么整条链路退化）。
实测踩过的形态：intel 抽取作业每 2 小时跑 40 条、单条 ~26 秒，
期间用户请求与它抢同一个槽位。

把等待挪到应用侧有三个好处：

1. **超时只计"生成时间"**，不再把排队时间算进去 —— 排队不再伪装成失败；
2. **可以观测**：`stats()` 给出等待次数与最长等待，运维页能看出"本地模型在被抢"；
3. **可以限流**：批量作业不该开 8~10 个并发去打一个单槽模型
   （`src/domain/intel/tone_job.py` 的 `CONCURRENCY = 1` 就是这个结论，
   现在把它变成**全局**约束而不是某个作业的自觉）。

## 为什么默认是 1

`MOSS_LOCAL_LLM_CONCURRENCY` 默认 **1**：与实测的单槽一致。调大**不会**提高
吞吐（GPU 已经是瓶颈，44 t/s 是它的上限），只会让多个请求分时切片、
**每个都变慢**，并且各自多吃一份 KV cache（8 GB 显存里 qwen3:8b 常驻已占
5.2 GB，余量 1.7 GB）。要提吞吐只能换更强的卡或更小的模型，不是调这个数。

## 与"多模型共存"的关系

闸只管**并发**，不管**显存**。显存那侧有一条硬规则（实测 Ollama 日志）：

    available="1.7 GiB" → 新模型 predicted="4.6 GiB" → evicting（换出常驻模型）
    → 重新 load_tensors

即 8 GB 卡上 **只能常驻一个 7~8B 模型**；再要一个 4~5 GB 的模型就会
"换出→加载"来回抖（每次几秒到几十秒，且 KV 缓存清零、prompt 要重算）。
所以选型见 `configs/models.yaml` 顶部的「本地模型与显存」一节：
**换模型，不要叠模型**。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

#: 同时在飞的本地调用数上限。默认 1（与 Ollama 单槽一致）。
ENV_LIMIT = "MOSS_LOCAL_LLM_CONCURRENCY"
#: 排队等待上限（秒）。超过就报错让调用方降级，而不是挂到 HTTP 超时。
ENV_QUEUE_TIMEOUT = "MOSS_LOCAL_LLM_QUEUE_TIMEOUT"

DEFAULT_LIMIT = 1
DEFAULT_QUEUE_TIMEOUT = 90.0


class LocalQueueTimeout(RuntimeError):
    """排队超时（本地模型被别的调用占着）。

    ⚠️ **刻意不继承 `LLMGatewayError`**：网关把 `LLMGatewayError` 当作
    "这一跳失败、降级到备模型"，而 `light` / `medium` 两层的备模型是
    **付费的 deepseek-flash** —— 那正好是我们要避免的事（"本地在排队"
    不该变成"悄悄花钱"）。所以它按普通异常抛出、由调用方按"本地不可用"
    降级；要花云端的钱必须显式允许（`local_only=False` + 显式切层）。
    """


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按 %d 处理", name, raw, default)
        return default
    return max(1, value)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，按 %.0f 秒处理", name, raw, default)
        return default
    return max(0.0, value)


class LocalModelGate:
    """本地（Ollama）调用的并发闸 + 排队统计。"""

    def __init__(self, *, limit: int = DEFAULT_LIMIT,
                 queue_timeout: float = DEFAULT_QUEUE_TIMEOUT) -> None:
        self._limit = max(1, int(limit))
        self._queue_timeout = max(0.0, float(queue_timeout))
        self._sem = asyncio.Semaphore(self._limit)
        self._waiting = 0
        self._waited_calls = 0
        self._max_wait = 0.0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def queue_timeout(self) -> float:
        return self._queue_timeout

    @contextlib.asynccontextmanager
    async def slot(self, model: str = "") -> AsyncIterator[float]:
        """占一个槽位；退出时释放。产出**等待时长**（秒）。

        等待超过 `queue_timeout` 抛 `LocalQueueTimeout` —— 宁可让调用方
        按"本地模型不可用"降级，也不要挂在那里等 HTTP 超时（那会把
        "在排队"记成"调用失败"，排查方向完全错）。
        """
        began = time.monotonic()
        queued = self._sem.locked()
        if queued:
            self._waiting += 1
        try:
            if queued:
                try:
                    await asyncio.wait_for(self._sem.acquire(),
                                           timeout=self._queue_timeout)
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    raise LocalQueueTimeout(
                        f"本地模型排队超过 {self._queue_timeout:.0f} 秒"
                        f"（并发上限 {self._limit}，模型 {model or '未知'}）"
                        f"—— 调用方应按本地不可用降级") from exc
            else:
                await self._sem.acquire()
        finally:
            if queued:
                self._waiting -= 1
        waited = time.monotonic() - began
        if waited > 0.05:
            self._waited_calls += 1
            self._max_wait = max(self._max_wait, waited)
            logger.info("本地模型排队 %.1fs 后开始（%s，并发上限 %d）",
                        waited, model or "未知", self._limit)
        try:
            yield waited
        finally:
            self._sem.release()

    def stats(self) -> dict[str, float | int]:
        """排队观测（供运维页/日志）：等待中的调用、等待过的调用、最长等待。"""
        return {
            "limit": self._limit,
            "queue_timeout_s": self._queue_timeout,
            "waiting": self._waiting,
            "waited_calls": self._waited_calls,
            "max_wait_s": round(self._max_wait, 2),
        }


_GATE: LocalModelGate | None = None


def get_local_gate() -> LocalModelGate:
    """进程内单例（读环境变量）。"""
    global _GATE
    if _GATE is None:
        _GATE = LocalModelGate(
            limit=_int_env(ENV_LIMIT, DEFAULT_LIMIT),
            queue_timeout=_float_env(ENV_QUEUE_TIMEOUT, DEFAULT_QUEUE_TIMEOUT))
        logger.info("本地模型并发闸：上限 %d，排队超时 %.0f 秒（%s / %s）",
                    _GATE.limit, _GATE.queue_timeout, ENV_LIMIT,
                    ENV_QUEUE_TIMEOUT)
    return _GATE


def reset_local_gate_for_test() -> None:
    """清掉单例（**仅测试用**）。"""
    global _GATE
    _GATE = None


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_QUEUE_TIMEOUT",
    "ENV_LIMIT",
    "ENV_QUEUE_TIMEOUT",
    "LocalModelGate",
    "LocalQueueTimeout",
    "get_local_gate",
    "reset_local_gate_for_test",
]
