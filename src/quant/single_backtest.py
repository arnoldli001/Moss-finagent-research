"""单股票多因子条件策略回测：把 DSL 条件变成可成交的交易，算出可复核的净值。

## 为什么之前的多因子回测不能回答"这只票该怎么买"

原来的多因子回测是**截面**逻辑（IC/ICIR/分层组合）：它回答的是"按因子排序分组，
哪一组更好"，输出的是组合收益。它天然**没法回答**"我就想交易 600519，什么时候买、
什么时候卖、能赚多少" —— 那需要的是**时序**逻辑：一只票的历史 × 一条择时规则。

## 撮合口径（每一条都按 A 股真实约束，宁可保守）

- **信号时点**：第 t 日收盘后算出的信号，在第 **t+1 日开盘**成交。
  用 t 日收盘价成交＝用了收盘后才知道的信息，是最常见的未来函数。
- **T+1**：当日买入的股票当日不可卖（交易所规则）。
- **整手**：买入股数向下取整到 100 股（真实委托约束）。
- **涨停不买 / 跌停不卖**：开盘触及涨停→放弃买入，收盘触及跌停→放弃卖出。
  一字板挂单成交不了，"回测里成交了"是最典型的虚高来源。
- **停牌**：停牌日不可买不可卖（停牌期间价格不动，硬算会凭空产生收益）。
- **止损/止盈同日触发**：一律按**止损**先成交。无法确知盘中先后顺序，
  取对策略不利的一侧。
- **费用**：佣金万三（最低 5 元）+ 卖出印花税 + 过户费 + 滑点（默认 5bp）。
  用真实费率；滑点单独列出，不藏进费率里。

**止损价的成交假设**：跳空低于止损价时按开盘价成交（不是止损价）——
止损单在跳空缺口里只能以更差的价格成交。

## 全样本 / 训练集 / 样本外

单股票回测最大的风险是**过拟合**：一只票几千个交易日，换几组参数总能拟合出漂亮曲线。
所以结果强制拆三段，并且**样本外那一段才是该看的**。同时给出交易笔数、
买入持有基准、以及"参数越多越不可信"的显式提醒 —— 这些提醒本身就是交付物的一部分。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.quant.condition_dsl import Condition, ConditionError, parse_condition

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
LOT_SIZE = 100          # A 股一手 = 100 股
RISK_FREE_RATE = 0.02   # 夏普的无风险利率（年化）


@dataclass
class CostConfig:
    """交易成本（默认取 2026 年散户常见费率）。"""

    commission_rate: float = 0.0003     # 佣金 万三（双边）
    min_commission: float = 5.0         # 单笔最低 5 元
    stamp_tax_rate: float = 0.0005      # 印花税 0.05%（**仅卖出**）
    transfer_fee_rate: float = 0.00001  # 过户费 0.001%（双边）
    slippage_bps: float = 5.0           # 滑点 5bp（0.05%）

    def buy_cost(self, amount: float) -> float:
        return (max(amount * self.commission_rate, self.min_commission)
                + amount * self.transfer_fee_rate)

    def sell_cost(self, amount: float) -> float:
        return (max(amount * self.commission_rate, self.min_commission)
                + amount * self.transfer_fee_rate
                + amount * self.stamp_tax_rate)

    @property
    def slippage(self) -> float:
        return self.slippage_bps / 10_000.0


@dataclass
class SingleBacktestConfig:
    """单股票策略参数。"""

    code: str = ""
    entry: str = ""                  # 入场条件（DSL，时序模式）
    exit: str = ""                   # 出场条件；空 = 只靠止损/止盈/最长持有
    initial_cash: float = 100_000.0
    position_pct: float = 1.0        # 每次买入使用的资金比例
    stop_loss_pct: float = 0.0       # 0 = 不启用（相对买入价的跌幅）
    take_profit_pct: float = 0.0     # 0 = 不启用
    max_hold_days: int = 20          # 最长持有交易日；<=0 表示不限制
    min_hold_days: int = 1           # 最短持有交易日
    t_plus_1: bool = True
    respect_price_limits: bool = True
    respect_suspension: bool = True
    train_ratio: float = 0.7
    index_code: str = "000300.SH"    # 指数基准（默认沪深300）
    recent_days: int = 240           # "近一年"分段长度（交易日）
    costs: CostConfig = field(default_factory=CostConfig)


@dataclass
class Trade:
    """一笔完整的买入 → 卖出。"""

    code: str
    entry_date: str
    entry_price: float
    shares: int
    entry_cost: float
    exit_date: str
    exit_price: float
    exit_cost: float
    hold_days: int
    pnl: float
    return_pct: float
    exit_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_date": self.entry_date,
            "entry_price": round(self.entry_price, 3),
            "shares": self.shares, "exit_date": self.exit_date,
            "exit_price": round(self.exit_price, 3),
            "hold_days": self.hold_days, "pnl": round(self.pnl, 2),
            "return_pct": round(self.return_pct * 100, 2),
            "exit_reason": self.exit_reason,
            "cost": round(self.entry_cost + self.exit_cost, 2),
        }


@dataclass
class SingleBacktestResult:
    """回测结果（全部可直接 JSON 化给前端）。"""

    code: str = ""
    name: str = ""
    index_code: str = ""
    dates: list[str] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    benchmark: list[float] = field(default_factory=list)
    benchmark_index: list[float] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    segments: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    entry_condition: dict[str, Any] = field(default_factory=dict)
    exit_condition: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name,
            "index_code": self.index_code,
            "dates": self.dates, "equity": self.equity,
            "benchmark": self.benchmark,
            "benchmark_index": self.benchmark_index,
            "positions": self.positions,
            "trades": [trade.as_dict() for trade in self.trades],
            "metrics": self.metrics, "segments": self.segments,
            "verdict": self.verdict,
            "entry_condition": self.entry_condition,
            "exit_condition": self.exit_condition,
            "warnings": self.warnings, "notes": self.notes,
            "config": self.config,
        }


# ==================================================================
# 时序因子帧
# ==================================================================


def stock_frame(panels: Any, code: str, *,
                extra: dict[str, pd.Series] | None = None) -> pd.DataFrame:
    """把面板里"这只票"的所有字段抽成一列一列的时间序列（index=日期）。

    这一步是单股票回测的关键转置：面板是 `字段 → (日期 × 股票)`，
    单股票策略要的是 `字段 → (日期)`。
    """
    columns: dict[str, pd.Series] = {}
    for key, panel in _iter_factor_panels(panels):
        if code not in panel.columns:
            continue
        columns[key] = panel[code].astype("float64")
    for key, series in (extra or {}).items():
        columns[key] = series.astype("float64")
    if not columns:
        return pd.DataFrame()
    frame = pd.DataFrame(columns)
    frame.index = frame.index.map(str)
    frame = frame.sort_index()
    fundamentals = _fundamental_frame(panels, code, list(frame.index))
    if len(fundamentals.columns):
        # 因子键与财务原始列会重名（如 `roe`：既在因子库里，也是 Tushare 的字段名）。
        # 保留因子版（它带方向归一，含义明确），并**把冲突记进 attrs** 而不是静默丢弃 ——
        # 否则用户写 `roe > 10` 时永远不知道自己用的是哪一版。
        overlap = [column for column in fundamentals.columns
                   if column in frame.columns]
        if overlap:
            frame.attrs["column_collisions"] = sorted(overlap)
            fundamentals = fundamentals.drop(columns=overlap)
        if len(fundamentals.columns):
            frame = frame.join(fundamentals, how="left")
    frame = _add_raw_quotes(frame, panels, code)
    return frame


def _add_raw_quotes(frame: pd.DataFrame, panels: Any,
                    code: str) -> pd.DataFrame:
    """补上**真实报价**（未复权）的开高低收列。

    面板里的 `open/high/low/close` 是后复权价，比例关系正确但不等于真实报价。
    写 `close > 1000` 这类绝对阈值时，用户想说的几乎一定是真实价，
    所以这里额外给出 `*_raw` 列，把两件事分开：
        指标/比例用它自己的口径 → 用 `close`（后复权）
        绝对价格阈值         → 用 `close_raw`
    """
    pools = getattr(panels, "prices", {}) or {}
    adj_close = pools.get("close")
    raw_close = pools.get("close_raw")
    if not isinstance(adj_close, pd.DataFrame) or code not in adj_close.columns:
        return frame
    if not isinstance(raw_close, pd.DataFrame) or code not in raw_close.columns:
        return frame
    base = pd.DataFrame(index=frame.index)
    base["close"] = adj_close[code].reindex(frame.index).astype("float64")
    base["close_raw"] = raw_close[code].reindex(frame.index).astype("float64")
    factor = adjust_factor(base)
    if factor is None:
        return frame
    raw_close_series = base["close_raw"]
    for key in ("open", "high", "low"):
        panel = pools.get(key)
        if isinstance(panel, pd.DataFrame) and code in panel.columns:
            adjusted = panel[code].reindex(frame.index).astype("float64")
            frame[f"{key}_raw"] = adjusted / factor
    frame["close_raw"] = raw_close_series
    return frame


def _iter_factor_panels(panels: Any) -> list[tuple[str, pd.DataFrame]]:
    """面板里的全部宽表（价格、估值、资金流、涨跌停、备用行情）。

    财务面板**不在这里** —— 见 `_fundamental_frame` 的说明。
    """
    pools: list[dict[str, pd.DataFrame]] = [
        getattr(panels, "prices", {}) or {},
        getattr(panels, "basics", {}) or {},
        getattr(panels, "flows", {}) or {},
        getattr(panels, "limits", {}) or {},
        getattr(panels, "bak", {}) or {},
    ]
    out: list[tuple[str, pd.DataFrame]] = []
    for pool in pools:
        for key, value in pool.items():
            if isinstance(value, pd.DataFrame) and len(value.columns):
                out.append((str(key), value))
    return out


def _pit_series(subset: pd.DataFrame, dates: Sequence[str]) -> pd.DataFrame:
    """单只票的 PIT 时序：每个交易日取"当时可见"的那一版财务数据。

    语义与 `PitPanel.as_of` 完全一致（只取 `usable_date ≤ 当日` 的最后一条），
    但**一次性向量化**而不是逐日调用：

    实测 5030 个交易日的全历史回测里，逐日 `as_of` 要 ~10 秒
    （每次都对小表做 过滤+排序+去重）；这里用 `searchsorted` 一次算完。

    平局（同一 `usable_date` 有多条记录，例如同日公告多个报告期）时必须与
    `as_of` 的 `drop_duplicates(keep="last")` 取同一条 —— 所以排序用
    `kind="stable"`，保证同键内部保持原始顺序，`side="right"` 取到最后一个。
    """
    ordered = subset.sort_values("usable_date", kind="stable")
    usable = ordered["usable_date"].astype(str).to_numpy()
    if usable.size == 0:
        return pd.DataFrame()
    targets = pd.Series([str(day) for day in dates])
    positions = np.searchsorted(usable, targets.to_numpy(), side="right") - 1
    valid = positions >= 0
    if not valid.any():
        return pd.DataFrame()
    picked = ordered.iloc[positions[valid]]
    metrics = [column for column in ordered.columns
               if column not in ("code", "name", "report_period", "ann_date",
                                 "usable_date")]
    frame = picked[metrics].copy()
    frame.index = targets[valid].to_numpy()
    frame.index.name = None
    return frame.astype("float64", errors="ignore")


def _fundamental_frame(panels: Any, code: str,
                       dates: Sequence[str]) -> pd.DataFrame:
    """只取这一只票的 PIT 财务时间序列（逐日"当时可见"的那一版）。

    **为什么不复用 `panels.fundamental_field(key)`**：那是给整个截面用的接口，
    对每个指标都要把 171 个交易日 × 5238 只股票的全市场面板物化一遍。
    单股票回测只需要一列，走那条路实测会把一次回测拖到十分钟以上还没结束
    （每个指标 171 次全市场 `as_of`）。

    这里先把 PIT 面板过滤成"只有这一只票"，再用 `_pit_series` 一次性算出
    全区间的时间序列 —— PIT 语义与 `PitPanel.as_of` 一致（公告日之后才可见、
    保留修正版本），但 5030 个交易日只要一次向量化运算。
    """
    root = getattr(panels, "fundamentals", None)
    if root is None or len(root) == 0:
        return pd.DataFrame(index=list(dates))
    records = getattr(root, "records", None)
    if records is None or len(records) == 0:
        return pd.DataFrame(index=list(dates))
    target = str(code).zfill(6)
    subset = records[records["code"].astype(str).str.zfill(6) == target]
    if len(subset) == 0:
        return pd.DataFrame(index=list(dates))
    frame = _pit_series(subset, [str(day) for day in dates])
    if frame.empty:
        return pd.DataFrame(index=list(dates))
    return frame.reindex(list(dates))


def _suspended_pairs(panels: Any) -> set[tuple[str, str]]:
    raw = getattr(panels, "suspended", None) or set()
    return {(str(day), str(code)) for day, code in raw}


# ==================================================================
# 回测主循环
# ==================================================================


def slice_panels(panels: Any, code: str) -> Any:
    """把面板裁成"只有这一只票"。

    **为什么这样等价**：35 个因子全部是**逐列独立**的（滚动窗口只在自己这一列上做，
    没有 `axis=1` 的截面运算），所以裁到单列再算，结果与全市场算完再取一列完全一致；
    而计算量降到 1/股票数 —— 实测全市场 171 天 × 35 因子要几分钟，
    单列是毫秒级。这是"单股票回测"能秒级出结果的前提。

    保留 `fundamentals` 与 `index_returns` 的原始引用：前者是 PIT 面板（按公告日对齐），
    后者是相对强度的基准，两者都不是"按股票列"的结构，裁员会改变语义。
    """
    from src.quant.panels import FactorPanels

    def box(pool: Any) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for key, value in (pool or {}).items():
            if isinstance(value, pd.DataFrame) and code in value.columns:
                out[str(key)] = value[[code]]
        return out

    suspended = {(str(day), str(item))
                 for day, item in (getattr(panels, "suspended", None) or set())
                 if str(item) == str(code)}
    return FactorPanels(
        dates=[str(day) for day in getattr(panels, "dates", [])],
        codes=[str(code)],
        prices=box(getattr(panels, "prices", {})),
        basics=box(getattr(panels, "basics", {})),
        flows=box(getattr(panels, "flows", {})),
        limits=box(getattr(panels, "limits", {})),
        bak=box(getattr(panels, "bak", {})),
        fundamentals=fundamental_for_code(panels, str(code)),
        index_returns=dict(getattr(panels, "index_returns", {}) or {}),
        suspended=suspended,
        gaps=list(getattr(panels, "gaps", []) or []),
        origins=list(getattr(panels, "origins", []) or []),
    )


def fundamental_for_code(panels: Any, code: str) -> Any:
    """把 PIT 财务面板也裁到这一只票。

    **为什么必须裁**：财务类因子通过 `panels.fundamental_field(metric)` 和
    `_year_ago` 取值，两者都对**每个交易日**做一次全市场 `as_of_records`。
    不裁的话"只回测一只票"仍会把 171 × 5238 的财务面板物化十几遍
    （实测：单次回测从秒级变成 47 秒，而其中 99% 是在算与这只票无关的股票）。

    裁完语义不变：这些取值全部按 `(code, report_period)` 查表，不含任何
    跨股票的截面统计。
    """
    root = getattr(panels, "fundamentals", None)
    if root is None or len(root) == 0:
        return root
    records = getattr(root, "records", None)
    if records is None or len(records) == 0:
        return root
    target = str(code).zfill(6)
    subset = records[records["code"].astype(str).str.zfill(6) == target]
    if len(subset) == 0:
        return root
    from src.quant.pit import PitPanel

    return PitPanel(subset, config=getattr(root, "config", None))


def candidate_names(panels: Any) -> list[str]:
    """条件里可以引用的名字：面板原始字段 + 全部因子键。"""
    from src.quant.factor_library_v2 import FACTORS

    names = set(FACTORS)
    for key, _panel in _iter_factor_panels(panels):
        names.add(key)
    return sorted(names)


def run_single_backtest(
    panels: Any, code: str, *,
    config: SingleBacktestConfig | None = None,
    factors: dict[str, pd.DataFrame] | None = None,
    name: str = "",
) -> SingleBacktestResult:
    """在单只股票上回测一条 DSL 条件策略（端到端，按需计算因子）。

    流程：解析条件 → 裁出这一只票 → **只计算条件里真正引用到的因子** → 逐日撮合。
    所以写 `close > MA(close, 20) AND ROE > 15` 只会算 `roe`（加价格字段），
    不会把 35 个因子全算一遍。

    `factors`：已经算好的因子宽表（可复用，避免重复计算）。给了就直接用，
    不再按需计算 —— 批量试多只票时先算一次更划算。
    """
    cfg = config or SingleBacktestConfig(code=code)
    cfg.code = code or cfg.code
    if not cfg.code:
        raise ValueError("必须指定股票代码")
    if not cfg.entry.strip():
        raise ValueError("必须给入场条件（entry）")

    result = SingleBacktestResult(code=cfg.code, name=name)
    notes, warnings = result.notes, result.warnings
    result.index_code = cfg.index_code

    known = candidate_names(panels)
    entry_cond = parse_condition(cfg.entry, known_factors=known, ts_mode=True)
    exit_cond: Condition | None = None
    if cfg.exit.strip():
        exit_cond = parse_condition(cfg.exit, known_factors=known, ts_mode=True)
    result.entry_condition = {"text": entry_cond.text,
                              "factors": list(entry_cond.factors)}
    if exit_cond is not None:
        result.exit_condition = {"text": exit_cond.text,
                                 "factors": list(exit_cond.factors)}
    warnings.extend(entry_cond.warnings)
    if exit_cond is not None:
        warnings.extend(exit_cond.warnings)

    single = slice_panels(panels, cfg.code)
    extra: dict[str, pd.Series] = {}
    if factors:
        for key, panel in factors.items():
            if isinstance(panel, pd.DataFrame) and cfg.code in panel.columns:
                extra[str(key)] = panel[cfg.code]
    else:
        extra = _needed_factors(single, [entry_cond, exit_cond], notes)

    frame = stock_frame(single, cfg.code, extra=extra)
    if frame.empty:
        raise ValueError(
            f"{cfg.code} 没有任何数据：请确认该票在面板覆盖范围内"
            f"（可能已退市、或数据未下载）")
    frame.attrs["suspended"] = _suspended_pairs(single)
    collisions = frame.attrs.get("column_collisions") or []
    if collisions:
        notes.append(f"字段重名保留因子版：{', '.join(collisions)}"
                     f"（既在因子库里、也是财务原始列名）")

    entry_mask = entry_cond.evaluate(frame, ts_mode=True)
    exit_mask = (exit_cond.evaluate(frame, ts_mode=True) if exit_cond is not None
                 else pd.Series(False, index=frame.index))
    hits = int(entry_mask.sum())
    notes.append(f"入场条件在 {hits}/{len(frame)} 个交易日成立"
                 f"（{hits / max(len(frame), 1) * 100:.1f}%）")
    if hits == 0:
        warnings.append(
            "入场条件从未成立，策略不会产生任何交易。常见原因："
            "窗口太长导致前段全是缺失、阈值过严、或因子在早期根本没有数据")

    prices = _price_panel(single, cfg.code, frame.index)
    trades, equity, positions = _simulate(
        cfg, frame, entry_mask, exit_mask, prices, notes, warnings)

    result.dates = list(frame.index)
    result.equity = [round(float(value), 2) for value in equity]
    result.positions = [int(value) for value in positions]
    result.benchmark = [round(float(value), 2)
                        for value in _buy_and_hold(cfg, prices, frame.index)]
    result.benchmark_index = [round(float(value), 2) for value in
                              _index_buy_and_hold(cfg, panels, frame.index,
                                                  notes, warnings)]
    result.trades = trades
    result.metrics = _metrics(cfg, equity, trades, positions)
    _attach_benchmark_metrics(result, cfg)
    result.segments = _segment_metrics(
        cfg, frame.index, equity, trades,
        benchmark=pd.Series(result.benchmark, index=result.dates),
        benchmark_index=pd.Series(result.benchmark_index, index=result.dates))
    result.config = _config_dict(cfg)
    _verify_trades(result, warnings)
    result.verdict = value_verdict(result)
    _append_honesty_notes(cfg, result)
    return result


def _index_buy_and_hold(cfg: SingleBacktestConfig, panels: Any,
                        dates: Sequence[str], notes: list[str],
                        warnings: list[str]) -> pd.Series:
    """指数基准（默认沪深300）买入持有；指数缺数据时如实说明而不是静默跳过。

    为什么需要它：拿"同一只票的买入持有"当唯一基准并不公平 ——
    在一只 20 年涨了 10 倍的存活股上，任何择时策略都很难在**绝对收益**上取胜。
    指数基准回答的是另一个问题：「这笔钱不做择时、直接买指数，会怎样」，
    这才是绝大多数投资者真实的替代方案。
    """
    pool = getattr(panels, "index_returns", {}) or {}
    series = pool.get(cfg.index_code)
    if series is None or len(series) == 0:
        warnings.append(
            f"没有 {cfg.index_code} 指数数据，无法给出指数基准 —— "
            f"仅用「同票买入持有」做比较时请记住：存活股的长期买入持有极难超越，"
            f"绝对收益跑输不代表策略没有价值（要看回撤与风险调整后收益）")
        return pd.Series([cfg.initial_cash] * len(dates), index=list(dates),
                         dtype="float64")
    aligned = series.reindex([str(day) for day in dates]).ffill().bfill()
    first = aligned.dropna()
    if first.empty:
        return pd.Series([cfg.initial_cash] * len(dates), index=list(dates),
                         dtype="float64")
    base = float(first.iloc[0])
    if not np.isfinite(base) or base <= 0:
        return pd.Series([cfg.initial_cash] * len(dates), index=list(dates),
                         dtype="float64")
    values = cfg.initial_cash * (aligned.astype("float64") / base)
    notes.append(
        f"指数基准 {cfg.index_code}：按指数点位买入持有（未计跟踪误差与 ETF 费率，"
        f"实际操作需通过对应 ETF，会有额外成本与跟踪偏差）")
    return values.fillna(cfg.initial_cash)


def _attach_benchmark_metrics(result: SingleBacktestResult,
                              cfg: SingleBacktestConfig) -> None:
    """把两条基准的对比指标并进 metrics（含风险调整维度）。"""
    metrics = result.metrics
    stock = pd.Series(result.benchmark, dtype="float64")
    index = pd.Series(result.benchmark_index, dtype="float64")
    base = cfg.initial_cash

    metrics["benchmark_final"] = round(float(stock.iloc[-1]), 2) if len(stock) else base
    metrics["index_final"] = round(float(index.iloc[-1]), 2) if len(index) else base
    metrics["benchmark_return_pct"] = round(
        (float(stock.iloc[-1]) / base - 1) * 100, 2) if len(stock) else 0.0
    metrics["index_return_pct"] = round(
        (float(index.iloc[-1]) / base - 1) * 100, 2) if len(index) else 0.0
    metrics["excess_vs_benchmark_pct"] = round(
        metrics["total_return_pct"] - metrics["benchmark_return_pct"], 2)
    metrics["excess_vs_index_pct"] = round(
        metrics["total_return_pct"] - metrics["index_return_pct"], 2)

    # 风险调整维度：趋势跟踪类策略的价值通常体现在"同样的收益、更小的回撤"上，
    # 只看最终金额会系统性低估它。
    stock_stats = _series_risk(stock, base)
    index_stats = _series_risk(index, base)
    for key, value in stock_stats.items():
        metrics[f"benchmark_{key}"] = value
    for key, value in index_stats.items():
        metrics[f"index_{key}"] = value
    annual = float(metrics.get("annual_return_pct") or 0.0) / 100
    max_dd = abs(float(metrics.get("max_drawdown_pct") or 0.0)) / 100
    metrics["calmar"] = round(annual / max_dd, 2) if max_dd > 1e-9 else None


def _series_risk(series: pd.Series, base: float) -> dict[str, Any]:
    """一条净值序列的风险指标（用于和策略做同口径对比）。"""
    series = series.astype("float64")
    if len(series) < 2 or base <= 0:
        return {"max_drawdown_pct": 0.0, "sharpe": None,
                "annual_return_pct": 0.0}
    returns = series.pct_change().dropna()
    total = float(series.iloc[-1]) / base - 1
    years = max(len(series) / TRADING_DAYS_PER_YEAR, 1e-9)
    annual = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    volatility = (float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
                  if len(returns) > 1 else 0.0)
    drawdown = series / series.cummax() - 1
    return {
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 2),
        "sharpe": (round((annual - RISK_FREE_RATE) / volatility, 2)
                   if volatility > 0 else None),
        "annual_return_pct": round(annual * 100, 2),
    }


def value_verdict(result: SingleBacktestResult) -> dict[str, Any]:
    """策略相对基准**是否有额外价值** —— 分维度判定，不合成一句口号。

    为什么不是一个布尔值：单股票回测里"跑赢"至少有四种互不相同的含义，
    混成一句话就会掩盖真正发生的事：

    - **绝对收益跑赢同票买入持有**：最难的一档。在一只长期上涨的存活股上，
      择时策略大部分时间空仓，很难在总金额上赢过"一直拿着"。
    - **绝对收益跑赢指数**：对多数人而言更实际的替代方案（不做择时就买指数）。
    - **风险调整后更优**：收益略低但回撤显著更小（Calmar/最大回撤），
      这是趋势跟踪类策略最典型、也最容易被"只看最终金额"埋没的价值。
    - **样本外仍成立**：前三条如果只在训练集成立，就不算数。

    **每一项都来自实测数字**，没有任何一项是写死的。
    """
    metrics = result.metrics or {}
    segments = result.segments or {}
    oos = segments.get("oos", {}) or {}
    train = segments.get("train", {}) or {}

    def better(a: Any, b: Any) -> bool | None:
        if a is None or b is None:
            return None
        return float(a) > float(b)

    absolute_vs_stock = better(metrics.get("excess_vs_benchmark_pct"), 0)
    absolute_vs_index = better(metrics.get("excess_vs_index_pct"), 0)
    dd_better = better(
        abs(float(metrics.get("benchmark_max_drawdown_pct") or 0)),
        abs(float(metrics.get("max_drawdown_pct") or 0)))
    calmar = metrics.get("calmar")
    risk_adjusted = None
    if dd_better is not None and calmar is not None:
        risk_adjusted = bool(dd_better) and float(calmar) > 0.3
    oos_ok = None
    if oos and train:
        oos_ok = (float(oos.get("return_pct") or 0) > 0
                  and float(oos.get("return_pct") or 0)
                  >= float(train.get("return_pct") or 0) * 0.5)

    checks = [
        {"key": "beats_benchmark", "label": "绝对收益跑赢同票买入持有",
         "passed": absolute_vs_stock,
         "detail": f"超额 {metrics.get('excess_vs_benchmark_pct')}%"},
        {"key": "beats_index", "label": "绝对收益跑赢指数",
         "passed": absolute_vs_index,
         "detail": f"超额 {metrics.get('excess_vs_index_pct')}%"
                   f"（{result.index_code or '指数'}）"},
        {"key": "risk_adjusted", "label": "风险调整后更优（回撤更小且有正 Calmar）",
         "passed": risk_adjusted,
         "detail": f"策略回撤 {metrics.get('max_drawdown_pct')}% vs 基准 "
                   f"{metrics.get('benchmark_max_drawdown_pct')}%，"
                   f"Calmar {calmar}"},
        {"key": "oos_holds", "label": "样本外仍成立",
         "passed": oos_ok,
         "detail": f"样本外 {oos.get('return_pct')}% vs 训练集 "
                   f"{train.get('return_pct')}%"},
    ]
    passed = [item for item in checks if item["passed"] is True]
    any_value = bool(passed)
    if all(item["passed"] is True for item in checks):
        summary = "四个维度全部成立：绝对收益与风险调整后都优于基准，且样本外未失效"
    elif any_value:
        summary = ("部分维度成立：" + "；".join(item["label"] for item in passed)
                   + "（其余维度不成立，见明细）")
    else:
        summary = "四个维度均不成立 —— 这份回测不支持「策略有价值」的结论"
    return {"has_value": any_value, "all_dimensions": len(passed) == len(checks),
            "passed_count": len(passed), "total_count": len(checks),
            "summary": summary, "checks": checks}


def _verify_trades(result: SingleBacktestResult, warnings: list[str]) -> None:
    """交易序列自检：能自动抓住"同一笔持仓被重复卖出"这类记账错误。

    实测背景：平仓后忘了清空持仓时，同一笔买入会被反复卖出，每次都在现金里
    加一笔钱 —— 结果是 171 天 +12410%、年化 123126% 的荒谬净值，
    而且**表面上完全看不出错**（净值曲线单调向上、回撤很小）。
    这类错误靠肉眼看曲线是发现不了的，必须用不变量守。

    不变量：
      1. 交易**不能重叠**（上一笔卖出日 ≤ 下一笔买入日）；
      2. 同一买入日不能出现多笔不同的持仓（同一日多笔 = 重复卖出）；
      3. 最终净值与"初始资金 + 累计已实现盈亏"必须对得上（允许持仓浮盈差异）。
    """
    trades = result.trades
    overlapping = []
    for previous, current in zip(trades, trades[1:], strict=False):
        if current.entry_date and previous.exit_date > current.entry_date:
            overlapping.append(f"{previous.entry_date}→{previous.exit_date} 与 "
                               f"{current.entry_date}→{current.exit_date}")
    if overlapping:
        warnings.append(
            f"⚠️ 自检失败：存在 {len(overlapping)} 组重叠交易（{overlapping[0]}）—— "
            f"同一笔持仓可能被重复卖出，净值会因此虚高。这份结果不可信")
    duplicates = {}
    for trade in trades:
        duplicates.setdefault(trade.entry_date, []).append(trade.exit_date)
    repeated = {day: exits for day, exits in duplicates.items()
                if len(set(exits)) > 1}
    if repeated:
        day = next(iter(repeated))
        warnings.append(
            f"⚠️ 自检失败：同一买入日 {day} 出现了 {len(repeated[day])} 笔"
            f"不同结束日的成交 —— 说明持仓状态没有被正确清空，结果不可信")


def _needed_factors(single: Any, conditions: Sequence[Condition | None],
                    notes: list[str]) -> dict[str, pd.Series]:
    """只计算条件里引用到的因子（裁到单票后计算，见 `slice_panels`）。"""
    from src.quant.factor_library_v2 import FACTORS, compute_factors

    used: set[str] = set()
    for condition in conditions:
        if condition is None:
            continue
        used.update(name for name in condition.factors if name in FACTORS)
    if not used:
        return {}
    keys = sorted(used)
    notes.append(f"按需计算因子：{', '.join(keys)}（只算条件里引用到的，"
                 f"不跑全部 {len(FACTORS)} 个）")
    computed = compute_factors(single, keys=keys)
    return {key: panel.iloc[:, 0] for key, panel in computed.items()
            if isinstance(panel, pd.DataFrame) and panel.shape[1] > 0}


def _price_panel(panels: Any, code: str, dates: Sequence[str]) -> pd.DataFrame:
    """该票的开高低收与涨跌停价。

    复权口径：面板的 `prices` 已是**后复权**（close × adj_factor），
    跨除权日的收益率才对；`close_raw` 保留未复权价供参考。
    """
    prices = pd.DataFrame(index=[str(day) for day in dates])
    pools = getattr(panels, "prices", {}) or {}
    for key in ("open", "high", "low", "close", "close_raw", "volume_lot"):
        panel = pools.get(key)
        if isinstance(panel, pd.DataFrame) and code in panel.columns:
            prices[key] = panel[code].reindex(prices.index).astype("float64")
    if "close" not in prices.columns:
        raise ValueError(f"{code} 缺少收盘价数据")
    for key in ("open", "high", "low"):
        if key not in prices.columns:
            prices[key] = prices["close"]
    limits = getattr(panels, "limits", {}) or {}
    for key in ("up_limit", "down_limit"):
        panel = limits.get(key)
        if isinstance(panel, pd.DataFrame) and code in panel.columns:
            prices[key] = panel[code].reindex(prices.index).astype("float64")
    _normalize_price_space(prices)
    return prices


def adjust_factor(prices: pd.DataFrame) -> pd.Series | None:
    """复权因子 = 后复权价 / 未复权价（两个都在面板里，不必另取数据）。"""
    if "close" not in prices.columns or "close_raw" not in prices.columns:
        return None
    raw = prices["close_raw"].replace(0.0, np.nan)
    factor = prices["close"] / raw
    factor = factor.replace([np.inf, -np.inf], np.nan)
    return factor if factor.notna().any() else None


def _normalize_price_space(prices: pd.DataFrame) -> None:
    """把整块价格归一到**回测首日的真实报价尺度**（就地修改）。

    为什么必须做这件事 —— 两个价格空间混用会同时错两件事：

    1. **涨跌停判断**：`up_limit` 是未复权报价，而开高低收是后复权价。
       直接比大小 → "开盘价 ≥ 涨停价"恒成立 → 所有买入被静默挡掉
       （实测：47 次买入全被跳过，回测收益为 0 但看不出原因）。
    2. **整手资金判断**：后复权价可以是最新真实价的几十上百倍
       （茅台实测一手算出来 108 万元，真实约 12.8 万元），
       于是"资金不足"永远成立。

    归一后价格 = `真实价 × adj_factor / adj_factor[首日]`：
    - **比例全部不变**（收益率、止损止盈、均线关系都与尺度无关）；
    - **首日尺度等于真实报价**，所以整手/资金判断回到真实量级；
    - 涨跌停价乘同一个系数，判断回到同一空间。
    """
    factor = adjust_factor(prices)
    if factor is None:
        return
    valid = factor.dropna()
    if valid.empty:
        return
    base = float(valid.iloc[0])
    if not np.isfinite(base) or base <= 0:
        return
    # 价格：`open_adj / base = 真实价 × factor / base` —— 首日尺度即真实报价。
    for key in ("open", "high", "low", "close"):
        if key in prices.columns:
            prices[key] = prices[key] / base
    # 涨跌停价在**未复权**空间，要乘 `factor / base` 才落到同一个空间。
    # （把这一行写成"除以 base"就会让涨停价小 factor 倍 → 每天都被判成涨停，
    #  实测 171/171 天全被挡下；这个坑我踩过一次，注释留在这里。）
    scale = (factor / base).ffill().fillna(1.0)
    for key in ("up_limit", "down_limit"):
        if key in prices.columns:
            prices[key] = prices[key] * scale
    prices.attrs["adjust_base"] = base


@dataclass
class _Position:
    """当前持仓（空仓时 shares=0）。"""

    shares: int = 0
    price: float = 0.0
    entry_index: int = -1
    entry_date: str = ""
    entry_cost: float = 0.0

    def market_value(self, price: float) -> float:
        return self.shares * price


def _simulate(
    cfg: SingleBacktestConfig, frame: pd.DataFrame, entry_mask: pd.Series,
    exit_mask: pd.Series, prices: pd.DataFrame, notes: list[str],
    warnings: list[str],
) -> tuple[list[Trade], pd.Series, pd.Series]:
    """逐日推进的撮合循环。

    每个交易日 t 的顺序：
        1. **先处理持仓**：止损/止盈（用 t 日最低/最高价判断是否触及）、
           出场条件（用 t-1 日收盘算出的信号）、最长持有 → 卖出；
        2. **再处理空仓**：用 t-1 日的入场信号在 t 日**开盘**买入。

    信号一律来自 t-1、成交一律发生在 t —— 这条纪律是整套回测可信度的地基。
    """
    dates = list(frame.index)
    close, open_ = prices["close"], prices["open"]
    high, low = prices["high"], prices["low"]
    up_limit = prices.get("up_limit")
    down_limit = prices.get("down_limit")
    suspended: set[tuple[str, str]] = frame.attrs.get("suspended", set())

    cash = float(cfg.initial_cash)
    position = _Position()
    trades: list[Trade] = []
    equity_values: list[float] = []
    position_values: list[int] = []
    skipped: dict[str, int] = {}
    blocked_sell = 0

    def note_skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for index, day in enumerate(dates):
        price_close = float(close.iloc[index])
        if not np.isfinite(price_close):
            # 数据缺失日：持仓按成本价盯市（不假装赚钱，也不假装亏钱）
            equity_values.append(cash + position.market_value(
                position.price if position.shares else 0.0))
            position_values.append(position.shares)
            continue

        # ---------- 1) 持仓处理 ----------
        if position.shares > 0:
            hold_days = index - position.entry_index
            fill_price, reason = _decide_exit(
                cfg, position, index, hold_days, open_, high, low, close,
                exit_mask)
            if reason:
                halted = (cfg.respect_suspension
                          and (day, cfg.code) in suspended)
                limit_down = False
                if cfg.respect_price_limits and down_limit is not None:
                    cap = float(down_limit.iloc[index])
                    limit_down = np.isfinite(cap) and price_close <= cap + 1e-9
                if halted or limit_down:
                    blocked_sell += 1
                else:
                    cash += _close_position(cfg, trades, position, fill_price,
                                            day, hold_days, reason)
                    # **必须清空持仓**：`_close_position` 只记账、不改 `position`，
                    # 忘了这一步的话下一日会拿同一笔持仓再"卖"一次 ——
                    # 每卖一次都往现金里加钱，净值曲线会漂亮到荒谬
                    # （实测：171 天 600519 跑出 +12410%、年化 123126%，
                    #  而且同一入场日出现 3 笔成交，这就是"看起来很强"的来源）。
                    position = _Position()

        # ---------- 2) 空仓处理（t-1 信号 → t 日开盘成交） ----------
        if position.shares == 0 and index > 0 and bool(entry_mask.iloc[index - 1]):
            open_price = float(open_.iloc[index])
            blocked = ""
            if not np.isfinite(open_price) or open_price <= 0:
                blocked = "无有效开盘价"
            elif cfg.respect_suspension and (day, cfg.code) in suspended:
                blocked = "停牌"
            elif cfg.respect_price_limits and up_limit is not None:
                cap = float(up_limit.iloc[index])
                if np.isfinite(cap) and open_price >= cap - 1e-9:
                    blocked = "开盘涨停（买不到）"
            if blocked:
                note_skip(blocked)
            else:
                fill = open_price * (1 + cfg.costs.slippage)
                quantity = int(cash * cfg.position_pct // (fill * LOT_SIZE)) * LOT_SIZE
                if quantity <= 0:
                    # 一手都买不起：10 万本金买不了一手 1500 元的票（真实现实）
                    note_skip(f"资金不足（一手 ≈ {fill * LOT_SIZE:,.0f} 元，"
                              f"可用 {cash:,.0f} 元）")
                else:
                    amount = quantity * fill
                    fee = cfg.costs.buy_cost(amount)
                    if amount + fee <= cash:
                        cash -= amount + fee
                        position = _Position(shares=quantity, price=fill,
                                             entry_index=index, entry_date=day,
                                             entry_cost=fee)
                    else:
                        note_skip("资金不足（含费用后不足）")

        equity_values.append(cash + position.market_value(price_close))
        position_values.append(position.shares)

    if position.shares > 0:
        # 末日强制平仓：否则最后一笔浮动盈亏不进统计，"胜率/盈亏比"会失真
        last = len(dates) - 1
        cash += _close_position(cfg, trades, position, float(close.iloc[last]),
                                dates[last], last - position.entry_index,
                                "回测结束平仓")
        position = _Position()
        equity_values[-1] = cash
        position_values[-1] = 0

    if skipped:
        # 按**类别**聚合，只保留一个具体例子。
        # 逐笔列出金额会把 47 次跳过写成 47 行（实测跑出来一大片，
        # 真正有用的信息"一手要 13 万、本金只有 10 万"被淹掉）。
        grouped: dict[str, int] = {}
        example: dict[str, str] = {}
        for reason, count in skipped.items():
            category = reason.split("（", 1)[0]
            grouped[category] = grouped.get(category, 0) + count
            example.setdefault(category, reason)
        detail = "；".join(
            f"{category} × {count}（例：{example[category]}）" if "（" in example[category]
            else f"{category} × {count}"
            for category, count in sorted(grouped.items()))
        notes.append(f"买入被跳过共 {sum(skipped.values())} 次：{detail}")
        if "资金不足" in grouped:
            # 只有"一手都买不起"才升级为警告；"含费用后不足"是尾差（例如
            # 10 万本金买 8700 股后剩下的钱不够再补一手），属于正常摩擦，
            # 混进同一条警告会让人以为本金不够 —— 实测就是这样误报的。
            if any("一手 ≈" in item for item in skipped):
                warnings.append(
                    f"初始资金不足以买入一手：{example['资金不足']}。"
                    f"请调大 initial_cash（A 股最小交易单位是 100 股），"
                    f"或换一只股价更低的票")
    if blocked_sell:
        notes.append(f"{blocked_sell} 次卖出因跌停/停牌被推迟")
    return (trades, pd.Series(equity_values, index=dates, dtype="float64"),
            pd.Series(position_values, index=dates, dtype="int64"))


def _decide_exit(cfg: SingleBacktestConfig, position: _Position, index: int,
                 hold_days: int, open_: pd.Series, high: pd.Series,
                 low: pd.Series, close: pd.Series,
                 exit_mask: pd.Series) -> tuple[float, str]:
    """判断本日是否该卖，返回 (**滑点前**成交价, 原因)；不卖则原因为空串。

    返回值刻意不含滑点：滑点统一在 `_close_position` 里扣一次 ——
    在两个地方各扣一次会让费率翻倍（第一版就写成了双重滑点）。

    **止损与止盈同日触发时一律按止损**：无法从日线数据确知盘中先后顺序，
    取对策略不利的一侧（保守），否则回测会系统性偏乐观。
    """
    if cfg.t_plus_1 and hold_days < 1:
        return 0.0, ""
    if hold_days < max(cfg.min_hold_days, 1):
        return 0.0, ""
    open_price = float(open_.iloc[index])
    if cfg.stop_loss_pct > 0:
        stop_price = position.price * (1 - cfg.stop_loss_pct)
        if np.isfinite(low.iloc[index]) and float(low.iloc[index]) <= stop_price:
            # 跳空低于止损价 → 只能按开盘价成交（更差的价格）
            return min(open_price, stop_price), "止损"
    if cfg.take_profit_pct > 0:
        target = position.price * (1 + cfg.take_profit_pct)
        if np.isfinite(high.iloc[index]) and float(high.iloc[index]) >= target:
            return max(open_price, target), "止盈"
    if index > 0 and bool(exit_mask.iloc[index - 1]):
        # 出场信号来自 t-1 收盘 → 在 t 日开盘成交
        return open_price, "出场条件"
    if cfg.max_hold_days > 0 and hold_days >= cfg.max_hold_days:
        # 到期没有"开盘触发信号"，按当日收盘卖出（不假装能在开盘价成交）
        return float(close.iloc[index]), "到期"
    return 0.0, ""


def _close_position(cfg: SingleBacktestConfig, trades: list[Trade],
                    position: _Position, price: float, day: str, hold_days: int,
                    reason: str) -> float:
    """平仓并记账，返回卖出净收入。`price` 是**滑点前**的成交价。"""
    fill = price * (1 - cfg.costs.slippage)
    amount = position.shares * fill
    fee = cfg.costs.sell_cost(amount)
    proceeds = amount - fee
    cost_basis = position.shares * position.price + position.entry_cost
    pnl = proceeds - cost_basis
    trades.append(Trade(
        code=cfg.code, entry_date=position.entry_date,
        entry_price=position.price, shares=position.shares,
        entry_cost=position.entry_cost, exit_date=day, exit_price=fill,
        exit_cost=fee, hold_days=max(hold_days, 0), pnl=pnl,
        return_pct=(pnl / cost_basis) if cost_basis else 0.0,
        exit_reason=reason))
    return proceeds


def _buy_and_hold(cfg: SingleBacktestConfig, prices: pd.DataFrame,
                  dates: Sequence[str]) -> pd.Series:
    """基准：同一只票一次性买入持有（含费用与滑点，口径与策略一致）。"""
    close = prices["close"].ffill()
    if close.dropna().empty:
        return pd.Series([cfg.initial_cash] * len(dates), index=list(dates),
                         dtype="float64")
    start_price = float(close.loc[close.first_valid_index()])
    if not np.isfinite(start_price) or start_price <= 0:
        return pd.Series([cfg.initial_cash] * len(dates), index=list(dates),
                         dtype="float64")
    fill = start_price * (1 + cfg.costs.slippage)
    shares = int(cfg.initial_cash // (fill * LOT_SIZE)) * LOT_SIZE
    amount = shares * fill
    cash = cfg.initial_cash - amount - cfg.costs.buy_cost(amount)
    values = []
    for day in dates:
        price = close.get(day, np.nan)
        if not np.isfinite(price):
            price = start_price
        values.append(cash + shares * float(price))
    return pd.Series(values, index=list(dates), dtype="float64")


# ==================================================================
# 指标
# ==================================================================


def _metrics(cfg: SingleBacktestConfig, equity: pd.Series,
             trades: list[Trade], positions: pd.Series) -> dict[str, Any]:
    equity = equity.astype("float64")
    returns = equity.pct_change().dropna()
    final = float(equity.iloc[-1]) if len(equity) else cfg.initial_cash
    total_return = final / cfg.initial_cash - 1 if cfg.initial_cash else 0.0
    years = max(len(equity) / TRADING_DAYS_PER_YEAR, 1e-9)
    annual = ((1 + total_return) ** (1 / years) - 1
              if total_return > -1 else -1.0)
    volatility = (float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
                  if len(returns) > 1 else 0.0)
    drawdown = equity / equity.cummax() - 1
    downside = returns[returns < 0]
    downside_vol = (float(downside.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
                    if len(downside) > 1 else 0.0)

    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl <= 0]
    gross_win = sum(trade.pnl for trade in wins)
    gross_loss = abs(sum(trade.pnl for trade in losses))
    return {
        "initial_cash": cfg.initial_cash,
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return * 100, 2),
        "annual_return_pct": round(annual * 100, 2),
        "volatility_pct": round(volatility * 100, 2),
        "sharpe": (round((annual - RISK_FREE_RATE) / volatility, 2)
                   if volatility > 0 else None),
        "sortino": (round((annual - RISK_FREE_RATE) / downside_vol, 2)
                    if downside_vol > 0 else None),
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 2)
        if len(drawdown) else 0.0,
        "trade_count": len(trades),
        "win_rate_pct": (round(len(wins) / len(trades) * 100, 1)
                         if trades else None),
        "avg_win_pct": (round(float(np.mean([t.return_pct for t in wins])) * 100, 2)
                        if wins else None),
        "avg_loss_pct": (round(float(np.mean([t.return_pct for t in losses])) * 100, 2)
                         if losses else None),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_hold_days": (round(float(np.mean([t.hold_days for t in trades])), 1)
                          if trades else None),
        "exposure_pct": round(float((positions > 0).mean()) * 100, 1)
        if len(positions) else 0.0,
        "total_cost": round(sum(t.entry_cost + t.exit_cost for t in trades), 2),
        "trading_days": len(equity),
        "exit_reasons": _reason_histogram(trades),
    }


def _reason_histogram(trades: list[Trade]) -> dict[str, int]:
    histogram: dict[str, int] = {}
    for trade in trades:
        histogram[trade.exit_reason] = histogram.get(trade.exit_reason, 0) + 1
    return histogram


def _segment_metrics(cfg: SingleBacktestConfig, dates: Sequence[str],
                     equity: pd.Series, trades: list[Trade],
                     benchmark: pd.Series | None = None,
                     benchmark_index: pd.Series | None = None) -> dict[str, Any]:
    """全样本 / 训练集 / 样本外三段指标。

    单股票回测最容易骗自己的地方：一只票几千个交易日，试几组参数总能挑出
    一段漂亮的曲线。所以必须把"挑参数用的那一段"和"检验用的那一段"分开报。
    """
    days = list(dates)
    split = max(1, int(len(days) * cfg.train_ratio))
    segments: dict[str, Any] = {}
    windows: list[tuple[str, list[str]]] = [
        ("all", days), ("train", days[:split]), ("oos", days[split:]),
    ]
    # "近一年"单独成段。为什么要它：用户关心的是"最近一年这个策略能不能打"，
    # 而 all/train/oos 都无法直接回答 —— 尤其当回测区间很长时，
    # 近一年的表现会被十多年前的行情稀释掉。
    #
    # 注意：这一段的收益来自**在整段面板上跑出来的净值**在窗口内的变化，
    # 所以窗口开始前建立的持仓，其窗口内盈亏也算进来（口径与"从窗口第一天
    # 才建仓"不同）。要在窗口起点空仓，就必须从窗口起点重新回测 ——
    # 那才是"近一年独立回测"，代价是丢掉预热样本。见 `recent_standalone`。
    if cfg.recent_days > 0 and len(days) > cfg.recent_days:
        windows.append(("recent", days[-cfg.recent_days:]))
    for label, window in windows:
        if len(window) < 2:
            continue
        window_set = set(window)
        segment = equity.loc[[day for day in window if day in equity.index]]
        if len(segment) < 2:
            continue
        window_trades = [t for t in trades if t.exit_date in window_set]
        base = float(segment.iloc[0])
        total = float(segment.iloc[-1]) / base - 1 if base else 0.0
        years = max(len(segment) / TRADING_DAYS_PER_YEAR, 1e-9)
        segment_returns = segment.pct_change().dropna()
        volatility = (float(segment_returns.std(ddof=1)
                            * math.sqrt(TRADING_DAYS_PER_YEAR))
                      if len(segment_returns) > 1 else 0.0)
        annual = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
        drawdown = segment / segment.cummax() - 1
        wins = [t for t in window_trades if t.pnl > 0]
        # 同期基准收益：没有它就没法算"这一段跑赢了多少" ——
        # 而"近一年跑赢买入持有 10%"这种结论必须建立在**同窗口**的基准上，
        # 拿全样本基准去比会得出完全不同的数字。
        benchmark_return = _window_return(benchmark, window)
        index_return = _window_return(benchmark_index, window)
        segments[label] = {
            "start": window[0], "end": window[-1],
            "trading_days": len(segment),
            "return_pct": round(total * 100, 2),
            "annual_return_pct": round(annual * 100, 2),
            "max_drawdown_pct": round(float(drawdown.min()) * 100, 2),
            "sharpe": (round((annual - RISK_FREE_RATE) / volatility, 2)
                       if volatility > 0 else None),
            "trades": len(window_trades),
            "win_rate_pct": (round(len(wins) / len(window_trades) * 100, 1)
                             if window_trades else None),
            "benchmark_return_pct": benchmark_return,
            "index_return_pct": index_return,
            "excess_vs_benchmark_pct": (round(total * 100 - benchmark_return, 2)
                                        if benchmark_return is not None else None),
            "excess_vs_index_pct": (round(total * 100 - index_return, 2)
                                    if index_return is not None else None),
        }
    return segments


def _window_return(series: pd.Series | None,
                   window: Sequence[str]) -> float | None:
    """一条净值序列在指定窗口内的收益率（%）。窗口内不足两天返回 None。"""
    if series is None or len(series) == 0:
        return None
    subset = series.reindex([day for day in window if day in series.index])
    subset = subset.dropna()
    if len(subset) < 2 or float(subset.iloc[0]) == 0:
        return None
    return round((float(subset.iloc[-1]) / float(subset.iloc[0]) - 1) * 100, 2)


def _append_honesty_notes(cfg: SingleBacktestConfig,
                          result: SingleBacktestResult) -> None:
    """把"这份回测结论有多可信"直接写进结果，而不是让用户自己猜。"""
    metrics = result.metrics
    trades = int(metrics.get("trade_count", 0))
    oos = result.segments.get("oos", {})
    oos_trades = int(oos.get("trades", 0))
    tunables = (2 + int(bool(cfg.exit)) + int(cfg.stop_loss_pct > 0)
                + int(cfg.take_profit_pct > 0)
                + int(cfg.max_hold_days != 20))

    if trades == 0:
        result.warnings.append("策略在整个区间没有产生任何交易 —— 没有交易就没有结论")
        return
    if trades < 10:
        result.warnings.append(
            f"全样本只有 {trades} 笔交易：胜率/盈亏比的统计噪声极大，"
            f"换个入场时点就可能翻盘。至少要 30 笔以上才值得当参考")
    if oos_trades and oos_trades < 10:
        result.warnings.append(
            f"样本外只有 {oos_trades} 笔交易 —— 样本外指标基本没有统计意义，"
            f"请回补更长历史或放宽入场条件")
    win_rate = metrics.get("win_rate_pct")
    profit_factor = metrics.get("profit_factor")
    if win_rate is not None and float(win_rate) >= 60 and (
            profit_factor is None or float(profit_factor) < 1.2):
        result.warnings.append(
            f"胜率 {float(win_rate):.0f}% 但盈亏比偏低（{profit_factor}）："
            f"这是「小赚多次、大亏一次」的典型形态，长期未必为正")
    exposure = float(metrics.get("exposure_pct") or 0)
    if exposure < 10:
        result.warnings.append(
            f"持仓时间只占 {exposure:.0f}%：净值样本很少，"
            f"年化收益由极少数几段贡献，稳定性差")
    if oos_trades:
        train_return = float(result.segments.get("train", {}).get("return_pct", 0))
        oos_return = float(oos.get("return_pct", 0))
        if train_return > 0 and oos_return < train_return * 0.5:
            result.warnings.append(
                f"样本外收益（{oos_return:.1f}%）明显低于训练集（{train_return:.1f}%）："
                f"典型的过拟合信号，不要按训练集的表现预期未来")
    result.warnings.append(
        f"参数越多越容易过拟合：本策略有 {tunables} 个可调项。"
        f"试过 N 组参数后挑「最好的一组」，它在样本外的表现通常会明显低于回测值")
    result.notes.append(
        f"撮合口径：t 日收盘信号 → t+1 日开盘成交"
        f"{'，T+1 不可当日卖出' if cfg.t_plus_1 else ''}"
        f"{'，涨停不买/跌停不卖' if cfg.respect_price_limits else ''}"
        f"{'，停牌不交易' if cfg.respect_suspension else ''}"
        f"，整手（100 股），佣金万三（最低 5 元）+ 卖出印花税 0.05%"
        f" + 过户费 0.001% + 滑点 {cfg.costs.slippage_bps:.0f}bp")
    result.notes.append("价格为后复权（close × adj_factor），跨除权日的收益率正确")


def _config_dict(cfg: SingleBacktestConfig) -> dict[str, Any]:
    return {
        "code": cfg.code, "entry": cfg.entry, "exit": cfg.exit,
        "initial_cash": cfg.initial_cash, "position_pct": cfg.position_pct,
        "stop_loss_pct": cfg.stop_loss_pct,
        "take_profit_pct": cfg.take_profit_pct,
        "max_hold_days": cfg.max_hold_days,
        "min_hold_days": cfg.min_hold_days,
        "t_plus_1": cfg.t_plus_1,
        "respect_price_limits": cfg.respect_price_limits,
        "respect_suspension": cfg.respect_suspension,
        "train_ratio": cfg.train_ratio,
        "costs": {
            "commission_rate": cfg.costs.commission_rate,
            "min_commission": cfg.costs.min_commission,
            "stamp_tax_rate": cfg.costs.stamp_tax_rate,
            "transfer_fee_rate": cfg.costs.transfer_fee_rate,
            "slippage_bps": cfg.costs.slippage_bps,
        },
    }


__all__ = [
    "ConditionError",
    "CostConfig",
    "LOT_SIZE",
    "SingleBacktestConfig",
    "SingleBacktestResult",
    "Trade",
    "candidate_names",
    "run_single_backtest",
    "slice_panels",
    "stock_frame",
]
