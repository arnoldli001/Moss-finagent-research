"""回测信号规则：本地确定性计算，无LLM、无未来数据。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    """月度对齐样本：period为YYYY-MM，price为月末收盘价。"""

    period: str
    price: float
    indicators: dict[str, float]
    pe: float | None = None


@dataclass(frozen=True)
class TrendPEConfig:
    """趋势+估值闸门规则参数。

    - 主指标环比变动绝对值超过 eps_pct 才认定上/下行；
    - 上行且（无PE或PE不高于水位线）→ +1 看多；
    - 下行 → -1 看空（A股多头策略中表现为空仓回避）；
    - 其余 → 0 中性。
    """

    trend_indicator: str
    eps_pct: float = 1.0
    pe_watermark: float | None = None


def _pct_change(prev: float, curr: float) -> float | None:
    if prev == 0:
        return None
    return (curr - prev) / abs(prev) * 100


def trend_pe_signal(history: list[Bar], cfg: TrendPEConfig) -> int:
    """根据截至t时刻（含）的历史bar返回信号 ∈ {-1,0,1}。

    调用方必须保证history不包含t之后的样本。
    """
    if len(history) < 2:
        return 0
    prev_bar, curr_bar = history[-2], history[-1]
    prev = prev_bar.indicators.get(cfg.trend_indicator)
    curr = curr_bar.indicators.get(cfg.trend_indicator)
    if prev is None or curr is None:
        return 0
    change = _pct_change(float(prev), float(curr))
    if change is None:
        return 0

    pe = curr_bar.pe
    pe_ok = cfg.pe_watermark is None or pe is None or pe <= cfg.pe_watermark

    if change > cfg.eps_pct and pe_ok:
        return 1
    if change < -cfg.eps_pct:
        return -1
    return 0


def generate_signals(bars: list[Bar], cfg: TrendPEConfig) -> list[int]:
    """逐月生成信号（严格扩展窗口，第i个信号仅用bars[:i+1]）。"""
    signals: list[int] = []
    for i in range(len(bars)):
        signals.append(trend_pe_signal(bars[: i + 1], cfg))
    return signals
