"""`RerankClient` 的离线判据 —— **不联网**（`CHG-0203`）。

## 为什么值得单独测

它是**判据组件**：它的输出直接决定"两条问句是不是同一件事"。
而它同时有三个容易写错、且**都不会报错**的性质：

1. **顺序对齐** —— 服务端按分数**降序**返回，还带 `index`。
   若直接拿返回顺序当文档顺序，就会出现"第 0 篇文档拿到第 3 篇的分数"，
   而调用方按 `zip(docs, scores)` 对齐 ⇒ **一次静默的错位判定**。
2. **fail-open** —— 它绝不能抛（判定失败只该让这一次"语义未命中"）。
3. **熔断** —— 进程外的网络黑洞会让**每一次**缓存查找白等一整个超时。
   没有熔断时，"端点慢"会静默放大成"分析慢几十秒"。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from src.infrastructure.llm.rerank import (
    CONSECUTIVE_FAILURES_TO_OPEN,
    RerankClient,
)


def _client(**kw) -> RerankClient:
    kw.setdefault("base_url", "https://example.invalid/v1")
    kw.setdefault("api_key", "sk-test")
    return RerankClient(**kw)


def test_not_configured_without_credentials() -> None:
    """缺 key / 缺 base_url ⇒ `configured=False`（调用方按"未启用"处置）。

    与 `EmbeddingClient` 同一条纪律：**宁可报"没接上"，
    也不要"接上了但每次都失败"** —— 后者的症状是"语义层没效果"，
    排查方向会完全跑偏。
    """
    assert _client(base_url="", api_key="k").configured is False
    assert _client(base_url="https://x", api_key="").configured is False
    assert _client().configured is True


@pytest.mark.asyncio
async def test_scores_are_returned_in_document_order(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ **服务端降序返回时，客户端必须按 `index` 回填成原顺序。**

    这是本文件最重要的一条：错位的分数**不会报错**，
    只会让判定用错文档 —— 而它看起来和"判定不准"一模一样。
    """
    docs = ["doc0", "doc1", "doc2"]

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            # ⚠️ 刻意**降序**返回（服务端就是这么给的），index 指回原位置。
            return {"results": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.1},
            ]}

    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await _client().rerank("q", docs)
    assert out == [0.5, 0.1, 0.9], (
        f"分数没有按文档顺序回填：{out} —— 调用方会 zip 出错位的判定")


@pytest.mark.asyncio
async def test_partial_results_are_a_failure_not_zero_scores(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端**少给**条目 ⇒ 视为失败，**不许**把缺的当成 0 分。

    "缺条目"与"这篇不相关"是两件事。当成 0 分就是**编造分数**，
    而且会把一次本该命中的复用静默变成不命中。
    """
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"results": [{"index": 0, "relevance_score": 0.9}]}

    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await _client().rerank("q", ["a", "b", "c"]) is None


@pytest.mark.asyncio
async def test_never_raises_on_transport_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """传输层异常 ⇒ 返回 `None`，**绝不抛**（fail-open）。"""
    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            raise httpx.ReadError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    c = _client()
    assert await c.rerank("q", ["a", "b"]) is None
    assert c.stats()["failures"] == 1 and c.stats()["calls"] == 0, (
        "失败时 `calls` 不该自增 —— `calls=0 & failures>0` 是"
        "「每次都失败」的签名，与「从没被调用过」（两者都是 0）必须分开读")


@pytest.mark.asyncio
async def test_http_error_carries_the_reason(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """402/429 这类**立即返回**的错误也要把原因留下来。

    本项目实测过余额耗尽（`HTTP 402 code:30001`）：那次排查之所以绕远，
    就是因为计数器只说了"失败了"，没说"为什么"。
    """
    class _Resp:
        status_code = 402
        text = '{"code":30001,"message":"Sorry, your account balance is insufficient"}'

        @staticmethod
        def json():
            return {}

    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    # ⚠️ **指名到具体 logger**（不是 root）：`caplog.at_level(DEBUG)` 设的是
    #    root logger 的级别，而全套跑时别的测试可能改过日志配置 ——
    #    实测过"单跑绿、全套红"，根因就是这里。
    with caplog.at_level(logging.DEBUG, logger="src.infrastructure.llm.rerank"):
        assert await _client().rerank("q", ["a"]) is None
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "402" in joined or "insufficient" in joined, (
        "失败原因没进日志 —— 只有计数没有原因，运维会去查错方向")


@pytest.mark.asyncio
async def test_circuit_breaker_stops_the_bleeding(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 连续失败 N 次后**熔断**：期间不再出网（返回 None 而不等超时）。

    ## 为什么必须有

    `fail-open` 只保证"不抛"，**不保证"不慢"**。进程外的网络黑洞
    （丢包、半开连接）会让每一次调用都白等一整个超时（3 s），
    而一次投研分析有 4~15 次缓存查找 ⇒ **静默放大成几十秒**。
    402 那种"立即返回"的错误不会有这个问题，所以只测超时类发现不了它。
    """
    calls = {"n": 0}

    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            calls["n"] += 1
            raise httpx.ReadTimeout("black hole")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    c = _client()
    for _ in range(CONSECUTIVE_FAILURES_TO_OPEN):
        assert await c.rerank("q", ["a"]) is None
    assert calls["n"] == CONSECUTIVE_FAILURES_TO_OPEN
    assert c.circuit_open is True, "失败到阈值了却没熔断"

    # 熔断生效：**不再出网**
    assert await c.rerank("q", ["a"]) is None
    assert calls["n"] == CONSECUTIVE_FAILURES_TO_OPEN, (
        "熔断期间仍然出网了 —— 那等于没有熔断")
    assert c.stats()["circuit_open_skips"] == 1, "跳过了却没记账"


@pytest.mark.asyncio
async def test_success_resets_the_failure_streak(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """**成功清零连续失败数** —— 否则偶发抖动会累积成假熔断。"""
    state = {"fail": True}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"results": [{"index": 0, "relevance_score": 0.9}]}

    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            if state["fail"]:
                raise httpx.ReadError("blip")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    c = _client()
    # 失败到"差一次就熔断"
    for _ in range(CONSECUTIVE_FAILURES_TO_OPEN - 1):
        assert await c.rerank("q", ["a"]) is None
    state["fail"] = False
    assert await c.rerank("q", ["a"]) == [0.9]      # 成功 ⇒ 清零
    state["fail"] = True
    for _ in range(CONSECUTIVE_FAILURES_TO_OPEN - 1):
        assert await c.rerank("q", ["a"]) is None
    assert c.circuit_open is False, (
        "成功没有清零连续失败数 —— 抖动会累积成假熔断，"
        "而假熔断的症状是'语义层莫名不工作'")


@pytest.mark.asyncio
async def test_empty_inputs_short_circuit() -> None:
    """空 query / 空文档 ⇒ `None`，且**不计失败**（那不是故障）。"""
    c = _client()
    assert await c.rerank("", ["a"]) is None
    assert await c.rerank("q", []) is None
    assert c.stats()["failures"] == 0, "空输入被记成了故障"
    assert asyncio.iscoroutinefunction(c.rerank)
