"""日线做T加权决策分（日K级别：「这只票现在该不该做这一轮滚动」）。

## 为什么日K做T需要一个和分时不同的总分

分时的十四个因子回答的是「**今天在哪一档动手**」；日K的七个因子回答的是
「**这只票现在处在什么位置、值不值得滚动**」。两者的问题不同，因子集合、
阈值刻度、甚至"什么算好信号"都不同：

- 分时看的是分钟级偏离与盘口，日K看的是趋势结构、量柱攻防、身位高低；
- 分时阈值是 ±20/±30（一天几十个bar，噪声大）；日K阈值是 ±15/±25
  （一天只有一根bar，过度敏感会让用户天天改主意）。

## 与规则信号（B1-B15 / S1-S6）的关系：**并列而非替代**

`daily_signals.py` 那套 B/S 是**规则布尔**（条件全满足才触发），
本模块是**加权打分**（多维度各自给分再合成）。两者刻意不做成一条链：

- 规则信号回答"**满不满足某套具体战法的形态**"，可解释到每一条条件；
- 加权总分回答"**综合来看现在偏向低吸还是高抛**"，能容纳"条件只满足一半"的情形。

所以 `DailySnapshot` 里两者是并列字段。**不要**把加权总分接回 `_finish` 的
`triggered` 判定 —— 那会让"某个形态满足了三条中的两条"也发正式信号，
而项目里已经有回归测试专门钉住"缺 required 条件不得静默触发"这条底线。

## 数据来源与降级

全部来自日线（已在 `daily.py` 里算好的 enriched 表/量柱锚点/量价形态/位置），
情绪周期取自 `market_cycle`（东财池），股性取自 `character`（该股日线画像）。
任一维度不可用 → 贡献分 0、从 `available_weight` 中扣除、原因进 `gaps`，
**绝不填 0 冒充"算过了"**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.intraday import indicators as ind
from src.intraday.character import CharacterProfile
from src.intraday.market_cycle import MarketCycle
from src.intraday.models import FactorScore, ScoreCard, ScoreZone
from src.intraday.weight_profiles import DAILY_FACTOR_ORDER


@dataclass
class DailyFactorOutcome:
    """单因子打分结果（与分时的 FactorOutcome 同形，便于两处代码互相参照）。"""

    score: float
    detail: str
    inputs: dict[str, Any]
    available: bool = True
    gap: str | None = None


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _finite(value)
    if number is None:
        return "缺失"
    return f"{number:.{digits}f}{suffix}"


def _flag(row: Any, name: str) -> bool:
    value = getattr(row, name, None)
    if value is None:
        return False
    return bool(value) and value == value


# ==================================================================
# 打分核（纯标量，唯一公式来源）
# ==================================================================

def trend_score_from(*, price: float | None, ma5: float | None, ma10: float | None,
                     ma60: float | None, ma120: float | None,
                     scale_pct: float = 3.0) -> float | None:
    """趋势结构分：四条均线的**相对位置**，而不是价格涨跌幅。

    四个分量各占 1/4，全部用「相对偏离 / 刻度」映射到 [-1,1]：

        price vs MA5    短周期动能（偏离 3% 给满分）
        MA5   vs MA10   短期多头排列
        MA10  vs MA60   中期多头排列（刻度放宽到 6%）
        price vs MA120  年线位置（刻度放宽到 15%，年线偏离天然更大）

    为什么不用「近N日涨幅」：涨幅只说明过去，均线排列说明**成本结构**——
    做T最怕的是"底下全是获利盘"或"上面全是套牢盘"，只有排列能表达这件事。
    """
    if price is None or not price:
        return None
    parts: list[float] = []
    if ma5:
        parts.append(ind.clip((price / ma5 - 1.0) / (scale_pct / 100.0)))
    if ma5 and ma10:
        parts.append(ind.clip((ma5 / ma10 - 1.0) / (scale_pct / 100.0)))
    if ma10 and ma60:
        parts.append(ind.clip((ma10 / ma60 - 1.0) / (scale_pct * 2 / 100.0)))
    if ma120:
        parts.append(ind.clip((price / ma120 - 1.0) / (scale_pct * 5 / 100.0)))
    if not parts:
        return None
    return ind.clip(sum(parts) / len(parts))


def position_score_from(*, percentile: float | None, streak: int = 0,
                        from_high_pct: float | None = None) -> float | None:
    """位置与身位分：分位越低越偏多（有低吸空间），越高越偏空（透支/待兑现）。

    `streak`（当前连续涨停板数）是**额外的风险扣分**，不是线性项：
    连板越多，做T的赔率越差（一次 T 反就是 -10% 量级），
    而分位本身在连板股上会一直贴着 1.0，区分不出"3板"与"7板"。
    """
    if percentile is None:
        return None
    base = -ind.clip((percentile - 0.5) * 2.0) * 0.8
    penalty = 0.0
    if streak >= 3:
        penalty = 0.15 * min(4, streak - 2)
    elif streak == 2:
        penalty = 0.08
    # 贴着区间高点（≤2%）时再加一点"突破未确认"的折价：
    # 此时低吸线会被箱体上沿顶到现价上方，档位差不足以覆盖摩擦成本。
    if from_high_pct is not None and 0 <= -from_high_pct <= 2.0:
        penalty += 0.1
    return ind.clip(base - penalty)


def volume_price_score_from(*, anchor_status: str | None, anchor_kind: str | None,
                            pattern_direction: str | None,
                            is_shrink_volume: bool = False,
                            is_shrink_half: bool = False,
                            is_big_yin: bool = False,
                            is_high_volume: bool = False,
                            close_above_body_top: bool | None = None) -> float | None:
    """量价结构分：高量柱攻防 + 量价形态方向。

    `anchor_status`（有效支撑/待观察/已破位）来自 `volume.find_volume_anchors`，
    它回答的是"**当前这根高量柱的实顶/实底还在不在**"——
    实底是风险线、实顶是安全线，这是量柱体系里最硬的一条支撑压力。

    缩量阴线（S3 风控）给负分：缩量下跌说明没有承接而非抛压释放。
    """
    parts: list[tuple[float, float]] = []
    score_by_status = {"有效支撑": 0.6, "待观察": 0.0, "已破位": -0.8}
    if anchor_status in score_by_status:
        parts.append((0.5, score_by_status[anchor_status]))
    if anchor_kind == "倍量":
        # 倍量柱是资金进场的直接证据，但它只放大"位置"的方向，
        # 这里作为**独立的正向证据**保留，因为量柱体系把它当独立信号使用
        parts.append((0.15, 0.3))
    direction_score = {"bullish": 1.0, "bearish": -1.0,
                       "reversal": 0.0, "neutral": 0.0}.get(
                           pattern_direction or "", None)
    if direction_score is not None:
        parts.append((0.35, direction_score))
    if is_shrink_volume or is_shrink_half:
        parts.append((0.2, -0.5))
    if is_big_yin:
        parts.append((0.15, -0.6))
    if is_high_volume and close_above_body_top is False:
        parts.append((0.15, -0.5))
    if not parts:
        return None
    weight_sum = sum(weight for weight, _ in parts) or 1.0
    return ind.clip(sum(weight * value for weight, value in parts) / weight_sum)


def signal_rule_score_from(*, buy_signals: list[Any],
                           sell_signals: list[Any]) -> float | None:
    """规则信号质量分：B1-B15 与 S1-S6 的触发条数与满足度。

    只为**已触发**的信号计分（`triggered=True`），未触发的不给"部分分"——
    否则每只票都会因为"沾点边"而拿到一堆小分，总分退化成噪声。

    归一化用 3 条满仓：触发 3 条以上买入信号就接近满分。
    实测（`docs/INTRADAY_T_DESIGN.md` §11）里同时满足 3 条以上属于罕见情形，
    所以这个刻度不会长期饱和。
    """
    buys = [item for item in (buy_signals or []) if getattr(item, "triggered", False)]
    sells = [item for item in (sell_signals or []) if getattr(item, "triggered", False)]
    if not buys and not sells:
        return 0.0
    buy_weight = sum(max(0.0, _finite(getattr(item, "score", 0.0)) or 0.0)
                     for item in buys)
    sell_weight = sum(max(0.0, _finite(getattr(item, "score", 0.0)) or 0.0)
                      for item in sells)
    raw = (buy_weight - sell_weight) / 3.0
    return ind.clip(raw)


def character_daily_score_from(*, t_friendly: float | None,
                               trend_efficiency: float | None) -> float | None:
    """股性适配分（日线口径）：做T友好度 → 该股"值不值得滚动"。

    与分时口径的差别：分时的股性因子是**方向分**（用自身ATR归一化偏离），
    日线的股性因子是**可行性分** —— 一天只有一根bar，无法谈"日内偏离"，
    能做的是回答"这只票适合做T吗"：

        t_friendly 80 → +0.6   高振幅+强均值回归+活跃+有涨停基因 → 滚动效率高
        t_friendly 50 →  0.0   中性
        t_friendly 20 → -0.6   钝化或单边趋势 → 滚动只会做丢或做反

    趋势票额外扣 0.2：趋势效率高的票做T最容易卖飞（技能库反复强调的一条）。
    """
    if t_friendly is None:
        return None
    score = ind.clip((float(t_friendly) - 50.0) / 50.0)
    if trend_efficiency is not None and trend_efficiency > 0.55:
        score -= 0.2
    return ind.clip(score)


def daily_zone_for(total: float, action: float, hint: float) -> ScoreZone:
    """总分 → 分档（纯函数，便于把边界值钉在单测里）。

    与分时 `engine.zone_for` 的分档语义一致：动手线两侧是"正式"，提示线两侧是"观察"。
    """
    if total >= action:
        return "strong_buy_zone"
    if total >= hint:
        return "buy_zone"
    if total <= -action:
        return "strong_sell_zone"
    if total <= -hint:
        return "sell_zone"
    return "neutral"


def _count_limit_up_streak(frame: pd.DataFrame, tolerance: float = 0.3,
                           limit_pct: float = 10.0) -> int:
    """末尾连续涨停板数（从最后一根bar往前数）。"""
    if frame is None or len(frame) == 0 or "close" not in frame.columns:
        return 0
    close = pd.to_numeric(frame["close"], errors="coerce")
    pct = (close / close.shift(1) - 1.0) * 100.0
    threshold = limit_pct - tolerance
    streak = 0
    for value in reversed(list(pct)):
        if value == value and value >= threshold:
            streak += 1
        else:
            break
    return streak


# ==================================================================
# 合成
# ==================================================================

def compose_daily_scorecard(
    *,
    code: str,
    frame: pd.DataFrame,
    ma: dict[str, float | None],
    position: Any = None,
    anchors: list[Any] | None = None,
    pattern: Any = None,
    buy_signals: list[Any] | None = None,
    sell_signals: list[Any] | None = None,
    protective: dict[str, Any] | None = None,
    cycle: MarketCycle | None = None,
    character: CharacterProfile | None = None,
    chan_structure: Any = None,
    weights: dict[str, float],
    action: float = 25.0,
    hint: float = 15.0,
    limit_pct: float = 10.0,
) -> ScoreCard:
    """日线做T七因子 → 加权总分（与分时 `compose_scorecard` 同口径：Σ得分×权重）。"""
    from src.intraday.factors import chan_score_from, cycle_score_from

    # 本地别名：下面几十处 detail 文案都要格式化数字，短名字更好读
    fmt = _fmt

    last = frame.iloc[-1] if frame is not None and len(frame) else None
    price = _finite(getattr(last, "close", None)) if last is not None else None
    anchors = list(anchors or [])
    active = anchors[0] if anchors else None
    outcomes: dict[str, DailyFactorOutcome] = {}
    gaps: list[str] = []

    # ---- 1. 趋势结构 ----
    trend = trend_score_from(
        price=price, ma5=ma.get("MA5"), ma10=ma.get("MA10"),
        ma60=ma.get("MA60"), ma120=ma.get("MA120"))
    if trend is None:
        outcomes["trend"] = DailyFactorOutcome(
            0.0, "均线数据不足，本因子不计入", {}, available=False,
            gap="日线样本不足以计算 MA5/10/60/120")
    else:
        arrangement = "多头排列" if trend > 0.15 else (
            "空头排列" if trend < -0.15 else "均线纠缠")
        outcomes["trend"] = DailyFactorOutcome(
            trend,
            f"价格 {fmt(price)} vs MA5 {fmt(ma.get('MA5'))} / MA10 {fmt(ma.get('MA10'))}"
            f" / MA60 {fmt(ma.get('MA60'))} / MA120 {fmt(ma.get('MA120'))} → {arrangement}",
            {"ma": {k: v for k, v in ma.items()}, "price": price})

    # ---- 2. 缠论买卖点 ----
    chan_ok = (chan_structure is not None
               and getattr(chan_structure, "available", False)
               and getattr(chan_structure, "zd", None) is not None
               and getattr(chan_structure, "zg", None) is not None)
    if chan_ok and price:
        zd = float(chan_structure.zd)
        zg = float(chan_structure.zg)
        divergence = getattr(chan_structure, "divergence", None)
        divergence_strength = getattr(chan_structure, "divergence_strength", 0.0)
        value = chan_score_from(
            price=price, zd=zd, zg=zg,
            divergence=divergence, divergence_strength=divergence_strength)
        zone = "中枢下方" if price < zd else ("中枢上方" if price > zg else "中枢内部")
        div_text = "" if not divergence else (
            f"，{'底' if divergence == 'bottom' else '顶'}背驰"
            f"（强度 {divergence_strength:.2f}）")
        outcomes["chan_daily"] = DailyFactorOutcome(
            value or 0.0,
            f"日线最后中枢 {zd:.2f}~{zg:.2f}（共{getattr(chan_structure, 'pivot_count', 1)}个），"
            f"现价在{zone}{div_text}",
            {"zd": zd, "zg": zg, "divergence": divergence,
             "pivot_count": getattr(chan_structure, "pivot_count", None)})
    else:
        gap = getattr(chan_structure, "gap", None) if chan_structure else None
        outcomes["chan_daily"] = DailyFactorOutcome(
            0.0, "缠论结构不可用，本因子不计入", {}, available=False,
            gap=gap or "日线样本不足以构建笔/中枢")

    # ---- 3. 量价结构 ----
    body_top = _finite(getattr(last, "body_top", None)) if last is not None else None
    close_above = None if (price is None or body_top is None) else (price >= body_top)
    volume_score = volume_price_score_from(
        anchor_status=getattr(active, "status", None) if active else None,
        anchor_kind=getattr(active, "kind", None) if active else None,
        pattern_direction=getattr(pattern, "direction", None) if pattern else None,
        is_shrink_volume=_flag(last, "is_shrink_volume"),
        is_shrink_half=_flag(last, "is_shrink_half"),
        is_big_yin=_flag(last, "is_big_yin"),
        is_high_volume=_flag(last, "is_high_volume"),
        close_above_body_top=close_above)
    if volume_score is None:
        outcomes["volume"] = DailyFactorOutcome(
            0.0, "量价数据不足，本因子不计入", {}, available=False,
            gap="未识别出量柱锚点或量价形态")
    else:
        anchor_text = (
            f"活动量柱 {getattr(active, 'kind', '—')}（{getattr(active, 'status', '—')}，"
            f"实顶 {fmt(getattr(active, 'body_top', None))}）"
            if active else "无活动量柱")
        pattern_text = (
            f"；量价形态「{getattr(pattern, 'name', '')}」"
            f"（{getattr(pattern, 'direction', 'neutral')}）" if pattern else "")
        outcomes["volume"] = DailyFactorOutcome(
            volume_score, anchor_text + pattern_text,
            {"anchor_status": getattr(active, "status", None) if active else None,
             "anchor_kind": getattr(active, "kind", None) if active else None,
             "pattern": getattr(pattern, "code", None) if pattern else None,
             "pattern_direction": getattr(pattern, "direction", None) if pattern else None})

    # ---- 4. 位置与身位 ----
    percentile = _finite(getattr(position, "percentile", None)) if position else None
    from_high = _finite(getattr(position, "from_high_pct", None)) if position else None
    streak = _count_limit_up_streak(frame, limit_pct=limit_pct)
    pos_score = position_score_from(
        percentile=percentile, streak=streak, from_high_pct=from_high)
    if pos_score is None:
        outcomes["position"] = DailyFactorOutcome(
            0.0, "位置数据不足，本因子不计入", {}, available=False,
            gap="近120日区间分位不可得")
    else:
        label = getattr(position, "label", "中位") if position else "中位"
        streak_text = f"，当前 {streak} 连板（身位惩罚）" if streak >= 2 else ""
        outcomes["position"] = DailyFactorOutcome(
            pos_score,
            f"现价处于近{getattr(position, 'window', 120)}日区间 "
            f"{percentile * 100:.0f}% 位（{label}），距高点 {fmt(from_high, 2, '%')}"
            f"{streak_text}",
            {"percentile": percentile, "from_high_pct": from_high, "streak": streak})

    # ---- 5. 规则信号质量 ----
    rule_score = signal_rule_score_from(
        buy_signals=buy_signals, sell_signals=sell_signals)
    triggered_buy = [i for i in (buy_signals or []) if getattr(i, "triggered", False)]
    triggered_sell = [i for i in (sell_signals or []) if getattr(i, "triggered", False)]
    outcomes["signal_rule"] = DailyFactorOutcome(
        rule_score or 0.0,
        f"已触发买入信号 {len(triggered_buy)} 条"
        f"（{'、'.join(getattr(i, 'code', '') for i in triggered_buy) or '无'}）；"
        f"卖出/风控 {len(triggered_sell)} 条"
        f"（{'、'.join(getattr(i, 'code', '') for i in triggered_sell) or '无'}）",
        {"buy": [getattr(i, "code", "") for i in triggered_buy],
         "sell": [getattr(i, "code", "") for i in triggered_sell]})

    # ---- 6. 市场情绪周期 ----
    if cycle is not None and cycle.available:
        value = cycle_score_from(temperature=float(cycle.temperature))
        outcomes["cycle"] = DailyFactorOutcome(
            value or 0.0,
            f"周期阶段「{cycle.stage}」，做T环境温度 {cycle.temperature}/100"
            + ("；⛔ " + "、".join(cycle.gates) if cycle.gates else ""),
            {"stage": cycle.stage, "temperature": cycle.temperature,
             "t_allowed": cycle.t_allowed, "gates": list(cycle.gates)})
    else:
        outcomes["cycle"] = DailyFactorOutcome(
            0.0, "市场情绪周期不可用，本因子不计入", {}, available=False,
            gap=(cycle.gap if cycle is not None else None) or "涨停池数据不可得")

    # ---- 7. 股性适配 ----
    if character is not None and character.available:
        value = character_daily_score_from(
            t_friendly=character.t_friendly,
            trend_efficiency=character.trend_efficiency)
        outcomes["character"] = DailyFactorOutcome(
            value or 0.0,
            f"股性「{character.grade}」/「{character.regime}」，"
            f"做T友好度 {character.t_friendly}/100（"
            + ("值得滚动" if (value or 0) > 0.15 else
               "不建议滚动" if (value or 0) < -0.15 else "中性") + "）",
            {"t_friendly": character.t_friendly, "regime": character.regime,
             "grade": character.grade, "atr_pct": character.atr_pct})
    else:
        outcomes["character"] = DailyFactorOutcome(
            0.0, "股性画像不可用，本因子不计入", {}, available=False,
            gap=(character.gap if character is not None else None) or "日线样本不足")

    # ---- 合成（与分时同一套算术：贡献分逐项相加恰好等于总分）----
    factors: list[FactorScore] = []
    total = 0.0
    available_weight = 0.0
    labels = {"trend": "趋势结构", "chan_daily": "缠论买卖点", "volume": "量价结构",
              "position": "位置与身位", "signal_rule": "规则信号质量",
              "cycle": "市场情绪周期", "character": "股性适配"}
    for key in DAILY_FACTOR_ORDER:
        outcome = outcomes[key]
        weight = float(weights.get(key, 0.0))
        contribution = outcome.score * weight
        rounded = round(contribution, 2)
        if outcome.available:
            available_weight += weight
            total += rounded
        else:
            rounded = 0.0
            if outcome.gap:
                gaps.append(f"{labels[key]}：{outcome.gap}")
        factors.append(FactorScore(
            key=key, label=labels[key], weight=weight,
            score=round(outcome.score, 4), contribution=rounded,
            detail=outcome.detail, inputs=outcome.inputs,
            available=outcome.available, gap=outcome.gap))
    total = round(total, 2)
    zone: ScoreZone = daily_zone_for(total, action, hint)
    coverage_text = (
        f"；有效权重 {available_weight:g}/100" if available_weight < 100 else "")
    if protective and protective.get("broken_stop"):
        verdict = (f"日线做T总分 {total:+.1f}，但**保护线已破**"
                   f"（{protective.get('stop_basis', '')}）→ 先处理风险，不做T")
    elif zone == "strong_buy_zone":
        verdict = f"日线做T总分 {total:+.1f} 突破动手线 +{action:g} → 偏多，可考虑低吸滚动"
    elif zone == "buy_zone":
        verdict = f"日线做T总分 {total:+.1f} 位于提示线 +{hint:g}～动手线之间 → 偏多，小仓位试"
    elif zone == "strong_sell_zone":
        verdict = f"日线做T总分 {total:+.1f} 跌破动手线 -{action:g} → 偏空，优先高抛降仓"
    elif zone == "sell_zone":
        verdict = f"日线做T总分 {total:+.1f} 位于 -{hint:g}～-{action:g} 之间 → 偏空，逢高减"
    else:
        verdict = f"日线做T总分 {total:+.1f} 处于震荡区间（|总分| < 提示线 {hint:g}）→ 不做"
    verdict += coverage_text
    return ScoreCard(
        total=total, threshold_action=action, threshold_hint=hint, zone=zone,
        verdict=verdict, factors=factors,
        weights_sum=round(sum(float(weights.get(k, 0.0)) for k in DAILY_FACTOR_ORDER), 4),
        available_weight=round(available_weight, 2), gaps=gaps)
