"""IndicatorRegistry 单元测试（catalog YAML 加载 + 模板匹配）。"""

from pathlib import Path

import pytest

from src.infrastructure.catalog import (
    FREQUENCIES,
    FreshnessState,
    IndicatorMeta,
    get_registry,
    reset_registry_for_test,
)


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_registry_for_test()
    yield
    reset_registry_for_test()


def test_registry_loads_yaml():
    r = get_registry()
    metas = r.all()
    assert len(metas) >= 30  # 配置里 50 个，至少 30 个应被加载
    # 关键条目存在
    assert r.get("CPI") is not None
    assert r.get("us_fed_rate") is not None


def test_registry_handles_missing_yaml_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_INDICATOR_CATALOG", str(tmp_path / "missing.yaml"))
    reset_registry_for_test()
    r = get_registry()
    assert r.all() == []  # 不报错，0 条


def test_registry_template_match_specific_code():
    r = get_registry()
    # 模板 `stock_close:{code}` 应匹配 `stock_close:300308`
    m = r.get("stock_close:300308")
    assert m is not None
    assert m.frequency == "daily"
    assert m.freshness_hours == 26
    # 真实 indicator id 是具体的（300308），不是模板
    assert m.indicator == "stock_close:300308"


def test_registry_template_match_different_codes():
    r = get_registry()
    m1 = r.get("stock_close:300308")
    m2 = r.get("stock_close:600519")
    m3 = r.get("PE(TTM):000001")
    assert m1 is not None and m2 is not None and m3 is not None
    # 不同代码但同模板应得同一元数据语义（除 indicator 字段）
    assert (m1.frequency, m1.freshness_hours) == (m2.frequency, m2.freshness_hours)
    assert (m2.frequency, m2.freshness_hours) == (m3.frequency, m3.freshness_hours)


def test_registry_returns_none_for_unknown():
    r = get_registry()
    assert r.get("XX:NOTREAL") is None
    assert r.get("") is None


def test_registry_by_frequency():
    r = get_registry()
    monthly = r.by_frequency("monthly")
    daily = r.by_frequency("daily")
    realtime = r.by_frequency("realtime")
    assert len(monthly) >= 5  # CPI/PPI/M2/社融/us_*/ind:创新药 等
    assert len(daily) >= 10
    assert len(realtime) >= 3  # fed:*/mkt:*


def test_registry_frequencies_whitelist():
    r = get_registry()
    metas = r.all()
    for m in metas:
        assert m.frequency in FREQUENCIES, f"{m.indicator} 频率非法: {m.frequency}"


def test_registry_freshness_matches_frequency_band():
    """频率 → freshness_hours 软约束：catalog 配置应大致落在合理区间。"""
    r = get_registry()
    bands = {
        "realtime": (0, 1),
        "intraday": (0, 4),
        "daily": (12, 30),
        "weekly": (100, 200),
        "monthly": (500, 900),
        "quarterly": (1500, 2400),
        "yearly": (6000, 9000),
    }
    metas = r.all()
    for m in metas:
        lo, hi = bands.get(m.frequency, (0, 24 * 365))
        assert lo <= m.freshness_hours <= hi, (
            f"{m.indicator} frequency={m.frequency} "
            f"freshness={m.freshness_hours} 不在 [{lo},{hi}]"
        )


def test_registry_reload_force():
    r = get_registry()
    n1 = len(r.all())
    r.reload()  # 不报错，幂等
    n2 = len(r.all())
    assert n1 == n2


def test_freshness_state_is_fresh_when_recent():
    """FreshnessState: 最新 fetch 在 freshness_hours 内 → fresh。"""
    import time

    meta = IndicatorMeta(
        indicator="test", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    now_ms = int(time.time() * 1000)
    state = FreshnessState(
        meta=meta,
        last_period_date="2026-09-28",
        last_fetch_time_ms=now_ms - 60_000,  # 1 分钟前
        row_count=10,
    )
    assert state.is_fresh(now_ms=now_ms) is True


def test_freshness_state_is_stale_when_old():
    """FreshnessState: 最新 fetch 超过 freshness_hours → stale。"""
    import time

    meta = IndicatorMeta(
        indicator="test", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    now_ms = int(time.time() * 1000)
    state = FreshnessState(
        meta=meta,
        last_period_date="2026-09-27",
        last_fetch_time_ms=now_ms - 25 * 3600 * 1000,  # 25 小时前
        row_count=10,
    )
    assert state.is_fresh(now_ms=now_ms) is False


def test_freshness_state_empty_data_is_stale():
    """FreshnessState: row_count=0 → 不 fresh（无法判断时按"必须 fetch"处理）。"""
    meta = IndicatorMeta(
        indicator="test", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    state = FreshnessState(
        meta=meta, last_period_date=None, last_fetch_time_ms=0, row_count=0,
    )
    assert state.is_fresh() is False


def test_freshness_state_disabled_is_stale():
    """FreshnessState: enabled=False → 不 fresh（不参与路由）。"""
    meta = IndicatorMeta(
        indicator="test", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=False, ttl_days=365,
    )
    state = FreshnessState(
        meta=meta,
        last_period_date="2026-09-28",
        last_fetch_time_ms=int(__import__("time").time() * 1000),
        row_count=10,
    )
    assert state.is_fresh() is False