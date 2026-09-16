"""做T模块 · 技术指标纯函数单元测试（含量纲自愈与K线标签口径）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intraday import indicators as ind
from src.intraday.config import IntradayConfig
from src.intraday.features import (
    build_intraday_features,
    build_oscillator_features,
    replay_totals,
    resample_bars,
)


def _session_stamps(day: str) -> list[str]:
    """一个完整交易日的5分钟bar结束时刻（09:35~11:30 + 13:05~15:00，共48根）。"""
    morning = pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min")
    afternoon = pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min")
    return [s.strftime("%Y-%m-%d %H:%M") for s in morning.union(afternoon)]


def _bars(closes: list[float], volumes: list[float] | None = None,
          start: str = "2026-09-15 09:35") -> pd.DataFrame:
    """构造等间隔5分钟K线（H/L 取收盘价±0.5%，便于断言）。"""
    volumes = volumes or [100.0] * len(closes)
    stamps = pd.date_range(start, periods=len(closes), freq="5min")
    return pd.DataFrame({
        "ts": [s.strftime("%Y-%m-%d %H:%M") for s in stamps],
        "open": [c * 0.999 for c in closes],
        "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes],
        "close": closes,
        "volume": volumes,
        "amount": [c * v for c, v in zip(closes, volumes, strict=True)],
    })


# ==================== VWAP ====================


def test_vwap_weighted_by_volume() -> None:
    """VWAP = Σ(价×量)/Σ量：放量段对均价影响更大。"""
    bars = _bars([10.0, 20.0], volumes=[100.0, 100.0])
    vwap = ind.vwap_series(bars)
    # amount/volume = 收盘价自身 → VWAP 末值应为两价等权平均
    assert ind.last_value(vwap) == pytest.approx(15.0)


def test_vwap_uses_amount_when_consistent() -> None:
    """成交额与成交量口径一致时，用真实成交额推导均价。"""
    bars = pd.DataFrame({
        "ts": ["2026-09-15 09:35", "2026-09-15 09:40"],
        "open": [10.0, 11.0], "high": [10.5, 11.5],
        "low": [9.5, 10.5], "close": [10.0, 11.0],
        "volume": [200.0, 100.0],
        "amount": [10.0 * 200, 11.0 * 100],
    })
    vwap = ind.last_value(ind.vwap_series(bars))
    assert vwap == pytest.approx((10.0 * 200 + 11.0 * 100) / 300, abs=0.01)


def test_vwap_self_heals_lot_share_unit_mismatch() -> None:
    """量按「手」、额按「元」时推导价被放大100倍 → 必须自动归一。

    这是真实踩过的坑：若不归一，均价线会飘到100倍价位上，静默给出错误档位。
    """
    bars = pd.DataFrame({
        "ts": ["2026-09-15 09:35", "2026-09-15 09:40"],
        "open": [100.0, 100.0], "high": [101.0, 101.0],
        "low": [99.0, 99.0], "close": [100.0, 100.0],
        "volume": [50.0, 50.0],                    # 手
        "amount": [100.0 * 50 * 100, 100.0 * 50 * 100],  # 元（=价×手×100）
    })
    vwap = ind.last_value(ind.vwap_series(bars))
    assert vwap == pytest.approx(100.0, abs=0.5)


def test_vwap_falls_back_to_typical_price_when_amount_zero() -> None:
    """成交额缺失/不可信（置0）时回退典型价 (H+L+C)/3，而不是产出0。"""
    bars = pd.DataFrame({
        "ts": ["2026-09-15 09:35", "2026-09-15 09:40"],
        "open": [10.0, 10.0], "high": [10.2, 10.4],
        "low": [9.8, 10.0], "close": [10.0, 10.2],
        "volume": [100.0, 100.0], "amount": [0.0, 0.0],
    })
    vwap = ind.last_value(ind.vwap_series(bars))
    assert vwap is not None and vwap > 9.5
    assert vwap == pytest.approx(
        ((10.2 + 9.8 + 10.0) / 3 * 100 + (10.4 + 10.0 + 10.2) / 3 * 100) / 200,
        abs=0.01)


def test_vwap_empty_bars() -> None:
    assert len(ind.vwap_series(pd.DataFrame())) == 0
    assert ind.vwap_series(pd.DataFrame()).empty


def test_deviation_zscore_needs_min_samples() -> None:
    dev = pd.Series([0.001, -0.002, 0.0005])
    assert ind.deviation_zscore(dev) is None
    longer = pd.Series([0.001, -0.002, 0.0005, 0.0, -0.001, 0.002])
    assert ind.deviation_zscore(longer) is not None


def test_deviation_zscore_flat_series_returns_none() -> None:
    """标准差为0（价格贴着均价）时不返回 inf。"""
    assert ind.deviation_zscore(pd.Series([0.0] * 8)) is None


# ==================== 布林带 ====================


def test_bollinger_pct_b_and_bandwidth() -> None:
    closes = [10.0] * 19 + [12.0]
    frame = _bars(closes)
    boll = ind.bollinger(frame, window=20, num_std=2.0)
    pct_b = ind.last_value(boll["pct_b"])
    bandwidth = ind.last_value(boll["bandwidth"])
    assert pct_b is not None and pct_b > 0.5  # 突然上冲 → 靠近上轨
    assert bandwidth is not None and bandwidth > 0
    assert ind.last_value(boll["mid"]) is not None


def test_bollinger_insufficient_sample_returns_nan() -> None:
    frame = _bars([10.0] * 5)
    boll = ind.bollinger(frame, window=20)
    assert ind.last_value(boll["pct_b"]) is None


# ==================== MACD / KDJ / RSI / ATR ====================


def test_macd_detects_golden_cross() -> None:
    closes = [10.0 - i * 0.05 for i in range(40)] + [8.0 + i * 0.25 for i in range(25)]
    frame = _bars(closes)
    macd_frame = ind.macd(frame)
    state, since = ind.macd_cross_state(macd_frame, lookback=30)
    assert state == "golden"
    assert since >= 0


def test_macd_detects_dead_cross() -> None:
    closes = [10.0 + i * 0.05 for i in range(40)] + [12.5 - i * 0.25 for i in range(25)]
    frame = _bars(closes)
    macd_frame = ind.macd(frame)
    state, _ = ind.macd_cross_state(macd_frame, lookback=30)
    assert state == "dead"


def test_macd_state_without_cross_reports_side() -> None:
    """无新交叉时只报 above/below，不误报 golden/dead。"""
    frame = _bars([10.0 + i * 0.02 for i in range(60)])
    state, since = ind.macd_cross_state(ind.macd(frame), lookback=3)
    assert state in ("above", "below")
    assert since == -1


def test_kdj_bounds_and_shape() -> None:
    frame = _bars([10.0 + np.sin(i / 3) for i in range(40)])
    kdj = ind.kdj(frame, n=9)
    assert ind.last_value(kdj["k"]) is not None
    k, d, j = (ind.last_value(kdj[col]) for col in ("k", "d", "j"))
    assert 0 <= k <= 100
    assert 0 <= d <= 100
    assert j == pytest.approx(3 * k - 2 * d, abs=1e-6)


def test_rsi_extremes() -> None:
    """全涨→100，全跌→0（RSI边界，防止除零产生NaN）。"""
    up = _bars([10.0 + i * 0.1 for i in range(30)])
    down = _bars([10.0 - i * 0.1 for i in range(30)])
    assert ind.last_value(ind.rsi(up, 14)) == pytest.approx(100.0)
    assert ind.last_value(ind.rsi(down, 14)) == pytest.approx(0.0)


def test_atr_positive() -> None:
    frame = _bars([10.0 + np.sin(i) for i in range(30)])
    value = ind.last_value(ind.atr(frame, 14))
    assert value is not None and value > 0


# ==================== 箱体与分位 ====================


def test_box_levels_uses_high_low_of_window() -> None:
    frame = _bars([10.0 + i * 0.1 for i in range(30)])
    high, low, count = ind.box_levels(frame, lookback=20)
    assert count == 20
    assert high == pytest.approx(frame["high"].tail(20).max())
    assert low == pytest.approx(frame["low"].tail(20).min())


def test_box_levels_insufficient_samples() -> None:
    high, low, count = ind.box_levels(_bars([10.0, 10.1]), lookback=20)
    assert high is None and low is None and count == 2


def test_box_position_and_degenerate_box() -> None:
    assert ind.box_position(10.0, 11.0, 9.0) == pytest.approx(0.5)
    assert ind.box_position(10.0, 10.0, 10.0) is None


def test_percentile_rank_min_samples() -> None:
    assert ind.percentile_rank([1, 2, 3]) is None          # 样本不足
    values = list(range(1, 21))
    assert ind.percentile_rank(values, 10) == pytest.approx(50.0)
    assert ind.percentile_rank(values, 20) == pytest.approx(100.0)


def test_clip_bounds() -> None:
    assert ind.clip(2.0) == 1.0
    assert ind.clip(-2.0) == -1.0
    assert ind.clip(0.3) == 0.3


# ==================== 分钟聚合与重采样 ====================


def test_aggregate_to_bars_labels_by_end_time() -> None:
    """逐笔/分时聚合出的K线以**结束时刻**命名（与行情源一致）。"""
    points = pd.DataFrame({
        "ts": [f"2026-09-15 09:{m:02d}" for m in range(30, 36)],
        "price": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5],
        "volume": [100.0] * 6,
        "amount": [1000.0] * 6,
    })
    bars = ind.aggregate_to_bars(points, minutes=5)
    assert list(bars["ts"]) == ["2026-09-15 09:35", "2026-09-15 09:40"]
    first = bars.iloc[0]
    assert first["open"] == pytest.approx(10.0)
    assert first["close"] == pytest.approx(10.4)
    assert first["high"] == pytest.approx(10.4)


def test_aggregate_to_bars_caps_at_session_boundaries() -> None:
    """11:30 / 15:00 边界不得越界命名（不能出现 11:35 或 15:05）。"""
    points = pd.DataFrame({
        "ts": ["2026-09-15 11:31", "2026-09-15 15:00", "2026-09-15 15:02"],
        "price": [10.0, 10.1, 10.2],
        "volume": [1.0, 1.0, 1.0],
        "amount": [10.0, 10.0, 10.0],
    })
    bars = ind.aggregate_to_bars(points, minutes=5)
    assert "2026-09-15 11:35" not in set(bars["ts"])
    assert "2026-09-15 15:05" not in set(bars["ts"])


def test_resample_5m_to_30m_end_labels() -> None:
    """5分钟→30分钟：标签为 10:00 / 11:30 / 13:30 / 15:00 等时段边界。"""
    stamps = _session_stamps("2026-09-15")
    frame = _bars([10.0 + i * 0.001 for i in range(len(stamps))])
    frame["ts"] = stamps
    out = resample_bars(frame, 30)
    labels = set(out["ts"])
    assert "2026-09-15 10:00" in labels
    assert "2026-09-15 11:30" in labels
    assert "2026-09-15 13:30" in labels
    assert "2026-09-15 15:00" in labels
    # 午休不应产生空桶，边界不得越界
    assert not any(label.endswith(("11:35", "12:00", "12:30", "15:05"))
                   for label in labels)
    # 一个交易日 240 分钟 / 30 分钟 = 8 根（上午4 + 下午4）
    assert len(out) == 8
    assert labels == {
        "2026-09-15 10:00", "2026-09-15 10:30", "2026-09-15 11:00",
        "2026-09-15 11:30", "2026-09-15 13:30", "2026-09-15 14:00",
        "2026-09-15 14:30", "2026-09-15 15:00",
    }


def test_session_minutes_of_folds_lunch() -> None:
    assert ind.session_minutes_of("2026-09-15 09:30") == 0
    assert ind.session_minutes_of("2026-09-15 11:30") == 120
    assert ind.session_minutes_of("2026-09-15 13:00") == 120
    assert ind.session_minutes_of("2026-09-15 15:00") == 240
    assert ind.session_minutes_of("bad-input") is None


# ==================== 特征构建（按日重置 / 因果性） ====================


def _two_day_bars() -> pd.DataFrame:
    """两个连续交易日的完整5分钟序列（用于校验按日重置与因果性）。"""
    def build(day: str, base: float) -> pd.DataFrame:
        stamps = _session_stamps(day)
        closes = [base + i * 0.01 for i in range(len(stamps))]
        frame = _bars(closes)
        frame["ts"] = stamps
        return frame

    return pd.concat([
        build("2026-09-14", 10.0), build("2026-09-15", 12.0)],
        ignore_index=True)


def test_features_vwap_resets_each_day() -> None:
    """VWAP 必须按交易日重置：跨日累计会得到一个没有交易含义的价格中枢。"""
    config = IntradayConfig()
    features = build_intraday_features(_two_day_bars(), config)
    day2 = features[features["day"] == "2026-09-15"]
    day1 = features[features["day"] == "2026-09-14"]
    # 第二日开盘时 VWAP 应回到第二日的价位区间（12.0 附近），而不是被第一日的低价拖住
    assert day2["vwap"].iloc[0] == pytest.approx(12.0, abs=0.1)
    # 第一日 VWAP 收在自身区间内（10.0~10.47），不跨越到第二日
    assert 10.0 <= float(day1["vwap"].iloc[-1]) <= 10.5
    # 第二日 VWAP 整体高于第一日 → 确认已重置而非跨日累计
    assert day2["vwap"].iloc[0] > day1["vwap"].iloc[-1]


def test_features_are_causal() -> None:
    """因果性：末根bar之前的值不随"未来"数据变化（无未来函数的基础）。"""
    config = IntradayConfig()
    bars = _two_day_bars()
    full = build_intraday_features(bars, config)
    truncated = build_intraday_features(bars.iloc[:-10], config)
    overlap = min(len(full), len(truncated)) - 10
    pd.testing.assert_series_equal(
        full["dev_z"].iloc[:overlap].reset_index(drop=True),
        truncated["dev_z"].iloc[:overlap].reset_index(drop=True),
        check_names=False)


def test_replay_totals_uses_same_kernels_as_live() -> None:
    """重放末bar总分与直接调用打分核的结果逐位一致（图上的点=面板里的分）。"""
    from src.intraday.factors import (
        blend_timeframes,
        boll_score_from_pct_b,
        macd_score_from_gap,
        vwap_score_from_z,
    )
    config = IntradayConfig()
    features = build_intraday_features(_two_day_bars(), config)
    day = features[features["day"] == "2026-09-15"]
    daily_macd = 0.2
    replay = replay_totals(
        intraday_features=day, oscillator_features=None,
        score_constants={"box": 0.5, "sentiment": None, "news": None},
        daily_macd_score=daily_macd, daily_osc_score=None,
        weights=config.weights.as_dict(), config=config)
    last = day.iloc[-1]
    wi, wd = config.factors.macd.tf_weights.normalized()
    expected = (
        boll_score_from_pct_b(
            float(last["pct_b"]),
            bool(float(last["bw_pctl"]) < config.factors.boll.squeeze_percentile),
            config.factors.boll.squeeze_damp) * config.weights.boll
        + vwap_score_from_z(float(last["dev_z"]),
                            config.factors.vwap.z_scale) * config.weights.vwap
        + 0.5 * config.weights.box
        + blend_timeframes(
            macd_score_from_gap(
                float(last["macd_gap"]) / float(last["close"]),
                config.factors.macd.gap_scale_pct,
                config.factors.macd.cross_bonus
                if last["macd_state"] == "golden"
                else -config.factors.macd.cross_bonus
                if last["macd_state"] == "dead" else 0.0),
            daily_macd, wi, wd) * config.weights.macd
    )
    assert replay.iloc[-1]["total"] == pytest.approx(expected, abs=0.02)
    # 无30分钟序列、情绪/消息面/市场环境/技能库四因子都不可用
    # → 有效权重 = 箱体17 + VWAP11 + 布林8 + MACD6
    assert replay.iloc[-1]["available_weight"] == pytest.approx(42.0)


def test_oscillator_features_bounds() -> None:
    config = IntradayConfig()
    osc = build_oscillator_features(resample_bars(_two_day_bars(), 30), config)
    assert len(osc) > 0
    scores = osc["osc_score"].dropna()
    assert ((scores >= -1) & (scores <= 1)).all()
