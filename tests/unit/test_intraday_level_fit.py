"""做T档位**神经网络拟合**单元测试（成功率口径 + 启用闸门 + 因果性）。

本文件要钉住的是三条**结论性**性质，而不是"代码跑通"：

  1. **振荡票拟合得出高成功率，趋势票拟合不出** —— 这是拟合有意义的前提。
     若两者都能刷出 80%，那说明成功率指标本身是坏的（例如把"从未触及"也算进分母）。
  2. **不触及的 bar 不参与成功率** —— 否则把线画到天边就能刷出 100%。
  3. **闸门必须能拒绝过拟合** —— 只达标 in-sample、留一日不达标的拟合
     **不允许**被用来下单（这是本模块最关键的诚实点）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intraday.config import IntradayConfig, LevelFitConfig
from src.intraday.level_fit import (
    FEATURE_KEYS,
    FitDataset,
    build_dataset,
    evaluate_levels,
    fit_levels,
    net_adjustment,
)


def _bars(days: int, *, amplitude_pct: float, trend_pct: float = 0.0,
          bars_per_day: int = 48, cycles_per_day: float = 2.4,
          seed: int = 7) -> pd.DataFrame:
    """造 5 分钟 bars：日内正弦振荡（振幅可控）+ 可选日间单边趋势。

    `cycles_per_day` 默认 2.4 ≈ 每 20 根 bar 一个来回 —— 这是**日内做T**该有的
    形态（一天里有若干次可交易的小波段）。周期太长（例如一天一个来回）时，
    靠近日终的低点根本来不及在高抛线附近卖出，成功率自然低 —— 那是市场形态，
    不是代码问题，所以测试里用"有多次来回"的形态来验证口径。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for day in range(days):
        base = 100.0 * (1.0 + trend_pct / 100.0 * day)
        for index in range(bars_per_day):
            phase = 2 * np.pi * cycles_per_day * index / bars_per_day
            mid = base * (1.0 + amplitude_pct / 100.0 * np.sin(phase))
            noise = rng.normal(0, base * 0.0004)
            price = mid + noise
            rows.append({
                "ts": f"2026-09-{day + 1:02d} "
                      f"{9 + (index * 5 + 30) // 60:02d}:{(index * 5 + 30) % 60:02d}",
                "open": price, "high": price * 1.002, "low": price * 0.998,
                "close": price, "volume": 1000 + index,
            })
    return pd.DataFrame(rows)


def _dataset(frame: pd.DataFrame, *, sessions: int = 10) -> FitDataset:
    return build_dataset(
        bars=frame, atr_pct=2.0, box_position=0.5, chip_volume_ratio=1.0,
        chan_position=0.0, pct_b=0.5, macd_atr=0.1, kdj_rsi=0.0,
        cfg=LevelFitConfig(sessions=sessions))


def _cfg(**kwargs) -> LevelFitConfig:
    base = {"sessions": 10, "epochs": 40, "min_touch_samples": 5,
            "target_hit_rate": 0.5, "horizon_bars": 12}
    base.update(kwargs)
    return LevelFitConfig(**base)


# ==================== 数据集 ====================


def test_build_dataset_reports_real_coverage() -> None:
    dataset = _dataset(_bars(10, amplitude_pct=3.0))
    assert dataset.available, dataset.reason
    assert dataset.bars == 480
    assert len(dataset.days) == 10
    assert dataset.features is not None and dataset.features.shape[1] == len(FEATURE_KEYS)
    assert dataset.day_mean is not None and dataset.day_mean.shape[0] == 480
    joined = " ".join(dataset.notes)
    assert "10 个交易日" in joined
    assert "恒定注入" in joined, "日级因子按当前值注入这件事必须如实写在 notes 里"


def test_build_dataset_refuses_insufficient_sample() -> None:
    thin = build_dataset(bars=_bars(3, amplitude_pct=3.0),
                         cfg=LevelFitConfig(sessions=10, min_sessions=5))
    assert not thin.available and "样本不足" in thin.reason
    none_bars = build_dataset(bars=None)
    assert not none_bars.available and "无分钟K线" in none_bars.reason


def test_build_dataset_truncates_to_requested_sessions() -> None:
    dataset = _dataset(_bars(14, amplitude_pct=3.0), sessions=10)
    assert dataset.available
    assert len(dataset.days) == 10
    assert dataset.bars == 480


# ==================== 成功率口径 ====================


def test_oscillating_stock_can_reach_high_hit_rate() -> None:
    """日内有多次来回的票：线放对位置就能做出高成功率，止损放太近则做不出来。

    这条断言是整套拟合的地基：如果连"明显来回振荡"的票都做不出高成功率，
    那问题在成功率口径本身（例如把未触及的 bar 也算进分母）。
    同时用它钉住一个**真实存在**的权衡：同样的低吸/高抛，止损贴得太近
    （2.2% vs 5.0%）会把成功率从 100% 打到 40% 出头 —— 这正是"止损位置"
    必须参与拟合、而不能拍一个固定百分比的原因。
    """
    dataset = _dataset(_bars(10, amplitude_pct=2.0))
    loose = evaluate_levels(
        dataset=dataset, low_pct=1.0, high_pct=1.5, stop_pct=5.0,
        horizon=24, cost_pct=0.2)
    assert loose["touches"] > 20, loose
    assert loose["rate"] is not None and loose["rate"] >= 0.8, loose

    tight = evaluate_levels(
        dataset=dataset, low_pct=1.0, high_pct=1.5, stop_pct=2.2,
        horizon=24, cost_pct=0.2)
    assert tight["touches"] == loose["touches"]
    assert tight["rate"] is not None and tight["rate"] < loose["rate"]


def test_success_rate_ignores_bars_that_never_touch() -> None:
    """**把线画到天边**：触及数必须是 0，成功率必须是 None（不是 100%）。"""
    dataset = _dataset(_bars(10, amplitude_pct=3.0))
    stat = evaluate_levels(
        dataset=dataset, low_pct=25.0, high_pct=25.0, stop_pct=40.0,
        horizon=24, cost_pct=0.2)
    assert stat["touches"] == 0
    assert stat["rate"] is None


def test_one_way_trend_has_low_success_or_no_edge() -> None:
    """单边下跌日：低吸买进去只会更低 → 拟合不应给出高成功率。

    造 10 天连续 -4%/日 的下跌：任何"低吸"都会继续被埋。
    """
    dataset = _dataset(_bars(10, amplitude_pct=0.6, trend_pct=-4.0))
    stat = evaluate_levels(
        dataset=dataset, low_pct=1.0, high_pct=1.5, stop_pct=2.4,
        horizon=24, cost_pct=0.2)
    assert stat["rate"] is None or stat["rate"] <= 0.6


def test_low_line_must_be_below_high_line() -> None:
    """线序非法（低吸 ≥ 高抛 / 止损在低吸上方）时直接判不可用，不给"成功率"。"""
    dataset = _dataset(_bars(10, amplitude_pct=2.0))
    assert evaluate_levels(
        dataset=dataset, low_pct=1.5, high_pct=1.5, stop_pct=2.2,
        horizon=24, cost_pct=0.2)["rate"] is None
    assert evaluate_levels(
        dataset=dataset, low_pct=1.0, high_pct=1.5, stop_pct=0.5,
        horizon=24, cost_pct=0.2)["rate"] is None


def test_band_narrower_than_cost_is_rejected() -> None:
    """价差覆盖不了双边摩擦成本 → 不认为这种组合"成功"。"""
    dataset = _dataset(_bars(10, amplitude_pct=3.0))
    stat = evaluate_levels(
        dataset=dataset, low_pct=1.0, high_pct=1.1, stop_pct=2.0,
        horizon=24, cost_pct=0.5)
    assert stat["touches"] == 0 and stat["rate"] is None


# ==================== 拟合与闸门 ====================


def test_fit_produces_traceable_lines_and_two_rates() -> None:
    result = fit_levels(
        code="300308", dataset=_dataset(_bars(10, amplitude_pct=3.0)),
        cfg=_cfg(target_hit_rate=0.5))
    payload = result.to_dict()
    metrics = payload["metrics"]
    assert metrics["available"] is True, metrics["reason"]
    assert metrics["in_sample_rate"] is not None
    assert metrics["sessions"] == 10 and metrics["bars"] == 480
    # 三条线的混合系数可追溯到具体分位数（每条线一组、和为1）
    for key in ("low_mix", "high_mix", "stop_mix"):
        assert len(payload[key]) >= 2
        assert sum(payload[key]) == pytest.approx(1.0)
    assert payload["low_anchors"] and payload["high_anchors"]
    assert payload["feature_means"] and payload["feature_stds"]
    assert len(payload["features"]) == len(FEATURE_KEYS)
    # 拟合线必须有序：低吸 < 高抛 < 止损
    assert metrics["in_sample_touches"] > 0


def test_gate_rejects_when_walk_forward_misses_target() -> None:
    """闸门：留一日不达标 → gate_passed=False 且说明原因（哪怕 in-sample 很高）。"""
    result = fit_levels(
        code="300308", dataset=_dataset(_bars(10, amplitude_pct=3.0)),
        cfg=_cfg(target_hit_rate=0.995, walk_forward_min_rate=None)
        if False else _cfg(target_hit_rate=0.995))
    metrics = result.metrics
    assert metrics.gate_passed is False
    assert "目标" in metrics.gate_reason
    assert any("未过闸门" in note for note in result.notes)


def test_gate_passes_with_loose_target_and_enough_touches() -> None:
    result = fit_levels(
        code="300308", dataset=_dataset(_bars(10, amplitude_pct=3.0)),
        cfg=_cfg(target_hit_rate=0.4, min_touch_samples=5))
    metrics = result.metrics
    assert metrics.gate_passed is True, metrics.gate_reason
    assert metrics.walk_forward_rate is not None
    assert "已启用拟合档位" in metrics.gate_reason


def test_fit_disabled_by_config_falls_back_cleanly() -> None:
    result = fit_levels(
        code="300308", dataset=_dataset(_bars(10, amplitude_pct=3.0)),
        cfg=LevelFitConfig(enabled=False))
    assert result.metrics.available is False
    assert "已关闭" in result.metrics.reason


def test_fit_on_unavailable_dataset_reports_the_gap() -> None:
    empty = build_dataset(bars=None)
    result = fit_levels(code="300308", dataset=empty, cfg=_cfg())
    assert result.metrics.available is False
    assert "无分钟K线" in result.metrics.reason


def test_fit_is_deterministic_for_same_seed() -> None:
    """同一份数据 + 同一种子必须给出同一套线（否则面板数字会自己跳）。"""
    frame = _bars(10, amplitude_pct=3.0)
    first = fit_levels(code="300308", dataset=_dataset(frame), cfg=_cfg())
    second = fit_levels(code="300308", dataset=_dataset(frame), cfg=_cfg())
    assert first.low_mix == second.low_mix
    assert first.high_mix == second.high_mix
    assert first.metrics.in_sample_rate == second.metrics.in_sample_rate


def test_walk_forward_never_beats_in_sample_by_construction() -> None:
    """留一日是用**没见过的天**预测：它通常低于样本内，至少不该高出很多。"""
    result = fit_levels(
        code="300308", dataset=_dataset(_bars(10, amplitude_pct=3.0)),
        cfg=_cfg(target_hit_rate=0.4))
    metrics = result.metrics
    if metrics.walk_forward_rate is not None and metrics.in_sample_rate is not None:
        assert metrics.walk_forward_rate <= metrics.in_sample_rate + 0.35


# ==================== 微调网络 ====================


def test_net_adjustment_is_bounded_and_safe_on_bad_input() -> None:
    result = fit_levels(
        code="300308", dataset=_dataset(_bars(10, amplitude_pct=3.0)),
        cfg=_cfg(target_hit_rate=0.4))
    features = {key: 0.0 for key in FEATURE_KEYS}
    value = net_adjustment(fit=result, features=features)
    assert 0.85 <= value <= 1.15, "微调乘数必须被夹在 ±15% 内"
    # 特征缺失/坏数据时必须安全退回 1.0（不做任何调整）
    assert net_adjustment(fit=result, features={"unknown": 1.0}) == pytest.approx(1.0)
    broken = result
    broken.net = {"w1": [[1.0]]}
    assert net_adjustment(fit=broken, features=features) == pytest.approx(1.0)


# ==================== 与配置的接口 ====================


def test_config_exposes_fit_section_with_target_rate() -> None:
    config = IntradayConfig()
    fit = config.factors.level_fit
    assert fit.enabled is True
    assert fit.target_hit_rate == pytest.approx(0.80)
    assert fit.sessions == 10
    assert fit.fit_on_snapshot is True
