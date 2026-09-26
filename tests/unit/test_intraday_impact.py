"""做T模块 · 「参数 → 价格线 / 触发门槛 / 因子影响度」解释层单元测试。

本文件要钉住的是**解释口径**，不是判定逻辑（判定在 test_intraday_engine.py）：
  1. 档位线的来源说出来必须与 `compute_levels` 的实际取值**对得上**
     （标记布林下轨，值就不能是箱体下沿 —— 用户拿这两个数一比就会怀疑面板乱算）；
  2. 触发价必须与 `decide_signal` 的贴线判定同口径
     （回踩触发价 = 回踩线×(1+贴线带宽)，价格跌到它就"触及"）；
  3. 「还差多少分/多少价」必须能直接读出结论，且止损/情绪周期否决要如实标出；
  4. 因子影响度就是得分（权重每 +1 分对总分的推动），关掉一项 = 减去它的贡献分。
"""

from __future__ import annotations

import pytest

from src.intraday.config import IntradayConfig
from src.intraday.engine import compose_scorecard, compute_levels
from src.intraday.factors import FactorContext
from src.intraday.impact import (
    annotate_level_basis,
    build_trigger_impact,
    cycle_facts_from_scorecard,
    factor_impacts,
    level_deltas,
)
from src.intraday.models import FactorScore, LevelSet, ScoreCard

CODE_PRICE = 10.0


def _levels(config: IntradayConfig | None = None, **kwargs) -> object:
    params = {
        "price": CODE_PRICE, "box_high": 10.3, "box_low": 9.7,
        "box_span_days": 20,
    }
    params.update(kwargs)
    return compute_levels(config=config or IntradayConfig(), **params)


def _card(total: float, factors: list[FactorScore] | None = None,
          available_weight: float = 100.0) -> ScoreCard:
    return ScoreCard(
        total=total, threshold_action=30.0, threshold_hint=20.0, zone="neutral",
        verdict="", factors=factors or [], weights_sum=100.0,
        available_weight=available_weight)


def _factor(key: str, score: float, weight: float, *, available: bool = True,
            inputs: dict | None = None) -> FactorScore:
    return FactorScore(
        key=key, label=key, weight=weight, score=score,
        contribution=round(score * weight, 2), detail="", inputs=inputs or {},
        available=available, gap=None if available else "缺数据")


# ==================== 档位来源 ====================


def test_level_source_matches_box_when_boll_missing() -> None:
    """没有布林数据时，回踩/冲高的来源就是箱体上下沿。"""
    config = IntradayConfig()
    annotated = annotate_level_basis(_levels(config=config), config)
    assert "箱体下沿 9.70" in annotated.low_source
    assert "箱体上沿 10.30" in annotated.high_source
    assert annotated.low_buy == pytest.approx(9.7)
    assert annotated.high_sell == pytest.approx(10.3)


def test_level_source_points_at_boll_band_actually_used() -> None:
    """布林下轨确实在现价下方时取较高者 —— 来源必须指向**真正被采纳**的那条轨。

    箱体取 9.0/11.0（更宽），布林 9.4/10.6 才会被采纳；若箱体下沿本来就高于
    布林下轨（例如 9.7 > 9.4），max 取到的仍是箱体值 —— 那时来源必须是箱体。
    """
    config = IntradayConfig()
    config.levels.max_band_pct = 40.0     # 放宽护栏，先看纯融合口径
    config.levels.min_band_pct = 0.0
    annotated = annotate_level_basis(
        _levels(config=config, box_low=9.0, box_high=11.0,
                boll_upper=10.6, boll_lower=9.4), config)
    assert annotated.low_buy == pytest.approx(9.4)
    assert annotated.high_sell == pytest.approx(10.6)
    assert annotated.low_source.startswith("布林下轨 9.40")
    assert annotated.high_source.startswith("布林上轨 10.60")
    assert annotated.band_clamped == ""

    # 箱体下沿高于布林下轨时：max 取到箱体 → 来源必须说箱体
    boxed = annotate_level_basis(_levels(config=config), config)
    assert boxed.low_buy == pytest.approx(9.7)
    assert boxed.low_source.startswith("箱体下沿 9.70")


def test_level_source_flags_band_clamp_so_value_and_label_agree() -> None:
    """档位差护栏动过线时必须明说，并给出护栏前的候选位置。

    实测场景（300308 2026-09-17）：候选 902.35~908.52 的间距恰好等于
    min_band_pct=1.5%，夹与不夹宽度一样，但两条线都被移动过 ——
    只看宽度会漏判，于是面板上"标记是布林轨、数值却是箱体值"。
    """
    config = IntradayConfig()
    config.levels.blend_boll_bands = True
    annotated = annotate_level_basis(
        _levels(price=905.0, box_high=949.73, box_low=804.02, config=config,
                boll_upper=908.52, boll_lower=902.35, boll_mid=905.44), config)
    assert annotated.low_buy != pytest.approx(902.35)
    assert annotated.pre_clamp_low == pytest.approx(902.35)
    assert annotated.pre_clamp_high == pytest.approx(908.52)
    assert "被最小档位差" in annotated.band_clamped
    assert "902.35" in annotated.band_clamped and "908.52" in annotated.band_clamped
    assert annotated.band_width_pct == pytest.approx(1.5, abs=0.01)


def test_trigger_prices_match_touch_band() -> None:
    """触发价 = 回踩线×(1+贴线带宽) / 冲高线×(1−贴线带宽)（与 decide_signal 同口径）。"""
    config = IntradayConfig()
    config.levels.touch_band_pct = 0.5
    annotated = annotate_level_basis(_levels(config=config), config)
    assert annotated.low_trigger_price == pytest.approx(
        annotated.low_buy * 1.005, rel=1e-6)
    assert annotated.high_trigger_price == pytest.approx(
        annotated.high_sell * 0.995, rel=1e-6)


# ==================== 触发门槛 ====================


def test_gate_rows_tell_how_much_is_missing() -> None:
    config = IntradayConfig()
    levels = _levels()
    impact = build_trigger_impact(
        scorecard=_card(total=12.0), levels=levels, config=config)
    low = next(row for row in impact.gates if row.key == "low_buy")
    high = next(row for row in impact.gates if row.key == "high_sell")
    assert low.ready is False
    assert low.score_need == pytest.approx(8.0)     # 提示线 20 - 12
    assert "还差 8.0 分" in low.note
    assert high.score_need == pytest.approx(32.0)   # 12 + 20
    assert "还差 32.0 分" in high.note
    # 回踩触发价在现价下方 → 价格需**下跌**才能触及
    assert low.price_need_pct is not None and low.price_need_pct < 0
    assert high.price_need_pct is not None and high.price_need_pct > 0


def test_gate_ready_at_hint_line_and_solid_at_action_line() -> None:
    config = IntradayConfig()
    levels = _levels()
    hollow = build_trigger_impact(
        scorecard=_card(total=21.0), levels=levels, config=config)
    low = next(row for row in hollow.gates if row.key == "low_buy")
    assert low.ready is True
    assert "已达提示线" in low.note
    assert "还差" in low.note          # 离动手线仍差 9 分

    solid = build_trigger_impact(
        scorecard=_card(total=31.0), levels=levels, config=config)
    low = next(row for row in solid.gates if row.key == "low_buy")
    assert "可出实心正式信号" in low.note


def test_gate_reports_coverage_downgrade_and_cycle_veto() -> None:
    """有效权重<70 降级、情绪周期否决 —— 两件事都必须在门槛行里说清楚。"""
    config = IntradayConfig()
    card = _card(total=40.0, available_weight=47.0, factors=[
        _factor("cycle", -0.9, 6.0, inputs={
            "stage": "退潮期", "temperature": 9.0, "t_allowed": False,
            "gates": ["大面 12 家 ≥ 10（一票否决）"]}),
    ])
    impact = build_trigger_impact(scorecard=card, levels=_levels(), config=config)
    low = next(row for row in impact.gates if row.key == "low_buy")
    # 总分 40 已越过动手线，但覆盖度<70 → 只能出空心；周期否决 → 回踩不可动手
    assert impact.coverage_blocked is True
    assert impact.cycle_blocked is True
    assert impact.cycle_stage == "退潮期"
    assert low.ready is False
    assert "有效权重<70" in low.blocked_by
    assert "退潮期" in low.blocked_by
    assert any("一票否决" in note for note in impact.notes) or any(
        "禁止正式回踩" in note for note in impact.notes)


def test_cycle_facts_are_read_back_from_scorecard() -> None:
    """周期口径从打分卡回读：与卡片上的分数必然同源，不会自相矛盾。"""
    card = _card(total=5.0, factors=[
        _factor("cycle", 0.76, 6.0, inputs={
            "stage": "主升期", "temperature": 88, "t_allowed": True, "gates": []}),
    ])
    facts = cycle_facts_from_scorecard(card)
    assert facts is not None
    assert facts["stage"] == "主升期"
    assert facts["t_allowed"] is True
    assert cycle_facts_from_scorecard(_card(total=0.0)) is None


def test_impact_unavailable_without_scorecard_or_levels() -> None:
    config = IntradayConfig()
    verdict = build_trigger_impact(
        scorecard=None, levels=_levels(), config=config)
    assert verdict.available is False
    assert "无法推导" in verdict.reason
    verdict = build_trigger_impact(
        scorecard=_card(total=1.0), levels=None, config=config)
    assert verdict.available is False


def test_stop_loss_gate_flags_breakdown() -> None:
    """跌破止损位 → ready=True 且明确写「禁止任何回踩信号」。

    直接构造「现价在止损位下方」的档位：`compute_levels` 只保证止损低于回踩线，
    而回踩线本身可以落在现价下方（早盘杀跌到回踩线以下的真实情形）。
    """
    config = IntradayConfig()
    levels = LevelSet(
        price=9.0, box_high=10.3, box_low=9.7, box_position=0.0, box_span_days=20,
        low_buy=9.3, high_sell=10.3, stop_loss=9.2, stop_loss_pct=1.0)
    impact = build_trigger_impact(
        scorecard=_card(total=60.0), levels=levels, config=config)
    stop = next(row for row in impact.gates if row.key == "stop_loss")
    assert stop.ready is True
    assert "现价已跌破" in stop.note
    assert "禁止任何回踩信号" in stop.note


# ==================== 因子影响度 ====================


def test_factor_impact_unit_equals_score_and_zero_equals_contribution() -> None:
    card = _card(total=10.0, factors=[
        _factor("boll", -0.46, 8.0),
        _factor("cycle", 0.76, 6.0),
    ])
    rows = {row.key: row for row in factor_impacts(card, top=None)}
    assert rows["boll"].unit_impact == pytest.approx(-0.46)
    assert rows["boll"].zero_impact == pytest.approx(3.68)
    assert "拉低" in rows["boll"].note
    assert rows["cycle"].unit_impact == pytest.approx(0.76)
    assert rows["cycle"].zero_impact == pytest.approx(-4.56)
    assert "拉高" in rows["cycle"].note
    # 按 |单位影响度| 降序：情绪周期(0.76) 排在布林(-0.46) 前面
    assert [row.key for row in factor_impacts(card, top=None)] == ["cycle", "boll"]


def test_factor_impact_of_unavailable_factor_is_zeroed() -> None:
    card = _card(total=0.0, available_weight=40.0, factors=[
        _factor("overseas", 0.5, 10.0, available=False),
    ])
    row = factor_impacts(card, top=None)[0]
    assert row.unit_impact == 0.0
    assert row.zero_impact == 0.0
    assert row.weight_share_pct == 0.0
    assert "不可用" in row.note


def test_factor_impact_share_is_of_effective_weight() -> None:
    """权重占比的分母是**有效权重**（不可用维度的权重已被扣除），不是名义 100。"""
    card = _card(total=0.0, available_weight=50.0, factors=[
        _factor("box", 0.5, 10.0),
    ])
    row = factor_impacts(card, top=None)[0]
    assert row.weight_share_pct == pytest.approx(20.0)


def test_top_factors_truncates_but_top_none_keeps_all() -> None:
    card = _card(total=0.0, factors=[
        _factor(f"f{index}", 0.5 - index * 0.1, 5.0) for index in range(10)])
    assert len(factor_impacts(card, top=4)) == 4
    assert len(factor_impacts(card, top=None)) == 10


# ==================== 端到端：真实打分卡 ====================


def test_examples_say_weights_do_not_move_level_lines() -> None:
    """面板上必须写明「调权重不会移动档位线」——这是用户最常问的误解。"""
    config = IntradayConfig()
    impact = build_trigger_impact(
        scorecard=_card(total=0.0), levels=_levels(), config=config)
    joined = " ".join(impact.examples)
    assert "与因子权重无关" in joined
    assert len(impact.level_rows) >= 3
    keys = {row.key for row in impact.level_rows}
    assert {"low_buy", "high_sell", "stop_loss"} <= keys


# ==================== 改动前 → 改动后 ====================


def test_level_deltas_compare_baseline_and_preview_params() -> None:
    """「改动前 → 改动后」：差值必须由服务端算，方向与幅度都要对。

    档位值本身由 engine 计算，所以这里直接给两份档位对象（这正是路由里的形态：
    基准来自该票当前口径的轻量快照，预览来自预览口径的完整快照）。
    """
    baseline = IntradayConfig()
    before = annotate_level_basis(_levels(config=baseline), baseline)
    # 模拟"止损参数放宽 + 冲高线上移"之后的两条线
    after = before.model_copy(update={
        "low_buy": round(before.low_buy - 1.0, 4),
        "high_sell": round(before.high_sell + 2.0, 4),
        "stop_loss": round(before.stop_loss - 3.0, 4),
    })
    rows = {row.key: row for row in level_deltas(before, after)}
    assert rows["low_buy"].delta == pytest.approx(-1.0)
    assert rows["high_sell"].delta == pytest.approx(2.0)
    assert rows["stop_loss"].delta == pytest.approx(-3.0)
    assert rows["low_buy"].current == pytest.approx(before.low_buy)
    assert rows["low_buy"].preview == pytest.approx(after.low_buy)
    # 百分比按"改动前"为分母
    assert rows["high_sell"].delta_pct == pytest.approx(
        2.0 / before.high_sell * 100.0, rel=1e-4)


def test_level_deltas_without_baseline_are_just_preview() -> None:
    """没有基准口径时不许编造「改动前」—— 行还在，但基准与差值必须为空。"""
    config = IntradayConfig()
    raw = _levels(config=config)
    rows = level_deltas(None, annotate_level_basis(raw, config))
    assert {row.key for row in rows} >= {"low_buy", "high_sell", "stop_loss"}
    for row in rows:
        assert row.current is None
        assert row.delta is None
        assert row.delta_pct is None


def test_build_trigger_impact_emits_deltas_only_with_baseline() -> None:
    config = IntradayConfig()
    levels = annotate_level_basis(_levels(config=config), config)
    without = build_trigger_impact(
        scorecard=_card(total=5.0), levels=levels, config=config)
    assert without.level_deltas, "对照行始终产出（保证表格结构稳定）"
    assert all(row.delta is None for row in without.level_deltas), \
        "没有基准口径时不允许编造差值"
    with_base = build_trigger_impact(
        scorecard=_card(total=5.0), levels=levels, config=config,
        current_levels=levels)
    assert all(row.delta == 0.0 for row in with_base.level_deltas), \
        "基准与预览完全相同时差值必须恰好为 0"


def test_impact_on_real_scorecard_matches_compose_scorecard() -> None:
    """用真实打分核跑一遍：解释层读出的总分/贡献必须与打分卡逐行一致。"""
    config = IntradayConfig()
    ctx = FactorContext(price=10.0, vwap=9.9, dev_pct=-0.5, dev_z=-0.8,
                        box_high=10.6, box_low=9.4, box_position=0.3,
                        box_span_days=20)
    card = compose_scorecard(ctx, config)
    impact = build_trigger_impact(
        scorecard=card, levels=_levels(config=config), config=config)
    assert impact.total == pytest.approx(card.total)
    contributions = {row.key: row.contribution for row in impact.factor_impact}
    for factor in card.factors:
        if factor.key in contributions:
            assert contributions[factor.key] == pytest.approx(
                factor.contribution, abs=0.01)
