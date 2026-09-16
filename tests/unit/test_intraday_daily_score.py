"""日线做T加权决策分单测（daily_score.py）。

日线总分是本次新增的**第二路输出**（与 B1-B15/S1-S6 规则信号并列）。
这里要钉住两件事：
  1. 七因子的算术与分时同口径（逐行贡献分相加 == 总分）；
  2. 不可用因子必须被"记缺口 + 从有效权重里扣除"，而不是填 0 混过去。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intraday.character import analyze_character
from src.intraday.config import IntradayConfig, load_intraday_config
from src.intraday.daily_score import (
    character_daily_score_from,
    compose_daily_scorecard,
    daily_zone_for,
    position_score_from,
    signal_rule_score_from,
    trend_score_from,
    volume_price_score_from,
)
from src.intraday.market_cycle import build_market_cycle
from src.intraday.models import DailySignalItem
from src.intraday.weight_profiles import DAILY_FACTOR_ORDER


def _frame(days: int = 140, drift: float = 0.002) -> pd.DataFrame:
    rs = np.random.RandomState(11)
    close = 20.0 * np.cumprod(1 + drift + rs.normal(0, 0.012, days))
    return pd.DataFrame({
        "date": pd.date_range("2025-06-01", periods=days).strftime("%Y-%m-%d"),
        "open": close * 0.998, "high": close * 1.015, "low": close * 0.985,
        "close": close, "volume": rs.uniform(8e5, 2e6, days),
        "body_top": close * 1.005, "body_bottom": close * 0.995,
        "pct_chg": np.concatenate([[0.0], np.diff(close) / close[:-1] * 100]),
        "is_shrink_volume": False, "is_shrink_half": False, "is_big_yin": False,
        "is_high_volume": True,
    })


def _ma(frame: pd.DataFrame) -> dict[str, float | None]:
    return {f"MA{period}": float(frame["close"].rolling(period).mean().iloc[-1])
            for period in (5, 10, 20, 60, 120)}


def _signal(code: str, kind: str, triggered: bool = True, score: float = 1.0):
    return DailySignalItem(code=code, name=code, kind=kind, triggered=triggered,
                           score=score, reason="测试")


def _compose(frame: pd.DataFrame, **kwargs) -> object:
    config = load_intraday_config()
    params = {
        "code": "300308", "frame": frame, "ma": _ma(frame),
        "position": None, "anchors": [], "pattern": None,
        "buy_signals": [], "sell_signals": [], "protective": {},
        "cycle": None, "character": None, "chan_structure": None,
        "weights": config.daily_weights.as_dict(),
        "action": config.daily_thresholds.action,
        "hint": config.daily_thresholds.hint,
    }
    params.update(kwargs)
    return compose_daily_scorecard(**params)


# ==================== 打分核 ====================

def test_trend_score_rewards_bullish_alignment() -> None:
    bull = trend_score_from(price=110.0, ma5=108.0, ma10=105.0, ma60=95.0, ma120=80.0)
    bear = trend_score_from(price=80.0, ma5=82.0, ma10=85.0, ma60=95.0, ma120=110.0)
    flat = trend_score_from(price=100.0, ma5=100.0, ma10=100.0, ma60=100.0,
                            ma120=100.0)
    assert bull is not None and bear is not None and flat is not None
    assert bull > 0.5 > flat > -0.5 > bear
    assert flat == pytest.approx(0.0, abs=1e-9)


def test_trend_score_needs_price_and_at_least_one_ma() -> None:
    assert trend_score_from(price=None, ma5=1.0, ma10=1.0, ma60=1.0, ma120=1.0) is None
    assert trend_score_from(price=10.0, ma5=None, ma10=None, ma60=None,
                            ma120=None) is None


def test_position_score_prefers_low_and_punishes_streaks() -> None:
    low = position_score_from(percentile=0.1, streak=0)
    high = position_score_from(percentile=0.95, streak=0)
    assert low is not None and high is not None
    assert low > 0 > high
    # 连板是**额外扣分**：同样是 95% 分位，6连板比0连板更该少做T
    streak6 = position_score_from(percentile=0.95, streak=6)
    assert streak6 is not None and streak6 < high
    # 贴近区间高点再加"突破未确认"折价
    at_high = position_score_from(percentile=0.95, streak=0, from_high_pct=-0.5)
    assert at_high is not None and at_high < high


def test_position_score_missing_percentile_is_none() -> None:
    assert position_score_from(percentile=None, streak=0) is None


def test_volume_price_score_reflects_anchor_and_pattern() -> None:
    support = volume_price_score_from(
        anchor_status="有效支撑", anchor_kind="标杆", pattern_direction="bullish")
    broken = volume_price_score_from(
        anchor_status="已破位", anchor_kind="标杆", pattern_direction="bearish",
        is_shrink_volume=True, is_big_yin=True)
    assert support is not None and broken is not None
    assert support > 0.5 > 0 > broken


def test_volume_price_score_none_when_no_inputs() -> None:
    assert volume_price_score_from(
        anchor_status=None, anchor_kind=None, pattern_direction=None) is None


def test_signal_rule_counts_only_triggered() -> None:
    buys = [_signal("B1", "buy"), _signal("B2", "buy"),
            _signal("B3", "buy", triggered=False)]
    sells = [_signal("S2", "sell")]
    score = signal_rule_score_from(buy_signals=buys, sell_signals=sells)
    assert score is not None
    # (2 - 1) / 3
    assert score == pytest.approx(1 / 3, abs=1e-6)
    assert signal_rule_score_from(buy_signals=[], sell_signals=[]) == 0.0


def test_signal_rule_saturates_at_three() -> None:
    buys = [_signal(f"B{i}", "buy") for i in range(6)]
    score = signal_rule_score_from(buy_signals=buys, sell_signals=[])
    assert score == pytest.approx(1.0, abs=1e-6)


def test_character_daily_score_discounts_trend_regime() -> None:
    friendly = character_daily_score_from(t_friendly=85, trend_efficiency=0.2)
    hostile = character_daily_score_from(t_friendly=20, trend_efficiency=0.2)
    trend = character_daily_score_from(t_friendly=85, trend_efficiency=0.8)
    assert friendly is not None and hostile is not None and trend is not None
    assert friendly > 0 > hostile
    assert trend < friendly, "趋势票做T最容易卖飞，同样的友好度要扣 0.2"
    assert character_daily_score_from(t_friendly=None, trend_efficiency=0.2) is None


# ==================== 合成 ====================

def test_scorecard_lists_seven_factors_and_sums_exactly() -> None:
    card = _compose(_frame())
    assert [item.key for item in card.factors] == list(DAILY_FACTOR_ORDER)
    assert len(card.factors) == 7
    assert round(sum(item.contribution for item in card.factors), 2) == card.total
    assert card.weights_sum == pytest.approx(100.0)


def test_missing_factors_are_gapped_and_excluded_from_available_weight() -> None:
    """未提供位置/周期/股性/缠论时，那四个因子记缺口并从有效权重里扣除。"""
    card = _compose(_frame())
    unavailable = {item.key for item in card.factors if not item.available}
    assert {"chan_daily", "cycle", "character", "position"} <= unavailable
    # 100 - 缠论18 - 周期10 - 股性10 - 位置14
    assert card.available_weight == pytest.approx(48.0)
    assert len(card.gaps) == 4
    assert all(item.contribution == 0.0 for item in card.factors
               if not item.available)


def test_full_context_produces_actionable_verdict() -> None:
    frame = _frame(drift=0.0)
    character = analyze_character(frame.rename(columns={"date": "ts"}),
                                  code="300308", mode="daily")
    cycle = build_market_cycle(
        trade_date="20260916",
        limit_up=pd.DataFrame({"连板数": [1] * 70 + [3] * 6}),
        broken=pd.DataFrame({"涨跌幅": [1.0]}),
        limit_down=pd.DataFrame({"涨跌幅": [-10.0]}))
    card = _compose(
        frame, character=character, cycle=cycle,
        pattern=type("P", (), {"direction": "bullish", "code": "up_vol",
                               "name": "涨放量"})(),
        anchors=[type("A", (), {"status": "有效支撑", "kind": "标杆",
                                "body_top": float(frame["close"].iloc[-1]) * 0.98})()],
        position=type("Pos", (), {"percentile": 0.25, "label": "低位",
                                  "window": 120, "from_high_pct": -12.0})(),
        buy_signals=[_signal("B4", "buy"), _signal("B7", "buy")])
    # 仍缺缠论结构（18）→ 有效权重 82，其余六个因子都参与
    assert card.available_weight == pytest.approx(82.0)
    assert card.total > 0
    assert "日线做T总分" in card.verdict
    assert len(card.gaps) == 1


def test_broken_protective_line_overrides_verdict() -> None:
    """保护线已破时，无论总分多少都要先说风险 —— 这是安全关键的优先级。"""
    card = _compose(_frame(drift=0.01), protective={"broken_stop": True,
                                                    "stop_basis": "跌破10均"})
    assert "保护线已破" in card.verdict
    assert "先处理风险" in card.verdict


@pytest.mark.parametrize(
    ("total", "expected"),
    [(30.0, "strong_buy_zone"), (25.0, "strong_buy_zone"), (24.9, "buy_zone"),
     (15.0, "buy_zone"), (14.9, "neutral"), (0.0, "neutral"),
     (-14.9, "neutral"), (-15.0, "sell_zone"), (-24.9, "sell_zone"),
     (-25.0, "strong_sell_zone"), (-60.0, "strong_sell_zone")],
)
def test_zone_boundaries_follow_action_and_hint(total: float,
                                                expected: str) -> None:
    """分档边界必须与 ±15/±25 严格对齐（差 0.1 就该换档）。"""
    from src.intraday.daily_score import daily_zone_for

    assert daily_zone_for(total, action=25.0, hint=15.0) == expected


def test_composed_zone_is_consistent_with_total() -> None:
    """端到端：合成出来的 zone 必须与它自己的 total/阈值自洽。"""
    for drift in (0.0, 0.002, 0.006, 0.012, -0.006):
        card = _compose(_frame(drift=drift), weights={"trend": 100.0})
        assert card.zone == daily_zone_for(card.total, card.threshold_action,
                                           card.threshold_hint)


def test_weights_dict_missing_keys_defaults_to_zero() -> None:
    """权重字典缺键时按 0 处理（而不是 KeyError 崩掉整个日K面板）。"""
    card = _compose(_frame(), weights={"trend": 100.0})
    assert card.total == pytest.approx(
        next(item.contribution for item in card.factors if item.key == "trend"))
    assert card.weights_sum == pytest.approx(100.0)


def test_classify_limit_up_streak_uses_board_tolerance() -> None:
    """连板计数只在主板 10cm 口径下成立；20cm 票要走 19.7% 的阈值。"""
    from src.intraday.daily_score import _count_limit_up_streak

    closes = [10.0]
    for _ in range(3):
        closes.append(closes[-1] * 1.10)
    frame = pd.DataFrame({"close": closes})
    assert _count_limit_up_streak(frame, limit_pct=10.0) == 3
    # 用 20cm 阈值时，10% 的涨幅不算涨停
    assert _count_limit_up_streak(frame, limit_pct=20.0) == 0


def test_daily_thresholds_default_are_coarser_than_intraday() -> None:
    config = IntradayConfig()
    assert config.daily_thresholds.action < config.thresholds.action
    assert config.daily_thresholds.hint < config.thresholds.hint
    assert config.daily_thresholds.hint < config.daily_thresholds.action
