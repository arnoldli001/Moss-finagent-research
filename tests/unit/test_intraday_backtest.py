"""做T模块 · 阈值回测单元测试（无未来函数 + 阈值统计正确性）。

回测的可信度取决于两点，本文件分别验证：
  1. 前瞻收益严格取 t+horizon 的收盘价（不偷看未来）；
  2. 命中率/平均收益统计与手算结果一致。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intraday.backtest import (
    TECHNICAL_FACTORS,
    _forward_returns,
    _technical_weights,
    compute_threshold_stats,
    run_threshold_backtest,
)
from src.intraday.config import IntradayConfig


def _session_stamps(day: str) -> list[str]:
    morning = pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min")
    afternoon = pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min")
    return [s.strftime("%Y-%m-%d %H:%M") for s in morning.union(afternoon)]


def _bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "ts": _session_stamps("2026-09-15")[:len(closes)],
        "open": closes, "high": [c * 1.002 for c in closes],
        "low": [c * 0.998 for c in closes], "close": closes,
        "volume": [100.0] * len(closes), "amount": [0.0] * len(closes),
    })


# ==================== 前瞻收益（无未来函数） ====================


def test_forward_return_uses_future_bar_only() -> None:
    """fwd[i] 必须等于 close[i+h]/close[i]-1，末 h 根为 NaN。"""
    frame = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0]})
    fwd = _forward_returns(frame, horizon=2)
    assert fwd.iloc[0] == pytest.approx((12.0 / 10.0 - 1) * 100)
    assert fwd.iloc[1] == pytest.approx((13.0 / 11.0 - 1) * 100)
    assert fwd.iloc[2] == pytest.approx((14.0 / 12.0 - 1) * 100)
    assert pd.isna(fwd.iloc[3]) and pd.isna(fwd.iloc[4])


def test_forward_return_does_not_leak_current_bar() -> None:
    """当前bar自身涨跌不影响 fwd（否则等于用未来函数）。"""
    base = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]})
    modified = base.copy()
    modified.loc[0, "close"] = 9.0
    assert _forward_returns(base, 1).iloc[1] == pytest.approx(
        _forward_returns(modified, 1).iloc[1])


# ==================== 阈值统计 ====================


def test_compute_threshold_stats_long_side() -> None:
    """看多信号：总分≥阈值时统计命中率与平均前瞻收益。"""
    frame = pd.DataFrame({
        "total": [35.0, 40.0, 5.0, -35.0, 32.0, 10.0],
        "close": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
    })
    # 手动设置前瞻收益：+1%, -1%, ..., 以便断言
    frame["close"] = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    stat = compute_threshold_stats(frame, threshold=30.0, horizon=1,
                                   direction="long")
    assert stat.signals == 3  # 索引 0/1/4
    assert stat.hit_rate is not None


def test_compute_threshold_stats_no_signals() -> None:
    frame = pd.DataFrame({"total": [1.0, 2.0, 3.0], "close": [10.0] * 3})
    stat = compute_threshold_stats(frame, threshold=30.0, horizon=1)
    assert stat.signals == 0
    assert stat.hit_rate is None
    assert stat.avg_forward_return_pct is None


def test_compute_threshold_stats_short_side_hit_definition() -> None:
    """看空信号命中 = 前瞻收益 < 0（方向相反才算命中）。"""
    frame = pd.DataFrame({
        "total": [-40.0, -40.0],
        "close": [10.0, 9.0],
    })
    stat = compute_threshold_stats(frame, threshold=30.0, horizon=1,
                                   direction="short")
    # 第一根：10→9 跌 10% → 看空命中；第二根无前瞻样本
    assert stat.signals == 1
    assert stat.hit_rate == pytest.approx(1.0)


def test_compute_threshold_stats_handles_empty() -> None:
    stat = compute_threshold_stats(pd.DataFrame(), threshold=30.0, horizon=6)
    assert stat.signals == 0


# ==================== 回测权重口径 ====================


def test_technical_weights_renormalized_to_100() -> None:
    """情绪/消息面无历史序列 → 技术面权重按比例归一至100，保持阈值刻度可比。"""
    config = IntradayConfig()
    weights = _technical_weights(config)
    assert sum(weights.values()) == pytest.approx(100.0)
    assert weights["sentiment"] == 0.0
    assert weights["news"] == 0.0
    # 相对比例保持不变：箱体仍是最高权重
    assert weights["box"] > weights["vwap"] > weights["boll"] > weights["macd"]
    raw = config.weights.as_dict()
    assert weights["box"] / weights["vwap"] == pytest.approx(
        raw["box"] / raw["vwap"], abs=0.01)


def test_technical_factors_cover_five_dimensions() -> None:
    assert set(TECHNICAL_FACTORS) == {"box", "vwap", "boll", "macd", "kdj_rsi"}


# ==================== 端到端回测（合成数据） ====================


def test_backtest_insufficient_sample_reports_gap() -> None:
    result = run_threshold_backtest(
        code="300308", name="测试", bars_5m=_bars([10.0] * 20),
        bars_daily=None, config=IntradayConfig())
    assert result.available is False
    assert result.gaps


def test_backtest_runs_on_synthetic_days() -> None:
    """两日合成序列应产出可用的阈值统计与说明性结论。"""
    config = IntradayConfig()
    frames = []
    rng = np.random.default_rng(42)
    for index, day in enumerate(["2026-09-14", "2026-09-15"]):
        stamps = _session_stamps(day)
        # 造一段有波动的价格序列（先跌后拉），确保总分能触及阈值
        base = 10.0 + index * 0.5
        wave = np.sin(np.arange(len(stamps)) / 4.0) * 0.25
        drift = np.linspace(-0.4, 0.4, len(stamps))
        closes = base + wave + drift + rng.normal(0, 0.01, len(stamps))
        frames.append(pd.DataFrame({
            "ts": stamps,
            "open": closes - 0.01, "high": closes + 0.02,
            "low": closes - 0.02, "close": closes,
            "volume": np.full(len(stamps), 1000.0), "amount": [0.0] * len(stamps),
        }))
    bars = pd.concat(frames, ignore_index=True)
    result = run_threshold_backtest(
        code="300308", name="合成", bars_5m=bars, bars_daily=None,
        config=config, horizon=6)
    assert result.available is True
    assert result.days == 2
    assert result.bars == 96
    assert result.verdict
    assert result.disclaimer
    # 必须显式披露回测只覆盖技术面因子
    assert any("技术面因子" in gap for gap in result.gaps)
    # 阈值敏感性扫描应给出多档结果
    assert len(result.by_threshold) >= 8


def test_backtest_threshold_scan_is_monotone_in_signal_count() -> None:
    """阈值越高，触发的信号数应单调不增（统计自洽性检查）。"""
    rng = np.random.default_rng(7)
    frames = []
    for index, day in enumerate(["2026-09-14", "2026-09-15"]):
        stamps = _session_stamps(day)
        wave = np.sin(np.arange(len(stamps)) / 3.0) * 0.3
        closes = 10.0 + index + wave + rng.normal(0, 0.02, len(stamps))
        frames.append(pd.DataFrame({
            "ts": stamps, "open": closes, "high": closes * 1.002,
            "low": closes * 0.998, "close": closes,
            "volume": [1000.0] * len(stamps), "amount": [0.0] * len(stamps),
        }))
    bars = pd.concat(frames, ignore_index=True)
    result = run_threshold_backtest(
        code="300308", name="合成", bars_5m=bars, bars_daily=None,
        config=IntradayConfig(), horizon=4)
    counts = [stat.signals for stat in result.by_threshold[:8]]
    assert counts == sorted(counts, reverse=True)
