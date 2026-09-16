"""消息面 LLM 调用的缓存优化：新闻没变就不重跑模型。

## 依据（2026-09-16 审计日志实测）

`data/audit/llm_audit.jsonl` 里做T消息面共 278 次调用，只有 **28 个不同的 prompt**
（重复率 90%，最热的一个 prompt 被推理了 **97 次**）；其中 161 次走本地推理模型
`qwen3:8b-q4_K_M`，**中位 53.7 秒/次**、13% 把 4096 输出额度全烧在思维链上。
这些全是"新闻一条没变却重跑一遍"的白烧。

修法：TTL 到期后**照常抓一次新闻**（本来也要抓），但先比内容指纹（标题+发布时间）；
没变 → 滑动 TTL 并复用上次打分，**不调 LLM**；变了 → 才重跑。
新闻抓取频率与之前完全一致，所以是纯收益。

> 附带结论：这条链路**不花云端 token** —— `light` 层主模型是本地 Ollama
> qwen2.5:1.5b（实测 115 次调用、中位 0.99 秒、输出约 70 token）。
> 只有本地不可用、熔断或超时时才会按 `models.yaml` 的 fallback 降级到
> deepseek-flash（那时才计费）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.intraday.sentiment import NewsSentimentAnalyzer


def asyncio_run(coro):
    return asyncio.run(coro)


class _Fetcher:
    """可控新闻源：items 可变，记录抓取次数。"""

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls = 0

    async def fetch_news(self, code: str, limit: int = 10) -> list[dict]:
        self.calls += 1
        return list(self.items)


class _Gateway:
    """假网关：记录调用次数，返回固定 JSON。"""

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, tier, system, prompt, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.prompts.append(prompt)

        class _Resp:
            content = ('{"items":[{"i":0,"polarity":"positive"}],'
                       '"positive":1,"negative":0,"neutral":0,'
                       '"score":0.4,"summary":"测试"}')
            tokens_in = 100
            tokens_out = 20
            model_used = "fake"
            fallback_used = False

        return _Resp()


_ITEMS = [
    {"title": "公司发布中报", "publish_time": "2026-09-16 09:00",
     "source_name": "东财", "text": "正文"},
    {"title": "机构调研", "publish_time": "2026-09-16 08:00",
     "source_name": "东财", "text": "正文"},
]


def _analyzer(ttl: int = 1, reuse: bool = True):
    fetcher = _Fetcher(_ITEMS)
    gateway = _Gateway()
    analyzer = NewsSentimentAnalyzer(
        gateway, fetcher, cache_ttl=ttl, reuse_when_unchanged=reuse)
    return analyzer, gateway, fetcher


def test_unchanged_news_reuses_score_without_llm() -> None:
    """TTL 过期但新闻没变 → 不再调 LLM（这是 90% 重复的来源）。"""
    analyzer, gateway, fetcher = _analyzer(ttl=1)

    async def _run():
        first = await analyzer.analyze(code="600036")
        await asyncio.sleep(1.1)                 # 让 TTL 过期
        second = await analyzer.analyze(code="600036")
        return first, second

    first, second = asyncio_run(_run())
    assert gateway.calls == 1, "内容未变却又调了一次 LLM"
    assert analyzer.reused_calls == 1
    assert first.llm_score == second.llm_score
    # 新闻确实又抓了一次（TTL 到期本来就该抓），所以抓取次数是 2
    assert fetcher.calls == 2


def test_changed_news_triggers_new_llm_call() -> None:
    """新闻变了 → 必须重跑（否则会拿旧情绪给新消息）。"""
    analyzer, gateway, fetcher = _analyzer(ttl=1)

    async def _run():
        await analyzer.analyze(code="600036")
        await asyncio.sleep(1.1)
        fetcher.items = [{"title": "突发：公司被立案调查",
                          "publish_time": "2026-09-16 10:30",
                          "source_name": "东财", "text": "正文"}]
        await analyzer.analyze(code="600036")

    asyncio_run(_run())
    assert gateway.calls == 2
    assert analyzer.llm_calls == 2
    assert analyzer.reused_calls == 0


def test_ttl_hit_does_not_even_fetch_news() -> None:
    """TTL 内直接命中：连新闻都不用抓（保持原有行为）。"""
    analyzer, gateway, fetcher = _analyzer(ttl=600)

    async def _run():
        await analyzer.analyze(code="600036")
        await analyzer.analyze(code="600036")

    asyncio_run(_run())
    assert gateway.calls == 1
    assert fetcher.calls == 1


def test_reuse_can_be_disabled() -> None:
    """关掉开关后退回旧行为（每个 TTL 周期都重跑一次）。"""
    analyzer, gateway, _ = _analyzer(ttl=1, reuse=False)

    async def _run():
        await analyzer.analyze(code="600036")
        await asyncio.sleep(1.1)
        await analyzer.analyze(code="600036")

    asyncio_run(_run())
    assert gateway.calls == 2
    assert analyzer.reused_calls == 0


def test_no_news_never_calls_llm() -> None:
    """抓不到新闻 → 直接走关键词计数兜底，**一次 LLM 都不调**。

    实测今天盘中就是这种情况（"近24小时未取到相关新闻"），
    所以做T面板开着也完全不花 token。
    """
    analyzer, gateway, _ = _analyzer(ttl=1)
    analyzer._news_fetcher.items = []  # noqa: SLF001

    async def _run():
        return await analyzer.analyze(code="600036")

    result = asyncio_run(_run())
    assert gateway.calls == 0
    assert result.available is False
    assert "未取到个股新闻" in (result.gap or "")


def test_fingerprint_ignores_unrelated_fields() -> None:
    """指纹只看标题+发布时间：来源/正文变化不该导致重跑（省调用）。"""
    analyzer, gateway, fetcher = _analyzer(ttl=1)

    async def _run():
        await analyzer.analyze(code="600036")
        await asyncio.sleep(1.1)
        fetcher.items = [{**item, "text": "正文更新了", "source_name": "新浪"}
                         for item in _ITEMS]
        await analyzer.analyze(code="600036")

    asyncio_run(_run())
    assert gateway.calls == 1, "只改了正文/来源，不该重跑 LLM"


def test_per_code_isolation() -> None:
    """不同标的各自缓存：切标的不会互相污染，也不会因此省掉该跑的调用。"""
    analyzer, gateway, _ = _analyzer(ttl=600)

    async def _run():
        await analyzer.analyze(code="600036")
        await analyzer.analyze(code="300308")

    asyncio_run(_run())
    assert gateway.calls == 2


def test_default_config_enables_reuse() -> None:
    from src.intraday.config import IntradayConfig

    assert IntradayConfig().factors.news.reuse_when_unchanged is True


@pytest.mark.parametrize("ttl", [0, 1])
def test_after_ttl_changed_news_is_reanalyzed(ttl: int) -> None:
    """TTL 过期（或没开 TTL）+ 新闻变了 → 必须重跑（新消息不能拿旧情绪）。"""
    analyzer, gateway, fetcher = _analyzer(ttl=ttl)

    async def _run():
        await analyzer.analyze(code="600036")
        if ttl:
            await asyncio.sleep(1.1)
        fetcher.items = [{"title": "全新消息", "publish_time": "2026-09-16 11:00",
                          "source_name": "东财", "text": "x"}]
        return await analyzer.analyze(code="600036")

    asyncio_run(_run())
    assert gateway.calls == 2


def test_within_ttl_news_change_is_tolerated_by_design() -> None:
    """TTL **窗口内**即使新闻变了也不重取 —— 这是刻意取舍，不是缺陷。

    消息面是七因子里变化最慢的维度，而 LLM 是整条链路最慢的一环；
    为了"10 分钟内可能出现的一条新闻"把每次快照都拖上十几秒（本地推理模型
    中位 53.7 秒）明显不划算。要立刻反映新消息，用「强制刷新」。
    """
    analyzer, gateway, fetcher = _analyzer(ttl=600)

    async def _run():
        await analyzer.analyze(code="600036")
        fetcher.items = [{"title": "10分钟内出现的突发消息",
                          "publish_time": "2026-09-16 10:31",
                          "source_name": "东财", "text": "x"}]
        return await analyzer.analyze(code="600036")

    asyncio_run(_run())
    assert gateway.calls == 1        # 窗口内不重跑
    assert fetcher.calls == 1        # 窗口内连新闻都不重抓


def test_reuse_counter_is_observable() -> None:
    """省了多少次要被记录下来（否则这种优化没法被验证）。"""
    analyzer, gateway, _ = _analyzer(ttl=1)

    async def _run():
        await analyzer.analyze(code="600036")
        for _ in range(3):
            await asyncio.sleep(1.1)
            await analyzer.analyze(code="600036")

    asyncio_run(_run())
    assert gateway.calls == 1
    assert analyzer.reused_calls == 3
    assert analyzer.llm_calls == 1


def test_wall_clock_saving_is_large_with_slow_model() -> None:
    """量化收益：慢模型（本地 qwen3:8b 中位 53.7s）下省下的等待时间。

    10 分钟内轮询 N 次标的、新闻不变 → 只跑 1 次推理而不是 N 次。
    """
    analyzer, gateway, _ = _analyzer(ttl=1)
    slow_seconds = 53.7

    async def _run():
        await analyzer.analyze(code="600036")
        for _ in range(9):
            await asyncio.sleep(1.1)
            await analyzer.analyze(code="600036")

    asyncio_run(_run())
    saved_calls = analyzer.reused_calls
    assert saved_calls == 9
    assert saved_calls * slow_seconds / 60 > 8       # ≈ 8 分钟本地算力
    assert gateway.calls == 1


def test_elapsed_wall_time_reflects_no_llm_on_reuse() -> None:
    """复用路径必须**立刻**返回（不能又走一遍 54 秒的推理）。"""
    analyzer, _, _ = _analyzer(ttl=1)

    async def _run():
        await analyzer.analyze(code="600036")
        await asyncio.sleep(1.1)
        started = time.perf_counter()
        await analyzer.analyze(code="600036")
        return time.perf_counter() - started

    elapsed = asyncio_run(_run())
    assert elapsed < 0.5
