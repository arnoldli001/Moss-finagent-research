"""逐bar特征构建与「历史重放」（分时图三角标记 + 阈值回测共用）。

为什么需要重放：
- 分时图上的三角标记必须与实时打分同源同口径，否则图上画的点和面板里的分数对不上；
- 阈值回测要回答「±20/±30 这两条线到底有没有效」，必须在历史每根bar上复现同一套打分；
- 所有指标均为因果（rolling/ewm/expanding），第 i 根bar的取值只依赖 ≤ i 的数据，
  因此重放结果天然无未来函数（这是回测结论可信的前提）。

周期口径（关键设计取舍）：
  - 5分钟指标（BOLL/MACD/KDJ/RSI）在**连续**分钟序列上计算（跨日累计，含隔夜跳空），
    这是盘中惯例：开盘即拥有完整窗口，不必等到当日第20根bar才有布林带；
  - VWAP 及其 z-score **按交易日重置**（VWAP 的定义就是「当日」成交量加权均价），
    跨日累计会得到一个没有交易含义的价格中枢；
  - 带宽分位取「近 N 根bar（默认240≈5个交易日）」的滚动分位，样本更充分。

分层：
  build_intraday_features  : 5分钟bars → 每bar的 vwap/dev/dev_z/pct_b/bandwidth/bw_pctl/macd
  build_oscillator_features: 30分钟bars → 每bar的 K/D/J/RSI
  replay_totals            : 上述特征 + 当日常量因子（箱体/日线MACD/日线KDJ/情绪/消息）
                             → 每bar总分（与 engine.compose_scorecard 同一批打分核）
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from src.intraday import indicators as ind
from src.intraday.config import IntradayConfig
from src.intraday.factors import (
    blend_timeframes,
    boll_score_from_pct_b,
    box_score_from_position,
    cross_bonus_for,
    macd_score_from_gap,
    news_score_from,
    osc_score_from_jr,
    sentiment_score_from,
)

_MIN_DEV_SAMPLES = 5
_DEFAULT_BW_WINDOW = 240


def _day_of(ts_series: pd.Series) -> pd.Series:
    return ts_series.astype(str).str.slice(0, 10)


def resample_bars(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """把 N 分钟bars聚合成 M 分钟bars（M 为 N 的整数倍时精确对齐交易时段）。

    输入/输出均以**结束时刻**命名；按「当日第几分钟」分桶，跨日自动切分，
    午休不产生空桶，11:30/15:00 边界不越界。
    """
    empty = pd.DataFrame(
        columns=["ts", "open", "high", "low", "close", "volume", "amount"])
    if bars is None or len(bars) == 0:
        return empty
    frame = bars.copy()
    frame["dt"] = pd.to_datetime(frame["ts"])
    frame["day"] = frame["dt"].dt.strftime("%Y-%m-%d")
    minute_of_day = (frame["dt"].dt.hour * 60 + frame["dt"].dt.minute).astype(int)
    # 行情bar以结束时刻命名 → 归属桶 = ((分钟-1)//M)*M（10:00 应落在 [09:30,10:00) 桶）
    frame["bucket"] = ((minute_of_day - 1).clip(lower=0) // minutes) * minutes
    grouped = frame.groupby(["day", "bucket"], sort=True)
    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum(),
        "amount": grouped["amount"].sum() if "amount" in frame.columns else 0.0,
    }).reset_index()
    ends = [ind._bucket_end_label(int(start), minutes)  # noqa: SLF001 同包内复用时段边界逻辑
            for start in out["bucket"]]
    out["ts"] = [
        f"{day} {end // 60:02d}:{end % 60:02d}"
        for day, end in zip(out["day"], ends, strict=True)
    ]
    out = out.drop(columns=["bucket", "day"])
    return out[["ts", "open", "high", "low", "close", "volume", "amount"]]


def _expanding_zscore_by_day(deviation: pd.Series, day_key: pd.Series,
                             min_samples: int = _MIN_DEV_SAMPLES) -> pd.Series:
    """按交易日分组的因果 z-score：(x_i - mean(当日 x_0..i)) / std(当日 x_0..i, ddof=1)。

    与实时口径严格一致：实时打分时「当日至今全部偏离样本」正是当日的 x_0..i。
    """
    out = pd.Series(np.nan, index=deviation.index, dtype="float64")
    for _, idx in deviation.groupby(day_key, sort=False).groups.items():
        series = deviation.loc[idx]
        mean = series.expanding(min_periods=min_samples).mean()
        std = series.expanding(min_periods=min_samples).std(ddof=1)
        out.loc[idx] = (series - mean) / std.replace(0.0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """滚动分位：x_i 在「近 window 根bar」中的百分位（0~1）；样本不足为 NaN。"""
    values = series.astype("float64")
    out = pd.Series(np.nan, index=values.index, dtype="float64")
    array = values.to_numpy()
    for i in range(len(array)):
        start = max(0, i - window + 1)
        chunk = array[start: i + 1]
        chunk = chunk[~np.isnan(chunk)]
        if len(chunk) < max(20, window // 4):
            continue
        current = array[i]
        if np.isnan(current):
            continue
        out.iloc[i] = float((chunk <= current).sum() / len(chunk))
    return out


def _causal_cross_states(diff: pd.Series, lookback: int = 3) -> list[str]:
    """逐bar因果交叉状态（仅使用截至该bar的数据）。"""
    values = diff.astype("float64").to_numpy()
    states: list[str] = []
    for i in range(len(values)):
        state = "above" if values[i] > 0 else "below"
        for offset in range(1, min(lookback, i) + 1):
            prev, cur = values[i - offset], values[i - offset + 1]
            if np.isnan(prev) or np.isnan(cur):
                continue
            if prev <= 0 < cur:
                state = "golden"
                break
            if prev >= 0 > cur:
                state = "dead"
                break
        states.append(state)
    return states


def build_intraday_features(bars: pd.DataFrame,
                            config: IntradayConfig) -> pd.DataFrame:
    """5分钟bars → 逐bar因果特征表。

    输出列：ts/day/open/high/low/close/volume/vwap/dev_pct/dev_z/pct_b/bandwidth/
            boll_upper/boll_mid/boll_lower/bw_pctl/dif/dea/macd_gap/macd_score/macd_state

    逐bar带出布林三条轨是为了**按"当时那一刻"的档位做回放**：图上档位横线画的是
    当前值，拿它去判早盘的bar会把"后来才抬高的止损位"追认成破位（实测 300308
    09:45 那根被错标为止损，而当时止损位其实在价格下方 3%）。
    """
    columns = [
        "ts", "day", "open", "high", "low", "close", "volume", "vwap", "dev_pct",
        "dev_z", "pct_b", "bandwidth", "boll_upper", "boll_mid", "boll_lower",
        "bw_pctl", "dif", "dea", "macd_gap", "macd_score", "macd_state"]
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=columns)
    frame = bars.copy()
    frame["day"] = _day_of(frame["ts"])

    # ---- VWAP 按日重置 ----
    vwap = pd.Series(np.nan, index=frame.index, dtype="float64")
    for _, idx in frame.groupby("day", sort=False).groups.items():
        vwap.loc[idx] = ind.vwap_series(frame.loc[idx])
    frame["vwap"] = vwap
    frame["dev_pct"] = ((frame["close"] - frame["vwap"]) / frame["vwap"]
                        ).replace([np.inf, -np.inf], np.nan) * 100.0
    frame["dev_z"] = _expanding_zscore_by_day(
        (frame["dev_pct"] / 100.0), frame["day"])

    # ---- 连续分钟序列上的 BOLL / MACD ----
    boll_params = config.factors.boll
    boll = ind.bollinger(frame, window=boll_params.window,
                         num_std=boll_params.num_std)
    frame["pct_b"] = boll["pct_b"]
    frame["bandwidth"] = boll["bandwidth"]
    frame["boll_upper"] = boll["upper"]
    frame["boll_mid"] = boll["mid"]
    frame["boll_lower"] = boll["lower"]
    frame["bw_pctl"] = _rolling_percentile(boll["bandwidth"], _DEFAULT_BW_WINDOW)

    macd_params = config.factors.macd
    macd_frame = ind.macd(frame, fast=macd_params.fast,
                          slow=macd_params.slow, signal=macd_params.signal)
    frame["dif"] = macd_frame["dif"]
    frame["dea"] = macd_frame["dea"]
    frame["macd_gap"] = frame["dif"] - frame["dea"]
    states = _causal_cross_states(frame["macd_gap"])
    frame["macd_state"] = states
    scores: list[float] = []
    for gap, close, state in zip(
            frame["macd_gap"], frame["close"], states, strict=True):
        if pd.isna(gap) or not close or float(close) <= 0:
            scores.append(float("nan"))
            continue
        scores.append(macd_score_from_gap(
            float(gap) / float(close), macd_params.gap_scale_pct,
            cross_bonus_for(state, macd_params.cross_bonus)))
    frame["macd_score"] = scores
    return frame[columns]


def build_oscillator_features(bars_30m: pd.DataFrame,
                              config: IntradayConfig) -> pd.DataFrame:
    """30分钟bars → 逐bar因果 K/D/J/RSI/osc_score（连续序列，跨日累计）。"""
    columns = ["ts", "close", "kdj_k", "kdj_d", "kdj_j", "rsi", "osc_score"]
    if bars_30m is None or len(bars_30m) == 0:
        return pd.DataFrame(columns=columns)
    frame = bars_30m.copy()
    params = config.factors.kdj_rsi
    kdj_frame = ind.kdj(frame, n=params.kdj_n)
    frame["kdj_k"] = kdj_frame["k"]
    frame["kdj_d"] = kdj_frame["d"]
    frame["kdj_j"] = kdj_frame["j"]
    frame["rsi"] = ind.rsi(frame, n=params.rsi_n)
    frame["osc_score"] = [
        osc_score_from_jr(
            None if pd.isna(j) else float(j),
            None if pd.isna(r) else float(r),
            params.rsi_scale)
        for j, r in zip(frame["kdj_j"], frame["rsi"], strict=True)
    ]
    frame["osc_score"] = pd.to_numeric(frame["osc_score"], errors="coerce")
    return frame[columns]


def day_level_constants(
    *,
    box_position: float | None,
    breadth: float | None,
    relative_strength_pct: float | None,
    news_llm_score: float | None,
    news_positive: int,
    news_negative: int,
    news_count: int,
    config: IntradayConfig,
    index_change_pct: float | None = None,
    index_volume_ratio: float | None = None,
    board_rank: str | None = None,
    board_change_pct: float | None = None,
    overseas_overnight_pct: float | None = None,
    overseas_intraday_pct: float | None = None,
    overseas_available: bool | None = None,
    chan_zd: float | None = None,
    chan_zg: float | None = None,
    chan_divergence: str | None = None,
    chan_divergence_strength: float = 0.0,
    chan_available: bool | None = None,
    chip_avg_volume: float | None = None,
    chip_available: bool | None = None,
    cycle_temperature: float | None = None,
    cycle_available: bool | None = None,
    character_atr_pct: float | None = None,
    character_trend_efficiency: float | None = None,
    character_available: bool | None = None,
) -> dict[str, float | None]:
    """当日常量因子得分（箱体/情绪/消息面/指数量能/板块排行/海外映射/情绪周期）。

    这些项在日内都是**当日恒定**（不随分钟bar变化），重放与实时打分共用同一批打分核，
    保证分时图上的三角标记与面板分数同源同口径。

    缠论/筹码/股性三因子**只有一部分是常量**（中枢上下沿、近5日均量、日ATR），
    价格相关的那一半必须逐bar算 —— 所以这里除了直接给分，还会把「原料」一并带出去：

        chan_zd / chan_zg / chan_div_signed   → 逐bar用 chan_score_from 重算位置分
        chip_avg_volume                       → 逐bar用累计量算量比
        char_atr_pct / char_trend_efficiency  → 逐bar用 dev_pct 重算股性分

    如果只在实时链路加因子、忘了在 `replay_totals` 里同步，
    分时图上的三角标记就会与实际总分口径不一致（这是本项目最容易踩的坑之一）。
    """
    from src.intraday.factors import (
        board_rank_score_from,
        cycle_score_from,
        index_volume_score_from,
        overseas_score_from,
    )

    box = (
        box_score_from_position(box_position, config.factors.box.exponent)
        if box_position is not None else None
    )
    sentiment = sentiment_score_from(
        breadth, relative_strength_pct, config.factors.sentiment)
    news = (
        news_score_from(news_llm_score, news_positive, news_negative,
                        config.factors.news)
        if (news_count > 0 or news_llm_score is not None) else None
    )
    index_volume = index_volume_score_from(
        index_change_pct, index_volume_ratio, config.factors.index_volume)
    board_rank_score = board_rank_score_from(
        board_rank, board_change_pct, config.factors.board_rank)
    overseas = None
    if overseas_available is not False:
        overseas = overseas_score_from(
            overseas_overnight_pct, overseas_intraday_pct,
            config.factors.overseas)
    cycle = None
    if config.factors.cycle.enabled and cycle_available is not False:
        cycle = cycle_score_from(
            temperature=cycle_temperature,
            neutral=config.factors.cycle.neutral_temperature)
    chan_ok = chan_available is not False and chan_zd is not None and chan_zg is not None
    chan_div_signed = 0.0
    if chan_ok and chan_divergence == "bottom":
        chan_div_signed = float(chan_divergence_strength)
    elif chan_ok and chan_divergence == "top":
        chan_div_signed = -float(chan_divergence_strength)
    return {
        "box": box, "sentiment": sentiment, "news": news,
        "index_volume": index_volume, "board_rank": board_rank_score,
        "overseas": overseas, "cycle": cycle,
        "chan_zd": chan_zd if chan_ok else None,
        "chan_zg": chan_zg if chan_ok else None,
        "chan_div_signed": chan_div_signed if chan_ok else None,
        "chan_available": 1.0 if chan_ok else None,
        "chip_avg_volume": (
            chip_avg_volume if chip_available is not False and chip_avg_volume else None),
        "char_atr_pct": (
            character_atr_pct
            if character_available is not False and character_atr_pct else None),
        "char_trend_efficiency": character_trend_efficiency,
    }



def replay_totals(
    *,
    intraday_features: pd.DataFrame,
    oscillator_features: pd.DataFrame | None,
    score_constants: dict[str, float | None],
    daily_macd_score: float | None,
    daily_osc_score: float | None,
    weights: dict[str, float],
    config: IntradayConfig,
) -> pd.DataFrame:
    """逐bar重放总分（与实时打分同一批打分核）。

    score_constants：当日不变的因子得分（box/sentiment/news/index_volume/board_rank/
    overseas/cycle）与「半常量原料」（缠论中枢上下沿、近5日均量、日ATR/趋势效率）；
    None 表示该因子当日不可用；
    daily_macd_score / daily_osc_score：日线周期分量（当日恒定）；
    价格类因子（vwap/boll/筹码/股性/缠论位置）与分钟周期因子（macd_5m/kdj_rsi_30m）
    由逐bar特征给出。

    30分钟分量按 as-of 对齐：只用「结束时刻 ≤ 当前5分钟bar」的已完成30分钟bar，
    避免把未走完的30分钟bar当成已完成（未来函数）。

    筹码因子的量比同样按 as-of 计算：用**截至当前bar的累计成交量**除以
    「近5日均量 × 当日已交易时间占比」。不能直接用全天量 —— 那会把收盘后才知道的
    信息提前到早盘（未来函数），实测这是回测虚高的常见来源。

    返回列：ts/close/total/available_weight。
    """
    from src.intraday.character import character_score_from
    from src.intraday.factors import chan_score_from, chip_score_from

    if intraday_features is None or len(intraday_features) == 0:
        return pd.DataFrame(columns=["ts", "close", "total", "available_weight"])
    params = config.factors
    boll_params = params.boll
    wi_macd, wd_macd = params.macd.tf_weights.normalized()
    wi_osc, wd_osc = params.kdj_rsi.tf_weights.normalized()

    osc = None
    if oscillator_features is not None and len(oscillator_features):
        osc = oscillator_features.copy()
        osc["dt"] = pd.to_datetime(osc["ts"])
        osc = osc.sort_values("dt").reset_index(drop=True)

    chan_zd = score_constants.get("chan_zd")
    chan_zg = score_constants.get("chan_zg")
    chan_div = score_constants.get("chan_div_signed") or 0.0
    chan_divergence = "bottom" if chan_div > 0 else ("top" if chan_div < 0 else None)
    chip_avg_volume = score_constants.get("chip_avg_volume")
    char_atr_pct = score_constants.get("char_atr_pct")
    char_efficiency = score_constants.get("char_trend_efficiency")

    rows: list[dict[str, Any]] = []
    cumulative_volume = 0.0
    day_high: float | None = None
    day_low: float | None = None
    for record in intraday_features.itertuples(index=False):
        ts = str(record.ts)
        close = float(record.close)
        total = 0.0
        weight = 0.0

        bar_high = _finite(getattr(record, "high", None))
        bar_low = _finite(getattr(record, "low", None))
        if bar_high is not None:
            day_high = bar_high if day_high is None else max(day_high, bar_high)
        if bar_low is not None:
            day_low = bar_low if day_low is None else min(day_low, bar_low)
        bar_volume = _finite(getattr(record, "volume", None))
        if bar_volume is not None and bar_volume > 0:
            cumulative_volume += bar_volume

        box_score = score_constants.get("box")
        if box_score is not None:
            total += box_score * weights["box"]
            weight += weights["box"]

        dev_z = _finite(getattr(record, "dev_z", None))
        if dev_z is not None:
            total += ind.clip(-dev_z / params.vwap.z_scale) * weights["vwap"]
            weight += weights["vwap"]

        pct_b = _finite(getattr(record, "pct_b", None))
        if pct_b is not None:
            bw_pctl = _finite(getattr(record, "bw_pctl", None))
            squeeze = (
                bw_pctl is not None
                and bw_pctl < boll_params.squeeze_percentile
            )
            total += boll_score_from_pct_b(
                pct_b, squeeze, boll_params.squeeze_damp) * weights["boll"]
            weight += weights["boll"]

        macd_i = _finite(getattr(record, "macd_score", None))
        macd_total = blend_timeframes(macd_i, daily_macd_score, wi_macd, wd_macd)
        if macd_total is not None:
            total += macd_total * weights["macd"]
            weight += weights["macd"]

        osc_i = None
        if osc is not None:
            past = osc[osc["dt"] <= pd.to_datetime(ts)]
            if len(past):
                osc_i = _finite(past["osc_score"].iloc[-1])
        osc_total = blend_timeframes(osc_i, daily_osc_score, wi_osc, wd_osc)
        if osc_total is not None:
            total += osc_total * weights["kdj_rsi"]
            weight += weights["kdj_rsi"]

        sentiment = score_constants.get("sentiment")
        if sentiment is not None:
            total += sentiment * weights["sentiment"]
            weight += weights["sentiment"]

        news_const = score_constants.get("news")
        if news_const is not None:
            total += news_const * weights["news"]
            weight += weights["news"]

        # ---- 当日常量的市场环境因子（指数量能/板块排行/海外映射/情绪周期）----
        for key in ("index_volume", "board_rank", "overseas", "cycle"):
            value = score_constants.get(key)
            if value is not None:
                total += value * weights.get(key, 0.0)
                weight += weights.get(key, 0.0)

        # ---- 缠论结构：中枢上下沿是当日常量，位置分随价格逐bar变化 ----
        if chan_zd is not None and chan_zg is not None:
            chan_value = chan_score_from(
                price=close, zd=chan_zd, zg=chan_zg,
                divergence=chan_divergence,
                divergence_strength=abs(chan_div),
                overshoot=params.chan.overshoot,
                divergence_weight=params.chan.divergence_weight)
            if chan_value is not None:
                total += chan_value * weights.get("chan", 0.0)
                weight += weights.get("chan", 0.0)

        # ---- 筹码量能：量比按 as-of 累计量算，位置按截至当前bar的区间算 ----
        if chip_avg_volume and chip_avg_volume > 0:
            elapsed = ind.session_minutes_of(ts)
            ratio_of_day = 1.0 if elapsed is None else max(0.02, min(1.0, elapsed / 240.0))
            volume_ratio = cumulative_volume / (chip_avg_volume * ratio_of_day)
            position = None
            if day_high is not None and day_low is not None and day_high > day_low:
                position = (close - day_low) / (day_high - day_low)
            chip_value = chip_score_from(
                volume_ratio=volume_ratio, position=position,
                volume_ratio_scale=params.chip.volume_ratio_scale)
            if chip_value is not None:
                total += chip_value * weights.get("chip", 0.0)
                weight += weights.get("chip", 0.0)

        # ---- 股性适配：日ATR/趋势效率是常量，偏离随bar变化 ----
        if char_atr_pct and char_atr_pct > 0:
            char_value = character_score_from(
                dev_pct=_finite(getattr(record, "dev_pct", None)),
                atr_pct=char_atr_pct, trend_efficiency=char_efficiency,
                scale=params.character.atr_scale)
            if char_value is not None:
                total += char_value * weights.get("character", 0.0)
                weight += weights.get("character", 0.0)

        rows.append({
            "ts": ts, "close": close, "total": round(total, 2),
            "available_weight": round(weight, 2),
        })
    return pd.DataFrame(rows)



def _finite(value: Any) -> float | None:
    """任意取值 → 有限 float；NaN/None/Inf/非数值 一律 None。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def dominant_period_label(period: str) -> str:
    """周期字符串 → 中文标签（前端展示用）。"""
    return {"1m": "1分钟", "5m": "5分钟", "15m": "15分钟", "30m": "30分钟",
            "60m": "60分钟", "1d": "日线"}.get(period, period)


def safe_float(value: Any) -> float | None:
    """外部源取值 → 有限float（NaN/None/字符串一律 None）。"""
    return _finite(value)
