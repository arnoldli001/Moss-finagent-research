"""AkShare连接器测试：df转换纯函数用真实pandas小样本；不联网。"""

import sys

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.akshare_connector import (
    AkshareConnector,
    df_to_data_points,
)


class _FakeAk:
    """假akshare：东财行情可注入故障，记录新浪回退调用。"""

    def __init__(self, hist_error: bool = False, daily_error: bool = False):
        self.hist_symbol: str | None = None
        self.daily_call: tuple | None = None
        self._hist_error = hist_error
        self._daily_error = daily_error

    def stock_zh_a_hist(self, symbol, period, start_date, end_date):
        self.hist_symbol = symbol
        if self._hist_error:
            raise ConnectionError("Remote end closed connection")
        return pd.DataFrame({"日期": ["2026-09-10"], "收盘": [10.5]})

    def stock_zh_a_daily(self, symbol, adjust):
        self.daily_call = (symbol, adjust)
        if self._daily_error:
            raise ConnectionError("sina down")
        return pd.DataFrame(
            {"date": ["2026-09-10"], "open": [10.0], "close": [10.5]}
        )


def _sample_macro_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "月份": ["2026-08", "2026-07"],
            "全国同比": [2.1, 2.0],
            "全国环比": [0.3, 0.1],
        }
    )


def test_df_to_data_points_row_per_point():
    points = df_to_data_points(_sample_macro_df(), "CPI", "AkShare", "https://x")

    assert len(points) == 2
    assert points[0].indicator == "CPI"
    assert points[0].value == 2.1  # 优先"同比"列
    assert points[0].period_date == "2026-08"
    assert points[0].source_name == "AkShare"
    assert points[0].confidence == 0.8
    assert points[0].extra["全国环比"] == 0.3
    assert len(points[0].raw_content_hash) == 64


def test_df_to_data_points_empty():
    assert df_to_data_points(pd.DataFrame(), "CPI", "s", "u") == []


def test_stock_df_picks_close_column():
    df = pd.DataFrame({"日期": ["2026-09-10"], "开盘": [10.0], "收盘": [10.5]})
    points = df_to_data_points(df, "stock_close:000001", "AkShare", "https://x")
    assert points[0].value == 10.5  # 优先"收盘"列


def test_macro_report_format_picks_jinzhi_column():
    """2026版akshare宏观接口结构：商品/日期/今值/预测值/前值。"""
    df = pd.DataFrame(
        {
            "商品": ["中国CPI月率报告"],
            "日期": ["2026-08-01"],
            "今值": [2.1],
            "预测值": [2.0],
            "前值": [1.9],
        }
    )
    points = df_to_data_points(df, "CPI", "AkShare", "https://x")
    assert points[0].value == 2.1  # "今值"关键词命中，而非文本列"商品"
    assert points[0].period_date == "2026-08-01"


def test_unsupported_indicator_raises():
    connector = AkshareConnector()
    with pytest.raises(DataFetchError):
        connector._load_dataframe("GDP", None, None)


def test_capabilities():
    caps = AkshareConnector().get_capabilities()
    assert caps["name"] == "AkShare"
    assert "CPI" in caps["indicators"]


def test_stock_primary_eastmoney_no_fallback(monkeypatch):
    fake = _FakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    df = AkshareConnector()._load_dataframe("stock_close:601088", None, None)
    assert fake.hist_symbol == "601088"
    assert fake.daily_call is None
    assert "收盘" in df.columns


def test_stock_falls_back_to_sina_with_prefix(monkeypatch):
    fake = _FakeAk(hist_error=True)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    df = connector._load_dataframe("stock_close:601088", None, None)
    assert fake.daily_call == ("sh601088", "qfq")
    assert "日期" in df.columns and "收盘" in df.columns
    assert df_to_data_points(df, "stock_close:601088", "AkShare", "x")[0].value == 10.5

    fake2 = _FakeAk(hist_error=True)
    monkeypatch.setitem(sys.modules, "akshare", fake2)
    connector._load_dataframe("stock_close:000001", None, None)
    assert fake2.daily_call[0] == "sz000001"


def test_stock_both_sources_fail_raises(monkeypatch):
    fake = _FakeAk(hist_error=True, daily_error=True)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    with pytest.raises(ConnectionError):
        AkshareConnector()._load_dataframe("stock_close:601088", None, None)


def test_sina_symbol_prefix():
    assert AkshareConnector._sina_symbol("601088") == "sh601088"
    assert AkshareConnector._sina_symbol("000001") == "sz000001"
