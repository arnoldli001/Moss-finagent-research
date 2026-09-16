"""个股股性画像单测（character.py）。

股性直接决定"给这只票推荐什么权重、什么档位"，所以每一条口径都要能被钉住：
它算错的后果不是"少一个提示"，而是"把一只单边趋势票当成震荡票，
建议用户在里面反复做T"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intraday.character import (
    MIN_SAMPLE_DAYS,
    analyze_character,
)


def _bars(closes: list[float], *, volumes: list[float] | None = None,
          start: str = "2025-01-01", spread: float = 0.03) -> pd.DataFrame:
    """合成日线。`spread` 决定每根bar的高低点相对收盘价的半径（=波动来源）。"""
    count = len(closes)
    close = pd.Series(closes, dtype="float64")
    volume = pd.Series(volumes if volumes is not None else [1_000_000.0] * count)
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=count, freq="D").strftime("%Y-%m-%d"),
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * (1 + spread),
        "low": close * (1 - spread),
        "close": close,
        "volume": volume,
        "amount": close * volume,
    })


def _swing_series(days: int = 200) -> list[float]:
    """纯震荡：围绕 20 元上下往复（趋势效率接近 0，但振幅足够）。"""
    values = []
    for index in range(days):
        values.append(20.0 + (1.2 if index % 2 == 0 else -1.2))
    return values


def _trend_series(days: int = 200) -> list[float]:
    """单边上涨：每天 +0.5%（趋势效率接近 1）。"""
    return [10.0 * (1.005 ** index) for index in range(days)]


def test_insufficient_sample_is_unavailable_with_gap() -> None:
    profile = analyze_character(_bars([10.0] * (MIN_SAMPLE_DAYS - 1)), code="300308")
    assert profile.available is False
    assert profile.gap and "日线样本不足" in profile.gap
    # 不可用时也必须给出**可用**的兜底权重（前端不能拿到空配方）
    assert abs(sum(profile.weights.values()) - 100.0) < 1e-6
    assert profile.levels == {}


def test_none_and_empty_bars_do_not_raise() -> None:
    for source in (None, pd.DataFrame()):
        profile = analyze_character(source, code="600036")
        assert profile.available is False
        assert profile.gap


def test_swing_stock_is_classified_and_recommends_mean_reversion() -> None:
    profile = analyze_character(_bars(_swing_series()), code="600036",
                                name="招商银行")
    assert profile.available is True
    assert profile.regime == "swing"
    assert profile.trend_efficiency is not None and profile.trend_efficiency < 0.35
    # 震荡票：均值回归族（箱体/VWAP/布林）必须比趋势族（macd/chan）更重
    assert profile.weights["box"] > profile.weights["macd"]
    assert profile.weights["vwap"] > profile.weights["chan"]
    assert profile.template in ("swing", "low_vol")
    assert abs(sum(profile.weights.values()) - 100.0) < 1e-6


def test_trend_stock_is_classified_and_tilts_to_structure() -> None:
    profile = analyze_character(_bars(_trend_series()), code="300308")
    assert profile.available is True
    assert profile.regime == "trend"
    assert profile.trend_efficiency is not None and profile.trend_efficiency > 0.55
    assert profile.template == "trend"
    # 趋势票：缠论结构权重被抬高（×1.25），箱体被压低（×0.85）
    balanced = analyze_character(_bars(_swing_series()), code="300308").weights
    assert profile.weights["chan"] > balanced.get("chan", 0)
    assert profile.weights["box"] < balanced.get("box", 100)


def test_two_stocks_get_different_levels_by_volatility() -> None:
    """高波动票的档位差必须显著大于低波动票 —— 这是"按个股股性"最直接的落点。"""
    high = _bars([20.0 * (1 + 0.02 * (-1) ** i) for i in range(120)], spread=0.04)
    low = _bars([20.0 * (1 + 0.002 * (-1) ** i) for i in range(120)], spread=0.004)
    high_profile = analyze_character(high, code="300308")
    low_profile = analyze_character(low, code="600036")
    assert high_profile.available and low_profile.available
    assert high_profile.grade == "活跃"
    assert low_profile.grade == "钝化"
    assert high_profile.levels["min_band_pct"] > low_profile.levels["min_band_pct"]
    assert high_profile.levels["stop_loss_pct"] > low_profile.levels["stop_loss_pct"]
    assert high_profile.t_friendly > low_profile.t_friendly
    assert low_profile.template == "low_vol"


def test_limit_up_gene_pushes_template_to_dragon() -> None:
    """有涨停基因的高换手活跃票 → 推荐「高换手题材/连板」配方。

    用主板代码（10cm 口径）构造涨停：创业板/科创板走 20cm 判定，
    拿 300xxx 配 10% 的涨幅会一个都数不出来（这是实测踩过的口径坑）。
    """
    days = 120
    closes = [10.0]
    for index in range(days - 1):
        closes.append(closes[-1] * (1.10 if index % 9 == 0 else 1.002))
    # 近 20 日放量（量能活跃度 > 1），触发 dragon 判定
    volumes = [1_000_000.0] * (days - 20) + [3_000_000.0] * 20
    profile = analyze_character(_bars(closes, volumes=volumes), code="600519")
    assert profile.available is True
    assert profile.limit_up_count >= 5
    assert profile.limit_up_freq and profile.limit_up_freq >= 0.06
    assert profile.template == "dragon"
    assert profile.weights["chip"] > 0


def test_character_snapshot_is_json_serializable() -> None:
    import json

    profile = analyze_character(_bars(_swing_series()), code="600036")
    payload = json.dumps(profile.to_dict(), ensure_ascii=False)
    assert "trend_efficiency" in payload


def test_notes_explain_the_recommendation_in_chinese() -> None:
    profile = analyze_character(_bars(_swing_series()), code="600036")
    assert profile.notes
    joined = "；".join(profile.notes)
    assert "趋势效率" in joined and "推荐权重配方" in joined
    assert "ATR" in joined


def test_high_gap_frequency_damps_vwap_weight() -> None:
    """频繁跳空的票，当日 VWAP 起点不可靠 → vwap 权重被压低。"""
    days = 120
    closes = []
    value = 20.0
    for index in range(days):
        value = value * (1.05 if index % 3 == 0 else 0.99)
        closes.append(value)
    frame = _bars(closes)
    frame["open"] = frame["close"].shift(1).fillna(frame["close"].iloc[0])
    # 人为制造大幅跳空
    frame.loc[frame.index % 3 == 0, "open"] = (
        frame.loc[frame.index % 3 == 0, "close"] * 1.06)
    profile = analyze_character(frame, code="300308")
    assert profile.available is True
    assert profile.gap_frequency is not None
    assert profile.gap_frequency > 0.25


def test_gap_frequency_is_none_when_open_price_is_unreliable() -> None:
    """日线源没有开盘价时，跳空频率必须判为不可得 —— 不能拿涨跌幅冒充它。

    实测踩过：`daily_bars_from_points` 会把缺失的 open 用 close 填上，
    于是"跳空频率"退化成"当日涨跌幅>1%的频率"，300308 因此报出 67% 的假跳空率。
    """
    frame = _bars(_swing_series())
    frame["open"] = frame["close"]  # 模拟数据源未提供开盘价
    profile = analyze_character(frame, code="300308")
    assert profile.available is True
    assert profile.gap_frequency is None
    assert any("跳空频率不可得" in note for note in profile.notes)


def test_gap_frequency_is_computed_when_open_price_is_real() -> None:
    frame = _bars(_swing_series())
    assert analyze_character(frame, code="300308").gap_frequency is not None


def test_volume_activity_ratio_tracks_recent_expansion() -> None:
    volumes = [1_000_000.0] * 80 + [3_000_000.0] * 20
    profile = analyze_character(_bars(_swing_series(100), volumes=volumes),
                                code="300308")
    assert profile.available is True
    assert profile.volume_activity is not None
    # 近20日均量 3.0M / 近60日均量 1.667M ≈ 1.8
    assert profile.volume_activity > 1.5


def test_no_nan_leaks_into_profile() -> None:
    """画像里的每个数值字段要么是有限数，要么是 None —— 不能把 NaN 传出去。"""
    import math

    frame = _bars(_swing_series())
    frame.loc[10:14, "close"] = np.nan
    profile = analyze_character(frame, code="300308")
    payload = profile.to_dict()
    for key, value in payload.items():
        if isinstance(value, float):
            assert math.isfinite(value), key
        if isinstance(value, dict):
            for inner, number in value.items():
                if isinstance(number, float):
                    assert math.isfinite(number), f"{key}.{inner}"


def test_mode_daily_recommends_daily_weights_and_no_levels() -> None:
    profile = analyze_character(_bars(_swing_series()), code="600036", mode="daily")
    assert profile.available is True
    assert set(profile.weights) == {"trend", "chan_daily", "volume", "position",
                                    "signal_rule", "cycle", "character"}
    assert abs(sum(profile.weights.values()) - 100.0) < 1e-6
    # 日线模式没有「低吸/高抛档位」概念，不应返回 levels
    assert profile.levels == {}


def test_explicit_template_override_wins() -> None:
    profile = analyze_character(_bars(_swing_series()), code="600036",
                                template_override="dragon")
    assert profile.template == "dragon"


def test_unknown_template_falls_back_to_balanced() -> None:
    profile = analyze_character(_bars(_swing_series()), code="600036",
                                template_override="not-a-template")
    assert profile.available is True
    assert profile.template == "balanced"


@pytest.mark.parametrize("days", [60, 120, 260])
def test_sample_size_does_not_break_math(days: int) -> None:
    profile = analyze_character(_bars(_swing_series(days)), code="300308")
    assert profile.available is True
    # 取样上限 250 根（约一年）—— 更久的历史股性参考价值低，且会被稀释
    assert profile.sampled_days == min(days, 250)
    assert 0 <= profile.t_friendly <= 100
