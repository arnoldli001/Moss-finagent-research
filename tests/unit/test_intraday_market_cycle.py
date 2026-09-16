"""市场情绪周期单测（market_cycle.py）。

这一支的判定结果直接决定"今天允不允许做T"（退潮/冰点一票否决），
所以阶段判定必须能被穷举钉住 —— 判错一天就会把最危险的阶段当成最安全的阶段。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
import pytest

from src.intraday.market_cycle import (
    THRESHOLDS,
    MarketCycleProvider,
    build_market_cycle,
    classify_stage,
    temperature_from,
)


def _zt_frame(streaks: list[int], columns: bool = True) -> pd.DataFrame:
    if not columns:
        return pd.DataFrame({"代码": ["000001"] * len(streaks)})
    return pd.DataFrame({
        "代码": [f"{index:06d}" for index in range(len(streaks))],
        "连板数": streaks,
    })


def _broken_frame(pct_changes: list[float] | None = None) -> pd.DataFrame:
    values = pct_changes if pct_changes is not None else [3.0, 1.0]
    return pd.DataFrame({"代码": [f"9{index:05d}" for index in range(len(values))],
                         "涨跌幅": values})


# ==================== 阶段判定（穷举） ====================

@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # 退潮：炸板率过高 / 涨停过少
        (dict(limit_up_count=50, broken_rate=0.70, max_streak=5, streak2plus=10,
              big_loss_count=2, limit_down_count=1), "退潮期"),
        (dict(limit_up_count=10, broken_rate=0.20, max_streak=4, streak2plus=3,
              big_loss_count=2, limit_down_count=1), "退潮期"),
        # 一票否决（大面/跌停过多）直接把阶段打成退潮
        (dict(limit_up_count=80, broken_rate=0.10, max_streak=6, streak2plus=15,
              big_loss_count=12, limit_down_count=3), "退潮期"),
        (dict(limit_up_count=80, broken_rate=0.10, max_streak=6, streak2plus=15,
              big_loss_count=2, limit_down_count=15), "退潮期"),
        # 冰点：高度与连板家数双低
        (dict(limit_up_count=20, broken_rate=0.10, max_streak=2, streak2plus=2,
              big_loss_count=1, limit_down_count=2), "冰点"),
        # 主升：涨停多 + 有身位 + 承接健康
        (dict(limit_up_count=80, broken_rate=0.10, max_streak=6, streak2plus=15,
              big_loss_count=2, limit_down_count=2), "主升期"),
        # 高位震荡：有身位但炸板率偏高
        (dict(limit_up_count=80, broken_rate=0.45, max_streak=6, streak2plus=15,
              big_loss_count=2, limit_down_count=2), "高位震荡期"),
        # 发酵
        (dict(limit_up_count=40, broken_rate=0.15, max_streak=3, streak2plus=6,
              big_loss_count=2, limit_down_count=2), "发酵期"),
        # 试错
        (dict(limit_up_count=25, broken_rate=0.15, max_streak=3, streak2plus=5,
              big_loss_count=2, limit_down_count=2), "试错期"),
    ],
)
def test_classify_stage_matrix(kwargs: dict, expected: str) -> None:
    stage, gates = classify_stage(**kwargs)
    assert stage == expected
    assert isinstance(gates, list)


def test_veto_gates_are_reported_verbatim() -> None:
    _, gates = classify_stage(limit_up_count=80, broken_rate=0.1, max_streak=6,
                              streak2plus=15, big_loss_count=12, limit_down_count=15)
    assert len(gates) == 2
    assert any("大面" in item for item in gates)
    assert any("跌停" in item for item in gates)


# ==================== 温度 ====================

def test_temperature_is_bounded_and_monotonic_in_breadth() -> None:
    low = temperature_from(limit_up_count=5, broken_rate=0.6, max_streak=2,
                           limit_down_count=12)
    high = temperature_from(limit_up_count=90, broken_rate=0.05, max_streak=7,
                            limit_down_count=0)
    assert 0 <= low <= 100
    assert 0 <= high <= 100
    assert high > low


def test_temperature_missing_broken_rate_uses_midpoint() -> None:
    """炸板率缺失给中位分，不假装很好也不假装很差。"""
    value = temperature_from(limit_up_count=60, broken_rate=None, max_streak=5,
                             limit_down_count=0)
    assert 0 < value < 100


# ==================== 快照构建 ====================

def test_build_market_cycle_from_pools() -> None:
    cycle = build_market_cycle(
        trade_date="20260916", limit_up=_zt_frame([1] * 77 + [2] * 8 + [6] + [3] * 3),
        broken=_broken_frame([2.0, 1.0, -9.5]), limit_down=_broken_frame([-10.0]))
    assert cycle.available is True
    assert cycle.limit_up_count == 89
    assert cycle.streak2plus == 12
    assert cycle.first_board == 77
    assert cycle.max_streak == 6
    assert cycle.limit_down_count == 1
    # 大面 = 跌停1 + 炸板中跌幅≤-9% 的1家
    assert cycle.big_loss_count == 2
    assert cycle.broken_rate == pytest.approx(3 / 92, abs=1e-4)
    assert cycle.promotion_rate == pytest.approx(12 / 89, abs=1e-4)
    assert cycle.stage in ("主升期", "高位震荡期")
    assert cycle.notes and "涨停 89 家" in cycle.notes[0]
    assert cycle.to_dict()["thresholds"] == THRESHOLDS


def test_build_market_cycle_empty_pools_is_unavailable() -> None:
    cycle = build_market_cycle(trade_date="20260916",
                               limit_up=pd.DataFrame(), broken=pd.DataFrame(),
                               limit_down=pd.DataFrame())
    assert cycle.available is False
    assert cycle.gap and "均为空" in cycle.gap


def test_build_market_cycle_without_streak_column() -> None:
    """池子没有「连板数」列时不能崩：高度记 0 并如实反映到阶段判定里。"""
    cycle = build_market_cycle(
        trade_date="20260916", limit_up=_zt_frame([1, 2, 3], columns=False),
        broken=_broken_frame(), limit_down=pd.DataFrame())
    assert cycle.available is True
    assert cycle.max_streak == 0
    assert cycle.streak2plus == 0


def test_retreat_stage_forbids_t() -> None:
    cycle = build_market_cycle(
        trade_date="20260916", limit_up=_zt_frame([1] * 8),
        broken=_broken_frame([-1.0] * 20), limit_down=_broken_frame([-10.0] * 12))
    assert cycle.stage in ("退潮期", "冰点")
    assert cycle.t_allowed is False
    assert cycle.gates


# ==================== Provider（取数 + 缓存 + 降级） ====================

class _FakeFetcher:
    def __init__(self, results: dict[str, dict | None]) -> None:
        self.results = results
        self.calls: list[str] = []

    def __call__(self, date: str) -> dict:
        self.calls.append(date)
        value = self.results.get(date)
        if value is None:
            raise RuntimeError("模拟：接口不可用")
        return value


def _payload(streaks: list[int]) -> dict:
    return {"limit_up": _zt_frame(streaks), "broken": _broken_frame(),
            "limit_down": _broken_frame([-10.0]), "strong": None}


def test_provider_caches_within_ttl() -> None:
    fetcher = _FakeFetcher({})
    fetcher.results[datetime.now().strftime("%Y%m%d")] = _payload([1] * 40 + [3] * 5)
    provider = MarketCycleProvider(ttl_seconds=60, fetcher=fetcher)
    first = asyncio.run(provider.snapshot())
    second = asyncio.run(provider.snapshot())
    assert first.available and second.available
    assert len(fetcher.calls) == 1, "TTL 内不该重复取数"


def test_provider_force_bypasses_cache() -> None:
    today = datetime.now().strftime("%Y%m%d")
    fetcher = _FakeFetcher({today: _payload([1] * 40)})
    provider = MarketCycleProvider(ttl_seconds=600, fetcher=fetcher)
    asyncio.run(provider.snapshot())
    asyncio.run(provider.snapshot(force=True))
    assert len(fetcher.calls) == 2


def test_provider_falls_back_to_previous_day_when_today_empty() -> None:
    """非交易日/数据未就绪时向前找最近一个有数据的日期（最多 lookback 天）。"""
    today = datetime.now()
    yesterday = (today.replace(hour=12) - __import__("datetime").timedelta(days=1))
    fetcher = _FakeFetcher({
        today.strftime("%Y%m%d"): {"limit_up": pd.DataFrame(),
                                   "broken": pd.DataFrame(),
                                   "limit_down": pd.DataFrame(), "strong": None},
        yesterday.strftime("%Y%m%d"): _payload([1] * 45 + [4] * 3),
    })
    provider = MarketCycleProvider(ttl_seconds=0, fetcher=fetcher, lookback_days=7)
    cycle = asyncio.run(provider.snapshot())
    assert cycle.available is True
    assert cycle.trade_date == yesterday.strftime("%Y%m%d")


def test_provider_returns_gap_when_all_days_fail() -> None:
    """取不到就如实记缺口 —— **绝不返回上一次的旧值**（周期错一天会放开退潮期）。"""
    provider = MarketCycleProvider(
        ttl_seconds=0, fetcher=_FakeFetcher({}), lookback_days=3)
    cycle = asyncio.run(provider.snapshot())
    assert cycle.available is False
    assert cycle.gap and "均未取到" in cycle.gap
    assert cycle.temperature == 50  # 默认中性，不代表任何判断


def test_provider_invalidate_clears_cache() -> None:
    today = datetime.now().strftime("%Y%m%d")
    fetcher = _FakeFetcher({today: _payload([1] * 40)})
    provider = MarketCycleProvider(ttl_seconds=600, fetcher=fetcher)
    asyncio.run(provider.snapshot())
    provider.invalidate()
    asyncio.run(provider.snapshot())
    assert len(fetcher.calls) == 2
