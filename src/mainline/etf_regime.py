"""ETF 环境调节层：市场风险偏好乘数（`regime_multiplier`）。

## 这个模块做什么

在漏斗三层评分**之后**加一层环境调节：

    最终主线异动评分 = 漏斗三层评分 × regime_multiplier

乘数由核心宽基 ETF 份额变化与指数位置共同决定，外加两条行业级修正。

## 必须先说清的四件事（需求文档里没有，但会决定这个模块是否有用）

### 1. 对**所有**行业乘同一个数，不改变任何排名

`service.py` 的排序键是 `base_total + gate_bonus`，而 `total` 是它 clamp 到
0-100 的结果。给每一项乘同一个正数 `m` 是**单调变换** —— 排序完全不变。

所以全局乘数的真实作用是：**改变绝对分越过告警阈值（强 70 / 中 55）的板块
数量**。`m=1.15` 等效于把告警门槛降到 70/1.15≈60.9，`m=0.85` 则抬到 82.4。
它是一个**全市场告警闸门**，不是"重新排序谁更强"。

这个区别很重要：如果以为它在调排名，就会觉得"调了 15% 怎么排序没动"是 bug；
而实际情况是**它按设计工作**，只是设计目标该被表述成"多出/少出多少条告警"。

只有规则 3 / 规则 4 是**行业级**的，它们才会真正改变排序。

### 2. `total` 已经 clamp 到 0-100，乘数会被截断

`base_total` 最大 100（V2.3 起等于六维层分；此前为六维 50% + 建仓痕迹 50%），
加 `gate_bonus`（0~20）最大 120，最后 `clamp(..., 0, 100)`。所以：

- 高端板块本来就被截到 100，再乘 1.15 仍是 100 —— **调节在高分段失效**；
- 真正被抬过阈值的是中段板块（55~70 区间）。

因此本模块把乘数作用在 **clamp 之前**的 `base_total + gate_bonus` 上，
并同时保留 `pre_regime_total`，让面板能看出"这条告警的原始分是多少"。
作用在 clamp 之后等于对高分段白调，而且看不出来。

### 3. 规则 3 与规则 4 对同一个板块是**互相抵消**的

- 规则 3：行业 ETF 流入排名前 3 → ×1.1
- 规则 4：行业 ETF 流入分位 > 90% → ×0.9

一个"流入排名前 3"的行业，它的流入分位大概率也 > 90% —— 两条规则同时命中，
乘数变成 `1.1 × 0.9 = 0.99`，**约等于没调，而且反转警示被静默抵消**。
这正是需求文档里没有处理的地方（它只写了"此部分在应用到行业评分时单独处理"）。

本模块的裁决：**规则 4 优先，规则 3 让位**。
理由是方向性 —— 规则 4 是风险警示，规则 3 是机会加成；
把风险警示抵消掉会让系统在最需要提示的时候保持沉默，
而"少给一次加成"的代价小得多。该优先级可通过
`precedence` 配置改成 `rule3`（让规则 3 赢）或 `stack`（允许相乘，不推荐）。

### 4. 规则 1 与已回测验证的环境门控**方向冲突**

需求规则 1 是：「沪深300 近 34 日分位 ≤ 30% 且份额 5 日累计 > 5% → 全体 +15%」。

但 `docs/ETF_FLOW_BACKTEST.md` 用 2018-2026 的 1252 个信号验证过：
这个形态（低位 + 份额大增）**只在熊市有效**，在牛市与震荡市是**反向**的：

    熊市（ 77 个）T+34 中位数 +8.78%   胜率 76.6%
    牛市（ 60 个）T+34 中位数 -1.53%   胜率 36.7%
    震荡（445 个）T+34 中位数 -0.70%   胜率 46.5%

按需求原样实现，规则 1 会在牛市回调时把全场评分抬 15% —— 而那正是历史上
后续 34 个交易日**跑输**的情形。

因此 `regime_multiplier.rule1_require_bear` 默认为 `true`：规则 1 只在
市场环境为熊市时生效，其余环境降级为"触发但不调节"（`flags` 里仍会记录，
面板能显示"形态出现但环境不支持"，而不是装作没看见）。
把它设成 `false` 就是需求原样的行为 —— 但**改之前请先看回测章节的实测数字**。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.mainline.etf_flow import (
    REGIME_BEAR,
    EtfIndicator,
    FlowConfig,
    IndexPosition,
    MarketRegime,
    index_percentile,
)

logger = logging.getLogger(__name__)

#: 规则标识（面板/告警用；`ruleN` 与需求文档的编号一一对应）
RULE1 = "rule1"       # 大盘底部机会共振
RULE2 = "rule2"       # 大盘顶部风险警示
RULE3 = "rule3"       # 风格切换（宽基流出 + 行业流入）
RULE4 = "rule4"       # 行业 ETF 极端流入反转警示
RULE_LABELS = {
    RULE1: "🟢 大盘底部机会共振",
    RULE2: "🔴 大盘顶部风险警示",
    RULE3: "🔀 风格切换（宽基→行业）",
    RULE4: "🟡 行业 ETF 极端流入反转",
}

#: 乘数上下限（需求 0.8~1.2；超出会被夹住，防止多层叠加失控）
MIN_MULTIPLIER, MAX_MULTIPLIER = 0.8, 1.2


@dataclass
class RegimeAdjustment:
    """一次环境调节的全部产出（面板与告警都读它）。"""

    trade_date: str = ""
    multiplier: float = 1.0
    #: 沪深300 近 N 日分位（0-1）
    index_percentile: float | None = None
    #: 沪深300 系列 ETF 份额 5 日累计变化（小数）
    hs300_share_5d: float | None = None
    #: 行业主题 ETF 整体份额 5 日累计变化（小数）
    industry_share_5d: float | None = None
    #: 市场环境（来自 `etf_flow` 的 120 日口径）
    regime_key: str = "range"
    regime_label: str = "震荡市"
    #: 已触发的规则（含"触发但不调节"的）
    flags: list[str] = field(default_factory=list)
    #: 触发但因环境/冲突未实际调节的规则 → 面板要显示"形态出现但不生效"
    suppressed: list[str] = field(default_factory=list)
    #: 规则 3：行业 ETF 流入排名前 3 的**组别**
    style_switch_groups: list[str] = field(default_factory=list)
    #: 规则 4：极端流入（分位 > 90%）的组别
    reversal_groups: list[str] = field(default_factory=list)
    #: `{组别: 行业级乘数}`（只有规则 3/4 会在这里出现）
    group_multipliers: dict[str, float] = field(default_factory=dict)
    #: `{组别: 份额 5 日变化}`（面板散点图用）
    group_change_5d: dict[str, float] = field(default_factory=dict)
    banner: str = "⚪ 中性（无环境调节）"
    reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """是否实际产生了调节（面板据此决定要不要显示角标）。"""
        return bool(self.flags)

    def group_multiplier(self, group: str) -> float:
        """某个板块所属组的行业级乘数（无规则命中时为 1.0）。"""
        return float(self.group_multipliers.get(str(group or ""), 1.0))

    def to_dict(self) -> dict[str, Any]:
        return {"trade_date": self.trade_date,
                "multiplier": round(self.multiplier, 4),
                "index_percentile": (round(self.index_percentile, 4)
                                     if self.index_percentile is not None else None),
                "hs300_share_5d": self.hs300_share_5d,
                "industry_share_5d": self.industry_share_5d,
                "regime_key": self.regime_key,
                "regime_label": self.regime_label,
                "flags": list(self.flags),
                "flags_label": [RULE_LABELS.get(item, item) for item in self.flags],
                "suppressed": list(self.suppressed),
                "style_switch_groups": list(self.style_switch_groups),
                "reversal_groups": list(self.reversal_groups),
                "group_multipliers": dict(self.group_multipliers),
                "group_change_5d": dict(self.group_change_5d),
                "banner": self.banner, "reasons": list(self.reasons),
                "gaps": list(self.gaps)}


# ==================================================================
# 阈值
# ==================================================================


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else default


def _median(values: Sequence[float]) -> float | None:
    """中位数。用中位数而不是均值汇总同系列 ETF：4 只沪深300 ETF 里
    可能只有一只被大额申赎（做市/套利），均值和会被单只主导。"""
    clean = sorted(item for item in values if item is not None)
    if not clean:
        return None
    middle = len(clean) // 2
    if len(clean) % 2:
        return clean[middle]
    return (clean[middle - 1] + clean[middle]) / 2.0


@dataclass
class MultiplierConfig:
    """`configs/etf_flow.yaml` 的 `regime_multiplier` 段。"""

    enabled: bool = False
    #: 规则 1 是否要求熊市环境（见模块文档第 4 条）
    rule1_require_bear: bool = True
    #: 规则 3/4 冲突时的裁决：`rule4`（默认）/ `rule3` / `stack`
    precedence: str = "rule4"
    rule1_multiplier: float = 1.15
    rule2_multiplier: float = 0.85
    rule3_multiplier: float = 1.10
    rule4_multiplier: float = 0.90
    rule1_max_percentile: float = 0.30
    rule1_min_share_5d: float = 0.05
    rule2_min_percentile: float = 0.70
    rule2_max_share_5d: float = -0.05
    rule3_broad_max_share_5d: float = -0.02
    rule3_industry_min_share_5d: float = 0.02
    rule3_top_n: int = 3
    rule4_min_percentile: float = 0.90
    #: 规则 4 的分位回看窗口（交易日）
    rule4_window: int = 250
    percentile_window: int = 34


def load_multiplier_config(config: FlowConfig) -> MultiplierConfig:
    """从 `etf_flow.yaml` 读 `regime_multiplier` 段（缺失时用默认值）。

    刻意**复用 `etf_flow.yaml`** 而不是新建一个 `etf_config.yaml`：
    观察清单、阈值、乘数参数都是同一件事的分层，拆成两个文件会让
    "改观察池要记得同步改另一个文件里的分组" —— 这类不一致只会在
    面板上表现为某个板块永远没有行业乘数，排查成本极高。
    """
    raw = config.threshold("regime_multiplier")
    out = MultiplierConfig()
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if not hasattr(out, key):
            continue
        current = getattr(out, key)
        if isinstance(current, bool):
            setattr(out, key, bool(value))
        elif isinstance(current, int) and not isinstance(current, bool):
            number = _num(value)
            if number is not None:
                setattr(out, key, int(number))
        elif isinstance(current, float):
            number = _num(value)
            if number is not None:
                setattr(out, key, number)
        elif isinstance(current, str):
            setattr(out, key, str(value))
    # 分位窗口与 etf_flow 的其他阈值保持一致，避免同一个概念两个数
    window = _num(config.threshold("percentile_window"))
    if window:
        out.percentile_window = int(window)
    return out


# ==================================================================
# 计算
# ==================================================================


def _group_change_5d(indicators: dict[str, EtfIndicator], config: FlowConfig,
                     group_keys: Sequence[str]) -> dict[str, float]:
    """各组的份额 5 日累计变化（组内取中位数）。"""
    out: dict[str, float] = {}
    for group in config.groups:
        if group_keys and group.key not in group_keys:
            continue
        values = []
        for spec in group.etfs:
            item = indicators.get(spec.code)
            if item is not None and item.change_5d is not None:
                values.append(float(item.change_5d))
        value = _median(values)
        if value is not None:
            out[group.key] = value
    return out


def compute_adjustment(indicators: dict[str, EtfIndicator],
                       positions: dict[str, IndexPosition], *,
                       config: FlowConfig,
                       regime: MarketRegime | None = None,
                       share_history: dict[str, Sequence[float]] | None = None,
                       trade_date: str = "") -> RegimeAdjustment:
    """算当日的 `regime_multiplier` 与两条行业级修正。

    `share_history` 是 `{etf_code: [5 日累计变化, ...]}`（升序，含当日），
    只用于规则 4 的历史分位。缺它就跳过规则 4 并在 `gaps` 里说明 ——
    **不猜分位**：把"没有历史"当成"分位不高"会让反转警示静默消失。
    """
    cfg = load_multiplier_config(config)
    out = RegimeAdjustment(trade_date=trade_date)
    if regime is not None:
        out.regime_key = regime.key
        out.regime_label = regime.label

    broad = [group.key for group in config.groups if group.level != "industry"]
    industry = [group.key for group in config.groups if group.level == "industry"]
    changes = _group_change_5d(indicators, config, [])
    out.group_change_5d = dict(changes)

    # 沪深300 系列：显式优先取名为 hs300 的组，否则取第一个非行业组
    anchor = "hs300" if "hs300" in changes else next(
        (key for key in broad if key in changes), "")
    if not anchor:
        out.gaps.append("观测清单里没有可用的宽基组，无法计算环境乘数")
        return out
    out.hs300_share_5d = changes.get(anchor)

    # 指数分位取锚点组对应指数的（不是清单里第一个指数）
    index_code = ""
    for group in config.groups:
        if group.key == anchor and group.index:
            index_code = group.index
            break
    position = positions.get(index_code) if index_code else None
    if position is not None:
        out.index_percentile = position.percentile
    else:
        out.gaps.append(f"锚点组 {anchor} 的指数分位不可用（缺指数日线）")

    industry_values = [changes[key] for key in industry if key in changes]
    out.industry_share_5d = _median(industry_values) if industry_values else None

    share = out.hs300_share_5d
    percentile = out.index_percentile
    multiplier = 1.0

    # ---- 规则 1：底部机会共振 ----
    if share is not None and percentile is not None and \
            percentile <= cfg.rule1_max_percentile and share > cfg.rule1_min_share_5d:
        out.flags.append(RULE1)
        blocked = cfg.rule1_require_bear and out.regime_key != REGIME_BEAR
        if blocked:
            out.suppressed.append(RULE1)
            out.reasons.append(
                f"⚠️ 规则1 形态已出现（分位 {percentile * 100:.1f}% ≤ "
                f"{cfg.rule1_max_percentile * 100:.0f}%，份额5日 "
                f"{share * 100:+.2f}% > {cfg.rule1_min_share_5d * 100:.0f}%），"
                f"但当前 {out.regime_label} —— 该形态只在熊市有正收益"
                "（牛市/震荡市历史为负），故不调节")
        else:
            multiplier *= cfg.rule1_multiplier
            out.reasons.append(
                f"🟢 规则1 大盘底部机会共振：分位 {percentile * 100:.1f}% ≤ "
                f"{cfg.rule1_max_percentile * 100:.0f}%，沪深300系列份额5日 "
                f"{share * 100:+.2f}% > {cfg.rule1_min_share_5d * 100:.0f}% "
                f"→ 全场 ×{cfg.rule1_multiplier}")

    # ---- 规则 2：顶部风险警示 ----
    if share is not None and percentile is not None and \
            percentile >= cfg.rule2_min_percentile and share < cfg.rule2_max_share_5d:
        out.flags.append(RULE2)
        multiplier *= cfg.rule2_multiplier
        out.reasons.append(
            f"🔴 规则2 大盘顶部风险警示：分位 {percentile * 100:.1f}% ≥ "
            f"{cfg.rule2_min_percentile * 100:.0f}%，沪深300系列份额5日 "
            f"{share * 100:+.2f}% < {cfg.rule2_max_share_5d * 100:.0f}% "
            f"→ 全场 ×{cfg.rule2_multiplier}（已告警板块提示止盈）")

    # ---- 规则 3：风格切换 ----
    top_groups: list[str] = []
    if share is not None and out.industry_share_5d is not None and \
            share < cfg.rule3_broad_max_share_5d and \
            out.industry_share_5d > cfg.rule3_industry_min_share_5d:
        out.flags.append(RULE3)
        ranked = sorted((key for key in industry if key in changes),
                        key=lambda key: -changes[key])
        top_groups = ranked[: max(int(cfg.rule3_top_n), 1)]
        out.style_switch_groups = list(top_groups)
        out.reasons.append(
            f"🔀 规则3 风格切换：沪深300系列份额5日 {share * 100:+.2f}% < "
            f"{cfg.rule3_broad_max_share_5d * 100:.0f}%，行业主题 ETF "
            f"{out.industry_share_5d * 100:+.2f}% > "
            f"{cfg.rule3_industry_min_share_5d * 100:.0f}% → 流入前 "
            f"{len(top_groups)} 名（{'、'.join(top_groups)}）额外 "
            f"×{cfg.rule3_multiplier}")

    # ---- 规则 4：行业 ETF 极端流入反转 ----
    reversal: list[str] = []
    if share_history:
        for key in industry:
            values: list[float] = []
            for group in config.groups:
                if group.key != key:
                    continue
                for spec in group.etfs:
                    values.extend(float(v) for v in share_history.get(spec.code, ())
                                  if v is not None)
            if len(values) < max(int(cfg.rule4_window) // 4, 20):
                continue
            current = changes.get(key)
            if current is None:
                continue
            rank = index_percentile(values + [current], window=len(values) + 1,
                                    mode="rank")
            if rank is not None and rank > cfg.rule4_min_percentile:
                reversal.append(key)
    elif industry:
        out.gaps.append(
            "缺少份额历史序列，规则4（行业 ETF 极端流入反转）未参与计算")

    if reversal:
        out.flags.append(RULE4)
        out.reversal_groups = sorted(reversal)
        out.reasons.append(
            f"🟡 规则4 行业 ETF 极端流入反转风险："
            f"{'、'.join(out.reversal_groups)} 的份额5日变化处于历史前 "
            f"{(1 - cfg.rule4_min_percentile) * 100:.0f}% 分位 → "
            f"×{cfg.rule4_multiplier}（ETF 资金流对行业短期收益呈负向预测）")

    _resolve_industry_multipliers(out, cfg, top_groups, reversal)
    out.multiplier = max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, multiplier))
    out.banner = _banner(out)
    return out


def _resolve_industry_multipliers(out: RegimeAdjustment, cfg: MultiplierConfig,
                                  top_groups: Sequence[str],
                                  reversal: Sequence[str]) -> None:
    """裁决规则 3 与规则 4 在同一板块上的冲突。

    两条规则对"流入排名前 3"的行业大概率同时命中：×1.1 与 ×0.9 相乘
    等于 ×0.99，**风险警示被静默抵消** —— 系统在最该提示时保持沉默。

    默认让规则 4 优先：少给一次机会加成的代价，远小于漏掉一次反转警示。
    """
    table: dict[str, float] = {}
    for key in top_groups:
        if cfg.precedence == "rule4" and key in reversal:
            continue                       # 让位给规则 4，不叠加
        table[key] = table.get(key, 1.0) * cfg.rule3_multiplier
    for key in reversal:
        if cfg.precedence == "rule3" and key in top_groups:
            table[key] = table.get(key, 1.0) * cfg.rule3_multiplier
        elif cfg.precedence == "stack":
            table[key] = table.get(key, 1.0) * cfg.rule4_multiplier
        else:
            table[key] = table.get(key, 1.0) * cfg.rule4_multiplier
    out.group_multipliers = {key: max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, value))
                             for key, value in table.items()}


def _banner(out: RegimeAdjustment) -> str:
    """顶部横幅文案。被抑制的规则要显式说出来，不能让它看起来像没触发。"""
    if RULE1 in out.suppressed:
        return "⚪ 中性（底部形态出现，但非熊市不放行）"
    if RULE2 in out.flags:
        return f"🔴 大盘顶部警示 · 全场 ×{out.multiplier:.2f}"
    if RULE1 in out.flags:
        return f"🟢 大盘底部辅助信号 · 全场 ×{out.multiplier:.2f}"
    if RULE3 in out.flags or RULE4 in out.flags:
        return "🔀 结构性调节（行业级，全场 ×1.00）"
    return "⚪ 中性（无环境调节）"


def adjusted_total(base_total: float, gate_bonus: float, *,
                   adjustment: RegimeAdjustment,
                   group: str = "") -> tuple[float, float]:
    """把乘数作用到 `base_total + gate_bonus` 上，返回 `(最终分, 调节前分)`。

    作用在 clamp **之前**（见模块文档第 2 条）：`total` 本来就被截到 0-100，
    对已经截断的高分再乘 1.15 是白调，而且在界面上看不出来。
    这里回传调节前分数，面板才能显示"原始分 → 调节后分"。
    """
    raw = float(base_total or 0.0) + float(gate_bonus or 0.0)
    factor = float(adjustment.multiplier) * adjustment.group_multiplier(group)
    return raw * factor, raw


__all__ = [
    "MAX_MULTIPLIER",
    "MIN_MULTIPLIER",
    "RULE1",
    "RULE2",
    "RULE3",
    "RULE4",
    "RULE_LABELS",
    "MultiplierConfig",
    "RegimeAdjustment",
    "adjusted_total",
    "compute_adjustment",
    "load_multiplier_config",
]
