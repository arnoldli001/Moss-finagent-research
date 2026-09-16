"""科技行业真实产业数据连接器测试（全离线：真实WSTS样本+构造DataFrame）。"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.real_industry_connector import (
    RealTechIndustryConnector,
    parse_csindex_pe,
    parse_nbs_ic_yoy,
    parse_wsts_workbook,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "wsts_billings_sample.xlsx"

WSTS_INDICATORS = (
    "ind:半导体销售额同比", "ind:芯片出货量同比", "ind:科技行业PE(TTM)",
)


def test_supports_and_capabilities_are_real_not_simulated():
    conn = RealTechIndustryConnector()
    for ind in WSTS_INDICATORS:
        assert conn.supports(ind) is True
    assert conn.supports("ind:白酒批价(元/瓶)") is False
    caps = conn.get_capabilities()
    assert caps["simulated"] is False
    assert set(WSTS_INDICATORS) <= set(caps["indicators"])


def test_parse_wsts_fixture_computes_worldwide_yoy():
    records = parse_wsts_workbook(_FIXTURE.read_bytes(), months=24)
    assert len(records) == 24
    periods = [r["period"] for r in records]
    assert periods == sorted(periods)
    assert records[-1]["period"] == "2026-07"
    assert records[-1]["yoy"] == pytest.approx(129.4, abs=0.1)
    for r in records:
        assert r["sales_usd_thousand"] > 0
        # 同比为与上年同月比（%）
        assert -100 < r["yoy"] < 300


def test_parse_nbs_ic_yoy_descending_columns_sorted():
    df = pd.DataFrame(
        [
            [10.0, 11.0, 12.0],
            [20.7, 18.8, 22.9],
        ],
        index=["集成电路产量_累计增长(%)", "集成电路产量_同比增长(%)"],
        columns=["2026年7月", "2026年6月", "2026年5月"],
    )
    records = parse_nbs_ic_yoy(df)
    assert [r["period"] for r in records] == ["2026-05", "2026-06", "2026-07"]
    assert records[-1]["yoy"] == 20.7


def test_parse_nbs_missing_row_raises():
    df = pd.DataFrame([[1.0]], index=["其他指标"], columns=["2026年7月"])
    with pytest.raises(DataFetchError):
        parse_nbs_ic_yoy(df)


def test_parse_nbs_drops_nan_february():
    df = pd.DataFrame(
        [[float("nan"), 15.0, 14.0]],
        index=["集成电路产量_同比增长(%)"],
        columns=["2026年2月", "2026年1月", "2025年12月"],
    )
    records = parse_nbs_ic_yoy(df)
    assert [r["period"] for r in records] == ["2025-12", "2026-01"]


def _fake_csindex_xls() -> bytes:
    buf = io.BytesIO()
    rows = [
        [20260910, "H30184", "中证全指半导体产品与设备", "半导体", "x", "x",
         100.0, 106.96, 0.1, 0.1],
        [20260911, "H30184", "中证全指半导体产品与设备", "半导体", "x", "x",
         99.0, 105.33, 0.1, 0.1],
    ]
    pd.DataFrame(rows).to_excel(buf, index=False)  # 带表头：解析器按列位重命名
    return buf.getvalue()


def test_parse_csindex_pe_ttm_daily():
    records = parse_csindex_pe(_fake_csindex_xls())
    assert [r["period"] for r in records] == ["2026-09-10", "2026-09-11"]
    assert records[-1]["pe_ttm"] == 105.33
    assert records[-1]["index_name"] == "半导体"


async def test_fetch_wsts_builds_verified_real_datapoints(monkeypatch):
    async def fake_records():
        return [{"period": "2026-06", "yoy": 142.8, "sales_usd_thousand": 50_000_000},
                {"period": "2026-07", "yoy": 129.4, "sales_usd_thousand": 51_000_000}]

    conn = RealTechIndustryConnector()
    monkeypatch.setattr(conn, "_fetch_wsts", fake_records)
    points = await conn.fetch("ind:半导体销售额同比")
    assert len(points) == 2
    p = points[-1]
    assert p.value == 129.4
    assert p.period_date == "2026-07"
    assert p.verified is True and p.confidence == 0.9
    assert p.extra["simulated"] is False
    assert p.extra["frequency"] == "monthly"
    assert "WSTS" in p.source_name
    assert p.source_type.value == "api"


async def test_fetch_nbs_datapoints_carry_proxy_note(monkeypatch):
    async def fake_records():
        return [{"period": "2026-07", "yoy": 20.7}]

    conn = RealTechIndustryConnector()
    monkeypatch.setattr(conn, "_fetch_nbs", fake_records)
    points = await conn.fetch("ind:芯片出货量同比")
    assert points[0].value == 20.7
    assert "集成电路产量" in points[0].extra["proxy"]
    assert "国家统计局" in points[0].source_name


async def test_fetch_csindex_daily_pe_and_date_filter(monkeypatch):
    async def fake_records():
        return [
            {"period": "2026-09-10", "pe_ttm": 106.96, "index_name": "半导体"},
            {"period": "2026-09-11", "pe_ttm": 105.33, "index_name": "半导体"},
        ]

    conn = RealTechIndustryConnector()
    monkeypatch.setattr(conn, "_fetch_csindex", fake_records)
    points = await conn.fetch("ind:科技行业PE(TTM)",
                              start_date="2026-09-11", end_date="2026-09-30")
    assert len(points) == 1
    assert points[0].period_date == "2026-09-11"
    assert points[0].extra["index_code"] == "H30184"


async def test_fetch_unsupported_indicator_raises():
    with pytest.raises(DataFetchError):
        await RealTechIndustryConnector().fetch("ind:不存在")


async def test_wsts_offline_fallback_marks_snapshot(monkeypatch):
    """在线失败+本地有快照 → 返回快照并标记 storage_fallback，不抛错。"""
    from src.infrastructure.connectors import real_industry_connector as mod

    records = [{"period": "2026-07", "yoy": 129.4,
                "sales_usd_thousand": 51_000_000}]

    async def fake_to_thread(func):
        raise RuntimeError("network down")

    monkeypatch.setattr(RealTechIndustryConnector, "_to_thread",
                        staticmethod(fake_to_thread))
    monkeypatch.setattr(mod, "_latest_snapshot",
                        lambda source: {"records": records})

    out = await RealTechIndustryConnector()._fetch_wsts()
    assert out[0]["storage_fallback"] is True
    assert out[0]["yoy"] == 129.4
