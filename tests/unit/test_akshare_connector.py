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


# ==================== 新浪回退路径的两个静默错误（实测回归） ====================
#
# 实测（2026-09-22）踩到两个都**不报错**的问题，用户只会看到"数据少了一截/量柱高 100 倍"：
#   1. 区间过滤把 boolean Series 拿去 reindex：先按起点过滤再按止点过滤，
#      第二个掩码带着原表 RangeIndex，未对齐位置一律 False →
#      2020-01-01~2026-09-22 的查询被静默截断成 2020-01-02~2025-12-31；
#   2. 新浪成交量单位是**股**（实测 600036 同日 43,342,069 ↔ 腾讯 433,421 手 = 100 倍），
#      不换算就是 100 倍级静默误差。
# 下面两条用例把修复钉死（都用假 akshare，不联网）。


class _FakeSinaAk:
    """假 akshare：东财必失败，新浪返回一段跨 2025/2026 的全历史帧。"""

    def __init__(self, dates: list[str] | None = None) -> None:
        self.dates = dates or [
            "2019-12-31", "2020-01-02", "2020-01-03",
            "2025-12-31", "2026-01-05", "2026-09-22",
        ]

    def stock_zh_a_hist(self, symbol, period, start_date, end_date):
        raise ConnectionError("Remote end closed connection without response")

    def stock_zh_a_daily(self, symbol, adjust):
        return pd.DataFrame({
            "date": self.dates,
            "open": [10.0] * len(self.dates),
            "high": [11.0] * len(self.dates),
            "low": [9.0] * len(self.dates),
            "close": [10.5] * len(self.dates),
            "volume": [43_342_069.0] * len(self.dates),
            "amount": [1_770_009_799.0] * len(self.dates),
        })


def test_sina_range_filter_keeps_2026_rows(monkeypatch):
    """区间过滤必须保住 2026 的行（修复前会被 reindex 静默截断在 2025-12-31）。"""
    monkeypatch.setitem(sys.modules, "akshare", _FakeSinaAk())
    df = AkshareConnector()._load_dataframe(
        "stock_close:600036", "2020-01-01", "2026-09-22")
    dates = [str(d) for d in df["日期"]]
    assert dates == ["2020-01-02", "2020-01-03", "2025-12-31",
                     "2026-01-05", "2026-09-22"]
    assert max(dates) == "2026-09-22"


def test_sina_volume_converted_to_lots(monkeypatch):
    """新浪 volume 是股 → 统一折成手（÷100），避免与东财/腾讯/Tushare 差 100 倍。"""
    monkeypatch.setitem(sys.modules, "akshare", _FakeSinaAk())
    df = AkshareConnector()._load_dataframe("stock_close:600036", None, None)
    assert df["volume"].iloc[0] == pytest.approx(433_420.69)
    # 中文列名也写一份，下游 daily_bars_from_points 的别名表两种都能命中
    assert df["成交量"].iloc[0] == pytest.approx(433_420.69)
    # 成交额单位本来就是元，**不得**被换算
    assert df["amount"].iloc[0] == pytest.approx(1_770_009_799.0)
    points = df_to_data_points(df, "stock_close:600036", "AkShare", "x")
    assert points[0].extra["volume"] == pytest.approx(433_420.69)


def test_index_and_etf_range_filter_also_keeps_latest(monkeypatch):
    """指数/ETF 走 _filter_frame_dates，同样不能把最新一段丢掉。"""
    class _FakeIndexAk:
        def stock_zh_index_daily(self, symbol):
            return pd.DataFrame({
                "date": ["2019-12-31", "2020-01-02", "2025-12-31", "2026-09-22"],
                "open": [1.0] * 4, "high": [1.0] * 4,
                "low": [1.0] * 4, "close": [1.0] * 4,
                "volume": [17_786_387_600.0] * 4,
            })

    monkeypatch.setitem(sys.modules, "akshare", _FakeIndexAk())
    df = AkshareConnector()._load_dataframe(
        "index_close:000300", "2020-01-01", "2026-09-22")
    dates = [str(d) for d in df["日期"]]
    assert dates == ["2020-01-02", "2025-12-31", "2026-09-22"]
    # 指数新浪 volume 同样是股 → 折手
    assert df["volume"].iloc[-1] == pytest.approx(177_863_876.0)
