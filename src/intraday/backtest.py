"""阈值回测：验证「动手线±30 / 提示线±20」是否真能带来正收益。

用户需求明确要求「先做回测，再实盘做T」。本模块回答三个问题：
  1. 总分突破 ±20 / ±30 之后，未来 N 根5分钟bar的方向性收益是多少（命中率/平均收益）？
  2. 与「随机/全程买入持有」的基准相比是否有超额？
  3. 按「回踩信号买入、冲高信号卖出」的机械做T规则跑一遍，累计收益与最大回撤如何？

方法学与项目 backtest 子系统保持一致（无未来函数、纯本地规则、含交易成本）：
  - 每根bar的总分只使用截至该bar的数据（features.replay_totals 全因果指标）；
  - 前瞻收益用 t+N 根bar后的收盘价（严格在信号之后）；
  - 做T为T+0回转交易（A股个股实为T+1，回测按「底仓做T」口径：
    先卖出后买回或先买后卖均在底仓上完成，故不额外计T+1限制），
    成本按单边佣金+滑点计，卖出含印花税。

已知局限（必须在结论中披露）：
  - 5分钟历史深度受数据源限制（腾讯接口约320根≈6-7个交易日；
    QMT在线时可用 download_history_data 回补更长历史）；
  - 板块情绪/消息面两个因子（合计15权重）无历史序列，回测只覆盖技术面85权重，
    按比例归一至100以保持±20/±30阈值刻度可比。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from src.intraday import indicators as ind
from src.intraday.config import FACTOR_LABELS, IntradayConfig
from src.intraday.features import (
    build_intraday_features,
    build_oscillator_features,
    replay_totals,
    resample_bars,
)
from src.intraday.models import (
    BacktestResult,
    BacktestTrade,
    ThresholdStat,
)

logger = logging.getLogger(__name__)

# 技术面因子（回测可覆盖的部分）；情绪/消息面无历史序列，回测中按比例归一
TECHNICAL_FACTORS = ("box", "vwap", "boll", "macd", "kdj_rsi")

DISCLAIMER = (
    "⚠️ 历史回测不代表未来收益，阈值有效性随市场状态漂移，结果仅用于机制验证，"
    "不构成投资建议；实盘做T风险极高，请自行承担全部交易风险。"
)


def _technical_weights(config: IntradayConfig) -> dict[str, float]:
    """把技术面因子权重归一至合计100（保持±20/±30阈值刻度可比）。"""
    weights = config.weights.as_dict()
    technical = {k: weights[k] for k in TECHNICAL_FACTORS}
    total = sum(technical.values())
    if total <= 0:
        return {k: 0.0 for k in weights}
    scaled = {k: v * 100.0 / total for k, v in technical.items()}
    return {**{k: 0.0 for k in weights}, **scaled}


def _forward_returns(frame: pd.DataFrame, horizon: int) -> pd.Series:
    """未来 horizon 根bar的收益率（%），严格使用 t+horizon 的收盘价。"""
    close = frame["close"].astype("float64")
    return (close.shift(-horizon) / close - 1.0) * 100.0


def compute_threshold_stats(
    frame: pd.DataFrame, *, threshold: float, horizon: int,
    direction: str = "long",
) -> ThresholdStat:
    """单阈值档位的信号后验统计。

    direction="long"：总分 ≥ threshold 视为看多信号（做T回踩）；
    direction="short"：总分 ≤ -threshold 视为看空信号（做T冲高）。
    命中判定：看多信号前瞻收益 > 0；看空信号前瞻收益 < 0。
    """
    if frame is None or len(frame) == 0 or "total" not in frame.columns:
        return ThresholdStat(threshold=threshold, direction=direction, signals=0)
    working = frame.copy()
    working["fwd"] = _forward_returns(working, horizon)
    mask = working["total"] >= threshold if direction == "long" else (
        working["total"] <= -threshold)
    hits = working[mask & working["fwd"].notna()]
    if hits.empty:
        return ThresholdStat(threshold=threshold, direction=direction, signals=0)
    returns = hits["fwd"].astype("float64")
    hit_rate = float((returns > 0).mean()) if direction == "long" else float(
        (returns < 0).mean())
    return ThresholdStat(
        threshold=threshold, direction=direction, signals=int(len(hits)),
        hit_rate=round(hit_rate, 4),
        avg_forward_return_pct=round(float(returns.mean()), 4),
        median_forward_return_pct=round(float(returns.median()), 4),
    )


def run_threshold_backtest(
    *,
    code: str,
    name: str,
    bars_5m: pd.DataFrame,
    bars_daily: pd.DataFrame,
    config: IntradayConfig,
    horizon: int = 6,
    initial_capital: float = 1_000_000.0,
) -> BacktestResult:
    """在历史5分钟序列上复现打分并统计阈值有效性 + 机械做T收益。"""
    gaps: list[str] = []
    if bars_5m is None or len(bars_5m) < 60:
        return BacktestResult(
            available=False, code=code, name=name, gaps=[
                "5分钟历史样本不足（<60根），无法回测"],
            disclaimer=DISCLAIMER)

    features = build_intraday_features(bars_5m, config)
    oscillator = build_oscillator_features(resample_bars(bars_5m, 30), config)

    # 箱体用**日线序列逐日重算**（只用当日及之前的日线，无未来函数）
    if bars_daily is not None and len(bars_daily) >= 5:
        daily_frame = bars_daily.copy()
    else:
        daily_frame = _daily_from_intraday(bars_5m)
        if len(daily_frame) >= 5:
            gaps.append("日线上下文缺失，箱体改由5分钟序列合成的日线近似计算")

    weights = _technical_weights(config)
    days = sorted(features["day"].unique())
    replay_parts: list[pd.DataFrame] = []
    for day in days:
        day_features = features[features["day"] == day].reset_index(drop=True)
        if day_features.empty:
            continue
        box_position, daily_macd_score, daily_osc_score = _day_context(
            day, day_features, daily_frame, config)
        part = replay_totals(
            intraday_features=day_features,
            oscillator_features=oscillator,
            score_constants={"box": box_position, "sentiment": None, "news": None},
            daily_macd_score=daily_macd_score,
            daily_osc_score=daily_osc_score,
            weights=weights,
            config=config,
        )
        replay_parts.append(part)
    if not replay_parts:
        return BacktestResult(
            available=False, code=code, name=name,
            gaps=["逐日重放无有效结果"], disclaimer=DISCLAIMER)
    replay = pd.concat(replay_parts, ignore_index=True)

    action = config.thresholds.action
    hint = config.thresholds.hint
    action_stat = compute_threshold_stats(
        replay, threshold=action, horizon=horizon, direction="long")
    hint_stat = compute_threshold_stats(
        replay, threshold=hint, horizon=horizon, direction="long")
    short_action = compute_threshold_stats(
        replay, threshold=action, horizon=horizon, direction="short")
    short_hint = compute_threshold_stats(
        replay, threshold=hint, horizon=horizon, direction="short")
    # 冲高侧两档都并入 by_threshold 扫描结果，供前端对比展示
    high_side = [short_action, short_hint]

    # 基准：全样本前瞻收益（相当于随机时点做T的期望）
    baseline = _baseline_stat(replay, horizon)
    for stat, base in ((action_stat, baseline), (hint_stat, baseline),
                       (short_action, baseline), (short_hint, baseline)):
        if stat.avg_forward_return_pct is not None and base.avg_forward_return_pct is not None:
            stat.excess_vs_baseline_pct = round(
                stat.avg_forward_return_pct - base.avg_forward_return_pct, 4)

    by_threshold = _scan_thresholds(replay, horizon)

    trades, total_return, win_rate, max_dd = _simulate_t_trades(
        replay, config=config, initial_capital=initial_capital)

    verdict = _verdict(action_stat, hint_stat, short_action, baseline, config)
    if action_stat.signals == 0 and hint_stat.signals == 0:
        gaps.append(
            f"样本期内总分从未触及 ±{hint:g}，无法评估阈值有效性"
            "（可增大历史窗口或放宽阈值再看）")
    gaps.append(
        "回测仅覆盖技术面因子（箱体/VWAP/布林/MACD/KDJ-RSI，"
        "权重已归一至100）；板块情绪与消息面因子无历史序列，未纳入")

    return BacktestResult(
        available=True, code=code, name=name,
        days=len(days), bars=int(len(replay)),
        range_start=str(replay["ts"].iloc[0]),
        range_end=str(replay["ts"].iloc[-1]),
        horizon_bars=horizon,
        trades=trades[:200],
        action_line=action_stat, hint_line=hint_stat, baseline=baseline,
        by_threshold=[*by_threshold, *high_side],
        total_return_pct=total_return, win_rate=win_rate,
        max_drawdown_pct=max_dd, gaps=gaps, verdict=verdict,
        disclaimer=DISCLAIMER,
    )


def _daily_from_intraday(bars_5m: pd.DataFrame) -> pd.DataFrame:
    """5分钟序列 → 日线（无独立日线源时的近似，口径已在 gaps 中披露）。"""
    frame = bars_5m.copy()
    frame["day"] = frame["ts"].str.slice(0, 10)
    grouped = frame.groupby("day", sort=True)
    return pd.DataFrame({
        "ts": [day for day, _ in grouped],
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum(),
        "amount": grouped["amount"].sum(),
    }).reset_index(drop=True)


def _day_context(
    day: str, day_features: pd.DataFrame, daily_frame: pd.DataFrame,
    config: IntradayConfig,
) -> tuple[float | None, float | None, float | None]:
    """某交易日的日线级分量（箱体位置/日线MACD/日线KDJ-RSI），全部因果。"""
    history = daily_frame[daily_frame["ts"].astype(str).str.slice(0, 10) < day]
    if len(history) < 5:
        return None, None, None
    lookback = config.factors.box.lookback_days
    window = history.tail(lookback)
    box_high = float(window["high"].max())
    box_low = float(window["low"].min())
    # 当日开盘价作为「当日价格」的代理（盘中位置会随时间变化，回测用开盘更保守）
    day_open = float(day_features["open"].iloc[0])
    position = ind.box_position(day_open, box_high, box_low)

    macd_frame = ind.macd(history, fast=config.factors.macd.fast,
                          slow=config.factors.macd.slow,
                          signal=config.factors.macd.signal)
    macd_score = None
    diff = ind.last_value(macd_frame["dif"])
    dea = ind.last_value(macd_frame["dea"])
    close = ind.last_value(history["close"])
    if diff is not None and dea is not None and close:
        state, _ = ind.macd_cross_state(macd_frame)
        from src.intraday.factors import cross_bonus_for, macd_score_from_gap
        macd_score = macd_score_from_gap(
            (diff - dea) / close, config.factors.macd.gap_scale_pct,
            cross_bonus_for(state, config.factors.macd.cross_bonus))

    kdj_frame = ind.kdj(history, n=config.factors.kdj_rsi.kdj_n)
    rsi_series = ind.rsi(history, n=config.factors.kdj_rsi.rsi_n)
    from src.intraday.factors import osc_score_from_jr
    osc_score = osc_score_from_jr(
        ind.last_value(kdj_frame["j"]), ind.last_value(rsi_series),
        config.factors.kdj_rsi.rsi_scale)
    return position, macd_score, osc_score


def _baseline_stat(frame: pd.DataFrame, horizon: int) -> ThresholdStat:
    """全样本前瞻收益基准（相当于不看信号、随机时点做T的期望）。"""
    working = frame.copy()
    working["fwd"] = _forward_returns(working, horizon)
    returns = working["fwd"].dropna()
    if returns.empty:
        return ThresholdStat(threshold=0.0, direction="all", signals=0)
    return ThresholdStat(
        threshold=0.0, direction="all", signals=int(len(returns)),
        hit_rate=round(float((returns > 0).mean()), 4),
        avg_forward_return_pct=round(float(returns.mean()), 4),
        median_forward_return_pct=round(float(returns.median()), 4),
    )


def _scan_thresholds(frame: pd.DataFrame, horizon: int) -> list[ThresholdStat]:
    """阈值敏感性扫描（网格搜索更优阈值，供调参参考）。"""
    stats: list[ThresholdStat] = []
    for threshold in (10, 15, 20, 25, 30, 35, 40, 50):
        stat = compute_threshold_stats(
            frame, threshold=float(threshold), horizon=horizon, direction="long")
        stats.append(stat)
    return stats


def _simulate_t_trades(
    replay: pd.DataFrame, *, config: IntradayConfig, initial_capital: float,
) -> tuple[list[BacktestTrade], float | None, float | None, float | None]:
    """机械做T模拟：总分≥动手线且价格≤回踩线→买入；总分≤-动手线且价格≥冲高线→卖出。

    单标的、单笔满仓底仓做T（日内回转），含佣金/印花税/滑点。
    止损位按「建仓价下方 stop_loss_pct%」硬执行（与实时引擎同一硬约束口径）。
    """
    if replay is None or len(replay) == 0:
        return [], None, None, None

    commission = 0.00025
    stamp = 0.0005
    slippage = 0.0005
    action = config.thresholds.action
    stop_pct = config.levels.stop_loss_pct / 100.0

    trades: list[BacktestTrade] = []
    equity = initial_capital
    peak = equity
    max_dd = 0.0
    position: dict[str, Any] | None = None

    # 逐日重算回踩档位（箱体下沿），保证与实时档位口径一致
    replay = replay.copy()
    replay["day"] = replay["ts"].str.slice(0, 10)
    day_low: dict[str, float] = {}
    day_high: dict[str, float] = {}
    for day, group in replay.groupby("day", sort=True):
        closes = group["close"].astype("float64")
        day_low[day] = float(closes.min())
        day_high[day] = float(closes.max())

    for row in replay.itertuples(index=False):
        day = str(row.day)
        price = float(row.close)
        total = float(row.total)
        low, high = day_low.get(day), day_high.get(day)
        if low is None or high is None:
            continue

        # 1) 跨日即平仓：当日未触发冲高/止损 → 以**上一交易日最后一根bar**的
        #    价格与时间平掉，绝不隔夜（做T为日内回转，隔夜属于波段而非做T）。
        if position is not None and day != position["day"]:
            trades.append(_close_position(
                position, float(position["last_price"]), "day_close",
                str(position["last_ts"]), commission, stamp, slippage))
            equity *= (1.0 + trades[-1].return_pct / 100.0)
            position = None
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)

        # 2) 空仓 → 找回踩入场点
        if position is None:
            stop_price = low * (1.0 - stop_pct)
            if total >= action and price <= low * (
                    1.0 + config.levels.touch_band_pct / 100.0):
                position = {"entry_ts": str(row.ts), "entry": price,
                            "score": total, "stop": stop_price, "day": day,
                            "strength": "solid", "last_price": price,
                            "last_ts": str(row.ts)}
            continue

        # 3) 持仓中：止损优先（硬约束），其次冲高
        exit_reason = None
        if price <= position["stop"]:
            exit_reason = "stop_loss"
        elif total <= -action and price >= high * (
                1.0 - config.levels.touch_band_pct / 100.0):
            exit_reason = "high_sell"
        if exit_reason is None:
            position["last_price"] = price
            position["last_ts"] = str(row.ts)
            continue
        trades.append(_close_position(
            position, price, exit_reason, str(row.ts),
            commission, stamp, slippage))
        equity *= (1.0 + trades[-1].return_pct / 100.0)
        position = None
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)

    # 样本末仍持仓 → 以最后一根bar价格平掉（不留悬空仓位）
    if position is not None:
        trades.append(_close_position(
            position, float(position["last_price"]), "sample_end",
            str(position["last_ts"]), commission, stamp, slippage))
        equity *= (1.0 + trades[-1].return_pct / 100.0)

    if not trades:
        return [], None, None, None
    returns = [t.return_pct for t in trades]
    total_return = round((equity / initial_capital - 1.0) * 100, 4)
    win_rate = round(sum(1 for r in returns if r > 0) / len(returns), 4)
    return trades, total_return, win_rate, round(max_dd * 100, 4)


def _close_position(
    position: dict[str, Any], exit_price: float, reason: str, exit_ts: str,
    commission: float, stamp: float, slippage: float,
) -> BacktestTrade:
    """平仓结算（含双边佣金、卖出印花税、双边滑点）。"""
    entry = float(position["entry"])
    gross = (exit_price / entry - 1.0)
    cost = commission * 2 + stamp + slippage * 2
    net = gross - cost
    return BacktestTrade(
        entry_ts=position["entry_ts"], exit_ts=exit_ts,
        direction="long_t", entry_price=round(entry, 3),
        exit_price=round(exit_price, 3), return_pct=round(net * 100, 4),
        exit_reason=reason, total_score_at_entry=float(position["score"]),
        strength=position["strength"])


def _verdict(action_stat: ThresholdStat, hint_stat: ThresholdStat,
             short_action: ThresholdStat, baseline: ThresholdStat,
             config: IntradayConfig) -> str:
    """把统计结果翻译成一句可执行结论。"""
    parts: list[str] = []
    if action_stat.signals == 0:
        parts.append(f"样本期内未出现 |总分|≥{config.thresholds.action:g} 的信号")
    else:
        edge = (
            None if (action_stat.avg_forward_return_pct is None
                     or baseline.avg_forward_return_pct is None)
            else action_stat.avg_forward_return_pct - baseline.avg_forward_return_pct)
        parts.append(
            f"动手线±{config.thresholds.action:g}：{action_stat.signals}次信号，"
            f"命中率{_rate(action_stat.hit_rate)}，"
            f"平均前瞻收益{_pct(action_stat.avg_forward_return_pct)}"
            + (f"，相对基准超额{_pct(edge)}" if edge is not None else "")
            + ("（正超额，阈值方向有效）" if edge is not None and edge > 0
               else "（无正超额，阈值需下调或该标的做T性价比低）"
               if edge is not None else ""))
    if hint_stat.signals > 0:
        parts.append(
            f"提示线±{config.thresholds.hint:g}：{hint_stat.signals}次信号，"
            f"命中率{_rate(hint_stat.hit_rate)}")
    if short_action.signals > 0:
        parts.append(
            f"冲高侧±{config.thresholds.action:g}：{short_action.signals}次信号，"
            f"下跌命中率{_rate(short_action.hit_rate)}")
    parts.append(
        f"基准（全样{baseline.signals}个样本）命中率{_rate(baseline.hit_rate)}、"
        f"平均前瞻{_pct(baseline.avg_forward_return_pct)}")
    return "；".join(parts)


def _rate(value: float | None) -> str:
    """命中率（0~1 的小数）→ 百分比文本。"""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value * 100:.1f}%"


def _pct(value: float | None) -> str:
    """已是百分比单位的数值 → 文本。"""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.2f}%"


def factor_weight_note(config: IntradayConfig) -> str:
    """回测权重口径说明（前端展示）。"""
    weights = _technical_weights(config)
    used = [f"{FACTOR_LABELS[k]}{weights[k]:.1f}" for k in TECHNICAL_FACTORS]
    return "回测权重口径（归一至100）：" + "、".join(used)
