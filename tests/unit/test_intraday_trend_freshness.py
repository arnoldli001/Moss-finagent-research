"""当日新鲜度闸门：分时/分钟K线绝不把上一交易日的数据当今日渲染。

## 这个测试对应的线上事故（2026-09-16 实盘）

09:52（开盘 22 分钟）打开招商银行 600036，分时图显示的却是 **2026-09-15
09:30~15:00 一整条完整曲线**。实测定位：

    fetch_trend("600036") → 迅投QMT, 241 行, 全部是 2026-09-15, 无今日数据
    600036.SH 当天 1m = 26 行   ← 本进程此前对它调用过一次补下载
    601988.SH / 000001.SZ / 600519.SH = 0 行   ← 从未调用过

QMT **不会**自动把当天分钟bar写进本地库：未补下载时 `get_market_data_ex`
只返回上一交易日的数据，而 `_trend_sync` 把「数据里最新的一天」当成当日切片，
于是昨天整天被顶着"今日分时"的名头画了出来。

修法分两层，本文件两层都测：
  1. `QmtMinuteSource.warm_today()` 在读之前补当日增量（实测 0~45ms）；
  2. `IntradayDataProvider` 的新鲜度闸门 —— 传输成功但日期陈旧的源判**失败**，
     让链上有当日数据的源（腾讯/新浪）接手；所有源都陈旧时才回退到最新的那份，
     并在 `SourceAttempt` 里显式标注"非当日"，绝不静默当今日。
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from src.intraday.config import IntradayConfig
from src.intraday.models import SourceAttempt
from src.intraday.sources import (
    IntradayDataProvider,
    QmtMinuteSource,
    series_date,
)

TODAY = "2026-09-16"
YESTERDAY = "2026-09-15"


def asyncio_run(coro):
    return asyncio.run(coro)


def _bars(date: str, count: int = 3, *, prefix: str = "") -> pd.DataFrame:
    """构造一份尾日在 `date` 的分钟序列。"""
    rows = []
    for minute in range(count):
        rows.append({
            "ts": f"{date} 09:{30 + minute:02d}",
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05,
            "volume": 100.0, "amount": 1000.0,
        })
    return pd.DataFrame(rows)


def _trend(date: str, count: int = 3) -> pd.DataFrame:
    frame = _bars(date, count)
    frame["price"] = frame["close"]
    frame["avg_price"] = frame["close"]
    return frame[["ts", "price", "avg_price", "volume", "amount"]]


def _provider(session: str = TODAY, **sources) -> IntradayDataProvider:
    """构造 provider 并把时钟与各数据源换成测试替身。

    `_client` 必须置成非 None 的哨兵：`_http()` 在 `self._client is None` 时会
    现场 new 一个**真的** TencentSource 覆盖掉替身 —— 那样测试会悄悄去联网，
    测的就不是闸门了（第一版就踩了这个坑：全源失败的用例"没抛异常"）。
    """
    config = IntradayConfig()
    config.data.source_cooldown_seconds = 0
    provider = IntradayDataProvider(config)
    provider._client = object()  # type: ignore[assignment]  # noqa: SLF001
    provider._session_date = lambda: session  # type: ignore[method-assign]  # noqa: SLF001
    # **所有**源都默认换成"无数据"的替身：漏掉一个就会真的去联网（新浪逐笔要跑
    # 2.5s 并返回当天真实行情，用例于是测出了一个假的通过/失败）。
    for name in ("qmt", "tencent", "eastmoney", "sina"):
        fake = sources[name] if name in sources else _FakeSource(None)
        setattr(provider, f"_{name}", fake)
    return provider


class _FakeSource:
    """按 (code, method) 返回预设帧的数据源替身。"""

    def __init__(self, frame: pd.DataFrame | None, *, error: str = "") -> None:
        self.frame = frame
        self.error = error
        self.calls = 0

    async def fetch_trend(self, code: str) -> pd.DataFrame:
        self.calls += 1
        if self.error:
            from src.core.exceptions import DataFetchError
            raise DataFetchError(self.error)
        if self.frame is None:
            from src.core.exceptions import DataFetchError
            raise DataFetchError("无数据")
        return self.frame

    async def fetch_bars(self, code: str, period: str, days: int) -> pd.DataFrame:
        return await self.fetch_trend(code)


# ==================== series_date ====================


def test_series_date_uses_max_not_last_row() -> None:
    """行序不保证递增，且尾部可能带"正在形成"的bar —— 取最大日期才稳。"""
    frame = pd.DataFrame({"ts": [f"{TODAY} 10:00", f"{YESTERDAY} 14:00",
                                 f"{TODAY} 09:31"]})
    assert series_date(frame) == TODAY


def test_series_date_empty_cases() -> None:
    assert series_date(None) == ""
    assert series_date(pd.DataFrame()) == ""
    assert series_date(pd.DataFrame({"close": [1.0]})) == ""


# ==================== 闸门：陈旧源让位给有当日数据的源 ====================


def test_stale_qmt_yields_to_fresh_tencent_on_trend() -> None:
    """QMT 回的是昨天的分时 → 判失败并降级腾讯；命中源必须是腾讯。"""
    qmt = _FakeSource(_trend(YESTERDAY))
    tencent = _FakeSource(_trend(TODAY))
    provider = _provider(qmt=qmt, tencent=tencent)

    frame, source, attempts = asyncio_run(provider.fetch_trend("600036"))

    assert source == "腾讯行情"
    assert series_date(frame) == TODAY
    assert tencent.calls == 1
    # QMT 那次"成功但陈旧"必须留痕：ok=False + 写明日期，否则排查时看不见
    qmt_attempt = [a for a in attempts if a.source == "迅投QMT"]
    assert qmt_attempt and qmt_attempt[0].ok is False
    assert YESTERDAY in qmt_attempt[0].detail and TODAY in qmt_attempt[0].detail


def test_stale_qmt_yields_to_fresh_tencent_on_bars() -> None:
    qmt = _FakeSource(_trend(YESTERDAY))
    tencent = _FakeSource(_trend(TODAY))
    provider = _provider(qmt=qmt, tencent=tencent)

    frame, source, attempts = asyncio_run(provider.fetch_bars("600036", days=5))

    assert source == "腾讯行情"
    assert series_date(frame) == TODAY


def test_fresh_qmt_is_still_preferred() -> None:
    """QMT 有当日数据时必须仍然是首选（它是权威源，不该被闸门挤掉）。"""
    qmt = _FakeSource(_trend(TODAY))
    tencent = _FakeSource(_trend(TODAY))
    provider = _provider(qmt=qmt, tencent=tencent)

    frame, source, attempts = asyncio_run(provider.fetch_trend("600036"))

    assert source == "迅投QMT"
    assert series_date(frame) == TODAY
    assert tencent.calls == 0
    assert all(a.ok for a in attempts if a.source == "迅投QMT")


# ==================== 所有源都陈旧：回退到最新的一份并标注 ====================


def test_all_stale_returns_newest_with_explicit_warning() -> None:
    """停牌/全天无成交等"当天本来就没数据"的场景：给最新的一份，但必须标注。"""
    older = _trend("2026-09-12")
    newer = _trend(YESTERDAY)
    provider = _provider(
        qmt=_FakeSource(older), tencent=_FakeSource(newer),
        eastmoney=_FakeSource(None), sina=_FakeSource(older))

    frame, source, attempts = asyncio_run(provider.fetch_trend("600036"))

    assert series_date(frame) == YESTERDAY  # 取最新，而不是链上第一个
    assert source == "腾讯行情"
    warning = [a for a in attempts if "非当日" in a.detail]
    assert warning and warning[0].ok is False
    assert YESTERDAY in warning[0].detail


def test_stale_fallback_is_not_cached() -> None:
    """陈旧回退结果不写缓存：下一次要重新走容灾链，源恢复后能立刻切回来。"""
    qmt = _FakeSource(_trend(YESTERDAY))
    provider = _provider(qmt=qmt, tencent=_FakeSource(None))

    asyncio_run(provider.fetch_trend("600036"))
    qmt.frame = _trend(TODAY)  # QMT 补上了当日数据
    _, source, _ = asyncio_run(provider.fetch_trend("600036"))

    assert source == "迅投QMT"


# ==================== 时钟不可用：放行，不误杀 ====================


def test_no_session_clock_disables_gate() -> None:
    """时钟取不到时不能拦数据 —— 判断依据缺失时宁可不过滤。"""
    qmt = _FakeSource(_trend(YESTERDAY))
    tencent = _FakeSource(_trend(TODAY))
    provider = _provider("", qmt=qmt, tencent=tencent)

    _, source, attempts = asyncio_run(provider.fetch_trend("600036"))

    assert source == "迅投QMT"
    assert tencent.calls == 0
    assert all(a.ok for a in attempts)


def test_holiday_keeps_last_session_data() -> None:
    """节假日：市场自己的时钟停在上一交易日 → 上一交易日的分时是**正确**数据。

    这是不能用"今天是不是工作日"判断的原因：工作日休市时按日历判会把唯一
    可用的数据也拒掉。
    """
    qmt = _FakeSource(_trend(YESTERDAY))
    provider = _provider(YESTERDAY, qmt=qmt, tencent=_FakeSource(_trend(YESTERDAY)))

    frame, source, attempts = asyncio_run(provider.fetch_trend("600036"))

    assert source == "迅投QMT"
    assert series_date(frame) == YESTERDAY
    assert all(a.ok for a in attempts)


# ==================== QMT 当日补下载 ====================


class _FakeXtdata:
    """记录 subscribe_quote 调用的 xtquant 替身。

    注意这里**故意不提供** `download_history_data`：做T链路有一条硬性约定是
    绝不允许进程内下载（`tests/unit/test_qmt_guard.py` 直接断言），当日数据
    必须靠订阅拿 —— 替身里没有这个方法，一旦代码回退到下载就会 AttributeError。
    """

    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, str, int]] = []

    def subscribe_quote(self, code: str, *, period: str = "1d",
                        count: int = 0) -> int:
        self.subscriptions.append((code, period, count))
        return len(self.subscriptions)

    def get_market_data_ex(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return {}


@pytest.fixture(autouse=True)
def _isolated_warm_state(monkeypatch):
    """两个 QMT 去重表都是模块级的：不隔离会让用例之间互相"已经订阅/补过了"。

    订阅去重表在**中性层** `qmt_guard` 里（项目日线链与做T链共用一份，
    避免对同一个终端重复订阅），所以这里重置那一份。
    """
    from src.core import qmt_guard
    from src.intraday import sources as module

    qmt_guard.reset_subscriptions()
    monkeypatch.setattr(module, "_qmt_history_asked", {})
    yield
    qmt_guard.reset_subscriptions()


def test_warm_today_subscribes_instead_of_downloading(monkeypatch) -> None:
    """当日数据靠**订阅**拿，绝不进程内 download（那是会带走服务进程的接口）。

    实测（2026-09-16，全新标的 601318/000002）：
        订阅前            当日 1m = 0 行，覆盖 0 个交易日
        subscribe_quote('1m', count=-1) 1 秒后 → 当日 38 行，覆盖 133 个交易日
        subscribe_quote('5m', count=-1) 1 秒后 → 当日  8 行，覆盖 172 个交易日
    所以 count 必须传 -1（把历史一起推下来），否则历史还得走 2.34s 的隔离子进程。
    """
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    fake = _FakeXtdata()
    source = QmtMinuteSource()
    source._xtdata = fake  # noqa: SLF001

    assert source.warm_today("600036", "1m") is True
    assert fake.subscriptions == [("600036.SH", "1m", -1)]


def test_warm_today_is_deduplicated_within_ttl(monkeypatch) -> None:
    """同一 (标的, 周期) 在窗口内只订阅一次（订阅是持久的，正常只需一次）。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    fake = _FakeXtdata()
    source = QmtMinuteSource()
    source._xtdata = fake  # noqa: SLF001

    assert source.warm_today("600036", "1m") is True
    assert source.warm_today("600036", "1m") is False
    # 不同周期要各自订阅（每个周期的本地库是独立的）
    assert source.warm_today("600036", "5m") is True
    assert len(fake.subscriptions) == 2


def test_warm_today_survives_subscribe_failure(monkeypatch) -> None:
    """订阅失败不能把读取路径带崩（本地旧数据仍可能可用）。"""
    from src.core.exceptions import DataFetchError
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)

    class _Broken(_FakeXtdata):
        def subscribe_quote(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("终端未登录")

    source = QmtMinuteSource()
    source._xtdata = _Broken()  # noqa: SLF001
    assert source.warm_today("600036", "1m") is False

    class _NoXt(_FakeXtdata):
        def subscribe_quote(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise DataFetchError("xtquant未安装，请执行: uv sync --extra data")

    source2 = QmtMinuteSource()
    source2._xtdata = _NoXt()  # noqa: SLF001
    assert source2.warm_today("600036", "5m") is False


def test_first_subscription_waits_briefly_for_same_day_data(monkeypatch) -> None:
    """首建订阅后数据要 ~1 秒才落本地：这一轮要短暂等待，别急着降级到别的源。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    source = QmtMinuteSource()
    reads = {"n": 0}
    arrived = _qmt_raw_frame([TODAY])

    def _read():
        reads["n"] += 1
        return arrived if reads["n"] >= 3 else _qmt_raw_frame([YESTERDAY])

    frame = source._await_today(_qmt_raw_frame([YESTERDAY]), _read)

    assert reads["n"] == 3  # 第 3 次读到当日数据立刻返回，不等满 1.2 秒
    assert QmtMinuteSource._covered_trading_days(frame) == 1  # noqa: SLF001
    assert str(frame.index[-1])[:8] == TODAY.replace("-", "")


def test_await_today_gives_up_after_deadline(monkeypatch) -> None:
    """等不到就返回现状（交给新鲜度闸门去降级），不能无限等。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    source = QmtMinuteSource()

    frame = source._await_today(
        _qmt_raw_frame([YESTERDAY]), lambda: _qmt_raw_frame([YESTERDAY]))

    assert str(frame.index[-1])[:8] == YESTERDAY.replace("-", "")


def test_await_today_is_skipped_without_a_session_clock(monkeypatch) -> None:
    """时钟不可用时不做等待（等谁都不知道，白等 1.2 秒）。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: "")
    source = QmtMinuteSource()
    reads = {"n": 0}

    def _read():
        reads["n"] += 1
        return _qmt_raw_frame([])

    source._await_today(_qmt_raw_frame([YESTERDAY]), _read)

    assert reads["n"] == 0


def test_qmt_source_still_warms_when_tencent_is_the_answer() -> None:
    """回归：闸门降级后 QMT 也不能停止补下载，否则永远回不到首选。"""
    qmt = _FakeSource(_trend(YESTERDAY))
    provider = _provider(qmt=qmt, tencent=_FakeSource(_trend(TODAY)))
    asyncio_run(provider.fetch_trend("600036"))
    assert qmt.calls == 1  # 每次请求都要试 QMT（补下载在源内部完成）


# ==================== QMT 历史覆盖不足（预热引入的回归） ====================


def _qmt_raw_frame(dates: list[str]) -> pd.DataFrame:
    """模拟 QMT 原始分钟帧：索引是 `YYYYMMDDHHMMSS`（`_frame_to_bars` 靠它取时间）。"""
    return pd.DataFrame(
        {"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
         "volume": 100.0, "amount": 1000.0},
        index=[f"{date.replace('-', '')}093500" for date in dates])


class _FakeXtdataHistory(_FakeXtdata):
    """按读取次序返回预设帧的 xtquant 替身。"""

    def __init__(self, frames: list[pd.DataFrame]) -> None:
        super().__init__()
        self.frames = frames
        self.reads = 0

    def get_market_data_ex(self, *args, **kwargs):  # noqa: ANN002, ANN003
        codes = args[1] if len(args) > 1 else []
        frame = self.frames[min(self.reads, len(self.frames) - 1)]
        self.reads += 1
        return {code: frame for code in codes}


def _history_stub(monkeypatch, result: tuple[bool, str] = (True, "")) -> list[str]:
    """替换隔离子进程补下载，返回调用记录列表。"""
    from src.core import qmt_guard

    calls: list[str] = []

    def _fake(qmt_code, period, **kwargs):  # noqa: ANN001, ANN003
        calls.append(f"{qmt_code}:{period}")
        return result

    monkeypatch.setattr(qmt_guard, "download_history_isolated", _fake)
    return calls


def test_load_downloads_history_when_coverage_is_short(monkeypatch) -> None:
    """回归：预热把"空库"变成"只有今天"，不能因此就永远不补历史。

    实测现场：600030 的 5m 只回 7 根（当天），而它本可以有 800 多根 ——
    分钟指标与阈值回测都会因此失真。
    """
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    calls = _history_stub(monkeypatch)
    thin = _qmt_raw_frame([TODAY.replace("-", "")])
    full = _qmt_raw_frame([f"2026-09-{day:02d}" for day in (10, 11, 12, 15, 16)])
    source = QmtMinuteSource()
    source._xtdata = _FakeXtdataHistory([thin, full])  # noqa: SLF001

    frame = source._load("600030", "5m", 5)  # noqa: SLF001

    assert calls == ["600030.SH:5m"]
    assert QmtMinuteSource._covered_trading_days(frame) == 5  # noqa: SLF001


def test_load_skips_history_download_when_coverage_is_enough(monkeypatch) -> None:
    """覆盖够了就不该再花 2.34s 起子进程（这是盘中刷新的主要开销之一）。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    calls = _history_stub(monkeypatch)
    full = _qmt_raw_frame([f"2026-09-{day:02d}" for day in (10, 11, 12, 15, 16)])
    source = QmtMinuteSource()
    source._xtdata = _FakeXtdataHistory([full])  # noqa: SLF001

    source._load("600030", "5m", 5)  # noqa: SLF001

    assert calls == []


def test_trend_only_needs_one_trading_day(monkeypatch) -> None:
    """分时只要当天：`_load(1m, days=1)` 不该因为"只有一天"去补多年份历史。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    calls = _history_stub(monkeypatch)
    source = QmtMinuteSource()
    source._xtdata = _FakeXtdataHistory(  # noqa: SLF001
        [_qmt_raw_frame([TODAY.replace("-", "")])])

    source._load("600030", "1m", 1)  # noqa: SLF001

    assert calls == []


def test_history_download_is_deduplicated(monkeypatch) -> None:
    """次新股历史天生不够：不能每个请求都背一次两秒多的隔离子进程开销。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    calls = _history_stub(monkeypatch)
    thin = _qmt_raw_frame([TODAY.replace("-", "")])
    source = QmtMinuteSource()
    source._xtdata = _FakeXtdataHistory([thin])  # noqa: SLF001

    source._load("688825", "5m", 5)  # noqa: SLF001
    source._load("688825", "5m", 5)  # noqa: SLF001

    assert len(calls) == 1


def test_short_frame_survives_failed_history_download(monkeypatch) -> None:
    """已有部分数据时补下载失败要降级返回，不能把整条链打断。"""
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: TODAY)
    _history_stub(monkeypatch, result=(False, "终端忙"))
    thin = _qmt_raw_frame([TODAY.replace("-", "")])
    source = QmtMinuteSource()
    source._xtdata = _FakeXtdataHistory([thin])  # noqa: SLF001

    frame = source._load("600030", "5m", 5)  # noqa: SLF001

    assert len(frame) == 1


def test_empty_frame_still_raises_when_history_download_fails(monkeypatch) -> None:
    """原有语义不能变：本地空 + 补下载失败 → DataFetchError，好让链上别的源接手。"""
    from src.core.exceptions import DataFetchError
    from src.intraday import sources as module

    monkeypatch.setattr(module, "live_session_date", lambda **_: "")
    _history_stub(monkeypatch, result=(False, "终端未登录"))
    source = QmtMinuteSource()
    source._xtdata = _FakeXtdataHistory([pd.DataFrame()])  # noqa: SLF001

    with pytest.raises(DataFetchError):
        source._load("600030", "5m", 5)  # noqa: SLF001


def test_covered_trading_days_counts_distinct_dates() -> None:
    frame = _qmt_raw_frame(["20260915", "20260915", "20260916"])
    assert QmtMinuteSource._covered_trading_days(frame) == 2  # noqa: SLF001
    assert QmtMinuteSource._covered_trading_days(pd.DataFrame()) == 0  # noqa: SLF001
    assert QmtMinuteSource._covered_trading_days(object()) == 0  # noqa: SLF001


# ==================== 既有行为不能破 ====================


def test_all_sources_failing_still_raises() -> None:
    """没有一个源返回数据时仍然是 DataFetchError，不能被"兜底候选"吞掉。"""
    from src.core.exceptions import DataFetchError

    provider = _provider(
        qmt=_FakeSource(None, error="终端未启动"),
        tencent=_FakeSource(None, error="网络异常"),
        eastmoney=_FakeSource(None), sina=_FakeSource(None, error="限流"))

    with pytest.raises(DataFetchError) as excinfo:
        asyncio_run(provider.fetch_trend("600036"))
    assert "全部数据源失败" in str(excinfo.value)


def test_attempt_records_are_returned_in_chain_order() -> None:
    provider = _provider(
        qmt=_FakeSource(_trend(YESTERDAY)), tencent=_FakeSource(_trend(TODAY)))
    _, _, attempts = asyncio_run(provider.fetch_trend("600036"))
    assert [a.source for a in attempts][:2] == ["迅投QMT", "腾讯行情"]


def test_source_attempt_is_mutable_so_gate_can_mark_failure() -> None:
    """闸门依赖 SourceAttempt 可写（复制粘贴改字段不会静默失效）。"""
    attempt = SourceAttempt(source="迅投QMT", ok=True, rows=3, detail="ok")
    attempt.ok = False
    attempt.detail = "陈旧"
    assert attempt.ok is False and attempt.detail == "陈旧"


def test_series_date_handles_date_only_and_year_rollover() -> None:
    """只有日期的 `ts`（日线口径）也要能取出交易日；跨年仍是正确的字符串序。"""
    assert series_date(pd.DataFrame({"ts": ["2026-09-16"]})) == "2026-09-16"
    frame = pd.DataFrame({"ts": ["2025-12-31 14:55", "2026-01-05 09:31"]})
    assert series_date(frame) == "2026-01-05"
