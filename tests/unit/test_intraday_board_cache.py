"""板块数据的**非阻塞与缓存**行为单测（离线，打桩掉网络）。

守着的是一次真实事故的修复：做T面板开盘后"两分钟数据还没出来"。
实测（2026-09-16 盘中）：

- `fetch_series`（板块分时）单次 **22.3 秒**，其中 14.4 秒是 akshare 为解析
  一个板块名而重新拉取全市场概念板块名单，**而它原先完全没有缓存**；
- 自选 7 只票 × 每只 1~3 个板块 → 每轮刷新都要重付一遍；
- 更糟的是本机东财 `push2` 域名被阻断，akshare 会在这个失败上**空转重试 17.5 秒**。

所以这里钉住三条不变量：

1. 命中缓存 → 不重新取数；
2. 过期 → **先返回旧值**、后台刷新，不阻塞调用方；
3. 源已经不可用（冷却中）→ 不再起后台任务，也**不承诺"稍后会出现"**。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.intraday.board import BoardContextProvider
from src.intraday.config import IntradayConfig
from src.intraday.models import BoardSeries


def provider() -> BoardContextProvider:
    return BoardContextProvider(IntradayConfig(), None)


def series(name: str, points: int = 3) -> BoardSeries:
    return BoardSeries(name=name, kind="concept", available=True,
                       source_name="测试源",
                       points=[{"ts": f"2026-09-16 09:3{index}:00",
                                "price": 100.0 + index}
                               for index in range(points)])


# ==================================================================
# 缓存
# ==================================================================


def test_series_is_cached_and_not_refetched(monkeypatch) -> None:
    """命中缓存时**不得**再触发取数（这是 22 秒成本的根源）。"""
    board = provider()
    calls = {"count": 0}

    async def fake_fetch(name, kind, peer_codes):
        calls["count"] += 1
        return series(name)

    monkeypatch.setattr(board, "_fetch_series_uncached", fake_fetch)

    async def run() -> None:
        first = await board.fetch_series("PCB概念")
        second = await board.fetch_series("PCB概念")
        third = await board.fetch_series("PCB概念")
        assert first.available and second.available and third.available

    asyncio.run(run())
    assert calls["count"] == 1, "缓存命中后不应再次取数"


def test_expired_series_returns_stale_value_without_blocking(monkeypatch) -> None:
    """过期时**先返回旧值**，后台刷新 —— 调用方不应等待。"""
    board = provider()
    board._series_cache["PCB概念"] = (time.monotonic() - 10_000, series("PCB概念"))

    async def slow_fetch(name, kind, peer_codes):
        await asyncio.sleep(5)          # 模拟 20 秒的慢取数
        return series(name, points=99)

    monkeypatch.setattr(board, "_fetch_series_uncached", slow_fetch)

    async def run() -> None:
        started = time.perf_counter()
        result = await board.fetch_series("PCB概念")
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"过期数据也要立刻返回，实际 {elapsed:.2f}s"
        assert result.available and len(result.points) == 3, "返回的应是旧值"
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_first_call_without_cache_does_not_block_when_wait_false(
        monkeypatch) -> None:
    """首屏（无缓存 + wait=False）必须立刻返回，并如实说明"后台在取"。"""
    board = provider()

    async def slow_fetch(name, kind, peer_codes):
        await asyncio.sleep(5)
        return series(name)

    monkeypatch.setattr(board, "_fetch_series_uncached", slow_fetch)

    async def run() -> None:
        started = time.perf_counter()
        result = await board.fetch_series("PCB概念", wait=False)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"首屏不应该等取数，实际 {elapsed:.2f}s"
        assert result.available is False
        assert "后台" in (result.gap or "")

    asyncio.run(run())


def test_wait_true_still_fetches_synchronously(monkeypatch) -> None:
    """`wait=True`（回测等场景）仍要拿到真实数据，不能被"后台获取中"敷衍过去。"""
    board = provider()
    calls = {"count": 0}

    async def fake_fetch(name, kind, peer_codes):
        calls["count"] += 1
        return series(name, points=7)

    monkeypatch.setattr(board, "_fetch_series_uncached", fake_fetch)

    async def run() -> None:
        result = await board.fetch_series("PCB概念", wait=True)
        assert result.available and len(result.points) == 7

    asyncio.run(run())
    assert calls["count"] == 1


# ==================================================================
# 阻断识别
# ==================================================================


def test_blocked_source_is_not_promised_to_appear_later() -> None:
    """源已被判定阻断时，必须**如实说不可用**，而不是"正在后台获取"。

    否则面板会一直显示"稍后出现"，用户等一个永远不会来的板块分时图 ——
    这比直接说"这个源在本机不可用"更糟。
    """
    board = provider()
    board._em_blocked_until = time.monotonic() + 3600

    async def run() -> None:
        result = await board.fetch_series("PCB概念", wait=False)
        assert result.available is False
        assert "阻断" in (result.gap or "") or "不可用" in (result.gap or "")
        assert "后台" not in (result.gap or ""), \
            "判定阻断后不应再承诺稍后出现"
        assert not board._series_inflight, "阻断时不应起后台任务"

    asyncio.run(run())


@pytest.mark.parametrize("text", [
    "ConnectionError: ('Connection aborted.', RemoteDisconnected(...))",
    "RemoteDisconnected: Remote end closed connection without response",
    "HTTPConnectionPool: Max retries exceeded with url",
])
def test_connection_failures_are_classified_as_block(text: str) -> None:
    assert BoardContextProvider._looks_like_block(text) is True


@pytest.mark.parametrize("text", [
    "KeyError: 'PCB概念'",
    "ValueError: 找不到该板块",
    "",
])
def test_non_connection_failures_are_not_blocks(text: str) -> None:
    """业务错误不能被当成"网络阻断" —— 否则会把一次名字写错当成整源挂掉。"""
    assert BoardContextProvider._looks_like_block(text) is False


def test_block_uses_longer_cooldown_than_single_name(monkeypatch) -> None:
    """阻断标记要压过单板块冷却：网络级问题不会 5 分钟自愈。"""
    board = provider()
    board._mark_em("PCB概念", False, gap="RemoteDisconnected")
    remaining = board._em_remaining("PCB概念")
    assert remaining > board._config.data.source_cooldown_seconds, \
        "阻断后的冷却应长于普通失败冷却"


def test_success_clears_block(monkeypatch) -> None:
    board = provider()
    board._mark_em("PCB概念", False, gap="RemoteDisconnected")
    assert board._em_remaining("PCB概念") > 0
    board._mark_em("PCB概念", True)
    assert board._em_remaining("PCB概念") == 0
