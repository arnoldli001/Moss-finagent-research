"""Tushare 日线连接器测试（离线：用假 client，不打网络）。

要钉住的是**口径**而不是"能不能连上"：
  1. 代码 → ts_code 的市场后缀正确，ETF 必须明确拒绝（不能拼错码换回空结果）；
  2. 输出必须**升序**（Tushare 原始是倒序，下游缠论/量柱全假定升序）；
  3. 单位换算：vol(手)→volume、amount(千元)→元；
  4. 前复权优先，权限不足时降级不复权并**如实标注** extra.adjust；
  5. 区间外无数据 → DataFetchError（而不是空列表冒充成功）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.tushare_connector import (
    TushareConnector,
    frame_to_points,
    to_ts_code,
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
                 raise_on_bar: bool = False) -> None:
        self.pro = _FakePro(raise_on_bar=raise_on_bar)
        self.qfq_frame = qfq_frame
        self.daily_frame = daily_frame
        self.calls: list[dict] = []

    def call(self, api: str, **params):
        self.calls.append({"api": api, **params})
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
    """ETF(5xxxxx/1xxxxx) 必须明确拒绝 —— 拼成 ts_code 去换回空结果无法与停牌区分。"""
    with pytest.raises(DataFetchError):
        to_ts_code(bad)


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

def test_supports_only_stock_close() -> None:
    assert TushareConnector.supports("stock_close:300308")
    assert not TushareConnector.supports("index_close:000001")
    assert not TushareConnector.supports("etf_close:588170")
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
async def test_fetch_raises_when_no_rows_in_range(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(
        TushareConnector, "_daily_frame",
        staticmethod(lambda c, ts_code, s, e: (pd.DataFrame(), "qfq")))
    connector = TushareConnector(client=client)
    with pytest.raises(DataFetchError, match="无"):
        await connector.fetch("stock_close:300308", "2026-09-01", "2026-09-17")


@pytest.mark.asyncio
async def test_fetch_rejects_unsupported_indicator() -> None:
    connector = TushareConnector(client=_FakeClient())
    with pytest.raises(DataFetchError, match="不支持"):
        await connector.fetch("index_close:000001")


def test_connector_constructs_without_token() -> None:
    """没有 token 时构造连接器不能失败 —— 它只是兜底，不该拖垮采集链装配。"""
    connector = TushareConnector()
    assert connector.get_capabilities()["indicators"] == ["stock_close:{code}"]


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
