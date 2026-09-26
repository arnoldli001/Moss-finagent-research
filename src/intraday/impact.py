"""档位/权重 → 做T价格线与触发门槛的**可解释推导**（面板直接展示）。

## 这个模块回答的两个问题

权重编辑面板上有一排滑杆和一排档位输入框，但用户拖完之后最常见的困惑是：

1. **「我把这个因子调到 8，回踩线会变成多少？」** —— 答案是：**不会变**。
   回踩线/冲高线/止损位来自**箱体 / 布林 / VWAP / ATR**，与因子权重完全无关；
   权重只决定总分（够不够格动手）。这不是设计缺陷而是分工：权重管"该不该动"，
   档位管"在哪个价格动"。所以 `examples` 里把这句话直接写出来，而不是让用户
   自己从两条链路里悟。
2. **「那我现在离动手还差多少？」** —— 这需要把两条链路**合起来**看：
   价格要走到 `回踩线×(1+贴线带宽)` 以内，且总分要 ≥ 提示线/动手线。
   两者各自给出「还差多少」，并把先卡住的那一条指出来。

## 口径来源

全部数值都取自**同一次真实打分**（服务端完整快照 / 预览快照）：`levels` 来自
`engine.compute_levels`，`total` 来自 `engine.compose_scorecard`，阈值与护栏来自
同一份 `IntradayConfig`。这里不重算、不近似、不猜 —— 只做"把每个数从哪个原始量
算出来的"这件事的展开，因此面板上的数与图上那条线**必然一致**。

## 为什么单列一个模块而不是塞进 engine.py

`engine.py` 已经在讲"怎么算"；本模块只讲"怎么把算出来的东西解释给人听"，
且不参与任何信号判定（纯读，无副作用）。分开后 engine 的判定逻辑仍可被
独立单测穷举，解释口径变了也不会碰判定分支。
"""

from __future__ import annotations

import math
from typing import Any

from src.intraday.config import IntradayConfig
from src.intraday.models import (
    FactorImpactRow,
    FactorScore,
    LevelSet,
    ScoreCard,
    TriggerGateRow,
    TriggerImpact,
    TriggerLevelDelta,
    TriggerLevelRow,
)

# 权重**单位影响度**的排序阈值：得分绝对值小于它时，该因子的权重怎么调都对总分
# 几乎没影响（例如得分为 0 的维度），列表里不必占位。
_MIN_VISIBLE_UNIT_IMPACT = 0.05

# 有效权重低于该值时，实心正式信号被降级为空心（与 engine.MIN_COVERAGE_FOR_SOLID 同值）。
# 刻意在这里再写一次而不是 import：impact 是**只读解释层**，
# 让它依赖判定层常量会把两层的边界弄糊（engine 改了阈值，解释层必须同步改，
# 那时测试会同时红 —— 这是想要的提醒，不是噪音）。
_MIN_COVERAGE_FOR_SOLID_PCT = 70.0


def annotate_level_basis(levels: LevelSet, config: IntradayConfig) -> LevelSet:
    """给**当前**档位对象补上「这一版线是谁定的 + 触发价在哪」。

    只补到传进来的这一个对象上（不逐bar调用），所以可以放开了写解释字段。
    """
    params = config.levels
    price = float(levels.price)
    band = float(params.touch_band_pct) / 100.0
    box_low, box_high = float(levels.box_low), float(levels.box_high)
    boll_lower = _finite(levels.boll_lower)
    boll_upper = _finite(levels.boll_upper)
    # 与 engine 同口径：回踩线低于「现价 - 0.2%」才算落到了现价下方
    # （_MIN_LEVEL_GAP=0.002），否则就是走了 ATR 兜底分支。
    below_price = price * (1.0 - 0.002)
    # 候选值：与 engine.compute_levels 依次取 max（回踩）/ min（冲高）的顺序一致，
    # 这样"这条线是谁定的"能用「与候选值相等」直接判出来，不靠猜。
    low_candidates: list[tuple[str, float]] = [(f"箱体下沿 {box_low:.2f}", box_low)]
    if params.blend_boll_bands and boll_lower is not None and boll_lower < price:
        low_candidates.append((f"布林下轨 {boll_lower:.2f}", boll_lower))
    low_value = max(value for _, value in low_candidates)
    low_pick = max(low_candidates, key=lambda item: item[1])
    low_source = low_pick[0]
    low_others = [name for name, _ in low_candidates if name != low_source]
    if low_others:
        low_source += f"（与{low_others[0]}中取高者）"
    if low_value > below_price:
        low_source = (
            f"箱体下沿 {box_low:.2f} / 布林下轨 {_fmt(boll_lower)} 都在现价上方 "
            f"→ 按 dip_fallback_atr {params.dip_fallback_atr:g}×ATR 兜底")

    buffer_text = ""
    if params.take_profit_buffer_pct:
        buffer_text = f"，再加冲高缓冲 {params.take_profit_buffer_pct:g}%"
    blended = box_high * (1.0 + params.take_profit_buffer_pct / 100.0)
    high_candidates: list[tuple[str, float]] = [
        (f"箱体上沿 {box_high:.2f}{buffer_text}", blended)]
    if params.blend_boll_bands and boll_upper is not None and boll_upper > price:
        high_candidates.append((f"布林上轨 {boll_upper:.2f}", boll_upper))
    high_value = min(value for _, value in high_candidates)
    high_pick = min(high_candidates, key=lambda item: item[1])
    high_source = high_pick[0]
    high_others = [name for name, _ in high_candidates if name != high_source]
    if high_others:
        high_source += f"（与{high_others[0]}中取低者）"

    band_width = float(levels.high_sell) - float(levels.low_buy)
    band_width_pct = round(band_width / price * 100.0, 3) if price > 0 else None
    # 是否被档位差护栏动过：直接比「候选位置」与「最终位置」。
    # 刻意不只比 band_width_pct 与上下限：候选区间恰好等于下限时（实测
    # 300308 的 902.35~908.52 对上 min_band_pct=1.5%），夹与不夹的宽度一样，
    # 但两条线其实都被移动过 —— 只看宽度会漏掉这种情况，
    # 于是面板上就会出现「标记是布林轨、数值却是箱体值」的解释错位。
    moved = (abs(float(levels.low_buy) - low_value) > 0.005
             or abs(float(levels.high_sell) - high_value) > 0.005)
    clamped = ""
    if price > 0 and band_width_pct is not None and moved:
        if band_width_pct <= float(params.min_band_pct) + 1e-6:
            clamped = (f"被最小档位差 {params.min_band_pct:g}% 撑开"
                       f"（候选 {low_value:.2f}~{high_value:.2f}，"
                       f"仅 {high_value - low_value:.2f} 元）")
        elif band_width_pct >= float(params.max_band_pct) - 1e-6:
            clamped = (f"被最大档位差 {params.max_band_pct:g}% 收窄"
                       f"（候选 {low_value:.2f}~{high_value:.2f}）")
        else:
            clamped = f"随档位差护栏微调（候选 {low_value:.2f}~{high_value:.2f}）"

    return levels.model_copy(update={
        "low_source": low_source,
        "high_source": high_source,
        "band_width_pct": band_width_pct,
        "band_clamped": clamped,
        "pre_clamp_low": round(low_value, 4),
        "pre_clamp_high": round(high_value, 4),
        "low_trigger_price": round(float(levels.low_buy) * (1.0 + band), 4),
        "high_trigger_price": round(float(levels.high_sell) * (1.0 - band), 4),
        "take_profit_buffer_pct": float(params.take_profit_buffer_pct),
        "dip_fallback_atr": float(params.dip_fallback_atr),
        "atr_stop_mult": float(params.atr_stop_mult),
    })


def cycle_facts_from_scorecard(scorecard: ScoreCard | None) -> dict[str, Any] | None:
    """从打分卡里回读「这次打分用的是哪个周期阶段」。

    情绪周期因子把 `stage / temperature / t_allowed / gates` 原样写进了
    `inputs`（见 `factors.score_cycle`），因此**这一份**就是本次打分真正用到的周期
    快照。相比另外去问一次周期提供方，它永远不会与卡片上的数字打架 ——
    「卡片按主升期算的分、提示却说退潮期禁止动手」这种自相矛盾必须避免。
    """
    if scorecard is None:
        return None
    for factor in scorecard.factors:
        if factor.key != "cycle" or not factor.available:
            continue
        inputs = factor.inputs or {}
        return {
            "stage": str(inputs.get("stage", "") or ""),
            "temperature": inputs.get("temperature"),
            "t_allowed": inputs.get("t_allowed"),
            "gates": list(inputs.get("gates") or []),
            "source": "scorecard",
        }
    return None


def factor_impacts(
    scorecard: ScoreCard, *, top: int | None = 6,
) -> list[FactorImpactRow]:
    """每个因子对总分的**影响度**（按 |单位影响度| 降序，可选截断）。

    单位影响度就是该因子的**得分**：总分 = Σ(得分×权重)，所以权重 +1 分会把总分
    推高「得分」那么多。得分为负的因子（例如现在偏空的 KDJ/RSI）权重越大，
    总分被**拉得越低** —— 这一点在面板上必须写清楚，否则用户会以为"权重都是加分项"。

    `zero_impact`：把这一项权重置 0、其余不动时总分的变化（关掉它的净影响）。
    它同时是「总分刻度」的护栏说明：权重置 0 的那个因子不再进入有效权重。
    """
    rows: list[FactorImpactRow] = []
    effective_weight = max(1e-9, float(scorecard.available_weight))
    for factor in scorecard.factors:
        contribution = float(factor.contribution)
        # 不可用维度的贡献分本身就是 0（未进总分），两个影响度都必须是 0 ——
        # 否则会出现「这一项不可用，关掉它却能改变 5 分」这种自相矛盾的解释。
        usable = bool(factor.available)
        rows.append(FactorImpactRow(
            key=factor.key,
            label=factor.label,
            weight=float(factor.weight),
            score=round(float(factor.score), 4),
            contribution=round(contribution, 2),
            unit_impact=round(float(factor.score), 4) if usable else 0.0,
            zero_impact=round(-contribution, 2) if usable else 0.0,
            weight_share_pct=(round(float(factor.weight) / effective_weight * 100.0, 1)
                              if usable else 0.0),
            available=usable,
            gap=factor.gap,
            note=_impact_note(factor),
        ))
    rows.sort(key=lambda item: (-abs(item.unit_impact), -abs(item.contribution)))
    if top is not None and top > 0:
        return rows[:top]
    return rows


def _impact_note(factor: FactorScore) -> str:
    if not factor.available:
        return "该维度当前不可用（数据缺口）：权重再大也不进总分与有效权重"
    if abs(float(factor.score)) < _MIN_VISIBLE_UNIT_IMPACT:
        return "当前得分≈0：权重怎么调都几乎不影响总分（中性状态）"
    direction = "拉高" if float(factor.score) > 0 else "拉低"
    return (f"权重每 +1 分，总分{direction} {abs(float(factor.score)):.2f}"
            f"（当前贡献 {float(factor.contribution):+.1f}）")


def level_deltas(
    current: LevelSet | None, preview: LevelSet | None,
) -> list[TriggerLevelDelta]:
    """「改动前 → 改动后」逐线对照（冲高 / 回踩 / 止损 / VWAP）。

    放在服务端算而不是前端各算一遍：两份档位都是服务端产出的对象，
    差异口径（用哪些字段、怎么算百分比）只该有一处定义 ——
    否则同一个「回踩线变动」在面板和接口里迟早会给出两个不一样的数。

    ⚠️ 口径说明（必须在面板上写清楚）：回踩/冲高/止损是**时刻量**，随
    VWAP/布林/现价每分钟重算。这里的两份值都基于**同一份行情**，
    比的是「参数与阈值改动」的效果，不是"上一分钟那条线在哪"。

    ⚠️ 实现约束：档位线是 `engine.compute_levels` 的产物，「同样行情 + 另一套
    档位参数 → 另一条线」这件事**只能由 engine 算**。所以本函数只做差值与格式化，
    绝不在解释层里重算档位（重算必然与判定层漂移到两套口径）。
    「改动前」那份由调用方用该票当前生效的档位参数标注后传进来。
    """
    if preview is None:
        return []
    pairs = [
        ("high_sell", "冲高线", "high_sell"),
        ("low_buy", "回踩线", "low_buy"),
        ("stop_loss", "止损位", "stop_loss"),
        ("vwap", "当日均价(VWAP)", "vwap"),
    ]
    rows: list[TriggerLevelDelta] = []
    for key, label, attr in pairs:
        after = _finite(getattr(preview, attr, None))
        if after is None:
            continue
        before = None if current is None else _finite(getattr(current, attr, None))
        delta = None if before is None else round(after - before, 4)
        delta_pct = (None if before is None or before == 0
                     else round((after - before) / before * 100.0, 4))
        rows.append(TriggerLevelDelta(
            key=key, label=label,
            current=None if before is None else round(before, 4),
            preview=round(after, 4), delta=delta, delta_pct=delta_pct))
    return rows


def build_trigger_impact(
    *,
    scorecard: ScoreCard | None,
    levels: LevelSet | None,
    config: IntradayConfig,
    cycle: Any = None,
    cycle_facts: dict[str, Any] | None = None,
    current_levels: LevelSet | None = None,
    top_factors: int = 6,
) -> TriggerImpact:
    """打分卡 + 档位 → 「价格线 / 触发门槛 / 因子影响度」的完整解释。

    `cycle` 是提供方给的周期快照对象（可选）；`cycle_facts` 是从打分卡回读的
    本次口径（优先）。两者都没有时只影响「周期一票否决」那一行提示。

    `current_levels`：用来做「改动前」那一列的档位（通常是把同一条线的档位值
    用**该票当前生效口径**标注一遍得到）。它只提供价格，不参与计算；
    不传时对照行的 `current/delta` 一律为空 —— **绝不编造基准**。
    """
    action = float(config.thresholds.action)
    hint = float(config.thresholds.hint)
    facts = cycle_facts or cycle_facts_from_scorecard(scorecard)
    if facts is not None:
        cycle_stage = str(facts.get("stage", "") or "")
        cycle_veto = (
            facts.get("t_allowed") is False
            and bool(getattr(config.factors.cycle, "veto_signals", True))
        )
    else:
        cycle_stage = str(getattr(cycle, "stage", "") or "")
        cycle_veto = (
            bool(getattr(cycle, "available", False))
            and getattr(cycle, "t_allowed", None) is False
            and bool(getattr(config.factors.cycle, "veto_signals", True))
        )
    if scorecard is None or levels is None:
        return TriggerImpact(
            available=False, reason="打分行或档位缺失（分钟K线/日线缺口），无法推导触发门槛",
            threshold_action=action, threshold_hint=hint,
            cycle_blocked=cycle_veto, cycle_stage=cycle_stage,
        )

    annotated = annotate_level_basis(levels, config)
    price = float(annotated.price)
    total = float(scorecard.total)
    coverage = float(scorecard.available_weight)
    coverage_blocked = coverage < _MIN_COVERAGE_FOR_SOLID_PCT

    notes: list[str] = []
    examples: list[str] = [
        "档位线（回踩/冲高/止损）只由 箱体 / 布林 / VWAP / ATR / 档位参数 决定，"
        "**与因子权重无关** —— 调权重不会让回踩线移动一分钱；",
        "权重决定的是总分：总分够不够 提示线/动手线，决定价格触线时是"
        "「空心软提示」还是「实心正式信号」；",
        "所以本页的两张表要合起来读：上面的价格线告诉你「在哪个价位动手」，"
        "下面的影响度告诉你「总分够不够格动手」。",
    ]

    level_rows = _level_rows(annotated, price)
    gates = _gate_rows(
        annotated=annotated, total=total, price=price, hint=hint, action=action,
        coverage_blocked=coverage_blocked, cycle_blocked=cycle_veto,
        cycle_stage=cycle_stage)
    impacts = factor_impacts(scorecard, top=top_factors)

    if annotated.band_clamped:
        notes.append(
            f"档位差 {annotated.band_width_pct:.2f}%（原始候选 回踩 "
            f"{annotated.pre_clamp_low:.2f} / 冲高 {annotated.pre_clamp_high:.2f}）"
            f"{annotated.band_clamped}，最终落在 回踩 {annotated.low_buy:.2f} / "
            f"冲高 {annotated.high_sell:.2f}"
            " —— 护栏的用意是「别让一轮做T被双边成本吃掉」与「别让线远到永不触发」。")
    if annotated.atr is not None and annotated.stop_loss is not None:
        notes.append(
            f"止损距离取「回踩线×{annotated.stop_loss_pct:g}%」与"
            f"「{annotated.atr_stop_mult:g}×ATR」中更宽的那个：{annotated.stop_basis}。")
    if coverage_blocked:
        notes.append(
            f"有效权重只有 {coverage:g}/100（<70）：即使总分越过动手线也会被降级为"
            "空心软提示，不会出实心正式信号。")
    if cycle_veto:
        notes.append(
            f"市场情绪周期「{cycle_stage or '未知'}」为一票否决阶段："
            "本模块禁止正式回踩区间提示（回踩胜率极低），总分再高也不会出实心信号。")

    weak = [row for row in impacts if not row.available]
    if weak:
        notes.append(
            "数据缺口维度：" + "、".join(row.label for row in weak)
            + "（权重已从有效权重中扣除，不参与总分）")

    return TriggerImpact(
        available=True, price=round(price, 4),
        low_trigger_price=annotated.low_trigger_price,
        high_trigger_price=annotated.high_trigger_price,
        stop_price=annotated.stop_loss,
        total=round(total, 2),
        threshold_action=action, threshold_hint=hint,
        available_weight=round(coverage, 2),
        coverage_blocked=coverage_blocked, cycle_blocked=cycle_veto,
        cycle_stage=cycle_stage,
        level_rows=level_rows,
        level_deltas=level_deltas(current_levels, annotated),
        gates=gates, factor_impact=impacts,
        examples=examples, notes=notes,
    )


def _level_rows(levels: LevelSet, price: float) -> list[TriggerLevelRow]:
    low_note = "回踩：先接后抛（做T的买入腿）"
    high_note = "冲高：先减后接（做T的卖出腿）"
    if levels.band_clamped:
        low_note += f"；当前值已被档位差护栏调整（候选位置 {_fmt(levels.pre_clamp_low)}）"
        high_note += f"；当前值已被档位差护栏调整（候选位置 {_fmt(levels.pre_clamp_high)}）"
    rows = [
        TriggerLevelRow(
            key="high_sell", label="冲高线",
            price=round(float(levels.high_sell), 4),
            distance_pct=_distance_pct(price, float(levels.high_sell)),
            source=levels.high_source or f"箱体上沿 {levels.box_high:.2f}",
            trigger_price=levels.high_trigger_price,
            trigger_note=(
                f"价格涨到 {levels.high_trigger_price:.2f} 及以上即「触及」"
                "，此时总分 ≤ −提示线才出冲高信号"
                if levels.high_trigger_price is not None else ""),
            note=high_note),
        TriggerLevelRow(
            key="low_buy", label="回踩线",
            price=round(float(levels.low_buy), 4),
            distance_pct=_distance_pct(price, float(levels.low_buy)),
            source=levels.low_source or f"箱体下沿 {levels.box_low:.2f}",
            trigger_price=levels.low_trigger_price,
            trigger_note=(
                f"价格跌到 {levels.low_trigger_price:.2f} 及以下即「触及」"
                "，此时总分 ≥ 提示线才出回踩信号"
                if levels.low_trigger_price is not None else ""),
            note=low_note),
        TriggerLevelRow(
            key="stop_loss", label="止损位",
            price=round(float(levels.stop_loss), 4),
            distance_pct=_distance_pct(price, float(levels.stop_loss)),
            source=levels.stop_basis or f"回踩线下方 {levels.stop_loss_pct:g}%",
            trigger_price=round(float(levels.stop_loss), 4),
            trigger_note=(
                "现价跌破即为止损提示（forced_exit），且**禁止一切回踩信号**"
                "；逐bar回放还要求跌破此前最低价的 0.3% 才算「真实破位」"),
            note="风控线：不是预测线，必须低于当日已成交区间"),
    ]
    if levels.vwap is not None:
        rows.append(TriggerLevelRow(
            key="vwap", label="当日均价(VWAP)",
            price=round(float(levels.vwap), 4),
            distance_pct=_distance_pct(price, float(levels.vwap)),
            source="当日累计成交额/成交量（盘中每一分钟都在变）",
            trigger_price=None,
            trigger_note=(
                "VWAP 本身不是档位；只有 |偏离z| ≥ vwap_extreme_z 时"
                "才算「价格触及关键档位」的等效条件"),
            note="现价相对均价的位置：上方=多头占优，下方=空头占优"))
    if levels.boll_lower is not None or levels.boll_upper is not None:
        rows.append(TriggerLevelRow(
            key="boll", label="布林轨（当日分钟口径）",
            price=round(float(levels.boll_lower or levels.boll_upper or 0.0), 4),
            distance_pct=_distance_pct(price, float(levels.boll_lower or price)),
            source=(f"下轨 {_fmt(levels.boll_lower)} / 中轨 {_fmt(levels.boll_mid)}"
                    f" / 上轨 {_fmt(levels.boll_upper)}，带宽 {_fmt(levels.bandwidth, 4)}"),
            trigger_price=None,
            trigger_note="开启 blend_boll_bands 时，下轨/上轨会参与回踩线/冲高线的取高/取低",
            note="强势股刚突破时带宽会窄到 1% 以内，护栏只采纳**在现价下方/上方**的那一侧"))
    return rows


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def _gate_rows(
    *, annotated: LevelSet, total: float, price: float, hint: float, action: float,
    coverage_blocked: bool, cycle_blocked: bool, cycle_stage: str,
) -> list[TriggerGateRow]:
    low_ready = total >= hint
    high_ready = total <= -hint
    low_solid = total >= action
    high_solid = total <= -action
    solid_note = "，但只能出空心软提示" if coverage_blocked else "，可出实心正式信号"
    blocks = []
    if coverage_blocked:
        blocks.append("有效权重<70 → 实心降级为空心")
    if cycle_blocked:
        blocks.append(f"情绪周期「{cycle_stage}」禁止回踩区间提示")
    cycle_gate_note = "；".join(blocks)
    # 冲高不受情绪周期否决（退潮期更需要减仓），因此只有覆盖度能拦它
    high_gate_note = ("有效权重<70 → 实心降级为空心" if coverage_blocked else "")

    price_to_low = _price_need_pct(price, annotated.low_trigger_price, falling=True)
    price_to_high = _price_need_pct(price, annotated.high_trigger_price, falling=False)
    return [
        TriggerGateRow(
            key="low_buy", label="回踩区间提示",
            ready=low_ready and not cycle_blocked,
            score_need=round(max(0.0, hint - total), 2),
            price_need_pct=price_to_low,
            blocked_by=cycle_gate_note,
            note=(f"总分 {total:+.1f}"
                  + (f"，已达提示线 +{hint:g}" if low_ready
                     else f"，距提示线 +{hint:g} 还差 {hint - total:.1f} 分")
                  + (f"，已达动手线 +{action:g}" + solid_note if low_solid
                     else f"，距动手线 +{action:g} 还差 {action - total:.1f} 分"))
            + ("" if price_to_low is None
               else f"；价格需再跌 {abs(price_to_low):.2f}% 到 "
                    f"{annotated.low_trigger_price:.2f} 才触及回踩线")),
        TriggerGateRow(
            key="high_sell", label="冲高区间提示",
            ready=high_ready,
            score_need=round(max(0.0, total + hint), 2),
            price_need_pct=price_to_high,
            blocked_by=high_gate_note,
            note=(f"总分 {total:+.1f}"
                  + (f"，已达提示线 -{hint:g}" if high_ready
                     else f"，距提示线 -{hint:g} 还差 {total + hint:.1f} 分")
                  + (f"，已达动手线 -{action:g}" + solid_note if high_solid
                     else f"，距动手线 -{action:g} 还差 {total + action:.1f} 分"))
            + ("" if price_to_high is None
               else f"；价格需再涨 {abs(price_to_high):.2f}% 到 "
                    f"{annotated.high_trigger_price:.2f} 才触及冲高线")),
        TriggerGateRow(
            key="stop_loss", label="止损（止损触发）",
            ready=price <= float(annotated.stop_loss) + 1e-9,
            score_need=0.0,
            price_need_pct=_distance_pct(price, float(annotated.stop_loss)),
            blocked_by="",
            note=(f"止损位 {annotated.stop_loss:.2f}"
                  + ("：**现价已跌破**，禁止任何回踩信号"
                     if price <= float(annotated.stop_loss) + 1e-9
                     else f"：现价距它还有 "
                          f"{abs(_distance_pct(price, float(annotated.stop_loss)) or 0.0):.2f}%"
                          " 的空间；跌破即止损提示并禁止回踩"))),
    ]


def _distance_pct(price: float, level: float) -> float | None:
    if price <= 0 or not math.isfinite(level):
        return None
    return round((level - price) / price * 100.0, 3)


def _price_need_pct(price: float, target: float | None,
                    *, falling: bool) -> float | None:
    """还要走多少%才触及目标价（正数=还需移动的幅度）。"""
    if target is None or price <= 0 or not math.isfinite(float(target)):
        return None
    delta = (float(target) - price) / price * 100.0
    if falling:
        # 回踩：目标在下方，已在线下则无需再跌（0）
        return round(min(0.0, delta), 3)
    return round(max(0.0, delta), 3)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
