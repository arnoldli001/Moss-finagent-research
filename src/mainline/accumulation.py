"""主线挖掘：第二层「三维建仓痕迹」（**只在候选池内计算**）。

## 这一层为什么只有三维

V1.0 有五维（主力资金强度 30 / 杠杆 20 / 北向 20 / 筹码集中 15 / 量价 15），
其中主力资金强度、筹码集中与第一层的 moneyflow、chips 维度**算的是同一批
数据**，只是权重不同 —— 结果就是"资金流"这个因子在三层合成里被加权了
2-3 次。回测胜率因此虚高（同一份信息被当成三份独立证据），实盘立刻失效。

V2.0 只保留第一层**没有覆盖**的增量信息：

    leverage      35%  融资余额变化 —— 第一层完全没有（第一层资金流是个股
                       主动买卖口径，两融是**杠杆资金**口径，来源不同）
    northbound    35%  北向持股变化 —— 第一层同样没有（外资是独立的资金源）
    volume_price  30%  量价**形态**识别 —— 与第一层技术指标部分重叠，
                       因此这里**只做形态判定**（地量后放量 / 底分型反包），
                       绝不再算一次均线排列、RSI 或原始成交量因子

删除的两个维度写在 `config.py` 的 `AccumulationConfig` 文档里，
并在 `configs/mainline.yaml` 里留了注释 —— 避免后人"顺手补回来"。

## 横截面排名在**候选池内**做，不是在全市

第一层已经把全市场筛成前 20%（默认 60 个板块左右）。第二层的分位在池内算，
有两个理由：

1. **成本**：池内排名只需候选板块的融资/北向序列（约 60 个板块），
   全市排名要为 300 个板块各算一遍（且要历史序列），实盘每天多花几分钟；
2. **语义**：候选池本身就是"第一层筛出的强者"，池内排名回答的是
   "强者之中谁在建仓"，这正是第二层要问的问题。

代价必须说清：**池内第 1 名不等于全市第 1 名**。所以 `leverage` 的触发条件
"进入全市场前 N 名"在本模块实现为"**候选池内**前 N 名"，
`configs/mainline.yaml` 的注释里也这么写了。

## 无数据不参与加权

三个维度里任何一个取不到数（板块没有融资标的、北向披露规则变化导致
序列为空），该维度 `available=False`，权重从分母里剔除并重新归一化。
把"没数据"当 0 分会系统性压低候选板块的第二层分，进而让最终分永远到不了
强信号阈值 —— 而界面上只看到"今天没有强信号"。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.mainline.config import MainlineConfig
from src.mainline.models import (
    BoardSeries,
    DimensionScore,
    LayerScore,
)
from src.mainline.scoring import (
    clamp,
    mean,
    percentile_score,
    safe_ratio,
    to_float,
    weighted_score,
)

logger = logging.getLogger(__name__)

#: 三维各自的**子因子权重**（每个维度内部合计 1.0，理由同 `six_dim.SUB_WEIGHTS`）
SUB_WEIGHTS: dict[str, dict[str, float]] = {
    "leverage": {"change": 0.55, "persistence": 0.30, "trigger": 0.15},
    "northbound": {"streak": 0.40, "ratio": 0.45, "trigger": 0.15},
    "volume_price": {"form": 0.70, "strength": 0.30},
    # ETF 资金异动（份额净申购 / 成交额放量 / 连续申购天数），
    # 详见 `src/mainline/etf.py` 与 `config.EtfConfig`
    "etf": {"share": 0.45, "amount": 0.35, "streak": 0.20},
}

DIM_LABELS = {
    "leverage": "杠杆资金", "northbound": "北向资金",
    "volume_price": "量价形态", "etf": "ETF资金",
}

#: 触发型子因子的满分（维度内其它子因子是 0-100 的分位分，这里对齐量纲）
TRIGGER_SCORE = 100.0


@dataclass
class AccumulationInput:
    """第二层打分需要的一个候选板块的输入。"""

    code: str
    name: str
    series: BoardSeries = field(default_factory=lambda: BoardSeries(code=""))
    #: 板块融资余额序列 `[(date, rzye)]`（升序）
    margin: list[tuple[str, float]] = field(default_factory=list)
    #: 板块北向持股数量序列 `[(date, vol)]`（升序）
    northbound: list[tuple[str, float]] = field(default_factory=list)
    #: 板块 ETF 资金异动信号（多只 ETF 聚合后；无数据时为 None）
    etf: Any = None
    #: 板块流通市值（元）
    circ_mv: float | None = None

    @property
    def has_series(self) -> bool:
        return len(self.series.bars) > 0


# ==================================================================
# 维度一：杠杆资金
# ==================================================================


def leverage_raw(item: AccumulationInput, *, config: MainlineConfig
                 ) -> dict[str, float | None]:
    """融资余额 5 日变化率 + 连续走强天数 + 是否处于板块指数低位。

    "连续 N 日进入前 N 名"在本模块实现为**连续 N 日变化率为正且处于池内
    前 N 名**（见模块文档：横截面排名在候选池内做）。
    """
    cfg = config.accumulation.leverage
    window = max(1, int(cfg.change_window))
    series = [value for _, value in item.margin if value > 0]
    if len(series) < window + 1:
        return {"change": None, "persistence": None, "trigger": None,
                "low": None}
    changes: list[float] = []
    for index in range(window, len(series)):
        base = series[index - window]
        if base > 0:
            changes.append(series[index] / base - 1.0)
    if not changes:
        return {"change": None, "persistence": None, "trigger": None,
                "low": None}
    streak = 0
    for value in reversed(changes):
        if value > 0:
            streak += 1
        else:
            break
    low = _in_low_zone(item.series, window=int(cfg.low_position_window),
                       quantile=float(cfg.low_position_quantile))
    return {"change": changes[-1],
            "persistence": min(1.0, streak / max(1, int(cfg.consecutive_days))),
            "trigger": None,           # 由 `_leverage_trigger` 在池内排名后填
            "low": 1.0 if low else 0.0}


def _in_low_zone(series: BoardSeries, *, window: int, quantile: float
                 ) -> bool | None:
    """板块指数是否处于近 `window` 日的**低位区域**（默认下 35%）。

    数据不足返回 None（不猜）：拿不足 60 日的板块判"低位"，
    得到的结论只是"它上市以来一直在跌"。
    """
    closes = [bar.close for bar in series.bars if bar.close > 0]
    if len(closes) < max(window, 20):
        return None
    tail = closes[-window:]
    low, high = min(tail), max(tail)
    if high - low <= 1e-12:
        return None
    position = (closes[-1] - low) / (high - low)
    return position <= quantile


# ==================================================================
# 维度二：北向资金
# ==================================================================


def northbound_raw(item: AccumulationInput, *, config: MainlineConfig
                   ) -> dict[str, float | None]:
    """北向持股连续增加天数 + 累计净买入/流通市值。

    净买入是**代理值**：`Δ持股数 × 板块指数收盘价`。
    为什么不直接用金额：交易所自 2024-08 起不再披露北向日度净买入金额，
    只剩持股数量。用数量变化 × 当期价格是最接近的口径，且**方向严格一致**
    （增持即净买入），满足"排序正确"这个使用场景。
    """
    cfg = config.accumulation.northbound
    volumes = [value for _, value in item.northbound if value >= 0]
    if len(volumes) < 2:
        return {"streak": None, "ratio": None, "cum_ratio": None, "trigger": None}
    changes = [volumes[index] - volumes[index - 1]
               for index in range(1, len(volumes))]
    streak = 0
    for value in reversed(changes):
        if value > 0:
            streak += 1
        else:
            break
    closes = [bar.close for bar in item.series.bars if bar.close > 0]
    price = closes[-1] if closes else 0.0
    lookback = max(1, int(cfg.consecutive_days))
    net_amount = sum(changes[-lookback:]) * price
    ratio = safe_ratio(net_amount, item.circ_mv or 0.0) if item.circ_mv else None
    return {"streak": min(1.0, streak / max(1, lookback)),
            "ratio": ratio,
            "cum_ratio": ratio,
            "raw_streak": float(streak),
            "trigger": None}


# ==================================================================
# 维度三：量价形态
# ==================================================================


def volume_price_raw(item: AccumulationInput, *, config: MainlineConfig
                     ) -> dict[str, float | None]:
    """量价形态识别（**不看任何原始成交量因子**）。

    形态 A「地量后放量 + 温和上行」：
        近 `new_low_window` 日里出现过"创阶段新低且成交量低于均量
        `shrink_ratio` 倍"的地量日，其后 `expand_min_days ~ expand_max_days`
        日内有至少 `expand_min_days` 天放量到均量 `expand_ratio` 倍以上，
        且从地量日至今的累计涨幅不超过 `max_rise_pct`（"温和"）。

    形态 B「底分型 + 放量反包」：
        最近 10 根 K 线里出现标准底分型（中间那根的 low 与 high **都**低于
        左右两根），且其后出现一根阳线**反包**前一根的开盘价并放量。

    两者都命中给 100；命中一个给 85；只具备部分特征（有地量无放量 /
    有底分型无反包）给 40-45 的"接近分"，让分数在横截面上有区分度 ——
    否则候选池里会出现大片完全相同的 0 分或 85 分，排序退化成随机。
    """
    cfg = config.accumulation.volume_price
    bars = item.series.bars
    need = max(int(cfg.shrink_window), int(cfg.new_low_window), 20) + 5
    if len(bars) < need:
        return {"form": None, "strength": None, "form_a": None, "form_b": None}
    form_a, strength_a = _form_shrink_then_expand(bars, cfg)
    form_b, strength_b = _form_bottom_reversal(bars, cfg)
    if form_a and form_b:
        score = 100.0
    elif form_a or form_b:
        score = 85.0
    elif strength_a > 0 or strength_b > 0:
        score = 30.0 + 15.0 * max(strength_a, strength_b)
    else:
        score = 0.0
    return {"form": score,
            "strength": round(100.0 * max(strength_a, strength_b), 4),
            "form_a": 1.0 if form_a else 0.0,
            "form_b": 1.0 if form_b else 0.0}


def _form_shrink_then_expand(bars: Sequence[Any], cfg: Any
                             ) -> tuple[bool, float]:
    """形态 A：地量（伴随阶段新低）→ 放量 → 温和上行。"""
    closes = [bar.close for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    shrink_window = max(5, int(cfg.shrink_window))
    baseline = mean(volumes[-shrink_window:])
    if not baseline or baseline <= 0:
        return False, 0.0
    new_low_window = max(5, int(cfg.new_low_window))
    low_edges = lows[-new_low_window:]
    if not low_edges:
        return False, 0.0
    stage_low = min(low_edges)
    start = len(bars) - new_low_window
    # 地量日：创阶段新低（low 距阶段最低 1% 以内）且成交量缩到均量的 shrink 倍以下
    quiet_index: int | None = None
    for offset, bar in enumerate(bars[start:]):
        if bar.low <= stage_low * 1.01 and bar.volume < baseline * float(cfg.shrink_ratio):
            quiet_index = start + offset
    if quiet_index is None:
        return False, 0.0
    expand_days = sum(
        1 for bar in bars[quiet_index + 1:]
        if bar.volume > baseline * float(cfg.expand_ratio))
    if expand_days < int(cfg.expand_min_days):
        # 有地量、没放量：给一个"接近分"而不是 0（0 会与"完全没形态"混同）
        return False, 0.4
    base_close = closes[quiet_index]
    if not base_close:
        return False, 0.5
    rise = closes[-1] / base_close - 1.0
    if rise > float(cfg.max_rise_pct) / 100.0:
        return False, 0.6      # 放量但已经涨过头，不是"温和上行"
    return True, 1.0


def _form_bottom_reversal(bars: Sequence[Any], cfg: Any) -> tuple[bool, float]:
    """形态 B：底分型 + 放量反包。"""
    if len(bars) < 5:
        return False, 0.0
    window = bars[-10:] if len(bars) >= 10 else bars
    pivot: int | None = None
    for index in range(1, len(window) - 1):
        left, mid, right = window[index - 1], window[index], window[index + 1]
        if mid.low < left.low and mid.low < right.low \
                and mid.high < left.high and mid.high < right.high:
            pivot = index
    if pivot is None:
        return False, 0.0
    after = window[pivot + 1:]
    if not after:
        return False, 0.5      # 底分型出现在最后一根上，还没有"反包"来确认
    reference = after[-2] if len(after) >= 2 else window[pivot]
    last = after[-1]
    if last.close <= last.open:
        return False, 0.5      # 底分型成立但没有阳线
    if reference.open and last.close <= reference.open:
        return False, 0.6      # 阳线但没有反包前一根的开盘
    if reference.volume and last.volume <= reference.volume * 1.2:
        return False, 0.7      # 反包但没放量
    return True, 1.0


# ==================================================================
# 维度四：ETF 资金异动
# ==================================================================


def etf_raw(item: AccumulationInput, *, config: MainlineConfig
            ) -> dict[str, float | None]:
    """ETF 资金异动的**原始量**（横截面分位由 `score_accumulation` 在池内算）。

    三个子因子都在 `etf.board_etf_signals` 里算好，这里只做搬运与缺数据标注：

        share   份额净申购率（小数）
        amount  成交额放量倍数
        streak  连续净申购天数
        level   异动级别 0/1/2（**最终分的加分**由 `etf.breakout_bonus` 用）
    """
    signal = item.etf
    if signal is None:
        return {"etf_share": None, "etf_amount": None, "etf_streak": None,
                "etf_breakout": None, "etf_level": 0.0}
    return {"etf_share": signal.share_change,
            "etf_amount": signal.amount_ratio,
            "etf_streak": float(signal.share_streak),
            "etf_breakout": 1.0 if signal.breakout else 0.0,
            "etf_level": float(signal.level_of())}


# ==================================================================
# 合成
# ==================================================================


def score_accumulation(inputs: Sequence[AccumulationInput], *,
                       config: MainlineConfig
                       ) -> dict[str, tuple[LayerScore, dict[str, float | None]]]:
    """对候选池内的板块算第二层三维得分。

    返回 `{board_code: (LayerScore, 因子原始值字典)}`，与 `six_dim` 同构，
    便于回测侧用同一段代码做因子相关性验证。
    """
    cfg = config.accumulation
    weights = cfg.normalized_weights()
    raws: dict[str, dict[str, float | None]] = {
        item.code: {} for item in inputs}
    for item in inputs:
        row = raws[item.code]
        row.update(leverage_raw(item, config=config))
        row.update(northbound_raw(item, config=config))
        row.update(volume_price_raw(item, config=config))
        row.update(etf_raw(item, config=config))

    # 池内排名：`change` 越大越好；`streak`（北向连续增加天数）越大越好
    change_col = [raws[item.code].get("change") for item in inputs]
    streak_col = [raws[item.code].get("raw_streak") for item in inputs]
    rank_col = [raws[item.code].get("cum_ratio") for item in inputs]
    share_col = [raws[item.code].get("etf_share") for item in inputs]
    etf_amount_col = [raws[item.code].get("etf_amount") for item in inputs]
    for item in inputs:
        row = raws[item.code]
        raw_streak = to_float(row.get("raw_streak")) or 0.0
        row["trigger"] = _leverage_trigger(
            row, item=item, config=config, changes=change_col,
            rank_values=change_col, streak=raw_streak)
        row["nb_rank"] = percentile_score(rank_col, row.get("cum_ratio"))
        row["streak_rank"] = percentile_score(streak_col, row.get("raw_streak"))
        row["change_rank"] = percentile_score(change_col, row.get("change"))
        row["etf_share_rank"] = percentile_score(share_col, row.get("etf_share"))
        row["etf_amount_rank"] = percentile_score(etf_amount_col,
                                                 row.get("etf_amount"))

    out: dict[str, tuple[LayerScore, dict[str, float | None]]] = {}
    for item in inputs:
        row = raws[item.code]
        dims: list[DimensionScore] = []
        for dim_key in cfg.DIMS:
            dim_weight = float(weights.get(dim_key, 0.0))
            parts: list[tuple[float, float, bool]] = []
            notes: list[str] = []
            if dim_key == "leverage":
                low = to_float(row.get("low"))
                for sub, weight in SUB_WEIGHTS["leverage"].items():
                    if sub == "change":
                        value = row.get("change_rank")
                    elif sub == "persistence":
                        value = (row.get("persistence") or 0.0) * 100.0 \
                            if row.get("persistence") is not None else None
                    else:
                        value = row.get("trigger")
                    parts.append((value or 0.0, weight, value is not None))
                if low is not None and low <= 0:
                    notes.append("板块指数不在近 60 日低位区域（建仓痕迹打折）")
                if low is None:
                    notes.append("板块指数历史不足，无法判定是否处于低位")
            elif dim_key == "northbound":
                for sub, weight in SUB_WEIGHTS["northbound"].items():
                    if sub == "streak":
                        value = row.get("streak_rank")
                    elif sub == "ratio":
                        value = row.get("nb_rank")
                    else:
                        value = row.get("trigger_nb") if "trigger_nb" in row \
                            else None
                    parts.append((value or 0.0, weight, value is not None))
                if row.get("cum_ratio") is None:
                    notes.append("北向持股序列为空（2024-08 后仅季末披露）")
            elif dim_key == "etf":
                for sub, weight in SUB_WEIGHTS["etf"].items():
                    if sub == "share":
                        value = row.get("etf_share_rank")
                    elif sub == "amount":
                        value = row.get("etf_amount_rank")
                    else:
                        raw_streak = to_float(row.get("etf_streak"))
                        value = (min(100.0, raw_streak / 3.0 * 100.0)
                                 if raw_streak is not None else None)
                    parts.append((value or 0.0, weight, value is not None))
                if row.get("etf_share") is None:
                    notes.append("该板块没有可用的 ETF 资金数据")
                else:
                    level = int(to_float(row.get("etf_level")) or 0)
                    if level >= 2:
                        notes.append("ETF 二级异动（历史级放量 + 份额净申购）")
                    elif level == 1:
                        notes.append("ETF 一级异动（历史级放量，份额未净申购）")
            else:
                for sub, weight in SUB_WEIGHTS["volume_price"].items():
                    value = row.get(sub)
                    parts.append((value or 0.0, weight, value is not None))
                if row.get("form") is None:
                    notes.append("板块指数历史不足，量价形态无法判定")
            score, coverage = weighted_score(parts)
            # 杠杆维度加"低位"折扣：高位放杠杆不是建仓痕迹，是追高
            if dim_key == "leverage" and to_float(row.get("low")) == 0.0:
                score *= 0.75
            # ETF 异动确认给下限：横截面分位在"全市场都没申赎"时会集体偏低，
            # 而 breakout 是**绝对口径**的确认，不该被相对分位压掉。
            #
            # ⚠️ 本维度权重已置 0（见 `AccumulationConfig`），这个下限只影响
            # **面板展示的维度分**，不参与任何合成 —— 真正起作用的是
            # `etf.breakout_bonus` 给出的最终分加分。
            if dim_key == "etf" and to_float(row.get("etf_level")) > 0:
                score = max(score, 75.0)
            dims.append(DimensionScore(
                key=dim_key, label=DIM_LABELS.get(dim_key, dim_key),
                score=clamp(score), weight=dim_weight,
                raw={key: (round(value, 6) if isinstance(value, float) else value)
                     for key, value in row.items()},
                note="；".join(notes),
                available=any(ok for _, _, ok in parts) and coverage > 0))
        layer_score, layer_coverage = weighted_score(
            [(dim.score, dim.weight, dim.available) for dim in dims])
        layer = LayerScore(key="accumulation", label="建仓痕迹",
                           score=layer_score, dimensions=dims,
                           weight=float(weights.get("accumulation", 0.0)),
                           coverage=layer_coverage,
                           notes=[f"{item.name}：{dim.label} {dim.score:.0f}"
                                  for dim in dims if dim.available][:3])
        out[item.code] = (layer, raws[item.code])
    return out


def _leverage_trigger(row: dict[str, float | None], *, item: AccumulationInput,
                      config: MainlineConfig, changes: Sequence[Any],
                      rank_values: Sequence[Any], streak: float
                      ) -> float | None:
    """杠杆资金的"触发"子因子：连续走强 + 处于池内前 N 名 + 板块在低位。

    三个条件都满足给满分；只满足一部分按比例给分。用 0.15 的小权重参与
    维度合成（它是"形态确认"而不是主要强度），这样即使条件全不满足，
    维度分也不会因为一个小权重项被拉到 0。
    """
    cfg = config.accumulation.leverage
    if row.get("change") is None:
        return None
    rank = percentile_score(rank_values, row.get("change"))
    if rank is None:
        return None
    top_zone = (100.0 - rank) < max(1, int(cfg.top_rank)) / max(len(rank_values), 1) * 100.0
    need = max(1, int(cfg.consecutive_days))
    persistent = streak >= need
    low = row.get("low")
    score = 0.0
    if top_zone:
        score += 40.0
    if persistent:
        score += 35.0
    if low == 1.0:
        score += 25.0
    return min(TRIGGER_SCORE, score)


def momentum_snapshot(item: AccumulationInput, *, window: int) -> float | None:
    """板块指数近 `window` 日涨幅（回测的持有期收益口径与它一致）。"""
    closes = [bar.close for bar in item.series.bars if bar.close > 0]
    if len(closes) < window + 1 or not closes[-1 - window]:
        return None
    return closes[-1] / closes[-1 - window] - 1.0


__all__ = [
    "DIM_LABELS",
    "SUB_WEIGHTS",
    "TRIGGER_SCORE",
    "AccumulationInput",
    "etf_raw",
    "leverage_raw",
    "momentum_snapshot",
    "northbound_raw",
    "score_accumulation",
    "volume_price_raw",
]
