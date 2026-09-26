"""消费/周期/医药行业估值连接器测试（全离线：构造中证OSS样本 + 打桩网络）。

对齐 test_real_industry_connector.py 的风格：不打真实网络，但走真实解析路径。
"""

from __future__ import annotations

import io

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors import industry_valuation_connector as iv_mod
from src.infrastructure.connectors.industry_valuation_connector import (
    _INDUSTRY_PE,
    IndustryValuationConnector,
)

PE_INDICATORS = (
    "ind:消费行业PE(TTM)", "ind:周期行业PE(TTM)", "ind:医药行业PE(TTM)",
)


def _csindex_xls(rows: list[tuple[int, float, float]], name: str = "800消费") -> bytes:
    """构造中证 indicator.xls 样本：列位与官网一致（解析器按列位重命名）。"""
    buf = io.BytesIO()
    pd.DataFrame([
        [d, "000932", "中证主要消费", name, "x", "x", pe1, pe2, 0.1, 0.1]
        for d, pe1, pe2 in rows
    ]).to_excel(buf, index=False)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_snapshot_io(monkeypatch):
    """默认不落盘、不读快照，保证测试不污染仓库 data/ 目录。"""
    monkeypatch.setattr(iv_mod, "_write_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(iv_mod, "_latest_snapshot", lambda source: None)


def _patch_oss(monkeypatch, content: bytes | Exception) -> None:
    class _FakeRequests:
        @staticmethod
        def get(url, **kwargs):
            if isinstance(content, Exception):
                raise content
            return _FakeResponse(content)

    monkeypatch.setattr(iv_mod, "requests", _FakeRequests)


def test_supports_and_capabilities_declare_real_data():
    conn = IndustryValuationConnector()
    for ind in PE_INDICATORS:
        assert conn.supports(ind) is True
    assert conn.supports("ind:不存在") is False
    caps = conn.get_capabilities()
    assert caps["simulated"] is False
    assert set(PE_INDICATORS) <= set(caps["indicators"])
    assert set(_INDUSTRY_PE) == set(PE_INDICATORS)


async def test_unsupported_indicator_raises():
    with pytest.raises(DataFetchError):
        await IndustryValuationConnector().fetch("ind:不存在")


async def test_fetch_csindex_builds_verified_real_daily_points(monkeypatch):
    _patch_oss(monkeypatch, _csindex_xls(
        [(20260910, 18.20, 16.90), (20260911, 18.23, 16.82)]))

    points = await IndustryValuationConnector().fetch("ind:消费行业PE(TTM)")

    assert len(points) == 2
    p = points[-1]
    assert p.value == pytest.approx(16.82)      # 市盈率2 = 滚动TTM口径
    assert p.period_date == "2026-09-11"
    assert p.unit == "倍"
    assert p.confidence == 0.9 and p.verified is True
    # 真实数据必须与模拟数据可区分
    assert p.extra["simulated"] is False
    assert p.extra["frequency"] == "daily"
    assert p.extra["metric"] == "PE_TTM"
    assert p.extra["index_code"] == "000932"
    # 口径必须披露：指数整体法 ≠ 个股PE中位数
    assert "整体法" in p.extra["proxy_note"]
    assert p.source_type.value == "api"
    assert "中证" in p.source_name


async def test_fetch_csindex_respects_date_bounds(monkeypatch):
    _patch_oss(monkeypatch, _csindex_xls(
        [(20260910, 18.20, 16.90),
         (20260911, 18.23, 16.82),
         (20260912, 18.30, 16.75)]))

    points = await IndustryValuationConnector().fetch(
        "ind:医药行业PE(TTM)", start_date="2026-09-11", end_date="2026-09-12")

    assert [p.period_date for p in points] == ["2026-09-11", "2026-09-12"]


async def test_cyclical_and_pharma_use_their_own_index_codes(monkeypatch):
    seen: list[str] = []

    class _FakeRequests:
        @staticmethod
        def get(url, **kwargs):
            seen.append(url)
            return _FakeResponse(_csindex_xls([(20260911, 19.0, 21.5)]))

    monkeypatch.setattr(iv_mod, "requests", _FakeRequests)
    conn = IndustryValuationConnector()

    await conn.fetch("ind:周期行业PE(TTM)")
    await conn.fetch("ind:医药行业PE(TTM)")

    assert "399998indicator.xls" in seen[0]
    assert "000933indicator.xls" in seen[1]


async def test_oss_failure_falls_back_to_local_snapshot(monkeypatch):
    """在线失败但有本地快照 → 返回快照并标 storage_fallback，不抛错。"""
    monkeypatch.setattr(iv_mod, "_latest_snapshot", lambda source: {
        "index_code": "000932",
        "records": [{"period": "2026-09-11", "pe_ttm": 16.82,
                     "index_name": "800消费"}],
    })
    _patch_oss(monkeypatch, RuntimeError("network down"))

    points = await IndustryValuationConnector().fetch("ind:消费行业PE(TTM)")

    assert len(points) == 1
    assert points[0].value == pytest.approx(16.82)
    assert points[0].extra["storage_fallback"] is True
    assert points[0].extra["simulated"] is False


async def test_snapshot_is_per_index_and_mismatch_is_rejected(monkeypatch):
    """快照按指数分文件；即便读到别的指数，也必须弃用而不是串档。

    三个行业PE共用同一份中证OSS来源，曾共用过一个快照文件名 —— 那会让"消费"
    的历史记录在某次网络失败时被当成"医药"的值返回（数值同源，肉眼难发现）。
    """
    keys: list[str] = []
    monkeypatch.setattr(iv_mod, "_write_snapshot",
                        lambda source, payload: keys.append(source))

    _patch_oss(monkeypatch, _csindex_xls([(20260911, 18.2, 16.82)]))
    conn = IndustryValuationConnector()
    await conn.fetch("ind:消费行业PE(TTM)")
    await conn.fetch("ind:医药行业PE(TTM)")

    assert keys == ["csindex_industry_pe_000932",
                    "csindex_industry_pe_000933"]
    assert len(set(keys)) == 2

    # 读到不匹配指数的快照 → 弃用 → 既不走快照，也不返回别人的数
    monkeypatch.setattr(iv_mod, "_latest_snapshot", lambda source: {
        "index_code": "000932",  # 消费的档案
        "records": [{"period": "2026-09-11", "pe_ttm": 16.82}],
    })
    _patch_oss(monkeypatch, RuntimeError("network down"))
    monkeypatch.setattr(IndustryValuationConnector, "_sw_row",
                        staticmethod(lambda name: {"pe_ttm": 28.05}))

    points = await IndustryValuationConnector().fetch("ind:医药行业PE(TTM)")
    assert [p.value for p in points] == [pytest.approx(28.05)]  # 申万兜底值
    assert "storage_fallback" not in points[0].extra


async def test_oss_failure_without_snapshot_falls_back_to_sw_industry(monkeypatch):
    """主源失败且无快照 → 退申万一级行业当期截面（单点，标注代理口径）。"""
    _patch_oss(monkeypatch, RuntimeError("network down"))
    monkeypatch.setattr(IndustryValuationConnector, "_sw_row",
                        staticmethod(lambda name: {"pe_ttm": 19.51}))

    points = await IndustryValuationConnector().fetch("ind:消费行业PE(TTM)")

    assert len(points) == 1
    p = points[0]
    assert p.value == pytest.approx(19.51)
    assert p.extra["industry_name"] == "食品饮料"
    assert p.extra["industry_code"] == "801120.SI"
    assert p.extra["simulated"] is False
    assert p.confidence == 0.85
    assert "申万" in p.source_name


async def test_both_sources_down_raises_datafetch_error(monkeypatch):
    _patch_oss(monkeypatch, RuntimeError("network down"))
    monkeypatch.setattr(IndustryValuationConnector, "_sw_row",
                        staticmethod(lambda name: None))

    with pytest.raises(DataFetchError):
        await IndustryValuationConnector().fetch("ind:周期行业PE(TTM)")


async def test_sw_row_missing_industry_returns_none(monkeypatch):
    """申万表里没有该行业/列为NaN时返回None，不抛错也不给假值。"""
    fake_df = pd.DataFrame([
        {"行业名称": "食品饮料", "TTM(滚动)市盈率": float("nan")},
        {"行业名称": "煤炭", "TTM(滚动)市盈率": 16.93},
    ])

    class _FakeAk:
        @staticmethod
        def sw_index_first_info():
            return fake_df

    import sys
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk)

    assert IndustryValuationConnector._sw_row("食品饮料") is None
    assert IndustryValuationConnector._sw_row("不存在的行业") is None
    assert IndustryValuationConnector._sw_row("煤炭") == {"pe_ttm": 16.93}
