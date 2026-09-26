"""止损位护栏：不允许"开盘即跌破"的伪止损信号。

## 事故记录（2026-09-16 实盘，用户报"几乎每只股早盘低位都是红色实心止损三角"）

那排三角的位置恰恰是全天最好的回踩区。逐字段拉真实快照后定位到：

    300308（中际旭创）2026-09-16 14:44
      箱体 804.02~949.73(20日)   布林 下902.06 中905.55 上909.04（带宽仅 0.77%）
      当日开盘 869.02   当日最低 867.44
      → 回踩线 = max(箱体下沿804, 布林下轨902) = 902   ← 布林是**当日分钟bar**算的
      → 档位差护栏再围绕中轨扩张 → 回踩 898.75
      → 止损 = 898.75 × 0.99 = **889.76**，高于当日最低 867.44
      → 开盘那一刻就已"跌破止损" → 226 个分时点里 60 个（27%）被判 forced_exit

三道护栏（见 `engine.compute_levels`）：
  1. 布林下轨只有在**现价下方**才算支撑候选（强势股突破时它会跑到开盘价上方）；
  2. 回踩线必须在现价下方（跳空高开时连箱体下沿都会在现价上方）；
  3. 止损距离 = max(固定百分比, k×ATR) —— 高波动股的固定 1% 会被噪声打穿；
     且止损必须**低于当日已成交低点**（"破位才走"，而不是"开盘即跌破"）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.intraday.config import IntradayConfig
from src.intraday.engine import compute_levels

# 真实快照数值（2026-09-16 14:44 从 /api/v1/intraday/snapshot?code=300308 取）
_ZX = dict(
    price=907.06, box_high=949.73, box_low=804.02, box_span_days=20,
    boll_lower=902.0578, boll_mid=905.55, boll_upper=909.0422,
    atr=52.8205, day_low=867.44,
)


def _zx_levels(**overrides) -> object:
    config = IntradayConfig()
    params = {**_ZX, **overrides}
    return compute_levels(config=config, **params)


def test_real_case_stop_is_below_price() -> None:
    """事故复现：回踩线必须在现价下方，止损又必须在回踩线下方。

    事故当刻的真实快照：布林下轨 902.06 **高于**当日开盘 869.02 —— 那是"支撑"
    这个词的反面；取 max 会把回踩线抬到现价上方，止损随之失效。
    """
    levels = _zx_levels()
    assert levels.low_buy < levels.price
    assert levels.stop_loss < levels.low_buy
    # 止损不能退化成"贴着现价"的噪声线
    assert levels.stop_loss < levels.price * 0.99
    # 结构位（872.34）比固定 1% 口径（889.76）低 17 元，正是 ATR 口径在起作用
    assert levels.stop_loss == pytest.approx(872.34, abs=0.5)


def test_real_case_no_forced_exit_markers_on_day_bars() -> None:
    """用当日真实区间逐bar检查：不该有任何一根被判为已跌破止损。"""
    from src.intraday.engine import build_markers
    from src.intraday.models import ScoreCard

    levels = _zx_levels()
    config = IntradayConfig()
    # 当日实际走过的价格（09:30~11:30 的近似：867.44 低点 → 917.50 高点）
    prices = [869.02, 872.0, 867.44, 880.0, 884.59, 900.0, 906.86, 917.5, 907.06]
    replay = pd.DataFrame([
        {"ts": f"2026-09-16 09:{30 + index:02d}", "close": price, "total": 0.0}
        for index, price in enumerate(prices)])
    scorecard = ScoreCard(
        total=0.0, threshold_action=config.thresholds.action,
        threshold_hint=config.thresholds.hint, zone="neutral",
        verdict="测试", factors=[], weights_sum=100.0, available_weight=100.0)
    markers = build_markers(
        replay=replay, levels=levels, scorecard=scorecard, config=config)
    forced = [(m.ts, m.price) for m in markers if m.kind == "stop_loss"]
    assert forced == [], f"这些bar被误判为止损：{forced}"


def test_atr_gives_wider_stop_for_volatile_stock() -> None:
    """高波动股：ATR 口径的止损距离必须大于固定百分比口径。

    300308 的 ATR=52.8（占价 5.8%），固定 1% 只有 9 元 —— 任何噪声都能打穿。
    这里把"当日最低"放到远离止损处，单独观察 ATR 的作用（事故场景里它被
    当日最低价的上限遮住了，见下一条用例）。
    """
    config = IntradayConfig()
    assert config.levels.atr_stop_mult == pytest.approx(0.5)
    with_atr = _zx_levels(day_low=700.0)
    without_atr = _zx_levels(day_low=700.0, atr=None)
    assert with_atr.stop_loss < without_atr.stop_loss - 15.0
    assert "ATR" in with_atr.stop_basis


def test_day_low_is_reported_in_basis() -> None:
    """当日最低价要写进止损依据：用户据此能判断这条线离现价有多远、可不可信。"""
    levels = _zx_levels()
    assert "当日最低 867.44" in levels.stop_basis


def test_atr_stop_can_be_disabled() -> None:
    """atr_stop_mult=0 → 退回纯百分比口径（保留旧行为的口子）。"""
    config = IntradayConfig()
    config.levels.atr_stop_mult = 0.0
    levels = compute_levels(config=config, **_ZX)
    assert "ATR" not in levels.stop_basis
    # 仍要报出当日最低（这是止损依据的一部分）
    assert "当日最低" in levels.stop_basis


def test_boll_lower_above_price_is_not_a_support() -> None:
    """布林下轨跑到现价上方时不算支撑（否则回踩线会被抬到现价之上）。"""
    config = IntradayConfig()
    price = 869.02          # 当日开盘
    levels = compute_levels(
        config=config, price=price, box_high=949.73, box_low=804.02,
        box_span_days=20, boll_lower=902.06, boll_upper=909.04, atr=52.82,
        day_low=867.44)
    assert levels.low_buy < price, (
        f"回踩线 {levels.low_buy} 高于现价 {price} —— 布林下轨 902 被当成了支撑")
    assert levels.stop_loss < price


def test_low_buy_reanchors_when_box_low_is_above_price() -> None:
    """跳空高开（连箱体下沿都在现价上方）时，回踩线退化为现价下方的防守位。"""
    config = IntradayConfig()
    levels = compute_levels(
        config=config, price=100.0, box_high=140.0, box_low=105.0,
        box_span_days=20, day_low=99.0)
    assert levels.low_buy < 100.0
    assert levels.stop_loss < levels.low_buy


def test_stop_basis_is_human_readable() -> None:
    """止损位要能解释自己是怎么来的 —— 用户看到数字没法判断该不该信它。"""
    levels = _zx_levels()
    assert levels.stop_basis
    assert ("%" in levels.stop_basis) or ("ATR" in levels.stop_basis)
    assert len(levels.stop_basis) < 120


def test_normal_stock_behavior_unchanged() -> None:
    """低波动股（ATR 远小于固定百分比）不受影响：止损仍在回踩线下方 1%。"""
    config = IntradayConfig()
    levels = compute_levels(
        config=config, price=40.97, box_high=42.0, box_low=40.5,
        box_span_days=20, boll_lower=40.6, boll_upper=41.8,
        atr=0.69, day_low=40.54)
    assert levels.stop_loss == pytest.approx(levels.low_buy * 0.99, abs=0.02)
    assert levels.stop_loss < 40.54


def test_levels_survive_missing_day_low() -> None:
    """没有当日低点（盘前/数据缺口）时不得报错，且仍满足"止损低于回踩线"。"""
    config = IntradayConfig()
    levels = compute_levels(
        config=config, price=100.0, box_high=110.0, box_low=95.0,
        box_span_days=20, atr=None, day_low=None)
    assert levels.stop_loss < levels.low_buy < levels.high_sell


def test_replay_markers_stop_loss_requires_actual_breakdown() -> None:
    """回放打点也要跟着变：止损位在当日最低之下时，不该有任何止损标记。"""
    from src.intraday.engine import build_markers
    from src.intraday.models import ScoreCard

    config = IntradayConfig()
    levels = _zx_levels()
    prices = [869.02, 872.0, 867.44, 884.59, 906.86, 907.06]
    replay = pd.DataFrame([
        {"ts": f"2026-09-16 09:{30 + index:02d}", "close": price, "total": 0.0}
        for index, price in enumerate(prices)])
    scorecard = ScoreCard(
        total=0.0, threshold_action=config.thresholds.action,
        threshold_hint=config.thresholds.hint, zone="neutral",
        verdict="测试", factors=[], weights_sum=100.0, available_weight=100.0)
    markers = build_markers(
        replay=replay, levels=levels, scorecard=scorecard, config=config)
    assert [m for m in markers if m.kind == "stop_loss"] == []
