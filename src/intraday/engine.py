"""做T信号引擎：关键价位计算 → 多因子合成总分 → 信号触发（含止损硬约束）。

一、总分
    总分 = Σ(因子得分 × 权重)，因子得分∈[-1,1]，权重合计=100 → 总分∈[-100,100]
    前端「多指标合成打分」表格的每一行贡献分相加，恰好等于总分（口径一致）。

二、阈值与信号
    动手线 ±action（默认30）：|总分|≥30 且价格触及关键档位 → 实心三角（正式信号）
    提示线 ±hint （默认20）：hint≤|总分|<action 且价格触及档位 → 空心三角（小仓位试仓）

三、止损硬约束（安全关键，任何情况下不可绕过）
    价格 ≤ 止损位 → 强制卖出警告（forced_exit），且**禁止一切低吸信号**：
    在止损判定中低吸分支根本不会被求值，杜绝「越跌越买」的越套越深路径。

四、数据覆盖度护栏
    因子数据缺口使有效权重不足时（coverage<0.7），实心信号自动降级为空心并说明原因，
    防止「缺了最高权重因子却照样打满分」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.intraday import indicators as ind
from src.intraday.config import FACTOR_LABELS, IntradayConfig
from src.intraday.factors import (
    FACTOR_ORDER,
    FactorContext,
    FactorOutcome,
    run_factor,
)
from src.intraday.models import (
    FactorScore,
    LevelSet,
    ScoreCard,
    ScoreZone,
    SignalMarker,
    TradeSignal,
)

# 有效权重低于该比例时禁止实心信号（数据缺口过大时不允许「正式动手」）
MIN_COVERAGE_FOR_SOLID = 0.7

# 档位之间、档位与现价之间的最小间距（做T一轮摩擦成本约 0.2%）
_MIN_LEVEL_GAP = 0.002
# 判定"真实破位"的余量：收盘价要跌破当日最低的 0.3% 才算破位（留出噪声余量）
_STOP_BELOW_LOW_GAP = 0.003


def compute_levels(
    *,
    price: float,
    box_high: float | None,
    box_low: float | None,
    box_span_days: int,
    boll_upper: float | None = None,
    boll_mid: float | None = None,
    boll_lower: float | None = None,
    pct_b: float | None = None,
    bandwidth: float | None = None,
    vwap: float | None = None,
    atr: float | None = None,
    day_low: float | None = None,
    config: IntradayConfig,
) -> LevelSet:
    """计算做T关键价位（低吸线/高抛线/止损位）。

    档位口径：
      - 低吸线 = 箱体下沿；开启 blend_boll_bands 且布林下轨**确实在现价下方**时取高者；
      - 高抛线 = 箱体上沿 + take_profit_buffer_pct；融合时取布林上轨中较低者；
      - 止损位 = 低吸线 - max(低吸线×stop_loss_pct%, atr_stop_mult×ATR)，
        且**必须低于当日已成交低点**（见下）。

    ## 为什么要有这三道护栏（2026-09-16 实盘事故）

    **现象**：几乎每只自选股的早盘低位都冒出一整排红色实心倒三角（强制止损），
    而那些位置恰恰是全天最好的低吸区。

    **实测根因**（300308，2026-09-16）：

        箱体 804.02~949.73(20日)   布林 下902.06 中905.55 上909.04（带宽仅 0.77%）
        当日开盘 869.02   当日最低 867.44
        → 低吸线 = max(箱体下沿804, 布林下轨902) = 902   ← 布林是**当日分钟bar**算的
        → 档位差护栏再围绕中轨扩张 → 低吸 898.75
        → 止损 = 898.75 × 0.99 = **889.76**（高于当日最低 867.44）
        → 开盘那一刻就"已跌破止损" → 27% 的分时点被判 forced_exit

    1. **布林轨是当日分钟口径**：强势股刚突破时 20 根bar带宽会窄到 1% 以内，
       下轨跑到当日开盘价**上方**。那不是支撑，取 max 只会把低吸线抬到现价上方。
    2. **止损是风控线不是预测线**：它必须低于当日**已经成交过**的区间，否则
       "开盘即跌破"，信号失去意义。低于当日低点是"破位才走"的口径。
    3. **固定百分比止损距离对高波动股形同噪声**：300308 的日线 ATR = 52.8（占价 5.8%），
       而默认 stop_loss_pct=1%，任何波动都能打穿。故取 max(百分比, ATR 比例)。
    """
    params = config.levels
    if box_low is None or box_high is None:
        # 日线缺失时用现价上下 stop_loss_pct 兜底成「窄箱体」，并让上层标注缺口
        box_low = price * (1.0 - 0.01)
        box_high = price * (1.0 + 0.01)
    low_buy = float(box_low)
    if (params.blend_boll_bands and boll_lower is not None
            and math.isfinite(float(boll_lower))
            and float(boll_lower) < price):
        # 护栏 1：只有确实在现价**下方**的布林下轨才算"支撑"候选。
        # 不要求留出摩擦成本间距 —— 那是 min_band_pct 护栏的职责，在这里顺手否掉
        # 会把有用的"窄带"信息一起丢掉（实测：863.10 对现价 864 是有效的近端支撑）。
        low_buy = max(low_buy, float(boll_lower))
    high_sell = float(box_high) * (1.0 + params.take_profit_buffer_pct / 100.0)
    if (params.blend_boll_bands and boll_upper is not None
            and math.isfinite(float(boll_upper))
            and float(boll_upper) > price):
        high_sell = min(high_sell, float(boll_upper))
    # 档位不可交叉：若融合后低吸线≥高抛线，退回纯箱体口径
    if low_buy >= high_sell:
        low_buy, high_sell = float(box_low), float(box_high) * (
            1.0 + params.take_profit_buffer_pct / 100.0)
    # 档位差护栏（围绕融合中点对称扩张/收缩）：
    #   下限——做T一轮摩擦成本约0.2%，档位差过小会让信号被成本吃光；
    #   上限——箱体过宽时档位离现价太远会永不触发，失去做T意义。
    low_buy, high_sell = _enforce_band_width(
        low_buy, high_sell, price, params)
    # 护栏 2：低吸线的语义是"跌到这里的支撑"，必须在现价下方。跳空高开 / 强势股
    # 盘中上冲时，连箱体下沿与布林下轨都会落到现价上方 —— 这时**不能**把线钉在
    # 现价附近当作替身：贴线判定是 `price ≤ low_buy × (1+touch_band)`，
    # 若低吸线取 `price × (1-touch_band)`，两者相乘恰好等于现价（0.997×1.003≈1），
    # 结果是"**永远差一线、低吸信号实际被做死**"。实测（2026-09-16）：
    # 300308/603083 当日触及低吸线 0 次，而这正是用户报"该低吸的位置没信号"的机制。
    #
    # 正确做法是给它一个**按波动率定的真实位置**：现价下方 dip_fallback_atr×ATR
    # （603083 的 0.3×ATR≈4.4 → 低吸线 215.5，恰好是当日最低 215.44 —— 价格真的会碰到它）。
    if low_buy >= price * (1.0 - _MIN_LEVEL_GAP):
        fallback = None
        if (atr is not None and math.isfinite(float(atr)) and float(atr) > 0
                and params.dip_fallback_atr > 0):
            fallback = float(atr) * params.dip_fallback_atr
        if fallback is None or fallback <= 0:
            # 没有 ATR（日线不足）时退化为"箱体宽度的一个比例"，仍保持真实距离
            fallback = max(price * 0.01, abs(price - float(box_low)) * 0.25)
        low_buy = min(price - fallback, high_sell * (1.0 - _MIN_LEVEL_GAP))
    # 止损距离 = max(百分比口径, ATR 口径)：高波动股的固定 1% 会被噪声打穿
    stop_distance = low_buy * params.stop_loss_pct / 100.0
    basis = f"低吸线下方 {params.stop_loss_pct:g}%"
    if (atr is not None and math.isfinite(float(atr)) and params.atr_stop_mult > 0
            and float(atr) * params.atr_stop_mult > stop_distance):
        stop_distance = float(atr) * params.atr_stop_mult
        basis = (f"低吸线下方 {params.atr_stop_mult:g}×ATR"
                 f"（ATR={float(atr):.2f}，大于 {params.stop_loss_pct:g}% 的"
                 f"{low_buy * params.stop_loss_pct / 100.0:.2f}）")
    stop_loss = low_buy - stop_distance
    if day_low is not None and math.isfinite(float(day_low)) and float(day_low) > 0:
        basis += f"；当日最低 {float(day_low):.2f}"
    return LevelSet(
        price=round(price, 4),
        box_high=round(float(box_high), 4),
        box_low=round(float(box_low), 4),
        box_position=None if None in (box_high, box_low)
        else _round_opt(ind.box_position(price, float(box_high), float(box_low)), 4),
        box_span_days=box_span_days,
        low_buy=round(low_buy, 4),
        high_sell=round(high_sell, 4),
        stop_loss=round(stop_loss, 4),
        stop_loss_pct=params.stop_loss_pct,
        stop_basis=basis,
        boll_upper=_round_opt(boll_upper, 4),
        boll_mid=_round_opt(boll_mid, 4),
        boll_lower=_round_opt(boll_lower, 4),
        pct_b=_round_opt(pct_b, 4),
        bandwidth=_round_opt(bandwidth, 6),
        vwap=_round_opt(vwap, 4),
        atr=_round_opt(atr, 4),
    )


def apply_level_fit(
    base: LevelSet, fit: Any, ctx: Any, *,
    adjustment_scale: dict[str, float] | None = None,
    blend: float = 1.0,
) -> LevelSet:
    """把**拟合出的档位**套到现价上，并用「调整项」做后置微调（用户口径）。

    分工（与用户给的两段口径一一对应）：

    | 阶段 | 输入 | 产物 |
    |---|---|---|
    | 拟合（主） | 7 个客观维度（筹码/箱体/缠论/VWAP/布林/MACD/KDJ·RSI） | 低吸/高抛/止损三条线的**尺度**（相对当日均价%） |
    | 调整（后置） | 其余 7 个维度（指数量能/消息面/市场情绪/情绪周期/海外映射/板块排行/股性） | 对上述尺度的**±50% 以内**微调 |

    实现要点：

    1. 拟合结果里记的是**相对当日均价的百分比**（见 `level_fit.fit_levels`），
       这里换成绝对价格：`均价 ×(1 ± pct)`。用均价而不是箱体，是因为拟合就是在
       "相对当日成本中枢"这个参照系里学的，换参照物会让训练与推理口径不一致。
    2. `adjustment_scale` 由 `level_fit.adjustment_factors` 给出（结构侧 + 环境侧 +
       微观侧三个乘数）。它只**缩放线的远近**，不会把线翻到另一侧。
    3. 拟合与调整后仍要过**三条硬约束**（低吸<高抛、止损在低吸下方、与现价留出
       最小间距）—— 拟合是统计结论，约束是风控底线，后者优先。
    4. `blend` 是"拟合占比"：<1 时与规则口径的档位按比例混合，便于灰度对比。
    """
    if fit is None or not getattr(fit, "metrics", None) or not fit.metrics.available:
        return base
    if not fit.metrics.gate_passed:
        # 闸门没过：不启用拟合档位（回退规则口径）。这是本模块最重要的安全阀。
        return base
    low_pct = _fit_pct(fit, "low")
    high_pct = _fit_pct(fit, "high")
    stop_pct = _fit_pct(fit, "stop")
    if low_pct is None or high_pct is None or stop_pct is None:
        return base

    scale = adjustment_scale or {}
    structure = max(0.5, min(1.5, float(scale.get("structure", 1.0))))
    environment = max(0.5, min(1.5, float(scale.get("environment", 1.0))))
    micro = max(0.5, min(1.5, float(scale.get("micro", 1.0))))
    # 低吸距离：负向环境 → 跌得更深才接（更谨慎）；正向环境 → 适度提前接
    width_scale = structure * (1.0 + 0.25 * (environment - 1.0))
    low_pct = low_pct * width_scale * micro
    high_pct = high_pct * structure * micro
    stop_pct = stop_pct * max(1.0, structure)

    # 三条线的次序与间距：低吸<高抛、止损<低吸，且价差不为负
    if high_pct <= low_pct:
        high_pct = low_pct * 1.05 + 0.05
    if stop_pct <= low_pct:
        stop_pct = low_pct * 1.05 + 0.05

    day_mean = _fit_reference_price(ctx, base)
    if day_mean is None or day_mean <= 0:
        return base
    price = float(ctx.price) if getattr(ctx, "price", None) else base.price
    fitted_low = day_mean * (1.0 - low_pct / 100.0)
    fitted_high = day_mean * (1.0 + high_pct / 100.0)
    fitted_stop = day_mean * (1.0 - stop_pct / 100.0)

    # 与规则口径混合（blend=1 时完全用拟合线）
    ratio = max(0.0, min(1.0, float(blend)))
    low_buy = base.low_buy * (1.0 - ratio) + fitted_low * ratio
    high_sell = base.high_sell * (1.0 - ratio) + fitted_high * ratio
    stop_loss = base.stop_loss * (1.0 - ratio) + fitted_stop * ratio

    # ---- 风控底线（与 compute_levels 的护栏同口径）----
    low_buy = min(low_buy, high_sell * (1.0 - _MIN_LEVEL_GAP))
    if low_buy >= price * (1.0 - _MIN_LEVEL_GAP):
        low_buy = price * (1.0 - max(0.01, low_pct / 100.0))
    stop_loss = min(stop_loss, low_buy - max(1e-6, low_buy * 0.002))
    if stop_loss <= 0:
        return base

    note = (f"档位：神经网络拟合（留一日成功率 "
            f"{_pct_text(fit.metrics.walk_forward_rate)}）"
            f"+ 调整项 ×{width_scale:.2f}/×{structure:.2f}/×{micro:.2f}")
    return base.model_copy(update={
        "low_buy": round(low_buy, 4),
        "high_sell": round(high_sell, 4),
        "stop_loss": round(stop_loss, 4),
        "low_source": (f"神经网络拟合：低吸 −{low_pct:.2f}%（相对当日均价）"
                       f"；底座 {base.low_source or '规则档位'}"),
        "high_source": (f"神经网络拟合：高抛 +{high_pct:.2f}%（相对当日均价）"
                        f"；底座 {base.high_source or '规则档位'}"),
        "stop_basis": (f"神经网络拟合：止损 −{stop_pct:.2f}%"
                       f"；低吸线下方 {stop_pct - low_pct:.2f}%（原口径 "
                       f"{base.stop_basis or '百分比/ATR'}）"),
        "level_fit_note": note,
    })


def _fit_pct(fit: Any, key: str) -> float | None:
    """从拟合结果里取"相对当日均价的百分比"（每条线取混合权重最大的那个锚点）。"""
    mix = getattr(fit, f"{key}_mix", None) or []
    anchors = getattr(fit, f"{key}_anchors", None) or []
    if not mix or not anchors or len(mix) != len(anchors):
        return None
    index = max(range(len(mix)), key=lambda i: mix[i])
    try:
        value = float(anchors[index])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _fit_reference_price(ctx: Any, base: LevelSet) -> float | None:
    """拟合线的参照价：当日均价（VWAP）优先，缺失时退回现价。"""
    vwap = getattr(ctx, "vwap", None)
    if vwap is not None and math.isfinite(float(vwap)) and float(vwap) > 0:
        return float(vwap)
    price = getattr(ctx, "price", None)
    if price is not None and math.isfinite(float(price)) and float(price) > 0:
        return float(price)
    return base.price if base.price and base.price > 0 else None


def _pct_text(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _enforce_band_width(    low_buy: float, high_sell: float, price: float, params: Any,
) -> tuple[float, float]:
    """把低吸~高抛档位差夹到 [min_band_pct, max_band_pct]×现价 区间内。

    围绕档位中点或现价对称调整：
      - 过窄 → 围绕**原档位中点**向外扩张（保留「箱体与布林中更早触及」的原始意图）；
      - 过宽 → 围绕**现价**向内收缩（箱体过宽时原中点可能远离现价，
        若围绕中点收缩会出现「低吸线高于现价」这种永不触发的档位）。
    仅当配置的上下限有效时才调整。
    """
    if price <= 0:
        return low_buy, high_sell
    width = high_sell - low_buy
    min_width = price * params.min_band_pct / 100.0
    max_width = price * params.max_band_pct / 100.0
    if min_width > 0 and width < min_width:
        midpoint = (low_buy + high_sell) / 2.0
        return midpoint - min_width / 2.0, midpoint + min_width / 2.0
    if max_width > 0 and width > max_width:
        return price - max_width / 2.0, price + max_width / 2.0
    return low_buy, high_sell


def _round_opt(value: float | None, digits: int) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def compose_scorecard(ctx: FactorContext, config: IntradayConfig) -> ScoreCard:
    """跑七因子 → 组装打分卡（贡献分之和 == 总分，前端表格可直接对齐）。"""
    weights = config.weights.as_dict()
    factors: list[FactorScore] = []
    gaps: list[str] = []
    available_weight = 0.0
    total = 0.0
    for key in FACTOR_ORDER:
        outcome: FactorOutcome = run_factor(key, ctx, config.factors)
        weight = float(weights[key])
        contribution = outcome.score * weight
        # 累加「四舍五入后」的贡献分：保证前端表格逐行相加与总分**完全相等**，
        # 不留 0.01 级别的显示裂缝（否则用户会以为表格算错了）
        rounded = round(contribution, 2)
        if outcome.available:
            available_weight += weight
            total += rounded
        elif outcome.gap:
            gaps.append(f"{FACTOR_LABELS.get(key, key)}：{outcome.gap}")
        factors.append(FactorScore(
            key=key, label=FACTOR_LABELS.get(key, key), weight=weight,
            score=round(outcome.score, 4), contribution=rounded,
            detail=outcome.detail, inputs=outcome.inputs,
            available=outcome.available, gap=outcome.gap,
        ))
    total = round(total, 2)
    zone = zone_for(total, config)
    return ScoreCard(
        total=total,
        threshold_action=config.thresholds.action,
        threshold_hint=config.thresholds.hint,
        zone=zone,
        verdict=_verdict(total, zone, available_weight, config),
        factors=factors,
        weights_sum=round(sum(weights.values()), 4),
        available_weight=round(available_weight, 2),
        gaps=gaps,
    )


def zone_for(total: float, config: IntradayConfig) -> ScoreZone:
    """总分 → 区间（与阈值严格一致，供前端刻度条与测试复用）。"""
    action, hint = config.thresholds.action, config.thresholds.hint
    if total >= action:
        return "strong_buy_zone"
    if total >= hint:
        return "buy_zone"
    if total <= -action:
        return "strong_sell_zone"
    if total <= -hint:
        return "sell_zone"
    return "neutral"


def _verdict(total: float, zone: ScoreZone, available_weight: float,
             config: IntradayConfig) -> str:
    action, hint = config.thresholds.action, config.thresholds.hint
    coverage_note = (
        f"；有效权重 {available_weight:g}/100" if available_weight < 100 else "")
    if zone == "strong_buy_zone":
        return (f"总分 {total:+.1f} 突破动手线 +{action:g} → 偏多低吸区，"
                f"价格触及低吸档位可正式动手{coverage_note}")
    if zone == "buy_zone":
        return (f"总分 {total:+.1f} 位于提示线与动手线之间 → 偏多，"
                f"仅小仓位试仓{coverage_note}")
    if zone == "strong_sell_zone":
        return (f"总分 {total:+.1f} 跌破动手线 -{action:g} → 偏空高抛区，"
                f"价格触及高抛档位可正式减仓{coverage_note}")
    if zone == "sell_zone":
        return (f"总分 {total:+.1f} 位于 -{hint:g}～-{action:g} 之间 → 偏空，"
                f"仅小仓位试减{coverage_note}")
    return (f"总分 {total:+.1f} 处于震荡区间（|总分| < 提示线 {hint:g}）"
            f"→ 不建议大动{coverage_note}")


def decide_signal(
    *,
    price: float,
    scorecard: ScoreCard,
    levels: LevelSet,
    config: IntradayConfig,
    ts: str,
    dev_z: float | None = None,
) -> TradeSignal:
    """按阈值与关键档位决定做T信号（止损优先级最高，且禁止低吸）。

    判定顺序（顺序即优先级）：
      1) 价格 ≤ 止损位 → forced_exit 强制卖出警告（并标记禁止低吸）
      2) 触及低吸档位（价格≤低吸线+触及带宽，或VWAP偏离达极值）且总分≥提示线/动手线
      3) 触及高抛档位（价格≥高抛线-触及带宽，或VWAP偏离达极值）且总分≤-提示线/-动手线
      4) 否则无信号
    """
    band = config.levels.touch_band_pct / 100.0
    action, hint = config.thresholds.action, config.thresholds.hint
    total = scorecard.total
    coverage = scorecard.available_weight / 100.0
    gated = coverage < MIN_COVERAGE_FOR_SOLID

    # ---- 1) 止损硬约束：优先级最高，低吸分支不再求值 ----
    if price <= levels.stop_loss + 1e-9:
        return TradeSignal(
            kind="stop_loss", strength="forced_exit", triggered=True,
            price=round(price, 4), ts=ts, total_score=total,
            reason=(
                f"⚠️ 现价 {price:.2f} 已跌破止损位 {levels.stop_loss:.2f}"
                f"（低吸线 {levels.low_buy:.2f} 下方 {levels.stop_loss_pct:g}%）"
                f"→ 强制卖出警告，本模块禁止任何低吸信号"),
            blocked_by_stop_loss=True, target_level=levels.stop_loss,
        )

    z = dev_z if dev_z is not None else None
    extreme = config.levels.vwap_extreme_z
    touch_low_level = price <= levels.low_buy * (1.0 + band)
    touch_low_vwap = z is not None and z <= -extreme
    touch_high_level = price >= levels.high_sell * (1.0 - band)
    touch_high_vwap = z is not None and z >= extreme

    # ---- 2) 低吸 ----
    if (touch_low_level or touch_low_vwap) and total >= hint:
        solid = total >= action and not gated
        how = "价格触及低吸线" if touch_low_level else f"VWAP偏离达极值(z={z:.2f})"
        why = f"总分 {total:+.1f} ≥ 动手线 {action:g}" if total >= action else (
            f"总分 {total:+.1f} 位于提示线 {hint:g}～动手线 {action:g}")
        gate_note = (
            f"；但有效权重仅 {scorecard.available_weight:g}/100"
            f"（<{MIN_COVERAGE_FOR_SOLID * 100:g}），已降级为软提示" if gated
            and total >= action else "")
        return TradeSignal(
            kind="low_buy", strength="solid" if solid else "hollow",
            triggered=True, price=round(price, 4), ts=ts, total_score=total,
            reason=(f"{how}（{levels.low_buy:.2f}），{why} → "
                    f"{'实心三角：正式低吸做T' if solid else '空心三角：小仓位试仓'}"
                    f"{gate_note}"),
            blocked_by_stop_loss=False,
            target_level=levels.low_buy,
        )

    # ---- 3) 高抛 ----
    if (touch_high_level or touch_high_vwap) and total <= -hint:
        solid = total <= -action and not gated
        how = "价格触及高抛线" if touch_high_level else f"VWAP偏离达极值(z={z:.2f})"
        why = (f"总分 {total:+.1f} ≤ 动手线 -{action:g}" if total <= -action
               else f"总分 {total:+.1f} 位于 -{hint:g}～-{action:g}")
        gate_note = (
            f"；但有效权重仅 {scorecard.available_weight:g}/100"
            f"（<{MIN_COVERAGE_FOR_SOLID * 100:g}），已降级为软提示" if gated
            and total <= -action else "")
        return TradeSignal(
            kind="high_sell", strength="solid" if solid else "hollow",
            triggered=True, price=round(price, 4), ts=ts, total_score=total,
            reason=(f"{how}（{levels.high_sell:.2f}），{why} → "
                    f"{'实心三角：正式高抛做T' if solid else '空心三角：小仓位试减'}"
                    f"{gate_note}"),
            blocked_by_stop_loss=False,
            target_level=levels.high_sell,
        )

    # ---- 4) 无信号 ----
    if abs(total) < hint:
        reason = (
            f"总分 {total:+.1f} 未达提示线 ±{hint:g} → 震荡区间不动手"
            f"（低吸线 {levels.low_buy:.2f} / 高抛线 {levels.high_sell:.2f}）")
    elif total >= hint:
        reason = (
            f"总分 {total:+.1f} 偏多但价格未触及低吸线 {levels.low_buy:.2f}"
            f"（现价 {price:.2f}）→ 继续观察")
    else:
        reason = (
            f"总分 {total:+.1f} 偏空但价格未触及高抛线 {levels.high_sell:.2f}"
            f"（现价 {price:.2f}）→ 继续观察")
    return TradeSignal(
        kind="none", strength="none", triggered=False, price=round(price, 4),
        ts=ts, total_score=total, reason=reason, blocked_by_stop_loss=False,
    )


def build_markers(
    *,
    replay: Any,
    levels: LevelSet,
    scorecard: ScoreCard,
    config: IntradayConfig,
    limit: int = 60,
    levels_series: list[LevelSet] | None = None,
) -> list[SignalMarker]:
    """由逐bar重放结果生成分时图三角标记（供前端打点）。

    replay 为 DataFrame，需含列：ts / close / low / total（逐bar总分）。
    仅标记「分数达标 + 价格触及档位」的bar；止损触发点单独标记。
    与实时信号同源（同一批打分核 + 同一套阈值），保证图上标记可复现。

    ## 为什么要逐bar档位（`levels_series`，2026-09-16 实测）

    档位是**时刻量**：低吸线/止损位随 VWAP、布林、现价每分钟重算（实测 300308
    当日低吸线从 862 一路抬到 898）。而图上那条横线画的是**当前值**，
    拿它去判早盘的 bar 会得出完全错误的结论：

        300308 09:45 那根被标成"止损"（因为收盘 867 < 当前止损 872），
        而当刻的真实止损位是 **853** —— 价格其实在它上方 14 元，根本没破位。

    传 `levels_series` 后，逐bar用**当刻**的档位判定；不传则退回单一 levels
    （旧行为，仅供不关心时刻性的调用方使用）。
    """
    if replay is None or len(replay) == 0:
        return []
    band = config.levels.touch_band_pct / 100.0
    action, hint = config.thresholds.action, config.thresholds.hint
    markers: list[SignalMarker] = []
    running_low: float | None = None
    for position, row in enumerate(replay.itertuples(index=False)):
        total = getattr(row, "total", None)
        close = getattr(row, "close", None)
        ts = str(getattr(row, "ts", ""))
        if total is None or close is None or not math.isfinite(float(total)):
            continue
        total = float(total)
        close = float(close)
        bar_levels = levels
        if levels_series is not None and position < len(levels_series):
            bar_levels = levels_series[position]
        bar_low = getattr(row, "low", None)
        bar_low = close if bar_low is None or not math.isfinite(float(bar_low)) \
            else float(bar_low)
        bar_high = getattr(row, "high", None)
        bar_high = close if bar_high is None or not math.isfinite(float(bar_high)) \
            else float(bar_high)
        # 与**本bar之前**的最低价比较：本bar自己创的新低不算"破位"，
        # 要跌破此前的最低价 0.3% 才算（这才叫破位）。
        previous_low = running_low
        running_low = bar_low if running_low is None else min(running_low, bar_low)
        breakdown = (previous_low is not None
                     and close <= previous_low * (1.0 - _STOP_BELOW_LOW_GAP))
        if close <= bar_levels.stop_loss + 1e-9 and breakdown:
            markers.append(SignalMarker(
                ts=ts, price=round(close, 3), kind="stop_loss",
                strength="forced_exit", label="止损"))
            continue
        # 「价格触及档位」用**盘中极值**判定，而不是收盘价：
        # 低吸是"跌到支撑位就动手"，盘中砸到线下（哪怕收盘又收回去）就已经触及了。
        # 实测踩过：600176 早盘 W 底的最低价正好落在低吸线上，但每根 5 分钟bar的**收盘**
        # 都在线上方 → 判定"从未触及" → 用户看到的图上明明摸到了却没有任何信号。
        if bar_low <= bar_levels.low_buy * (1.0 + band) and total >= hint:
            solid = total >= action
            markers.append(SignalMarker(
                ts=ts, price=round(close, 3), kind="low_buy",
                strength="solid" if solid else "hollow",
                label=f"{'低吸' if solid else '低吸提示'} {total:+.0f}"))
        elif bar_high >= bar_levels.high_sell * (1.0 - band) and total <= -hint:
            solid = total <= -action
            markers.append(SignalMarker(
                ts=ts, price=round(close, 3), kind="high_sell",
                strength="solid" if solid else "hollow",
                label=f"{'高抛' if solid else '高抛提示'} {total:+.0f}"))
    return markers[-limit:]


def replay_levels(
    features: Any,
    *,
    box_high: float | None,
    box_low: float | None,
    box_span_days: int,
    atr: float | None,
    config: IntradayConfig,
) -> list[LevelSet]:
    """逐bar重算档位（低吸/高抛/止损），用于**按当时那一刻**回放与画图。

    档位是时刻量：随 VWAP / 布林 / 现价每分钟变化。实测 300308 2026-09-16
    低吸线从 862.09 一路抬到 898.63、止损从 853.47 抬到 889.64；603083 低吸线
    从 215.42 抬到 221.91。所以：

    - **回放**必须用当刻档位，否则早盘的正常回踩会被后来的高位止损追认成"破位"
      （图1 里 09:45 那个红色 ▼ 就是这么来的）；
    - **画图**要能看出档位随时间漂移，否则用户会以为"开盘就在低吸线以下"
      （那只是当前值横贯全天的错觉）。

    每个 bar 只用**当日截至该bar**的数据（含 running low），不引入未来信息。
    """
    levels_series: list[LevelSet] = []
    if features is None or len(features) == 0:
        return levels_series
    running_low: float | None = None
    for row in features.itertuples(index=False):
        close = float(row.close)
        low = getattr(row, "low", None)
        low = close if low is None or not math.isfinite(float(low)) else float(low)
        running_low = low if running_low is None else min(running_low, low)
        levels_series.append(compute_levels(
            price=close, box_high=box_high, box_low=box_low,
            box_span_days=box_span_days,
            boll_upper=getattr(row, "boll_upper", None),
            boll_mid=getattr(row, "boll_mid", None),
            boll_lower=getattr(row, "boll_lower", None),
            pct_b=getattr(row, "pct_b", None),
            bandwidth=getattr(row, "bandwidth", None),
            vwap=getattr(row, "vwap", None), atr=atr, day_low=running_low,
            config=config))
    return levels_series


@dataclass
class EngineOutput:
    """引擎一次完整输出的打包（便于service/测试消费）。"""

    scorecard: ScoreCard
    signal: TradeSignal
    markers: list[SignalMarker]


def run_engine(
    *,
    ctx: FactorContext,
    levels: LevelSet,
    config: IntradayConfig,
    ts: str,
    replay: Any = None,
    levels_series: list[LevelSet] | None = None,
) -> EngineOutput:
    """组合调用：打分卡 → 信号 → 图表标记。"""
    scorecard = compose_scorecard(ctx, config)
    signal = decide_signal(
        price=ctx.price, scorecard=scorecard, levels=levels, config=config,
        ts=ts, dev_z=ctx.dev_z)
    markers = build_markers(
        replay=replay, levels=levels, scorecard=scorecard, config=config,
        levels_series=levels_series)
    return EngineOutput(scorecard=scorecard, signal=signal, markers=markers)
