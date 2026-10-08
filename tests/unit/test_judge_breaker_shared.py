"""判定客户端的熔断 —— **两个客户端必须共用同一份策略**（`CHG-0206`）。

## 这个文件是为一次**实测出来的退化**写的

`CHG-0203` 给 `RerankClient` 加了熔断，但**没给 `EmbeddingClient` 加**，
并且在 docstring 里把这个缺口写成"这一条是 `embedding.py` 没有的"。
那不是设计取舍，是漏了 —— 代价可以量（`scripts/_probe_cache_latency.py`，
端点指向 RFC 5737 的 `192.0.2.1`，**保证不可路由**）：

    第 1 次查找  4552 ms   （= embed 超时 1.5 s + rerank 超时 3.0 s）
    第 2 次查找  4577 ms
    第 3 次查找  4565 ms   ⇒ rerank 达到阈值，熔断
    第 4 次查找  1544 ms   ⇒ rerank 不再出网，**但 embedding 每次都重付 1.5 s**

⇒ 一次投研分析有 4~15 次缓存查找 ⇒ 最坏 **18.3 ~ 68.7 s** 只花在缓存上；
而加 rerank **之前**同一场景只有 embedding ⇒ **6 ~ 23 s**。
**只给一侧加熔断 = 把最坏情况放大 3 倍，且 embedding 侧永不恢复。**

## 所以这个文件测的不是"熔断能用"，是"**只有一份熔断**"

`test_strategy_has_exactly_one_source` 与 `test_two_clients_do_not_share_a_bucket`
是两条方向相反、缺一不可的约束：

    · 策略**必须**同源（阈值/冷却/窗口/恢复条件 → `judge_breaker()` 一处）
    · 状态**必须**分开（rerank 挂了不能让兜底的 embedding 也挂）
"""
from __future__ import annotations

import httpx
import pytest

from src.infrastructure.llm import rerank as rr_mod
from src.infrastructure.llm.circuit_breaker import (
    JUDGE_COOLDOWN_SEC,
    JUDGE_FAILURES_TO_OPEN,
    TimeWindowCircuitBreaker,
    judge_breaker,
)
from src.infrastructure.llm.embedding import EmbeddingClient
from src.infrastructure.llm.rerank import RerankClient

#: `judge_breaker` 是每次调用**新建**一个实例 —— 若改成返回全局单例，
#: 两个客户端就会共用一个桶，`test_two_clients_do_not_share_a_bucket` 会红。
_THRESHOLD = JUDGE_FAILURES_TO_OPEN


def _embed_client(**kw) -> EmbeddingClient:
    kw.setdefault("base_url", "https://example.invalid/v1")
    kw.setdefault("api_key", "sk-test")
    return EmbeddingClient(**kw)


def _rerank_client(**kw) -> RerankClient:
    kw.setdefault("base_url", "https://example.invalid/v1")
    kw.setdefault("api_key", "sk-test")
    return RerankClient(**kw)


def _fake_http(monkeypatch: pytest.MonkeyPatch, *, behaviour: dict):
    """把 `httpx.AsyncClient` 换成可控替身。`behaviour["fail"]` 决定这次成不成功。

    同时被两个客户端用到（它们都走 `httpx.AsyncClient(timeout=...)`
    + `async with`），所以一个替身就够 —— 这本身也是"同一份实现"的体现。
    """
    calls = {"n": 0}

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json():
            # 同时满足 `/embeddings` 与 `/rerank` 两条解析路径：
            # 哪个客户端来取都会拿到结构正确的载荷。
            return {
                "data": [{"embedding": [0.1, 0.2, 0.3]}],
                "results": [{"index": 0, "relevance_score": 0.9}],
            }

    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            calls["n"] += 1
            if behaviour["fail"]:
                raise httpx.ReadTimeout("black hole")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


@pytest.mark.asyncio
async def test_embed_breaker_opens_and_stops_going_out(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 本条就是那个退化：embedding 连续失败到阈值后**必须不再出网**。"""
    calls = _fake_http(monkeypatch, behaviour={"fail": True})
    c = _embed_client()

    for _ in range(_THRESHOLD):
        assert await c.embed("q") is None
    assert calls["n"] == _THRESHOLD
    assert c.circuit_open is True, (
        "embedding 失败到阈值了却没熔断 —— 端点黑洞时每次查找都要白等 "
        "一整个超时，且永不恢复")

    # 熔断生效：**不再出网**
    assert await c.embed("q") is None
    assert calls["n"] == _THRESHOLD, "熔断期间仍然出网了 —— 那等于没有熔断"
    assert c.stats()["circuit_open_skips"] == 1, "跳过了却没记账"


@pytest.mark.asyncio
async def test_embed_success_resets_the_failure_streak(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """与 `RerankClient` 对称：成功清零 ⇒ 抖动不累积成假熔断。"""
    behaviour = {"fail": True}
    _fake_http(monkeypatch, behaviour=behaviour)
    c = _embed_client()

    for _ in range(_THRESHOLD - 1):
        assert await c.embed("q") is None
    behaviour["fail"] = False
    assert await c.embed("q") == [0.1, 0.2, 0.3]      # 成功 ⇒ 清零
    behaviour["fail"] = True
    for _ in range(_THRESHOLD - 1):
        assert await c.embed("q") is None
    assert c.circuit_open is False, (
        "成功没有清零连续失败数 —— 假熔断的症状是'语义层莫名不工作'")


@pytest.mark.asyncio
async def test_two_clients_do_not_share_a_bucket(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ **状态必须分开**：rerank 熔断不能牵连 embedding。

    合桶的话，`_recall_and_judge` 的 fail-open 链（rerank 失败 ⇒ 退回
    embedding 阈值）会**整条同时失效** —— 而那正是设计里唯一的兜底。
    3-gram 兜底的质量实测差得多（召回@12 只有 40%，`CHG-0199`）。
    """
    behaviour = {"fail": True}
    calls = _fake_http(monkeypatch, behaviour=behaviour)
    rr = _rerank_client()
    em = _embed_client()
    assert rr._cb is not em._cb          # noqa: SLF001 刻意的白盒断言
    assert rr._cb.name != em._cb.name    # noqa: SLF001

    for _ in range(_THRESHOLD):
        assert await rr.rerank("q", ["d"]) is None
    assert rr.circuit_open is True
    assert em.circuit_open is False, "rerank 的熔断牵连到了 embedding 桶"

    # rerank 熔断中；把替身恢复正常 ⇒ embedding **照样出网并且成功**。
    # ⚠️ 证据是"真的出网了"（`calls` 自增），不是"没抛异常" ——
    #    替身对所有客户端一视同仁地抛，只看返回值会把两件事混在一起。
    behaviour["fail"] = False
    before = calls["n"]
    assert await em.embed("q") == [0.1, 0.2, 0.3], "embedding 被连带禁用了"
    assert calls["n"] == before + 1, "embedding 没有真的出网"


def test_strategy_has_exactly_one_source() -> None:
    """★ **策略只写一处**：阈值/冷却从 `circuit_breaker` 来，不是各自拍的数。

    本条的机器证据是"别名同源"：`rerank.CONSECUTIVE_FAILURES_TO_OPEN`
    必须是 `circuit_breaker.JUDGE_FAILURES_TO_OPEN` 的**同一个值**。
    若哪天有人在 `rerank.py` 里重新写一个字面量，这条就红。
    """
    assert rr_mod.CONSECUTIVE_FAILURES_TO_OPEN == JUDGE_FAILURES_TO_OPEN
    assert rr_mod.COOLDOWN_SEC == JUDGE_COOLDOWN_SEC

    for cb in (judge_breaker("rerank"), judge_breaker("embed")):
        assert cb.failure_threshold == JUDGE_FAILURES_TO_OPEN
        assert cb.recovery_cooldown_sec == JUDGE_COOLDOWN_SEC
        assert cb.reset_on_success is True, (
            "判定客户端要的是'连续失败'语义 —— 少了这个位，"
            "成功就不清零，抖动会攒成假熔断")


def test_default_breaker_keeps_the_generating_path_unchanged() -> None:
    """⚠️ **默认值即护栏**：`reset_on_success` 默认 `False`。

    生成路径（`CircuitBreakerRegistry` → provider/租户桶）用的是
    "窗口内失败数"，`test_circuit_breaker_tenancy.py` 与
    `test_llm_gateway.py` 都依赖它。加这个开关**不许**改动它们的语义 ——
    这条断言就是"没改"的机器证据。
    """
    cb = TimeWindowCircuitBreaker("deepseek")
    assert cb.reset_on_success is False
    assert cb.failure_window_sec == 60.0


def test_reading_circuit_open_does_not_advance_the_state_machine() -> None:
    """★ `circuit_open` 是**只读**的 —— 读状态不许改状态。

    若它用 `allow_request()` 实现，每读一次就会：把 OPEN 推进到 HALF_OPEN、
    给 `total_rejected` 加一 ⇒ 熔断行为会依赖**被观测的次数**
    （`/health` 每轮询一次就消耗掉一次探测机会）。
    """
    cb = judge_breaker("embed")
    for _ in range(JUDGE_FAILURES_TO_OPEN):
        cb.record_failure()
    assert cb.snapshot()["state"] == "OPEN"

    before = cb.snapshot()["total_rejected"]
    for _ in range(50):
        assert cb.snapshot()["state"] == "OPEN"
    assert cb.snapshot()["total_rejected"] == before, (
        "读快照改动了状态机 —— 观测行为影响了被观测对象")
