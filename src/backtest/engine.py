"""回测评估：方向命中率、前瞻收益、多头策略净值曲线（全部本地计算）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.backtest.signals import Bar, TrendPEConfig, generate_signals

_DEFAULT_HORIZONS = (1, 3, 6)  # 月
_TRADING_MONTHS = 12


@dataclass(frozen=True)
class CostConfig:
    """交易成本与空仓收益（费率均为小数，如万2.5=0.00025）。

    全部默认0 → 与旧版零成本回测完全一致。
    - commission_rate：单边佣金，买/卖双边各收
    - stamp_tax_rate：印花税，仅卖出收取（ETF/指数回测可置0）
    - slippage_rate：单边滑点，买入价上浮/卖出价下压
    - cash_annual_yield：空仓资金货币基金年化（如0.015），按月/12计息
    - initial_capital：初始资金，仅用于期末资产金额展示
    """

    commission_rate: float = 0.0
    stamp_tax_rate: float = 0.0
    slippage_rate: float = 0.0
    cash_annual_yield: float = 0.0
    initial_capital: float = 1.0


@dataclass(frozen=True)
class BacktestResult:
    rule: dict[str, Any]
    periods: int
    signals: dict[str, int]
    directional: dict[str, Any]
    strategy: dict[str, Any]
    equity_curve: list[dict[str, Any]]


def forward_return(bars: list[Bar], t: int, horizon: int) -> float | None:
    """t月末信号建仓，持有horizon个月的收益率；样本不足返回None。"""
    t_end = t + horizon
    if t_end >= len(bars):
        return None
    base = bars[t].price
    if base == 0:
        return None
    return bars[t_end].price / base - 1


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def directional_stats(
    bars: list[Bar], signals: list[int], horizons: tuple[int, ...]
) -> dict[str, Any]:
    """按信号分组统计各持有期的方向命中率与平均前瞻收益。

    命中：看多(+1)且前瞻收益>0，或看空(-1)且前瞻收益<0。
    基准（always_long）：所有可计算月份的平均前瞻收益，用于比较。
    """
    out: dict[str, Any] = {}
    for h in horizons:
        buckets: dict[int, dict[str, list[float]]] = {
            1: {"fwd": [], "hit": []},
            -1: {"fwd": [], "hit": []},
        }
        baseline: list[float] = []
        for t in range(len(bars)):
            fwd = forward_return(bars, t, h)
            if fwd is None:
                continue
            baseline.append(fwd)
            sig = signals[t]
            if sig in buckets:
                buckets[sig]["fwd"].append(fwd)
                hit = (sig == 1 and fwd > 0) or (sig == -1 and fwd < 0)
                buckets[sig]["hit"].append(1.0 if hit else 0.0)

        def _block(bucket: dict[str, list[float]]) -> dict[str, Any]:
            fwds = bucket["fwd"]
            hits = bucket["hit"]
            return {
                "n": len(fwds),
                "hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
                "avg_forward_return": round(_mean(fwds), 6) if fwds else None,
            }

        out[f"{h}m"] = {
            "long": _block(buckets[1]),
            "avoid": _block(buckets[-1]),
            "always_long_baseline": round(_mean(baseline), 6) if baseline else None,
            "baseline_n": len(baseline),
        }
    return out


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    worst = 0.0
    for v in equity:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def _curve_metrics(curve: list[float], monthly_returns: list[float]) -> dict[str, Any]:
    """由净值序列与月收益计算累计/年化/波动/回撤/夏普。"""
    n_months = len(monthly_returns)
    cumulative = curve[-1] - 1 if curve else 0.0
    cagr: float | None = None
    if n_months > 0 and curve[-1] > 0:
        cagr = curve[-1] ** (_TRADING_MONTHS / n_months) - 1
    if monthly_returns:
        avg = sum(monthly_returns) / len(monthly_returns)
        var = sum((r - avg) ** 2 for r in monthly_returns) / len(monthly_returns)
        vol_annual = math.sqrt(var) * math.sqrt(_TRADING_MONTHS)
        sharpe = (avg * _TRADING_MONTHS / vol_annual) if vol_annual > 0 else None
    else:
        vol_annual = 0.0
        sharpe = None
    return {
        "cumulative_return": round(cumulative, 6),
        "cagr": round(cagr, 6) if cagr is not None else None,
        "annualized_volatility": round(vol_annual, 6),
        "max_drawdown": round(_max_drawdown(curve), 6),
        "sharpe_rf0": round(sharpe, 4) if sharpe is not None else None,
        "invested_months": sum(1 for r in monthly_returns if r != 0.0),
        "total_months": n_months,
    }


def _simulate_equity(
    bars: list[Bar], signals: list[int], cost: CostConfig
) -> tuple[list[float], dict[str, Any]]:
    """全仓/空仓月度账户模拟，返回净值序列(长度=月数)与成本明细。

    时点：t月末按signal[t]调仓（执行价含滑点，扣佣金/印花税），
    持仓经历[t,t+1]股价变动；空仓经历货基月息。月末按收盘价盯市。
    零成本时净值与旧版逐月月收益连乘完全一致。
    """
    cash = cost.initial_capital
    shares = 0.0
    prev_holding = False
    total_fees = 0.0
    trades = 0
    equity_path = [cost.initial_capital]

    for t in range(len(bars) - 1):
        price_t = bars[t].price
        holding = signals[t] == 1
        if holding and not prev_holding and price_t > 0:
            exec_px = price_t * (1 + cost.slippage_rate)
            # 佣金从现金扣减：shares*px*(1+commission)=cash
            shares = cash / (exec_px * (1 + cost.commission_rate))
            total_fees += cash - shares * exec_px
            cash = 0.0
            trades += 1
        elif not holding and prev_holding and price_t > 0:
            exec_px = price_t * (1 - cost.slippage_rate)
            gross = shares * exec_px
            net = gross * (1 - cost.commission_rate - cost.stamp_tax_rate)
            total_fees += gross - net
            cash = net
            shares = 0.0
            trades += 1

        if shares > 0:
            equity_next = shares * bars[t + 1].price
        else:
            cash *= 1 + cost.cash_annual_yield / _TRADING_MONTHS
            equity_next = cash
        equity_path.append(equity_next)
        prev_holding = holding

    curve = [v / cost.initial_capital for v in equity_path]
    cost_detail = {
        "trades": trades,
        "total_transaction_cost": round(total_fees, 2),
        "initial_capital": cost.initial_capital,
        "final_equity": round(equity_path[-1], 2),
    }
    return curve, cost_detail


def run_backtest(
    bars: list[Bar],
    cfg: TrendPEConfig,
    horizons: tuple[int, ...] = _DEFAULT_HORIZONS,
    cost: CostConfig | None = None,
) -> BacktestResult:
    """执行完整回测：信号→方向统计→多头策略净值（含成本/空仓利息）。"""
    cost = cost or CostConfig()
    signals = generate_signals(bars, cfg)

    monthly_returns = [
        bars[t + 1].price / bars[t].price - 1
        for t in range(len(bars) - 1)
        if bars[t].price != 0
    ]

    strat_curve, cost_detail = _simulate_equity(bars, signals, cost)
    strategy_monthly = [
        strat_curve[i + 1] / strat_curve[i] - 1 for i in range(len(strat_curve) - 1)
    ]

    def _equity(returns: list[float]) -> list[float]:
        curve = [1.0]
        for r in returns:
            curve.append(curve[-1] * (1 + r))
        return curve

    hold_curve = _equity(monthly_returns)
    strategy_stats = _curve_metrics(strat_curve, strategy_monthly)
    hold_stats = _curve_metrics(hold_curve, monthly_returns)
    # 实际持仓月数（信号决定，旧实现以非零月收益近似）
    strategy_stats["invested_months"] = sum(1 for s in signals[:-1] if s == 1)
    strategy_stats["buy_and_hold"] = hold_stats
    strategy_stats["excess_cumulative_return"] = round(
        strategy_stats["cumulative_return"] - hold_stats["cumulative_return"], 6
    )
    strategy_stats.update(cost_detail)

    signal_counts = {"long": signals.count(1), "neutral": signals.count(0),
                     "avoid": signals.count(-1)}
    equity_curve = [
        {"period": bars[i].period if i < len(bars) else "",
         "strategy": round(strat_curve[i], 4),
         "buy_and_hold": round(hold_curve[i], 4),
         "signal": signals[i] if i < len(signals) else 0}
        for i in range(len(strat_curve))
    ]

    return BacktestResult(
        rule={
            "trend_indicator": cfg.trend_indicator,
            "eps_pct": cfg.eps_pct,
            "pe_watermark": cfg.pe_watermark,
            "kind": "trend+PE_gate_long_only",
            "cost": {
                "commission_rate": cost.commission_rate,
                "stamp_tax_rate": cost.stamp_tax_rate,
                "slippage_rate": cost.slippage_rate,
                "cash_annual_yield": cost.cash_annual_yield,
            },
        },
        periods=len(bars),
        signals=signal_counts,
        directional=directional_stats(bars, signals, horizons),
        strategy=strategy_stats,
        equity_curve=equity_curve,
    )


def result_to_dict(result: BacktestResult) -> dict[str, Any]:
    return {
        "rule": result.rule,
        "periods": result.periods,
        "signals": result.signals,
        "directional": result.directional,
        "strategy": result.strategy,
        "equity_curve": result.equity_curve,
    }
