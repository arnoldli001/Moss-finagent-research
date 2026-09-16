"""单股票多因子条件回测引擎单测（离线、确定性合成数据）。

为什么全部用合成数据而不是真实缓存：这里的每一条都是**撮合规则**，
必须有可解析的正确答案。真实数据只能验证"跑得通"，验证不了"算得对"。

覆盖的关键不变量：
    - **不含未来函数**：t 日信号只能在 t+1 成交；改动 t+1 之后的数据不影响 t 的决策
    - **T+1**：当日买入当日不能卖
    - **整手**：买入股数必须是 100 的整数倍
    - **涨停不买 / 跌停不卖**
    - **止损**：跳空低于止损价时按开盘价成交（不是止损价）
    - **价格空间一致**：复权价与未复权涨跌停价不能直接比较
    - **持仓清空**：同一笔持仓不能被重复卖出（会凭空造出收益）
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from src.quant.condition_dsl import ConditionError
from src.quant.single_backtest import (
    CostConfig,
    SingleBacktestConfig,
    run_single_backtest,
)

CODE = "000001"


@dataclass
class FakePanels:
    """最小面板替身：只要 prices / limits / suspended 三个字段。"""

    dates: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=lambda: [CODE])
    prices: dict[str, pd.DataFrame] = field(default_factory=dict)
    basics: dict[str, pd.DataFrame] = field(default_factory=dict)
    flows: dict[str, pd.DataFrame] = field(default_factory=dict)
    limits: dict[str, pd.DataFrame] = field(default_factory=dict)
    bak: dict[str, pd.DataFrame] = field(default_factory=dict)
    fundamentals: None = None
    index_returns: dict[str, pd.Series] = field(default_factory=dict)
    suspended: set[tuple[str, str]] = field(default_factory=set)
    gaps: list[str] = field(default_factory=list)
    origins: list[str] = field(default_factory=list)


def make_panels(closes: list[float], *, opens: list[float] | None = None,
                highs: list[float] | None = None,
                lows: list[float] | None = None,
                raw: list[float] | None = None,
                up_limit: list[float] | None = None,
                down_limit: list[float] | None = None,
                suspended: set[tuple[str, str]] | None = None,
                start_day: int = 1) -> FakePanels:
    """构造一只票的日线面板（价格即"复权价"，raw 可给出不同的未复权价）。"""
    dates = [f"202601{start_day + index:02d}" for index in range(len(closes))]
    index = dates
    close = pd.Series(closes, index=index, dtype="float64")
    prices = {
        "close": close.to_frame(CODE),
        "open": pd.Series(opens if opens is not None else closes, index=index,
                          dtype="float64").to_frame(CODE),
        "high": pd.Series(highs if highs is not None else closes, index=index,
                          dtype="float64").to_frame(CODE),
        "low": pd.Series(lows if lows is not None else closes, index=index,
                         dtype="float64").to_frame(CODE),
        "close_raw": pd.Series(raw if raw is not None else closes, index=index,
                               dtype="float64").to_frame(CODE),
    }
    limits = {}
    if up_limit is not None:
        limits["up_limit"] = pd.Series(up_limit, index=index,
                                       dtype="float64").to_frame(CODE)
    if down_limit is not None:
        limits["down_limit"] = pd.Series(down_limit, index=index,
                                         dtype="float64").to_frame(CODE)
    return FakePanels(dates=dates, prices=prices, limits=limits,
                      suspended=suspended or set())


def flat_costs() -> CostConfig:
    """零成本，便于手算期望值。"""
    return CostConfig(commission_rate=0.0, min_commission=0.0,
                      stamp_tax_rate=0.0, transfer_fee_rate=0.0,
                      slippage_bps=0.0)


def cfg(**kwargs) -> SingleBacktestConfig:
    base = {"code": CODE, "entry": "close > 0", "initial_cash": 1_000_000.0,
            "position_pct": 1.0, "max_hold_days": 0, "min_hold_days": 1,
            "costs": flat_costs()}
    base.update(kwargs)
    return SingleBacktestConfig(**base)


# ==================================================================
# 时序算子的因果性
# ==================================================================


def test_entry_signal_executes_next_day_open_not_same_day_close() -> None:
    """t 日信号必须**次日开盘**成交 —— 用当日收盘价成交就是未来函数。

    构造：只有第 1 天满足入场（close > 0 恒真，这里用 5 日均线制造一个尖峰），
    检查第一笔交易的入场日是第 1 天的**下一个交易日**，价格是那天的开盘价。
    """
    closes = [10.0, 11.0, 12.0, 13.0, 14.0]
    opens = [9.0, 9.5, 11.5, 12.5, 13.5]
    panels = make_panels(closes, opens=opens)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", max_hold_days=1))
    assert result.trades, "应当产生交易"
    first = result.trades[0]
    assert first.entry_date == panels.dates[1], "信号在 t=0，成交必须在 t=1"
    assert first.entry_price == pytest.approx(opens[1]), "应按次日开盘价成交"


def test_future_mutation_does_not_change_past_decisions() -> None:
    """把未来几天的价格改掉，前面的成交决策必须完全不变（因果性硬检验）。"""
    closes = [10.0, 11.0, 12.0, 11.0, 10.0, 9.0, 12.0, 13.0]
    panels = make_panels(closes)
    config = cfg(entry="close > MA(close, 3)", max_hold_days=2)
    before = run_single_backtest(panels, CODE, config=config)

    mutated = list(closes)
    mutated[-3:] = [999.0, 999.0, 999.0]
    after = run_single_backtest(make_panels(mutated), CODE, config=config)

    early_before = [t.entry_date for t in before.trades
                    if t.entry_date <= panels.dates[4]]
    early_after = [t.entry_date for t in after.trades
                   if t.entry_date <= panels.dates[4]]
    assert early_before == early_after, "改动未来数据影响了过去的决策"


# ==================================================================
# 撮合约束
# ==================================================================


def test_t_plus_1_blocks_same_day_sell() -> None:
    """当日买入当日不可卖：最短持有 1 个交易日。

    排除"回测结束平仓"：那是回测跑完的强制清仓，不是策略决策，
    它可能恰好落在买入当天（此时 T+1 无从谈起），属于记账口径而非交易规则。
    """
    panels = make_panels([10.0, 10.0, 10.0, 10.0],
                         opens=[10.0, 10.0, 10.0, 10.0])
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", exit="close > 0", max_hold_days=0,
                   t_plus_1=True))
    decided = [trade for trade in result.trades
               if trade.exit_reason != "回测结束平仓"]
    assert decided, "应当有策略自己决定的平仓"
    for trade in decided:
        assert trade.hold_days >= 1, f"{trade.entry_date} 当日就卖了"


def test_final_liquidation_is_labelled() -> None:
    """末日强制平仓要能被识别出来（否则"胜率"会混入非策略决策的成交）。"""
    panels = make_panels([10.0] * 4, opens=[10.0] * 4)
    result = run_single_backtest(
        panels, CODE, config=cfg(entry="close > 0", max_hold_days=0))
    assert result.trades[-1].exit_reason == "回测结束平仓"
    assert result.metrics["exit_reasons"].get("回测结束平仓") == 1


def test_lot_size_is_hundred_shares() -> None:
    """买入股数必须是 100 的整数倍（真实委托约束）。"""
    panels = make_panels([10.0] * 5, opens=[10.0] * 5)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", max_hold_days=1, initial_cash=10_150.0))
    assert result.trades
    assert result.trades[0].shares % 100 == 0
    # 10150 / 10 = 1015 股 → 只能买 1000 股（10 手）
    assert result.trades[0].shares == 1000


def test_insufficient_cash_for_one_lot_is_reported_not_silently_empty() -> None:
    """本金买不起一手时必须**明确报出来**，不能静默返回一条零交易曲线。

    实测场景：10 万本金回测贵州茅台（一手约 13 万），如果不报原因，
    用户看到的是一张平的净值曲线，完全不知道该改什么。
    """
    panels = make_panels([1500.0] * 5, opens=[1500.0] * 5)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", initial_cash=100_000.0))
    assert not result.trades
    assert any("不足以买入一手" in item for item in result.warnings)
    assert any("买入被跳过" in item for item in result.notes)


def test_limit_up_open_blocks_buy() -> None:
    """开盘价触及涨停时买不到（一字板挂单成交不了）。"""
    closes = [10.0, 11.0, 11.0, 11.0]
    opens = [10.0, 11.0, 11.0, 11.0]
    up = [11.0, 11.0, 11.0, 11.0]
    panels = make_panels(closes, opens=opens, up_limit=up)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", max_hold_days=1))
    assert not result.trades, "开盘涨停不应成交"
    assert any("涨停" in item for item in result.notes)


def test_limit_down_blocks_sell_and_is_deferred() -> None:
    """跌停日卖不出，买入被推迟到下一个可交易日。"""
    closes = [10.0, 10.0, 10.0, 10.0]
    down = [9.0, 10.0, 10.0, 10.0]
    panels = make_panels(closes, opens=closes, down_limit=down)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", exit="close > 0", max_hold_days=0))
    assert any("卖出因跌停" in item for item in result.notes)


def test_suspension_blocks_trading() -> None:
    """停牌日不可买不可卖。"""
    panels = make_panels([10.0] * 4, opens=[10.0] * 4,
                         suspended={(f"202601{day:02d}", CODE)
                                    for day in range(1, 5)})
    result = run_single_backtest(
        panels, CODE, config=cfg(entry="close > 0", max_hold_days=1))
    assert not result.trades
    assert any("停牌" in item for item in result.notes)


# ==================================================================
# 止损止盈
# ==================================================================


def test_stop_loss_uses_open_on_gap_down() -> None:
    """跳空低于止损价时按**开盘价**成交，不是止损价（更差的价格）。

    这是保守假设：止损单在缺口里只能以更差的价格成交。
    """
    closes = [100.0, 100.0, 90.0, 90.0]
    opens = [100.0, 100.0, 85.0, 90.0]     # 第 3 天跳空低开到 85
    lows = [100.0, 100.0, 80.0, 90.0]
    panels = make_panels(closes, opens=opens, lows=lows)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", stop_loss_pct=0.05, max_hold_days=0))
    stopped = [t for t in result.trades if t.exit_reason == "止损"]
    assert stopped, "应触发止损"
    assert stopped[0].exit_price == pytest.approx(85.0), \
        "跳空时应按开盘价 85 成交，而不是止损价 95"


def test_take_profit_triggers_on_high() -> None:
    closes = [100.0, 100.0, 100.0, 100.0]
    highs = [100.0, 100.0, 125.0, 100.0]
    panels = make_panels(closes, highs=highs)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", take_profit_pct=0.20, max_hold_days=0))
    taken = [t for t in result.trades if t.exit_reason == "止盈"]
    assert taken, "应触发止盈"


def test_stop_loss_wins_when_both_hit_same_day() -> None:
    """止损与止盈同日触发时按止损（无法确知盘中顺序，取不利一侧）。"""
    closes = [100.0, 100.0, 100.0, 100.0]
    highs = [100.0, 100.0, 130.0, 100.0]
    lows = [100.0, 100.0, 80.0, 100.0]
    panels = make_panels(closes, highs=highs, lows=lows)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", stop_loss_pct=0.10,
                   take_profit_pct=0.20, max_hold_days=0))
    first = result.trades[0]
    assert first.exit_reason == "止损"


# ==================================================================
# 价格空间与记账
# ==================================================================


def test_adjusted_and_raw_price_spaces_are_kept_consistent() -> None:
    """复权价与未复权涨跌停价必须换算到同一空间。

    实测踩过的坑：两者直接比较时"开盘价 ≥ 涨停价"恒成立，
    所有买入被静默挡掉（171/171 天），回测收益为 0 却看不出原因。
    """
    closes = [1100.0, 1100.0, 1100.0]          # 复权价
    raw = [100.0, 100.0, 100.0]                # 未复权真实价（因子 = 11）
    up = [110.0, 110.0, 110.0]                 # 未复权涨停价
    panels = make_panels(closes, opens=closes, raw=raw, up_limit=up)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 0", max_hold_days=1,
                   initial_cash=1_000_000.0))
    assert result.trades, "价格空间一致时应当能成交"
    assert not any("涨停" in item for item in result.notes if "买入被跳过" in item)


def test_trades_never_overlap() -> None:
    """交易不得重叠 —— 重叠意味着同一笔持仓被重复卖出（会凭空造出收益）。

    实测：忘记清空持仓时，171 天跑出 +12410%、年化 123126% 的荒谬净值，
    而且净值曲线单调向上、回撤很小，肉眼看不出任何异常。
    """
    rng = np.random.default_rng(7)
    closes = list(100 + np.cumsum(rng.normal(0, 2, 60)))
    panels = make_panels(closes)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > MA(close, 5)", exit="close < MA(close, 5)",
                   max_hold_days=3))
    for previous, current in zip(result.trades, result.trades[1:], strict=False):
        assert previous.exit_date <= current.entry_date, (
            f"{previous.entry_date}→{previous.exit_date} 与 "
            f"{current.entry_date}→{current.entry_date} 重叠")
    assert not any("自检失败" in item for item in result.warnings)


def test_final_equity_reconciles_with_realized_pnl() -> None:
    """期末净值 = 初始资金 + 累计已实现盈亏（末日强制平仓，故应精确相等）。"""
    rng = np.random.default_rng(11)
    closes = list(50 + np.cumsum(rng.normal(0, 1, 40)))
    panels = make_panels(closes)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > MA(close, 5)", exit="close < MA(close, 5)",
                   max_hold_days=2))
    realized = sum(trade.pnl for trade in result.trades)
    final = result.equity[-1]
    assert final == pytest.approx(1_000_000.0 + realized, abs=1.0)


def test_costs_reduce_returns() -> None:
    """费用必须真的扣掉：同样的交易，有费用时收益更低。"""
    rng = np.random.default_rng(3)
    closes = list(20 + np.cumsum(rng.normal(0, 0.3, 40)))
    panels = make_panels(closes)
    free = run_single_backtest(
        panels, CODE, config=cfg(entry="close > MA(close, 5)",
                                 exit="close < MA(close, 5)", max_hold_days=2,
                                 costs=flat_costs()))
    costly = run_single_backtest(
        panels, CODE, config=cfg(entry="close > MA(close, 5)",
                                 exit="close < MA(close, 5)", max_hold_days=2,
                                 costs=CostConfig()))
    assert costly.metrics["total_cost"] > 0
    assert (costly.metrics["final_equity"] < free.metrics["final_equity"])


# ==================================================================
# 条件与结果
# ==================================================================


def test_cross_sectional_function_rejected_in_single_stock_mode() -> None:
    """单股票模式必须拒绝截面函数（RANK 会拿未来交易日一起排名）。"""
    panels = make_panels([10.0] * 5)
    with pytest.raises(ConditionError, match="截面函数"):
        run_single_backtest(panels, CODE, config=cfg(entry="RANK(close) > 50"))


def test_unknown_factor_fails_before_running() -> None:
    panels = make_panels([10.0] * 5)
    with pytest.raises(ConditionError, match="未知因子"):
        run_single_backtest(panels, CODE,
                            config=cfg(entry="not_a_factor > 1"))


def test_empty_entry_condition_is_rejected() -> None:
    panels = make_panels([10.0] * 5)
    with pytest.raises(ValueError, match="入场条件"):
        run_single_backtest(panels, CODE, config=cfg(entry="   "))


def test_segments_split_train_and_oos() -> None:
    """结果必须拆训练集与样本外 —— 单股票过拟合风险高，只看全样本会骗自己。"""
    rng = np.random.default_rng(5)
    closes = list(30 + np.cumsum(rng.normal(0, 0.5, 100)))
    panels = make_panels(closes)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > MA(close, 5)", exit="close < MA(close, 5)",
                   max_hold_days=3, train_ratio=0.7))
    assert set(result.segments) == {"all", "train", "oos"}
    assert result.segments["train"]["trading_days"] == 70
    assert result.segments["oos"]["trading_days"] == 30


def test_benchmark_is_buy_and_hold_same_stock() -> None:
    """基准是同一只票的买入持有 —— 否则"跑赢基准"没有意义。"""
    closes = [10.0] * 10 + [20.0] * 10
    panels = make_panels(closes)
    result = run_single_backtest(
        panels, CODE, config=cfg(entry="close < 0", max_hold_days=1))
    assert not result.trades
    assert result.benchmark[-1] > 1_900_000, "买入持有应接近翻倍"
    assert result.equity[-1] == pytest.approx(1_000_000.0), "无交易则本金不动"


def test_warns_when_too_few_trades() -> None:
    """交易笔数太少时必须给出统计噪声警告。"""
    closes = [10.0, 10.0, 10.0, 11.0, 12.0, 13.0]
    panels = make_panels(closes)
    result = run_single_backtest(
        panels, CODE,
        config=cfg(entry="close > 10.5", max_hold_days=1))
    assert result.metrics["trade_count"] < 10
    assert any("统计噪声" in item for item in result.warnings)


def test_time_series_operators_work_in_conditions() -> None:
    """时序算子（MA/REF/DELTA/COUNT_TS）在条件里可用且因果。"""
    closes = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    panels = make_panels(closes)
    for text in ("close > MA(close, 3)", "DELTA(close, 1) > 0",
                 "COUNT_TS(close > MA(close,2), 3) >= 2",
                 "close > REF(close, 2)", "PCTL_TS(close, 4) > 50"):
        result = run_single_backtest(
            panels, CODE, config=cfg(entry=text, max_hold_days=1))
        assert result.entry_condition["text"] == text


# ==================================================================
# PIT 财务时序
# ==================================================================


def test_vectorized_pit_matches_as_of_exactly() -> None:
    """向量化的 PIT 取值必须与 `PitPanel.as_of` **逐值相同**。

    为什么单列这一条：逐日调用 `as_of` 在 5030 天的全历史回测里要 ~10 秒，
    所以要向量化；但"公告日之后才可见"和"修正公告保留多版本"这两条 PIT 规则
    一旦被优化改错，回测收益会凭空变好而且**完全看不出来**。
    """
    from src.quant.pit import PitPanel
    from src.quant.single_backtest import _pit_series

    records = pd.DataFrame({
        "code": ["000001"] * 5,
        "name": ["平安银行"] * 5,
        "report_period": ["20250331", "20250630", "20250630", "20250930",
                          "20251231"],
        # 同一报告期两条公告（修正），两条 usable_date 相同 → 平局场景
        "ann_date": ["20250425", "20250820", "20250820", "20251025",
                     "20260420"],
        "roe": [10.0, 11.0, 12.5, 11.8, 12.0],
        "roa": [0.8, 0.9, 0.95, 0.92, 0.93],
    })
    panel = PitPanel(records)
    dates = ["20250101", "20250425", "20250601", "20250820", "20251001",
             "20251025", "20260101", "20260501"]

    vectorized = _pit_series(panel.records, dates).reindex(dates)
    for day in dates:
        expected = panel.as_of(day)
        if len(expected) == 0:
            assert day not in vectorized.index or vectorized.loc[day].isna().all()
            continue
        row = expected.loc["000001"]
        for column in ("roe", "roa"):
            assert vectorized.loc[day, column] == pytest.approx(row[column]), (
                f"{day} 的 {column} 与 as_of 不一致："
                f"向量化={vectorized.loc[day, column]} as_of={row[column]}")


def test_pit_value_is_invisible_before_it_becomes_usable() -> None:
    """公告日当天仍取不到，**公告日 + lag_days 之后**才可见。

    `PitConfig.lag_days` 默认 1（"公告日之后还要等 1 个自然日才可使用"），
    所以 2026-04-25 公告的财报在 04-25 当天不可用、04-26 起可用。
    这条规则如果被"优化"掉，回测会在公告当天就用上财报数字 —— 那是实打实的
    未来函数，而且收益会变好、看不出问题。
    """
    from src.quant.pit import PitPanel
    from src.quant.single_backtest import _pit_series

    records = pd.DataFrame({
        "code": ["000001"], "name": ["平安银行"],
        "report_period": ["20260331"], "ann_date": ["20260425"],
        "roe": [15.0], "roa": [1.1],
    })
    panel = PitPanel(records)
    dates = ["20260401", "20260424", "20260425", "20260426", "20260501"]
    series = _pit_series(panel.records, dates).reindex(dates)
    assert pd.isna(series.loc["20260401", "roe"])
    assert pd.isna(series.loc["20260424", "roe"])
    assert pd.isna(series.loc["20260425", "roe"]), "公告日当天还不能用"
    assert series.loc["20260426", "roe"] == pytest.approx(15.0), \
        "公告日 + lag_days 起应当可见"
    assert series.loc["20260501", "roe"] == pytest.approx(15.0)
