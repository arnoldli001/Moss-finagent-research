"""超短战法（六式）回测：日线级复现 + 组合模拟 + 胜率统计。

## 回测的是"哪一套战法"

回测调用的是 `src.auction_select.modes.classify_mode` —— **实盘「量化选股/竞价选股」
用的同一个分类器**，不是另写一套"回测专用规则"。择时闸门（`MODE_PHASES`/`MODE_AVOID`）、
生态档位（`ecology.snapshot_from_series`）、小周期金叉与压力位，全部复用实盘代码。

这一点是刻意的：如果为回测另写一套判定，回测出来的就是**另一个策略**，
结论不能用来指导实盘。所以本模块只替换**数据侧**：

| 实盘取的数 | 回测取的数 | 是否等价 |
|---|---|---|
| `limit_ladder()` 昨日涨停池 | 仓库 `quant_stk_limit.up_limit` 判封板 + 规则1 修正连板数 | 等价 |
| 竞价 `open_change_pct` | T+1 开盘价 / T 收盘价 − 1 | 等价 |
| 东财四池 | 日线自建（收=涨停价 / 最高触板未封 / 收=跌停价） | 等价（强势股池缺席） |
| 竞价盘口承接分 `takeover_score` | **无历史数据** | 不等价 → 降级为记录项 |
| 题材热度榜 / 主线龙头 | **无历史数据** | 不等价 → 该模式本次回测不可用 |

## 数据缺口的处理原则（不猜、不污染）

历史上拿不到的两类数据会让部分模式在严格模式下**必然判否** ——
那是"数据不足"，不是"没机会"，直接报 0 笔会被误读成"胜率 0"。

处理方式分两种，刻意不同：

- **竞价盘口承接分**（`无极式` 5 条判据里的 1 条）：降级为**记录项**
  （`blocking=False`，只扣分不否决），其余 4 条（择时/断板前提/竞价翻红/
  近5日未当过最高板）仍然硬约束 —— 所以降级后仍然是"无极式"，不是"随便买"。
- **题材榜/主线龙头**（`趋龙式` 的全部身份判据）：**不降级**。降级后
  "趋龙式"会退化成"主升期买任意涨停股"，那个胜率不能挂"趋龙式"的名字。
  该模式本次回测标记为"数据不可用"，不出信号、也不报胜率。

## 交易规则（用户 2026-09-18 口径）

- **信号日 T**：用 T 日收盘后的数据判模式；**买入日 = T+1 开盘**（与实盘竞价一致）。
- **仓位**：每笔 = 当时总权益 × `per_position`（默认 20%），最多 `max_positions`
  （默认 5）笔并发；同日信号超额时按 `mode_score` 降序取前几，其余记入 `skipped`。
- **卖出**：日内跌幅 >7% → 止损；收盘价跌破 5 日均线 → 清仓。
- **A 股 T+1 制度**：买入当日**不可卖**（哪怕当天就跌破止损）；买入当天触发止损的
  记 `breached_entry_day` 并在**次日开盘**卖出。
- **一字涨停买不进**：T+1 开盘价 = 涨停价 → 记为 `buy_blocked`，不建仓。
- **跌停卖不出**：开盘/收盘价 = 跌停价 → 当日不可成交，挂到下一交易日开盘。

## 与实盘已知的差异（如实记录，不掩盖）

1. `advance_ratio`（上涨比率，冰点指标③）：实盘 `market_cycle` 不带该字段，
   回测**带了**（仓库有全市场涨跌幅）→ 回测比实盘多一项冰点共振指标，
   用 `use_advance_ratio` 开关。关掉即与实盘一致。
2. 强势股池（`strong_count`）无历史数据 → 恒为 0（只影响展示字段，不影响阶段判定）。
3. 候选池按"是否封板"重建，拿不到封单额/封成比，所以 `追击式` 的"日K不爆量"
   只能记 `None`（实盘在缺该数据时同样放行）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.auction_select import modes

logger = logging.getLogger(__name__)

#: 候选池
POOL_LIMIT_UP = "limit_up"
POOL_BROKEN = "broken_board"

POOL_NAMES: dict[str, str] = {
    POOL_LIMIT_UP: "涨停池",
    POOL_BROKEN: "断板池（前一日连板≥2、当日未封板）",
}

#: 历史不可复现的**数据域** —— 单一来源是 `auction_select.modes`
#: （在那儿定义，是为了让"声明缺失"和"决定是否降级"待在同一处，不出现两套口径）。
UNAVAILABLE_TAKEOVER = modes.UNAVAILABLE_TAKEOVER
UNAVAILABLE_THEME_RANK = modes.UNAVAILABLE_THEME_RANK

UNAVAILABLE_LABELS: dict[str, str] = dict(modes.UNAVAILABLE_LABELS)

#: 回测**允许降级**的数据域。刻意只有这一项：
#: 无极式 5 条判据里它只占 1 条，降级后剩余 4 条仍是硬约束。
#: 趋龙式不在此列 —— 见 `modes._fit_qu_long` 的说明（降级后名不副实）。
RELAXABLE_UNAVAILABLE: tuple[str, ...] = (UNAVAILABLE_TAKEOVER,)

_FLOAT_TOLERANCE = 1e-6
_TRADING_DAYS = 244


# ======================================================================
# 配置
# ======================================================================


@dataclass
class ShortTermConfig:
    """回测参数。默认值 = 用户 2026-09-18 给的口径。"""

    #: 信号日区间（含端点，YYYYMMDD）
    start: str = ""
    end: str = ""
    #: 数据加载起点（要覆盖生态 20 日窗口 + 连板上下文）；空 = 自动回推 75 自然日
    load_start: str = ""

    per_position: float = 0.20          # 单笔仓位（占总权益）
    max_positions: int = 5              # 最大并发笔数
    stop_loss_pct: float = 0.07         # 相对**买入价**日内跌幅超 7% → 止损
    #: 止损基准：`entry` = 买入价（用户口径"买入后"）；`prev_close` = 信号日收盘
    stop_reference: str = "entry"
    ma_window: int = 5                  # 收盘跌破 N 日均线 → 清仓

    commission_rate: float = 0.00025    # 单边佣金（万 2.5）
    stamp_tax_rate: float = 0.0005      # 印花税（卖出单边，千 0.5）
    slippage_rate: float = 0.0          # 单边滑点（默认 0，用 CLI 压测）
    initial_capital: float = 1.0

    #: 同日信号超额时的排序键（mode_score / streak / amount）
    rank_by: str = "mode_score"
    #: 候选池是否套用实盘竞价选股的股票池口径（非 ST + 流通市值区间）
    universe_filter: bool = True
    #: 股票池区间**钉住值**。None = 跟随 `auction_select/config.yaml` 的实盘配置。
    #:
    #: ⚠️ 为什么要有钉住：实盘配置是会被改的（本仓库同一天里就从 150 亿改到 110 亿），
    #: 不钉住的话同一条回测命令隔一小时跑出来的候选池就不一样，
    #: 报告上写的"候选池 30~150 亿"也会和实际用的区间对不上。
    #: 钉住后 `diagnostics.universe` 会如实标出 `pinned=True` 与生效区间。
    min_market_cap: float | None = None
    max_market_cap: float | None = None
    exclude_st: bool | None = None
    #: 是否把 `advance_ratio` 喂进冰点共振（实盘不带该字段）
    use_advance_ratio: bool = True
    #: 是否把"历史不可复现"的判据降级为记录项（False = 与实盘完全一致的严格版）
    relax_unavailable: bool = True
    #: 一字涨停（开盘即涨停）视为买不进
    block_one_word_entry: bool = True
    #: 只对"命中模式"的票出信号（False = 全涨停池，用于无条件对照）
    require_mode: bool = True

    def variant(self, **changes: Any) -> ShortTermConfig:
        """派生一个只改若干字段的副本（敏感性对比用，不改原配置）。"""
        import dataclasses

        return dataclasses.replace(self, **changes)


# ======================================================================
# 行情面板
# ======================================================================


@dataclass
class Bar:
    """一根日线（含当日涨跌停价与规则1 修正后的连板数）。"""

    code: str = ""
    date: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    pre_close: float = 0.0
    amount: float = 0.0                 # 成交额（元）
    pct_chg: float | None = None
    up_limit: float | None = None
    down_limit: float | None = None
    turnover_rate: float | None = None
    circ_mv: float | None = None
    #: 规则1 修正后的连板数（当日封板才 > 0；被剔除的缩量一字板为 0）
    streak: int = 0

    def sealed(self) -> bool:
        """当日是否封住涨停。"""
        return (self.up_limit is not None
                and abs(self.close - self.up_limit) < _FLOAT_TOLERANCE)

    def touched_limit(self) -> bool:
        """当日是否触及过涨停价（用于炸板池）。"""
        return (self.up_limit is not None
                and abs(self.high - self.up_limit) < _FLOAT_TOLERANCE)

    def at_down_limit(self, price: float) -> bool:
        """给定价格是否落在跌停价（"卖不出"判定）。"""
        return (self.down_limit is not None
                and abs(price - self.down_limit) < _FLOAT_TOLERANCE)


@dataclass
class MarketPanel:
    """区间面板 + 每日情绪周期 + 每日生态快照（全部只含当日及之前的数据）。"""

    days: list[str] = field(default_factory=list)
    #: code -> date -> Bar
    bars: dict[str, dict[str, Bar]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    #: date -> market_cycle 字典（已过 `attach_ecology`，含 ecology/压力位）
    cycles: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: date -> {"limit_up": [code...], "broken": [...], "limit_down": [...]}
    pools: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    #: date -> 当日计数 {limit_up, broken, limit_down, advance, total}
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    #: date -> 当日最高连板数 / 最高板个股
    top_streak: dict[str, int] = field(default_factory=dict)
    top_code: dict[str, str] = field(default_factory=dict)
    #: date -> EcologySnapshot（截至该日）
    snapshots: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    index: dict[str, int] = field(default_factory=dict, repr=False)

    def reindex(self) -> None:
        self.index = {day: position for position, day in enumerate(self.days)}

    def bar(self, code: str, date: str) -> Bar | None:
        return (self.bars.get(code) or {}).get(date)

    def day_before(self, date: str) -> str | None:
        position = self.index.get(date)
        return self.days[position - 1] if position else None

    def next_day(self, date: str) -> str | None:
        position = self.index.get(date)
        if position is None or position + 1 >= len(self.days):
            return None
        return self.days[position + 1]

    def ma(self, code: str, date: str, window: int = 5) -> float | None:
        """`date`（含）往前 `window` 根日线的收盘均价；不足 `window` 根返回 None。"""
        position = self.index.get(date)
        if position is None:
            return None
        closes: list[float] = []
        for day in self.days[max(0, position - window + 1):position + 1]:
            bar = self.bar(code, day)
            if bar is None:
                break
            closes.append(bar.close)
        if len(closes) < window:
            return None
        return sum(closes) / len(closes)

    def trailing(self, date: str, *, window: int) -> list[str]:
        """`date`（含）往前 `window` 个交易日。"""
        position = self.index.get(date)
        if position is None:
            return []
        return self.days[max(0, position - window + 1):position + 1]


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _minus_days(stamp: str, days: int) -> str:
    from datetime import date, timedelta

    try:
        base = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
    except (ValueError, IndexError):
        return stamp
    return (base - timedelta(days=int(days))).strftime("%Y%m%d")


# ======================================================================
# 面板构建（本模块唯一的 IO 处）
# ======================================================================


def build_panel(*, start: str, end: str = "", load_start: str = "",
                use_advance_ratio: bool = True) -> MarketPanel:
    """从本地仓库构建回测面板。

    ⚠️ **每一天的 `cycles[day]` 与生态都只用截至该日的数据** ——
    这是回测有效性的底线：把未来数据喂进择时闸门，胜率会好看得毫无意义。
    """
    from src.auction_select.ecology import _streak_records
    from src.quant.warehouse import load_dataset

    panel = MarketPanel()
    limit_cols = ["code", "trade_date", "up_limit", "down_limit"]

    probe, src_probe = load_dataset("stk_limit", start=start,
                                    columns=limit_cols)
    if probe is None or not len(probe):
        panel.gaps.append("仓库里没有涨跌停价数据 → 无法判定封板，回测不可用")
        return panel
    latest = str(probe["trade_date"].astype(str).max())
    end = end or latest
    if latest < end:
        panel.gaps.append(
            f"仓库最新交易日 {latest} 早于请求结束日 {end} → 实际结束日取 {latest}")
        end = latest

    if not load_start:
        load_start = _minus_days(start, 75)

    daily, src_daily = load_dataset(
        "daily", start=load_start, end=end,
        columns=["code", "trade_date", "open", "high", "low", "close",
                 "pre_close", "amount", "pct_chg"])
    limits, src_limits = load_dataset("stk_limit", start=load_start, end=end,
                                     columns=limit_cols)
    basics, src_basics = load_dataset(
        "daily_basic", start=load_start, end=end,
        columns=["code", "trade_date", "turnover_rate", "circ_mv"])
    directory, src_dir = load_dataset("stock_basic", columns=["code", "name"])
    panel.sources = {"daily": src_daily, "stk_limit": src_limits,
                     "daily_basic": src_basics, "stock_directory": src_dir,
                     "probe": src_probe}
    if daily is None or not len(daily) or limits is None or not len(limits):
        panel.gaps.append("日线或涨跌停价为空 → 回测不可用")
        return panel

    if directory is not None and len(directory):
        panel.names = {str(row.code).zfill(6): str(row.name)
                       for row in directory.itertuples(index=False)}

    records, day_list = _streak_records(days=100000, start=load_start)
    if not records:
        panel.gaps.append("连板数序列为空 → 回测不可用")
        return panel
    panel.days = [day for day in day_list if load_start <= day <= end]
    panel.reindex()

    limit_map: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    for row in limits.itertuples(index=False):
        limit_map[(str(row.code).zfill(6), str(row.trade_date))] = (
            _to_float(row.up_limit), _to_float(row.down_limit))
    basic_map: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    if basics is not None and len(basics):
        for row in basics.itertuples(index=False):
            basic_map[(str(row.code).zfill(6), str(row.trade_date))] = (
                _to_float(getattr(row, "turnover_rate", None)),
                _to_float(getattr(row, "circ_mv", None)))

    for row in daily.itertuples(index=False):
        code = str(row.code).zfill(6)
        date = str(row.trade_date)
        if date not in panel.index:
            continue
        up_limit, down_limit = limit_map.get((code, date), (None, None))
        turnover, circ_mv = basic_map.get((code, date), (None, None))
        item = records.get((code, date))
        panel.bars.setdefault(code, {})[date] = Bar(
            code=code, date=date,
            open=_to_float(row.open) or 0.0, high=_to_float(row.high) or 0.0,
            low=_to_float(row.low) or 0.0, close=_to_float(row.close) or 0.0,
            pre_close=_to_float(row.pre_close) or 0.0,
            amount=_to_float(row.amount) or 0.0,
            pct_chg=_to_float(row.pct_chg),
            up_limit=up_limit, down_limit=down_limit,
            turnover_rate=turnover, circ_mv=circ_mv,
            streak=(item.streak if item is not None else 0))

    _build_pools(panel)
    _attach_cycles(panel, use_advance_ratio=use_advance_ratio)
    _attach_ecologies(panel)
    return panel


def _build_pools(panel: MarketPanel) -> None:
    """一次遍历同时算出每日池子、计数、当日最高连板（避免 5200×100 扫两遍）。"""
    for date in panel.days:
        pools: dict[str, list[str]] = {POOL_LIMIT_UP: [], POOL_BROKEN: [],
                                       "limit_down": []}
        counts = {"limit_up": 0, "broken": 0, "limit_down": 0, "advance": 0,
                  "total": 0}
        top_streak = 0
        top_code = ""
        for code, series in panel.bars.items():
            bar = series.get(date)
            if bar is None:
                continue
            counts["total"] += 1
            if bar.pct_chg is not None and bar.pct_chg > 0:
                counts["advance"] += 1
            if bar.sealed():
                counts["limit_up"] += 1
                pools[POOL_LIMIT_UP].append(code)
                if bar.streak > top_streak:
                    top_streak, top_code = bar.streak, code
            elif bar.touched_limit():
                counts["broken"] += 1
            if bar.down_limit is not None and bar.at_down_limit(bar.close):
                counts["limit_down"] += 1
                pools["limit_down"].append(code)
        # 断板池：前一日**连板**（≥2）且当日未封板 —— 反核两式（无极/出水）的标的来源。
        # 首板断板不算"连板断板票"，原文口径是无极式做"连板断板后的次日反包"。
        previous = panel.day_before(date)
        if previous:
            for code in panel.pools.get(previous, {}).get(POOL_LIMIT_UP, []):
                before = panel.bar(code, previous)
                today = panel.bar(code, date)
                if before is None or today is None:
                    continue
                if before.streak >= 2 and not today.sealed():
                    pools[POOL_BROKEN].append(code)
        panel.pools[date] = pools
        panel.counts[date] = counts
        panel.top_streak[date] = top_streak
        panel.top_code[date] = top_code


def _attach_cycles(panel: MarketPanel, *, use_advance_ratio: bool) -> None:
    """逐日构建 `market_cycle`（只喂**当日及之前**的池子）。"""
    import pandas as pd

    from src.intraday.market_cycle import build_market_cycle

    for date in panel.days:
        pools = panel.pools.get(date) or {}
        limit_rows: list[dict[str, Any]] = []
        for code in pools.get(POOL_LIMIT_UP, []):
            bar = panel.bar(code, date)
            if bar is None:
                continue
            # 规则1 剔除掉的"开头缩量一字板"记 1 板 —— 它仍然是首板这个事实
            limit_rows.append({"code": code, "连板数": max(bar.streak, 1)})
        broken_rows: list[dict[str, Any]] = []
        for code, series in panel.bars.items():
            bar = series.get(date)
            if bar is None:
                continue
            if bar.touched_limit() and not bar.sealed():
                broken_rows.append({"code": code, "涨跌幅": bar.pct_chg})
        cycle = build_market_cycle(
            trade_date=date,
            limit_up=pd.DataFrame(limit_rows),
            broken=pd.DataFrame(broken_rows),
            limit_down=pd.DataFrame([{"code": c}
                                     for c in pools.get("limit_down", [])]),
            strong=None, source="warehouse_daily(回测重建)")
        payload = cycle.to_dict()
        if use_advance_ratio and (panel.counts.get(date) or {}).get("total"):
            counts = panel.counts[date]
            payload["advance_ratio"] = round(
                counts.get("advance", 0) / counts["total"], 4)
        panel.cycles[date] = payload


def _attach_ecologies(panel: MarketPanel) -> None:
    """逐日构建生态快照（序列**截至该日**）并合并进 `market_cycle`。

    窗口口径与 `ecology.build_snapshot()` 一致：喂 `days + 6` 个交易日进去，
    波峰只看最后 `SERIES_DAYS`（10）天 —— 多取的用于左右邻居判定。
    """
    from src.auction_select.ecology import (
        SERIES_DAYS,
        StockStreak,
        snapshot_from_series,
    )
    from src.auction_select.service import attach_ecology

    series_all = [(day, panel.top_streak.get(day, 0)) for day in panel.days]
    counts_all = {day: (panel.counts.get(day) or {}).get("limit_up", 0)
                  for day in panel.days}
    fetch_days = max(SERIES_DAYS + 6, 20)

    for position, date in enumerate(panel.days):
        window = series_all[max(0, position - fetch_days + 1):position + 1]
        top_code = panel.top_code.get(date, "")
        top_item: StockStreak | None = None
        if top_code:
            bar = panel.bar(top_code, date)
            if bar is not None:
                top_item = StockStreak(code=top_code, date=date,
                                       raw_streak=bar.streak,
                                       streak=bar.streak,
                                       limit_up_price=bar.up_limit,
                                       close=bar.close,
                                       turnover_rate=bar.turnover_rate)
        snapshot = snapshot_from_series(window, counts=counts_all,
                                        top_stock=top_item, days=SERIES_DAYS)
        panel.snapshots[date] = snapshot
        panel.cycles[date] = attach_ecology(panel.cycles.get(date) or {},
                                            ecology_snapshot=snapshot)


# ======================================================================
# 信号
# ======================================================================


@dataclass
class Signal:
    """一个"T 日判定、T+1 开盘买入"的信号。"""

    signal_date: str = ""
    buy_date: str = ""
    code: str = ""
    name: str = ""
    pool: str = ""
    mode: str = ""
    mode_name: str = ""
    mode_branch: str = ""
    mode_score: float = 0.0
    mode_reason: str = ""
    streak: int = 0
    is_top_board: bool = False
    market_stage: str = ""
    ecology: str = ""
    entry_price: float = 0.0
    amount: float = 0.0
    buy_blocked: str = ""
    relaxations: list[str] = field(default_factory=list)
    feature: dict[str, Any] = field(default_factory=dict)
    fits: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, with_detail: bool = False) -> dict[str, Any]:
        payload = {
            "signal_date": self.signal_date, "buy_date": self.buy_date,
            "code": self.code, "name": self.name,
            "pool": self.pool, "pool_name": POOL_NAMES.get(self.pool, self.pool),
            "mode": self.mode, "mode_name": self.mode_name,
            "mode_branch": self.mode_branch, "mode_score": round(self.mode_score, 1),
            "mode_reason": self.mode_reason,
            "streak": self.streak, "is_top_board": self.is_top_board,
            "market_stage": self.market_stage, "ecology": self.ecology,
            "entry_price": round(self.entry_price, 4),
            "buy_blocked": self.buy_blocked,
            "relaxations": list(self.relaxations),
        }
        if with_detail:
            payload["feature"] = self.feature
            payload["fits"] = self.fits
        return payload

    def sort_key(self, rank_by: str = "mode_score") -> tuple[Any, ...]:
        if rank_by == "streak":
            return (-self.streak, -self.mode_score, self.code)
        if rank_by == "amount":
            return (-self.amount, -self.mode_score, self.code)
        return (-self.mode_score, -self.streak, self.code)


def build_feature(panel: MarketPanel, code: str, signal_date: str,
                  buy_date: str, *, pool: str,
                  cycle: dict[str, Any]) -> dict[str, Any]:
    """由**日线**构造 `modes.classify_mode` 需要的特征字典。

    字段名与 `auction_select.service.build_feature` 完全对齐（同一套判据），
    拿不到历史的那几项**显式置 None 并记 degraded**，不用近似值冒充。
    """
    today = panel.bar(code, signal_date)
    tomorrow = panel.bar(code, buy_date)
    degraded: list[str] = []
    feature: dict[str, Any] = {
        "code": code, "name": panel.names.get(code, ""),
        "trade_date": signal_date,
        "open_gap_pct": None, "open_price": None, "pre_close": None,
        "auction_amount_ratio": None, "auction_volume_ratio": None,
        "takeover_score": None,
        "prev_limit_up_streak": None, "is_market_top_board": False,
        "market_max_streak": cycle.get("max_streak"),
        "market_top_board_count": None,
        "prev_amount": None, "prev_broken_board": pool == POOL_BROKEN,
        "broken_from_streak": None,
        "recent_5d_top_board": False,
        "theme_is_leader": None, "theme_heat": None,
        "is_one_word_board": None,
        "turnover_rate": None, "circulating_market_value": None,
        "amplitude": None, "degraded": degraded,
    }
    if today is None:
        degraded.append("signal_bar_missing:信号日无日线")
        return feature

    feature["prev_amount"] = today.amount
    feature["prev_limit_up_streak"] = today.streak or None
    # 断板票的**身位**是它断板前的高度（信号日连板数必然是 0，直接取会显示成"没身位"）。
    # ⚠️ 只用于展示与 `Signal.streak`：`is_market_top_board` 仍然只看**信号日封板**
    # 的票 —— 否则"今日断板"会被当成"当前最高板"，追击式会从断板池里出信号。
    if pool == POOL_BROKEN:
        previous = panel.day_before(signal_date)
        before = panel.bar(code, previous) if previous else None
        feature["broken_from_streak"] = before.streak if before is not None else None
    feature["turnover_rate"] = today.turnover_rate
    feature["circulating_market_value"] = today.circ_mv
    market_top = _to_float(cycle.get("max_streak"))
    if market_top is not None and today.streak:
        feature["is_market_top_board"] = today.streak >= int(market_top)
    top_count = sum(1 for item in
                    (panel.pools.get(signal_date) or {}).get(POOL_LIMIT_UP, [])
                    if (panel.bar(item, signal_date) or Bar()).streak == panel
                    .top_streak.get(signal_date, 0))
    feature["market_top_board_count"] = top_count
    # 近 5 日是否当过最高板（无极式的避开项）
    recent = False
    for day in panel.trailing(signal_date, window=5):
        if panel.top_code.get(day) == code:
            recent = True
            break
    feature["recent_5d_top_board"] = recent
    if today.pre_close:
        feature["amplitude"] = round(
            (today.high - today.low) / today.pre_close * 100.0, 3)

    if tomorrow is None:
        degraded.append("buy_bar_missing:买入日无日线（停牌？）")
        return feature
    feature["open_price"] = tomorrow.open
    feature["pre_close"] = today.close
    if today.close:
        feature["open_gap_pct"] = round(
            (tomorrow.open / today.close - 1.0) * 100.0, 4)
    if tomorrow.up_limit is not None:
        feature["is_one_word_board"] = (
            abs(tomorrow.open - tomorrow.up_limit) < _FLOAT_TOLERANCE)

    degraded.append("auction_takeover_unavailable:历史无逐笔委托 → 无盘口承接分")
    degraded.append("theme_rank_unavailable:历史无题材热度榜 → 无主线龙头判定")
    return feature


def _relaxed_rules(strict: Any, relaxed: Any) -> list[str]:
    """`unavailable` 声明**实际降级掉**的判据（严格 → 宽松 的差集）。

    ⚠️ 不能简单收集"所有 `blocking=False` 且没过的判据" ——
    像破冰式的"冰点共振单项"本来就是 `blocking=False`（原文要求共振、
    不要求全中），那样会把**设计如此**的项误报成"因缺数据而降级"。
    所以要比对同一只票的两次判定，取**否决权真的被摘掉**的那些。
    """
    out: list[str] = []
    for before, after in zip(strict.fits, relaxed.fits, strict=True):
        for old, new in zip(before.checks, after.checks, strict=True):
            if old.get("blocking", True) and not new.get("blocking", True):
                out.append(f"{after.name}·{new['rule']}")
    return out


def _universe_guard(config: ShortTermConfig) -> tuple[Any, dict[str, Any]]:
    """实盘股票池口径（非 ST + 流通市值区间）。

    默认读 `auction_select` 的配置（**回测的候选池必须和实盘选股是同一个池子**，
    否则回测出来的胜率对应的不是线上跑的那个策略）；
    配置里给了钉住值就用钉住值，并把生效区间写进 `diagnostics.universe`。
    """
    if not config.universe_filter:
        return None, {"enabled": False}
    import dataclasses

    from src.auction_select.config import CONFIG_PATH, load_config

    live = load_config()
    pinned = (config.min_market_cap is not None
              or config.max_market_cap is not None
              or config.exclude_st is not None)
    universe = dataclasses.replace(
        live.universe,
        exclude_st=(live.universe.exclude_st if config.exclude_st is None
                    else bool(config.exclude_st)),
        min_market_cap=(float(live.universe.min_market_cap)
                        if config.min_market_cap is None
                        else float(config.min_market_cap)),
        max_market_cap=(float(live.universe.max_market_cap)
                        if config.max_market_cap is None
                        else float(config.max_market_cap)))
    effective = dataclasses.replace(live, universe=universe)
    detail = {
        "enabled": True,
        "pinned": pinned,
        "exclude_st": bool(universe.exclude_st),
        "min_market_cap": float(universe.min_market_cap),
        "max_market_cap": float(universe.max_market_cap),
        "config_path": str(CONFIG_PATH),
    }
    return effective, detail


def _passes_universe(*, name: str, market_cap: float | None, config: Any) -> bool:
    from src.auction_select.config import in_market_cap_range, is_st

    if config.universe.exclude_st and is_st(name):
        return False
    return in_market_cap_range(market_cap, config)


def generate_signals(panel: MarketPanel, config: ShortTermConfig
                     ) -> tuple[list[Signal], dict[str, Any]]:
    """逐日逐票跑 `classify_mode`，产出信号（买入日 = 信号日的下一交易日）。"""
    from src.auction_select import modes as modes_module

    unavailable: list[str] = []
    if config.relax_unavailable:
        unavailable = list(RELAXABLE_UNAVAILABLE)
    universe_config, universe_detail = _universe_guard(config)

    signals: list[Signal] = []
    diagnostics: dict[str, Any] = {
        "candidates": 0, "matched": 0, "unmatched": 0,
        "blocked_one_word": 0, "missing_buy_bar": 0,
        "filtered_universe": 0, "filtered_st": 0, "filtered_cap": 0,
        "filtered_no_cap": 0, "signals_only_via_relaxation": 0,
        "relax_only_by_mode": {},
        "mode_histogram": {}, "relaxations": {}, "stages": {},
        "unevaluated_modes": {}, "pool_candidates": {},
        "universe": universe_detail,
    }
    for signal_date in panel.days:
        if config.start and signal_date < config.start:
            continue
        if config.end and signal_date > config.end:
            continue
        buy_date = panel.next_day(signal_date)
        cycle = panel.cycles.get(signal_date) or {}
        if buy_date is None:
            continue
        stage = str(cycle.get("stage") or "")
        diagnostics["stages"][stage] = diagnostics["stages"].get(stage, 0) + 1
        for pool, codes in (panel.pools.get(signal_date) or {}).items():
            if pool == "limit_down":
                continue
            diagnostics["pool_candidates"][pool] = (
                diagnostics["pool_candidates"].get(pool, 0) + len(codes))
            for code in codes:
                diagnostics["candidates"] += 1
                today = panel.bar(code, signal_date)
                if universe_config is not None:
                    name = panel.names.get(code, "")
                    cap = today.circ_mv if today is not None else None
                    if not _passes_universe(name=name, market_cap=cap,
                                            config=universe_config):
                        diagnostics["filtered_universe"] += 1
                        from src.auction_select.config import is_st

                        if universe_config.universe.exclude_st and is_st(name):
                            diagnostics["filtered_st"] += 1
                        elif cap is None:
                            diagnostics["filtered_no_cap"] = (
                                diagnostics.get("filtered_no_cap", 0) + 1)
                        else:
                            diagnostics["filtered_cap"] += 1
                        continue
                feature = build_feature(panel, code, signal_date, buy_date,
                                        pool=pool, cycle=cycle)
                verdict = modes_module.classify_mode(
                    feature=feature, market_cycle=cycle,
                    unavailable=unavailable)
                relaxations: list[str] = []
                strict_mode = verdict.mode
                if unavailable:
                    strict = modes_module.classify_mode(
                        feature=feature, market_cycle=cycle, unavailable=[])
                    relaxations = _relaxed_rules(strict, verdict)
                    strict_mode = strict.mode
                if not verdict.mode:
                    if config.require_mode:
                        diagnostics["unmatched"] += 1
                        continue
                else:
                    diagnostics["matched"] += 1
                    diagnostics["mode_histogram"][verdict.mode] = (
                        diagnostics["mode_histogram"].get(verdict.mode, 0) + 1)
                    # 只在宽松版成立、严格版判否 → 这个信号的**存在本身**依赖降级。
                    # 必须单独计数：否则"缺数据的模式"会以漂亮胜率混进结论里。
                    if not strict_mode:
                        diagnostics["signals_only_via_relaxation"] = (
                            diagnostics.get("signals_only_via_relaxation", 0) + 1)
                        only = diagnostics.setdefault("relax_only_by_mode", {})
                        only[verdict.mode] = only.get(verdict.mode, 0) + 1
                for text in relaxations:
                    diagnostics["relaxations"][text] = (
                        diagnostics["relaxations"].get(text, 0) + 1)
                tomorrow = panel.bar(code, buy_date)
                entry = tomorrow.open if tomorrow is not None else 0.0
                signal = Signal(
                    signal_date=signal_date, buy_date=buy_date, code=code,
                    name=panel.names.get(code, ""), pool=pool,
                    mode=verdict.mode, mode_name=verdict.name or "无匹配模式",
                    mode_branch=verdict.branch, mode_score=verdict.score,
                    mode_reason=verdict.reason,
                    streak=(panel.bar(code, signal_date) or Bar()).streak
                    or (feature.get("broken_from_streak") or 0),
                    is_top_board=bool(feature.get("is_market_top_board")),
                    market_stage=stage, ecology=str(cycle.get("ecology") or ""),
                    entry_price=entry,
                    amount=(panel.bar(code, signal_date) or Bar()).amount,
                    relaxations=relaxations,
                    feature=feature,
                    fits=[item.to_dict() for item in verdict.fits])
                if tomorrow is None:
                    signal.buy_blocked = "买入日无日线（停牌）"
                    diagnostics["missing_buy_bar"] += 1
                elif (config.block_one_word_entry
                      and tomorrow.up_limit is not None
                      and abs(tomorrow.open - tomorrow.up_limit) < _FLOAT_TOLERANCE):
                    signal.buy_blocked = "一字涨停（开盘即涨停）买不进"
                    diagnostics["blocked_one_word"] += 1
                signals.append(signal)

    # 数据不可用、本次回测**给不出结论**的模式（如实列出，不报 0 笔当胜率 0）
    if config.relax_unavailable:
        diagnostics["unevaluated_modes"] = {
            "qu_long": UNAVAILABLE_LABELS[UNAVAILABLE_THEME_RANK]}
    return signals, diagnostics


# ======================================================================
# 卖出走位（单笔）
# ======================================================================


def stop_price_for(panel: MarketPanel, code: str, buy_date: str,
                   entry_price: float, config: ShortTermConfig) -> float:
    """止损线。

    基准由 `config.stop_reference` 决定：
    - `entry`（默认，用户口径"**买入后**出现日内跌幅大于7%"）→ 买入价 × (1−7%)；
    - `prev_close` → 信号日收盘价 × (1−7%)（另一种常见读法，做敏感性对比用）。
    """
    base = entry_price
    if config.stop_reference == "prev_close":
        bar = panel.bar(code, buy_date)
        if bar is not None and bar.pre_close > 0:
            base = bar.pre_close
    return base * (1.0 - config.stop_loss_pct)


def walk_exit(panel: MarketPanel, code: str, buy_date: str, entry_price: float,
              *, config: ShortTermConfig) -> dict[str, Any]:
    """按用户口径走出这笔的卖出点（纯函数，不碰账户）。

    - 买入当日不检查（A 股 T+1）；当日若已跌破止损，记 `breached_entry_day`
      并在次日**开盘**卖出。
    - 跌停价不可成交 → 挂到下一交易日开盘（一字跌停可能连续挂几天）。
    """
    stop = stop_price_for(panel, code, buy_date, entry_price, config)
    breached = False
    pending = ""
    hold_days = 0

    entry_bar = panel.bar(code, buy_date)
    if entry_bar is not None and entry_bar.low <= stop:
        breached = True
        pending = "止损-买入当日触发（T+1 次日开盘卖）"

    def _result(date: str, price: float, reason: str, *,
                open_position: bool = False) -> dict[str, Any]:
        return {"exit_date": date, "exit_price": price, "exit_reason": reason,
                "hold_days": hold_days, "breached_entry_day": breached,
                "open_position": open_position}

    date = buy_date
    while True:
        following = panel.next_day(date)
        if following is None:
            bar = panel.bar(code, date) or entry_bar
            price = bar.close if bar is not None else entry_price
            return _result(date, price, "期末未平仓", open_position=True)
        date = following
        bar = panel.bar(code, date)
        if bar is None:
            hold_days += 1
            continue
        hold_days += 1
        if pending:
            if not bar.at_down_limit(bar.open):
                return _result(date, bar.open, pending)
            continue
        if bar.open <= stop:
            if not bar.at_down_limit(bar.open):
                return _result(date, bar.open, "止损-跳空跌破")
            pending = "止损-跳空跌破（一字跌停无法成交）"
            continue
        if bar.low <= stop:
            return _result(date, stop, "止损-日内跌破")
        average = panel.ma(code, date, config.ma_window)
        if average is not None and bar.close < average:
            if not bar.at_down_limit(bar.close):
                return _result(date, bar.close, f"破{config.ma_window}日线")
            pending = f"破{config.ma_window}日线（跌停无法成交）"


def _trade_return(entry_price: float, exit_price: float,
                  config: ShortTermConfig) -> tuple[float, float]:
    """返回 `(净收益率, 单位成本费用率)`：买入含佣金、卖出含佣金+印花税。"""
    buy = entry_price * (1.0 + config.slippage_rate) * (1.0 + config.commission_rate)
    sell = exit_price * (1.0 - config.slippage_rate) * (
        1.0 - config.commission_rate - config.stamp_tax_rate)
    if buy <= 0:
        return 0.0, 0.0
    return sell / buy - 1.0, 1.0 - buy / (entry_price * (1.0 + config.slippage_rate))


def flat_trades(panel: MarketPanel, signals: list[Signal],
                config: ShortTermConfig, *, label: str = "") -> list[dict[str, Any]]:
    """**不受仓位限制**地逐笔走一遍卖出（用于"战法信号 vs 全部涨停股"的对照）。

    组合模拟受"最多 5 笔并发"约束，会把信号最多的那几天砍掉大半；
    要做 apples-to-apples 的胜率对比，必须另有一层不做仓位裁剪的口径。
    """
    out: list[dict[str, Any]] = []
    for signal in signals:
        if signal.buy_blocked or not signal.entry_price:
            continue
        walk = walk_exit(panel, signal.code, signal.buy_date,
                         signal.entry_price, config=config)
        ret, _fee = _trade_return(signal.entry_price, walk["exit_price"], config)
        out.append({
            "label": label or signal.mode, "code": signal.code,
            "name": signal.name, "mode": signal.mode,
            "mode_name": signal.mode_name, "pool": signal.pool,
            "signal_date": signal.signal_date, "buy_date": signal.buy_date,
            "entry_price": signal.entry_price,
            "exit_date": walk["exit_date"], "exit_price": walk["exit_price"],
            "exit_reason": walk["exit_reason"], "hold_days": walk["hold_days"],
            "ret_pct": ret, "streak": signal.streak,
            "market_stage": signal.market_stage, "ecology": signal.ecology,
            "open_position": walk["open_position"],
            "breached_entry_day": walk["breached_entry_day"],
        })
    return out


# ======================================================================
# 组合模拟
# ======================================================================


@dataclass
class Position:
    signal: Signal
    entry_price: float
    shares: float
    size: float
    cost: float
    stop_price: float
    pending: str = ""
    breached_entry_day: bool = False


def simulate_portfolio(panel: MarketPanel, signals: list[Signal],
                       config: ShortTermConfig
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                  list[dict[str, Any]], dict[str, Any]]:
    """账户级模拟：20%/笔、最多 5 笔并发、T+1 制度、跌停卖不出。

    返回 `(成交明细, 净值曲线, 被跳过的信号, 汇总)`。
    """
    by_date: dict[str, list[Signal]] = {}
    for signal in signals:
        by_date.setdefault(signal.buy_date, []).append(signal)
    for items in by_date.values():
        items.sort(key=lambda item: item.sort_key(config.rank_by))

    start_index = min((panel.index[d] for d in by_date if d in panel.index),
                      default=None)
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if start_index is None:
        return trades, curve, skipped, {"trades": 0, "total_fees": 0.0,
                                        "final_equity": config.initial_capital,
                                        "avg_positions": 0.0, "blocked": 0}

    cash = float(config.initial_capital)
    positions: dict[str, Position] = {}
    total_fees = 0.0
    position_days = 0
    blocked = 0

    def _close(position: Position, date: str, price: float,
               reason: str, hold_days: int) -> None:
        nonlocal cash, total_fees
        fill = price * (1.0 - config.slippage_rate)
        gross = position.shares * fill
        fee = gross * (config.commission_rate + config.stamp_tax_rate)
        net = gross - fee
        cash += net
        total_fees += fee
        ret, _ = _trade_return(position.entry_price, price, config)
        trades.append({
            "code": position.signal.code, "name": position.signal.name,
            "mode": position.signal.mode, "mode_name": position.signal.mode_name,
            "pool": position.signal.pool,
            "signal_date": position.signal.signal_date,
            "buy_date": position.signal.buy_date,
            "entry_price": position.entry_price,
            "exit_date": date, "exit_price": price, "exit_reason": reason,
            "shares": position.shares, "size": position.size,
            "hold_days": hold_days, "ret_pct": ret,
            "pnl": net - position.cost, "fees": fee,
            "streak": position.signal.streak,
            "market_stage": position.signal.market_stage,
            "ecology": position.signal.ecology,
            "breached_entry_day": position.breached_entry_day,
            "open_position": reason == "期末未平仓",
        })

    for day in panel.days[start_index:]:
        # ---- 1) 开盘卖出（挂单 + 跳空跌破止损）→ 释放的现金当天开盘可用 ----
        for code in list(positions):
            position = positions[code]
            if position.signal.buy_date == day:
                continue
            bar = panel.bar(code, day)
            if bar is None:
                continue
            reason = position.pending or (
                "止损-跳空跌破" if bar.open <= position.stop_price else "")
            if not reason or bar.at_down_limit(bar.open):
                continue
            hold = panel.index[day] - panel.index[position.signal.buy_date]
            _close(position, day, bar.open, reason, hold)
            del positions[code]

        # ---- 2) 开盘买入 ----
        for signal in by_date.get(day, []):
            if signal.buy_blocked:
                blocked += 1
                skipped.append({"buy_date": day, "code": signal.code,
                                "mode": signal.mode, "reason": signal.buy_blocked})
                continue
            bar = panel.bar(signal.code, day)
            if bar is None or bar.open <= 0 or bar.at_down_limit(bar.open):
                blocked += 1
                skipped.append({"buy_date": day, "code": signal.code,
                                "mode": signal.mode,
                                "reason": "买入日无成交（停牌/一字跌停）"})
                continue
            if signal.code in positions:
                skipped.append({"buy_date": day, "code": signal.code,
                                "mode": signal.mode, "reason": "已持有该票"})
                continue
            if len(positions) >= config.max_positions:
                skipped.append({"buy_date": day, "code": signal.code,
                                "mode": signal.mode,
                                "reason": f"并发已满（{config.max_positions} 笔）"})
                continue
            fill = bar.open * (1.0 + config.slippage_rate)
            equity = cash + sum(
                item.shares * (panel.bar(code, day).open
                               if panel.bar(code, day) else item.entry_price)
                for code, item in positions.items())
            budget = min(equity * config.per_position, cash)
            if budget <= 0 or fill <= 0:
                skipped.append({"buy_date": day, "code": signal.code,
                                "mode": signal.mode, "reason": "现金不足"})
                continue
            shares = budget / (fill * (1.0 + config.commission_rate))
            gross = shares * fill
            fee = gross * config.commission_rate
            cash -= gross + fee
            total_fees += fee
            position = Position(
                signal=signal, entry_price=fill, shares=shares,
                size=gross, cost=gross + fee,
                stop_price=stop_price_for(panel, signal.code, day, fill, config))
            if bar.low <= position.stop_price:
                position.breached_entry_day = True
                position.pending = "止损-买入当日触发（T+1 次日开盘卖）"
            positions[signal.code] = position

        # ---- 3) 日内跌破止损 → 止损价成交 ----
        for code in list(positions):
            position = positions[code]
            if position.signal.buy_date == day or position.pending:
                continue
            bar = panel.bar(code, day)
            if bar is None or bar.low > position.stop_price:
                continue
            hold = panel.index[day] - panel.index[position.signal.buy_date]
            _close(position, day, position.stop_price, "止损-日内跌破", hold)
            del positions[code]

        # ---- 4) 收盘跌破 N 日均线 → 收盘价成交 ----
        for code in list(positions):
            position = positions[code]
            if position.signal.buy_date == day or position.pending:
                continue
            bar = panel.bar(code, day)
            if bar is None:
                continue
            average = panel.ma(code, day, config.ma_window)
            if average is None or bar.close >= average:
                continue
            if bar.at_down_limit(bar.close):
                position.pending = f"破{config.ma_window}日线（跌停无法成交）"
                continue
            hold = panel.index[day] - panel.index[position.signal.buy_date]
            _close(position, day, bar.close, f"破{config.ma_window}日线", hold)
            del positions[code]

        # ---- 5) 收盘盯市 ----
        holdings = sum(
            item.shares * (panel.bar(code, day).close
                           if panel.bar(code, day) else item.entry_price)
            for code, item in positions.items())
        position_days += len(positions)
        curve.append({"date": day, "equity": cash + holdings,
                      "cash": cash, "positions": len(positions)})

    # 期末未平仓 → 按最后一天收盘价估值并平掉（便于统计，标记 open_position）
    last_day = panel.days[-1]
    for code, position in list(positions.items()):
        bar = panel.bar(code, last_day)
        price = bar.close if bar is not None else position.entry_price
        hold = panel.index[last_day] - panel.index[position.signal.buy_date]
        _close(position, last_day, price, "期末未平仓", hold)

    summary = {
        "trades": len(trades),
        "total_fees": round(total_fees, 6),
        "final_equity": (curve[-1]["equity"] if curve else config.initial_capital),
        "avg_positions": (round(position_days / len(curve), 3) if curve else 0.0),
        "blocked": blocked,
    }
    return trades, curve, skipped, summary


# ======================================================================
# 统计
# ======================================================================


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def summarize_returns(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """单笔层面的胜率/期望/盈亏比（`rows` 需带 `ret_pct`）。"""
    returns = [float(row["ret_pct"]) for row in rows]
    if not returns:
        return {"trades": 0, "win_rate": None, "avg_return": None,
                "avg_return_ex_best": None, "median_return": None,
                "avg_win": None, "avg_loss": None,
                "payoff_ratio": None, "profit_factor": None,
                "expectancy": None, "best": None, "worst": None,
                "avg_hold_days": None, "open_positions": 0,
                "exit_reasons": {}}
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    win_rate = len(wins) / len(returns)
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    ordered = sorted(returns)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2
              else (ordered[middle - 1] + ordered[middle]) / 2)
    # 去掉**最好的一笔**后的均值：超短收益分布右偏极重（一笔翻倍能把均值拉正），
    # 只报均值会让"靠一笔运气撑起来的正期望"看起来像稳定盈利。
    trim = sorted(returns)[:-1]
    reasons: dict[str, int] = {}
    for row in rows:
        key = str(row.get("exit_reason") or "")
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "trades": len(returns),
        "win_rate": round(win_rate, 4),
        "avg_return": round(sum(returns) / len(returns), 6),
        "avg_return_ex_best": round(sum(trim) / len(trim), 6) if trim else None,
        "median_return": round(median, 6),
        "avg_win": round(avg_win, 6) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 6) if avg_loss is not None else None,
        "payoff_ratio": (round(avg_win / abs(avg_loss), 4)
                         if avg_win is not None and avg_loss else None),
        "profit_factor": (round(gross_profit / gross_loss, 4)
                          if gross_loss > 0 else None),
        "expectancy": round(
            win_rate * (avg_win or 0.0) + (1 - win_rate) * (avg_loss or 0.0), 6),
        "best": round(max(returns), 6),
        "worst": round(min(returns), 6),
        "avg_hold_days": round(_mean([float(row.get("hold_days") or 0)
                                      for row in rows]) or 0.0, 2),
        "open_positions": sum(1 for row in rows if row.get("open_position")),
        "exit_reasons": reasons,
    }


def summarize_curve(curve: list[dict[str, Any]], *,
                    initial: float = 1.0) -> dict[str, Any]:
    """账户层面：累计收益 / 最大回撤 / 年化波动 / 夏普 / 平均持仓数。"""
    if not curve:
        return {"days": 0, "total_return": None, "max_drawdown": None,
                "annualized_volatility": None, "sharpe_rf0": None,
                "avg_positions": None, "exposure": None}
    equity = [float(row["equity"]) for row in curve]
    returns = [equity[i] / equity[i - 1] - 1.0
               for i in range(1, len(equity)) if equity[i - 1] > 0]
    total = equity[-1] / initial - 1.0 if initial else None
    volatility = None
    sharpe = None
    if len(returns) > 1:
        average = sum(returns) / len(returns)
        variance = sum((value - average) ** 2 for value in returns) / len(returns)
        volatility = (variance ** 0.5) * (_TRADING_DAYS ** 0.5)
        if volatility > 0:
            sharpe = average * _TRADING_DAYS / volatility
    positions = [float(row.get("positions") or 0) for row in curve]
    return {
        "days": len(curve),
        "total_return": round(total, 6) if total is not None else None,
        "max_drawdown": round(_max_drawdown(equity), 6),
        "annualized_volatility": round(volatility, 6) if volatility else None,
        "sharpe_rf0": round(sharpe, 4) if sharpe is not None else None,
        "avg_positions": round(sum(positions) / len(positions), 3),
        "exposure": round(sum(positions) / len(positions) * 0.20, 4),
    }


def group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row.get(key) or "—"), []).append(row)
    return out


# ======================================================================
# 顶层
# ======================================================================


def _window(panel: MarketPanel, config: ShortTermConfig,
            signal_count: int) -> dict[str, Any]:
    """回测区间摘要。

    ⚠️ **可出信号的最后一天 = 倒数第二个交易日**：信号要买入日（T+1）才能成交，
    最后一天没有 T+1。把它报成"信号结束日"会让人以为最后一天也在选股。
    """
    start = config.start or panel.days[0]
    evaluable = panel.days[-2] if len(panel.days) > 1 else panel.days[-1]
    end = min(config.end or evaluable, evaluable)
    days = [day for day in panel.days if start <= day <= end]
    return {
        "panel_start": panel.days[0], "panel_end": panel.days[-1],
        "signal_start": start, "signal_end": end,
        "signal_days": len(days),
        "last_bar_date_has_no_buy_day": panel.days[-1],
        "signal_count": signal_count,
    }


def _code_revision() -> dict[str, str]:
    """本报告依赖的模块的短哈希。

    仓库里可能有并发改动（这个仓库确实出现过），所以报告必须能对上**代码版本** ——
    否则同一份 JSON 隔一天复现不出同样的数字，也没人说得清是谁改的。
    """
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    out: dict[str, str] = {}
    for name in ("src/backtest/shortterm.py",
                 "src/auction_select/modes.py",
                 "src/auction_select/ecology.py",
                 "src/auction_select/service.py",
                 "src/auction_select/config.py",
                 "src/intraday/market_cycle.py"):
        path = root / name
        if path.exists():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return out


@dataclass
class ShortTermReport:
    """一次超短战法回测的完整结果。"""

    config: ShortTermConfig = field(default_factory=ShortTermConfig)
    window: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    overall: dict[str, Any] = field(default_factory=dict)
    by_mode: dict[str, Any] = field(default_factory=dict)
    by_pool: dict[str, Any] = field(default_factory=dict)
    by_stage: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    code_revision: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self, *, with_trades: bool = True,
                with_signals: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "config": {
                "start": self.config.start, "end": self.config.end,
                "per_position": self.config.per_position,
                "max_positions": self.config.max_positions,
                "stop_loss_pct": self.config.stop_loss_pct,
                "ma_window": self.config.ma_window,
                "commission_rate": self.config.commission_rate,
                "stamp_tax_rate": self.config.stamp_tax_rate,
                "slippage_rate": self.config.slippage_rate,
                "use_advance_ratio": self.config.use_advance_ratio,
                "relax_unavailable": self.config.relax_unavailable,
                "rank_by": self.config.rank_by,
            },
            "window": self.window,
            "diagnostics": self.diagnostics,
            "overall": self.overall,
            "by_mode": self.by_mode,
            "by_pool": self.by_pool,
            "by_stage": self.by_stage,
            "baseline": self.baseline,
            "portfolio": self.portfolio,
            "equity_curve": self.equity_curve,
            "skipped_count": len(self.skipped),
            "skipped_sample": self.skipped[:20],
            "sources": self.sources,
            "code_revision": self.code_revision,
            "gaps": self.gaps,
        }
        if with_trades:
            payload["trades"] = self.trades
        if with_signals:
            payload["signals"] = self.signals
        return payload


def run_shortterm_backtest(config: ShortTermConfig | None = None,
                           *, panel: MarketPanel | None = None
                           ) -> ShortTermReport:
    """跑一次完整回测（面板可注入，便于测试不碰数据库）。"""
    config = config or ShortTermConfig()
    report = ShortTermReport(config=config)
    panel = panel or build_panel(start=config.start, end=config.end,
                                 load_start=config.load_start,
                                 use_advance_ratio=config.use_advance_ratio)
    report.sources = dict(panel.sources)
    report.code_revision = _code_revision()
    report.gaps = list(panel.gaps)
    if not panel.days:
        return report

    signals, diagnostics = generate_signals(panel, config)
    report.diagnostics = diagnostics
    report.window = _window(panel, config, len(signals))
    report.signals = [signal.to_dict() for signal in signals]

    trades = flat_trades(panel, signals, config, label="mode")
    report.trades = trades
    report.overall = summarize_returns(trades)
    report.by_mode = {key: summarize_returns(rows)
                      for key, rows in group_by(trades, "mode").items()}
    report.by_pool = {key: summarize_returns(rows)
                      for key, rows in group_by(trades, "pool").items()}
    report.by_stage = {key: summarize_returns(rows)
                       for key, rows in group_by(trades, "market_stage").items()}

    # ---- 无条件对照：当日**全部**涨停股、同样的卖出规则、不做仓位裁剪 ----
    baseline_config = ShortTermConfig(**{**config.__dict__, "require_mode": False})
    baseline_signals, _ = generate_signals(panel, baseline_config)
    baseline_rows = flat_trades(panel, baseline_signals, config, label="all")
    report.baseline = {
        "label": "无条件对照：当日全部涨停股（含无模式），同样的卖出规则",
        **summarize_returns(baseline_rows),
    }

    portfolio_trades, curve, skipped, summary = simulate_portfolio(
        panel, signals, config)
    report.skipped = skipped
    report.equity_curve = [
        {"date": row["date"], "equity": round(row["equity"], 6),
         "positions": row["positions"]} for row in curve]
    report.portfolio = {
        **summarize_returns(portfolio_trades),
        **summarize_curve(curve, initial=config.initial_capital),
        **summary,
        "skipped": len(skipped),
    }
    return report
