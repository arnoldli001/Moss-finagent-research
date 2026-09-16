"""技能库四因子的打分核 + 权重模板单测（分时做T 的 chan/chip/cycle/character）。

为什么这些用例值得写：
它们是"把交易经验接进量化"的**唯一可验证边界** —— 上游那套战法是否成立无法用
单测证明，但"给定这些输入，公式必须产出这个分数"是可以钉死的。
一旦有人（包括以后的我）改了公式，这些用例会立刻指出改了哪一项。
"""

from __future__ import annotations

import pytest

from src.intraday.character import character_score_from, limit_up_pct_for
from src.intraday.config import (
    ChanParams,
    CharacterParams,
    ChipParams,
    CycleParams,
    FactorParams,
    IntradayConfig,
)
from src.intraday.factors import (
    FACTOR_ORDER,
    FactorContext,
    chan_score_from,
    chip_score_from,
    cycle_score_from,
    run_factor,
)
from src.intraday.weight_profiles import (
    DEFAULT_WEIGHTS,
    diff_weights,
    factor_keys,
    factor_meta,
    merge_weights,
    normalize_weights,
    template,
    templates,
    weights_sum,
)

# ==================== 因子目录与权重 ====================

def test_intraday_catalog_has_fourteen_factors_and_matches_weights() -> None:
    """目录、FACTOR_ORDER、Weights 三者必须完全一致（任一处漂移都直接报错）。"""
    assert len(FACTOR_ORDER) == 14
    assert set(FACTOR_ORDER) == set(IntradayConfig().weights.model_dump())
    assert factor_keys("intraday") == FACTOR_ORDER
    assert len(factor_meta("intraday")) == 14


def test_daily_catalog_has_seven_factors_and_matches_weights() -> None:
    assert len(factor_keys("daily")) == 7
    assert set(factor_keys("daily")) == set(
        IntradayConfig().daily_weights.model_dump())


def test_default_intraday_weights_keep_box_highest() -> None:
    """总权重配平后「箱体仍是单项最高」，且技能四因子真的占 29 分。"""
    weights = DEFAULT_WEIGHTS["intraday"]
    assert weights_sum(weights, "intraday") == pytest.approx(100.0)
    assert max(weights, key=lambda k: weights[k]) == "box"
    skill = sum(weights[key] for key in ("chan", "chip", "cycle", "character"))
    assert skill == pytest.approx(29.0)


@pytest.mark.parametrize("mode", ["intraday", "daily"])
def test_every_template_sums_to_100(mode: str) -> None:
    """所有预设配方必须各自合计=100 —— 否则用户点一下模板就会拿到 400。"""
    for item in templates(mode):
        assert weights_sum(item.normalized(), mode) == pytest.approx(100.0), item.key


def test_legacy_template_zeroes_skill_factors() -> None:
    item = template("legacy", "intraday")
    assert item is not None
    weights = item.normalized()
    for key in ("chan", "chip", "cycle", "character"):
        assert weights[key] == 0.0
    assert weights["box"] == pytest.approx(24.0)


def test_normalize_keeps_user_zero_and_hits_exact_target() -> None:
    """归一化：用户刻意置0的项不能被"复活"，且结果必须**精确**等于100。"""
    weights = normalize_weights({"box": 30, "vwap": 10, "chan": 0}, "intraday")
    assert weights["chan"] == 0.0
    assert weights["box"] == pytest.approx(75.0)
    assert weights_sum(weights, "intraday") == pytest.approx(100.0)
    # 三项等权 33.33… 的取整必须仍然精确 100（最大余额法）
    odd = normalize_weights({"box": 1, "vwap": 1, "boll": 1}, "intraday")
    assert weights_sum(odd, "intraday") == pytest.approx(100.0)


def test_normalize_falls_back_when_all_zero() -> None:
    """全 0 不能返回全 0（那会让总分恒为 0 却仍显示"有效权重100"）。"""
    weights = normalize_weights(dict.fromkeys(factor_keys("intraday"), 0.0), "intraday")
    assert weights_sum(weights, "intraday") == pytest.approx(100.0)


def test_merge_and_diff_weights_report_only_changed() -> None:
    base = DEFAULT_WEIGHTS["intraday"]
    merged = merge_weights(base, {"chan": 20.0}, "intraday")
    assert merged["chan"] == pytest.approx(20.0)
    changed = diff_weights(base, merged, "intraday")
    assert len(changed) == 1
    assert changed[0]["key"] == "chan"
    assert changed[0]["delta"] == pytest.approx(8.0)


# ==================== 缠论结构分 ====================

def test_chan_score_below_pivot_is_positive_and_saturates() -> None:
    """中枢下方 = 超跌 → 正分（利于低吸），且离开中枢超过 overshoot 后饱和。

    默认 `divergence_weight=0.5`：位置分与背驰分各占一半，
    所以这里看到的分数是"位置分 × 0.5"（无背驰时背驰项为 0）。
    """
    near = chan_score_from(price=94.0, zd=95.0, zg=105.0, overshoot=0.5)
    far = chan_score_from(price=80.0, zd=95.0, zg=105.0, overshoot=0.5)
    assert near is not None and far is not None
    assert near > 0 and far > 0
    # (95-94)/10 = 0.1 → clip(0.1/0.5)=0.2 → ×(1-0.5)=0.1
    assert near == pytest.approx(0.1, abs=1e-6)
    # 饱和：p=-1.5 → clip(1.5/0.5)=1.0 → ×0.5
    assert far == pytest.approx(0.5, abs=1e-6)
    # 纯位置分（背驰权重置0）时不打折
    pure = chan_score_from(price=80.0, zd=95.0, zg=105.0, overshoot=0.5,
                           divergence_weight=0.0)
    assert pure == pytest.approx(1.0, abs=1e-6)


def test_chan_score_above_pivot_is_negative_and_saturates() -> None:
    near = chan_score_from(price=106.0, zd=95.0, zg=105.0, overshoot=0.5)
    far = chan_score_from(price=130.0, zd=95.0, zg=105.0, overshoot=0.5)
    assert near is not None and far is not None
    assert near < 0 and far < 0
    assert far == pytest.approx(-0.5, abs=1e-6)


def test_chan_score_inside_pivot_is_mild_mean_reversion() -> None:
    low = chan_score_from(price=96.0, zd=95.0, zg=105.0)
    mid = chan_score_from(price=100.0, zd=95.0, zg=105.0)
    high = chan_score_from(price=104.0, zd=95.0, zg=105.0)
    assert low is not None and mid is not None and high is not None
    assert mid == pytest.approx(0.0, abs=1e-9)
    assert low > 0 > high
    # 中枢内系数 0.6：-clip((0.9-0.5)*2) × 0.6 = -0.48，再 ×(1-0.5)
    assert high == pytest.approx(-0.24, abs=1e-6)


def test_chan_divergence_overrides_and_conflicts_cancel() -> None:
    """底背驰加成、顶背驰扣分；位置与背驰矛盾时互相抵消（不是强行裁决）。"""
    bottom = chan_score_from(price=94.0, zd=95.0, zg=105.0,
                             divergence="bottom", divergence_strength=0.8)
    top = chan_score_from(price=106.0, zd=95.0, zg=105.0,
                          divergence="top", divergence_strength=0.8)
    assert bottom is not None and top is not None
    assert bottom > 0.3 and top < -0.3
    # 顶背驰但价格在中枢下方：位置给正、背驰给负 → 落在两者之间
    conflict = chan_score_from(price=94.0, zd=95.0, zg=105.0,
                               divergence="top", divergence_strength=1.0,
                               divergence_weight=0.5)
    assert conflict is not None
    assert -0.6 < conflict < 0.2


def test_chan_score_rejects_degenerate_pivot() -> None:
    assert chan_score_from(price=100.0, zd=100.0, zg=100.0) is None
    assert chan_score_from(price=None, zd=95.0, zg=105.0) is None


def test_score_chan_reports_gap_when_unavailable() -> None:
    outcome = run_factor("chan", FactorContext(price=100.0), FactorParams())
    assert outcome.available is False
    assert outcome.score == 0.0
    assert outcome.gap and "笔/中枢" in outcome.gap


# ==================== 筹码量能 ====================

def test_chip_volume_only_amplifies_direction() -> None:
    """量能只放大方向：低位放量→强正、高位放量→强负、缩量→被压回中性附近。

    量能强度 = clip(量比 - 1)，幅度因子 = `0.5 + 0.5×强度`：
    倍量(强度1)→×1.0；平量(强度0)→×0.5；极度缩量(强度-1)→×0。
    **方向始终由位置决定，量能本身不给方向**。
    """
    low_big = chip_score_from(volume_ratio=2.0, position=0.1)
    high_big = chip_score_from(volume_ratio=2.0, position=0.9)
    low_shrink = chip_score_from(volume_ratio=0.5, position=0.1)
    high_shrink = chip_score_from(volume_ratio=0.5, position=0.9)
    assert low_big is not None and high_big is not None
    assert low_shrink is not None and high_shrink is not None
    assert low_big > 0.5 and high_big < -0.5
    # 缩量 → 幅度因子只有 0.25，得分被压到放量时的 1/4，但方向不变
    assert 0 < low_shrink < low_big
    assert 0 > high_shrink > high_big
    assert low_shrink == pytest.approx(low_big * 0.25, abs=1e-9)
    # 极度缩量（量比 0）→ 幅度因子 0 → 中性
    assert chip_score_from(volume_ratio=0.0, position=0.1) == pytest.approx(0.0)


def test_chip_flat_volume_is_neutral() -> None:
    score = chip_score_from(volume_ratio=1.0, position=0.0)
    assert score is not None and score == pytest.approx(0.5, abs=1e-9)


def test_chip_thin_turnover_is_damped_not_flipped() -> None:
    """换手率<1%只打折，不反转 —— 流动性差不构成看空理由。"""
    normal = chip_score_from(volume_ratio=2.0, position=0.1, turnover_pct=3.0)
    thin = chip_score_from(volume_ratio=2.0, position=0.1, turnover_pct=0.4)
    assert normal is not None and thin is not None
    assert thin == pytest.approx(normal * 0.7, abs=1e-9)
    assert thin > 0


def test_chip_requires_volume_and_position() -> None:
    assert chip_score_from(volume_ratio=None, position=0.2) is None
    assert chip_score_from(volume_ratio=1.5, position=None) is None


def test_score_chip_marks_gap_without_daily_volume() -> None:
    ctx = FactorContext(price=10.0, chip_volume_ratio=1.5, chip_position=0.2,
                        chip_available=False, chip_gap="缺少当日成交量或近5日均量")
    outcome = run_factor("chip", ctx, FactorParams())
    assert outcome.available is False
    assert outcome.gap == "缺少当日成交量或近5日均量"


# ==================== 情绪周期温度 ====================

@pytest.mark.parametrize(
    ("temperature", "expected"),
    [(100.0, 1.0), (75.0, 0.5), (50.0, 0.0), (25.0, -0.5), (0.0, -1.0)],
)
def test_cycle_score_is_piecewise_linear_around_neutral(
        temperature: float, expected: float) -> None:
    score = cycle_score_from(temperature=temperature, neutral=50.0)
    assert score == pytest.approx(expected, abs=1e-6)


def test_cycle_score_tolerates_out_of_range_and_missing() -> None:
    assert cycle_score_from(temperature=None) is None
    assert cycle_score_from(temperature=180.0) == pytest.approx(1.0)
    assert cycle_score_from(temperature=-40.0) == pytest.approx(-1.0)


def test_score_cycle_disabled_by_config() -> None:
    ctx = FactorContext(price=10.0, cycle_available=True, cycle_temperature=80.0,
                        cycle_stage="发酵期", cycle_t_allowed=True)
    outcome = run_factor("cycle", ctx, FactorParams(cycle=CycleParams(enabled=False)))
    assert outcome.available is False
    assert "已关闭" in outcome.detail


def test_score_cycle_reports_gates_and_veto() -> None:
    ctx = FactorContext(
        price=10.0, cycle_available=True, cycle_temperature=12.0,
        cycle_stage="退潮期", cycle_t_allowed=False,
        cycle_gates=["大面 12 家 ≥ 10（一票否决）"])
    outcome = run_factor("cycle", ctx, FactorParams())
    assert outcome.available is True
    assert outcome.score < 0
    assert "一票否决" in outcome.detail
    assert outcome.inputs["t_allowed"] is False


# ==================== 股性适配 ====================

def test_character_score_uses_own_atr_as_denominator() -> None:
    """同一偏离在高低波动票上得分不同 —— 这正是"按股性"的核心。"""
    high_vol = character_score_from(dev_pct=-1.7, atr_pct=5.8,
                                    trend_efficiency=0.3, scale=0.35)
    low_vol = character_score_from(dev_pct=-1.7, atr_pct=1.6,
                                   trend_efficiency=0.3, scale=0.35)
    assert high_vol is not None and low_vol is not None
    assert 0 < high_vol < low_vol
    assert low_vol == pytest.approx(1.0, abs=1e-6)  # 低波动票的偏离已饱和


def test_character_score_discounts_trend_stocks() -> None:
    """趋势票上同样的偏离要打折（偏离常是趋势本身，不是超买超卖）。"""
    swing = character_score_from(dev_pct=-1.7, atr_pct=5.8, trend_efficiency=0.1)
    trend = character_score_from(dev_pct=-1.7, atr_pct=5.8, trend_efficiency=0.9)
    assert swing is not None and trend is not None
    assert swing > trend


def test_character_score_sign_follows_deviation() -> None:
    below = character_score_from(dev_pct=-1.0, atr_pct=4.0, trend_efficiency=0.3)
    above = character_score_from(dev_pct=1.0, atr_pct=4.0, trend_efficiency=0.3)
    assert below is not None and above is not None
    assert below > 0 > above


def test_character_score_requires_atr_and_deviation() -> None:
    assert character_score_from(dev_pct=None, atr_pct=4.0,
                                trend_efficiency=0.3) is None
    assert character_score_from(dev_pct=-1.0, atr_pct=0.0,
                                trend_efficiency=0.3) is None


def test_score_character_reports_regime_and_multiple() -> None:
    ctx = FactorContext(
        price=100.0, dev_pct=-1.7, character_available=True, character_atr_pct=5.8,
        character_trend_efficiency=0.3, character_t_friendly=68,
        character_grade="活跃", character_regime="swing")
    outcome = run_factor("character", ctx, FactorParams(
        character=CharacterParams(atr_scale=0.35)))
    assert outcome.available is True
    assert outcome.score > 0
    assert "震荡型" in outcome.detail and "做T友好度 68" in outcome.detail
    assert "倍日常波动" in outcome.detail


# ==================== 涨跌停幅度口径 ====================

@pytest.mark.parametrize(
    ("code", "expected"),
    [("300308", 20.0), ("301001", 20.0), ("688981", 20.0),
     ("600036", 10.0), ("000001", 10.0), ("830799", 30.0)],
)
def test_limit_up_pct_by_board(code: str, expected: float) -> None:
    assert limit_up_pct_for(code) == pytest.approx(expected)


def test_chan_params_defaults_are_sane() -> None:
    params = ChanParams()
    assert params.min_bars >= 10 and params.min_gap >= 3
    assert 0 < params.area_ratio_max <= 1.0
    assert 0 <= params.divergence_weight <= 1


def test_chip_params_reject_inverted_positions() -> None:
    """高点判定线必须严格高于低点判定线（相等会让"中部"区间退化）。"""
    from src.core.exceptions import ConfigError

    with pytest.raises(ConfigError, match="low_position"):
        ChipParams(low_position=0.5, high_position=0.5)
