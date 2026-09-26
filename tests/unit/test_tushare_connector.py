"""Tushare 日线连接器测试（离线：用假 client，不打网络）。

要钉住的是**口径**而不是"能不能连上"：
  1. 代码 → ts_code 的市场后缀按指标类别分派（个股/指数/ETF 三套规则），
     类别与代码段不符必须明确拒绝（不能拼错码换回空结果）；
  2. 输出必须**升序**（Tushare 原始是倒序，下游缠论/量柱全假定升序）；
  3. 单位换算：vol(手)→volume、amount(千元)→元；**指数例外**（vol 是股 → ÷100 折手）；
  4. 个股前复权优先，权限不足时降级不复权并**如实标注** extra.adjust；
     指数/ETF 走原生接口，恒标 `none`（不冒充前复权）；
  5. 区间外无数据 → DataFetchError（而不是空列表冒充成功）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.tushare_connector import (
    TushareConnector,
    frame_to_points,
    kind_of,
    to_ts_code,
    units_for,
)


class _FakePro:
    """pro_bar / client.call 的替身。"""

    def __init__(self, frame: pd.DataFrame | None = None,
                 raise_on_bar: bool = False) -> None:
        self.frame = frame
        self.raise_on_bar = raise_on_bar
        self.calls: list[tuple] = []


class _FakeClient:
    def __init__(self, *, qfq_frame: pd.DataFrame | None = None,
                 daily_frame: pd.DataFrame | None = None,
                 api_frames: dict[str, pd.DataFrame] | None = None,
                 raise_on_bar: bool = False) -> None:
        self.pro = _FakePro(raise_on_bar=raise_on_bar)
        self.qfq_frame = qfq_frame
        self.daily_frame = daily_frame
        self.api_frames = api_frames or {}
        self.calls: list[dict] = []

    def call(self, api: str, **params):
        self.calls.append({"api": api, **params})
        if api in self.api_frames:
            return self.api_frames[api]
        return self.daily_frame if self.daily_frame is not None else pd.DataFrame()


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


_SAMPLE = [
    {"ts_code": "300308.SZ", "trade_date": "20260916", "open": 869.02,
     "high": 918.38, "low": 866.00, "close": 907.80, "pre_close": 864.01,
     "pct_chg": 5.07, "vol": 240218.46, "amount": 21471490.0},
    {"ts_code": "300308.SZ", "trade_date": "20260915", "open": 873.38,
     "high": 883.73, "low": 858.00, "close": 864.01, "pre_close": 873.00,
     "pct_chg": -1.03, "vol": 179554.41, "amount": 15613590.0},
]

# 指数帧：000300.SH 2026-09-18 实测 vol=189924166（股）、amount=537699359.2269（千元）
_INDEX_SAMPLE = [
    {"ts_code": "000300.SH", "trade_date": "20260918", "close": 4507.3926,
     "open": 4492.3233, "high": 4523.0915, "low": 4480.5856,
     "pre_close": 4460.1557, "change": 47.2369, "pct_chg": 1.0591,
     "vol": 189924166.0, "amount": 537699359.2269},
]

# ETF 帧：510300.SH 2026-09-18 实测 vol=6297211.23（手）、amount=2877462.543（千元）
_ETF_SAMPLE = [
    {"ts_code": "510300.SH", "trade_date": "20260918", "pre_close": 4.532,
     "open": 4.556, "high": 4.596, "low": 4.548, "close": 4.582,
     "change": 0.05, "pct_chg": 1.1, "vol": 6297211.23,
     "amount": 2877462.543},
]


# ==================== 代码映射 ====================

@pytest.mark.parametrize(
    ("code", "expected"),
    [("300308", "300308.SZ"), ("002463", "002463.SZ"), ("600036", "600036.SH"),
     ("688981", "688981.SH"), ("000001", "000001.SZ"), ("920001", "920001.BJ"),
     ("830799", "830799.BJ")],
)
def test_to_ts_code_mapping(code: str, expected: str) -> None:
    assert to_ts_code(code) == expected


@pytest.mark.parametrize("bad", ["30030", "abcdef", "", "588170", "510300"])
def test_to_ts_code_rejects_bad_or_etf(bad: str) -> None:
    """个股路径下 ETF(5xxxxx/1xxxxx) 必须明确拒绝 —— 拼成 ts_code 去换回空结果无法与停牌区分。"""
    with pytest.raises(DataFetchError):
        to_ts_code(bad)


@pytest.mark.parametrize(
    ("code", "expected"),
    [("510300", "510300.SH"), ("588170", "588170.SH"), ("560050", "560050.SH"),
     ("159915", "159915.SZ"), ("161725", "161725.SZ")],
)
def test_to_ts_code_etf_mapping(code: str, expected: str) -> None:
    """ETF 走基金接口：51/56/58 沪，15/16 深（588170 是沪市科创ETF）。"""
    assert to_ts_code(code, kind="etf") == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [("000300", "000300.SH"), ("000001", "000001.SH"),
     ("880001", "880001.SH"), ("399006", "399006.SZ")],
)
def test_to_ts_code_index_mapping(code: str, expected: str) -> None:
    """指数不能用个股规则：000300 若拼成 .SZ 会换回一个空结果（与停牌无法区分）。"""
    assert to_ts_code(code, kind="index") == expected


@pytest.mark.parametrize(
    ("code", "kind"),
    [("30030", "etf"), ("600036", "etf"), ("500001", "etf"),
     ("600036", "index"), ("300308", "index"), ("920001", "index"),
     ("510300", "index")],
)
def test_to_ts_code_rejects_kind_mismatch(code: str, kind: str) -> None:
    with pytest.raises(DataFetchError):
        to_ts_code(code, kind=kind)


def test_kind_of_maps_prefixes_and_rejects_others() -> None:
    assert kind_of("stock_close:300308") == "stock"
    assert kind_of("index_close:000300") == "index"
    assert kind_of("etf_close:510300") == "etf"
    for bad in ("CPI", "stock_close:", "mkt:turnover", "PE(TTM):600036"):
        with pytest.raises(DataFetchError):
            kind_of(bad)


# ==================== 帧转换（口径） ====================

def test_frame_to_points_is_ascending_and_converts_units() -> None:
    points = frame_to_points(_frame(_SAMPLE), "stock_close:300308",
                             ts_code="300308.SZ", adjust="qfq")
    # Tushare 原始是倒序 → 输出必须升序
    assert [p.period_date for p in points] == ["2026-09-15", "2026-09-16"]
    latest = points[-1]
    assert latest.value == pytest.approx(907.80)
    assert latest.extra["open"] == pytest.approx(869.02)
    assert latest.extra["volume"] == pytest.approx(240218.46)      # 手，原样
    assert latest.extra["amount"] == pytest.approx(21471490.0 * 1000)  # 千元→元
    assert latest.extra["adjust"] == "qfq"
    assert latest.extra["ts_code"] == "300308.SZ"


def test_frame_to_points_index_volume_is_lots_not_scaled() -> None:
    """指数 `vol` 原样即为**手**，与腾讯逐位相同 —— 不得再做任何缩放。

    这条是"好心帮倒忙"的护栏：曾按"指数 vol 是股、要 ÷1000"改过一次，
    指数成交量会凭空小 1000 倍。真值即 1.7786 亿手。
    实测依据（2026-09-22，000300.SH 同日四源对照）：
      Tushare index_daily vol=177,863,876（手）
      腾讯 fqkline 指数 成交量=177,863,876（手，**完全相等**）
      baostock volume=17,786,387,600（股 ÷1000 = 177,863,876 手）
      AkShare 新浪指数 volume=17,786,387,600（股）
      成交额：Tushare 523,616,704.7562 千元 ×1000 == baostock 523,616,704,756.2 元
    """
    points = frame_to_points(_frame(_INDEX_SAMPLE), "index_close:000300",
                             ts_code="000300.SH", adjust="none",
                             units=units_for("index"))
    latest = points[-1]
    assert latest.value == pytest.approx(4507.3926)
    assert latest.extra["volume"] == pytest.approx(189924166.0)   # 原样，不缩放
    assert latest.extra["amount"] == pytest.approx(537699359.2269 * 1000)  # 千元→元
    assert latest.extra["adjust"] == "none", "指数不除权，绝不能标成 qfq"
    assert latest.extra["volume_unit"] == "手"


def test_frame_to_points_etf_keeps_lots_and_converts_amount() -> None:
    points = frame_to_points(_frame(_ETF_SAMPLE), "etf_close:510300",
                             ts_code="510300.SH", adjust="none",
                             units=units_for("etf"))
    latest = points[-1]
    assert latest.extra["volume"] == pytest.approx(6297211.23)   # 手，原样
    assert latest.extra["amount"] == pytest.approx(2877462.543 * 1000)
    assert latest.extra["volume_unit"] == "手"


def test_frame_to_points_filters_range_and_skips_bad_rows() -> None:
    rows = [*_SAMPLE, {"trade_date": "bad-date", "close": 1.0},
            {"trade_date": "20260101", "close": None}]
    points = frame_to_points(_frame(rows), "stock_close:300308",
                             ts_code="300308.SZ", adjust="qfq",
                             start_date="2026-09-16", end_date="2026-09-17")
    assert [p.period_date for p in points] == ["2026-09-16"]


def test_frame_to_points_empty_frame_returns_empty() -> None:
    assert frame_to_points(None, "stock_close:300308",
                           ts_code="300308.SZ", adjust="qfq") == []
    assert frame_to_points(pd.DataFrame(), "stock_close:300308",
                           ts_code="300308.SZ", adjust="qfq") == []


# ==================== 连接器行为 ====================

def test_supports_stock_index_and_etf_close() -> None:
    assert TushareConnector.supports("stock_close:300308")
    assert TushareConnector.supports("index_close:000300")
    assert TushareConnector.supports("etf_close:588170")
    assert not TushareConnector.supports("CPI")


@pytest.mark.asyncio
async def test_fetch_prefers_qfq(monkeypatch) -> None:
    client = _FakeClient(qfq_frame=_frame(_SAMPLE))
    monkeypatch.setattr(
        TushareConnector, "_daily_frame",
        staticmethod(lambda c, ts_code, s, e: (client.qfq_frame, "qfq")))
    connector = TushareConnector(client=client)
    points = await connector.fetch("stock_close:300308", "2026-09-01", "2026-09-17")
    assert [p.period_date for p in points] == ["2026-09-15", "2026-09-16"]
    assert points[-1].extra["adjust"] == "qfq"
    assert points[-1].source_name == "Tushare Pro"


@pytest.mark.asyncio
async def test_fetch_degrades_to_unadjusted_with_label(monkeypatch) -> None:
    """前复权不可用时降级不复权，但必须在 extra.adjust 标出来（不静默换口径）。"""
    client = _FakeClient(daily_frame=_frame(_SAMPLE))
    monkeypatch.setattr(
        TushareConnector, "_daily_frame",
        staticmethod(lambda c, ts_code, s, e: (client.daily_frame, "none")))
    connector = TushareConnector(client=client)
    points = await connector.fetch("stock_close:300308")
    assert points[-1].extra["adjust"] == "none"


@pytest.mark.asyncio
async def test_fetch_index_uses_index_daily_and_labels_none() -> None:
    """指数走 index_daily（点数口径），标 none，且不得落到个股 daily 接口。"""
    client = _FakeClient(api_frames={"index_daily": _frame(_INDEX_SAMPLE)})
    points = await TushareConnector(client=client).fetch(
        "index_close:000300", "2026-09-01", "2026-09-30")
    assert points[-1].extra["adjust"] == "none"
    assert points[-1].extra["ts_code"] == "000300.SH"
    assert client.calls[0]["api"] == "index_daily"
    assert client.calls[0]["ts_code"] == "000300.SH"


@pytest.mark.asyncio
async def test_fetch_etf_uses_fund_daily_and_labels_none() -> None:
    client = _FakeClient(api_frames={"fund_daily": _frame(_ETF_SAMPLE)})
    points = await TushareConnector(client=client).fetch(
        "etf_close:510300", "2026-09-01", "2026-09-30")
    assert points[-1].extra["adjust"] == "none"
    assert points[-1].extra["ts_code"] == "510300.SH"
    assert client.calls[0]["api"] == "fund_daily"


@pytest.mark.asyncio
async def test_fetch_raises_when_no_rows_in_range(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(
        TushareConnector, "_daily_frame",
        staticmethod(lambda c, ts_code, s, e: (pd.DataFrame(), "qfq")))
    connector = TushareConnector(client=client)
    with pytest.raises(DataFetchError, match="无"):
        await connector.fetch("stock_close:300308", "2026-09-01", "2026-09-17")


@pytest.mark.asyncio
async def test_fetch_raises_when_index_frame_empty() -> None:
    """指数区间无数据也必须抛错，不能用空列表冒充成功。"""
    client = _FakeClient(api_frames={"index_daily": pd.DataFrame()})
    with pytest.raises(DataFetchError, match="无"):
        await TushareConnector(client=client).fetch("index_close:399006")


@pytest.mark.asyncio
async def test_fetch_rejects_unsupported_indicator() -> None:
    connector = TushareConnector(client=_FakeClient())
    with pytest.raises(DataFetchError, match="不支持"):
        await connector.fetch("CPI")


def test_connector_constructs_without_token() -> None:
    """没有 token 时构造连接器不能失败 —— 它只是兜底，不该拖垮采集链装配。"""
    connector = TushareConnector()
    assert connector.get_capabilities()["indicators"] == [
        "stock_close:{code}", "index_close:{code}", "etf_close:{code}"]


def test_daily_frame_falls_back_from_pro_bar_to_daily(monkeypatch) -> None:
    """pro_bar 抛错（无 adj_factor 权限）→ 降级 client.call('daily')。"""
    client = _FakeClient(daily_frame=_frame(_SAMPLE))

    def _boom(**kwargs):
        raise RuntimeError("没有 adj_factor 权限")

    monkeypatch.setattr("tushare.pro_bar", _boom, raising=False)
    frame, adjust = TushareConnector._daily_frame(  # noqa: SLF001
        client, "300308.SZ", "20260901", "20260917")
    assert adjust == "none"
    assert len(frame) == 2
    assert client.calls and client.calls[0]["api"] == "daily"
