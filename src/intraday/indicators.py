"""技术指标纯函数库（Pandas/NumPy 实现，无 TA-Lib 依赖）。

设计原则：
- 全部为纯函数：输入序列 → 输出序列/标量，便于单元测试与回测复用；
- 样本不足时返回 None（而不是抛错或补零），由上层决定降级或标注数据缺口；
- 全部使用「截至当前bar」的滚动窗口，天然无未来函数；
- EMA/DIF/DEA 等递推指标用 ewm(adjust=False) 实现，与通达信/同花顺口径一致。

指标与做T因子的对应关系见 src/intraday/factors.py。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from src.core.trading_session import (
    AFTERNOON_CLOSE,
    AFTERNOON_OPEN,
    MORNING_CLOSE,
    session_offset,
)

# 分钟级周期字符串 → 分钟数（用于分时聚合与周期换算）
PERIOD_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1d": 240,
}


def _clean(values: Any) -> pd.Series:
    """转 float Series 并剔除 NaN/Inf（外部数据源偶发空值）。"""
    s = pd.Series(values, dtype="float64")
    return s.replace([np.inf, -np.inf], np.nan)


def last_value(series: pd.Series | None) -> float | None:
    """取序列最后一个有限值；无有效值返回 None。"""
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return float(value)


def nth_value(series: pd.Series | None, offset: int) -> float | None:
    """取倒数第 offset 个（0=最后一个）有限值；越界返回 None。"""
    if series is None or len(series) <= offset:
        return None
    value = series.iloc[-1 - offset]
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return float(value)


def clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    """把分值裁剪到 [low, high]（因子得分统一落在 [-1,1]）。"""
    return max(low, min(high, value))


# ==================== 均线 / 平滑 ====================

def sma(values: Any, window: int) -> pd.Series:
    """简单移动平均。"""
    return _clean(values).rolling(window=window, min_periods=window).mean()


def ema(values: Any, span: int) -> pd.Series:
    """指数移动平均（adjust=False，与通达信 EMA 口径一致）。"""
    return _clean(values).ewm(span=span, adjust=False).mean()


# ==================== VWAP ====================

def vwap_series(bars: pd.DataFrame) -> pd.Series:
    """当日累计VWAP（成交量加权平均价）。

    约定：bars 必须为「同一交易日」的K线，按时间升序，列含 close/high/low/volume。
    - 有成交额时用 Σ额/Σ量 的真实均价（最准确）；无成交额时退化为典型价 (H+L+C)/3；
    - **量纲自愈**：不同数据源的 volume 有「手」与「股」两种口径、amount 有「元」与
      「亿元」两种口径。此处以「额/量 推导价 vs 收盘价中位数」的比值判定是否存在
      100 倍手数错配，存在则归一，保证 VWAP 与 K 线价格同量纲（否则均价线会飘到
      100 倍价位上，静默给错档位）；
    - 累计口径：前 i 根bar的加权均价，即盘中不断更新的均价线。
    """
    if bars is None or len(bars) == 0:
        return pd.Series(dtype="float64")
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    volume = _clean(bars["volume"]).fillna(0.0).clip(lower=0.0)
    close = _clean(bars["close"])
    amount = (
        _clean(bars["amount"]).fillna(0.0).clip(lower=0.0)
        if "amount" in bars.columns else pd.Series(0.0, index=bars.index)
    )
    # 仅当「量与额都有效」时才用 额/量 推导真实均价；否则回退典型价。
    # （腾讯分钟线实测成交额字段高估约3.6%，其源已置0，正是走到这里回退）
    usable = (volume > 0) & (amount > 0)
    price_used = (amount / volume.where(usable)).where(usable, typical)
    reference = float(close.median()) if len(close.dropna()) else 0.0
    implied_values = price_used[usable]
    implied = float(implied_values.median()) if len(implied_values) else 0.0
    if reference > 0 and implied > reference * 10:
        # 量按「手」计而额按「元」计 → 推导价被放大100倍，统一回落到每股价格
        price_used = price_used / 100.0
    cum_amount = (price_used * volume).cumsum()
    cum_volume = volume.cumsum()
    return (cum_amount / cum_volume.replace(0.0, np.nan)).ffill()


def vwap_deviation(bars: pd.DataFrame) -> pd.Series:
    """价格相对当日VWAP的偏离率序列（(close-vwap)/vwap）。"""
    vwap = vwap_series(bars)
    if len(vwap) == 0:
        return pd.Series(dtype="float64")
    close = _clean(bars["close"])
    return ((close - vwap) / vwap).replace([np.inf, -np.inf], np.nan)


def deviation_zscore(deviations: pd.Series) -> float | None:
    """当前偏离率的当日 z-score（样本<5 时返回 None）。"""
    if deviations is None or len(deviations) < 5:
        return None
    series = deviations.dropna()
    if len(series) < 5:
        return None
    std = float(series.std(ddof=1))
    if not math.isfinite(std) or std <= 1e-12:
        return None
    return float((series.iloc[-1] - series.mean()) / std)


# ==================== 布林带 ====================

def bollinger(bars: pd.DataFrame, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """布林带：返回 upper/mid/lower/pct_b/bandwidth 五列。

    - mid  = MA(close, window)
    - std  = 总体标准差（ddof=0，与通达信 BOLL 口径一致）
    - pct_b = (close - lower) / (upper - lower)，0=下轨，1=上轨
    - bandwidth = (upper - lower) / mid（收口/扩张判据）
    """
    close = _clean(bars["close"]) if bars is not None and len(bars) else pd.Series(dtype="float64")
    mid = close.rolling(window=window, min_periods=window).mean()
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    span = (upper - lower).replace(0.0, np.nan)
    pct_b = ((close - lower) / span).replace([np.inf, -np.inf], np.nan)
    bandwidth = ((upper - lower) / mid.abs().replace(0.0, np.nan)).replace(
        [np.inf, -np.inf], np.nan)
    return pd.DataFrame({
        "upper": upper, "mid": mid, "lower": lower,
        "pct_b": pct_b, "bandwidth": bandwidth,
    })


def bandwidth_percentile(bandwidth: pd.Series) -> float | None:
    """当前带宽在自身历史中的分位（0~1）；用于识别布林收口。"""
    series = bandwidth.dropna() if bandwidth is not None else pd.Series(dtype="float64")
    if len(series) < 20:
        return None
    current = float(series.iloc[-1])
    return float((series <= current).sum() / len(series))


# ==================== MACD ====================

def macd(bars: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD：DIF = EMA(fast) - EMA(slow)；DEA = EMA(DIF, signal)；HIST = 2×(DIF-DEA)。

    HIST 乘 2 与通达信 MACD 柱口径一致（部分软件不乘2，趋势方向不受影响）。
    """
    close = _clean(bars["close"]) if bars is not None and len(bars) else pd.Series(dtype="float64")
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": (dif - dea) * 2.0})


def macd_cross_state(macd_df: pd.DataFrame, lookback: int = 3) -> tuple[str, int]:
    """判定MACD交叉状态。

    返回 (state, bars_since)：
      state ∈ {"golden","dead","above","below"}；bars_since 为距今bar数。
      - golden：最近 lookback 根内 DIF 上穿 DEA
      - dead  ：最近 lookback 根内 DIF 下穿 DEA
      - above/below：无新交叉，仅维持在同一侧
    """
    if macd_df is None or len(macd_df) < 2:
        return "below", -1
    dif = macd_df["dif"]
    dea = macd_df["dea"]
    diff = (dif - dea).dropna()
    if len(diff) < 2:
        return "below", -1
    for offset in range(1, min(lookback, len(diff) - 1) + 1):
        prev = float(diff.iloc[-1 - offset])
        cur = float(diff.iloc[-offset])
        if prev <= 0 < cur:
            return "golden", offset - 1
        if prev >= 0 > cur:
            return "dead", offset - 1
    return ("above" if float(diff.iloc[-1]) > 0 else "below"), -1


# ==================== KDJ ====================

def kdj(bars: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """KDJ：RSV → K/D 用 SMA(n,1) 递推（等效 ewm(alpha=1/n)）→ J = 3K - 2D。

    通达信 SMA(X, n, 1) = 前值×(n-1)/n + X/n，等价于 ewm(alpha=1/n, adjust=False)。
    """
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=["k", "d", "j", "rsv"])
    high = _clean(bars["high"])
    low = _clean(bars["low"])
    close = _clean(bars["close"])
    low_n = low.rolling(window=n, min_periods=n).min()
    high_n = high.rolling(window=n, min_periods=n).max()
    span = (high_n - low_n).replace(0.0, np.nan)
    rsv = ((close - low_n) / span * 100.0).fillna(50.0)
    k = rsv.ewm(alpha=1.0 / m1, adjust=False).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False).mean()
    return pd.DataFrame({"k": k, "d": d, "j": 3.0 * k - 2.0 * d, "rsv": rsv})


# ==================== RSI ====================

def rsi(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """RSI（Wilder 平滑，alpha=1/n）。"""
    if bars is None or len(bars) == 0:
        return pd.Series(dtype="float64")
    close = _clean(bars["close"])
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # 全涨（avg_loss=0）→ RSI=100；全跌 → RSI=0
    return out.where(avg_loss > 0, 100.0).where(avg_gain > 0, 0.0).fillna(50.0)


# ==================== ATR ====================

def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR（真实波幅均值，Wilder 平滑）。"""
    if bars is None or len(bars) < 2:
        return pd.Series(dtype="float64")
    high = _clean(bars["high"])
    low = _clean(bars["low"])
    close = _clean(bars["close"])
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


# ==================== 箱体 / 分位 ====================

def box_levels(
    bars: pd.DataFrame, lookback: int = 20,
) -> tuple[float | None, float | None, int]:
    """N日箱体上下沿 = 近 lookback 根日线的最高价/最低价。

    返回 (box_high, box_low, 实际使用的bar数)。样本不足5根返回 (None, None, n)。
    """
    if bars is None or len(bars) < 5:
        return None, None, 0 if bars is None else len(bars)
    window = bars.tail(lookback)
    high = _clean(window["high"]).max()
    low = _clean(window["low"]).min()
    if not math.isfinite(high) or not math.isfinite(low):
        return None, None, len(window)
    return float(high), float(low), len(window)


def box_position(price: float, box_high: float, box_low: float) -> float | None:
    """现价在箱体中的位置（0=下沿，1=上沿）；箱体退化时返回 None。"""
    span = box_high - box_low
    if not math.isfinite(span) or span <= 1e-9:
        return None
    return float((price - box_low) / span)


def percentile_rank(values: Any, current: float | None = None) -> float | None:
    """当前值在历史序列中的分位（0~100，0=最低）。

    样本不足8个返回 None（与项目内 A10/申万分位口径一致的最小样本量）。
    """
    series = _clean(values).dropna() if values is not None else pd.Series(dtype="float64")
    if len(series) < 8:
        return None
    target = float(series.iloc[-1]) if current is None else float(current)
    return float((series <= target).sum() / len(series) * 100.0)


# ==================== 分钟数据聚合 ====================

def _bucket_end_label(minutes_of_day: int, minutes: int) -> int:
    """桶起点（当日第几分钟）→ 该桶的**结束时刻**（A股K线以结束时间命名）。

    交易时段边界不得越界：
      - 桶起点已落在午休区间 [11:30, 13:00) → 并入上午收盘 11:30
        （收盘瞬间的逐笔会被分到起点=11:30的桶，标签必须回到 11:30 而非 11:35）；
      - 桶起点 ≥ 15:00 → 并入收盘 15:00；
      - 其余按起点+M分钟。
    """
    if MORNING_CLOSE <= minutes_of_day < AFTERNOON_OPEN:
        return MORNING_CLOSE
    if minutes_of_day >= AFTERNOON_CLOSE:
        return AFTERNOON_CLOSE
    return minutes_of_day + minutes


def aggregate_to_bars(points: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """把分时/逐笔数据聚合为 N 分钟K线（输出以**结束时刻**命名，与行情源口径一致）。

    输入列：ts(可解析为时间, 同一交易日)、price、volume（可选 amount）。
    输出列：ts/open/high/low/close/volume/amount，按时间升序。
    用于「数据源只提供分时或逐笔」时的降级路径（如新浪逐笔 → 5分钟K线）。
    """
    if points is None or len(points) == 0:
        return pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume", "amount"])
    df = points.copy()
    df["dt"] = pd.to_datetime(df["ts"])
    df = df.sort_values("dt")
    # 以「交易日 + 当日第几分钟」分桶：跨日自动切分（不同交易日不能用同一个日期标签），
    # 午休不产生空桶；输出标签取桶的结束时刻
    df["day"] = df["dt"].dt.strftime("%Y-%m-%d")
    minutes_of_day = (df["dt"].dt.hour * 60 + df["dt"].dt.minute).astype(int)
    df["bucket"] = (minutes_of_day // minutes) * minutes
    grouped = df.groupby(["day", "bucket"], sort=True)
    out = pd.DataFrame({
        "open": grouped["price"].first(),
        "high": grouped["price"].max(),
        "low": grouped["price"].min(),
        "close": grouped["price"].last(),
        "volume": grouped["volume"].sum() if "volume" in df.columns else 0.0,
        "amount": grouped["amount"].sum() if "amount" in df.columns else 0.0,
    }).reset_index()
    ends = [_bucket_end_label(int(start), minutes) for start in out["bucket"]]
    out["ts"] = [
        f"{day} {end // 60:02d}:{end % 60:02d}"
        for day, end in zip(out["day"], ends, strict=True)
    ]
    out = out.drop(columns=["bucket", "day"])
    return out[["ts", "open", "high", "low", "close", "volume", "amount"]]


def bars_to_frame(bars: list[Any]) -> pd.DataFrame:
    """模型 Bar 列表 → DataFrame（DB/外部源缺失时返回空表）。"""
    if not bars:
        return pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume", "amount"])
    if isinstance(bars, pd.DataFrame):
        return bars
    records = [
        b.model_dump() if hasattr(b, "model_dump") else dict(b) for b in bars
    ]
    return pd.DataFrame(records)


def session_minutes_of(ts: str) -> int | None:
    """时间戳 → 当日交易分钟序号（09:30 起算，午休跳过）；解析失败返回 None。

    用于跨午休对齐分时图的 x 轴，使 11:30 与 13:00 相邻而不留空隙。
    越界点（集合竞价、盘后）夹到两端，边界常量见 `core.trading_session`。
    """
    try:
        dt = pd.to_datetime(ts)
    except (ValueError, TypeError):
        return None
    return session_offset(int(dt.hour) * 60 + int(dt.minute))
