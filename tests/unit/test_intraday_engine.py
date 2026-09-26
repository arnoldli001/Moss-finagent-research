"""做T模块 · 信号引擎单元测试（阈值触发 + 止损硬约束 + 数据覆盖度护栏）。

安全关键：止损硬约束必须"任何情况下都不可绕过"——本文件用参数化用例覆盖
"高总分 + 跌破止损位"的组合，确保不会出现"越跌越买"的回踩信号。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.intraday.config import IntradayConfig
from src.intraday.engine import (
    MIN_COVERAGE_FOR_SOLID,
    build_markers,
    compose_scorecard,
    compute_levels,
    decide_signal,
)
from src.intraday.factors import FactorContext
from src.intraday.models import LevelSet, ScoreCard


def _levels(price: float = 10.0, low_buy: float = 9.5,
            high_sell: float = 10.5, stop_loss: float = 9.4) -> LevelSet:
    return LevelSet(
        price=price, box_high=high_sell, box_low=low_buy,
        box_position=0.5, box_span_days=20,
        low_buy=low_buy, high_sell=high_sell, stop_loss=stop_loss,
        stop_loss_pct=1.0)


def _scorecard(total: float, available_weight: float = 100.0) -> ScoreCard:
    return ScoreCard(
        total=total, threshold_action=30.0, threshold_hint=20.0,
        zone="neutral", verdict="", factors=[], weights_sum=100.0,
        available_weight=available_weight)


# ==================== 关键价位 ====================


def test_levels_stop_loss_is_below_low_buy() -> None:
    """止损位 = 回踩线下方 stop_loss_pct%；三档满足 止损 < 回踩 < 冲高。"""
    config = IntradayConfig()
    levels = compute_levels(
        price=10.0, box_high=10.3, box_low=9.7, box_span_days=20,
        config=config)
    assert levels.low_buy == pytest.approx(9.7)
    assert levels.high_sell == pytest.approx(10.3)
    assert levels.stop_loss == pytest.approx(9.7 * (1 - 0.01))
    assert levels.stop_loss < levels.low_buy < levels.high_sell


def test_levels_blend_takes_earlier_touch() -> None:
    """融合布林带后取「更早被触及」的档位：回踩取较高者，冲高取较低者。

    本用例专门校验融合规则本身，故把档位差护栏放宽以免其介入。
    """
    config = IntradayConfig()
    config.levels.blend_boll_bands = True
    config.levels.max_band_pct = 40.0
    levels = compute_levels(
        price=10.0, box_high=11.0, box_low=9.0, box_span_days=20,
        boll_upper=10.6, boll_lower=9.4, config=config)
    assert levels.low_buy == pytest.approx(9.4)
    assert levels.high_sell == pytest.approx(10.6)


def test_levels_fall_back_to_narrow_box_without_daily() -> None:
    """日线缺失时用现价±1%兜底成窄箱体（由上层标注缺口）。"""
    config = IntradayConfig()
    levels = compute_levels(
        price=100.0, box_high=None, box_low=None, box_span_days=0,
        config=config)
    assert levels.box_low == pytest.approx(99.0)
    assert levels.box_high == pytest.approx(101.0)
    # 兜底箱体宽度2% 已落在 [min_band_pct=1.5%, max_band_pct=8%] 内 → 护栏不介入
    assert levels.low_buy == pytest.approx(99.0)
    assert levels.high_sell == pytest.approx(101.0)


def test_levels_min_band_width_protects_against_friction_cost() -> None:
    """布林带把档位压得过窄时，必须扩张到最小档位差（否则被双边成本吃光）。"""
    config = IntradayConfig()
    assert config.levels.min_band_pct == pytest.approx(1.5)
    # 箱体 804~1009（很宽），但布林带只有 863.10/873.54（约1.2%窄带）
    levels = compute_levels(
        price=864.0, box_high=1009.0, box_low=804.0, box_span_days=20,
        boll_upper=873.54, boll_lower=863.10, config=config)
    width = levels.high_sell - levels.low_buy
    assert width >= 864.0 * 0.015 - 0.01
    # 扩张围绕原档位中点对称进行（不偏向任何一侧）
    assert (levels.low_buy + levels.high_sell) / 2 == pytest.approx(
        (863.10 + 873.54) / 2, abs=0.02)


def test_levels_max_band_width_keeps_levels_reachable() -> None:
    """箱体过宽（20日振幅25%）时档位差被收缩到上限，且**围绕现价**，保证可触达。"""
    config = IntradayConfig()
    config.levels.blend_boll_bands = False
    levels = compute_levels(
        price=100.0, box_high=125.0, box_low=75.0, box_span_days=20,
        config=config)
    width = levels.high_sell - levels.low_buy
    assert width == pytest.approx(100.0 * 0.08, abs=0.01)
    assert levels.low_buy < 100.0 < levels.high_sell


def test_levels_shrink_centers_on_price_when_box_is_offset() -> None:
    """现价贴近箱体下沿且箱体过宽时，档位必须围绕现价（否则回踩线会高于现价）。"""
    config = IntradayConfig()
    config.levels.blend_boll_bands = False
    levels = compute_levels(
        price=810.0, box_high=1000.0, box_low=800.0, box_span_days=20,
        config=config)
    assert levels.low_buy < 810.0 < levels.high_sell


def test_levels_box_position_none_when_flat() -> None:
    """箱体退化（上下沿相等）时位置为 None，而不是除零崩掉。"""
    config = IntradayConfig()
    levels = compute_levels(
        price=10.0, box_high=10.0, box_low=10.0, box_span_days=20,
        config=config)
    assert levels.box_position is None


# ==================== 阈值触发 ====================


@pytest.mark.parametrize("total,expected_strength", [
    (30.0, "solid"), (45.0, "solid"), (29.9, "hollow"), (20.0, "hollow"),
])
def test_low_buy_thresholds(total: float, expected_strength: str) -> None:
    """回踩：≥动手线→实心，提示线~动手线→空心。"""
    signal = decide_signal(
        price=9.5, scorecard=_scorecard(total), levels=_levels(),
        config=IntradayConfig(), ts="2026-09-15 10:35")
    assert signal.kind == "low_buy"
    assert signal.triggered is True
    assert signal.strength == expected_strength


@pytest.mark.parametrize("total,expected_strength", [
    (-30.0, "solid"), (-55.0, "solid"), (-29.9, "hollow"), (-20.0, "hollow"),
])
def test_high_sell_thresholds(total: float, expected_strength: str) -> None:
    """冲高：≤-动手线→实心，-动手线~-提示线→空心。"""
    signal = decide_signal(
        price=10.5, scorecard=_scorecard(total), levels=_levels(),
        config=IntradayConfig(), ts="2026-09-15 14:00")
    assert signal.kind == "high_sell"
    assert signal.strength == expected_strength


def test_score_below_hint_never_triggers_even_at_level() -> None:
    """价格触及回踩线但总分未达提示线 → 不触发（截图口径：震荡区间不动手）。"""
    signal = decide_signal(
        price=9.45, scorecard=_scorecard(2.6), levels=_levels(),
        config=IntradayConfig(), ts="2026-09-15 10:35")
    assert signal.kind == "none"
    assert signal.triggered is False
    assert "震荡区间" in signal.reason


def test_score_above_hint_but_price_not_touching() -> None:
    """总分达标但价格没到档位 → 继续观察（不追）。"""
    signal = decide_signal(
        price=10.2, scorecard=_scorecard(35.0), levels=_levels(),
        config=IntradayConfig(), ts="2026-09-15 10:35")
    assert signal.kind == "none"
    assert "未触及回踩线" in signal.reason


def test_vwap_extreme_counts_as_touch() -> None:
    """需求中的"VWAP极值"作为等效触达条件：价格未到档位但z达极值也可触发。"""
    signal = decide_signal(
        price=10.2, scorecard=_scorecard(40.0), levels=_levels(),
        config=IntradayConfig(), ts="2026-09-15 10:35", dev_z=-2.0)
    assert signal.kind == "low_buy"
    assert signal.strength == "solid"
    assert "VWAP偏离达极值" in signal.reason


def test_touch_band_allows_near_touch() -> None:
    """触及带宽：价格略高于回踩线但在 band 内仍算触及。"""
    config = IntradayConfig()
    band = config.levels.touch_band_pct / 100.0
    signal = decide_signal(
        price=9.5 * (1 + band * 0.5), scorecard=_scorecard(35.0),
        levels=_levels(), config=config, ts="2026-09-15 10:35")
    assert signal.kind == "low_buy"


# ==================== 止损硬约束（安全关键） ====================


@pytest.mark.parametrize("total", [100.0, 60.0, 30.0, 0.0, -100.0])
def test_stop_loss_overrides_every_score(total: float) -> None:
    """跌破止损位 → 强制卖出警告；即使总分满分也绝不产生回踩信号。"""
    signal = decide_signal(
        price=9.0, scorecard=_scorecard(total), levels=_levels(),
        config=IntradayConfig(), ts="2026-09-15 10:35")
    assert signal.kind == "stop_loss"
    assert signal.strength == "forced_exit"
    assert signal.triggered is True
    assert signal.blocked_by_stop_loss is True
    assert "禁止任何回踩信号" in signal.reason


def test_stop_loss_boundary_is_inclusive() -> None:
    """恰好等于止损位即触发（保守取向：边界不下穿）。"""
    signal = decide_signal(
        price=9.4, scorecard=_scorecard(80.0), levels=_levels(stop_loss=9.4),
        config=IntradayConfig(), ts="2026-09-15 10:35")
    assert signal.kind == "stop_loss"


def test_above_stop_loss_still_allows_low_buy() -> None:
    """止损位之上，回踩逻辑照常工作（约束只向下生效）。"""
    signal = decide_signal(
        price=9.45, scorecard=_scorecard(45.0),
        levels=_levels(low_buy=9.5, stop_loss=9.4), config=IntradayConfig(),
        ts="2026-09-15 10:35")
    assert signal.kind == "low_buy"


# ==================== 数据覆盖度护栏 ====================


def test_low_coverage_downgrades_solid_to_hollow() -> None:
    """有效权重不足70时，即使总分破动手线也只给软提示。"""
    signal = decide_signal(
        price=9.45, scorecard=_scorecard(60.0, available_weight=50.0),
        levels=_levels(), config=IntradayConfig(), ts="2026-09-15 10:35")
    assert signal.strength == "hollow"
    assert "已降级为软提示" in signal.reason


def test_full_coverage_keeps_solid() -> None:
    signal = decide_signal(
        price=9.45, scorecard=_scorecard(60.0, available_weight=85.0),
        levels=_levels(), config=IntradayConfig(), ts="2026-09-15 10:35")
    assert signal.strength == "solid"
    assert MIN_COVERAGE_FOR_SOLID == pytest.approx(0.7)


# ==================== 打分卡组装 ====================


def test_scorecard_contributions_sum_to_total() -> None:
    """前端表格逐行贡献分相加必须**完全等于**总分（口径一致，不留0.01裂缝）。"""
    ctx = FactorContext(
        price=10.0, vwap=10.2, dev_z=-1.0, dev_pct=-0.2,
        box_high=11.0, box_low=9.0, box_position=0.5, box_span_days=20,
        pct_b=0.3, bandwidth=0.02, bandwidth_pctl=0.5,
        breadth=0.6, up_count=18, down_count=12,
        stock_change_pct=0.5, board_change_pct=0.2,
        news_positive=3, news_negative=1, news_count=4, news_llm_score=0.5)
    card = compose_scorecard(ctx, IntradayConfig())
    assert round(sum(f.contribution for f in card.factors), 2) == card.total
    assert len(card.factors) == 14


def test_scorecard_marks_missing_factors_and_gaps() -> None:
    """缺失因子 available=False、贡献为0、进入 gaps，且有效权重相应下降。"""
    card = compose_scorecard(FactorContext(price=10.0), IntradayConfig())
    unavailable = [f for f in card.factors if not f.available]
    assert unavailable, "全空上下文下应有多项因子被判为数据缺口"
    assert card.available_weight < 100.0
    assert card.gaps
    for factor in unavailable:
        assert factor.contribution == 0.0
        assert factor.score == 0.0


def test_scorecard_zone_mapping() -> None:
    """总分→区间映射与阈值严格一致（含边界值）。"""
    from src.intraday.engine import zone_for

    config = IntradayConfig()
    assert config.thresholds.action == 30.0
    assert config.thresholds.hint == 20.0
    cases = [
        (30.0, "strong_buy_zone"), (100.0, "strong_buy_zone"),
        (29.99, "buy_zone"), (20.0, "buy_zone"),
        (19.99, "neutral"), (0.0, "neutral"), (-19.99, "neutral"),
        (-20.0, "sell_zone"), (-29.99, "sell_zone"),
        (-30.0, "strong_sell_zone"), (-100.0, "strong_sell_zone"),
    ]
    for total, expected in cases:
        assert zone_for(total, config) == expected, total
        assert decide_signal(
            price=10.0, scorecard=_scorecard(total), levels=_levels(),
            config=config, ts="2026-09-15 10:35").total_score == total


# ==================== 图表标记 ====================


def test_build_markers_from_replay() -> None:
    """逐bar重放 → 三角标记：回踩/冲高/止损三类都能落点。"""
    replay = pd.DataFrame({
        "ts": ["2026-09-15 09:35", "2026-09-15 10:00", "2026-09-15 10:30",
               "2026-09-15 14:00"],
        "close": [9.45, 10.6, 9.3, 10.0],
        "total": [35.0, -35.0, -10.0, 0.0],
    })
    markers = build_markers(
        replay=replay, levels=_levels(), scorecard=_scorecard(0.0),
        config=IntradayConfig())
    kinds = [m.kind for m in markers]
    assert "low_buy" in kinds
    assert "high_sell" in kinds
    assert "stop_loss" in kinds
    low = next(m for m in markers if m.kind == "low_buy")
    assert low.strength == "solid"
    assert low.label.startswith("回踩")


def test_build_markers_hollow_when_between_thresholds() -> None:
    replay = pd.DataFrame({
        "ts": ["2026-09-15 09:35"],
        "close": [9.45],
        "total": [22.0],
    })
    markers = build_markers(
        replay=replay, levels=_levels(), scorecard=_scorecard(0.0),
        config=IntradayConfig())
    assert len(markers) == 1
    assert markers[0].strength == "hollow"
    assert "软提示" in markers[0].label or "提示" in markers[0].label


def test_build_markers_empty_replay() -> None:
    assert build_markers(
        replay=pd.DataFrame(), levels=_levels(),
        scorecard=_scorecard(0.0), config=IntradayConfig()) == []
