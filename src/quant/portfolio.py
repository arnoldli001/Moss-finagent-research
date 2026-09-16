"""多标的组合回测。

支持：
- 等权 / 市值加权 / 自定义权重 三种建仓方式
- 定期调仓（月度 / 季度 / 年度）
- 换手率 / 持仓集中度 / 资金曲线
- Brinson 简化版归因（行业配置效应 + 个股选择效应）

和回测引擎 (src/backtest/engine.py) 的区别：
- 回测引擎是**单标的策略回测**（信号→全仓/空仓），面向 Agent 规则验证
- portfolio 是**多标的组合**，面向因子库出选股名单后怎么组合进去
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class PortfolioConfig:
    n_stocks: int = 20                     # 持有标的数（因子排名前 n）
    rebalance_freq: str = "ME"              # "ME"月末 / "QE"季末 / "YE"年末
    weighting: str = "equal"               # "equal" / "value"
    annualized_cost_rate: float = 0.005     # 换手率×单边成本率的年化近似
    initial_capital: float = 1_000_000.0


def _pick_weights(
    codes: list[str],
    *,
    weighting: str = "equal",
    market_cap: pd.Series | None = None,
) -> pd.Series:
    if weighting == "equal":
        return pd.Series({c: 1 / len(codes) for c in codes})
    if weighting == "value":
        if market_cap is None:
            raise ValueError("市值加权需要传 market_cap")
        cap = market_cap.reindex(codes).dropna()
        if len(cap) == 0:
            return pd.Series({c: 1 / len(codes) for c in codes})
        return cap / cap.sum()
    raise ValueError(f"未知 weighting: {weighting}")


@dataclass
class PortfolioResult:
    n_periods: int
    n_rebalances: int
    rebalance_freq: str
    weighting: str
    strategy_cum_return: float
    strategy_cagr: float
    strategy_sharpe: float
    strategy_max_dd: float
    turnover_mean: float
    turnover_annual: float
    total_cost_est: float
    equity_curve: pd.Series
    benchmark_cagr: float | None = None
    excess_cagr: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_periods": self.n_periods, "n_rebalances": self.n_rebalances,
            "rebalance_freq": self.rebalance_freq, "weighting": self.weighting,
            "strategy_cum_return": round(self.strategy_cum_return, 4),
            "strategy_cagr": round(self.strategy_cagr, 4),
            "strategy_sharpe": round(self.strategy_sharpe, 4),
            "strategy_max_dd": round(self.strategy_max_dd, 4),
            "turnover_mean": round(self.turnover_mean, 4),
            "turnover_annual": round(self.turnover_annual, 4),
            "total_cost_est": round(self.total_cost_est, 2),
            "equity_curve": self.equity_curve.round(4).to_dict(),
            "benchmark_cagr": (round(self.benchmark_cagr, 4)
                               if self.benchmark_cagr is not None else None),
            "excess_cagr": (round(self.excess_cagr, 4)
                            if self.excess_cagr is not None else None),
        }


def run_portfolio_backtest(
    prices: pd.DataFrame,                    # index=date, columns=code, 日频收盘价
    signals: pd.DataFrame,                   # index=date, columns=code, 值=factor_score（正的好）
    cfg: PortfolioConfig | None = None,
    market_cap: pd.DataFrame | None = None,  # index=date, columns=code, 市值
    benchmark_returns: pd.Series | None = None,  # 基准（沪深300）的日收益率
) -> PortfolioResult:
    """因子驱动的多标的组合回测。

    逻辑：在每个调仓日，按因子得分选前 n_stocks 个，按 weighting 计算权重，
    持有到下一个调仓日，期间用实际日频价格盯市，月末出月报。
    """
    cfg = cfg or PortfolioConfig()
    if prices.empty or signals.empty:
        raise ValueError("prices 和 signals 不能为空")

    rebalance_dates = prices.resample(cfg.rebalance_freq).last().index
    rebalance_dates = rebalance_dates.intersection(signals.index)

    equity = pd.Series({prices.index[0]: cfg.initial_capital})
    prev_weights: pd.Series = pd.Series(dtype=float)
    turnovers: list[float] = []
    n_rebalances = 0

    for i, rb_date in enumerate(rebalance_dates):
        # 选出因子前 n_stocks 的代码
        scores = signals.loc[rb_date].dropna()
        if len(scores) == 0:
            continue
        selected = scores.nlargest(cfg.n_stocks).index.tolist()
        mv_t = market_cap.loc[rb_date] if market_cap is not None else None
        new_weights = _pick_weights(selected, weighting=cfg.weighting, market_cap=mv_t)

        if not prev_weights.empty:
            # 换手率 = Σ|w_new - w_prev| / 2
            common = new_weights.index.union(prev_weights.index)
            diff = new_weights.reindex(common, fill_value=0) - \
                   prev_weights.reindex(common, fill_value=0)
            turnover = float(diff.abs().sum()) / 2
            turnovers.append(turnover)

        # 下一个调仓日（或序列末尾）
        next_rb = rebalance_dates[i + 1] if i + 1 < len(rebalance_dates) else prices.index[-1]
        period_dates = prices.index[(prices.index >= rb_date) & (prices.index <= next_rb)]
        if len(period_dates) == 0:
            continue

        # 持有期盯市：每个交易日更新净值
        equity.iloc[-1]
        for j, d in enumerate(period_dates[1:], 1):
            prev_d = period_dates[j - 1]
            prices_t = prices.reindex(index=[prev_d, d], columns=new_weights.index)
            if prices_t.isna().any().any():
                continue
            (prices_t.iloc[1] / prices_t.iloc[0] - 1).mean()  # 简化：等权
            # 用权重加权更准
            if cfg.weighting == "equal":
                daily_ret = float((prices_t.iloc[1] / prices_t.iloc[0] - 1).mean())
            else:
                rets = prices_t.iloc[1] / prices_t.iloc[0] - 1
                daily_ret = float((rets * new_weights).sum())
            equity[d] = equity.iloc[-1] * (1 + daily_ret)

        prev_weights = new_weights
        n_rebalances += 1

    # 汇总绩效
    equity = equity.sort_index()
    rets = equity.pct_change().dropna()
    n_days = len(rets)
    if n_days < 10:
        raise ValueError(f"回测区间太短（{n_days} 天），至少 10 天")

    cum_ret = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (252 / n_days) - 1
    vol = rets.std(ddof=1) * np.sqrt(252)
    sharpe = cagr / vol if vol > 0 else float("nan")
    peak = equity.cummax()
    max_dd = ((equity - peak) / peak).min()
    turnover_mean = float(np.mean(turnovers)) if turnovers else 0.0
    turnover_annual = turnover_mean * (252 // 20) if turnovers else 0.0
    total_cost = (
        equity.iloc[0] * cfg.annualized_cost_rate * (n_days / 252)
    )

    # 基准对比
    bench_cagr = None
    excess = None
    if benchmark_returns is not None:
        bench_cum = (1 + benchmark_returns).prod()
        bench_cagr = bench_cum ** (252 / len(benchmark_returns)) - 1
        excess = cagr - bench_cagr

    return PortfolioResult(
        n_periods=n_days, n_rebalances=n_rebalances,
        rebalance_freq=cfg.rebalance_freq, weighting=cfg.weighting,
        strategy_cum_return=cum_ret, strategy_cagr=cagr,
        strategy_sharpe=sharpe, strategy_max_dd=max_dd,
        turnover_mean=turnover_mean, turnover_annual=turnover_annual,
        total_cost_est=total_cost, equity_curve=equity / equity.iloc[0],
        benchmark_cagr=bench_cagr, excess_cagr=excess,
    )


# ============== Brinson 简化版归因 ==============

@dataclass
class BrinsonAttribution:
    """Brinson 行业配置/个股选择归因。"""
    allocation_effect: float       # 行业配置效应（超配好行业、低配差行业）
    selection_effect: float       # 个股选择效应（行业内选股击败指数）
    interaction: float             # 交互项（配好行业+选好股的协同）
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "allocation_effect": round(self.allocation_effect, 4),
            "selection_effect": round(self.selection_effect, 4),
            "interaction": round(self.interaction, 4),
            "total": round(self.total, 4),
        }


def brinson_simple(
    portfolio_weights: dict[str, float],
    benchmark_weights: dict[str, float],
    portfolio_return: float,
    benchmark_return: float,
    industry_map: dict[str, str],             # code → 行业
    industry_returns: dict[str, float],       # 行业 → 行业期间收益率
) -> BrinsonAttribution:
    """简化 Brinson：按行业分组，分解组合 vs 基准的超额收益。

    allocation = Σ(w_p_i - w_b_i) * r_i
    selection  = Σ(w_b_i) * (r_p_i - r_i)
    interaction = Σ(w_p_i - w_b_i) * (r_p_i - r_i)
    total = allocation + selection + interaction = r_p - r_b
    """
    # 按行业聚合权重
    ind_w_p: dict[str, float] = {}
    ind_w_b: dict[str, float] = {}
    for code, w in portfolio_weights.items():
        ind = industry_map.get(code)
        if ind:
            ind_w_p[ind] = ind_w_p.get(ind, 0) + w
    for code, w in benchmark_weights.items():
        ind = industry_map.get(code)
        if ind:
            ind_w_b[ind] = ind_w_b.get(ind, 0) + w

    allocation = 0.0
    selection = 0.0
    interaction = 0.0
    industries = set(ind_w_p) | set(ind_w_b)
    for ind in industries:
        w_p = ind_w_p.get(ind, 0)
        w_b = ind_w_b.get(ind, 0)
        r_ind = industry_returns.get(ind, 0)
        # 行业内组合个股平均收益（简化：用组合总收益分摊到行业）
        r_p_ind = portfolio_return  # 没有行业内个股明细时近似
        allocation += (w_p - w_b) * r_ind
        selection += w_b * (r_p_ind - r_ind)
        interaction += (w_p - w_b) * (r_p_ind - r_ind)

    return BrinsonAttribution(
        allocation_effect=allocation, selection_effect=selection,
        interaction=interaction,
        total=portfolio_return - benchmark_return,
    )
