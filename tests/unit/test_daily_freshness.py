"""日线新鲜度：QMT 日线也要"订阅预热"，且日K链路要绕过路由缓存。

## 这个测试对应的用户诉求（2026-09-16）

> 日k级别做T界面，自选股也是昨天的数据，没有盘中实时刷新，需要根据数据源更新周期
> 时间的三倍周期自动刷新

实测根因有两层，本文件分别覆盖：

1. **QMT 本地库盘中不写当天日线**（与分钟线同一个坑）：

       600036.SH 1d 末根 = 2026-09-14（当天 09-16，缺两天）
       588170.SH 1d 末根 = 2026-09-15
       subscribe_quote(period='1d', count=-1) 1 秒后 → 末根 **20260916**
       （当天形成中的bar，close=40.71 与实时快照一致）

   "本地库非空但停在过去"最隐蔽：原有的"空才补下载"分支根本不会触发。

2. **采集链会把昨天的日线当成可用数据**：日线指标有 4 小时 TTL
   （`router._TTL_BY_PREFIX`），且 DB 里的昨日bar在 `DataFreshnessEvaluator` 里
   只是 lagging（conf≥0.4）→ `_is_db_fresh` 判可用 → 直接返回，永不穿透网络。
   所以日K链路改为**显式给日期区间**（路由在指定 start/end 时跳过 TTL 与 DB）。
"""

from __future__ import annotations

import asyncio
import time

import pandas as pd
import pytest

from src.core import qmt_guard


def asyncio_run(coro):
    return asyncio.run(coro)


class _FakeXtData:
    """记录 subscribe / read 顺序的 xtquant 替身。"""

    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self.events: list[str] = []
        self.subscriptions: list[tuple[str, str, int]] = []
        self.reads: list[dict] = []
        self.frame = frame
        self.enable_hello = True

    def subscribe_quote(self, code: str, *, period: str = "1d", count: int = 0) -> int:
        self.events.append(f"subscribe:{code}:{period}")
        self.subscriptions.append((code, period, count))
        return len(self.subscriptions)

    def get_market_data_ex(self, fields, codes, **kwargs):  # noqa: ANN001, ANN003
        self.events.append(f"read:{codes[0]}")
        self.reads.append(dict(kwargs))
        frame = self.frame
        if frame is None:
            ts = 1789056000000  # 2026-09-11 UTC，东八区当日不跨天
            frame = pd.DataFrame({
                "time": [ts], "open": [10.0], "high": [10.2], "low": [9.9],
                "close": [10.1], "volume": [1.0], "amount": [10.0]})
        return {codes[0]: frame}

    def download_history_data(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.events.append("download")


@pytest.fixture(autouse=True)
def _isolated_subscriptions():
    """订阅去重表在 qmt_guard 里是模块级的：不隔离会让用例互相"已经订阅过了"。"""
    qmt_guard.reset_subscriptions()
    yield
    qmt_guard.reset_subscriptions()


# ==================== 中性层：subscribe_once ====================


def test_subscribe_once_uses_count_minus_one() -> None:
    """count=-1 是关键：它把**历史+当天**一起推下来，才能修好"库非空但停在昨天"。"""
    fake = _FakeXtData()
    assert qmt_guard.subscribe_once("600036.SH", "1d", client=fake) is True
    assert fake.subscriptions == [("600036.SH", "1d", -1)]


def test_subscribe_once_dedupes_within_ttl_across_callers() -> None:
    """两个模块共用同一份去重表：做T链路订阅过的，日线链不会再订阅一次。"""
    fake = _FakeXtData()
    assert qmt_guard.subscribe_once("600036.SH", "1d", client=fake) is True
    assert qmt_guard.subscribe_once("600036.SH", "1d", client=fake) is False
    assert len(fake.subscriptions) == 1

    from src.intraday.sources import QmtMinuteSource

    source = QmtMinuteSource()
    source._xtdata = fake  # noqa: SLF001
    assert source.warm_today("600036", "1d") is False   # 已被上面的调用覆盖
    assert len(fake.subscriptions) == 1
    # 不同周期各自订阅（每个周期的本地库是独立的）
    assert source.warm_today("600036", "5m") is True
    assert len(fake.subscriptions) == 2


def test_subscribe_once_failure_is_not_retried_immediately() -> None:
    """订阅失败也要记时间：否则每次取数都会重试一遍注定失败的订阅。"""
    class _Broken(_FakeXtData):
        def subscribe_quote(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("终端未登录")

    fake = _Broken()
    with pytest.raises(RuntimeError):
        qmt_guard.subscribe_once("600036.SH", "1d", client=fake)
    assert qmt_guard.subscribe_once("600036.SH", "1d", client=fake) is False


def test_guard_describe_lists_subscriptions() -> None:
    fake = _FakeXtData()
    qmt_guard.subscribe_once("600036.SH", "1d", client=fake)
    assert "600036.SH:1d" in qmt_guard.describe()["subscribed"]


# ==================== 项目日线链：读之前先订阅 ====================


def test_daily_connector_subscribes_before_reading(tmp_path) -> None:
    """日线连接器必须**先订阅再读** —— 否则读到的末根还停在两天前。"""
    from src.infrastructure.connectors.xtquant_connector import XtQuantConnector

    connector = XtQuantConnector()
    fake = _FakeXtData()
    connector._xtdata = fake  # noqa: SLF001
    points = asyncio_run(
        connector.fetch("stock_close:600036", "2026-08-01", "2026-09-30"))

    assert fake.events[0] == "subscribe:600036.SH:1d", fake.events
    assert fake.events[1].startswith("read:600036.SH")
    assert len(points) == 1


def test_daily_connector_subscribe_failure_does_not_break_fetch() -> None:
    """订阅失败不能打断既有读取路径（旧数据仍可能可用）。"""
    from src.infrastructure.connectors.xtquant_connector import XtQuantConnector

    class _NoSubscribe(_FakeXtData):
        def subscribe_quote(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("QMT 忙碌")

    connector = XtQuantConnector()
    connector._xtdata = _NoSubscribe()  # noqa: SLF001
    points = asyncio_run(connector.fetch("stock_close:600036"))
    assert len(points) == 1


# ==================== 日K做T链路：显式区间绕过 TTL/DB ====================


class _RecordingBackend:
    """记录 fetch 入参的采集链替身。"""

    def __init__(self, points: list | None = None) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.min_dates: list[str | None] = []
        self.points = points if points is not None else []

    async def fetch(self, indicator, start_date=None, end_date=None,  # noqa: ANN001
                    *, min_date=None):
        self.calls.append((indicator, start_date, end_date))
        self.min_dates.append(min_date)
        return self.points


def _daily_points(days: int = 80) -> list:
    """构造 n 根日线 DataPoint（最后一根是今天）。"""
    from src.core.schemas import DataPoint, DataSourceType, FetchMethod

    today = pd.Timestamp.now().normalize()
    points = []
    for offset in range(days - 1, -1, -1):
        day = today - pd.Timedelta(days=offset)
        points.append(DataPoint(
            indicator="stock_close:600036", value=10.0 + offset * 0.01,
            source_type=DataSourceType.API, source_url="qmt://test",
            fetch_method=FetchMethod.API_CALL, period_date=day.strftime("%Y-%m-%d"),
            extra={"open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1,
                   "volume": 100.0, "amount": 1000.0}))
    return points


def test_daily_compute_runs_off_the_event_loop(monkeypatch) -> None:
    """★ 日K的**计算段必须离开事件循环**（`CHG-0099`）。

    判据是"这次计算发生在哪个线程"，**不是**"耗时多少毫秒" —— 毫秒换个负载、
    换台机器就不成立，而线程归属是个布尔判据：把 `asyncio.to_thread` 改回直接
    `await`，这条必红。

    实测背景（2026-09-29）：`analyse_daily` 在循环线程里同步跑一次 =
    **547 ms 的循环停顿**；预热轮并发 2~3 只时 akshare 线程与 pandas 纯 Python
    段互挤 GIL，停顿被放大到 **~1.9 s**（>1s 停顿 11~12 次）。而
    `/api/v1/health/live`（前端「后端服务当前不可达」红条的探针）超时是 **4 s**、
    连续两次失败即亮 —— 停顿成片出现，用户看到的就是"断网"。
    """
    import threading

    from src.intraday import daily as daily_module
    from src.intraday.config import IntradayConfig
    from src.intraday.daily import fetch_daily_snapshot

    seen: dict[str, str] = {}
    real = daily_module.analyse_daily

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        seen["thread"] = threading.current_thread().name
        return real(*args, **kwargs)

    monkeypatch.setattr(daily_module, "analyse_daily", spy)
    asyncio_run(fetch_daily_snapshot(
        "600036", "招商银行", IntradayConfig(),
        _RecordingBackend(_daily_points())))

    assert seen.get("thread"), "analyse_daily 压根没被调到（判据会因此假绿）"
    assert "MainThread" not in seen["thread"], (
        "日K计算段跑在事件循环线程上 —— 它会按住循环，让 /health/live 与所有"
        f"在途请求排队（实测 547 ms，并发下 ~1.9 s）；实际线程={seen['thread']}")


def test_daily_snapshot_requests_explicit_range() -> None:
    """必须给显式 start/end：路由在指定区间时跳过 TTL 与 DB 短路。

    否则日线指标的 4 小时 TTL/DB 会把上一交易日的bar直接返回，
    日K面板就永远停在昨天（用户报的现象）。
    """
    from src.intraday.config import IntradayConfig
    from src.intraday.daily import fetch_daily_snapshot

    backend = _RecordingBackend(_daily_points())
    snapshot = asyncio_run(fetch_daily_snapshot(
        "600036", "招商银行", IntradayConfig(), backend))

    assert len(backend.calls) == 1
    indicator, start, end = backend.calls[0]
    assert indicator == "stock_close:600036"
    assert start and end, "必须显式给日期区间才能绕过路由缓存"
    assert end == pd.Timestamp.now().strftime("%Y-%m-%d")
    assert start < end
    # 区间要覆盖 lookback_days(250) + 绘图120 + 缓冲，不能把样本切少
    assert (pd.Timestamp(end) - pd.Timestamp(start)).days >= 400
    assert snapshot.available is True


def test_daily_snapshot_reports_today_bar_without_stale_gap() -> None:
    """拿到当天bar时**不该**再出现"日线最新为 X（非当日）"的缺口。"""
    from src.intraday.config import IntradayConfig
    from src.intraday.daily import fetch_daily_snapshot

    backend = _RecordingBackend(_daily_points())
    snapshot = asyncio_run(fetch_daily_snapshot(
        "600036", "招商银行", IntradayConfig(), backend))
    assert snapshot.trade_date == pd.Timestamp.now().strftime("%Y-%m-%d")
    assert not any("非当日" in gap for gap in snapshot.health.gaps), snapshot.health.gaps


def test_daily_snapshot_exposes_refresh_seconds_for_frontend() -> None:
    """前端按「数据源周期×3」轮询，值由服务端下发，避免两边各写一份。"""
    from src.intraday.config import IntradayConfig
    from src.intraday.daily import fetch_daily_snapshot

    config = IntradayConfig()
    backend = _RecordingBackend(_daily_points())
    snapshot = asyncio_run(fetch_daily_snapshot("600036", "", config, backend))
    assert snapshot.config_snapshot["refresh_seconds"] == config.data.daily_snapshot_ttl
    assert config.data.daily_snapshot_ttl == 180      # 3 × 分钟级数据源周期


# ==================== 当日形成中bar：市场时钟做新鲜度下限 ====================
#
# 2026-09-23 用户报障：盘中（10:07）日K面板仍停在 09-22，提示还写着
# "QMT 日线订阅未生效"（QMT 自 2026-09-22 起已默认关闭，这句是误导）。
# 实测两层根因：① 腾讯日K**带区间**时不返回当天形成中bar（连接器已修，
# 见 test_tencent_daily_connector.py）；② 路由的自动下限是"本地 DB 最新日期"
# = 昨天，第一个返回昨天的源就被采纳，带当天 bar 的源没机会被问到。
# 所以链路要把"市场时钟当天"当 `min_date` 传下去。


def test_daily_snapshot_passes_market_clock_as_freshness_floor(monkeypatch) -> None:
    """盘中：`min_date` 必须等于市场时钟当天，否则"只到昨天"的源会把当天bar挡掉。"""
    from src.intraday.config import IntradayConfig
    from src.intraday.daily import fetch_daily_snapshot

    monkeypatch.setattr("src.intraday.daily.live_session_date",
                        lambda: "2026-09-23")
    backend = _RecordingBackend(_daily_points())
    asyncio_run(fetch_daily_snapshot("600036", "招商银行",
                                     IntradayConfig(), backend))
    assert backend.min_dates == ["2026-09-23"]


def test_daily_snapshot_fails_open_when_market_clock_unavailable(monkeypatch) -> None:
    """时钟探测失败 → 下限传 None（fail-open），取数照常，绝不因时钟坏了取不到数。"""
    from src.intraday.config import IntradayConfig
    from src.intraday.daily import fetch_daily_snapshot

    def _boom() -> str:
        raise RuntimeError("模拟：时钟探测超时")

    monkeypatch.setattr("src.intraday.daily.live_session_date", _boom)
    backend = _RecordingBackend(_daily_points())
    snapshot = asyncio_run(fetch_daily_snapshot("600036", "招商银行",
                                                IntradayConfig(), backend))
    assert backend.min_dates == [None]
    assert snapshot.available is True


def test_session_min_date_returns_clock_value_or_none(monkeypatch) -> None:
    """时钟返回空串时是 None（路由的下限退回默认口径），而不是空字符串。"""
    from src.intraday.daily import _session_min_date

    monkeypatch.setattr("src.intraday.daily.live_session_date", lambda: "")
    assert _session_min_date() is None
    monkeypatch.setattr("src.intraday.daily.live_session_date",
                        lambda: "2026-09-23")
    assert _session_min_date() == "2026-09-23"


# ==================== 服务层缓存 ====================


class _FakeDailyService:
    """直接测服务层缓存语义（不碰网络）。"""

    def __init__(self) -> None:
        from src.intraday.service import IntradayService

        self.service = IntradayService()
        self.service._config = self.service._config.model_copy(deep=True)  # noqa: SLF001
        self.calls = 0

    def _reload_config(self):  # noqa: ANN202
        return self.service._config


def test_service_daily_caches_and_refresh_bypasses(monkeypatch) -> None:
    from src.intraday.models import DailySnapshot

    service = _FakeDailyService()
    counter = {"n": 0}

    async def _fake_fetch(code, name, config, backend, **kwargs):  # noqa: ANN001
        counter["n"] += 1
        return DailySnapshot(available=True, code=code, name=name,
                             trade_date="2026-09-16")

    monkeypatch.setattr("src.intraday.daily.fetch_daily_snapshot", _fake_fetch)
    service.service._reload_config = service._reload_config  # type: ignore[method-assign]  # noqa: SLF001

    async def _run():
        first = await service.service.daily("600036", refresh=True)
        second = await service.service.daily("600036")      # 命中缓存
        third = await service.service.daily("600036", refresh=True)   # 绕过缓存
        return first, second, third

    first, second, third = asyncio_run(_run())
    assert counter["n"] == 2, "第二次必须命中缓存，不该重算"
    assert first is second and third is not second


def test_service_daily_does_not_cache_failures(monkeypatch) -> None:
    """不可用的快照不进缓存：下次要重试，不能把失败也缓存 3 分钟。"""
    from src.intraday.models import DailySnapshot

    service = _FakeDailyService()
    counter = {"n": 0}

    async def _fake_fetch(code, name, config, backend, **kwargs):  # noqa: ANN001
        counter["n"] += 1
        return DailySnapshot(available=False, code=code, name=name, gaps=["无数据"])

    monkeypatch.setattr("src.intraday.daily.fetch_daily_snapshot", _fake_fetch)
    service.service._reload_config = service._reload_config  # type: ignore[method-assign]  # noqa: SLF001

    async def _run():
        await service.service.daily("600036")
        await service.service.daily("600036")

    asyncio_run(_run())
    assert counter["n"] == 2


def test_daily_refresh_seconds_defaults_to_three_times_source_period() -> None:
    """用户口径：数据源更新周期（分钟级=60s）的 3 倍 = 180 秒。"""
    from src.intraday.config import IntradayConfig

    assert IntradayConfig().data.daily_snapshot_ttl == 180
    assert 180 == 3 * 60


def test_daily_cache_is_per_code() -> None:
    """缓存按标的隔离：换标的不能拿到上一只票的日K结论。"""
    service = _FakeDailyService()
    assert service.service._daily_cache == {}  # noqa: SLF001
    service.service._daily_cache["600036"] = (time.monotonic(), "A")  # noqa: SLF001
    assert "300308" not in service.service._daily_cache  # noqa: SLF001
