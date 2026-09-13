"""回测引擎离线测试（合成确定性序列，无网络/无LLM）。"""

from __future__ import annotations

from src.backtest.engine import (
    forward_return,
    result_to_dict,
    run_backtest,
)
from src.backtest.signals import (
    Bar,
    TrendPEConfig,
    generate_signals,
    trend_pe_signal,
)


def _bars(prices: list[float], ind: list[float],
          pe: list[float | None] | None = None,
          start_year: int = 2020) -> list[Bar]:
    periods = [
        f"{start_year + i // 12:04d}-{i % 12 + 1:02d}" for i in range(len(prices))
    ]
    pe = pe or [None] * len(prices)
    return [
        Bar(period=periods[i], price=prices[i],
            indicators={"x": ind[i]}, pe=pe[i])
        for i in range(len(prices))
    ]


def test_signal_requires_two_bars():
    cfg = TrendPEConfig("x")
    assert trend_pe_signal([], cfg) == 0
    assert trend_pe_signal([Bar("2020-01", 10, {"x": 100})], cfg) == 0


def test_signal_directions_with_eps_band():
    cfg = TrendPEConfig("x", eps_pct=1.0)
    up = _bars([10, 10], [100, 105])
    assert trend_pe_signal(up, cfg) == 1
    down = _bars([10, 10], [100, 95])
    assert trend_pe_signal(down, cfg) == -1
    flat = _bars([10, 10], [100, 100.5])
    assert trend_pe_signal(flat, cfg) == 0


def test_pe_gate_blocks_expensive_uptrend():
    cfg = TrendPEConfig("x", eps_pct=1.0, pe_watermark=30)
    cheap = _bars([10, 10], [100, 105], pe=[20, 25])
    expensive = _bars([10, 10], [100, 105], pe=[20, 35])
    assert trend_pe_signal(cheap, cfg) == 1
    assert trend_pe_signal(expensive, cfg) == 0


def test_missing_indicator_is_neutral():
    cfg = TrendPEConfig("x")
    hist = [Bar("2020-01", 10, {"other": 1}), Bar("2020-02", 10, {"other": 2})]
    assert trend_pe_signal(hist, cfg) == 0


def test_signals_have_no_lookahead():
    """未来价格/指标变动不得改变t时刻信号。"""
    cfg = TrendPEConfig("x")
    bars = _bars([10, 11, 12], [100, 102, 104])
    signals = generate_signals(bars, cfg)
    extended = _bars([10, 11, 12, 1, 1], [100, 102, 104, 1, 1])
    assert generate_signals(extended, cfg)[:3] == signals


def test_forward_return_and_insufficient_horizon():
    bars = _bars([100, 110, 121], [1, 1, 1])
    assert round(forward_return(bars, 0, 1), 6) == 0.1
    assert round(forward_return(bars, 0, 2), 6) == 0.21
    assert forward_return(bars, 2, 1) is None


def test_backtest_uptrend_strategy_tracks_buy_and_hold():
    # 指标逐月上行5%→t≥1全程看多（t=0无历史为中性，踏空首个月，符合无未来函数）
    n = 24
    ind = [100 * 1.05 ** i for i in range(n)]
    prices = [10 * 1.01 ** i for i in range(n)]
    result = run_backtest(_bars(prices, ind), TrendPEConfig("x"))
    assert result.signals == {"long": n - 1, "neutral": 1, "avoid": 0}
    strat = result.strategy
    assert strat["invested_months"] == n - 2
    assert strat["total_months"] == n - 1
    assert strat["cumulative_return"] < strat["buy_and_hold"]["cumulative_return"]
    assert strat["excess_cumulative_return"] < 0
    assert strat["max_drawdown"] == strat["buy_and_hold"]["max_drawdown"]


def test_backtest_downtrend_strategy_avoids_losses():
    # 指标与价格同步下跌：信号-1，多头策略全程空仓
    n = 18
    ind = [100 * 0.95 ** i for i in range(n)]
    prices = [10 * 0.98 ** i for i in range(n)]
    result = run_backtest(_bars(prices, ind), TrendPEConfig("x"))
    assert result.signals["long"] == 0
    assert result.signals["avoid"] == n - 1
    assert result.strategy["cumulative_return"] == 0.0
    assert result.strategy["max_drawdown"] == 0.0
    assert result.strategy["buy_and_hold"]["cumulative_return"] < 0
    # 看空方向命中率应为100%（前瞻收益均为负）
    block = result.directional["1m"]["avoid"]
    assert block["n"] > 0 and block["hit_rate"] == 1.0


def test_backtest_known_hit_rate():
    # 前两期构造一个+1信号：t=1，之后1月涨；6月跌（index7=9）
    ind = [100, 105] + [105] * 10
    prices = [10, 10, 11, 10, 10, 10, 10, 9, 10, 10, 10, 10]
    result = run_backtest(
        _bars(prices, ind), TrendPEConfig("x"), horizons=(1, 3, 6)
    )
    long_1m = result.directional["1m"]["long"]
    # 信号仅出现在t=1（后续指标持平→0）；1月后11/10-1=0.1
    assert long_1m["n"] == 1 and long_1m["hit_rate"] == 1.0
    assert long_1m["avg_forward_return"] == 0.1
    assert result.directional["6m"]["long"]["n"] == 1  # t=1→t=7: 9/10-1
    assert result.directional["6m"]["long"]["avg_forward_return"] == -0.1


def test_empty_and_single_bar_backtest():
    result = run_backtest([], TrendPEConfig("x"))
    assert result.periods == 0
    assert result.strategy["cumulative_return"] == 0.0
    assert result.equity_curve == [
        {"period": "", "strategy": 1.0, "buy_and_hold": 1.0, "signal": 0}
    ]
    one = run_backtest(_bars([10], [100]), TrendPEConfig("x"))
    assert one.strategy["cagr"] is None


def test_result_to_dict_serializable():
    result = run_backtest(_bars([10, 11], [100, 102]), TrendPEConfig("x"))
    d = result_to_dict(result)
    assert d["rule"]["kind"] == "trend+PE_gate_long_only"
    assert len(d["equity_curve"]) == 2
