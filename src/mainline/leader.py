"""主线挖掘：第三层「龙头共振确认」（**门控加分，不占权重**）。

## 这一层的定位：只算派生指标与真正独立的新数据

V2.0 明确要求本层**不再计算任何原始资金流因子** —— 龙头股的资金流已经被
第一层的 moneyflow 维度用过了，再算一次就是重复加权。本层只做两件事：

1. **龙头资金集中度**（派生指标）：
   `龙头股 N 日主力净流入 / 板块 N 日主力净流入`
   底层数据是第一层已经取到的个股与板块资金流，**不新增原始因子**；
2. **龙虎榜席位追踪**（真正独立的新数据）：整个模块里唯一一处用到
   `top_inst` 的地方。前两层完全不碰龙虎榜，因此它的信息是纯增量。

## 门控语义

集中度 > `resonance_ratio`（默认 30%）时触发"龙头共振"，给最终分加
`gate_bonus`（15~20 分，按集中度在 [ratio, 2×ratio] 上线性插值）。
**它不改权重**：最终分仍是 `第一层×w + 第二层×(100−w) + 门控加分`，
`w` 由 `synthesis.layer_weights` 决定（V2.3 起为 100，即六维层独占）。

为什么用门控而不是第三层权重：V1.0 给龙头 25% 权重，等于把"龙头股资金流"
这件第一层已经算过的事，再以 25% 的力度算第二遍。改成门控之后，
它只在**确认信号**时起作用（加 15-20 分足以把 55 分推过 70 的强信号线），
不确认时不产生任何影响。

## 三维交集法识别龙头

每个交易日动态更新，三个维度各取板块内前 `top_n` 名：

    资金维度  近 5 日主力净流入前 3
    涨幅维度  近 10 日涨幅前 3（启动早于板块内其他个股）
    成交维度  近 5 日成交额占板块比例最高

**三维取交集**（`hits == 3`）是核心龙头；只有两维命中（`hits == 2`）算次级。
取交集而不是并集，是因为并集在热门板块里会把十几只票都算成"龙头"，
集中度分母被摊薄，反而永远触发不了共振。

## 无数据一律 None

板块 5 日主力净流入 ≤ 0（净流出）时集中度**无定义**（负分母算出来的
"集中度 250%"毫无意义），此时返回 None 而不是硬算一个数。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.mainline.config import MainlineConfig
from src.mainline.models import (
    BoardSeries,
    DimensionScore,
    LayerScore,
    LeaderInfo,
    SeatRow,
)
from src.mainline.scoring import clamp, rank_percentile, safe_ratio, to_float

logger = logging.getLogger(__name__)

DIM_LABELS = {"concentration": "龙头资金集中度", "seat": "龙虎榜席位"}

#: 集中度在 [ratio, 2×ratio] 上线性映射到 [0, 100] 的展示分
CONCENTRATION_SPAN = 2.0

#: 席位确认在 layer score 上的基础分（机构权重大于游资，见 `seat_confirmation`）
SEAT_SCORE_CAP = 100.0


@dataclass
class LeaderInput:
    """第三层打分需要的一个精选板块的输入。"""

    code: str
    name: str
    series: BoardSeries = field(default_factory=lambda: BoardSeries(code=""))
    #: 板块近 N 日主力净流入（元；来自第一层已取到的板块资金流）
    board_net: float | None = None
    #: `{成分股代码: {net_5d, ret_10d, amount_5d, close, name}}`
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 区间内该板块成分股的龙虎榜席位
    seats: list[SeatRow] = field(default_factory=list)
    #: 成分股代码 → 名称（席位确认要展示个股名）
    names: dict[str, str] = field(default_factory=dict)
    #: `{成分股代码: 业务相关性}` —— **只用于展示，不参与任何判定**。
    #:
    #: 用户 2026-09-22 的诉求：龙头表要能看出"这只票凭什么是这个概念的成员"。
    #: 目前"龙头"只看资金/动量/量能，业务相关性从不参与，所以雅克科技
    #: （主营半导体材料）会出现在氟化工概念的龙头里。这里把业务信息带上，
    #: **让口径可见** —— 不改变选取结果（零破坏性）。
    #:
    #: 结构：`{"score": float|None, "source": "llm"|"corr"|"", "reason": str,
    #: "admission": str}`。`admission` 是入池通道（`corr` / `business` /
    #: `corr_business_failed`），与 `source` 不是同一件事 —— 见
    #: `LeaderInfo.admission` 的说明。
    business: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ResonanceOutcome:
    """龙头共振的判定结果。"""

    leaders: list[LeaderInfo] = field(default_factory=list)
    concentration: float | None = None      # 龙头资金集中度（口径见 evaluate_resonance）
    #: 原始口径（龙头净流入 / 板块净流入）—— 板块净流出时为负或无定义，
    #: 只作复核展示，不参与判定
    raw_concentration: float | None = None
    triggered: bool = False
    seat_confirmed: bool = False
    seat_stocks: int = 0
    seat_note: str = ""
    bonus: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"concentration": self.concentration,
                "raw_concentration": self.raw_concentration,
                "triggered": self.triggered,
                "seat_confirmed": self.seat_confirmed,
                "seat_stocks": self.seat_stocks, "seat_note": self.seat_note,
                "bonus": self.bonus,
                "leaders": [row.to_dict() for row in self.leaders],
                "notes": list(self.notes)}


def _yi(value: Any) -> str:
    """金额显示成"亿元"（龙头层日志与面板都用这个量纲）。"""
    number = to_float(value)
    return "—" if number is None else f"{number / 1e8:+.2f} 亿"


# ==================================================================
# 龙头识别
# ==================================================================


def identify_leaders(item: LeaderInput, *, config: MainlineConfig
                     ) -> list[LeaderInfo]:
    """三维交集法识别动态龙头（按 hits 降序、资金强度降序）。

    ⚠️ **成分股太少时直接返回空**：`hits` 统计的是"进入各维度前 `top_n` 名"，
    当成分股数 ≤ `top_n` 时**每一只票在每个维度都进榜**，所有票的 `hits` 都是 3，
    于是整个板块被算成"龙头"、集中度退化成 100%。
    这是一个不会报错、只会让结论失去意义的失效方式（板块越小越容易"共振"），
    因此这里要求成分股至少是 `top_n` 的 2 倍 —— 前 3 名在 6 只票里才有区分度。
    """
    cfg = config.leader
    top_n = max(1, int(cfg.top_n))
    pool = {code: stats for code, stats in item.members.items() if stats}
    if len(pool) < max(3, top_n * 2):
        return []
    capital = _top_keys(pool, "net_5d", top_n, reverse=True)
    momentum = _top_keys(pool, "ret_10d", top_n, reverse=True)
    volume = _top_keys(pool, "amount_5d", top_n, reverse=True)
    leaders: list[LeaderInfo] = []
    for code, stats in pool.items():
        capital_rank = _rank_of(capital, code)
        momentum_rank = _rank_of(momentum, code)
        volume_rank = _rank_of(volume, code)
        hits = sum(1 for rank in (capital_rank, momentum_rank, volume_rank)
                   if rank > 0)
        if hits < 2:
            continue
        info = item.business.get(code) or {}
        leaders.append(LeaderInfo(
            code=code, name=item.names.get(code) or str(stats.get("name") or ""),
            net_5d=to_float(stats.get("net_5d")),
            ret_10d=(to_float(stats.get("ret_10d")) or 0.0) * 100.0,
            amount_ratio=None,
            capital_rank=capital_rank, momentum_rank=momentum_rank,
            volume_rank=volume_rank, hits=hits,
            source="local:quant_warehouse 三维交集",
            # 下面几个只影响展示（见 `LeaderInfo` 的 docstring）：
            # "资金龙头"与"业务龙头"要能分得开，否则用户会按业务去理解
            # 一个纯资金口径的榜。
            business_score=to_float(info.get("score")),
            business_source=str(info.get("source") or ""),
            business_reason=str(info.get("reason") or ""),
            admission=str(info.get("admission") or "")))
    leaders.sort(key=lambda row: (-row.hits, -(row.net_5d or 0.0)))
    return leaders[:max(top_n * 3, top_n)]


def _top_keys(pool: dict[str, dict[str, Any]], key: str, size: int, *,
              reverse: bool = True) -> list[str]:
    """取某指标的前 `size` 名代码（值缺失的股票不参与排名）。"""
    items = [(code, to_float(stats.get(key))) for code, stats in pool.items()]
    usable = [(code, value) for code, value in items if value is not None]
    if not usable:
        return []
    usable.sort(key=lambda row: row[1], reverse=reverse)
    return [code for code, _ in usable[:size]]


def _rank_of(order: list[str], code: str) -> int:
    """在榜单里的名次（1-based；不在榜上返回 0）。"""
    try:
        return order.index(code) + 1
    except ValueError:
        return 0


def amount_share(item: LeaderInput, code: str) -> float | None:
    """个股近 5 日成交额占板块比例（展示用；识别时用的是绝对额排名）。"""
    total = sum(to_float(stats.get("amount_5d")) or 0.0
                for stats in item.members.values())
    value = to_float((item.members.get(code) or {}).get("amount_5d"))
    if value is None or total <= 0:
        return None
    return value / total


# ==================================================================
# 共振判定
# ==================================================================


def evaluate_resonance(item: LeaderInput, *, config: MainlineConfig
                       ) -> ResonanceOutcome:
    """算龙头资金集中度、席位确认与门控加分。"""
    cfg = config.leader
    outcome = ResonanceOutcome()
    outcome.leaders = identify_leaders(item, config=config)
    for leader in outcome.leaders:
        leader.amount_ratio = amount_share(item, leader.code)

    # ---------- 龙头资金集中度 ----------
    #
    # ⚠️ 这个指标的口径比看上去微妙，2024-06 的 CRO 案例就是被它挡住的。
    #
    # 原口径：`龙头净流入 / 板块净流入`。板块**净流出**时（分母 < 0）比值无意义，
    # 早期实现直接返回 None → 不触发共振。问题是"板块还在净流出、龙头已经被
    # 大额买入"恰恰是本模块最想抓的场景（**启动前**）—— 医药 2024-06 就是如此：
    # 药明康德被三维交集法正确识别为龙头，但整个医药板块 5 日聚合净流出，
    # 于是这个指标在最该报警的时候失灵。
    #
    # 修正口径（分母换成"板块内所有个股净流入的绝对值之和"）：
    #
    #     集中度 = 龙头净流入 / Σ|成分股净流入|
    #
    # 这个分母**恒为正**，含义是"龙头在板块全部资金动作里占多大比重"：
    #   - 板块普遍流入时，它退化成原口径的近似（Σ|net| ≈ Σnet）；
    #   - 板块普遍流出、只有龙头逆势流入时，它给出一个**高值**（而非无定义），
    #     正好把"逆势吸筹"识别出来。
    # 原始口径（含净流出场景的说明）保留在 `raw` 里供复核。
    core = [row for row in outcome.leaders if row.hits >= 3]
    if not core:
        core = outcome.leaders
    lead_net = sum(row.net_5d or 0.0 for row in core) if core else 0.0
    board_net = to_float(item.board_net)
    gross = abs(board_net) if board_net is not None else None
    if item.members:
        gross = sum(abs(to_float(stats.get("net_5d")) or 0.0)
                    for stats in item.members.values()) or gross
    outcome.notes.append(
        f"板块近 N 日净流入 {_yi(board_net)}；参与龙头 {len(core)} 只")
    if core and gross and gross > 0:
        outcome.concentration = safe_ratio(lead_net, gross)
        outcome.raw_concentration = (safe_ratio(lead_net, board_net)
                                     if board_net else None)
    if outcome.concentration is not None:
        outcome.triggered = outcome.concentration > float(cfg.resonance_ratio)

    # ---------- 龙虎榜席位（唯一真正独立的新数据） ----------
    confirmed, stocks, weight, note = seat_confirmation(item, config=config)
    outcome.seat_confirmed = confirmed
    outcome.seat_stocks = stocks
    outcome.seat_note = note
    if confirmed and cfg.seat_counts_as_resonance and not outcome.triggered:
        outcome.triggered = True
        outcome.notes.append("席位确认触发共振（龙头资金集中度未达门槛）")

    # ---------- 门控加分 ----------
    outcome.bonus = gate_bonus(outcome.concentration, config=config,
                              seat_confirmed=confirmed,
                              seat_weight=weight,
                              triggered=outcome.triggered)
    return outcome


def seat_confirmation(item: LeaderInput, *, config: MainlineConfig
                      ) -> tuple[bool, int, float, str]:
    """龙虎榜席位确认：板块内 ≥N 只个股同时被**同一席位**大额净买入。

    返回 `(是否确认, 涉及个股数, 权重, 说明)`。

    机构专用席位的权重高于游资营业部（需求 3.4）：机构建仓通常持续更久，
    游资一日游居多。`SeatRow.is_institution` 按席位名里是否含"机构专用"判定。
    """
    cfg = config.leader.seat
    if not item.seats:
        return False, 0, 0.0, ""
    min_buy = float(cfg.min_net_buy)
    buckets: dict[str, dict[str, Any]] = {}
    for row in item.seats:
        if (row.net_buy or 0.0) < min_buy:
            continue
        slot = buckets.setdefault(row.exalter, {"codes": set(), "inst": False,
                                                "net": 0.0})
        slot["codes"].add(row.code)
        slot["net"] += row.net_buy
        slot["inst"] = slot["inst"] or row.is_institution
    best: tuple[int, bool, float, str] | None = None
    for exalter, slot in buckets.items():
        count = len(slot["codes"])
        if count < max(1, int(cfg.min_stocks)):
            continue
        weight = float(cfg.institution_weight if slot["inst"]
                       else cfg.branch_weight)
        if best is None or (count, weight) > (best[0], best[1]):
            label = "机构专用席位" if slot["inst"] else "游资营业部"
            best = (count, slot["inst"], weight,
                    f"{label}「{exalter}」同时净买入 {count} 只成分股"
                    f"（合计 {slot['net'] / 1e8:.2f} 亿）")
    if best is None:
        return False, 0, 0.0, ""
    return True, best[0], best[2], best[3]


def gate_bonus(concentration: float | None, *, config: MainlineConfig,
               seat_confirmed: bool = False, seat_weight: float = 1.0,
               triggered: bool = False) -> float:
    """门控加分（0 或 `[bonus_min, bonus_max]`）。

    集中度在 `[ratio, CONCENTRATION_SPAN × ratio]` 上线性映射到
    `[bonus_min, bonus_max]`；席位权重（机构 1.0 / 游资 0.6）在结果上缩放，
    让"机构确认"比"游资确认"多加一点。未触发共振返回 0。
    """
    cfg = config.leader
    low = float(cfg.bonus_min)
    high = max(low, float(cfg.bonus_max))
    if not triggered:
        return 0.0
    ratio = to_float(concentration)
    threshold = max(float(cfg.resonance_ratio), 1e-6)
    if ratio is None:
        # 只有席位确认、没有集中度：给下界（有确认但强度未知）
        return round(low * max(0.0, min(1.0, seat_weight)), 4)
    span = threshold * (CONCENTRATION_SPAN - 1.0)
    progress = clamp((ratio - threshold) / span, 0.0, 1.0) if span > 0 else 1.0
    bonus = low + (high - low) * progress
    if not seat_confirmed:
        return round(bonus, 4)
    # 席位确认时按权重微调（游资 0.6 → 只在下界之上加 60% 的增量）
    scale = max(0.0, min(1.0, seat_weight))
    return round(low + (bonus - low) * max(scale, 0.5), 4)


def score_leader(item: LeaderInput, outcome: ResonanceOutcome, *,
                 config: MainlineConfig) -> LayerScore:
    """把共振结果包装成 `LayerScore`（**权重恒为 0**，只作展示与 IC 统计）。

    `LayerScore.weight` 留 0 是刻意的：任何把它当成"第三层权重"的用法
    都会悄悄回到 V1.0 的三层加权相加。展示层要读的是 `gate_bonus`。
    """
    cfg = config.leader
    threshold = max(float(cfg.resonance_ratio), 1e-6)
    span = threshold * (CONCENTRATION_SPAN - 1.0)
    concentration_score = None
    if outcome.concentration is not None:
        concentration_score = clamp(
            (outcome.concentration - threshold) / span * 100.0 + 50.0) \
            if span > 0 else 50.0
    seat_score = (min(SEAT_SCORE_CAP, outcome.seat_stocks / max(
        1, int(cfg.seat.min_stocks)) * 60.0)
        if outcome.seat_confirmed else None)
    dims = [
        DimensionScore(key="concentration", label=DIM_LABELS["concentration"],
                       score=concentration_score or 0.0, weight=70.0,
                       raw={"concentration": outcome.concentration,
                            "threshold": cfg.resonance_ratio,
                            "leaders": [row.to_dict() for row in outcome.leaders]},
                       note="；".join(outcome.notes),
                       available=concentration_score is not None),
        DimensionScore(key="seat", label=DIM_LABELS["seat"],
                       score=seat_score or 0.0, weight=30.0,
                       raw={"stocks": outcome.seat_stocks,
                            "note": outcome.seat_note},
                       note=outcome.seat_note,
                       available=seat_score is not None),
    ]
    available = [dim for dim in dims if dim.available]
    score = (sum(dim.score * dim.weight for dim in available)
             / sum(dim.weight for dim in available)) if available else 0.0
    notes = list(outcome.notes)
    if outcome.bonus:
        notes.append(f"门控加分 +{outcome.bonus:.1f}")
    return LayerScore(key="leader", label="龙头共振确认", score=clamp(score),
                      dimensions=dims, weight=0.0,
                      coverage=1.0 if available else 0.0, notes=notes)


def relative_strength(item: LeaderInput, *, window: int = 20,
                      benchmark: BoardSeries | None = None) -> float | None:
    """板块相对基准的超额收益（前端"重点板块跟踪"用；数据不足返回 None）。"""
    closes = [bar.close for bar in item.series.bars if bar.close > 0]
    if len(closes) < window + 1 or not closes[-1 - window]:
        return None
    own = closes[-1] / closes[-1 - window] - 1.0
    if benchmark is None:
        return own
    bench = [bar.close for bar in benchmark.bars if bar.close > 0]
    if len(bench) < window + 1 or not bench[-1 - window]:
        return own
    return own - (bench[-1] / bench[-1 - window] - 1.0)


def leader_percentile(values: list[Any], value: Any) -> float | None:
    """集中度的横截面分位（回测报告里的"龙头维度 IC"直接用原始值算，
    这里只是给面板一个相对位置）。"""
    percentile = rank_percentile(values, value)
    return None if percentile is None else percentile * 100.0


__all__ = [
    "CONCENTRATION_SPAN",
    "DIM_LABELS",
    "SEAT_SCORE_CAP",
    "LeaderInput",
    "ResonanceOutcome",
    "amount_share",
    "evaluate_resonance",
    "gate_bonus",
    "identify_leaders",
    "leader_percentile",
    "relative_strength",
    "score_leader",
    "seat_confirmation",
]
