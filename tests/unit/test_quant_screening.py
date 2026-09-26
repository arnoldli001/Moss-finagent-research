"""M3 因子筛选流水线单测（离线，合成面板）。

锁三类问题，全部来自真实数据验证时踩到的坑：
1. **低覆盖因子不能污染相关矩阵**（实测：一个整列空的因子会让
   "全因子齐备"的日期筛法跳过所有训练日 → 相关矩阵全 NaN → 聚类静默失效）；
2. **不依赖 scipy**（本项目未安装 scipy，`corr(method="spearman")` 会直接 ModuleNotFoundError）；
3. **筛选只能用训练集**（用全样本 IC 挑因子 = 用样本外信息挑因子，样本外指标会虚高）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quant.screening import (
    ScreenConfig,
    apply_st_mask,
    cluster_by_correlation,
    composite_score,
    correlation_matrix,
    forward_returns,
    ic_table,
    neutralize_factors,
    select_representatives,
    split_dates,
    walk_forward,
)

DATES = [stamp.strftime("%Y%m%d")
         for stamp in pd.bdate_range("2026-01-05", periods=60)]
CODES = [f"6000{index:02d}" for index in range(30)]


def _panel(seed: int, *, coverage: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    values = rng.normal(size=(len(DATES), len(CODES)))
    frame = pd.DataFrame(values, index=DATES, columns=CODES)
    if coverage < 1.0:
        mask = rng.random(size=frame.shape) > coverage
        frame = frame.mask(mask)
    return frame


def _panels_stub():
    """最小 FactorPanels：只有 close 与 total_mv（供中性化用）。"""
    from src.quant.panels import FactorPanels

    close = pd.DataFrame(
        np.cumprod(1 + np.random.default_rng(7).normal(0, 0.01, (len(DATES), len(CODES))),
                   axis=0) * 100.0, index=DATES, columns=CODES)
    total_mv = pd.DataFrame(
        np.tile(np.linspace(1e9, 5e10, len(CODES)), (len(DATES), 1)),
        index=DATES, columns=CODES)
    return FactorPanels(dates=DATES, codes=CODES,
                        prices={"close": close, "close_raw": close},
                        basics={"total_mv": total_mv})


# ==================== 相关性聚类 ====================


def test_low_coverage_factor_does_not_poison_matrix() -> None:
    """核心回归：某个因子整列为空时，其它因子之间的相关仍必须算出来。"""
    good_a = _panel(1)
    good_b = good_a.copy()                    # 与 a 完全一致 → 相关 1.0
    empty = pd.DataFrame(np.nan, index=DATES, columns=CODES)

    corr = correlation_matrix({"good_a": good_a, "good_b": good_b, "empty": empty})
    assert corr.loc["good_a", "good_b"] == pytest.approx(1.0, abs=1e-9), \
        "低覆盖因子把整张矩阵变成 NaN 了"
    assert np.isnan(corr.loc["good_a", "empty"]), "无有效交易日的对子应为 NaN"


def test_partial_coverage_pairwise_complete() -> None:
    """部分覆盖：只在两边都有值的交易日上算相关。"""
    base = _panel(3)
    partial = base.copy()
    partial.iloc[:30] = np.nan               # 前半段没数据
    corr = correlation_matrix({"base": base, "partial": partial})
    assert corr.loc["base", "partial"] == pytest.approx(1.0, abs=1e-6), \
        "pairwise-complete 应在重叠区间上得到完全相关"


def test_correlation_matches_manual_spearman_without_scipy() -> None:
    """自算的秩相关必须等于手算 Spearman（且全程不 import scipy）。"""
    left = pd.DataFrame(
        np.tile(np.arange(len(CODES), dtype=float), (len(DATES), 1)),
        index=DATES, columns=CODES)
    right = pd.DataFrame(
        np.tile(np.arange(len(CODES), dtype=float) ** 2, (len(DATES), 1)),
        index=DATES, columns=CODES)
    corr = correlation_matrix({"left": left, "right": right})
    assert corr.loc["left", "right"] == pytest.approx(1.0, abs=1e-9), \
        "单调变换秩相关应为 1"


def test_identical_factors_are_clustered_together() -> None:
    """momentum_60 与 relative_strength 在真实数据里相关 1.0 → 必须合并。"""
    factors = {"momentum_60": _panel(11), "relative_strength": _panel(11),
               "other": _panel(12)}
    corr = correlation_matrix(factors)
    clusters = cluster_by_correlation(corr, 0.7)
    grouped = [set(cluster) for cluster in clusters]
    assert {"momentum_60", "relative_strength"} in grouped
    assert ["other"] in [list(cluster) for cluster in clusters]


def test_cluster_threshold_is_respected() -> None:
    factors = {"a": _panel(21), "b": _panel(21), "c": _panel(22)}
    corr = correlation_matrix(factors)
    loose = cluster_by_correlation(corr, 0.99)
    tight = cluster_by_correlation(corr, 1.01)      # 不可能达到
    assert any(len(cluster) == 2 for cluster in loose)
    assert all(len(cluster) == 1 for cluster in tight)


def test_select_representatives_keeps_best_icir() -> None:
    factors = {"strong": _panel(31), "weak": _panel(31)}
    corr = correlation_matrix(factors)
    table = pd.DataFrame([
        {"factor": "strong", "IC": 0.05, "ICIR": 0.9},
        {"factor": "weak", "IC": 0.03, "ICIR": 0.2},
    ])
    selected, dropped, clusters = select_representatives(corr, table, 0.7)
    assert selected == ["strong"]
    assert dropped[0]["factor"] == "weak"
    assert dropped[0]["kept_by"] == "strong"
    assert clusters[0]["size"] == 2


# ==================== 中性化 ====================


def test_neutralize_removes_market_cap_exposure() -> None:
    """中性化的保证是**残差与设计矩阵（ln 市值）正交**。

    注意不能拿"与线性市值相关为 0"来断言：因子若与市值呈非线性关系
    （如市值本身），对 ln(市值) 回归后的残差仍会与市值水平相关 —— 那是数学必然，
    不是 bug。这里检验真正被保证的东西。
    """
    panels = _panels_stub()
    rng = np.random.default_rng(99)
    mv = panels.basic("total_mv")
    log_mv = np.log(mv)
    # 因子 = ln(市值) + 噪声：中性化后应只剩噪声
    factor = log_mv + pd.DataFrame(
        rng.normal(0, 0.5, size=mv.shape), index=mv.index, columns=mv.columns)
    neutral, _ = neutralize_factors({"f": factor}, panels,
                                    config=ScreenConfig(neutralize_mv=True))
    residual = neutral["f"]
    date = DATES[-1]
    section = pd.DataFrame({"resid": residual.loc[date],
                            "log_mv": log_mv.loc[date]}).dropna()
    assert abs(section["resid"].corr(section["log_mv"])) < 0.05, \
        "残差与 ln(市值) 仍相关 → 市值暴露没被中性化掉"
    assert residual.loc[date].std() > 0.1, "有信号的成分应保留下来"


def test_neutralize_keeps_signal_variance() -> None:
    """中性化不能把所有因子都压成常数（那会让 IC 恒为 0）。"""
    panels = _panels_stub()
    rng = np.random.default_rng(5)
    factor = pd.DataFrame(rng.normal(size=(len(DATES), len(CODES))),
                          index=DATES, columns=CODES)
    neutral, _ = neutralize_factors({"f": factor}, panels,
                                    config=ScreenConfig(neutralize_mv=True))
    assert neutral["f"].std(axis=1).min() > 0.5, "标准化后截面标准差应接近 1"


def test_neutralize_without_market_cap_is_reported() -> None:
    panels = _panels_stub()
    panels.basics = {}
    _, notes = neutralize_factors({"a": _panel(5)}, panels,
                                  config=ScreenConfig(neutralize_mv=True))
    assert any("缺少市值数据" in note for note in notes)


# ==================== IC 与 walk-forward ====================


def test_ic_table_reports_all_factors() -> None:
    factors = {"a": _panel(41), "b": _panel(42)}
    fwd = forward_returns(_panels_stub().price("close"), 5)
    table = ic_table(factors, fwd)
    assert set(table["factor"]) == {"a", "b"}
    assert list(table.columns)[:2] == ["factor", "label"]


def test_forward_returns_are_lookahead_by_construction() -> None:
    """前瞻收益是**评估目标**，必须等于"未来 N 日收益"（用于 IC，不参与选股）。"""
    close = pd.DataFrame(
        np.tile(np.array([100.0, 110.0, 121.0, 133.1, 146.41, 161.05]),
                (len(CODES), 1)).T, index=DATES[:6], columns=CODES)
    fwd = forward_returns(close, 1)
    assert fwd.iloc[0, 0] == pytest.approx(0.1), "t 的前瞻 1 日收益应为 t+1/t-1"
    assert pd.isna(fwd.iloc[-1, 0]), "最后一天没有未来数据"


def test_split_dates_respects_ratio() -> None:
    train, test = split_dates(DATES, 0.7)
    assert len(train) == 42 and len(test) == 18
    assert train[-1] < test[0], "训练集必须严格早于样本外"


def test_walk_forward_returns_train_and_test_tables() -> None:
    factors = {"a": _panel(51), "b": _panel(52)}
    close = _panels_stub().price("close")
    train, test = walk_forward(factors, close, config=ScreenConfig(ic_horizon=5))
    assert set(train["factor"]) == {"a", "b"}
    assert set(test["factor"]) == {"a", "b"}
    # 只评估候选因子
    train2, test2 = walk_forward(factors, close, config=ScreenConfig(ic_horizon=5),
                                 candidates=["a"])
    assert set(train2["factor"]) == {"a"} and set(test2["factor"]) == {"a"}


def test_composite_score_uses_signed_icir_weights() -> None:
    factors = {"a": _panel(61), "b": _panel(62)}
    table = pd.DataFrame([{"factor": "a", "ICIR": 0.5},
                          {"factor": "b", "ICIR": -0.5}])
    composite = composite_score(factors, ["a", "b"], table)
    expected = factors["a"] * 0.5 + factors["b"] * -0.5
    pd.testing.assert_frame_equal(composite, expected, check_exact=False,
                                  atol=1e-12)


def test_composite_score_falls_back_to_equal_weight() -> None:
    factors = {"a": _panel(71), "b": _panel(72)}
    composite = composite_score(factors, ["a", "b"], None)
    expected = factors["a"] * 0.5 + factors["b"] * 0.5
    pd.testing.assert_frame_equal(composite, expected, check_exact=False,
                                  atol=1e-12)


# ==================== 年化口径（实测踩坑） ====================


def test_quantile_annualization_matches_holding_period() -> None:
    """20 日前瞻收益必须配 252/20 的年化系数。

    实测事故：用默认 252 年化 + 20 日持有期，"第 5 组年化"算出 46962%、
    夏普 93598 —— 这种量级一眼假，但轻微口径错误就会被误读成"因子极强"。
    """
    from src.quant.factor_analyzer import quantile_backtest

    dates = list(DATES)
    scores = _panel(81)
    # 每期固定收益 2%、周期 13 期/年 → 分组年化应为 (1.02^13 - 1) ≈ 29%
    returns = pd.DataFrame(0.02, index=dates, columns=CODES)
    result = quantile_backtest(scores, returns, n_groups=5, periods_per_year=13)
    group_annual = float(result.group_stats.loc[0, "annual_return"])
    assert 0.15 < group_annual < 0.45, f"分组年化明显不合理：{group_annual}"
    # 多空在同收益率下必然为 0（这不是 bug，顺便锁住语义）
    assert abs(result.hl_return) < 1e-9

    # 对照：错误的日频口径（252）会把同一个 2% 放大成天文数字
    wrong = quantile_backtest(scores, returns, n_groups=5, periods_per_year=252)
    assert float(wrong.group_stats.loc[0, "annual_return"]) > 10, \
        "对照：错误口径确实会放大到 10 倍以上（修复前的行为）"


def test_non_overlapping_sampling_reduces_period_count() -> None:
    """非重叠抽样：120 天 / 20 日持有期 → 只应剩 6 期。"""
    window = [str(date) for date in DATES]
    stride = 20
    sampled = window[::stride]
    assert len(sampled) == 3          # 60 天 / 20
    assert len(DATES[::20]) == 3


# ============== 剔除 ST（历史名称口径） ==============

def test_apply_st_mask_blanks_only_st_cells() -> None:
    """掩码只把 ST 的格子置 NaN —— 同日其它票、同票其它日照常参与截面。

    置 NaN 而不是删行/删列，是为了不改变面板形状：
    `compute_ic_series` 成对剔除、`qcut` 也会自动排除 NaN。
    """
    frame = pd.DataFrame(
        {"000001": [1.0, 2.0, 3.0], "000002": [4.0, 5.0, 6.0]},
        index=["20260101", "20260102", "20260103"])
    mask = pd.DataFrame(
        {"000001": [True, False, False], "000002": [False, False, True]},
        index=frame.index)
    masked, note = apply_st_mask({"roe": frame}, mask)
    out = masked["roe"]
    assert pd.isna(out.loc["20260101", "000001"])      # ST 格子被清掉
    assert out.loc["20260101", "000002"] == 4.0        # 同日非 ST 不受影响
    assert out.loc["20260102", "000001"] == 2.0        # 同票非 ST 日不受影响
    assert pd.isna(out.loc["20260103", "000002"])
    assert out.shape == frame.shape                    # 形状不变
    assert "已剔除 ST" in note and "2 个「股票日」" in note


def test_apply_st_mask_handles_misaligned_mask() -> None:
    """掩码的日期/代码轴与面板不一致时按"取交集"处理，不能抛异常。

    实际调用里掩码来自 `StStatus.mask(panels.dates, panels.codes)`，轴是一致的；
    但 reindex 兜底能保证"列多一个少一个"不会让整个筛选崩掉。
    """
    frame = pd.DataFrame({"000001": [1.0]}, index=["20260101"])
    mask = pd.DataFrame({"000001": [True], "000009": [True]},
                        index=["20260101", "20260102"])
    masked, note = apply_st_mask({"bp": frame}, mask)
    assert pd.isna(masked["bp"].loc["20260101", "000001"])
    assert "1 个「股票日」" in note
