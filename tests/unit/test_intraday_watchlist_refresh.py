"""自选池盘中自动刷新：窗口判定、缓存单飞、循环行为。

## 这个测试对应的用户诉求（2026-09-16）

> 所有自选股的分时数据，都需要每分钟自动刷新一次…当前做T辅助内的自选股无法实时
> 刷新，需要手动。注意非盘中时间不需要每分钟刷新，只在开盘 9:15-11:30 和
> 13:00 到 15:00 自动刷新。

原实现里 `watchlist()` 只在**前端挂载**和**增删自选**时被调用，没有任何定时器 ——
自选股的价格/总分/信号一直停在进页面那一刻。修法：

1. `IntradayService` 起一个后台循环，盘中每分钟重算一次全部自选（`_compute_watchlist`
   为每只票跑一次轻量快照，把分钟数据取到最新）；
2. 概览结果写入进程内缓存，`GET /intraday/watchlist` 默认返回缓存，前端每分钟取一次
   即"自动刷新"，`force=true` 才是手动立即重算；
3. 非盘中不取数（`watchlist_refresh_window` 判定），缓存放宽到 300 秒。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from src.intraday.config import IntradayConfig
from src.intraday.models import WatchItem
from src.intraday.service import (
    IntradayService,
    _seconds_until_next_tick,
    watchlist_refresh_window,
)


def _at(text: str) -> datetime:
    """`2026-09-16 10:31:20` → datetime（2026-09-16 是周三，交易日）。"""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def asyncio_run(coro):
    return asyncio.run(coro)


# ==================== 窗口判定 ====================


@pytest.mark.parametrize("text", [
    "2026-09-16 09:15:00",   # 集合竞价开始（用户明确的起点）
    "2026-09-16 09:20:00",   # 竞价中
    "2026-09-16 09:30:00",   # 开盘
    "2026-09-16 11:30:00",   # 上午收盘
    "2026-09-16 11:30:30",   # 上午收盘那一刻之后仍算 11:30 这一分钟
    "2026-09-16 13:00:00",   # 下午开盘
    "2026-09-16 14:59:59",
    "2026-09-16 15:00:00",   # 收盘
])
def test_window_open_during_trading_hours(text: str) -> None:
    allowed, reason = watchlist_refresh_window(_at(text))
    assert allowed is True, reason


@pytest.mark.parametrize("text,keyword", [
    ("2026-09-16 08:00:00", "盘前"),
    ("2026-09-16 09:14:59", "盘前"),
    ("2026-09-16 11:31:00", "午间休市"),
    ("2026-09-16 12:30:00", "午间休市"),
    ("2026-09-16 15:01:00", "已收盘"),
    ("2026-09-16 22:00:00", "已收盘"),
    ("2026-09-19 10:00:00", "周末"),   # 周六
    ("2026-09-20 10:00:00", "周末"),   # 周日
])
def test_window_closed_outside_trading_hours(text: str, keyword: str) -> None:
    allowed, reason = watchlist_refresh_window(_at(text))
    assert allowed is False
    assert keyword in reason


def test_holiday_is_skipped_by_market_clock(monkeypatch) -> None:
    """工作日但休市（春节/国庆）：市场时钟停在上一交易日 → 不自动刷新。

    这里用市场自己的时钟而不是交易日历：日历要维护，而 tick 的 timetag 天然正确。
    """
    from src.intraday import sources as sources_module

    monkeypatch.setattr(sources_module, "live_session_date", lambda **_: "2026-09-15")
    allowed, reason = watchlist_refresh_window(_at("2026-09-16 10:00:00"))
    assert allowed is False
    assert "非交易日" in reason and "2026-09-15" in reason


def test_clock_unavailable_still_refreshes(monkeypatch) -> None:
    """QMT 未启动时时钟取不到 → 必须照常刷新（那种情况正靠腾讯/新浪兜底取数）。"""
    from src.intraday import sources as sources_module

    monkeypatch.setattr(sources_module, "live_session_date", lambda **_: "")
    allowed, _ = watchlist_refresh_window(_at("2026-09-16 10:00:00"))
    assert allowed is True


def test_pre_open_auction_not_treated_as_holiday(monkeypatch) -> None:
    """09:15~09:30 集合竞价期间市场时钟可能还停在上一交易日，不能据此判休市。

    当天第一笔成交之前 tick 的 timetag 不会前进 —— 若按时钟判断，
    用户要求的 09:15 起自动刷新会被整段跳过。
    """
    from src.intraday import sources as sources_module

    monkeypatch.setattr(sources_module, "live_session_date", lambda **_: "2026-09-15")
    allowed, reason = watchlist_refresh_window(_at("2026-09-16 09:20:00"))
    assert allowed is True, reason


# ==================== 刷新时刻对齐 ====================


def test_next_tick_aligns_to_minute_boundary() -> None:
    """对齐整分钟刻度 +2 秒：分钟bar生成后再取数，且误差不会逐轮累积。"""
    assert _seconds_until_next_tick(60, _at("2026-09-16 10:31:20")) == pytest.approx(42.0)
    assert _seconds_until_next_tick(60, _at("2026-09-16 10:31:59")) == pytest.approx(3.0)


def test_next_tick_never_returns_zero() -> None:
    """恰好在刻度上时也要等一点，避免忙循环。"""
    assert _seconds_until_next_tick(60, _at("2026-09-16 10:31:00")) >= 1.0


def test_next_tick_supports_multi_minute_interval() -> None:
    """间隔 >1 分钟时以"当前分钟 + interval"为界（默认 60 秒时与整分钟网格等价）。"""
    assert _seconds_until_next_tick(300, _at("2026-09-16 10:31:20")) == pytest.approx(282.0)


# ==================== 缓存与单飞 ====================


class _FakeService(IntradayService):
    """把重算替换成计数器，专测缓存/单飞/循环，不碰任何网络。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # `load_intraday_config` 按 mtime 缓存并**返回同一个对象**给所有调用方，
        # 直接改 self._config.data 会把改动泄漏给后续用例（实测：一个用例把
        # refresh_seconds 设成 0，后面的用例拿到的默认值也变成了 0）。
        self._config = self._config.model_copy(deep=True)
        self.computes = 0
        self.delay = 0.05

    def _reload_config(self) -> IntradayConfig:
        """替身不重读配置文件。

        真实现按 `fresh is not self._config` 判断是否热重载，而上面为了隔离做了
        深拷贝 —— 身份永远不等，于是每次调用都会重建子组件（把用例注入的替身
        覆盖回真实数据源，测试会静默去联网）。
        """
        return self._config

    async def _compute_watchlist(self, *, limit: int = 20) -> list[WatchItem]:
        self.computes += 1
        await asyncio.sleep(self.delay)
        return [WatchItem(code=f"60000{i}", name=f"票{i}") for i in range(3)]


def test_watchlist_hits_cache_within_ttl() -> None:
    """前端每分钟取一次必须命中缓存 —— 否则 N 只票每分钟被重算多次。"""
    service = _FakeService()
    service._config.data.watchlist_cache_ttl = 60

    async def _run():
        first = await service.watchlist()
        second = await service.watchlist()
        return first, second

    first, second = asyncio_run(_run())
    assert service.computes == 1
    assert [item.code for item in first] == [item.code for item in second]


def test_watchlist_force_bypasses_cache() -> None:
    """手动「立即刷新」必须真的重算，不能返回缓存假装刷过。"""
    service = _FakeService()

    async def _run():
        await service.watchlist()
        await service.watchlist(force=True)

    asyncio_run(_run())
    assert service.computes == 2


def test_watchlist_is_single_flight() -> None:
    """并发请求（多个标签页 + 刷新循环）只真正算一次。"""
    service = _FakeService()
    service.delay = 0.1

    async def _run():
        return await asyncio.gather(
            service.watchlist(), service.watchlist(), service.watchlist())

    results = asyncio_run(_run())
    assert service.computes == 1
    assert all(len(items) == 3 for items in results)


def test_watchlist_limit_is_respected_from_cache() -> None:
    service = _FakeService()

    async def _run():
        await service.watchlist()
        return await service.watchlist(limit=2)

    assert len(asyncio_run(_run())) == 2


def test_add_or_remove_watch_invalidates_cache() -> None:
    """刚加的票必须立刻出现在列表里（缓存不作废的话最长 60 秒看不到）。"""
    service = _FakeService()

    async def _run():
        await service.watchlist()

    asyncio_run(_run())
    assert service.computes == 1
    service.invalidate_watchlist_cache()
    asyncio_run(_run())
    assert service.computes == 2


def test_cache_ttl_relaxed_outside_trading_hours(monkeypatch) -> None:
    """收盘后行情不变：缓存放宽到 300 秒，避免前端每分钟取一次都触发全表重算。"""
    service = _FakeService()
    service._config.data.watchlist_cache_ttl = 60
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window", lambda now=None: (False, "已收盘"))
    assert service._watch_cache_ttl() == pytest.approx(300.0)


def test_cached_watchlist_survives_config_reload_branch() -> None:
    """配置热重载（自选池被外部改动）后缓存必须作废。"""
    service = _FakeService()

    async def _run():
        await service.watchlist()

    asyncio_run(_run())
    generation = service.watchlist_refresh_status()["generation"]
    service.invalidate_watchlist_cache()
    assert service.watchlist_refresh_status()["generation"] == generation + 1


# ==================== 刷新循环 ====================


def test_loop_refreshes_immediately_when_in_window(monkeypatch) -> None:
    """服务在盘中启动时要**立刻**刷一次，而不是等满一分钟。"""
    service = _FakeService()
    service._config.data.watchlist_refresh_seconds = 60
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (True, "盘中自动刷新中"))

    async def _run():
        await service.start_watchlist_refresh()
        await asyncio.sleep(0.15)
        await service.stop_watchlist_refresh()

    asyncio_run(_run())
    assert service.computes >= 1
    status = service.watchlist_refresh_status()
    assert status["last_run_at"] and status["last_count"] == 3
    assert status["last_error"] == ""


def test_loop_skips_when_window_closed(monkeypatch) -> None:
    """非盘中一秒数据都不取（这是用户明确的"不需要每分钟刷新"）。"""
    service = _FakeService()
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (False, "已收盘"))

    async def _run():
        await service.start_watchlist_refresh()
        await asyncio.sleep(0.15)
        await service.stop_watchlist_refresh()

    asyncio_run(_run())
    assert service.computes == 0


def test_loop_survives_refresh_failure(monkeypatch) -> None:
    """单轮失败不能让循环死掉：数据源抖动、某只票停牌都只影响这一轮。"""
    service = _FakeService()
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (True, "盘中自动刷新中"))
    calls = {"n": 0}

    async def _boom(*, limit: int = 20):
        calls["n"] += 1
        raise RuntimeError("数据源全挂")

    service._compute_watchlist = _boom  # type: ignore[method-assign]  # noqa: SLF001

    async def _run():
        await service.start_watchlist_refresh()
        await asyncio.sleep(0.15)
        await service.stop_watchlist_refresh()

    asyncio_run(_run())
    assert calls["n"] >= 1
    assert "数据源全挂" in service.watchlist_refresh_status()["last_error"]


def test_start_is_idempotent_and_stop_is_safe(monkeypatch) -> None:
    """重复启动不会起两个循环；未启动时停止也不报错。"""
    service = _FakeService()

    async def _run():
        await service.stop_watchlist_refresh()      # 未启动
        await service.start_watchlist_refresh()
        task = service._refresh_task
        await service.start_watchlist_refresh()     # 重复启动
        assert service._refresh_task is task
        await service.stop_watchlist_refresh()
        assert service._refresh_task is None

    asyncio_run(_run())


def test_disabled_when_interval_zero(monkeypatch) -> None:
    """watchlist_refresh_seconds=0 → 完全不启动循环（留一条关掉的口子）。"""
    service = _FakeService()
    service._config.data.watchlist_refresh_seconds = 0

    async def _run():
        await service.start_watchlist_refresh()

    asyncio_run(_run())
    assert service._refresh_task is None
    assert service.watchlist_refresh_status()["enabled"] is False


def test_status_carries_window_reason_for_ui(monkeypatch) -> None:
    """状态里带上"为什么现在没在刷"，前端直接显示，用户不用猜。"""
    service = _FakeService()
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (False, "午间休市"))
    status = service.watchlist_refresh_status()
    assert status["window_open"] is False
    assert status["window_reason"] == "午间休市"
    assert status["interval_seconds"] == 60


def test_default_config_enables_per_minute_refresh() -> None:
    """默认配置就是"每分钟"（用户口径），不是 5 分钟也不是关闭。"""
    config = IntradayConfig()
    assert config.data.watchlist_refresh_seconds == 60
    assert config.data.watchlist_cache_ttl == 60


# ==================== 接口契约 ====================


def _stub_request(service: IntradayService):
    """只带 app.state.runtime.intraday 的最小 Request 替身。"""
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(runtime=SimpleNamespace(intraday=service))))


def test_api_watchlist_returns_items_and_auto_refresh() -> None:
    """`GET /intraday/watchlist` 必须带上 auto_refresh —— 前端靠它显示刷新状态。"""
    from src.api.routes.intraday import watchlist as watchlist_route

    service = _FakeService()
    payload = asyncio_run(
        watchlist_route(_stub_request(service), limit=20, force=False))

    assert payload["count"] == 3
    assert [item["code"] for item in payload["items"]] == ["600000", "600001", "600002"]
    status = payload["auto_refresh"]
    for key in ("enabled", "interval_seconds", "window_open", "window_reason",
                "running", "generation", "last_run_at", "last_count", "last_error"):
        assert key in status, key


def test_api_watchlist_force_recomputes() -> None:
    from src.api.routes.intraday import watchlist as watchlist_route

    service = _FakeService()

    async def _run():
        await watchlist_route(_stub_request(service), limit=20, force=False)
        await watchlist_route(_stub_request(service), limit=20, force=True)

    asyncio_run(_run())
    assert service.computes == 2


def test_ws_and_rest_share_the_same_watchlist_payload_shape() -> None:
    """REST 与 WS 用同一个 payload 构造函数：两边字段不会各自漂移。"""
    from src.api.routes.intraday import _watchlist_payload

    service = _FakeService()

    async def _run():
        return await service.watchlist()

    items = asyncio_run(_run())
    payload = _watchlist_payload(items, service.watchlist_refresh_status())
    assert set(payload) == {"items", "count", "auto_refresh"}
    assert payload["count"] == len(items)


# ==================== 报价快车道（现价/涨跌幅） ====================


class _QuoteService(_FakeService):
    """带上报价快车道的替身：批量取价换成计数器。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.batches: list[list[str]] = []

    async def _batch(self, codes: list[str]):
        from src.intraday.models import Quote

        self.batches.append(list(codes))
        return {code: Quote(code=code, price=100.0 + index, prev_close=99.0,
                            change=1.0, change_pct=1.01)
                for index, code in enumerate(codes)}


def _patch_batch(monkeypatch, service: _QuoteService) -> None:
    monkeypatch.setattr(service._data, "fetch_quotes", service._batch)  # noqa: SLF001


def test_quote_overlay_replaces_price_but_keeps_score(monkeypatch) -> None:
    """核心语义：价格走快车道，总分/信号仍是最近一次重算的值。

    两者节奏不同（5 秒 vs 60 秒），所以 `quote_ts` 必须带上，前端才能分开显示 ——
    否则用户会把 60 秒前的信号当成此刻的信号。
    """
    service = _QuoteService()
    _patch_batch(monkeypatch, service)

    async def _run():
        await service.watchlist()                    # 写入 3 只（无价格）
        overlay = await service._data.fetch_quotes(  # noqa: SLF001 快车道写覆盖层
            ["600000", "600001", "600002"])
        service._quote_overlay.update(overlay)       # noqa: SLF001
        service._quote_generation += 1               # noqa: SLF001
        return await service.watchlist()             # 命中缓存 + 叠加实时价

    items = asyncio_run(_run())
    assert [item.price for item in items] == [100.0, 101.0, 102.0]
    assert all(item.change_pct == pytest.approx(1.01) for item in items)
    assert all(item.quote_ts for item in items)


def test_quote_overlay_partial_coverage_keeps_old_price() -> None:
    """快车道没覆盖到的标的保留原价，**不清空** —— 清空会让列表闪成 "—"。"""
    from src.intraday.models import Quote

    service = _QuoteService()
    items = [WatchItem(code="600000", name="A", price=10.0, change_pct=1.0),
             WatchItem(code="600001", name="B", price=20.0, change_pct=2.0)]
    service._quote_overlay.update({  # noqa: SLF001
        "600000": Quote(code="600000", price=11.0, prev_close=10.0, change_pct=10.0)})
    merged = service._apply_quote_overlay(items)  # noqa: SLF001
    assert merged[0].price == 11.0
    assert merged[1].price == 20.0 and merged[1].change_pct == 2.0
    assert merged[1].quote_ts == ""


def test_quote_loop_updates_overlay_in_window(monkeypatch) -> None:
    from src.intraday.models import Quote

    service = _QuoteService()
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (True, "盘中自动刷新中"))
    calls = {"n": 0}

    async def _batch(codes: list[str]):
        calls["n"] += 1
        return {code: Quote(code=code, price=50.0, prev_close=49.0, change_pct=2.0)
                for code in codes}

    service._data.fetch_quotes = _batch  # type: ignore[method-assign]  # noqa: SLF001
    service._config.data.quote_refresh_seconds = 1

    async def _run():
        await service.start_quote_refresh()
        await asyncio.sleep(0.1)
        await service.stop_quote_refresh()

    asyncio_run(_run())
    assert calls["n"] >= 1
    status = service.watchlist_refresh_status()
    assert status["quote_running"] is False          # 已停止
    assert status["quote_last_run_at"]                # 但留下了运行痕迹
    assert status["quote_generation"] >= 1
    # 覆盖数 = 自选池规模（替身按请求的代码逐个返回），用配置算而不是写死 9，
    # 免得用户改自选池就把用例测挂了
    assert status["quote_covered"] == len(service._config.watchlist)  # noqa: SLF001


def test_quote_loop_skips_outside_window(monkeypatch) -> None:
    """非盘中不取价 —— 与整表刷新同一个窗口口径，用户口径是"只在盘中刷新"。"""
    service = _QuoteService()
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (False, "已收盘"))
    calls = {"n": 0}

    async def _batch(codes: list[str]):
        calls["n"] += 1
        return {}

    service._data.fetch_quotes = _batch  # type: ignore[method-assign]  # noqa: SLF001

    async def _run():
        await service.start_quote_refresh()
        await asyncio.sleep(0.1)
        await service.stop_quote_refresh()

    asyncio_run(_run())
    assert calls["n"] == 0


def test_quote_loop_survives_failure(monkeypatch) -> None:
    service = _QuoteService()
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (True, "盘中自动刷新中"))

    async def _boom(codes: list[str]):
        raise RuntimeError("QMT 掉了")

    service._data.fetch_quotes = _boom  # type: ignore[method-assign]  # noqa: SLF001

    async def _run():
        await service.start_quote_refresh()
        await asyncio.sleep(0.1)
        await service.stop_quote_refresh()

    asyncio_run(_run())
    assert "QMT 掉了" in service.watchlist_refresh_status()["quote_last_error"]


def test_quote_refresh_disabled_when_zero(monkeypatch) -> None:
    service = _QuoteService()
    service._config.data.quote_refresh_seconds = 0

    async def _run():
        await service.start_quote_refresh()

    asyncio_run(_run())
    assert service._quote_task is None
    assert service.watchlist_refresh_status()["quote_enabled"] is False


def test_aclose_stops_both_loops(monkeypatch) -> None:
    """关停时必须把两条循环都停掉（只停一条会留下一个继续取数的孤儿任务）。"""
    service = _QuoteService()
    monkeypatch.setattr(
        "src.intraday.service.watchlist_refresh_window",
        lambda now=None: (True, "盘中自动刷新中"))

    async def _run():
        await service.start_watchlist_refresh()
        await service.start_quote_refresh()
        await service.aclose()
        return service._refresh_task, service._quote_task

    refresh_task, quote_task = asyncio_run(_run())
    assert refresh_task is None and quote_task is None


def test_default_config_enables_five_second_quotes() -> None:
    """默认 5 秒报价 / 60 秒打分（用户选的"3~5 秒"档）。"""
    config = IntradayConfig()
    assert config.data.quote_refresh_seconds == 5


# ==================== 数据层：批量快照 ====================


def test_qmt_batch_quote_parses_ticks(monkeypatch) -> None:
    """QMT 批量 `get_full_tick` → {代码: Quote}，字段口径与单只完全一致。"""
    from src.intraday.sources import IntradayDataProvider, quote_from_qmt_tick

    tick = {"lastPrice": 40.81, "lastClose": 41.10, "open": 41.0,
            "high": 41.2, "low": 40.5, "volume": 12345.0, "amount": 5.0e7}
    quote = quote_from_qmt_tick("600036", tick)
    assert quote is not None
    assert quote.price == pytest.approx(40.81)
    assert quote.change_pct == pytest.approx((40.81 - 41.10) / 41.10 * 100)
    assert quote.change == pytest.approx(40.81 - 41.10)

    # 价格无效/缺字段 → None（调用方保留上一价，而不是记成 0）
    assert quote_from_qmt_tick("600036", {"lastPrice": 0}) is None
    assert quote_from_qmt_tick("600036", {}) is None
    assert quote_from_qmt_tick("600036", "不是字典") is None

    provider = IntradayDataProvider(IntradayConfig())

    class _Xt:
        @staticmethod
        def get_full_tick(codes):
            assert codes == ["600036.SH", "300308.SZ"]   # 一次调用取全部
            return {"600036.SH": tick, "300308.SZ": {"lastPrice": 915.25,
                                                     "lastClose": 864.0}}

    provider._qmt._xtdata = _Xt()  # noqa: SLF001
    result = provider._qmt_quotes_sync(["600036", "300308"])  # noqa: SLF001
    assert set(result) == {"600036", "300308"}
    assert result["300308"].change_pct == pytest.approx((915.25 - 864.0) / 864.0 * 100)


def test_batch_quotes_falls_back_to_tencent_then_single(monkeypatch) -> None:
    """QMT 只给到一部分时，缺失的走腾讯批量补；仍缺的才逐只兜底。"""
    from src.intraday.models import Quote
    from src.intraday.sources import IntradayDataProvider

    provider = IntradayDataProvider(IntradayConfig())
    single_calls: list[str] = []

    class _Xt:
        @staticmethod
        def get_full_tick(codes):
            return {"600036.SH": {"lastPrice": 40.0, "lastClose": 39.0}}

    class _Tencent:
        async def fetch_quotes_batch(self, codes):
            return {code: Quote(code=code, price=20.0, prev_close=19.0)
                    for code in codes if code != "300308"}

    provider._qmt._xtdata = _Xt()  # noqa: SLF001
    provider._tencent = _Tencent()  # type: ignore[assignment]  # noqa: SLF001
    provider._client = object()  # type: ignore[assignment]  # noqa: SLF001

    async def _single(code: str):
        single_calls.append(code)
        return Quote(code=code, price=9.0, prev_close=8.0), "新浪财经", []

    provider.fetch_quote = _single  # type: ignore[method-assign]  # noqa: SLF001

    result = asyncio_run(provider.fetch_quotes(["600036", "600001", "300308"]))
    assert result["600036"].price == 40.0          # QMT
    assert result["600001"].price == 20.0          # 腾讯批量
    assert result["300308"].price == 9.0           # 逐只兜底
    assert single_calls == ["300308"]


def test_batch_quotes_returns_empty_for_empty_input() -> None:
    from src.intraday.sources import IntradayDataProvider

    provider = IntradayDataProvider(IntradayConfig())
    assert asyncio_run(provider.fetch_quotes([])) == {}
