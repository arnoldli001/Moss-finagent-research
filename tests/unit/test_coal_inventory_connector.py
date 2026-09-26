"""电厂煤炭库存连接器测试（全离线：真实CECI正文片段 + 打桩网络）。

正文片段取自 2026-09-26 实抓的中电联《CECI周报》，覆盖两种绝对量句式与
"海路运输电厂"干扰列 —— 这两处正是最容易静默抓错的地方。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors import coal_inventory_connector as coal_mod
from src.infrastructure.connectors.coal_inventory_connector import (
    INDICATOR,
    CoalInventoryConnector,
    parse_cec_inventory,
)

#: 句式一：环比增量夹在锚点与绝对量之间（"较9月10日增30万吨至10362万吨"）
_ARTICLE_WITH_DELTA = (
    "电厂库存由去化转为累积，9月17日纳入统计发电企业煤炭库存较9月10日"
    "增30万吨至10362万吨，可用天数回升至21.7天；海路运输电厂库存虽继续"
    "小幅去化，但整体库存规模仍处合理区间。"
)
#: 句式二：绝对量紧跟锚点，环比增量在后面
_ARTICLE_BARE = (
    "随着日耗走低、入厂煤量同步减少，电厂库存基本保持稳定，9月10日纳入统计"
    "的发电企业煤炭库存10340万吨，较9月3日减少35万吨，库存可用天数上升至"
    "20.5天；其中，海路运输电厂库存环比继续回升。"
)
#: 只有比较基期、没有直接写出统计日 → 统计日 = 基期 + 7 天
_ARTICLE_DERIVED = (
    "电厂库存延续季节性去化，降幅较前期有所扩大，纳入统计的发电企业煤炭库存"
    "10417万吨，较8月20日减少367万吨，电厂库存可用天数19.0天。其中，海路"
    "运输电厂日均耗煤量172万吨；电厂煤炭库存2891万吨，较8月20日减少40万吨。"
)


def test_supports_and_capabilities_declare_real_data():
    conn = CoalInventoryConnector()
    assert conn.supports(INDICATOR) is True
    assert conn.supports("ind:白酒批价(元/瓶)") is False
    caps = conn.get_capabilities()
    assert caps["simulated"] is False
    assert caps["indicators"] == [INDICATOR]
    # 口径必须写明是"发电企业"而不是别的总体
    assert "发电企业" in caps["notes"]


def test_parses_value_when_delta_sits_between_anchor_and_value():
    """句式一：正则在"增30万吨至10362万吨"上必须取 10362，不能取 30。"""
    got = parse_cec_inventory(_ARTICLE_WITH_DELTA, date(2026, 9, 22))
    assert got is not None
    period, value, basis = got
    assert value == pytest.approx(10362.0)
    assert period == "2026-09-17"
    assert basis == "stat_date_stated"


def test_parses_bare_value_right_after_anchor():
    got = parse_cec_inventory(_ARTICLE_BARE, date(2026, 9, 15))
    assert got is not None
    period, value, _ = got
    assert value == pytest.approx(10340.0)
    assert period == "2026-09-10"


def test_derives_stat_date_from_comparison_base_when_absent():
    """只有"较8月20日"时，统计日 = 基期 + 7 天。"""
    got = parse_cec_inventory(_ARTICLE_DERIVED, date(2026, 8, 31))
    assert got is not None
    period, value, basis = got
    assert value == pytest.approx(10417.0)
    assert period == "2026-08-27"
    assert basis == "stat_date_derived_+7d"


def test_anchor_excludes_sea_route_plant_series():
    """锚点必须是"发电企业煤炭库存"：正文里"电厂煤炭库存2891万吨"是另一个总体。

    若把锚点缩短成"煤炭库存"，这里会抓到 2891（差 3.6 倍）且不报错。
    """
    got = parse_cec_inventory(_ARTICLE_DERIVED, date(2026, 8, 31))
    assert got is not None and got[1] == pytest.approx(10417.0)
    assert got[1] != pytest.approx(2891.0)


def test_out_of_range_value_is_rejected_not_returned():
    """量级越界（抓错列/抓到港口口径）时宁缺勿错，返回 None。"""
    bogus = ("纳入统计的发电企业煤炭库存605万吨，较上周减少10万吨。")
    assert parse_cec_inventory(bogus, date(2026, 9, 24)) is None


def test_article_without_anchor_or_value_returns_none():
    assert parse_cec_inventory("本周煤价震荡运行，供需双弱。", date(2026, 9, 22)) is None
    # 老期次只公布环比增减，没有绝对量
    rel_only = ("燃煤电厂煤炭库存低于去年同期731.1万吨，库存可用天数低于上年同期1.8天。")
    assert parse_cec_inventory(rel_only, date(2026, 6, 20)) is None


async def test_fetch_builds_verified_real_points(monkeypatch):
    rows = [("2026-09-03", 10383.0, "stat_date_derived_+7d"),
            ("2026-09-10", 10340.0, "stat_date_stated"),
            ("2026-09-17", 10362.0, "stat_date_stated")]
    monkeypatch.setattr(CoalInventoryConnector, "_fetch_rows",
                        staticmethod(lambda: rows))
    monkeypatch.setattr(coal_mod, "_write_snapshot", lambda *a, **k: None)

    points = await CoalInventoryConnector().fetch(INDICATOR)

    assert [p.value for p in points] == [10383.0, 10340.0, 10362.0]
    p = points[-1]
    assert p.period_date == "2026-09-17"
    assert p.unit == "万吨"
    assert p.confidence == 0.85 and p.verified is True
    assert p.extra["simulated"] is False
    assert p.extra["frequency"] == "weekly"
    assert "发电企业" in p.extra["scope"]
    # 样本口径 ≠ 全国统调/重点电厂，必须随数据披露
    assert "样本" in p.extra["scope_note"]
    assert "CECI" in p.source_name or "中电联" in p.source_name


async def test_fetch_respects_date_bounds(monkeypatch):
    rows = [("2026-09-03", 10383.0, "b"), ("2026-09-10", 10340.0, "b"),
            ("2026-09-17", 10362.0, "b")]
    monkeypatch.setattr(CoalInventoryConnector, "_fetch_rows",
                        staticmethod(lambda: rows))
    monkeypatch.setattr(coal_mod, "_write_snapshot", lambda *a, **k: None)

    points = await CoalInventoryConnector().fetch(
        INDICATOR, start_date="2026-09-10")
    assert [p.period_date for p in points] == ["2026-09-10", "2026-09-17"]


async def test_stale_series_falls_back_to_snapshot(monkeypatch):
    """最新一期过旧 → 视为失败 → 退本地快照并标 storage_fallback。"""
    monkeypatch.setattr(CoalInventoryConnector, "_fetch_rows", staticmethod(
        lambda: [("2020-01-01", 12000.0, "b"), ("2020-01-08", 12100.0, "b")]))
    monkeypatch.setattr(coal_mod, "_latest_snapshot", lambda source: {
        "records": [{"period": "2026-09-17", "value": 10362.0}],
    })

    points = await CoalInventoryConnector().fetch(INDICATOR)
    assert [p.value for p in points] == [10362.0]
    assert points[0].extra["storage_fallback"] is True
    assert points[0].extra["simulated"] is False


async def test_network_failure_without_snapshot_raises(monkeypatch):
    def boom() -> list:
        raise RuntimeError("network down")

    monkeypatch.setattr(CoalInventoryConnector, "_fetch_rows", staticmethod(boom))
    monkeypatch.setattr(coal_mod, "_latest_snapshot", lambda source: None)

    with pytest.raises(DataFetchError):
        await CoalInventoryConnector().fetch(INDICATOR)


async def test_unsupported_indicator_raises():
    with pytest.raises(DataFetchError):
        await CoalInventoryConnector().fetch("ind:不存在")
