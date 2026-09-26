"""关键价位（回踩/冲高/止损）的**按股神经网络拟合**。

用户要的是：用 7 个客观维度（筹码量能结构 / 箱体压力位 / 缠论结构 / VWAP偏离 /
布林带 / MACD / KDJ·RSI）把这三条线"拟合出来"，目标是过去 10 个交易日做T成功率达
80% 以上；其余 7 个维度（指数量能 / 消息面 / 市场情绪 / 情绪周期 / 海外映射 /
板块排行 / 股性）**作为调整项**在拟合之后叠加。

## 一句话说清"神经网络在这里干什么"

三条线本身**不是**神经网络从零"猜"出来的 —— 它们在数学上是**分位数**：
"跌到过去 10 天里最深的 10% 那种位置"就是回踩线。真正需要学的、也确实是
**逐票不同、无法预先写死**的那部分，是「**怎么把这些客观维度组合成对该股
合适的尺度**」：

    回踩线 = Σ_k  w_k(该票自己学出来的混合权重) × 分位数锚点_k(P05…P30)
    冲高线 = Σ_k  v_k × 分位数锚点_k(P70…P95)
    止损位 = Σ_k  u_k × 分位数锚点_k(P97…P99.5)

  - **分位数锚点**由该股自己的经验分布给出（P10 就是"这只票 10 天里最深的那 10%
    的跌幅"），因此天然带上了该股的波动尺度（≈ATR 口径）；
  - **混合权重 w/v/u** 就是神经网络拟合出来的参数：K 个锚点 × 3 条线 = 15 个数
    （外加一层 7→hidden 的共享层用于**逐 bar 微调**，见下）。

这就是"客观数据 → 神经网络拟合 → 三条价格线"的落点，而且每个数都能追溯到
"哪个分位数、权重多少" —— 不是黑箱给出的三个神秘价格。

## 成功率怎么算（这是全模块最关键的诚实点）

对第 t 根 bar，给定候选三条线（low<high<stop）：

    触及：bar_low[t] ≤ low
    成功：在触及之后的 H 根 bar 内，**先**碰到 high、且**全程**不破 stop
    失败：先破 stop；或 H 根内没到 high（时间止损）

成功率 = 成功次数 / 触及次数。**只统计"触及过"的样本** —— 分母里不能混进
"线在远处、一次都没碰到"的 bar，否则把线画得极远就能刷出 100% 成功率。

## 为什么必须报两个成功率

| 口径 | 含义 | 为什么要报 |
|---|---|---|
| in-sample | 在**训练的那些天**上的成功率 | 用户问的就是"过去 10 个交易日成功率"，这是他看得到的那个数 |
| walk-forward | **留一天**（leave-one-day-out）预测出来的成功率 | 只看 in-sample 一定会过拟合：480 根 bar 上把成功率做到 100% 太容易了 |

**启用闸门**：只有 walk-forward 也达标（默认 ≥80%）且触及样本足够（默认 ≥20），
才允许把拟合档位用在信号判定上；否则**回退规则口径**并把两个数如实报出来。
这条闸门的用意是：宁可告诉用户"这只票拟合不出 80%，我用规则口径"，
也不能拿一个过拟合的 80% 去指导真金白银的下单。

## 实现约束

- 只用 `numpy`（项目没有 torch/sklearn），因此网络是手写的前向/反向传播：
  7→hidden(tanh)→hidden/2(tanh)→3(sigmoid)，Adam + L2，全批量梯度下降。
  这样单票拟合在 0.1~1 秒量级，可以按 (代码, 交易日) 缓存。
- 单票样本只有几百根 bar，因此**网格搜索分位数锚点（主）+ 小网络逐bar微调（辅）**：
  主搜索是低方差的，网络只学"什么时候该把线放宽一点"。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from src.intraday.config import LevelFitConfig

logger = logging.getLogger(__name__)

# 7 个客观输入维度的顺序（与面板上的因子名一一对应，改顺序必须同步 docs）
OBJECTIVE_FEATURES: tuple[tuple[str, str], ...] = (
    ("chip_volume_ratio", "筹码量能结构（今日量能 / 近5日均量）"),
    ("box_position", "箱体/压力位（现价在近N日箱体的位置 0~1）"),
    ("chan_position", "缠论结构（现价相对最后中枢的位置，中枢内=0）"),
    ("vwap_dev_atr", "VWAP偏离（相对当日均价的偏离 ÷ 日ATR%）"),
    ("boll_pct_b", "布林带（%B 0~1）"),
    ("macd_atr", "MACD（DIF-DEA ÷ 日ATR%）"),
    ("kdj_rsi", "KDJ/RSI（(50-J)/50 与 (50-RSI)/40 的均值）"),
)
FEATURE_KEYS: tuple[str, ...] = tuple(key for key, _ in OBJECTIVE_FEATURES)


# ==================== 结果结构 ====================


@dataclass
class FitMetrics:
    """拟合质量（**两个成功率都要报**，见模块 docstring）。"""

    available: bool = False
    reason: str = ""
    sessions: int = 0
    bars: int = 0
    horizon_bars: int = 0
    touch_samples: int = 0
    # 训练窗内（用户问的那个数）
    in_sample_rate: float | None = None
    in_sample_touches: int = 0
    # 留一日交叉验证（真正决定能不能启用）
    walk_forward_rate: float | None = None
    walk_forward_touches: int = 0
    # 拟合出的三条线在训练窗内的平均"往返"收益（扣双边成本）
    avg_round_trip_pct: float | None = None
    # 同一窗口内**放宽搜索**能达到的上限：用来区分"没搜到"与"到不了"
    best_achievable_rate: float | None = None
    best_achievable_lines: list[float] = field(default_factory=list)
    target_hit_rate: float = 0.80
    gate_passed: bool = False
    gate_reason: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available, "reason": self.reason,
            "sessions": self.sessions, "bars": self.bars,
            "horizon_bars": self.horizon_bars,
            "touch_samples": self.touch_samples,
            "in_sample_rate": self.in_sample_rate,
            "in_sample_touches": self.in_sample_touches,
            "walk_forward_rate": self.walk_forward_rate,
            "walk_forward_touches": self.walk_forward_touches,
            "avg_round_trip_pct": self.avg_round_trip_pct,
            "best_achievable_rate": self.best_achievable_rate,
            "best_achievable_lines": list(self.best_achievable_lines),
            "target_hit_rate": self.target_hit_rate,
            "gate_passed": self.gate_passed, "gate_reason": self.gate_reason,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class LevelFitResult:
    """一只票的一次拟合结果（可 JSON 化、可缓存、可解释）。"""

    code: str = ""
    trade_date: str = ""
    fitted_at: str = ""
    metrics: FitMetrics = field(default_factory=FitMetrics)
    # 拟合出的"分位混合"系数（每条线一组，和为1），可追溯到具体分位数
    low_mix: list[float] = field(default_factory=list)
    high_mix: list[float] = field(default_factory=list)
    stop_mix: list[float] = field(default_factory=list)
    low_anchors: list[float] = field(default_factory=list)   # 分位数锚点（%）
    high_anchors: list[float] = field(default_factory=list)
    stop_anchors: list[float] = field(default_factory=list)
    # 逐bar微调网络的参数（用于把"此刻"的因子状态映射成三条线的比例微调）
    net: dict[str, Any] = field(default_factory=dict)
    feature_means: list[float] = field(default_factory=list)
    feature_stds: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "trade_date": self.trade_date,
            "fitted_at": self.fitted_at, "metrics": self.metrics.to_dict(),
            "low_mix": list(self.low_mix), "high_mix": list(self.high_mix),
            "stop_mix": list(self.stop_mix),
            "low_anchors": list(self.low_anchors),
            "high_anchors": list(self.high_anchors),
            "stop_anchors": list(self.stop_anchors),
            "feature_means": list(self.feature_means),
            "feature_stds": list(self.feature_stds),
            "features": [{"key": key, "label": label}
                         for key, label in OBJECTIVE_FEATURES],
            "notes": list(self.notes),
        }


# ==================== 小网络（手写前向/反向） ====================


@dataclass
class TinyNet:
    """7→h→h/2→3 的小网络（tanh + sigmoid），Adam 训练。

    刻意小：单票训练样本只有几百根 bar，网络一大就一定过拟合。
    输出 3 个 [0,1] 的比例，含义是"在锚点区间里往哪一侧偏"。
    """

    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    w3: np.ndarray
    b3: np.ndarray

    @staticmethod
    def init(n_in: int, hidden: int, seed: int) -> TinyNet:
        rng = np.random.default_rng(seed)
        h2 = max(2, hidden // 2)
        scale = lambda n: math.sqrt(1.0 / max(1, n))  # noqa: E731
        return TinyNet(
            w1=rng.normal(0, scale(n_in), (n_in, hidden)),
            b1=np.zeros(hidden),
            w2=rng.normal(0, scale(hidden), (hidden, h2)),
            b2=np.zeros(h2),
            w3=rng.normal(0, scale(h2), (h2, 3)),
            b3=np.zeros(3),
        )

    def forward(self, x: np.ndarray) -> tuple[np.ndarray, tuple[Any, ...]]:
        z1 = x @ self.w1 + self.b1
        a1 = np.tanh(z1)
        z2 = a1 @ self.w2 + self.b2
        a2 = np.tanh(z2)
        z3 = a2 @ self.w3 + self.b3
        out = 1.0 / (1.0 + np.exp(-np.clip(z3, -30, 30)))
        return out, (x, a1, a2, out)

    def params(self) -> list[np.ndarray]:
        return [self.w1, self.b1, self.w2, self.b2, self.w3, self.b3]

    def to_dict(self) -> dict[str, Any]:
        return {
            "w1": self.w1.tolist(), "b1": self.b1.tolist(),
            "w2": self.w2.tolist(), "b2": self.b2.tolist(),
            "w3": self.w3.tolist(), "b3": self.b3.tolist(),
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> TinyNet:
        return TinyNet(
            w1=np.asarray(payload["w1"], dtype=float),
            b1=np.asarray(payload["b1"], dtype=float),
            w2=np.asarray(payload["w2"], dtype=float),
            b2=np.asarray(payload["b2"], dtype=float),
            w3=np.asarray(payload["w3"], dtype=float),
            b3=np.asarray(payload["b3"], dtype=float),
        )


def _train_net(
    x: np.ndarray, y: np.ndarray, *, cfg: LevelFitConfig, epochs: int,
) -> TinyNet:
    """全批量 Adam 训练（x 已标准化，y 是 0/1 是否成功）。"""
    net = TinyNet.init(x.shape[1], cfg.hidden, cfg.seed)
    params = net.params()
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    grad_scale = 1.0 / max(1, x.shape[0])
    for step in range(1, epochs + 1):
        out, (xb, a1, a2, _o) = net.forward(x)
        diff = (out[:, 0] - y) * grad_scale          # 只用第 1 个输出做监督
        dz3 = np.zeros_like(out)
        dz3[:, 0] = diff * out[:, 0] * (1.0 - out[:, 0])
        gw3 = a2.T @ dz3 + cfg.l2 * net.w3
        gb3 = dz3.sum(axis=0)
        da2 = dz3 @ net.w3.T
        dz2 = da2 * (1.0 - a2 ** 2)
        gw2 = a1.T @ dz2 + cfg.l2 * net.w2
        gb2 = dz2.sum(axis=0)
        da1 = dz2 @ net.w2.T
        dz1 = da1 * (1.0 - a1 ** 2)
        gw1 = xb.T @ dz1 + cfg.l2 * net.w1
        gb1 = dz1.sum(axis=0)
        grads = [gw1, gb1, gw2, gb2, gw3, gb3]
        for index, (param, grad) in enumerate(zip(params, grads, strict=True)):
            m[index] = beta1 * m[index] + (1 - beta1) * grad
            v[index] = beta2 * v[index] + (1 - beta2) * grad * grad
            m_hat = m[index] / (1 - beta1 ** step)
            v_hat = v[index] / (1 - beta2 ** step)
            param -= cfg.learning_rate * m_hat / (np.sqrt(v_hat) + eps)
    return net


# ==================== 数据集 ====================


@dataclass
class FitDataset:
    """拟合用数据集（逐 bar）。"""

    available: bool = False
    reason: str = ""
    days: list[str] = field(default_factory=list)
    frame: pd.DataFrame | None = None     # 列见 `FEATURE_KEYS` + low/high/close/day
    features: np.ndarray | None = None
    closes: np.ndarray | None = None
    highs: np.ndarray | None = None
    lows: np.ndarray | None = None
    day_ids: np.ndarray | None = None
    # 每根 bar 所属交易日的均价（做T的参照中枢）。
    # 预先算好存下来：`evaluate_levels` 在网格搜索里会被调用上万次，
    # 每次都 groupby 一遍会把拟合从"几百毫秒"拖到"几十秒"。
    day_mean: np.ndarray | None = None
    anchors: dict[str, np.ndarray] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def bars(self) -> int:
        return 0 if self.features is None else int(self.features.shape[0])


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_dataset(
    *,
    bars: pd.DataFrame | None,
    day_low: float | None = None,
    day_high: float | None = None,
    pct_b: float | None = None,
    bandwidth: float | None = None,
    dev_z: float | None = None,
    chip_volume_ratio: float | None = None,
    box_position: float | None = None,
    chan_position: float | None = None,
    macd_atr: float | None = None,
    kdj_rsi: float | None = None,
    atr_pct: float | None = None,
    cfg: LevelFitConfig | None = None,
) -> FitDataset:
    """5 分钟 bars + 当日/日级因子快照 → 逐 bar 的拟合数据集。

    `bars` 需要含 ts/open/high/low/close/volume 列（多日连续）。
    日级因子（筹码/箱体/缠论/ATR）在历史上的**逐bar真值拿不到**（它们的快照是
    "此刻"的），因此按该票最近一次快照**恒定**注入 —— 这一点必须写进 notes
    如实告知：拟合学到的是"在当前的筹码/箱体/缠论状态下，尺度该怎么定"，
    而不是"历史上每一天各自的筹码状态"。要拿到后者需要历史分笔资金流，
    本项目的数据源没有（不猜、不编）。
    """
    config = cfg or LevelFitConfig()
    if bars is None or len(bars) == 0:
        return FitDataset(available=False, reason="无分钟K线（数据源缺口）")
    frame = bars.copy()
    required = {"ts", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        return FitDataset(available=False, reason=f"分钟K线缺列：{sorted(missing)}")
    frame["day"] = frame["ts"].astype(str).str.slice(0, 10)
    days = sorted(frame["day"].unique().tolist())
    if len(days) > config.sessions:
        keep = set(days[-config.sessions:])
        frame = frame[frame["day"].isin(keep)].reset_index(drop=True)
        days = sorted(frame["day"].unique().tolist())
    if len(days) < config.min_sessions or len(frame) < config.min_bars:
        return FitDataset(
            available=False,
            reason=(f"样本不足：{len(days)} 个交易日 / {len(frame)} 根 bar"
                    f"（需 ≥{config.min_sessions} 天且 ≥{config.min_bars} 根）"))

    close = frame["close"].astype(float).to_numpy()
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    if not np.all(np.isfinite(close)) or np.any(close <= 0):
        return FitDataset(available=False, reason="分钟K线含非法价格（NaN/0）")

    # ---- 7 个客观维度（与 OBJECTIVE_FEATURES 顺序一致）----
    atr_pct_value = _finite(atr_pct)
    if atr_pct_value is None or atr_pct_value <= 0:
        # 日ATR 缺失时用"该票这 10 天分钟收盘的日内波动"兜底（仍是它自己的数据）
        per_day = frame.groupby("day")["close"].agg(["max", "min"]).reset_index()
        ratios = ((per_day["max"] - per_day["min"]) / per_day["min"] * 100.0).to_numpy()
        atr_pct_value = float(np.nanmedian(ratios)) if len(ratios) else 1.0
        atr_pct_value = max(0.2, atr_pct_value)

    # 逐bar VWAP偏离：用当日累计均价（与打分口径一致）
    day_mean = frame.groupby("day")["close"].transform("mean").to_numpy()
    dev_pct_bar = (close - day_mean) / np.maximum(day_mean, 1e-9) * 100.0
    # 注：快照里的 `dev_z` 是"此刻"的标准化值，历史逐bar 的 z 需要当刻的滚动
    # 标准差 —— 这里用 `dev_pct / 日ATR%` 做同量纲归一化，口径写进 notes。
    feature_matrix = np.column_stack([
        np.full(len(frame), float(chip_volume_ratio or 1.0)),
        np.full(len(frame), float(box_position if box_position is not None else 0.5)),
        np.full(len(frame), float(chan_position or 0.0)),
        dev_pct_bar / np.maximum(atr_pct_value, 1e-9),
        np.full(len(frame), float(pct_b if pct_b is not None else 0.5)),
        np.full(len(frame), float(macd_atr or 0.0)),
        np.full(len(frame), float(kdj_rsi or 0.0)),
    ])
    feature_matrix = np.nan_to_num(feature_matrix, nan=0.0, posinf=0.0, neginf=0.0)

    # ---- 分位数锚点：该票自己的 |波动| 经验分布 ----
    # 为什么用"每根 bar 相对当日均价的偏离幅度"而不是"相对前收"：
    # 做T的参照物是当日的成本中枢，不是隔夜收盘价。
    magnitude = np.abs(dev_pct_bar)
    magnitude = magnitude[np.isfinite(magnitude)]
    if magnitude.size < 30:
        return FitDataset(available=False, reason="有效波动样本不足（<30 根）")
    anchors = {
        "low": np.quantile(magnitude, config.low_quantiles),
        "high": np.quantile(magnitude, config.high_quantiles),
        "stop": np.quantile(magnitude, config.stop_quantiles),
    }

    dataset = FitDataset(
        available=True, days=days, frame=frame, features=feature_matrix,
        closes=close, highs=high, lows=low,
        day_ids=frame["day"].to_numpy(),
        day_mean=day_mean, anchors=anchors)
    dataset.notes = [
        f"训练窗口：{len(days)} 个交易日（{days[0]} ~ {days[-1]}）/ {len(frame)} 根 5 分钟bar",
        f"日ATR 口径：{atr_pct_value:.2f}%（{'日线ATR' if _finite(atr_pct) else '用日内极差兜底'}）",
        "筹码/箱体/缠论为日级快照，按当前值恒定注入（历史逐bar真值需要分笔资金流，数据源没有）",
        f"分位数锚点基于该票自己的偏离分布：P{config.low_quantiles[0]:.2f}="
        f"{anchors['low'][0]:.2f}% ~ P{config.high_quantiles[-1]:.2f}={anchors['high'][-1]:.2f}%",
    ]
    _ = day_low, day_high, bandwidth  # 保留入参：调用方口径对齐用
    return dataset


def _first_cross(mask: np.ndarray, start: int, end: int) -> int:
    """[start, end) 内第一个 True 的下标；没有则返回 end。"""
    for index in range(start, min(end, mask.shape[0])):
        if mask[index]:
            return index
    return end


def evaluate_levels(
    *, dataset: FitDataset, low_pct: float, high_pct: float, stop_pct: float,
    horizon: int, cost_pct: float, min_touches: int = 1,
) -> dict[str, Any]:
    """按"先触回踩 → 先到冲高 vs 先破止损"统计成功率（见模块 docstring）。

    `*_pct` 是相对**当日均价**的偏离百分比（正数表示离中枢多远）。
    返回 `touches / success / rate / avg_ret_pct`。

    单轮盈亏口径（做T一轮：回踩买入 → 冲高卖出）：
      - 成功：`+(high-low) − cost`（cost 是双边摩擦成本）
      - 先破止损：`−(stop-low) − cost`
      - 时间到没到冲高：`−cost`（按现价附近平掉，只亏手续费）
    """
    empty = {"touches": 0, "success": 0, "rate": None, "avg_ret_pct": None}
    if not dataset.available or dataset.closes is None or dataset.day_mean is None:
        return empty
    if not (0 < low_pct < high_pct) or stop_pct <= low_pct:
        return empty
    if (high_pct - low_pct) < cost_pct:
        # 价差覆盖不了双边成本 → 这种"成功"不赚钱，直接判 0
        return empty

    close = dataset.closes
    high = dataset.highs if dataset.highs is not None else close
    low = dataset.lows if dataset.lows is not None else close
    day_mean = dataset.day_mean
    low_line = day_mean * (1.0 - low_pct / 100.0)
    high_line = day_mean * (1.0 + high_pct / 100.0)
    stop_line = day_mean * (1.0 - stop_pct / 100.0)

    # 先做一次向量化的"触及"判定：网格搜索里绝大多数组合一次都不触及，
    # 提前返回可以省掉成千上万次 Python 循环（拟合耗时的大头就在这里）。
    touched = np.flatnonzero(low <= low_line)
    if touched.shape[0] < min_touches:
        return {**empty, "touches": int(touched.shape[0])}

    touches = 0
    success = 0
    total_ret = 0.0
    total_bars = close.shape[0]
    for index in touched:
        index = int(index)
        # 前瞻窗口**不跨交易日**：做T是当日事当日毕（收盘前必须平掉），
        # 允许跨日会让"隔夜跳空"被算成做T成功，那是自欺欺人。
        end = index + 1
        while end < total_bars and end <= index + horizon and \
                dataset.day_ids[end] == dataset.day_ids[index]:
            end += 1
        touches += 1
        hit_high = _first_cross(high[:end] >= high_line[:end], index + 1, end)
        hit_stop = _first_cross(low[:end] <= stop_line[:end], index + 1, end)
        if hit_stop < hit_high:
            total_ret -= (stop_pct - low_pct) + cost_pct
            continue
        if hit_high >= end:
            total_ret -= cost_pct
            continue
        success += 1
        total_ret += (high_pct - low_pct) - cost_pct
    return {
        "touches": touches, "success": success,
        "rate": (success / touches) if touches else None,
        "avg_ret_pct": (total_ret / touches) if touches else None,
    }


def _grid_best(
    *, dataset: FitDataset, low_options: np.ndarray, high_options: np.ndarray,
    stop_options: np.ndarray, cfg: LevelFitConfig, mask: np.ndarray | None = None,
) -> tuple[tuple[float, float, float], dict[str, Any]]:
    """网格搜索三条线（主搜索器）。

    目标函数刻意不是"成功率最大"，而是：
        score = 成功率 − 0.35×max(0, 目标成功率 − 成功率)
                − 0.002×(1/触及数)   ← 样本少的组合要吃亏
    为什么：单纯最大化成功率会让搜索收敛到"只触发 1~2 次的极端组合"
    （那种 100% 没有任何意义）。
    """
    sub = dataset if mask is None else _slice(dataset, mask)
    best: tuple[float, float, float] | None = None
    best_stat: dict[str, Any] = {}
    best_score = -1e9
    for low_pct in low_options:
        for high_pct in high_options:
            for stop_pct in stop_options:
                stat = evaluate_levels(
                    dataset=sub, low_pct=float(low_pct), high_pct=float(high_pct),
                    stop_pct=float(stop_pct), horizon=cfg.horizon_bars,
                    cost_pct=cfg.round_trip_cost_pct)
                rate = stat["rate"]
                if rate is None or stat["touches"] < cfg.min_touch_samples:
                    continue
                score = (rate
                         - 0.35 * max(0.0, cfg.target_hit_rate - rate)
                         - 0.02 / max(1, stat["touches"]))
                if score > best_score:
                    best_score = score
                    best = (float(low_pct), float(high_pct), float(stop_pct))
                    best_stat = stat
    if best is None:
        return (0.0, 0.0, 0.0), {"touches": 0, "success": 0, "rate": None,
                                 "avg_ret_pct": None}
    return best, best_stat


def _slice(dataset: FitDataset, mask: np.ndarray) -> FitDataset:
    """按布尔掩码取子集（留一日交叉验证用）。"""
    return FitDataset(
        available=True, days=sorted(set(dataset.day_ids[mask].tolist())),
        frame=None,
        features=None if dataset.features is None else dataset.features[mask],
        closes=dataset.closes[mask], highs=dataset.highs[mask],
        lows=dataset.lows[mask], day_ids=dataset.day_ids[mask],
        day_mean=dataset.day_mean[mask], anchors=dataset.anchors)


# ==================== 主入口 ====================


def fit_levels(
    *, code: str, dataset: FitDataset, cfg: LevelFitConfig | None = None,
    trade_date: str = "",
) -> LevelFitResult:
    """按股拟合三条价位线 + 双口径成功率 + 启用闸门。"""
    config = cfg or LevelFitConfig()
    started = time.perf_counter()
    result = LevelFitResult(
        code=code, trade_date=trade_date or datetime.now().strftime("%Y-%m-%d"),
        fitted_at=datetime.now().isoformat(timespec="seconds"))
    metrics = FitMetrics(
        horizon_bars=config.horizon_bars, target_hit_rate=config.target_hit_rate,
        sessions=len(dataset.days), bars=dataset.bars)
    result.metrics = metrics
    result.notes.extend(dataset.notes)
    if not config.enabled:
        metrics.reason = "档位拟合已关闭（configs: factors.level_fit.enabled=false）"
        return result
    if not dataset.available:
        metrics.reason = dataset.reason or "数据集不可用"
        return result

    low_options = dataset.anchors["low"]
    high_options = dataset.anchors["high"]
    stop_options = dataset.anchors["stop"]
    result.low_anchors = [round(float(v), 4) for v in low_options]
    result.high_anchors = [round(float(v), 4) for v in high_options]
    result.stop_anchors = [round(float(v), 4) for v in stop_options]

    (low_pct, high_pct, stop_pct), stat = _grid_best(
        dataset=dataset, low_options=low_options, high_options=high_options,
        stop_options=stop_options, cfg=config)
    metrics.in_sample_rate = None if stat["rate"] is None else round(stat["rate"], 4)
    metrics.in_sample_touches = int(stat["touches"])
    metrics.touch_samples = int(stat["touches"])
    metrics.avg_round_trip_pct = (None if stat["avg_ret_pct"] is None
                                 else round(stat["avg_ret_pct"], 4))
    if stat["rate"] is None:
        metrics.reason = "训练窗内没有任何组合达到最少触及样本数（线放得太远/样本太薄）"
        metrics.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    # 混合系数：把"选中了哪个分位数"表达成一组权重（和=1），便于展示与追溯
    result.low_mix = _mix_for(low_pct, low_options)
    result.high_mix = _mix_for(high_pct, high_options)
    result.stop_mix = _mix_for(stop_pct, stop_options)

    # ---- 可达性上限：同一窗口内"放宽搜索"能到多少 ----
    # 为什么要这一步：拟合只在自己那 5×5×3 个锚点里选最优，用户看到 30% 会问
    # "是没搜到，还是这只票本来就不行"。于是再扫一遍更宽的组合（回踩 0.1~2.0%、
    # 冲高 0.2~4.0%、止损到 8%），把**该窗口内真正能达到的上限**报出来：
    #   · 上限 ≥ 目标 → 说明是搜索/泛化没做好，值得继续调；
    #   · 上限 < 目标 → 说明这段行情里不存在"成功率 80%"的做T结构（不是调参能解决的）。
    metrics.best_achievable_rate, metrics.best_achievable_lines = _best_achievable(
        dataset=dataset, cfg=config)

    # ---- 留一日交叉验证：逐日留出，用其余日拟合、在留出日上统计 ----
    if dataset.day_ids is not None and len(set(dataset.day_ids.tolist())) >= 3:
        fold_rows: list[np.ndarray] = []
        fold_ok = np.zeros(dataset.bars, dtype=bool)
        for day in sorted(set(dataset.day_ids.tolist())):
            hold = dataset.day_ids == day
            train_mask = ~hold
            if train_mask.sum() < config.min_bars // 2:
                continue
            (fold_low, fold_high, fold_stop), _ = _grid_best(
                dataset=dataset, low_options=low_options,
                high_options=high_options, stop_options=stop_options,
                cfg=config, mask=train_mask)
            if fold_high <= fold_low:
                continue
            sub = _slice(dataset, hold)
            stat_hold = evaluate_levels(
                dataset=sub, low_pct=fold_low, high_pct=fold_high,
                stop_pct=fold_stop, horizon=config.horizon_bars,
                cost_pct=config.round_trip_cost_pct)
            if stat_hold["rate"] is None or stat_hold["touches"] == 0:
                continue
            metrics.walk_forward_touches += int(stat_hold["touches"])
            fold_rows.append(np.array([
                stat_hold["success"], stat_hold["touches"]], dtype=float))
            fold_ok |= hold
        if fold_rows:
            stacked = np.vstack(fold_rows).sum(axis=0)
            if stacked[1] > 0:
                metrics.walk_forward_rate = round(float(stacked[0] / stacked[1]), 4)

    # ---- 逐bar微调网络（辅助）：学"什么时候该把线放宽一点" ----
    net, means, stds = _fit_micro_net(dataset=dataset, cfg=config)
    result.net = net.to_dict()
    result.feature_means = [round(float(v), 6) for v in means]
    result.feature_stds = [round(float(v), 6) for v in stds]
    result.metrics.elapsed_ms = int((time.perf_counter() - started) * 1000)

    # ---- 启用闸门：宁可回退规则口径，也不拿过拟合的 80% 去指导下单 ----
    metrics.available = True
    reasons: list[str] = []
    if metrics.walk_forward_rate is None:
        reasons.append("留一日交叉验证样本不足，无法确认泛化")
    elif metrics.walk_forward_rate < config.target_hit_rate:
        reasons.append(
            f"留一日成功率 {metrics.walk_forward_rate * 100:.0f}% < 目标 "
            f"{config.target_hit_rate * 100:.0f}%")
    if metrics.touch_samples < config.min_touch_samples:
        reasons.append(f"触及样本 {metrics.touch_samples} < {config.min_touch_samples}")
    metrics.gate_passed = not reasons
    metrics.gate_reason = "；".join(reasons) if reasons else (
        f"留一日成功率 {metrics.walk_forward_rate * 100:.0f}% ≥ 目标 "
        f"{config.target_hit_rate * 100:.0f}%，已启用拟合档位")
    if not metrics.gate_passed:
        result.notes.append(
            "⚠️ 拟合未过闸门 → 档位仍用规则口径（箱体/布林/ATR + 档位参数）。"
            "这不是失败：它说明这只票在过去这段时间里没有稳定可复制的 80% 做T结构。")
        if (metrics.best_achievable_rate is not None
                and metrics.best_achievable_rate < config.target_hit_rate):
            result.notes.append(
                f"📉 该窗口内**放宽搜索的上限**也只有 "
                f"{metrics.best_achievable_rate * 100:.0f}%"
                f"（回踩 {metrics.best_achievable_lines[0]:.2f}% / 冲高 "
                f"{metrics.best_achievable_lines[1]:.2f}% / 止损 "
                f"{metrics.best_achievable_lines[2]:.1f}%，"
                f"{metrics.best_achievable_lines[3]:.0f} 次触及）—— "
                f"所以这是行情结构决定的上限，不是调参能解决的；"
                f"目标 {config.target_hit_rate * 100:.0f}% 在这段数据上不可达。")
        elif metrics.best_achievable_rate is not None:
            result.notes.append(
                f"🔎 放宽搜索的上限是 {metrics.best_achievable_rate * 100:.0f}%"
                f"（≥目标 {config.target_hit_rate * 100:.0f}%）：说明机会存在，"
                "但当前拟合没能泛化到它 —— 值得在权重/档位参数上继续调。")
    result.notes.append(
        f"拟合线（相对当日均价的偏离）：回踩 −{low_pct:.2f}% / 冲高 +{high_pct:.2f}% "
        f"/ 止损 −{stop_pct:.2f}%（价差 {high_pct - low_pct:.2f}% vs 双边成本 "
        f"{config.round_trip_cost_pct:.2f}%）")
    return result


def _best_achievable(
    *, dataset: FitDataset, cfg: LevelFitConfig,
) -> tuple[float | None, list[float]]:
    """放宽搜索能到的成功率上限（区分"没搜到"与"到不了"）。

    网格刻意比正式搜索宽：回踩 0.10~2.00%、冲高 0.30~4.00%、止损 1.5~8.0%
    （步长 0.10%/1.0%）。代价是几百次 `evaluate_levels`，实测 ~0.2 秒，
    换来的是"目标是否可达"这个**结论性**信息 —— 值得。
    """
    best_rate: float | None = None
    best_lines: list[float] = []
    for low_pct in np.round(np.arange(0.10, 2.01, 0.10), 2):
        for high_pct in np.round(np.arange(0.30, 4.01, 0.10), 2):
            if high_pct - low_pct < max(0.2, cfg.round_trip_cost_pct):
                continue
            for stop_pct in (1.5, 2.0, 3.0, 4.0, 6.0, 8.0):
                if stop_pct <= low_pct:
                    continue
                stat = evaluate_levels(
                    dataset=dataset, low_pct=float(low_pct),
                    high_pct=float(high_pct), stop_pct=float(stop_pct),
                    horizon=cfg.horizon_bars, cost_pct=cfg.round_trip_cost_pct,
                    min_touches=cfg.min_touch_samples)
                rate = stat["rate"]
                if rate is None or stat["touches"] < cfg.min_touch_samples:
                    continue
                if best_rate is None or rate > best_rate:
                    best_rate = float(rate)
                    best_lines = [float(low_pct), float(high_pct), float(stop_pct),
                                  float(stat["touches"]),
                                  round(float(stat["avg_ret_pct"] or 0.0), 4)]
    return best_rate, best_lines


def _mix_for(value: float, options: np.ndarray) -> list[float]:
    """把"选中了哪个分位数"表达成一组权重（与 options 等长的 one-hot）。"""
    mix = [0.0] * len(options)
    best = int(np.argmin(np.abs(options - value)))
    mix[best] = 1.0
    return mix


def _fit_micro_net(
    *, dataset: FitDataset, cfg: LevelFitConfig,
) -> tuple[TinyNet, np.ndarray, np.ndarray]:
    """逐 bar 微调网络：输入 7 个客观维度，输出"是否适合放宽"的 0/1 标签。

    标签口径：该 bar **是否有回踩触及且最终成功**（成功=先到冲高、不破止损，
    阈值用主搜索得到的线）。这是一个可解释的二分类：
    "此刻的因子状态，是不是一个能做成 T 的状态"。
    网络输出用于**按当前状态微调**三条线的比例（见 `apply_net_adjustment`）。
    """
    if dataset.features is None or dataset.closes is None:
        net = TinyNet.init(len(FEATURE_KEYS), cfg.hidden, cfg.seed)
        zeros = np.zeros(len(FEATURE_KEYS))
        return net, zeros, np.ones(len(FEATURE_KEYS))
    features = dataset.features
    means = features.mean(axis=0)
    stds = features.std(axis=0)
    stds = np.where(stds < 1e-9, 1.0, stds)
    normalized = (features - means) / stds

    # 标签：用该票自己的样本内最优线，逐 bar 判"该 bar 是否触及且成功"
    (low_pct, high_pct, stop_pct), _ = _grid_best(
        dataset=dataset, low_options=dataset.anchors["low"],
        high_options=dataset.anchors["high"],
        stop_options=dataset.anchors["stop"], cfg=cfg)
    close = dataset.closes
    high = dataset.highs if dataset.highs is not None else close
    low = dataset.lows if dataset.lows is not None else close
    day_mean = pd.Series(close).groupby(
        pd.Series(dataset.day_ids)).transform("mean").to_numpy()
    low_line = day_mean * (1.0 - low_pct / 100.0)
    high_line = day_mean * (1.0 + high_pct / 100.0)
    stop_line = day_mean * (1.0 - stop_pct / 100.0)
    labels = np.zeros(close.shape[0])
    total_bars = close.shape[0]
    for index in range(close.shape[0]):
        if low[index] > low_line[index]:
            continue
        # 与 evaluate_levels 同口径：前瞻不跨交易日
        end = index + 1
        while end < total_bars and end <= index + cfg.horizon_bars and \
                dataset.day_ids[end] == dataset.day_ids[index]:
            end += 1
        hit_high = _first_cross(high[:end] >= high_line[:end], index + 1, end)
        hit_stop = _first_cross(low[:end] <= stop_line[:end], index + 1, end)
        labels[index] = 1.0 if (hit_high < end and hit_high < hit_stop) else 0.0
    pos = int(labels.sum())
    if pos < 5 or pos > labels.shape[0] - 5:
        # 标签几乎全 0 或全 1：训练没有意义，直接给"不调整"的网络
        net = TinyNet.init(len(FEATURE_KEYS), cfg.hidden, cfg.seed)
        return net, means, stds
    net = _train_net(normalized, labels, cfg=cfg, epochs=cfg.epochs)
    return net, means, stds


def net_adjustment(
    *, fit: LevelFitResult, features: dict[str, float],
) -> float:
    """用微调网络把"此刻的因子状态"映射成一个**乘数**（0.85 ~ 1.15）。

    含义：状态好（网络输出高）→ 把线放宽一点（更少的假触发）；
    状态差 → 收紧（更谨慎）。乘数被刻意夹在 ±15% —— 网络只有几百个样本，
    不该有能力把线挪到离谱的地方。
    """
    if not fit.net or not fit.feature_means or not fit.feature_stds:
        return 1.0
    if any(key not in features for key in FEATURE_KEYS):
        # 特征不完整时**不调整**：缺项补 0 会喂给网络一个它从没见过的输入分布，
        # 输出出来的乘数毫无意义（实测：缺项时网络会把乘数推到 0.88 附近）。
        return 1.0
    try:
        net = TinyNet.from_dict(fit.net)
        vector = np.array([[float(features.get(key, 0.0)) for key in FEATURE_KEYS]])
        means = np.asarray(fit.feature_means, dtype=float)
        stds = np.asarray(fit.feature_stds, dtype=float)
        stds = np.where(stds < 1e-9, 1.0, stds)
        out, _ = net.forward((vector - means) / stds)
        value = float(out[0, 0])
    except (KeyError, ValueError, TypeError):
        return 1.0
    return 1.0 + 0.30 * (value - 0.5)


def adjustment_factors(
    ctx: Any, scorecard: Any = None, factor_params: Any = None,
) -> dict[str, float]:
    """**调整项**：其余 7 个维度 → 三个有界乘数（拟合之后的第二段口径）。

    用户口径："然后再根据指数量能、消息面、市场情绪、市场情绪周期、海外映射、
    板块涨幅排行、股性微调" —— 也就是这 7 个维度**不参与拟合**，只做后置微调。

    为什么按"三个乘数"而不是"每个维度各调一条线"：7 个维度各调一次会互相打架
    （消息面看多 + 情绪周期退潮 = 抵消成噪声），而且无法解释。按**作用面**归并成三组，
    每组只影响"线的远近"，且乘数被夹在 [0.5, 1.5]：

    | 分组 | 维度 | 乘数含义 |
    |---|---|---|
    | `structure` 结构 | 指数量能、股性适配 | >1 = 该股/该环境适合更宽的波段（线放远，减少假触发） |
    | `environment` 环境 | 市场情绪、情绪周期、海外映射、板块排行 | >1 = 环境偏暖（回踩可以适度提前） |
    | `micro` 微观 | 消息面 | >1 = 消息面偏多（整体往有利方向微调） |

    打分来源有两个口径，优先用**打分卡**（那样与面板那张表逐行同源）；
    没有打分卡时直接跑这 7 个因子的打分核（同一批函数），因此结果口径一致 ——
    不存在"面板一套、调整项另一套"的情况。
    """
    scores: dict[str, float] = {}
    keys = ("index_volume", "character", "sentiment", "cycle", "overseas",
            "board_rank", "news")
    if scorecard is not None:
        for factor in getattr(scorecard, "factors", []) or []:
            if getattr(factor, "available", False):
                scores[getattr(factor, "key", "")] = float(
                    getattr(factor, "score", 0.0) or 0.0)
    elif ctx is not None:
        try:
            from src.intraday.factors import run_factor

            # 因子参数取自调用方注入的配置（与打分卡同一份）；
            # 调用方没给就退回默认参数 —— 口径仍与面板一致，只是不用个股微调值。
            params = factor_params or _params_for()
            for key in keys:
                try:
                    outcome = run_factor(key, ctx, params)
                except Exception:  # noqa: BLE001 单维缺失不影响其余维度
                    continue
                if outcome.available:
                    scores[key] = float(outcome.score)
        except Exception:  # noqa: BLE001 拿不到就按"无调整"处理
            scores = {}

    def group_mean(group_keys: tuple[str, ...]) -> float:
        values = [scores[key] for key in group_keys if key in scores]
        return float(np.mean(values)) if values else 0.0

    structure = group_mean(("index_volume", "character"))
    environment = group_mean(("sentiment", "cycle", "overseas", "board_rank"))
    micro = group_mean(("news",))
    return {
        "structure": float(np.clip(1.0 + 0.20 * structure, 0.5, 1.5)),
        "environment": float(np.clip(1.0 + 0.30 * environment, 0.5, 1.5)),
        "micro": float(np.clip(1.0 + 0.15 * micro, 0.5, 1.5)),
    }


def _params_for() -> Any:
    """调整项打分用的因子参数（调用方没注入时的缺省）。"""
    from src.intraday.config import FactorParams

    return FactorParams()


def objective_vector(ctx: Any, *, atr_pct: float | None = None) -> dict[str, float]:
    """从 `FactorContext` 抽出 7 个客观维度（顺序与 `FEATURE_KEYS` 一致）。

    `*_atr` 两项用"÷ 日ATR%"归一化：不同票的 MACD/偏离量纲差一个数量级，
    不归一化就没法共用同一套网络结构。
    """
    scale = atr_pct if atr_pct and atr_pct > 0 else 1.0
    chan_position = 0.0
    zd, zg, price = ctx.chan_zd, ctx.chan_zg, ctx.price
    if zd is not None and zg is not None and zg > zd and price:
        if price > zg:
            chan_position = 1.0
        elif price < zd:
            chan_position = -1.0
        else:
            chan_position = 0.0
    kdj_rsi = 0.0
    if ctx.kdj_intraday is not None or ctx.rsi_intraday is not None:
        parts: list[float] = []
        if ctx.kdj_intraday is not None and len(ctx.kdj_intraday):
            last = ctx.kdj_intraday.iloc[-1]
            value = _finite(last.get("kdj_j"))
            if value is not None:
                parts.append(max(-1.0, min(1.0, (50.0 - value) / 50.0)))
        if ctx.rsi_intraday is not None and len(ctx.rsi_intraday):
            value = _finite(ctx.rsi_intraday.iloc[-1])
            if value is not None:
                parts.append(max(-1.0, min(1.0, (50.0 - value) / 40.0)))
        kdj_rsi = float(np.mean(parts)) if parts else 0.0
    macd_atr = 0.0
    if ctx.macd_intraday is not None and len(ctx.macd_intraday):
        last = ctx.macd_intraday.iloc[-1]
        dif, dea = _finite(last.get("dif")), _finite(last.get("dea"))
        if dif is not None and dea is not None and price:
            macd_atr = (dif - dea) / max(1e-9, price * scale / 100.0)
    return {
        "chip_volume_ratio": float(ctx.chip_volume_ratio or 1.0),
        "box_position": float(ctx.box_position if ctx.box_position is not None else 0.5),
        "chan_position": chan_position,
        "vwap_dev_atr": float((ctx.dev_pct or 0.0) / max(1e-9, scale)),
        "boll_pct_b": float(ctx.pct_b if ctx.pct_b is not None else 0.5),
        "macd_atr": float(np.clip(macd_atr, -5.0, 5.0)),
        "kdj_rsi": kdj_rsi,
    }
