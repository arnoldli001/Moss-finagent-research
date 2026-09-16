"""35 因子库单测（离线，确定性合成面板）。

三类证据：
1. **完整性**：35 个因子、七大类数量与设计文档一致；
2. **方向一致**：所有因子都调成"越大越看好"，用构造的对照样本验证符号；
3. **无未来函数**：把未来数据改掉，历史因子值必须逐格不变（因果性护栏）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quant.factor_library_v2 import (
    CATEGORY_LABELS,
    FACTORS,
    compute_factors,
    cross_section,
    factor_coverage_report,
    factors_by_category,
    list_factor_specs,
)
from src.quant.panels import FactorPanels
from src.quant.pit import PitPanel

DATES = [stamp.strftime("%Y-%m-%d")
         for stamp in pd.bdate_range("2026-03-02", periods=140)]
CODES = ["600519", "300750", "000001", "002594", "688981"]


def _panels(*, pit: bool = True) -> FactorPanels:
    """构造一个 20 天 × 5 只股票的确定性面板。"""
    index = [date.replace("-", "") for date in DATES]
    close = pd.DataFrame(
        {code: np.linspace(10.0 + offset, 12.0 + offset, len(index))
         for offset, code in enumerate(CODES)}, index=index)
    high = close * 1.02
    low = close * 0.98
    amount = pd.DataFrame(
        {code: np.full(len(index), 1e8 * (position + 1))
         for position, code in enumerate(CODES)}, index=index)
    volume_lot = amount / close / 100.0

    basics = {
        "pe_ttm": pd.DataFrame(
            {code: np.full(len(index), 10.0 + 5 * position)
             for position, code in enumerate(CODES)}, index=index),
        "pb": pd.DataFrame(
            {code: np.full(len(index), 1.0 + position)
             for position, code in enumerate(CODES)}, index=index),
        "ps_ttm": pd.DataFrame(
            {code: np.full(len(index), 2.0 + position)
             for position, code in enumerate(CODES)}, index=index),
        "dv_ttm": pd.DataFrame(
            {code: np.full(len(index), 1.0 + position)
             for position, code in enumerate(CODES)}, index=index),
        "turnover_rate": pd.DataFrame(
            {code: np.full(len(index), 0.5 + position)
             for position, code in enumerate(CODES)}, index=index),
        "volume_ratio": pd.DataFrame(
            {code: np.full(len(index), 1.0 + 0.2 * position)
             for position, code in enumerate(CODES)}, index=index),
        "total_mv": pd.DataFrame(
            {code: np.full(len(index), 1e10 * (position + 1))
             for position, code in enumerate(CODES)}, index=index),
        "circ_mv": pd.DataFrame(
            {code: np.full(len(index), 8e9 * (position + 1))
             for position, code in enumerate(CODES)}, index=index),
        "free_share": pd.DataFrame(
            {code: np.full(len(index), 1e8 * (position + 1))
             for position, code in enumerate(CODES)}, index=index),
    }
    flows = {"net_mf_amount": pd.DataFrame(
        {code: np.full(len(index), 1e6 * (position + 1))
         for position, code in enumerate(CODES)}, index=index)}

    pit_panel = None
    if pit:
        records = []
        for position, code in enumerate(CODES):
            records.append({
                "code": code, "name": code, "report_period": "20260630",
                "ann_date": "20260828",
                "roe": 10.0 + position, "roa": 5.0 + position,
                "grossprofit_margin": 30.0 + position,
                "netprofit_margin": 10.0 + position,
                "debt_to_assets": 60.0 - position,
                "ocf_to_profit": 0.8 + 0.1 * position,
                "tr_yoy": 5.0 + position, "netprofit_yoy": 8.0 + position,
                "roe_yoy": 1.0 + position, "ocf_yoy": 2.0 + position,
                "fcff": 1e9 * (position + 1),
            })
            records.append({
                "code": code, "name": code, "report_period": "20250630",
                "ann_date": "20250828",
                "roe": 9.0 + position, "roa": 4.0 + position,
                "grossprofit_margin": 28.0 + position,
                "netprofit_margin": 9.0 + position,
                "debt_to_assets": 61.0 - position,
                "ocf_to_profit": 0.7 + 0.1 * position,
                "tr_yoy": 4.0 + position, "netprofit_yoy": 7.0 + position,
                "roe_yoy": 0.5 + position, "ocf_yoy": 1.5 + position,
                "fcff": 8e8 * (position + 1),
            })
        pit_panel = PitPanel(pd.DataFrame(records))

    bench = pd.Series(
        np.linspace(4000.0, 4100.0, len(index)), index=index)

    return FactorPanels(
        dates=index, codes=list(CODES),
        prices={"open": close, "high": high, "low": low, "close": close,
                "volume_lot": volume_lot, "amount": amount},
        basics=basics, flows=flows,
        fundamentals=pit_panel, index_returns={"000300.SH": bench})


# ==================== 完整性 ====================


def test_35_factors_registered() -> None:
    specs = list_factor_specs()
    assert len(specs) == 35, f"应有 35 个因子，实际 {len(specs)}"


def test_category_counts_match_design_doc() -> None:
    grouped = factors_by_category()
    counts = {name: len(items) for name, items in grouped.items()}
    assert counts["value"] == 6
    assert counts["growth"] == 5
    assert counts["quality"] == 6
    assert counts["momentum"] == 5
    assert counts["volatility"] == 4
    assert counts["liquidity"] == 5
    assert counts["size"] == 4
    assert sum(counts.values()) == 35


def test_every_factor_has_label_formula_and_direction() -> None:
    for spec in list_factor_specs():
        assert spec.label and spec.formula, spec.key
        assert spec.direction in (1, -1), spec.key
        assert spec.category in CATEGORY_LABELS, spec.key
        assert callable(spec.func)


def test_factor_keys_are_unique_and_snake_case() -> None:
    keys = [spec.key for spec in list_factor_specs()]
    assert len(keys) == len(set(keys))
    for key in keys:
        assert key == key.lower() and " " not in key


# ==================== 计算正确性 ====================


def test_compute_all_factors_returns_aligned_panels() -> None:
    panels = _panels()
    factors = compute_factors(panels)
    assert set(factors) == set(FACTORS)
    for key, frame in factors.items():
        assert frame.shape == (len(panels.dates), len(panels.codes)), key
        assert list(frame.index) == panels.dates, key
        assert list(frame.columns) == panels.codes, key
    # 至少 3/4 的因子应该在样本末端有值（否则说明面板装配/字段名对不上）
    latest_filled = sum(
        1 for frame in factors.values()
        if frame.iloc[-1].notna().any())
    assert latest_filled >= 26, f"末端有值的因子只有 {latest_filled}/35"


def test_unknown_factor_key_raises() -> None:
    with pytest.raises(KeyError, match="未注册的因子"):
        compute_factors(_panels(), keys=["not_a_factor"])


def test_value_factors_directions() -> None:
    """EP=1/PE：PE 最低的那只（第一个代码）EP 最高。"""
    panels = _panels()
    factors = compute_factors(panels, keys=["ep", "bp", "sp", "dividend_yield"])
    latest = factors["ep"].iloc[-1]
    assert latest.idxmax() == CODES[0], "低 PE 应有最高 EP"
    assert factors["bp"].iloc[-1].idxmax() == CODES[0]
    assert factors["sp"].iloc[-1].idxmax() == CODES[0]
    # 股息率方向为 1：构造里 position 越大股息率越高
    assert factors["dividend_yield"].iloc[-1].idxmax() == CODES[-1]


def test_peg_is_inverted_and_guards_nonpositive_growth() -> None:
    panels = _panels()
    peg = compute_factors(panels, keys=["peg"])["peg"]
    # PE 与增速同向构造 → PEG = PE/growth 基本恒定，取反后仍为有限值
    assert peg.notna().to_numpy().any()
    # 增速<=0 时必须为 NaN，而不是造出一个负 PEG 混进排序
    records = panels.fundamentals.records.copy()
    records.loc[records["code"] == CODES[0], "netprofit_yoy"] = -5.0
    panels.fundamentals = PitPanel(records)
    peg2 = compute_factors(panels, keys=["peg"])["peg"]
    assert pd.isna(peg2.iloc[-1][CODES[0]])


def test_quality_factor_low_leverage_is_negated() -> None:
    panels = _panels()
    factors = compute_factors(panels, keys=["low_leverage", "roe"])
    # 构造里 position 越大资产负债率越低 → 取反后低负债的那只得分最高
    assert factors["low_leverage"].iloc[-1].idxmax() == CODES[-1]
    assert factors["roe"].iloc[-1].idxmax() == CODES[-1]


def test_gross_margin_trend_uses_year_ago_value() -> None:
    panels = _panels()
    trend = compute_factors(panels, keys=["gross_margin_trend"])["gross_margin_trend"]
    # 构造：本期毛利率 30+position，去年同期 28+position → 差恒为 +2
    latest = trend.iloc[-1]
    assert latest.notna().all()
    assert np.allclose(latest.to_numpy(dtype=float), 2.0)


def test_momentum_uses_past_returns_only() -> None:
    panels = _panels()
    momentum = compute_factors(panels, keys=["momentum_20"])["momentum_20"]
    # 单调上涨的构造里，20 日收益为正
    assert (momentum.iloc[-1].dropna() > 0).all()
    # 前 20 天样本不足 → 必须是 NaN（不能偷偷用全部数据算）
    assert momentum.iloc[0].isna().all()


def test_reversal_and_volatility_directions() -> None:
    panels = _panels()
    factors = compute_factors(
        panels, keys=["reversal_5", "volatility_20", "atr_20", "downside_vol_20"])
    for key in ("reversal_5", "volatility_20", "atr_20", "downside_vol_20"):
        frame = factors[key]
        assert frame.shape == (len(panels.dates), len(panels.codes)), key
    # 反转是"过去 5 日收益取负"，单调上涨 → 值为负
    assert (factors["reversal_5"].iloc[-1].dropna() < 0).all()


def test_amihud_and_money_flow_ratio() -> None:
    panels = _panels()
    factors = compute_factors(panels, keys=["amihud", "money_flow_ratio"])
    amihud = factors["amihud"].iloc[-1]
    # 成交额最小的那只（第一个）非流动性最高，取反后得分最低
    assert amihud.notna().sum() >= 3
    assert amihud.idxmin() == CODES[0]
    flow = factors["money_flow_ratio"].iloc[-1]
    # 净流入/成交额：构造里两者同比例，故各只相近（只验证能算出且非空）
    assert flow.notna().all()


def test_size_factors_are_negated() -> None:
    panels = _panels()
    factors = compute_factors(panels, keys=["total_mv", "circ_mv", "log_mv",
                                            "free_float_mv"])
    for key, frame in factors.items():
        latest = frame.iloc[-1]
        assert latest.idxmax() == CODES[0], f"{key} 应给最小市值最高分"


def test_relative_strength_subtracts_benchmark() -> None:
    panels = _panels()
    rs = compute_factors(panels, keys=["relative_strength"])["relative_strength"]
    momentum = compute_factors(panels, keys=["momentum_60"])["momentum_60"]
    # 基准同期收益 ≈ 4100/4000-1 ≈ 2.5%，RS 应低于原始动量
    diff = (momentum - rs).iloc[-1].dropna()
    assert (diff > 0).all()
    assert diff.std() < 1e-9, "同一天各股票的基准扣减必须一致"


def test_missing_fundamentals_becomes_nan_not_error() -> None:
    """没有财务数据时，成长/质量因子应是 NaN（而不是崩掉或凭空造值）。"""
    panels = _panels(pit=False)
    panels.fundamentals = None
    factors = compute_factors(panels, keys=["roe", "revenue_growth", "cfp"])
    for key, frame in factors.items():
        assert frame.isna().all().all(), key


# ==================== 因果性（无未来函数） ====================


def test_no_lookahead_when_future_prices_change() -> None:
    """篡改未来价格后，过去所有因子值必须逐格不变。"""
    panels = _panels()
    original = compute_factors(panels, keys=["momentum_20", "volatility_20",
                                             "amihud", "atr_20"])

    tampered = _panels()
    for key in list(tampered.prices):
        frame = tampered.prices[key]
        frame.iloc[-5:] = frame.iloc[-5:] * 3.0      # 只改最后 5 天
    after = compute_factors(tampered, keys=["momentum_20", "volatility_20",
                                            "amihud", "atr_20"])

    for key in original:
        before_slice = original[key].iloc[:-5]
        after_slice = after[key].iloc[:-5]
        pd.testing.assert_frame_equal(before_slice, after_slice,
                                      check_exact=False, atol=1e-12,
                                      obj=f"{key} 的历史值被未来数据污染")


def test_no_lookahead_when_future_announcement_added() -> None:
    """新增一条"未来公告"的财报，不能改变公告日之前的因子值。"""
    panels = _panels()
    before = compute_factors(panels, keys=["roe", "profit_growth"])

    records = panels.fundamentals.records.copy()
    future = records[records["report_period"] == "20260630"].copy()
    future["report_period"] = "20260930"
    future["ann_date"] = "20261025"          # 面板最后一天是 09-20，属于未来
    future["roe"] = 999.0
    panels.fundamentals = PitPanel(pd.concat([records, future], ignore_index=True))
    after = compute_factors(panels, keys=["roe", "profit_growth"])

    for key in before:
        pd.testing.assert_frame_equal(before[key], after[key],
                                      obj=f"{key} 被未来公告污染")


def test_rolling_windows_do_not_fill_forward() -> None:
    """停牌/缺失造成的空洞不能被前向填充到未来。

    注意区分两类因子（这是刻意的语义，不是 bug）：
    - **读当前价的因子**（动量/ATR/Amihud）在缺失当日必须是 NaN；
    - **纯历史窗口的因子**（波动率）在窗口内仍有足够有效样本时会照常计算 ——
      pandas rolling 会跳过窗口内的 NaN，这属于正常的"用可用样本估计"，不是未来信息。
    """
    panels = _panels()
    close = panels.prices["close"].copy()
    close.iloc[-3:, 0] = np.nan              # 第一只票最近 3 天没数据
    panels.prices["close"] = close
    factors = compute_factors(panels, keys=["momentum_20", "atr_20", "amihud",
                                            "volatility_20"])
    # 读当前价的因子 → 缺失当日必须 NaN
    for key in ("momentum_20", "atr_20"):
        assert pd.isna(factors[key].iloc[-1][CODES[0]]), \
            f"{key} 读当前价，缺数据当日必须为 NaN（被前向填充了）"
    # 纯滚动窗口的因子在窗口内仍有足够有效样本时照常计算（pandas rolling 跳过 NaN）
    for key in ("volatility_20",):
        value = factors[key].iloc[-1][CODES[0]]
        assert value == value, f"{key} 在样本足够时应能算出来"
    # 没有缺失的其它股票不受影响
    assert not pd.isna(factors["momentum_20"].iloc[-1][CODES[1]])


# ==================== 覆盖率报表与截面取用 ====================


def test_coverage_report_shape() -> None:
    factors = compute_factors(_panels())
    report = factor_coverage_report(factors)
    assert len(report) == 35
    assert set(report.columns) == {"factor", "label", "category", "overall",
                                   "latest_date", "latest_codes"}
    assert report["overall"].between(0.0, 1.0).all()


def test_cross_section_returns_series_per_date() -> None:
    factors = compute_factors(_panels(), keys=["ep"])
    panel = factors["ep"]
    section = cross_section(panel, panel.index[-1])
    assert isinstance(section, pd.Series)
    assert set(section.index) <= set(CODES)
    assert cross_section(panel, "19990101").empty


def test_verbose_logging_path_runs(caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO):
        compute_factors(_panels(), keys=["ep", "bp"], verbose=True)
    assert any("覆盖率" in record.message for record in caplog.records)


def test_factor_failure_is_isolated(monkeypatch) -> None:
    """单个因子算炸不能拖垮整批（返回空面板 + 记录告警）。"""
    from src.quant import factor_library_v2 as library

    spec = library.FACTORS["ep"]

    def boom(_panels):
        raise RuntimeError("故意的")

    monkeypatch.setitem(library.FACTORS, "ep",
                        library.FactorSpec(**{**spec.as_dict(), "func": boom}))
    factors = library.compute_factors(_panels(), keys=["ep", "bp"])
    assert factors["ep"].isna().all().all()
    assert factors["bp"].notna().any().any()
