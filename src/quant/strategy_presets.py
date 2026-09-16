"""策略预设库：把公开文献里被反复验证过的**择时范式**写成可直接回测的条件。

## 收录标准（不是"我觉得会涨"）

只收**有公开研究支持、且机制可解释**的范式，并对每一个都写明它为什么可能有效：

- **时间序列动量（TSMOM）** — Moskowitz, Ooi & Pedersen (2012),
  *Time Series Momentum*：过去 12 个月上涨的资产，未来一个月倾向继续上涨；
  跨资产类别稳健。
- **趋势跟踪（200 日均线）** — Faber (2007), *A Quantitative Approach to
  Tactical Asset Allocation*；Hurst/Ooi/Pedersen (2017),
  *A Century of Evidence on Trend-Following*：长期均线之上持有、之下离场，
  主要价值在**规避大回撤**而非提高收益。
- **双均线 + 绝对动量过滤** — Faber (2007) 的组合形式：快线上穿慢线进场，
  但要求 12 个月绝对动量为正，过滤熊市里的假突破。
- **低波动 + 趋势** — 低波动异象（Ang et al. 2006；
  Baker-Bradley-Wurgler 2011）+ 趋势：波动率低的阶段持有，波动放大时离场。
- **回撤修复** — 行为金融里的处置效应/过度反应：深度回撤后企稳
  （收复短均线）时介入。

**必须说清的两件事**：

1. 这些范式在**指数/多资产**上有较强证据，在**单只 A 股**上的证据弱得多
   （个股噪声远大于指数，且 A 股制度与样本期差异大）。所以本模块只提供
   "可回测的候选"，不宣称"有效"—— 有效性必须由**样本外 + 跨股票篮子**的
   实测说话（见 `scripts/quant_strategy_scan.py`）。
2. 收录这些预设**不等于**推荐使用它们。回测结果不好看时，结论是
   "这个范式在这只票/这段样本上没有效"，而不是"参数没调好"。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategyPreset:
    """一个可回测的策略范式。"""

    key: str
    label: str
    entry: str
    exit: str
    note: str                    # 机制与依据
    params: dict[str, float] = field(default_factory=dict)
    """回测参数覆盖（止损/止盈/最长持有等）；空 = 用界面上的默认值。"""

    def as_dict(self) -> dict[str, object]:
        return {"key": self.key, "label": self.label, "entry": self.entry,
                "exit": self.exit, "note": self.note, "params": self.params}


PRESETS: tuple[StrategyPreset, ...] = (
    StrategyPreset(
        key="tsmom_12m",
        label="时间序列动量（12 个月）",
        entry="DELTA(close, 250) > 0 AND close > MA(close, 200)",
        exit="DELTA(close, 250) < 0",
        note="Moskowitz/Ooi/Pedersen (2012)：过去 12 个月上涨则持有。"
             "只用绝对动量、不看相对排名，因此天然是「择时」而非「选股」。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.0},
    ),
    StrategyPreset(
        key="faber_200ma",
        label="趋势跟踪（200 日均线）",
        entry="close > MA(close, 200)",
        exit="close < MA(close, 200)",
        note="Faber (2007)：月线级别站上 10 月均线（≈200 日）则持有。"
             "核心价值是**把最大回撤砍掉一大截**，而不是提高年化收益。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.0},
    ),
    StrategyPreset(
        key="dual_ma_momentum",
        label="双均线 + 绝对动量过滤",
        entry="close > MA(close, 50) AND MA(close, 50) > MA(close, 200) "
              "AND DELTA(close, 250) > 0",
        exit="close < MA(close, 50)",
        note="Faber (2007) 的组合形式：快线上穿慢线进场，且要求 12 个月绝对动量为正，"
             "用来过滤熊市里的假突破。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.0},
    ),
    StrategyPreset(
        key="lowvol_trend",
        label="低波动 + 趋势",
        entry="close > MA(close, 120) AND ZSCORE_TS(volatility_20, 120) < 0",
        exit="close < MA(close, 120) OR ZSCORE_TS(volatility_20, 120) > 1",
        note="低波动异象（Ang et al. 2006）：低波动阶段的收益风险比更高；"
             "配合趋势过滤避免在下跌中硬扛。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.0},
    ),
    StrategyPreset(
        key="drawdown_recovery",
        label="回撤修复",
        entry="PCTL_TS(close, 250) < 30 AND close > MA(close, 20)",
        exit="PCTL_TS(close, 250) > 70 OR close < MA(close, 20)",
        note="过度反应/处置效应：深度回撤后重新站上短均线时介入，回到高位区离场。"
             "属于均值回复类，与趋势类负相关，可作为互补。",
        params={"max_hold_days": 60, "stop_loss_pct": 0.10},
    ),
    StrategyPreset(
        key="quality_momentum",
        label="质量 + 动量（基本面过滤）",
        entry="roe > 12 AND DELTA(close, 120) > 0 AND close > MA(close, 60)",
        exit="close < MA(close, 60)",
        note="质量因子（高 ROE）+ 动量：用基本面排除基本面恶化的标的，"
             "再用动量决定介入时点。依赖 PIT 财务数据，按月对齐公告日。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.10},
    ),
    StrategyPreset(
        key="value_trend",
        label="低估值 + 趋势",
        entry="pe_ttm > 0 AND pe_ttm < 25 AND close > MA(close, 120)",
        exit="close < MA(close, 120) OR pe_ttm > 45",
        note="估值提供安全边际、趋势提供介入时点。注意：PE 为负或极高时"
             "会被条件直接排除（`pe_ttm > 0` 那一段不是装饰）。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.12},
    ),
    # ---------- 以下为基于本项目 35 因子的组合（动量/波动/流动性/质量/价值） ----------
    StrategyPreset(
        key="mom120_trend",
        label="中期动量 + 趋势确认",
        entry="momentum_120 > 0 AND close > MA(close, 60)",
        exit="momentum_120 < 0 OR close < MA(close, 60)",
        note="动量因子（120 日）与趋势同向时才持有。动量是 A 股上被反复报告的"
             "少数稳健因子之一，但反转同样剧烈，所以用趋势做第二重确认。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.10},
    ),
    StrategyPreset(
        key="mom60_lowvol",
        label="动量 + 低波动筛选",
        entry="momentum_60 > 0 AND volatility_20 < 0.035 AND close > MA(close, 20)",
        exit="close < MA(close, 20)",
        note="动量叠加低波动过滤：高波动阶段的动量最容易反转（拥挤交易），"
             "用 volatility_20 的绝对阈值把这类阶段排除。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.08},
    ),
    StrategyPreset(
        key="reversal_lowvol",
        label="短期反转 + 低波动",
        entry="reversal_5 > 0 AND volatility_20 < 0.04 AND close > MA(close, 10)",
        exit="PCTL_TS(close, 60) > 80 OR close < MA(close, 10)",
        note="短期反转因子（A 股散户结构下显著）+ 低波动过滤。"
             "与动量类策略负相关，作为组合里的互补项。",
        params={"max_hold_days": 30, "stop_loss_pct": 0.08},
    ),
    StrategyPreset(
        key="quality_lowvol",
        label="质量 + 低波动",
        entry="roe > 10 AND volatility_20 < 0.03 AND close > MA(close, 60)",
        exit="close < MA(close, 60) OR roe < 5",
        note="质量（ROE）与低波动的经典组合（'quality minus junk' 思路的简化版）。"
             "依赖 PIT 财务数据，按公告日对齐，不会用到未公布的报表。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.10},
    ),
    StrategyPreset(
        key="flow_trend",
        label="资金流 + 趋势",
        entry="money_flow_ratio > 0 AND close > MA(close, 20)",
        exit="money_flow_ratio < 0 OR close < MA(close, 20)",
        note="主力资金净流入为正且价格在短均线上方时持有。资金流因子的"
             "信号衰减快，所以配了较短的趋势离场条件。",
        params={"max_hold_days": 20, "stop_loss_pct": 0.07},
    ),
    StrategyPreset(
        key="turnover_breakout",
        label="放量突破",
        entry="close > REF(MAX_TS(close, 60), 1) AND volume_ratio > 1.2",
        exit="close < MA(close, 20)",
        note="突破 60 日新高且成交放大（volume_ratio > 1.2）。"
             "放量是突破有效性的常见确认条件（无量突破多为假突破）。",
        params={"max_hold_days": 40, "stop_loss_pct": 0.08},
    ),
    StrategyPreset(
        key="atr_channel",
        label="ATR 通道趋势",
        entry="close > MA(close, 20) + 2 * atr_20",
        exit="close < MA(close, 20)",
        note="以 ATR 作为波动自适应通道宽度（海龟交易法则的简化形式）："
             "高波动时通道自动放宽，减少被噪声打掉的次数。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.10},
    ),
    StrategyPreset(
        key="relstrength_index",
        label="相对强度（跑赢指数）+ 趋势",
        entry="relative_strength > 0 AND close > MA(close, 60)",
        exit="relative_strength < 0 OR close < MA(close, 60)",
        note="只买**相对指数走强**的标的：相对强度为正是「跑赢大盘」的直接度量，"
             "比绝对动量更贴近「超额收益」这个目标。",
        params={"max_hold_days": 0, "stop_loss_pct": 0.10},
    ),
    StrategyPreset(
        key="stabilize_ma200",
        label="长期均线企稳修复",
        entry="close > MA(close, 200) AND close < MA(close, 200) * 1.06",
        exit="close < MA(close, 200) OR close > MA(close, 200) * 1.35",
        note="只在**刚站上 200 日线不久**（不超过 6%）时介入，涨到偏离 35% 就止盈。"
             "避免在长期上涨的尾段追高。",
        params={"max_hold_days": 120, "stop_loss_pct": 0.09},
    ),
    StrategyPreset(
        key="drawdown_buy_quality",
        label="回撤 + 质量双过滤",
        entry="PCTL_TS(close, 250) < 40 AND roe > 8 AND close > MA(close, 20)",
        exit="PCTL_TS(close, 250) > 75",
        note="深度回撤（近 250 日分位 < 40%）+ 基本面未恶化（ROE > 8）+ 短期企稳。"
             "三条同时成立才买，用它压制'越跌越买'的冲动。",
        params={"max_hold_days": 90, "stop_loss_pct": 0.12},
    ),
)

PRESET_BY_KEY = {preset.key: preset for preset in PRESETS}


def list_presets() -> list[dict[str, object]]:
    return [preset.as_dict() for preset in PRESETS]


def get_preset(key: str) -> StrategyPreset:
    preset = PRESET_BY_KEY.get(key)
    if preset is None:
        raise KeyError(f"未知策略预设 {key!r}；可用："
                       f"{', '.join(PRESET_BY_KEY)}")
    return preset


__all__ = ["PRESETS", "PRESET_BY_KEY", "StrategyPreset", "get_preset",
           "list_presets"]
