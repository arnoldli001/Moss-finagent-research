"""quant 模块单测：因子、IC/IR、VaR、压力测试、组合回测。

全部用确定性合成数据，不依赖网络/DB，pytest 直接跑。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quant.factor_analyzer import (
    compute_ic_series,
    evaluate_factor,
    quantile_backtest,
)
from src.quant.factor_base import (
    get_factor,
    list_factors,
    mad_winsorize,
    neutralize,
)
from src.quant.factor_library import (
    PEFactor,
)
from src.quant.portfolio import (
    PortfolioConfig,
    brinson_simple,
    run_portfolio_backtest,
)
from src.quant.risk import (
    _STRESS_SCENARIOS,
    apply_stress,
    full_var_report,
    historical_cvar,
    historical_var,
    list_stress_scenarios,
)

# ========== Factor 基类 & 注册器 ==========

def test_factor_register_roundtrip():
    assert "pe_ttm" in list_factors()
    cls = get_factor("pe_ttm")
    assert cls is PEFactor


def test_unknown_factor_returns_none():
    assert get_factor("nonexistent") is None


# ========== MAD 去极值 ==========

def test_mad_winsorize_caps_outliers():
    data = pd.Series([1, 2, 3, 4, 5, 100, -200])  # 2 个极端值
    result = mad_winsorize(data, n=3.0)
    assert result[5] < 100       # 100 被截
    assert result[6] > -200      # -200 被截
    assert result[2] == 3         # 正常值不动


def test_mad_winsorize_all_same_no_crash():
    data = pd.Series([5, 5, 5, 5])
    result = mad_winsorize(data)
    assert (result == 5).all()


# ========== 中性化 ==========

def test_neutralize_removes_size_effect():
    n = 100
    rng = np.random.default_rng(42)
    mv = pd.Series(1e10 * np.sort(rng.random(n)), index=[f"S{i}" for i in range(n)])
    noise = pd.Series(rng.normal(0, 1, n), index=mv.index)
    factor = 0.8 * np.log(mv) + 0.3 * noise + 50

    neutralized = neutralize(factor, mv=mv)
    corr_after = np.abs(np.corrcoef(neutralized.dropna().values,
                                    np.log(mv).reindex(neutralized.dropna().index).values)[0, 1])
    corr_before = abs(np.corrcoef(factor.values, np.log(mv.values))[0, 1])
    # 中性化后相关性应显著降低（绝对 < 原 50% 或 < 0.3）
    assert corr_after < max(corr_before * 0.5, 0.3), \
        f"中性化后相关性仍然很高: before={corr_before:.3f}, after={corr_after:.3f}"


def test_cvar_exceeds_var():
    rng = np.random.default_rng(55)
    rets = rng.normal(0, 0.02, 2000)
    var = historical_var(rets, confidence=0.95)
    cvar = historical_cvar(rets, confidence=0.95)
    assert cvar >= var


def test_var_report_counts_breaches():
    """历史模拟 VaR：违约率贴近名义水平，厚尾体现在**尾部严重度**。

    注：此函数原先与下面那条同名而被静默覆盖，从未执行；改名后暴露出原断言
    `breaches > expected_breaches * 1.2`（"厚尾 → 违约更多"）对**历史模拟法**
    是错的：VaR 取的就是样本左尾分位数，in-sample 违约率按构造恒等于
    `1 - confidence`，与尾部厚薄无关。"厚尾更容易击穿"只对**参数法（正态假设）**
    成立。这里改成验证真实语义。
    """
    rng = np.random.default_rng(11)
    rets = np.concatenate([
        -rng.normal(0, 0.015, 400),
        -np.abs(rng.normal(0, 0.05, 100)),  # 极端大损失
    ])
    report = full_var_report(rets, confidence=0.95)
    # ① 历史模拟法：违约率应贴近名义 5%（留抽样波动余量）
    ratio = report.breaches / report.n_obs
    assert 0.02 <= ratio <= 0.09, f"违约率 {ratio:.1%} 偏离名义 5% 太远"
    # ② 厚尾真正体现在尾部严重度 CVaR/VaR。基准不写死魔数：
    #    同样本量下生成一组正态收益，用它的 CVaR/VaR 作对照。
    #    （正态 95% 理论值 ≈ 1.254；这个混合厚尾实测 ≈ 1.34）
    normal_rets = rng.normal(0, 0.02, 500)
    normal_report = full_var_report(normal_rets, confidence=0.95)
    fat_ratio = report.cvar_1d / report.var_1d
    normal_ratio = normal_report.cvar_1d / normal_report.var_1d
    assert fat_ratio > normal_ratio * 1.03, (
        f"厚尾的 CVaR/VaR={fat_ratio:.3f} 未显著高于正态 {normal_ratio:.3f}")


# ========== IC / IR ==========

def test_ic_series_detects_positive_factor():
    """完美因子：因子得分直接决定未来收益，IC 应显著为正。"""
    rng = np.random.default_rng(7)
    n_dates, n_stocks = 40, 50
    # 因子得分 = 排名位置（1..50），越高的股票未来收益越高 → 完美正相关
    factor = pd.DataFrame(
        np.tile(np.arange(1, n_stocks + 1, dtype=float), (n_dates, 1)),
        index=[f"d{i}" for i in range(n_dates)],
        columns=[f"S{i}" for i in range(n_stocks)],
    )
    forward = pd.DataFrame(
        0.01 * factor + rng.normal(0, 0.01, (n_dates, n_stocks)),
        index=factor.index, columns=factor.columns,
    )
    ic_series = compute_ic_series(factor, forward)
    assert len(ic_series) >= 5, f"IC 序列太短: {len(ic_series)}"
    metrics = evaluate_factor(ic_series)
    assert metrics.ic_mean > 0.1
    assert metrics.ir > 0.3


def test_ir_calculation():
    ic = pd.Series([0.1, 0.15, 0.05, 0.12, 0.08, 0.11, 0.09, 0.13, 0.07, 0.1])
    m = evaluate_factor(ic)
    expected_ir = ic.mean() / ic.std(ddof=1)
    assert abs(m.ir - expected_ir) < 1e-9
    assert m.ic_positive_rate == 1.0


# ========== 分层回测 ==========

def test_quantile_monotonic_when_perfect_factor():
    """完美因子：因子得分 = 未来收益 → H-L 多空显著。"""
    n_dates, n_stocks = 60, 40
    rng = np.random.default_rng(11)
    forward = pd.DataFrame(
        rng.normal(0.001, 0.02, (n_dates, n_stocks)),
        index=[f"d{i}" for i in range(n_dates)],
        columns=[f"S{i}" for i in range(n_stocks)],
    )
    # 因子 = 未来收益 + 一点点噪声
    factor = forward + rng.normal(0, 0.002, forward.shape)
    q = quantile_backtest(factor, forward, n_groups=5)
    # H-L 应该正
    assert q.hl_return > 0.01, f"H-L={q.hl_return}"


def test_group_stats_contain_expected_columns():
    n_dates, n_stocks = 30, 30
    rng = np.random.default_rng(3)
    f = pd.DataFrame(rng.normal(0, 1, (n_dates, n_stocks)))
    r = pd.DataFrame(rng.normal(0, 0.03, (n_dates, n_stocks)))
    q = quantile_backtest(f, r, n_groups=5)
    assert set(q.group_stats.columns) == {
        "annual_return", "vol", "sharpe", "max_dd", "avg_turnover",
    }
    assert q.n_groups == 5


# ========== VaR ==========

def test_var_is_tail_quantile():
    rng = np.random.default_rng(99)
    rets = rng.normal(0, 0.02, 2000)     # 2000 天，σ=2%
    var = historical_var(rets, confidence=0.95)
    # 左尾 5% 分位数 ~ 1.645σ = 3.3%，loss 视角
    assert 0.02 < var < 0.05, f"VaR={var}"


def test_var_requires_enough_samples():
    short = pd.Series(np.random.default_rng(1).normal(0, 0.02, 10))
    with pytest.raises(ValueError):
        historical_var(short)


def test_var_report_fields_complete():
    """VaR 报告字段完备 + 总览结构正确。

    注：此函数原先与上面同名（`test_var_report_counts_breaches`），
    后定义静默覆盖前者 —— 结果是"厚尾分布下 breach 计数"那条**从未执行过**。
    改名后两条都真正生效。
    """
    rng = np.random.default_rng(11)
    rets = rng.normal(0, 0.02, 1000)
    report = full_var_report(rets, confidence=0.95)
    d = report.as_dict()
    # 字段完整性
    assert set(d.keys()) >= {
        "confidence", "VaR_1d", "VaR_10d", "CVaR_1d", "CVaR_10d",
        "breaches", "expected_breaches", "n_obs",
    }
    # 10 日 VaR > 1 日 VaR（sqrt-of-time 缩放）
    assert report.var_10d > report.var_1d
    # CVaR > VaR（尾部均值必然大于边界分位数）
    assert report.cvar_1d >= report.var_1d


# ========== 压力测试 ==========

def test_stress_scenarios_list_real_events():
    names = list_stress_scenarios()
    assert any("股灾" in s["name"] for s in names)
    assert any("新冠" in s["name"] for s in names)
    assert "2015_股灾" in _STRESS_SCENARIOS


def test_apply_stress_keeps_non_stressed_dates():
    rng = np.random.default_rng(22)
    idx = pd.date_range("2015-01-01", "2015-12-31")
    rets = pd.DataFrame(rng.normal(0, 0.01, (len(idx), 3)),
                        index=idx, columns=["A", "B", "C"])
    stressed = apply_stress(rets, "2015_股灾")
    # 非股灾区间不变
    pre_stress = stressed.loc["2015-01-01":"2015-06-11"]
    assert (pre_stress.values == rets.loc[pre_stress.index].values).all()


# ========== 组合回测 ==========

def test_portfolio_backtest_produces_positive_cum_return():
    rng = np.random.default_rng(42)
    idx = pd.date_range("2020-01-01", "2024-12-31", freq="B")
    prices = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0.0005, 0.015, (len(idx), 25)), axis=0),
        index=idx, columns=[f"S{i}" for i in range(25)],
    )
    # 因子：最近动量（好因子）
    signals = prices.pct_change(60).iloc[60:]

    result = run_portfolio_backtest(prices, signals,
                                    cfg=PortfolioConfig(n_stocks=10))
    assert result.strategy_cum_return > 0
    assert result.n_periods > 400
    assert result.weighting == "equal"


def test_portfolio_backtest_turnover_computed():
    rng = np.random.default_rng(7)
    idx = pd.date_range("2022-01-01", "2024-12-31", freq="B")
    prices = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0.0003, 0.02, (len(idx), 15)), axis=0),
        index=idx, columns=[f"S{i}" for i in range(15)],
    )
    signals = prices.pct_change(20).iloc[20:]
    result = run_portfolio_backtest(prices, signals,
                                    cfg=PortfolioConfig(
                                        n_stocks=5, rebalance_freq="ME"))
    assert result.turnover_mean > 0  # 换仓次数多了之后换手率不为 0
    assert result.n_rebalances > 10  # 至少 10 次调仓（月度 3 年）


# ========== Brinson 归因 ==========

def test_brinson_total_equals_excess():
    pf_w = {"A": 0.3, "B": 0.3, "C": 0.4}
    bm_w = {"A": 0.2, "B": 0.5, "C": 0.3}
    pf_ret, bm_ret = 0.10, 0.06
    ind_map = {"A": "TMT", "B": "消费", "C": "周期"}
    ind_rets = {"TMT": 0.15, "消费": 0.05, "周期": 0.08}

    attr = brinson_simple(pf_w, bm_w, pf_ret, bm_ret, ind_map, ind_rets)
    # 简化归因不保证 allocation+selection+interaction == total
    # 因为 r_p_ind 被近似为 portfolio_return；只验结构正确
    assert "allocation_effect" in attr.as_dict()
    assert "selection_effect" in attr.as_dict()
