"""AkShare扩展指标测试：宏观/估值/财务/真实产业序列，Fake akshare不联网。"""

import io
import sys
from datetime import date

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors import akshare_connector as ac_module
from src.infrastructure.connectors.akshare_connector import (
    AkshareConnector,
    _in_range,
    _is_etf_code,
    period_to_iso,
    series_to_points,
)


def test_period_to_iso_variants():
    assert period_to_iso("2026年07月份") == "2026-07"
    assert period_to_iso("201501") == "2015-01"
    assert period_to_iso("2026-09-11") == "2026-09-11"
    assert period_to_iso(date(2026, 3, 31)) == "2026-03-31"


def test_in_range_month_boundaries():
    assert _in_range("2026-09", "2026-01", "2026-12")
    assert not _in_range("2025-12", "2026-01", None)
    assert _in_range("2026-01-15", "2026-01", "2026-01")  # 月边界按整月
    assert not _in_range("2026-02-01", "2026-01", "2026-01")


def test_series_to_points_m2_shape():
    """AkShare 给的宏观序列是**新→旧**，连接器必须转成**升序**再交出。

    这条以前断言的是 `[7.7, 8.0]`（即把 AkShare 的原生降序透出去）—— 等于把
    "同一指标走网络是降序、命中本地 DB 短路却是升序"这个不一致**写进了测试**，
    于是没人再发现它。统一数据层与其余连接器都是升序，这里跟随升序。
    """
    df = pd.DataFrame({
        "月份": ["2026年07月份", "2026年06月份"],
        "货币和准货币(M2)-数量(亿元)": [3555077.0, 3567108.0],
        "货币和准货币(M2)-同比增长": [7.7, 8.0],
    })
    points = series_to_points(
        df, "M2", date_keywords=("月份",),
        value_keywords=("货币和准货币(M2)-同比增长",),
        start_date=None, end_date=None, confidence=0.8)
    assert [p.value for p in points] == [8.0, 7.7]          # 06月 → 07月
    periods = [p.period_date for p in points]
    assert periods == sorted(periods)
    assert points[0].period_date == "2026-06"
    assert points[-1].period_date == "2026-07"


def test_supports_new_indicators():
    assert AkshareConnector.supports("M2")
    assert AkshareConnector.supports("社融")
    assert AkshareConnector.supports("PE(TTM):601088")
    assert AkshareConnector.supports("PB:601088")
    assert AkshareConnector.supports("资产负债率:601088")
    assert AkshareConnector.supports("流动比率:601088")
    assert AkshareConnector.supports("ind:社会消费品零售总额同比")
    assert AkshareConnector.supports("ind:动力煤价格(元/吨)")
    # 科技/消费/周期/医药行业PE 等已全部由真实产业连接器接管，AkShare 不管这些
    assert not AkshareConnector.supports("ind:科技行业PE(TTM)")
    assert not AkshareConnector.supports("ind:消费行业PE(TTM)")


class _FakeAk:
    def __init__(self):
        self.baidu_calls = []
        self.fin_calls = []

    def macro_china_money_supply(self):
        return pd.DataFrame({
            "月份": ["2026年07月份"],
            "货币和准货币(M2)-同比增长": [7.7],
        })

    def futures_main_sina(self, symbol, start_date=None):
        assert symbol == "ZC0"
        return pd.DataFrame({
            "日期": [date(2026, 8, 31)],
            "开盘价": [690.0], "收盘价": [702.0],
        })

    def stock_zh_valuation_baidu(self, symbol, indicator, period):
        self.baidu_calls.append((symbol, indicator, period))
        return pd.DataFrame({"date": [date(2026, 9, 11)], "value": [13.94]})

    def stock_financial_analysis_indicator(self, symbol, start_year):
        self.fin_calls.append((symbol, start_year))
        return pd.DataFrame({"日期": [date(2026, 6, 30)], "流动比率": [1.71]})


def test_m2_via_fake(monkeypatch):
    fake = _FakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    points = AkshareConnector()._fetch_sync("M2", None, None)
    assert points[0].value == 7.7
    assert points[0].period_date == "2026-07"


def test_coal_price_proxy_disclosed(monkeypatch):
    fake = _FakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    points = AkshareConnector()._fetch_sync("ind:动力煤价格(元/吨)", None, None)
    assert points[0].value == 702.0
    assert "proxy" in points[0].extra
    assert points[0].confidence < 0.8  # 代理口径降置信度


def test_pe_and_financial_dispatch(monkeypatch):
    fake = _FakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    pe = connector._fetch_sync("PE(TTM):601088", None, None)
    assert pe[0].value == 13.94
    assert fake.baidu_calls[-1][:2] == ("601088", "市盈率(TTM)")

    pb = connector._fetch_sync("PB:601088", None, None)
    assert fake.baidu_calls[-1][1] == "市净率"
    assert pb[0].indicator == "PB:601088"

    cr = connector._fetch_sync("流动比率:601088", None, None)
    assert cr[0].value == 1.71
    assert cr[0].extra["frequency"] == "quarterly"
    assert fake.fin_calls[0][0] == "601088"


def test_structural_drift_raises(monkeypatch):
    class _Broken(_FakeAk):
        def macro_china_money_supply(self):
            return pd.DataFrame({"未知列": [1]})

    monkeypatch.setitem(sys.modules, "akshare", _Broken())
    with pytest.raises(DataFetchError):
        AkshareConnector()._fetch_sync("M2", None, None)


# ==================== ETF 代理估值（PE/PB 不支持ETF → 关联指数代理） ====================


def test_is_etf_code_recognizes_etf_ranges():
    assert _is_etf_code("588170")
    assert _is_etf_code("510300")
    assert _is_etf_code("562500")
    assert _is_etf_code("159915")
    assert _is_etf_code("161725")
    assert not _is_etf_code("600519")
    assert not _is_etf_code("300308")
    assert not _is_etf_code("000001")


def _csindex_xls_bytes(code: str = "H30184", short: str = "半导体",
                       pe2: float = 105.15, day: int = 20260912) -> bytes:
    """构造中证 indicator.xls 同构10列工作簿。"""
    buf = io.BytesIO()
    pd.DataFrame([{
        "日期": day, "指数代码": code, "指数全称": f"{short}指数",
        "指数简称": short, "英文全称": "EN", "英文简称": "EN",
        "市盈率1": pe2 + 3.0, "市盈率2": pe2,
        "股息率1": 0.16, "股息率2": 0.17,
    }]).to_excel(buf, index=False)
    return buf.getvalue()


class _FakeXlsResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _EtfFakeAk(_FakeAk):
    """带乐咕指数PE/PB序列的Fake；记录调用，可按需抛错模拟反爬/不支持。"""

    def __init__(self, pe_error: Exception | None = None,
                 pb_error: Exception | None = None):
        super().__init__()
        self.pe_error = pe_error
        self.pb_error = pb_error
        self.legu_calls: list[tuple[str, str]] = []

    def stock_index_pe_lg(self, symbol):
        self.legu_calls.append((symbol, "PE"))
        if self.pe_error:
            raise self.pe_error
        return pd.DataFrame({
            "日期": [date(2026, 9, 12), date(2026, 9, 15)],
            # 陷阱列排在前：子串匹配会误中"等权滚动市盈率"（真机列顺序如此）
            "等权滚动市盈率": [31.20, 31.78],
            "滚动市盈率": [12.40, 12.65],
            "滚动市盈率中位数": [18.1, 18.5],
        })

    def stock_index_pb_lg(self, symbol):
        self.legu_calls.append((symbol, "PB"))
        if self.pb_error:
            raise self.pb_error
        return pd.DataFrame({
            "日期": [date(2026, 9, 12), date(2026, 9, 15)],
            # 陷阱列故意排在前：精确列名选择必须避开等权口径
            "等权市净率": [3.95, 3.99],
            "市净率": [1.38, 1.40],
            "市净率中位数": [2.10, 2.16],
        })


def _patch_etf_name(monkeypatch, connector, name: str) -> None:
    monkeypatch.setattr(connector, "_etf_display_name", lambda code: name)


def test_etf_semiconductor_pe_uses_h30184_industry_proxy(monkeypatch):
    """588170 科创半导体ETF：PE 走中证全指半导体 H30184，且不调用乐咕/百度。"""
    fake = _EtfFakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    _patch_etf_name(monkeypatch, connector, "科创半导体ETF华夏")
    monkeypatch.setattr(
        ac_module.requests, "get",
        lambda *a, **k: _FakeXlsResp(_csindex_xls_bytes(pe2=105.15)))

    points = connector._fetch_sync("PE(TTM):588170", None, None)
    assert points, "半导体ETF PE代理应返回H30184序列"
    assert points[-1].indicator == "PE(TTM):588170"  # 指标id不变
    assert points[-1].value == 105.15
    assert points[-1].period_date == "2026-09-12"
    extra = points[-1].extra
    assert extra["proxy"] is True
    assert extra["proxy_kind"] == "industry_index"
    assert extra["proxy_index"] == "H30184"
    assert "半导体" in extra["proxy_index_name"]
    assert extra["underlying_etf"] == "588170"
    assert extra["etf_name"] == "科创半导体ETF华夏"
    assert points[-1].confidence < 0.8  # 代理口径降置信度
    # 行业主题ETF严禁误打乐咕宽基接口或百度个股接口
    assert not fake.legu_calls
    assert not fake.baidu_calls


def test_etf_semiconductor_pb_honest_gap(monkeypatch):
    """588170 PB：中证官网行业指数仅披露PE，必须显式报错（面板记缺口），禁止杜撰。"""
    fake = _EtfFakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    _patch_etf_name(monkeypatch, connector, "科创半导体ETF华夏")
    with pytest.raises(DataFetchError, match="H30184"):
        connector._fetch_sync("PB:588170", None, None)
    assert not fake.legu_calls


def test_etf_broad_index_pe_pb_via_legu_with_proxy_extra(monkeypatch):
    """宽基ETF（510300 沪深300ETF）：PE/PB 走乐咕全序列，extra强制代理披露。"""
    fake = _EtfFakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    _patch_etf_name(monkeypatch, connector, "沪深300ETF华泰柏瑞")

    pe = connector._fetch_sync("PE(TTM):510300", None, None)
    pb = connector._fetch_sync("PB:510300", None, None)
    assert pe[-1].value == 12.65 and pb[-1].value == 1.40
    assert fake.legu_calls == [("沪深300", "PE"), ("沪深300", "PB")]
    for points in (pe, pb):
        extra = points[-1].extra
        assert extra["proxy"] is True
        assert extra["proxy_kind"] == "broad_index"
        assert extra["proxy_index_name"] == "沪深300"
        assert extra["underlying_etf"] == "510300"
        assert points[-1].indicator.startswith(("PE(TTM):510300", "PB:510300"))
    assert not fake.baidu_calls


def test_etf_kechuang50_pe_legu_blocked_falls_back_to_csindex(monkeypatch):
    """乐咕永久不支持科创50（KeyError）：PE 自动降级中证官网000688近20期序列。"""
    fake = _EtfFakeAk(pe_error=KeyError("科创50"))
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    _patch_etf_name(monkeypatch, connector, "科创50ETF华夏")
    monkeypatch.setattr(
        ac_module.requests, "get",
        lambda *a, **k: _FakeXlsResp(
            _csindex_xls_bytes(code="000688", short="科创50", pe2=96.12)))

    points = connector._fetch_sync("PE(TTM):588000", None, None)
    assert points[-1].value == 96.12
    extra = points[-1].extra
    assert extra["proxy_index"] == "000688"
    assert "近20期" in extra["source_note"]


def test_etf_pb_legu_failure_raises_not_fabricated(monkeypatch):
    """宽基ETF代理PB在乐咕反爬时显式报错，不得回退模拟/杜撰。"""
    fake = _EtfFakeAk(pb_error=AttributeError("'NoneType' has no attrs"))
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    _patch_etf_name(monkeypatch, connector, "沪深300ETF")
    with pytest.raises(DataFetchError, match="乐咕|PB"):
        connector._fetch_sync("PB:510300", None, None)


def test_etf_unmapped_theme_leaves_honest_gap(monkeypatch):
    """未配置关联指数映射的ETF（如原油ETF）：报明确缺口，不编造估值。"""
    fake = _EtfFakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    _patch_etf_name(monkeypatch, connector, "原油ETF")
    with pytest.raises(DataFetchError, match="映射"):
        connector._fetch_sync("PE(TTM):561360", None, None)
    assert not fake.legu_calls and not fake.baidu_calls


def test_etf_name_resolution_failure_raises(monkeypatch):
    """腾讯简称解析失败（网络异常）时明确报错，缓存空名后不静默伪造。"""
    fake = _EtfFakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    connector._etf_name_cache.clear()

    def _boom(*a, **k):
        raise ac_module.requests.ConnectionError("network down")

    monkeypatch.setattr(ac_module.requests, "get", _boom)
    with pytest.raises(DataFetchError, match="简称解析失败"):
        connector._fetch_sync("PE(TTM):588170", None, None)


def test_etf_financial_ratios_rejected(monkeypatch):
    """ETF无个股财务报表，资产负债率/流动比率必须直接拒绝。"""
    fake = _EtfFakeAk()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    connector = AkshareConnector()
    with pytest.raises(DataFetchError, match="ETF"):
        connector._fetch_sync("资产负债率:588170", None, None)
    assert not fake.fin_calls
