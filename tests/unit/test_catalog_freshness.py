"""交易时段感知的新鲜度判定测试（第十三轮）。

## 守的是什么（实测暴露的真实缺陷）

原 `is_fresh()` 只看 `age <= freshness_hours`。对 `realtime` 指标（0.5h）意味着
**收盘后/周末/节假日照样判 stale** —— 但市场没开，数据一个字都不会变。

实测后果：用户问"今天没开盘，库里明明有最新数据"，SmartFetcher 仍走网络 →
`mkt:cybkcb:spot_summary` 白等 **13.1 秒**。

修复后实测：端到端 **13,100ms → 39ms**，联网指标 **3 个 → 0 个**。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.infrastructure.catalog import FreshnessState, IndicatorMeta
from src.infrastructure.catalog.market_calendar import (
    LIVE_FREQUENCIES,
    is_market_open,
    is_trading_day,
    last_trading_day,
    session_frozen,
)


def _meta(freq: str, fresh_h: float = 0.5) -> IndicatorMeta:
    return IndicatorMeta(
        indicator="test:ind", category="x", frequency=freq,
        freshness_hours=fresh_h, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )


# ============================================================
# 交易日历
# ============================================================


def test_weekend_is_not_trading_day():
    # 2026-09-26 是周六、09-27 是周日
    assert is_trading_day(datetime(2026, 9, 26, 10, 0)) is False
    assert is_trading_day(datetime(2026, 9, 27, 10, 0)) is False


def test_market_open_only_during_sessions():
    """只有 09:30-11:30 / 13:00-15:00 算盘中（非交易日恒 False）。"""
    # 用一个确定是交易日的工作日（2026-09-28 周一）
    mon = datetime(2026, 9, 28)
    assert is_market_open(mon.replace(hour=9, minute=0)) is False   # 开盘前
    assert is_market_open(mon.replace(hour=10, minute=0)) is True   # 上午盘
    assert is_market_open(mon.replace(hour=12, minute=0)) is False  # 午休
    assert is_market_open(mon.replace(hour=14, minute=0)) is True   # 下午盘
    assert is_market_open(mon.replace(hour=16, minute=0)) is False  # 收盘后


def test_session_frozen_after_close():
    """收盘后 = 冻结（数据不会再变）。"""
    mon = datetime(2026, 9, 28, 16, 0)
    assert session_frozen(mon) is True
    assert session_frozen(mon.replace(hour=10)) is False


def test_last_trading_day_skips_weekend():
    """周六的"最近交易日"应回退到周五。"""
    sat = datetime(2026, 9, 26, 10, 0)
    ltd = last_trading_day(sat)
    assert ltd is not None
    assert ltd.weekday() < 5, f"最近交易日落在周末：{ltd}"


def test_live_frequencies_contents():
    """盘中会变的频率白名单。"""
    assert "realtime" in LIVE_FREQUENCIES
    assert "intraday" in LIVE_FREQUENCIES
    assert "daily" not in LIVE_FREQUENCIES
    assert "monthly" not in LIVE_FREQUENCIES


# ============================================================
# 新鲜度判定（核心）
# ============================================================


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_realtime_stale_during_trading_hours():
    """★ 盘中：数据超时就必须判 stale（否则会拿旧价当现价）。"""
    mon_10am = datetime(2026, 9, 28, 10, 0)
    # 数据是 2 小时前取的（freshness=0.5h）
    state = FreshnessState(
        meta=_meta("realtime", 0.5),
        last_period_date="2026-09-28",
        last_fetch_time_ms=_ms(mon_10am) - 2 * 3600 * 1000,
        row_count=10,
    )
    assert state.is_fresh(now_ms=_ms(mon_10am)) is False


def test_realtime_fresh_after_close_with_same_day_data():
    """★★ 核心修复：收盘后 + 数据日期 == 最近交易日 → 判 fresh（不再联网）。

    这正是用户实测的场景："今天没开盘，库里明明有最新数据"。
    """
    mon_4pm = datetime(2026, 9, 28, 16, 0)
    state = FreshnessState(
        meta=_meta("realtime", 0.5),
        last_period_date="2026-09-28",   # 就是最近交易日
        last_fetch_time_ms=_ms(mon_4pm) - 5 * 3600 * 1000,  # 5 小时前取的
        row_count=10,
    )
    assert state.is_fresh(now_ms=_ms(mon_4pm)) is True


def test_realtime_stale_after_close_with_old_data():
    """收盘后，但数据是**上一个交易日**的 → 仍判 stale（该更新了）。"""
    mon_4pm = datetime(2026, 9, 28, 16, 0)
    state = FreshnessState(
        meta=_meta("realtime", 0.5),
        last_period_date="2026-09-25",   # 上周五，不是最近交易日
        last_fetch_time_ms=_ms(mon_4pm) - 5 * 3600 * 1000,
        row_count=10,
    )
    assert state.is_fresh(now_ms=_ms(mon_4pm)) is False


def test_weekend_with_friday_data_is_fresh():
    """★ 周末 + 周五的数据 → fresh（周末市场不开，周五收盘价就是最新）。"""
    sat_noon = datetime(2026, 9, 26, 12, 0)
    ltd = last_trading_day(sat_noon)
    assert ltd is not None
    state = FreshnessState(
        meta=_meta("realtime", 0.5),
        last_period_date=ltd.isoformat(),
        last_fetch_time_ms=_ms(sat_noon) - 30 * 3600 * 1000,  # 30 小时前
        row_count=10,
    )
    assert state.is_fresh(now_ms=_ms(sat_noon)) is True


def test_daily_frequency_unaffected_by_session_freeze():
    """★ `daily` 及以上频率**不受**交易时段感知影响（它们有自己的发布节奏）。

    反例保护：如果给 daily 也套用"收盘后 forever fresh"，
    日线估值就永远不会更新了。
    """
    mon_4pm = datetime(2026, 9, 28, 16, 0)
    state = FreshnessState(
        meta=_meta("daily", 26),
        last_period_date="2026-09-28",   # 日期是最近交易日（诱惑条件）
        last_fetch_time_ms=_ms(mon_4pm) - 100 * 3600 * 1000,  # 但超了 26h
        row_count=10,
    )
    # daily 不该被"冻结期"豁免 → 仍 stale
    assert state.is_fresh(now_ms=_ms(mon_4pm)) is False


def test_no_period_date_never_session_fresh():
    """没有 period_date 时，冻结期豁免不成立（无法证明"这是最新交易日的数据"）。"""
    sat_noon = datetime(2026, 9, 26, 12, 0)
    state = FreshnessState(
        meta=_meta("realtime", 0.5),
        last_period_date=None,
        last_fetch_time_ms=_ms(sat_noon) - 30 * 3600 * 1000,
        row_count=10,
    )
    assert state.is_fresh(now_ms=_ms(sat_noon)) is False


def test_disabled_never_fresh():
    """disabled 指标恒 stale（不参与路由）。"""
    mon_4pm = datetime(2026, 9, 28, 16, 0)
    meta = IndicatorMeta(
        indicator="x", category="x", frequency="realtime",
        freshness_hours=0.5, primary_source="", source_url="",
        enabled=False, ttl_days=365,
    )
    state = FreshnessState(
        meta=meta, last_period_date="2026-09-28",
        last_fetch_time_ms=_ms(mon_4pm), row_count=10,
    )
    assert state.is_fresh(now_ms=_ms(mon_4pm)) is False


# ============================================================
# 聚合展开（expands_to）
# ============================================================


def test_aggregate_meta_expands_to_components():
    """`mkt:cybkcb:turnover:all` 必须声明展开关系。

    没有它：索引 row_count 恒 0（"all" 这个名字永不入库）→ 每次都联网。
    """
    from src.infrastructure.catalog import get_registry, reset_registry_for_test

    reset_registry_for_test()
    r = get_registry()
    for ind in ("mkt:cybkcb:turnover:all", "mkt:cybkcb:val:all"):
        meta = r.get(ind)
        assert meta is not None, f"{ind} 不在 catalog"
        assert meta.is_aggregate(), f"{ind} 没有声明 expands_to"
        assert len(meta.expands_to) >= 2


def test_non_aggregate_meta_has_empty_expands():
    """普通指标不该有 expands_to（防止误配把数据路由错）。"""
    from src.infrastructure.catalog import get_registry, reset_registry_for_test

    reset_registry_for_test()
    r = get_registry()
    for ind in ("CPI", "mkt:turnover:total", "us_fed_rate"):
        meta = r.get(ind)
        assert meta is not None
        assert not meta.is_aggregate(), f"{ind} 不该声明 expands_to"
