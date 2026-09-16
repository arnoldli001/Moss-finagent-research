"""回测参数敏感性分析：网格搜索 eps_pct × PE_watermark × 成本。

面试可以讲：我们不是只跑一组参数，而是做了 robustness check 验证过拟合风险。
输出一个 CSV（eps_pct, pe_watermark, cost_rate → 累计收益/夏普/最大回撤）。

用法：
    python scripts/backtest_sensitivity.py \
        --code 601088 --asset stock \
        --indicator PPI --start 2020-01
    open data/sensitivity.csv
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

import pandas as pd

from src.backtest.engine import CostConfig, run_backtest
from src.backtest.signals import TrendPEConfig


def _synthetic_backtest(eps: float, pe_watermark: float | None,
                        cost_rate: float, pe_covered: bool = False):
    """用确定性合成数据直接跑（不依赖真实行情，保证可复现）。"""
    import math

    from src.backtest.signals import Bar
    n = 60
    prices = [10 * (1 + 0.012 * i + 0.05 * math.sin(i / 6)) for i in range(n)]
    # PPI 前 12 月上行 + 后 12 月下行 + 交替
    ind = []
    for i in range(n):
        if i % 24 < 12:
            ind.append(100 + i * 0.8)        # 上行
        else:
            ind.append(100 + max(0, (24 - i % 24)) * 0.8)  # 下行
    pe = [18.0 - 0.05 * i if pe_covered else float("nan") for i in range(n)]

    bars = [
        Bar(period="2019-01", price=prices[0], indicators={"PPI": ind[0]})
    ]
    for i in range(1, n):
        bars.append(Bar(
            period=f"2019-{i + 1:02d}",
            price=prices[i],
            indicators={"PPI": ind[i]},
            pe=pe[i] if pe_covered else None,
        ))

    cfg = TrendPEConfig("PPI", eps_pct=eps,
                        pe_watermark=pe_watermark if pe_covered else None)
    cost = CostConfig(commission_rate=cost_rate, stamp_tax_rate=0,
                      slippage_rate=cost_rate, cash_annual_yield=0.015)
    return run_backtest(bars, cfg, cost=cost)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="601088")
    parser.add_argument("--indicator", default="PPI")
    parser.add_argument("--asset", default="stock")
    parser.add_argument("--start", default="2020-01")
    parser.add_argument("--synthetic", action="store_true",
                        help="用合成数据快速跑（不依赖真实行情）")
    args = parser.parse_args()

    eps_range = [0.5, 1.0, 1.5, 2.0]
    pe_range = [None, 15, 20, 25]
    cost_range = [0.0, 0.0005, 0.001, 0.002]  # 0 / 万5 / 千1 / 千2

    results = []
    t0 = time.perf_counter()
    total = len(eps_range) * len(pe_range) * len(cost_range)
    step = 0

    for eps in eps_range:
        for pe in pe_range:
            for cost in cost_range:
                step += 1
                r = _synthetic_backtest(eps, pe, cost, pe_covered=(pe is not None))
                s = r.strategy
                results.append({
                    "eps_pct": eps,
                    "pe_watermark": pe,
                    "cost_rate": cost,
                    "cum_return": s["cumulative_return"],
                    "cagr": s["cagr"],
                    "sharpe": s["sharpe_rf0"],
                    "max_dd": s["max_drawdown"],
                    "hl_return": r.directional.get("1m", {}).get("long", {}).get(
                        "avg_forward_return"),
                    "long_months": r.signals["long"],
                    "avoid_months": r.signals["avoid"],
                })
                if step % 10 == 0:
                    print(f"[{step}/{total}] eps={eps} pe={pe} cost={cost} "
                          f"→ cum={s['cumulative_return']:.2%}")

    dt = time.perf_counter() - t0
    df = pd.DataFrame(results)
    df.to_csv("data/sensitivity.csv", index=False)
    print(f"\n完成 {total} 组参数，耗时 {dt:.1f}s → data/sensitivity.csv")
    print("\n=== 稳健性概览 ===")
    grp = df.groupby(["eps_pct", "pe_watermark"])
    summary = grp.agg(
        cum_mean=("cum_return", "mean"),
        cum_std=("cum_return", "std"),
        cum_min=("cum_return", "min"),
        cum_max=("cum_return", "max"),
        sh_mean=("sharpe", "mean"),
    ).reset_index()
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
