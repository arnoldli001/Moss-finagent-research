"""资金流监控：数据模型（全部可 JSON 化，前端直接渲染）。

## 口径总览（每一条都标注了来源与频率，避免"看起来像实时其实不是"）

| 实体 | 数据 | 来源 | 频率 |
|---|---|---|---|
| 板块 | 当日流入/流出/净额、涨跌幅、公司家数、领涨股 | 同花顺 `stock_fund_flow_industry/concept(symbol="即时")` | **盘中实时** |
| 板块 | 近 N 日净额序列 | 东财 `stock_sector_fund_flow_hist` / `stock_concept_fund_flow_hist` | 日频（收盘定稿） |
| 个股 | 主力/大单/超大单净额序列（**权威、可回溯**） | Tushare `moneyflow`（本地仓库） | 日频（收盘后） |
| 个股 | 流通市值（用于"净流入/流通市值"口径） | Tushare `daily_basic.circ_mv`（本地仓库） | 日频 |
| 个股 | 盘中实时净额/净占比 | 东财 `stock_individual_fund_flow`（akshare） | 日频接口，盘中返回当日进行中口径 |

**排序口径**（用户要求"净流入大资金平均值 / 流通市值"）：
    score = mean(最近 N 日 net_mf_amount) / circ_mv
`net_mf_amount` 是 Tushare 的"净流入额"（主力口径，单位元），`circ_mv` 是流通市值（元）。
用**比值**而不是绝对额，是为了让大小盘可比 —— 绝对值排行永远是大市值股票的天下。

**诚实标注**：每个序列带 `source` 与 `unit`，每个响应带 `gaps`；取不到就空着并说明原因，
绝不用旧值或估算值顶替（与项目其余部分的缺口处理一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["sector", "stock"]


@dataclass
class FlowPoint:
    """一天的一条资金流数据（单位：元）。"""

    date: str
    net: float | None = None               # 净额（板块=净额；个股=主力净额）
    buy_lg: float | None = None            # 大单买入（个股）
    sell_lg: float | None = None           # 大单卖出（个股）
    buy_elg: float | None = None           # 超大单买入（个股）
    sell_elg: float | None = None          # 超大单卖出（个股）
    # ---- 日线 OHLCV（走势图叠加 K 线与成交量柱用；板块无此三项，留空）----
    close: float | None = None             # 收盘价
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None            # 成交量（股）
    amount: float | None = None            # 成交额（元）
    pct_chg: float | None = None           # 当日涨跌幅（%）

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "net": self.net, "buy_lg": self.buy_lg,
                "sell_lg": self.sell_lg, "buy_elg": self.buy_elg,
                "sell_elg": self.sell_elg, "close": self.close,
                "open": self.open, "high": self.high, "low": self.low,
                "volume": self.volume, "amount": self.amount,
                "pct_chg": self.pct_chg}


@dataclass
class FlowEntity:
    """一个被监控的实体（板块或个股）及其近 N 日资金流。"""

    kind: Kind
    code: str
    name: str = ""
    # manual=用户手动加入；default=系统默认热门
    source: str = "manual"
    available: bool = False
    unit: str = "元"
    # 数据来源说明（前端直接展示，避免"这数哪来的"）
    data_source: str = ""
    series: list[FlowPoint] = field(default_factory=list)
    # ---- 排名口径 ----
    # 最近 N 日净额均值（元）
    net_avg: float | None = None
    # 个股：净额均值 / 流通市值（无量纲，越小越"轻"）
    net_to_mv: float | None = None
    circ_mv: float | None = None
    # 板块：当日盘中净额（元，来自同花顺即时）
    today_net: float | None = None
    change_pct: float | None = None
    # 最近一日净额（用于排行展示"最新一日"）
    latest_net: float | None = None
    latest_date: str = ""
    # ---- 个股榜的三类来源（2026-09-17 用户口径）----
    #: 该票属于哪一类：昨日涨停 / 净流入前10 / 净流出前10 / 自选 / 其他
    rank_group: str = ""
    #: 涨停原因（东财涨停池的"所属行业"，仅涨停股有；取不到留空）
    limitup_reason: str = ""
    #: 涨停原因所属的交易日（避免把昨天的池子当成今天的）
    limitup_date: str = ""
    #: 涨幅数据来源（腾讯盘中快照 / 本地仓库日频），前端可据此判断新旧
    change_source: str = ""
    gap: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "code": self.code, "name": self.name,
            "source": self.source, "available": self.available, "unit": self.unit,
            "data_source": self.data_source,
            "series": [point.to_dict() for point in self.series],
            "net_avg": self.net_avg, "net_to_mv": self.net_to_mv,
            "circ_mv": self.circ_mv, "today_net": self.today_net,
            "change_pct": self.change_pct, "latest_net": self.latest_net,
            "latest_date": self.latest_date, "gap": self.gap,
            "rank_group": self.rank_group,
            "limitup_reason": self.limitup_reason,
            "limitup_date": self.limitup_date,
            "change_source": self.change_source,
            "notes": list(self.notes),
        }


@dataclass
class FlowBoard:
    """一次资金流监控的完整结果（板块榜 + 个股榜 + 走势）。"""

    generated_at: str = ""
    window_days: int = 10
    trade_date: str = ""
    session_state: str = ""
    session_label: str = ""
    # 榜单（按口径排序）
    sector_rank: list[FlowEntity] = field(default_factory=list)
    stock_rank: list[FlowEntity] = field(default_factory=list)
    # 用户选择（含榜单外的自定义加入项）的走势数据
    sectors: list[FlowEntity] = field(default_factory=list)
    stocks: list[FlowEntity] = field(default_factory=list)
    # 行情时间（用于"多快能到屏幕上"的诚实标注）
    source_notes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    refresh_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "window_days": self.window_days,
            "trade_date": self.trade_date,
            "session_state": self.session_state,
            "session_label": self.session_label,
            "sector_rank": [item.to_dict() for item in self.sector_rank],
            "stock_rank": [item.to_dict() for item in self.stock_rank],
            "sectors": [item.to_dict() for item in self.sectors],
            "stocks": [item.to_dict() for item in self.stocks],
            "source_notes": list(self.source_notes),
            "gaps": list(self.gaps),
            "refresh_hint": self.refresh_hint,
        }
