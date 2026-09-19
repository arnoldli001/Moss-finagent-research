"""缠论结构取数：把 `chan.py` 的原始结构压成打分需要的几个叶子字段。

为什么单独一个模块而不是各自在 service / daily 里写一遍：
分时链路（`service._extract_chan`）与日线链路（`daily._daily_chan`）都需要
「最后一个笔中枢 + MACD 背驰」，两处各写一次必然出现口径漂移
（一处传了 `area_ratio_max`、另一处忘了传，同一只票在两个面板上得到不同的中枢）。
这里做成唯一入口，顺带把「用哪根周期K线定中枢」也钉死：**都用日线**。

为什么中枢用日线而不是分时：
日线中枢才是"这只票当前的结构位置"，一天只生长 0~1 个；
分时中枢一天要长好几个，拿它当支撑压力会到处报警。
分时的超买超卖交给 vwap/boll/kdj 三个因子即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.intraday import indicators as ind
from src.intraday.config import IntradayConfig


@dataclass
class ChanFacts:
    """打分直接消费的缠论事实（可 JSON 序列化）。"""

    available: bool = False
    gap: str | None = None
    zd: float | None = None
    zg: float | None = None
    divergence: str | None = None          # "top" / "bottom" / None
    divergence_strength: float = 0.0
    bars_used: int = 0
    pivot_count: int = 0
    stroke_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available, "gap": self.gap,
            "zd": self.zd, "zg": self.zg,
            "divergence": self.divergence,
            "divergence_strength": self.divergence_strength,
            "bars_used": self.bars_used, "pivot_count": self.pivot_count,
            "stroke_count": self.stroke_count,
        }


def extract_chan_facts(
    bars: pd.DataFrame | None, config: IntradayConfig, *,
    macd_frame: pd.DataFrame | None = None,
) -> ChanFacts:
    """日线表 → 缠论事实（包含处理→分型→笔→中枢→背驰）。

    任何一步失败都返回 `available=False` + 中文 `gap`，**绝不抛异常**：
    它挂在做T主链路上，缠论算不出来不该让整个面板挂掉。
    """
    from src.intraday.chan import build_structure, detect_divergence

    params = config.factors.chan
    if bars is None or len(bars) < params.min_bars:
        have = 0 if bars is None else len(bars)
        return ChanFacts(
            available=False,
            gap=f"日线样本不足（{have}根，缠论结构至少需要{params.min_bars}根）")
    try:
        structure = build_structure(
            bars, min_bars=params.min_bars, min_gap=params.min_gap)
    except Exception as exc:  # noqa: BLE001 结构算法异常按"不可用"处理
        return ChanFacts(available=False, gap=f"缠论结构构建失败：{brief(exc, BRIEF_TIGHT)}")
    if not structure.available or not structure.pivots:
        return ChanFacts(
            available=False, bars_used=structure.bars_used,
            stroke_count=len(structure.strokes),
            gap=structure.gap or "未识别出笔中枢（样本内无三笔重叠）")
    pivot = structure.pivots[-1]
    macd = macd_frame
    if macd is None:
        macd_params = config.factors.macd
        macd = ind.macd(bars, fast=macd_params.fast, slow=macd_params.slow,
                        signal=macd_params.signal)
    try:
        divergence = detect_divergence(
            bars, structure, macd, area_ratio_max=params.area_ratio_max)
    except Exception:  # noqa: BLE001 背驰识别失败只是少一个加成，不是致命错误
        divergence = None
    return ChanFacts(
        available=True, zd=float(pivot.zd), zg=float(pivot.zg),
        divergence=None if divergence is None else divergence.kind,
        divergence_strength=0.0 if divergence is None else float(divergence.strength),
        bars_used=structure.bars_used, pivot_count=len(structure.pivots),
        stroke_count=len(structure.strokes))
