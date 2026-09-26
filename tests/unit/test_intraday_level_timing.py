"""档位是时刻量：回放/画图都要按"当时那一刻"的档位（2026-09-16 用户实测）。

## 三个真实误判

用户拿三只票的图来问，实测逐bar复算档位后发现：

1. **图1 中际旭创 09:45 那根 ▼止损是伪影**：当日回踩线从 862.09 抬到 898.63、
   止损从 853.47 抬到 889.64；09:45 那根的收盘 867.48 **低于当前的止损 872.22**，
   却**远高于当刻的止损 853.47** —— 用当前值回放早盘的bar，就把正常回踩追认成破位。
   逐bar档位后该票当日**跌破止损 0 次**。

2. **图3 剑桥科技"开盘在回踩线以下"是展示错觉**：图上那条横线是当前值（221.91），
   而当刻回踩线是 215.42，开盘 219.86 一直在它**上方** → 根本没有触及。

3. **"触及档位"原先用收盘价判**：600176 早盘 W 底的最低价正好落在回踩线上，
   但每根 5 分钟bar的收盘都在线上方 → 判成"从未触及"。改用盘中极值后，
   当日实际触及 9 次（最高总分 +5.4）。

## 顺带纠正一个我上轮引入的坑
"回踩线必须在现价下方"的护栏若把线钉在 `现价×(1-贴线带宽)`，与贴线判定
`price ≤ low_buy×(1+带宽)` 相乘恰好≈现价 → **回踩信号被彻底做死**。
现在改为按 `dip_fallback_atr × ATR` 给一个真实可触及的位置。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.intraday.config import IntradayConfig
from src.intraday.engine import build_markers, compute_levels, replay_levels
from src.intraday.models import ScoreCard


def _scorecard(total: float = 0.0) -> ScoreCard:
    config = IntradayConfig()
    return ScoreCard(
        total=total, threshold_action=config.thresholds.action,
        threshold_hint=config.thresholds.hint, zone="neutral", verdict="测试",
        factors=[], weights_sum=100.0, available_weight=100.0)


def _features(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """(ts, close, low, high) → 一份可用作 replay 的特征表。"""
    return pd.DataFrame([
        {"ts": ts, "close": close, "low": low, "high": high,
         "boll_upper": close * 1.01, "boll_mid": close, "boll_lower": close * 0.99,
         "pct_b": 0.4, "bandwidth": 0.02, "vwap": close * 0.999}
        for ts, close, low, high in rows])


# ==================== 逐bar档位：不再把早盘回踩当破位 ====================


def test_current_stop_would_misfire_but_per_bar_stop_does_not() -> None:
    """回归：用当前止损回放早盘 → 伪止损；用当刻止损 → 不误报。

    复刻 300308 09:45 那根：收盘 867.48，当刻止损 853.47，当前止损 872.22。
    """
    replay = pd.DataFrame({
        "ts": ["2026-09-16 09:40", "2026-09-16 09:45", "2026-09-16 10:30"],
        "close": [870.0, 867.48, 892.92],
        "low": [869.0, 866.0, 891.0],
        "high": [871.0, 870.0, 894.0],
        "total": [0.0, 0.0, 0.0],
    })
    current = compute_levels(
        price=907.8, box_high=949.73, box_low=804.02, box_span_days=20,
        boll_lower=902.0, boll_upper=909.0, atr=52.8, day_low=867.44,
        config=IntradayConfig())
    # 当刻档位：早盘价格低，止损也低（853 一带），价格在它上方
    early = compute_levels(
        price=867.48, box_high=949.73, box_low=804.02, box_span_days=20,
        boll_lower=866.0, boll_upper=871.0, atr=52.8, day_low=866.0,
        config=IntradayConfig())

    config = IntradayConfig()
    without_series = build_markers(
        replay=replay, levels=current, scorecard=_scorecard(), config=config)
    with_series = build_markers(
        replay=replay, levels=current, scorecard=_scorecard(), config=config,
        levels_series=[early, early, current])
    del without_series
    # 逐bar档位下不该出现任何止损标记（当刻价格都在当刻止损上方）
    assert [m for m in with_series if m.kind == "stop_loss"] == []


def test_replay_levels_drift_over_the_day() -> None:
    """逐bar档位要真的动：回踩线随 VWAP/布林抬升（实测 862 → 898）。"""
    features = _features([
        ("2026-09-16 09:35", 869.0, 867.0, 871.0),
        ("2026-09-16 10:30", 890.0, 888.0, 892.0),
        ("2026-09-16 14:00", 907.0, 905.0, 909.0),
    ])
    series = replay_levels(
        features, box_high=949.73, box_low=804.02, box_span_days=20,
        atr=52.8, config=IntradayConfig())
    assert len(series) == 3
    assert series[0].low_buy < series[-1].low_buy
    assert series[0].stop_loss < series[-1].stop_loss


def test_replay_levels_is_causal() -> None:
    """每根bar的档位只用当日截至该bar的数据（不能因为后面涨了就把早盘档位抬高）。"""
    features = _features([
        ("2026-09-16 09:35", 869.0, 867.0, 871.0),
        ("2026-09-16 14:00", 907.0, 905.0, 909.0),
    ])
    config = IntradayConfig()
    full = replay_levels(features, box_high=949.73, box_low=804.02,
                         box_span_days=20, atr=52.8, config=config)
    prefix = replay_levels(features.iloc[:1], box_high=949.73, box_low=804.02,
                           box_span_days=20, atr=52.8, config=config)
    assert full[0].low_buy == pytest.approx(prefix[0].low_buy)
    assert full[0].stop_loss == pytest.approx(prefix[0].stop_loss)


# ==================== 触及判定用盘中极值 ====================


def test_wick_to_level_counts_as_touch() -> None:
    """最低价砸到回踩线（收盘又收回去）就算触及 —— 这正是用户说的"W底"。"""
    levels = compute_levels(
        price=47.0, box_high=48.6, box_low=37.8, box_span_days=20,
        boll_lower=46.5, boll_upper=47.6, atr=2.97, day_low=45.6,
        config=IntradayConfig())
    replay = pd.DataFrame({
        "ts": ["2026-09-16 10:00"],
        "close": [47.0],          # 收盘在线上方
        "low": [levels.low_buy * 0.999],   # 盘中砸到线下
        "high": [47.2],
        "total": [25.0],          # 总分达标
    })
    markers = build_markers(
        replay=replay, levels=levels, scorecard=_scorecard(25.0),
        config=IntradayConfig())
    assert [m.kind for m in markers] == ["low_buy"]


def test_no_touch_no_signal_even_with_high_score() -> None:
    """价格没到档位，分数再高也不该出信号。"""
    levels = compute_levels(
        price=47.0, box_high=48.6, box_low=37.8, box_span_days=20,
        boll_lower=46.5, boll_upper=47.6, atr=2.97, day_low=45.6,
        config=IntradayConfig())
    replay = pd.DataFrame({
        "ts": ["2026-09-16 10:00"], "close": [47.5], "low": [47.3],
        "high": [47.6], "total": [80.0]})
    markers = build_markers(
        replay=replay, levels=levels, scorecard=_scorecard(80.0),
        config=IntradayConfig())
    assert markers == []


# ==================== 回踩线兜底不能"贴脸" ====================


def test_dip_fallback_uses_atr_distance_not_touch_band() -> None:
    """结构支撑跑到现价上方时，回踩线要有**真实距离**（按 ATR 给），否则信号永远不触发。"""
    config = IntradayConfig()
    assert config.levels.dip_fallback_atr == pytest.approx(0.3)
    levels = compute_levels(
        price=100.0, box_high=112.0, box_low=105.0,  # 箱体窄且整体在现价上方
        box_span_days=20, atr=10.0, day_low=99.0, config=config)
    # 0.3×ATR = 3 元 → 回踩线 97，而不是"现价下方一个贴线带宽"
    assert levels.low_buy == pytest.approx(97.0, abs=0.01)
    assert levels.low_buy < 100.0 - 2.0
    assert levels.stop_loss < levels.low_buy


def test_dip_fallback_without_atr_still_has_distance() -> None:
    """日线不足（无 ATR）时也要给真实距离，不能退化成贴线带宽。"""
    levels = compute_levels(
        price=100.0, box_high=112.0, box_low=105.0, box_span_days=20,
        atr=None, day_low=99.0, config=IntradayConfig())
    assert levels.low_buy <= 100.0 * 0.995


def test_normal_stock_keeps_structural_levels() -> None:
    """箱体下沿正常时不该走兜底（避免把正常档位也改掉）。"""
    levels = compute_levels(
        price=47.4, box_high=48.6, box_low=37.8, box_span_days=20,
        boll_lower=46.5, boll_upper=47.6, atr=2.97, day_low=45.6,
        config=IntradayConfig())
    assert levels.low_buy == pytest.approx(46.5, abs=0.05)
