"""ETF代理估值在做T估值面板的披露测试（纯静态方法，离线）。"""

from __future__ import annotations

from types import SimpleNamespace

from src.intraday.valuation import ValuationProvider


def _point(value: float, extra: dict | None = None, period: str = "2026-09-15"):
    return SimpleNamespace(value=value, period_date=period, extra=extra or {})


def test_non_proxy_series_has_no_disclosure() -> None:
    """个股自身百度估值：不生成代理披露，来源标注走默认口径。"""
    points = [_point(30.0, {"valuation": "市盈率(TTM)"})]
    assert ValuationProvider._proxy_source(points, []) == ""
    assert ValuationProvider._proxy_disclosure(points, []) == []


def test_industry_proxy_disclosed_with_short_series_warning() -> None:
    """588170 半导体ETF：行业指数代理 + 短序列分位口径警告（仅近20期）。"""
    pe = [_point(105.15, {
        "proxy": True, "proxy_kind": "industry_index",
        "proxy_index": "H30184", "proxy_index_name": "中证全指半导体产品与设备",
        "source": "中证指数官网indicator.xls", "underlying_etf": "588170",
        "etf_name": "科创半导体ETF华夏",
    })]
    source = ValuationProvider._proxy_source(pe, [])
    assert "ETF代理估值" in source and "中证指数官网" in source

    notes = ValuationProvider._proxy_disclosure(pe, [])
    assert len(notes) == 1
    note = notes[0]
    assert "PE为ETF代理估值" in note
    assert "行业" in note and "中证全指半导体产品与设备" in note
    assert "序列1期" in note
    assert "不足3个月" in note  # 分位口径警告


def test_broad_proxy_pe_and_pb_dedup() -> None:
    """宽基ETF：PE/PB两条代理线各披露一次，同指数不重复。"""
    extra = {
        "proxy": True, "proxy_kind": "broad_index",
        "proxy_index_name": "沪深300",
        "source": "AKShare乐咕乐股", "underlying_etf": "510300",
        "etf_name": "沪深300ETF",
    }
    pe = [_point(12.65, extra)]
    pb = [_point(1.40, extra)]
    notes = ValuationProvider._proxy_disclosure(pe, pb)
    assert len(notes) == 2
    assert any(n.startswith("PE为ETF代理估值") for n in notes)
    assert any(n.startswith("PB为ETF代理估值") for n in notes)


def test_verdict_window_reflects_actual_series_length() -> None:
    """ETF行业代理仅近20期：结论必须写实际样本窗口，禁止套用「近三年」。"""
    short = {"pe_current": 105.71, "pe_percentile": 50.0, "pe_days": 20,
             "pb_current": None, "pb_percentile": None}
    verdict = ValuationProvider._verdict("stretched", short, None, [])
    assert "近20个交易日 50% 分位" in verdict
    assert "近三年" not in verdict

    long = {"pe_current": 30.0, "pe_percentile": 62.0, "pe_days": 720,
            "pb_current": 3.2, "pb_percentile": 55.0, "pb_days": 720}
    verdict3y = ValuationProvider._verdict("moderate", long, None, [])
    assert "近三年 62% 分位" in verdict3y
    assert "三年55%分位" in verdict3y


def test_storage_fallback_marked_in_disclosure() -> None:
    pe = [_point(105.15, {
        "proxy": True, "proxy_kind": "industry_index",
        "proxy_index": "H30184", "proxy_index_name": "中证全指半导体产品与设备",
        "source": "中证指数官网indicator.xls", "storage_fallback": True,
    })]
    notes = ValuationProvider._proxy_disclosure(pe, [])
    assert "本地最近快照" in notes[0]
