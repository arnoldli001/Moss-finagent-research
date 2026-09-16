"""十四因子打分（每个因子输出归一化到 [-1,1] 的得分）。

三层结构，避免公式重复实现：
  1. 打分核（*_score_from_* 纯标量函数）——**唯一**公式来源，全程无副作用；
  2. 单标的打分器（score_box / score_vwap / …）——读 FactorContext，产出得分+解释文本；
  3. 逐bar重放（src/intraday/features.py）——对历史每根bar复用同一批打分核，
     用于分时图三角标记与阈值回测（保证「图中标记的分数」与「实时分数」同源同口径）。

符号约定（全模块统一）：
  得分 > 0 → 对「低吸做T」有利（超跌/超卖/情绪暖/消息多）
  得分 < 0 → 对「高抛做T」有利（超买/冲高/跑输/消息空）

因子分四批接入：
  ① 原七因子：box/vwap/boll/macd/kdj_rsi/sentiment/news
  ② 市场环境三因子：index_volume/board_rank/overseas
  ③ 交易技能库四因子（2026-09-16）：chan/chip/cycle/character
     来源见 docs/SKILL_TO_T_FACTORS.md（每个因子的技能出处与阈值口径）

参考基准（用户给定的目标截图，回归校验用例见 tests/unit/test_intraday_factors.py）：
  box(70%位)=-0.10 | vwap(偏离-0.337%,z=-0.85)=+0.28 | boll=+0.19 | macd=+0.48
  kdj_rsi=-0.08 | sentiment(19/33上涨,跑输1.02%)=-0.18 | news(偏多5/偏空10)=-1.00
  该基准是**旧七因子口径**，校验用例固定用 legacy 权重算总分，不受新增因子影响。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.intraday import indicators as ind
from src.intraday.character import character_score_from
from src.intraday.config import (
    BoardRankParams,
    BollParams,
    BoxParams,
    ChanParams,
    CharacterParams,
    ChipParams,
    CycleParams,
    FactorParams,
    IndexVolumeParams,
    KdjRsiParams,
    MacdParams,
    NewsParams,
    OverseasParams,
    SentimentParams,
    VwapParams,
)
from src.intraday.weight_profiles import INTRADAY_FACTOR_ORDER


@dataclass
class FactorOutcome:
    """单因子打分结果（引擎据此组装 FactorScore）。"""

    score: float
    detail: str
    inputs: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    gap: str | None = None


@dataclass
class FactorContext:
    """七因子打分所需的全部输入（service 层一次性算好，避免因子间重复计算）。"""

    price: float
    vwap: float | None = None
    dev_z: float | None = None
    dev_pct: float | None = None
    box_high: float | None = None
    box_low: float | None = None
    box_position: float | None = None
    box_span_days: int = 0
    pct_b: float | None = None
    bandwidth: float | None = None
    bandwidth_pctl: float | None = None
    macd_intraday: pd.DataFrame | None = None
    macd_daily: pd.DataFrame | None = None
    kdj_intraday: pd.DataFrame | None = None
    rsi_intraday: pd.Series | None = None
    kdj_daily: pd.DataFrame | None = None
    rsi_daily: pd.Series | None = None
    breadth: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    board_name: str = ""
    board_change_pct: float | None = None
    stock_change_pct: float | None = None
    news_llm_score: float | None = None
    news_positive: int = 0
    news_negative: int = 0
    news_count: int = 0
    # ---- 新增三因子（市场环境维度）----
    # 指数量能
    index_code: str = ""
    index_name: str = ""
    index_change_pct: float | None = None
    index_volume_ratio: float | None = None
    index_volume_yesterday: float | None = None
    index_volume_projected: float | None = None
    index_elapsed_ratio: float = 0.0
    index_gap: str | None = None
    # 板块涨幅排行
    board_rank: str | None = None
    board_rank_percentile: float | None = None
    board_rank_gap: str | None = None
    # 海外映射
    overseas_available: bool | None = None
    overseas_overnight_pct: float | None = None
    overseas_intraday_pct: float | None = None
    overseas_symbols: list[str] = field(default_factory=list)
    overseas_gap: str | None = None
    # ---- 交易技能库四因子（2026-09-16 接入）----
    # 缠论结构：最后一个笔中枢的位置 + MACD 背驰
    chan_available: bool | None = None
    chan_zd: float | None = None
    chan_zg: float | None = None
    chan_divergence: str | None = None          # "top" / "bottom" / None
    chan_divergence_strength: float = 0.0
    chan_bars_used: int = 0
    chan_gap: str | None = None
    # 筹码量能结构：今日量能进度 × 放量位置
    chip_available: bool | None = None
    chip_volume_ratio: float | None = None      # 今日预测量能 / 近5日均量
    chip_position: float | None = None          # 现价在当日分时区间中的位置 0~1
    chip_turnover_pct: float | None = None
    chip_gap: str | None = None
    # 市场情绪周期：涨停家数/炸板率/连板高度 → 做T环境温度
    cycle_available: bool | None = None
    cycle_stage: str = ""
    cycle_temperature: float | None = None
    cycle_t_allowed: bool | None = None
    cycle_gates: list[str] = field(default_factory=list)
    cycle_gap: str | None = None
    # 股性适配：用该股自身日ATR归一化偏离
    character_available: bool | None = None
    character_atr_pct: float | None = None
    character_trend_efficiency: float | None = None
    character_t_friendly: int | None = None
    character_grade: str = ""
    character_regime: str = ""
    character_gap: str | None = None


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "缺失"
    return f"{value:.{digits}f}{suffix}"


# ==================================================================
# 打分核（唯一公式来源，标量纯函数）
# ==================================================================

def box_score_from_position(position: float, exponent: float = 2.5) -> float:
    """箱体位置 → 得分。

    score = -sign(p - 0.5) × |2(p - 0.5)|^exponent
      p=0.5 → 0（中枢，方向中性）；p→0 → +1（贴近下沿）；p→1 → -1（贴近上沿）
      exponent=2.5 时 p=0.7 → -0.10（与目标截图口径一致）
    非线性指数的作用：中枢附近得分衰减更快，只有真正贴近箱体边缘才给高分，
    避免「箱体中部」被误判为买卖点。
    """
    magnitude = abs(2.0 * (position - 0.5))
    return ind.clip(-math.copysign(magnitude ** exponent, position - 0.5))


def vwap_score_from_z(zscore: float, z_scale: float = 3.0) -> float:
    """VWAP偏离 z-score → 得分：clip(-z / z_scale)。

    负偏离（现价低于均价）→ 正分（超跌反弹预期）；z_scale=3 表示偏离达当日
    3倍标准差给满分。参考口径：z=-0.85 → +0.283 ≈ 截图的 0.28 ✓
    """
    if z_scale <= 0:
        return 0.0
    return ind.clip(-zscore / z_scale)


def boll_score_from_pct_b(pct_b: float, squeeze: bool = False,
                          squeeze_damp: float = 0.6) -> float:
    """布林%B → 得分：clip((0.5 - %B) × 2)，收口阶段按 squeeze_damp 打折。

    %B=0 → +1（触下轨，超卖）；%B=1 → -1（触上轨，超买）。
    收口（带宽分位极低）时价格常沿轨道钝化运行，均值回归信号可信度下降 → 主动降权。
    """
    raw = ind.clip((0.5 - pct_b) * 2.0)
    return raw * (squeeze_damp if squeeze else 1.0)


def macd_score_from_gap(gap_pct: float, gap_scale_pct: float,
                        cross_bonus: float = 0.0) -> float:
    """单周期 MACD 得分：(DIF-DEA)/price 线性映射 + 新交叉加成。"""
    if gap_scale_pct <= 0:
        return ind.clip(cross_bonus)
    return ind.clip(gap_pct / gap_scale_pct + cross_bonus)


def cross_bonus_for(state: str, bonus: float) -> float:
    """交叉状态 → 加成（金叉正、死叉负、无交叉0）。"""
    if state == "golden":
        return bonus
    if state == "dead":
        return -bonus
    return 0.0


def osc_score_from_jr(j_value: float | None, rsi_value: float | None,
                      rsi_scale: float = 40.0) -> float | None:
    """KDJ 的 J 值与 RSI 的平均超买超卖得分。

    J: clip((50 - J)/50)；RSI: clip((50 - RSI)/rsi_scale)。
    两者皆缺 → None（该周期不计入）。
    """
    parts: list[float] = []
    if j_value is not None and math.isfinite(j_value):
        parts.append(ind.clip((50.0 - j_value) / 50.0))
    if rsi_value is not None and math.isfinite(rsi_value) and rsi_scale > 0:
        parts.append(ind.clip((50.0 - rsi_value) / rsi_scale))
    if not parts:
        return None
    return sum(parts) / len(parts)


def blend_timeframes(intraday: float | None, daily: float | None,
                     w_intraday: float, w_daily: float) -> float | None:
    """多周期共振加权；缺一周期时自动由另一周期独担（权重归一）。"""
    if intraday is None and daily is None:
        return None
    if intraday is None:
        return ind.clip(float(daily))
    if daily is None:
        return ind.clip(float(intraday))
    total = w_intraday + w_daily
    if total <= 0:
        return ind.clip((float(intraday) + float(daily)) / 2.0)
    return ind.clip(
        (w_intraday * float(intraday) + w_daily * float(daily)) / total)


def sentiment_score_from(breadth: float | None, relative_strength_pct: float | None,
                         params: SentimentParams) -> float | None:
    """板块内涨跌家数 + 个股相对板块强度 → 得分。

    score = 加权平均[ 2×breadth-1 , clip(rs / rs_scale_pct) ]
    参考口径：19/33家上涨(0.576) 且跑输板块1.02%（等权）→
      0.5×0.152 + 0.5×(-0.51) = -0.179 ≈ 截图的 -0.18 ✓
    """
    pairs: list[tuple[float, float]] = []
    if breadth is not None and math.isfinite(breadth):
        pairs.append((params.breadth_weight, ind.clip((breadth - 0.5) * 2.0)))
    if relative_strength_pct is not None and math.isfinite(relative_strength_pct):
        scale = params.rs_scale_pct if params.rs_scale_pct > 0 else 2.0
        pairs.append((params.rs_weight, ind.clip(relative_strength_pct / scale)))
    weight_sum = sum(w for w, _ in pairs)
    if weight_sum <= 0:
        return None
    return ind.clip(sum(w * v for w, v in pairs) / weight_sum)


def news_count_score(positive: int, negative: int, saturation: float = 3.0) -> float:
    """偏多/偏空计数比 → [-1,1]（饱和映射，避免少量新闻给出极端分）。

    score = clip(saturation × (pos - neg) / (pos + neg))
    参考口径：偏多5 / 偏空10 → 3 × (-5)/15 = -1.00（与截图一致 ✓）
    """
    total = positive + negative
    if total <= 0:
        return 0.0
    return ind.clip(saturation * (positive - negative) / total)


def news_score_from(llm_score: float | None, positive: int, negative: int,
                    params: NewsParams) -> float:
    """消息面融合分：LLM 语义分与计数分加权（LLM 不可用时纯计数口径）。"""
    count = news_count_score(positive, negative, params.count_saturation)
    if llm_score is None or not math.isfinite(llm_score):
        return count
    total = params.llm_weight + params.count_weight
    if total <= 0:
        return count
    wl, wc = params.llm_weight / total, params.count_weight / total
    return ind.clip(wl * ind.clip(llm_score) + wc * count)


def bandwidth_is_squeeze(bandwidth_pctl: float | None, params: BollParams) -> bool:
    """带宽分位是否处于收口区（None 视为不是，避免误打折）。"""
    return bandwidth_pctl is not None and bandwidth_pctl < params.squeeze_percentile


# ==================================================================
# 单标的打分器
# ==================================================================

def score_box(ctx: FactorContext, params: BoxParams) -> FactorOutcome:
    """箱体/压力位（权重最高30）：现价在N日箱体中的位置。"""
    if ctx.box_position is None or ctx.box_high is None or ctx.box_low is None:
        return FactorOutcome(
            score=0.0, detail="箱体数据不足，本因子不计入",
            available=False, gap=f"日线样本不足（仅{ctx.box_span_days}根，至少5根）",
        )
    p = ctx.box_position
    score = box_score_from_position(p, params.exponent)
    zone = "贴近下沿（低吸空间）" if p < 0.35 else (
        "贴近上沿（高抛/透支）" if p > 0.65 else "箱体中枢（方向不明）")
    detail = (
        f"箱体 {_fmt(ctx.box_low)}~{_fmt(ctx.box_high)}（{ctx.box_span_days}日），"
        f"现价处于 {p * 100:.0f}% 位 → {zone}"
    )
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "box_high": round(ctx.box_high, 3), "box_low": round(ctx.box_low, 3),
            "position_pct": round(p * 100, 1), "exponent": params.exponent,
            "lookback_days": ctx.box_span_days,
        },
    )


def score_vwap(ctx: FactorContext, params: VwapParams) -> FactorOutcome:
    """VWAP偏离（权重20）：现价相对当日分时均价的偏离度z-score。"""
    if ctx.dev_z is None or ctx.vwap is None:
        return FactorOutcome(
            score=0.0, detail="VWAP数据不足，本因子不计入", available=False,
            gap="分时/VWAP样本不足（至少5根分钟线）",
        )
    score = vwap_score_from_z(ctx.dev_z, params.z_scale)
    if ctx.dev_z <= -1.5:
        note = "显著低于均价，超跌反弹预期强"
    elif ctx.dev_z <= -0.5:
        note = "低于均价，存在均值回归动能"
    elif ctx.dev_z < 0.5:
        note = "贴近均价，方向中性"
    elif ctx.dev_z < 1.5:
        note = "高于均价，冲高回落风险"
    else:
        note = "显著高于均价，短线透支"
    detail = (
        f"偏离分时均价 {_fmt(ctx.dev_pct, 3, '%')}"
        f"（z={_fmt(ctx.dev_z)}）→ {note}"
    )
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "vwap": round(ctx.vwap, 3),
            "deviation_pct": None if ctx.dev_pct is None else round(ctx.dev_pct, 4),
            "zscore": None if ctx.dev_z is None else round(ctx.dev_z, 3),
            "z_scale": params.z_scale,
        },
    )


def score_boll(ctx: FactorContext, params: BollParams) -> FactorOutcome:
    """布林带（权重15）：%B位置 + 带宽收口修正。"""
    if ctx.pct_b is None:
        return FactorOutcome(
            score=0.0, detail="布林带数据不足，本因子不计入", available=False,
            gap=f"分钟K线样本不足（至少{params.window}根）",
        )
    squeeze = bandwidth_is_squeeze(ctx.bandwidth_pctl, params)
    score = boll_score_from_pct_b(ctx.pct_b, squeeze, params.squeeze_damp)
    if ctx.pct_b <= 0.2:
        note = "贴近下轨，超卖"
    elif ctx.pct_b < 0.45:
        note = "中下轨之间，偏弱"
    elif ctx.pct_b <= 0.55:
        note = "中轨附近，中性"
    elif ctx.pct_b < 0.8:
        note = "中上轨之间，偏强"
    else:
        note = "贴近上轨，超买"
    damp_note = (
        f"；带宽分位 {_fmt(ctx.bandwidth_pctl, 2)} 处于收口区，信号打折"
        f"×{params.squeeze_damp:g}" if squeeze else ""
    )
    detail = f"%B={_fmt(ctx.pct_b)}（{note}），带宽 {_fmt(ctx.bandwidth, 4)}{damp_note}"
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "pct_b": round(ctx.pct_b, 3),
            "bandwidth": None if ctx.bandwidth is None else round(ctx.bandwidth, 5),
            "bandwidth_percentile": None if ctx.bandwidth_pctl is None
            else round(ctx.bandwidth_pctl, 3),
            "squeeze": squeeze,
        },
    )


def _macd_timeframe(
    frame: pd.DataFrame | None, price: float, params: MacdParams,
) -> tuple[float | None, str, dict[str, Any]]:
    """单周期 MACD：得分 + 中文状态 + 中间量。"""
    if frame is None or len(frame) < 2 or price <= 0:
        return None, "", {}
    dif = ind.last_value(frame["dif"])
    dea = ind.last_value(frame["dea"])
    if dif is None or dea is None:
        return None, "", {}
    gap_pct = (dif - dea) / price
    state, since = ind.macd_cross_state(frame)
    score = macd_score_from_gap(
        gap_pct, params.gap_scale_pct, cross_bonus_for(state, params.cross_bonus))
    label = {"golden": f"金叉{since}根", "dead": f"死叉{since}根",
             "above": "多头排列", "below": "空头排列"}.get(state, state)
    return score, label, {
        "dif": round(dif, 4), "dea": round(dea, 4),
        "gap_pct": round(gap_pct, 5), "state": state, "bars_since_cross": since,
    }


def score_macd(ctx: FactorContext, params: MacdParams) -> FactorOutcome:
    """MACD（权重10）：5分钟与日线共振（默认 5分钟0.6 / 日线0.4）。"""
    intraday, label_i, inputs_i = _macd_timeframe(
        ctx.macd_intraday, ctx.price, params)
    daily, label_d, inputs_d = _macd_timeframe(ctx.macd_daily, ctx.price, params)
    wi, wd = params.tf_weights.normalized()
    score = blend_timeframes(intraday, daily, wi, wd)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="MACD数据不足，本因子不计入", available=False,
            gap="分钟与日线样本均不足（日线至少26+9根）",
        )
    tone = "偏多" if score > 0.15 else ("偏空" if score < -0.15 else "中性")
    gap_text = inputs_i.get("gap_pct") if inputs_i else None
    detail = (
        f"5分钟 {label_i or '样本不足'}"
        f"（DIF-DEA相对价格 {_fmt(None if gap_text is None else gap_text * 100, 3, '%')}）；"
        f"日线 {label_d or '样本不足'} → 多周期{tone}"
    )
    return FactorOutcome(
        score=score, detail=detail,
        inputs={"intraday": inputs_i, "daily": inputs_d,
                "tf_weights": {"intraday": wi, "daily": wd}},
    )


def _oscillator_timeframe(
    kdj_frame: pd.DataFrame | None, rsi_series: pd.Series | None,
    params: KdjRsiParams,
) -> tuple[float | None, str, dict[str, Any]]:
    """单周期 KDJ(J)/RSI 平均分。"""
    j = ind.last_value(kdj_frame["j"]) if kdj_frame is not None and len(kdj_frame) else None
    r = ind.last_value(rsi_series)
    score = osc_score_from_jr(j, r, params.rsi_scale)
    if score is None:
        return None, "", {}
    return score, f"J={_fmt(j, 1)}/RSI={_fmt(r, 1)}", {
        "kdj_j": None if j is None else round(j, 2),
        "kdj_k": None if kdj_frame is None or not len(kdj_frame)
        else round(ind.last_value(kdj_frame["k"]) or 0.0, 2),
        "kdj_d": None if kdj_frame is None or not len(kdj_frame)
        else round(ind.last_value(kdj_frame["d"]) or 0.0, 2),
        "rsi": None if r is None else round(r, 2),
    }


def score_kdj_rsi(ctx: FactorContext, params: KdjRsiParams) -> FactorOutcome:
    """KDJ/RSI（权重10）：30分钟与日线超买超卖（默认各半）。"""
    intraday, label_i, inputs_i = _oscillator_timeframe(
        ctx.kdj_intraday, ctx.rsi_intraday, params)
    daily, label_d, inputs_d = _oscillator_timeframe(
        ctx.kdj_daily, ctx.rsi_daily, params)
    wi, wd = params.tf_weights.normalized()
    score = blend_timeframes(intraday, daily, wi, wd)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="KDJ/RSI数据不足，本因子不计入", available=False,
            gap="分钟与日线样本均不足",
        )
    tone = "超卖偏多" if score > 0.15 else ("超买偏空" if score < -0.15 else "中性")
    detail = f"30分钟 {label_i or '样本不足'}；日线 {label_d or '样本不足'} → {tone}"
    return FactorOutcome(
        score=score, detail=detail,
        inputs={"intraday": inputs_i, "daily": inputs_d,
                "tf_weights": {"intraday": wi, "daily": wd}},
    )


def score_sentiment(ctx: FactorContext, params: SentimentParams) -> FactorOutcome:
    """市场情绪（权重10）：板块内涨跌家数 + 个股相对板块强度。"""
    rs = None
    if ctx.stock_change_pct is not None and ctx.board_change_pct is not None:
        rs = ctx.stock_change_pct - ctx.board_change_pct
    score = sentiment_score_from(ctx.breadth, rs, params)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="板块/情绪数据不足，本因子不计入", available=False,
            gap="板块涨跌家数与个股相对强度均不可得",
        )
    notes: list[str] = []
    if ctx.breadth is not None:
        total = (ctx.up_count or 0) + (ctx.down_count or 0)
        notes.append(
            f"板块内 {ctx.up_count}/{total} 家上涨（占比 {ctx.breadth * 100:.0f}%）")
    if rs is not None:
        notes.append(
            f"个股 {ctx.stock_change_pct:+.2f}% vs 板块 {ctx.board_change_pct:+.2f}%"
            f"（{'跑赢' if rs >= 0 else '跑输'} {abs(rs):.2f}%）")
    tone = "偏暖" if score > 0.15 else ("偏冷" if score < -0.15 else "中性")
    return FactorOutcome(
        score=score, detail="；".join(notes) + f" → 情绪{tone}",
        inputs={
            "breadth": None if ctx.breadth is None else round(ctx.breadth, 4),
            "up_count": ctx.up_count, "down_count": ctx.down_count,
            "relative_strength_pct": None if rs is None else round(rs, 4),
            "breadth_score": None if ctx.breadth is None
            else round(ind.clip((ctx.breadth - 0.5) * 2.0), 3),
            "rs_score": None if rs is None
            else round(ind.clip(rs / params.rs_scale_pct), 3),
        },
    )


def score_news(ctx: FactorContext, params: NewsParams) -> FactorOutcome:
    """消息面（权重4）：DeepSeek 情绪分 × 偏多/偏空计数分融合。"""
    if ctx.news_count <= 0 and ctx.news_llm_score is None:
        return FactorOutcome(
            score=0.0, detail="无可用个股新闻，本因子不计入", available=False,
            gap="近24小时无个股新闻（新闻源不可用或确无新闻）",
        )
    count_score = news_count_score(
        ctx.news_positive, ctx.news_negative, params.count_saturation)
    score = news_score_from(
        ctx.news_llm_score, ctx.news_positive, ctx.news_negative, params)
    tone = "明显偏多" if score > 0.4 else "偏多" if score > 0.15 else (
        "明显偏空" if score < -0.4 else "偏空" if score < -0.15 else "中性")
    weight_note = "（无AI情绪分，纯计数口径）" if ctx.news_llm_score is None else ""
    detail = (
        f"偏多 {ctx.news_positive} / 偏空 {ctx.news_negative}"
        f"（共 {ctx.news_count} 条，计数分 {count_score:+.2f}）"
        f"{weight_note} → 消息面{tone}"
    )
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "llm_score": None if ctx.news_llm_score is None
            else round(ctx.news_llm_score, 3),
            "count_score": round(count_score, 3),
            "positive_count": ctx.news_positive,
            "negative_count": ctx.news_negative,
            "news_count": ctx.news_count,
        },
    )


# ==================== 8. 指数量能 ====================

def index_volume_score_from(
    change_pct: float | None, volume_ratio: float | None,
    params: IndexVolumeParams,
) -> float | None:
    """指数量价配合打分核：方向 × (0.5 + 0.5×量能强度)。

    - 放量上涨 → +1（流动性支持做多）
    - 缩量上涨 → 0（"上涨无量=见顶"，无量上涨不可靠）
    - 放量下跌 → -1（放量杀跌）
    - 缩量下跌 → 0（卖压不足，缩量阴跌）
    量能只放大/衰减方向分，**不单独给方向** —— 避免把"放量"本身当利好或利空。
    """
    if change_pct is None:
        return None
    scale = params.change_scale_pct or 1.0
    direction = ind.clip(change_pct / scale)
    if volume_ratio is None:
        return ind.clip(direction * 0.75)
    strength = ind.clip((volume_ratio - 1.0) / (params.volume_scale or 0.5))
    return ind.clip(direction * (0.5 + 0.5 * strength))


def score_index_volume(ctx: FactorContext,
                       params: IndexVolumeParams) -> FactorOutcome:
    """指数量能（权重8）：个股所属指数的量价配合。"""
    if ctx.index_change_pct is None:
        return FactorOutcome(
            score=0.0, detail="指数行情不可用，本因子不计入", available=False,
            gap=ctx.index_gap or "所属指数行情缺失")
    score = index_volume_score_from(
        ctx.index_change_pct, ctx.index_volume_ratio, params)
    assert score is not None
    ratio = ctx.index_volume_ratio
    ratio_text = "缺失" if ratio is None else f"{ratio:.2f}倍"
    if ratio is None:
        note = "量能数据缺失，按指数方向打折计分"
    elif ratio >= 1.1:
        note = "量能增量（放量）"
    elif ratio <= 0.9:
        note = "量能缩量"
    else:
        note = "量能与昨日持平"
    tone = "偏多" if score > 0.15 else ("偏空" if score < -0.15 else "中性")
    detail = (f"{ctx.index_name or '所属指数'} "
              f"{ctx.index_change_pct:+.2f}%，全天预测量能 {ratio_text}（{note}）"
              f" → {tone}")
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "index_code": ctx.index_code, "index_name": ctx.index_name,
            "index_change_pct": ctx.index_change_pct,
            "volume_ratio": None if ratio is None else round(ratio, 4),
            "yesterday_volume": ctx.index_volume_yesterday,
            "projected_volume": ctx.index_volume_projected,
            "elapsed_ratio": ctx.index_elapsed_ratio,
        })


# ==================== 9. 板块涨幅排行 ====================

def board_rank_score_from(
    rank: str | None, change_pct: float | None,
    params: BoardRankParams,
) -> float | None:
    """板块排行打分核：排名分位（主）+ 板块涨跌幅（次）。

    rank 形如 "10/390" → 分位 = 1-(名次-1)/(总数-1)，再线性映射到 [-1,1]。
    """
    from src.intraday.market import _parse_rank

    parts: list[tuple[float, float]] = []
    if rank:
        parsed = _parse_rank(rank)
        if parsed is not None:
            position, total = parsed
            percentile = 1.0 - (position - 1) / max(1, total - 1)
            parts.append((params.rank_weight, ind.clip((percentile - 0.5) * 2.0)))
    if change_pct is not None:
        scale = params.change_scale_pct or 2.0
        parts.append((params.change_weight, ind.clip(change_pct / scale)))
    if not parts:
        return None
    weight_sum = sum(w for w, _ in parts) or 1.0
    return ind.clip(sum(w * v for w, v in parts) / weight_sum)


def score_board_rank(ctx: FactorContext,
                     params: BoardRankParams) -> FactorOutcome:
    """板块涨幅排行（权重6）：关联板块涨跌幅 + 全A板块涨幅排名分位。"""
    score = board_rank_score_from(ctx.board_rank, ctx.board_change_pct, params)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="板块排名与涨跌幅均不可得，本因子不计入",
            available=False, gap=ctx.board_rank_gap or "板块涨幅排名缺失")
    rank_text = ctx.board_rank or "缺失"
    # 分位用于展示：优先用上下文里已算好的；缺失时自行从 rank 解析，
    # 避免出现「打分用了分位、明细却显示 —」的口径不一致。
    percentile = ctx.board_rank_percentile
    if percentile is None and ctx.board_rank:
        from src.intraday.market import _parse_rank

        parsed = _parse_rank(ctx.board_rank)
        if parsed is not None:
            position, total = parsed
            percentile = 1.0 - (position - 1) / max(1, total - 1)
    pctl_text = "—" if percentile is None else f"前{max(1, round((1 - percentile) * 100))}%"
    if percentile is None:
        note = "仅板块涨跌幅可用（排名缺失）"
    elif percentile >= 0.75:
        note = "板块涨幅排名居前，属当日强势板块"
    elif percentile <= 0.25:
        note = "板块涨幅排名靠后，属当日弱势板块"
    else:
        note = "板块涨幅排名居中"
    tone = "偏多" if score > 0.15 else ("偏空" if score < -0.15 else "中性")
    detail = (f"{ctx.board_name or '关联板块'} 涨跌 "
              f"{_fmt(ctx.board_change_pct, 2, '%')}，"
              f"全A板块排名 {rank_text}（{pctl_text}）"
              f"—— {note} → {tone}")
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "board_name": ctx.board_name, "rank": ctx.board_rank,
            "rank_percentile": None if percentile is None
            else round(percentile, 4),
            "board_change_pct": ctx.board_change_pct,
        })


# ==================== 10. 海外映射 ====================

def overseas_score_from(
    overnight_avg_pct: float | None, intraday_avg_pct: float | None,
    params: OverseasParams,
) -> float | None:
    """海外映射打分核：美股隔夜分与韩股盘中同步分加权。"""
    parts: list[tuple[float, float]] = []
    scale = params.scale_pct or 3.0
    if overnight_avg_pct is not None:
        parts.append((params.overnight_weight,
                      ind.clip(overnight_avg_pct / scale)))
    if intraday_avg_pct is not None:
        parts.append((params.intraday_weight,
                      ind.clip(intraday_avg_pct / scale)))
    if not parts:
        return None
    weight_sum = sum(w for w, _ in parts) or 1.0
    return ind.clip(sum(w * v for w, v in parts) / weight_sum)


def score_overseas(ctx: FactorContext, params: OverseasParams) -> FactorOutcome:
    """海外映射（权重6）：美股隔夜 + 韩股盘中同步。"""
    if ctx.overseas_available is False:
        return FactorOutcome(
            score=0.0, detail="海外映射行情不可用，本因子不计入", available=False,
            gap=ctx.overseas_gap or "无可用海外映射行情")
    score = overseas_score_from(
        ctx.overseas_overnight_pct, ctx.overseas_intraday_pct, params)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="未配置海外映射标的，本因子不计入", available=False,
            gap=ctx.overseas_gap or "该标的未配置海外映射（watchlist.overseas）")
    notes: list[str] = []
    if ctx.overseas_overnight_pct is not None:
        notes.append(f"美股映射隔夜均 {ctx.overseas_overnight_pct:+.2f}%")
    if ctx.overseas_intraday_pct is not None:
        notes.append(f"韩股盘中同步 {ctx.overseas_intraday_pct:+.2f}%")
    tone = "偏多" if score > 0.15 else ("偏空" if score < -0.15 else "中性")
    detail = "；".join(notes) + f" → 海外映射{tone}"
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "overnight_avg_pct": ctx.overseas_overnight_pct,
            "intraday_avg_pct": ctx.overseas_intraday_pct,
            "symbols": ctx.overseas_symbols,
        })


# ==================== 11. 缠论结构 ====================

def chan_score_from(
    *, price: float | None, zd: float | None, zg: float | None,
    divergence: str | None = None, divergence_strength: float = 0.0,
    overshoot: float = 0.5, divergence_weight: float = 0.5,
) -> float | None:
    """缠论结构打分核：**最后一个笔中枢**的位置分 + 背驰分。

    位置分的含义（与箱体因子形似但来源不同：中枢由「连续三笔重叠」结构定义，
    不是固定 N 日高低点，因此它会随走势生长而移动）：

        现价 < zd（中枢下方） → 正分，越深越正（超跌，等一买）
        现价 > zg（中枢上方） → 负分，越高越负（透支，防一卖/三买后追高）
        中枢内              → 轻微均值回归分（靠近下沿偏多、上沿偏空，系数0.6）

    `overshoot` 的作用：离开中枢超过 `overshoot × (zg-zd)` 后位置分**饱和**。
    没有这个上限时，一只连续拉升的强势股会被算出 p=8，判成"极端超买"，
    而实际上那不是超买、是趋势 —— 饱和后再由背驰项决定要不要反手。

    背驰分：底背驰 +strength（一买，低吸最有力的结构证据），顶背驰 -strength。
    两项**加权相加而非取其一**：位置与背驰方向冲突时（例如顶背驰却在中枢下方）
    得分自动互相抵消 —— 这正是「信号矛盾时降低把握」的期望行为，
    比强行裁决成某一侧更诚实。
    """
    if price is None or zd is None or zg is None or zg <= zd:
        return None
    span = zg - zd
    position = (price - zd) / span
    if position <= 0.0:
        position_score = ind.clip(-position / max(0.05, overshoot))
    elif position >= 1.0:
        position_score = -ind.clip((position - 1.0) / max(0.05, overshoot))
    else:
        position_score = -ind.clip((position - 0.5) * 2.0) * 0.6

    divergence_score = 0.0
    if divergence == "bottom":
        divergence_score = ind.clip(abs(divergence_strength))
    elif divergence == "top":
        divergence_score = -ind.clip(abs(divergence_strength))

    weight = ind.clip(divergence_weight)
    blended = (1.0 - weight) * position_score + weight * divergence_score
    return ind.clip(blended)


def score_chan(ctx: FactorContext, params: ChanParams) -> FactorOutcome:
    """缠论结构（技能库）：笔中枢位置 + MACD 背驰。"""
    if ctx.chan_available is False or ctx.chan_zd is None or ctx.chan_zg is None:
        return FactorOutcome(
            score=0.0, detail="缠论结构不可用，本因子不计入", available=False,
            gap=ctx.chan_gap or "K线样本不足以构建笔/中枢（至少30根）")
    score = chan_score_from(
        price=ctx.price, zd=ctx.chan_zd, zg=ctx.chan_zg,
        divergence=ctx.chan_divergence,
        divergence_strength=ctx.chan_divergence_strength,
        overshoot=params.overshoot, divergence_weight=params.divergence_weight)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="中枢区间非法（zg≤zd），本因子不计入", available=False,
            gap=ctx.chan_gap or "最后一个中枢的上下沿退化")
    span = ctx.chan_zg - ctx.chan_zd
    position = (ctx.price - ctx.chan_zd) / span if span > 0 else None
    if position is None:
        zone = "中枢退化"
    elif position < 0:
        zone = "中枢下方（超跌待一买）"
    elif position <= 1:
        zone = "中枢内部（震荡）"
    else:
        zone = "中枢上方（防透支/卖飞）"
    div_text = {
        "bottom": f"底背驰（强度 {ctx.chan_divergence_strength:.2f}）",
        "top": f"顶背驰（强度 {ctx.chan_divergence_strength:.2f}）",
    }.get(ctx.chan_divergence or "", "无背驰")
    pct_text = "—" if position is None else f"{position * 100:.0f}%"
    detail = (
        f"最后中枢 {ctx.chan_zd:.2f}~{ctx.chan_zg:.2f}（{ctx.chan_bars_used}根K线），"
        f"现价位于 {pct_text} 位 → {zone}；{div_text}")
    if ctx.chan_divergence and position is not None and (
            (ctx.chan_divergence == "top" and position < 0.5)
            or (ctx.chan_divergence == "bottom" and position > 0.5)):
        detail += "；⚠️ 位置与背驰方向矛盾，两项互相抵消（把握度下降）"
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "zd": round(ctx.chan_zd, 3), "zg": round(ctx.chan_zg, 3),
            "position": None if position is None else round(position, 4),
            "divergence": ctx.chan_divergence,
            "divergence_strength": round(ctx.chan_divergence_strength, 4),
            "bars_used": ctx.chan_bars_used,
            "divergence_weight": params.divergence_weight,
        })


# ==================== 12. 筹码量能结构 ====================

def chip_score_from(
    *, volume_ratio: float | None, position: float | None,
    turnover_pct: float | None = None, volume_ratio_scale: float = 1.0,
) -> float | None:
    """筹码/量能结构打分核：`方向 × (0.5 + 0.5×量能强度)`。

    与指数量能因子同形（一致性优先），但方向与强度都换成**个股自己的**：

    - 方向 = `clip((0.5 - position) × 2)`：现价在**当日分时区间**的位置。
      高位（position→1）方向 −1，低位（→0）方向 +1。
    - 强度 = `clip((量比 − 1) / scale)`：量比 = 今日预测量能 / 近5日均量。
      幅度因子 = `0.5 + 0.5 × 强度` ∈ [0,1]，**量能只放大方向，不单独给方向** ——
      放量本身既可能是吸筹也可能是派发，位置才决定它是哪一种：

        低位倍量（+0.8 × 1.00） → +0.80  吸筹/承接，低吸最有利
        高位倍量（−0.8 × 1.00） → −0.80  派发/出货，该高抛
        低位平量（+0.8 × 0.50） → +0.40  位置略偏多，但缺乏量能确认
        高位缩量（−0.8 × 0.25） → −0.20  无量滞涨，方向保留但强度大幅衰减
        任意位置极度缩量（×0）  →   0    没有量能信息 → 中性

    换手率只做**打折**不做方向：日换手 <1% 的票盘口太薄，挂单滑点会吃掉档位差，
    此时把得分打七折（不是反转），因为「流动性差」不构成看多或看空理由。
    """
    if volume_ratio is None or position is None:
        return None
    strength = ind.clip((volume_ratio - 1.0) / max(0.05, volume_ratio_scale))
    direction = ind.clip((0.5 - position) * 2.0)
    score = direction * (0.5 + 0.5 * strength)
    if turnover_pct is not None and turnover_pct < 1.0:
        score *= 0.7
    return ind.clip(score)


def score_chip(ctx: FactorContext, params: ChipParams) -> FactorOutcome:
    """筹码量能结构（技能库）：今日量能进度 × 放量位置。"""
    if ctx.chip_available is False or ctx.chip_volume_ratio is None \
            or ctx.chip_position is None:
        return FactorOutcome(
            score=0.0, detail="筹码量能数据不足，本因子不计入", available=False,
            gap=ctx.chip_gap or "缺少当日成交量或近5日均量")
    score = chip_score_from(
        volume_ratio=ctx.chip_volume_ratio, position=ctx.chip_position,
        turnover_pct=ctx.chip_turnover_pct,
        volume_ratio_scale=params.volume_ratio_scale)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="筹码量能数据不足，本因子不计入", available=False,
            gap=ctx.chip_gap or "量比或位置缺失")
    ratio = ctx.chip_volume_ratio
    tone = "放量" if ratio >= 1.1 else ("缩量" if ratio <= 0.9 else "平量")
    if ctx.chip_position >= params.high_position:
        where = "区间高位（该处放量偏派发）"
    elif ctx.chip_position <= params.low_position:
        where = "区间低位（该处放量偏吸筹）"
    else:
        where = "区间中部（位置信息弱）"
    turnover_text = (
        "" if ctx.chip_turnover_pct is None
        else f"，换手率 {ctx.chip_turnover_pct:.2f}%"
        + ("（<1%，盘口薄，得分打七折）" if ctx.chip_turnover_pct < 1.0 else ""))
    detail = (
        f"今日量能为近5日均量的 {ratio:.2f} 倍（{tone}），"
        f"现价处于当日 {ctx.chip_position * 100:.0f}% 位 → {where}{turnover_text}")
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "volume_ratio": round(ratio, 4),
            "position": round(ctx.chip_position, 4),
            "turnover_pct": ctx.chip_turnover_pct,
            "volume_ratio_scale": params.volume_ratio_scale,
        })


# ==================== 13. 市场情绪周期 ====================

def cycle_score_from(
    *, temperature: float | None, neutral: float = 50.0,
) -> float | None:
    """情绪周期温度 → 做T环境分：以中性温度为原点的分段线性映射。

        温度 100 → +1（环境极好，低吸胜率高）
        温度  50 →  0（中性）
        温度   0 → −1（退潮/冰点，做T大概率 T 反）

    分段而不是单条直线，是为了让 [50,100] 与 [0,50] 各自满量程：
    温度 88 与 62 的差别，应该和 38 与 12 的差别一样大 ——
    用单条 `(T-50)/50` 会让高温区间永远够不到 +1，主升期的机会被系统性低估。
    """
    if temperature is None:
        return None
    value = max(0.0, min(100.0, float(temperature)))
    if value >= neutral:
        span = max(1.0, 100.0 - neutral)
        return ind.clip((value - neutral) / span)
    span = max(1.0, neutral)
    return -ind.clip((neutral - value) / span)


def score_cycle(ctx: FactorContext, params: CycleParams) -> FactorOutcome:
    """市场情绪周期（技能库）：涨停家数/炸板率/连板高度 → 做T环境温度。"""
    if not params.enabled:
        return FactorOutcome(
            score=0.0, detail="情绪周期因子已关闭（configs），本因子不计入",
            available=False, gap="factors.cycle.enabled=false")
    if ctx.cycle_available is False or ctx.cycle_temperature is None:
        return FactorOutcome(
            score=0.0, detail="市场情绪周期不可用，本因子不计入", available=False,
            gap=ctx.cycle_gap or "涨停池/炸板池数据不可得")
    score = cycle_score_from(
        temperature=ctx.cycle_temperature, neutral=params.neutral_temperature)
    assert score is not None
    tone = "适合动手" if score > 0.15 else ("不宜做T" if score < -0.15 else "中性")
    gate_text = (
        "；⛔ 一票否决：" + "、".join(ctx.cycle_gates) if ctx.cycle_gates else "")
    veto_text = ""
    if params.veto_signals and ctx.cycle_t_allowed is False and not ctx.cycle_gates:
        veto_text = "；⛔ 该阶段禁止低吸做T（退潮/冰点）"
    detail = (
        f"周期阶段「{ctx.cycle_stage or '未知'}」，做T环境温度 "
        f"{ctx.cycle_temperature:.0f}/100 → {tone}{gate_text}{veto_text}")
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "stage": ctx.cycle_stage,
            "temperature": round(ctx.cycle_temperature, 2),
            "t_allowed": ctx.cycle_t_allowed,
            "gates": list(ctx.cycle_gates),
            "neutral": params.neutral_temperature,
        })


# ==================== 14. 股性适配 ====================

def score_character(ctx: FactorContext, params: CharacterParams) -> FactorOutcome:
    """股性适配（技能库）：偏离按该股**自身日ATR**归一化，按震荡/趋势缩放。

    与 VWAP 因子的分工：VWAP 因子问「今天偏了几个当日标准差」，
    本因子问「偏了几个**这只票的日常波动**」。前者早盘样本少会失真，
    后者跨日跨票稳定，因此两者不重复而是互补。
    """
    if ctx.character_available is False or ctx.character_atr_pct is None:
        return FactorOutcome(
            score=0.0, detail="股性画像不可用，本因子不计入", available=False,
            gap=ctx.character_gap or "日线样本不足以计算股性（至少60根）")
    score = character_score_from(
        dev_pct=ctx.dev_pct, atr_pct=ctx.character_atr_pct,
        trend_efficiency=ctx.character_trend_efficiency, scale=params.atr_scale)
    if score is None:
        return FactorOutcome(
            score=0.0, detail="VWAP偏离缺失，本因子不计入", available=False,
            gap="当日分时样本不足，无法计算相对均价的偏离")
    regime_text = {"swing": "震荡型", "trend": "趋势型",
                   "mixed": "混合型"}.get(ctx.character_regime, "未知")
    swing = (1.0 - ctx.character_trend_efficiency
             if ctx.character_trend_efficiency is not None else None)
    damp_text = (
        "（趋势型：偏离打折，防止把趋势当超买超卖）" if regime_text == "趋势型"
        else "（震荡型：偏离全额生效，回归概率高）" if regime_text == "震荡型" else "")
    friendly_text = (
        "" if ctx.character_t_friendly is None
        else f"；该股做T友好度 {ctx.character_t_friendly}/100")
    multiple = abs(ctx.dev_pct or 0.0) / max(0.01, ctx.character_atr_pct * params.atr_scale)
    detail = (
        f"该股日ATR占价 {ctx.character_atr_pct:.2f}%（波动等级「"
        f"{ctx.character_grade or '未知'}」），现价相对均价偏离 {_fmt(ctx.dev_pct, 3, '%')}"
        f" → 相当于 {multiple:.2f} 倍日常波动；{regime_text}{damp_text}{friendly_text}")
    return FactorOutcome(
        score=score, detail=detail,
        inputs={
            "atr_pct": round(ctx.character_atr_pct, 3),
            "trend_efficiency": ctx.character_trend_efficiency,
            "regime": ctx.character_regime, "grade": ctx.character_grade,
            "t_friendly": ctx.character_t_friendly,
            "atr_scale": params.atr_scale,
            "swing_ratio": None if swing is None else round(swing, 4),
        })


# ==================== 因子调度表 ====================

FACTOR_ORDER: tuple[str, ...] = INTRADAY_FACTOR_ORDER


def run_factor(key: str, ctx: FactorContext, params: FactorParams) -> FactorOutcome:
    """按因子键调用对应打分器（未知键抛 KeyError）。"""
    if key == "box":
        return score_box(ctx, params.box)
    if key == "vwap":
        return score_vwap(ctx, params.vwap)
    if key == "boll":
        return score_boll(ctx, params.boll)
    if key == "macd":
        return score_macd(ctx, params.macd)
    if key == "kdj_rsi":
        return score_kdj_rsi(ctx, params.kdj_rsi)
    if key == "sentiment":
        return score_sentiment(ctx, params.sentiment)
    if key == "news":
        return score_news(ctx, params.news)
    if key == "index_volume":
        return score_index_volume(ctx, params.index_volume)
    if key == "board_rank":
        return score_board_rank(ctx, params.board_rank)
    if key == "overseas":
        return score_overseas(ctx, params.overseas)
    if key == "chan":
        return score_chan(ctx, params.chan)
    if key == "chip":
        return score_chip(ctx, params.chip)
    if key == "cycle":
        return score_cycle(ctx, params.cycle)
    if key == "character":
        return score_character(ctx, params.character)
    raise KeyError(f"未知因子: {key}")
