"""本地模型并发闸：排队发生在应用侧，且超时不会被记成"调用失败"。

## 为什么需要（实测 2026-09-26，RTX 4060 8GB）

Ollama 日志里**只出现过 `slot id 0`**（`srv update_slots: all slots are idle`），
生成速度 ~44 t/s 且单请求就打满 GPU —— 也就是说同一个模型上的并发请求
**严格串行**。把并发请求直接丢给 Ollama，它们会挂在 HTTP 上等，而
`llm_timeout_seconds=120`："排队"最终表现为"Ollama 调用失败"。

## 这套断言要守住四件事

1. **并发上限真的生效**（默认 1，与单槽一致）：两个同时的调用不重叠；
2. **排队超时要能报出来**，而不是挂到 HTTP 超时；
3. **排队超时不许触发付费降级**（它不能是 `LLMGatewayError`）——
   `light`/`medium` 的备模型是付费的 deepseek-flash；
4. **可观测**：`stats()` 能回答"本地模型是不是在被抢"。
"""

from __future__ import annotations

import asyncio

import pytest

from src.infrastructure.llm.local_gate import (
    DEFAULT_LIMIT,
    ENV_LIMIT,
    ENV_QUEUE_TIMEOUT,
    LocalModelGate,
    LocalQueueTimeout,
    get_local_gate,
    reset_local_gate_for_test,
)


@pytest.fixture(autouse=True)
def _clean_gate(monkeypatch):
    for name in (ENV_LIMIT, ENV_QUEUE_TIMEOUT):
        monkeypatch.delenv(name, raising=False)
    reset_local_gate_for_test()
    yield
    reset_local_gate_for_test()


# ======================================================================
# 一、串行化
# ======================================================================

@pytest.mark.asyncio
async def test_default_limit_serializes_local_calls() -> None:
    """★ 默认上限 1：两个并发调用**不许重叠**（Ollama 只有一个槽位）。"""
    gate = LocalModelGate(limit=1)
    assert gate.limit == DEFAULT_LIMIT == 1
    order: list[str] = []

    async def job(tag: str, hold: float) -> float:
        async with gate.slot("qwen3:8b") as waited:
            order.append(f"{tag}:start")
            await asyncio.sleep(hold)
            order.append(f"{tag}:end")
            return waited

    waited = await asyncio.gather(job("a", 0.15), job("b", 0.05))
    # 严格串行 → 一定是 a 开始、a 结束、b 开始、b 结束
    assert order == ["a:start", "a:end", "b:start", "b:end"], order
    assert waited[0] == 0.0, "第一个不该等"
    assert waited[1] > 0.05, "第二个必须等到第一个释放"


@pytest.mark.asyncio
async def test_limit_can_be_raised_by_env(monkeypatch) -> None:
    monkeypatch.setenv(ENV_LIMIT, "2")
    gate = get_local_gate()
    assert gate.limit == 2
    peak = 0
    live = 0

    async def job() -> None:
        nonlocal peak, live
        async with gate.slot():
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.08)
            live -= 1

    await asyncio.gather(job(), job())
    assert peak == 2, "调大上限后应当真的能并行两个"


def test_bad_env_falls_back_to_default(monkeypatch) -> None:
    """写坏的环境变量不能把进程搞崩，也不能把护栏变成"无上限"。"""
    monkeypatch.setenv(ENV_LIMIT, "abc")
    assert get_local_gate().limit == DEFAULT_LIMIT
    reset_local_gate_for_test()
    monkeypatch.setenv(ENV_LIMIT, "0")
    assert get_local_gate().limit == 1, "0/负数按 1 处理（不能等于不限流）"


# ======================================================================
# 二、排队超时
# ======================================================================

@pytest.mark.asyncio
async def test_queue_timeout_raises_instead_of_hanging() -> None:
    gate = LocalModelGate(limit=1, queue_timeout=0.05)
    holder = asyncio.create_task(_hold(gate, 0.4))
    await asyncio.sleep(0.02)          # 让 holder 先拿到槽位
    with pytest.raises(LocalQueueTimeout) as exc:
        async with gate.slot("qwen3:8b"):
            pass
    assert "排队超过" in str(exc.value)
    await holder
    # 槽位必须被释放（不能因为超时把闸门锁死）
    async with gate.slot():
        pass


async def _hold(gate: LocalModelGate, seconds: float) -> None:
    async with gate.slot():
        await asyncio.sleep(seconds)


def test_queue_timeout_is_not_a_gateway_error() -> None:
    """★ 排队超时**不能**是 `LLMGatewayError`。

    否则网关会把它当"这一跳失败"而降级到**付费**的 deepseek-flash ——
    "本地在排队"就变成了"悄悄花钱"，正是这一层要防的事。
    """
    from src.core.exceptions import LLMGatewayError

    assert not issubclass(LocalQueueTimeout, LLMGatewayError)


# ======================================================================
# 三、可观测
# ======================================================================

@pytest.mark.asyncio
async def test_stats_report_queueing() -> None:
    """运维要能回答"本地模型是不是在被抢"。"""
    gate = LocalModelGate(limit=1)
    assert gate.stats()["waited_calls"] == 0

    async def job(hold: float) -> None:
        async with gate.slot("qwen3:8b"):
            await asyncio.sleep(hold)

    await asyncio.gather(job(0.12), job(0.02))
    stats = gate.stats()
    assert stats["limit"] == 1
    assert stats["waited_calls"] == 1, stats
    assert stats["max_wait_s"] > 0.05, stats
    assert stats["waiting"] == 0, "结束后不该还有等待计数"


# ======================================================================
# 四、接线：OllamaProvider 必须真的走这道闸
# ======================================================================

@pytest.mark.asyncio
async def test_ollama_provider_goes_through_the_gate(monkeypatch) -> None:
    """★ 闸门没接线 = 没做。

    用一个假 httpx 客户端统计"同时在飞的请求数"：上限 1 时必须恒为 1。
    """
    from src.infrastructure.llm import providers as P
    from src.infrastructure.llm.models import ModelSpec

    peak = 0
    live = 0

    class _FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"message": {"content": "ok"}, "prompt_eval_count": 1,
                    "eval_count": 1}

    class _FakeClient:
        async def post(self, url, json=None):  # noqa: A002
            nonlocal peak, live
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.1)
            live -= 1
            return _FakeResponse()

    monkeypatch.setenv(ENV_LIMIT, "1")
    reset_local_gate_for_test()
    provider = P.OllamaProvider()
    provider._client = _FakeClient()   # noqa: SLF001 测试替身
    spec = ModelSpec(name="local_medium", provider="ollama",
                     model_name="qwen3:8b-q4_K_M", base_url="http://x",
                     max_tokens=64, temperature=0.1)
    await asyncio.gather(
        provider.chat(spec, "s", "p"), provider.chat(spec, "s", "p"),
        provider.chat(spec, "s", "p"))
    assert peak == 1, f"三个并发调用同时进了 {peak} 个（闸门没拦住）"
    assert get_local_gate().stats()["waited_calls"] >= 1
