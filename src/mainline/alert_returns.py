"""主线挖掘 · 信号收益追踪面板（回测报告页签里的「回测收益展示」）的数据层。

## 这个面板回答什么问题

「回测报告」原本只看**汇总指标**（IC / ICIR / 多空年化）—— 那是"这套评分作为
选股因子有没有效"。用户真正会追问的下一句是：

    "那它具体在哪些概念上赚了钱？发信号之后我该拿多久？"

所以这个模块把**每一条告警**摊开成一行，追踪信号发出后三个周期的
**最大涨幅**（7 / 20 / 60 个交易日），并回答三件事：

1. **哪些概念板块的钱是好赚的** —— 按板块汇总平均最大涨幅与胜率；
2. **同一概念反复触发算几次** —— 10 个交易日内重复触发折成 `X2/X3`，不重复占行；
3. **领涨的到底是哪几只票** —— 概念成分股里区间涨幅最好的 2~3 只（含股名与涨幅）。

## 三个必须说清楚的口径

**① 「最大涨幅」不是「区间收益」。** 取信号日之后窗口内**最高价**相对信号日
收盘价的最大偏离：`max(high) / entry_close - 1`。它是**理论可捕获空间**，
不是能拿到的收益 —— 择时卖出、滑点、涨停买不进都还没有扣。面板文案必须
带着这个限定词，否则会被读成"这个信号能赚 60%"。

**② 「暂时不填」而不是"用不足的窗口凑一个数"。** 数据到 2026-09-23 为止，
2026-08-25 之后的信号连 60 个交易日都还没走完。这时候如果照常输出
"60 日最大涨幅"，那个数**每天都在变**，和历史行的数不是同一个东西 ——
放在同一列里会让人以为可比。所以：
`max_gain_pct` 只在窗口**走满**时给值，没走满一律 `None`（前端显示为空白 +
"还剩 N 日"）；同时在 `current_gain_pct` 里如实给出"截至最新数据"的进度，
两个字段分开，不混用。

**③ 成分股用提纯股池，不用原始成员表。** `ml_member` 里概念板块的成员
是"同花顺正式成员"，含大量只沾一点边的公司 —— 项目里已经因为
"锂电池概念把 MLCC 的风华高科选成龙头"被用户报障过一次。所以领涨股优先取
`ml_member_pure` 里 `relevant=1` 的提纯股池；该板块没有提纯结果时才回落到原始
成员表，并在 `leaders_source` 里标明用了哪个口径。

## 性能：为什么分两段算

板块指数只有 9 万行（~0.3 秒），个股行情要走 15 GiB 行情仓、109715 个
(板块, 信号日, 成分股) 三元组（~5.6 秒，3 线程分片）。

**不能为了"一个接口一次拿全"让首屏等 6 秒**，所以领涨股是可选段：
`leaders=False` 时先返回全部板块级数据（首屏 ~0.5 秒），前端渲染完表格后再带
`leaders=True` 补一次，领涨股列从"计算中…"填成实际结果。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Collection, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief

logger = logging.getLogger(__name__)

#: 追踪的持有周期（交易日）。任务书要求 7/20/60；做成常量是因为**口径只能有一处**，
#: 前端表头、报告正文、这里的计算必须同源。
DEFAULT_HORIZONS: tuple[int, ...] = (7, 20, 60)
#: 重复触发折叠窗口（交易日）：同一概念在这个天数内再次触发算同一个信号周期。
DEFAULT_DEDUP_DAYS = 10
#: 领涨成分股的观察窗口（交易日）。取 20 日与项目既有的"信号兑现率"口径一致
#: （`BacktestMetrics.signal_hit_rate` 就是"触发后 20 日涨幅 > 5% 比例"）。
DEFAULT_STOCK_WINDOW = 20
#: 每行展示的领涨股数量。
DEFAULT_LEADERS = 3
#: 历史 / 近期 的分界日（含）。用户口径："历史数据（最早到 2026.8.25）"，
#: 其后的一个月算"近期与未来"。
DEFAULT_SPLIT = "20260825"
#: 默认只看中信号与强信号（弱信号不进入追踪面板）。
DEFAULT_LEVELS = ("strong", "medium")
#: 板块筛选门槛：**20 日胜率**下限（用户口径：*"只显示 20 日胜率大于 40% 的概念板块"*）。
#:
#: 判定口径是 `win_rate_20d` = 该板块**已走满**的 20 日窗口里
#: 「窗口末收盘 > 信号日收盘」的比例。这一条**取代**了早先的
#: 「20 日平均实际收益 ≥ 0」（见 `summarize_boards` 的长注释）。
#:
#: ⚠️ 比较是**严格大于**：胜率恰好等于门槛的板块**不显示**。
#: 用户先取 50%，后按"减少假阳性"放宽到 40%（2026-09-25）——
#: 本机实测：>50% 留 88 个题材、>40% 留 110 个（池子 138 个）。
DEFAULT_MIN_WIN_RATE = 0.4
#: 领涨股计算的并行分片数。实测 1 片 10.1 秒 / 3 片 5.6 秒 / 6 片 5.7 秒 ——
#: 行情仓是**同一个文件**，再多的连接只是在抢同一块盘的 IO，取 3。
_LEADER_WORKERS = 3

_LEVEL_LABELS = {"strong": "🔴 强信号", "medium": "🟡 中信号",
                 "weak": "🟢 弱信号", "none": "未触发"}


def _num(value: Any) -> float | None:
    """转 float；`None` / 空串 / 非数值一律 `None`（不要退化成 0）。"""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    """百分比涨幅。分母缺失/非正一律 `None`（不返回 0，避免"没数据"冒充"没涨"）。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round((numerator / denominator - 1.0) * 100.0, 2)


@dataclass
class WindowStat:
    """信号发出后某个周期的追踪结果。

    ## 为什么"最大涨幅"与"实际收益"要分成两组字段

    用户要看的表头是**最大涨幅**（"这个信号理论上能给我多大空间"），但
    **判断一个板块"亏不亏钱"绝不能只看最大涨幅**：窗口内最高价高于信号日
    收盘价几乎是必然事件（20 个交易日里总有一根上影线），实测按最大涨幅算
    "胜率"是 **97%** —— 那个数没有任何区分度，拿它做筛选等于不筛。

    所以每个周期同时算两族数，各回答一个问题：

    * `max_gain_pct` / `current_gain_pct` —— 理论可捕获空间（用户口径的表头）
    * `ret_pct` / `current_ret_pct` / `worst_pct` —— **持有到底赚没赚**（期末收盘）
      与**中途最多浮亏多少**（窗口内最低价）

    板块排行的显示/隐藏筛选走 `ret_pct` 一族（口径见 `summarize_boards`）。
    """

    days: int
    #: done=窗口走满 / partial=还没走满 / pending=信号日之后还没有K线
    status: str = "pending"
    #: **只有走满才给值**；没走满为 None（"暂时不填"，见模块文档②）
    max_gain_pct: float | None = None
    #: 截至最新数据、已走部分的最大涨幅（如实记录的"当前收益统计"）
    current_gain_pct: float | None = None
    #: 走满才给：窗口末收盘价相对信号日收盘的涨跌幅（"持有到底"的收益）
    ret_pct: float | None = None
    #: 至今收盘价相对信号日收盘的涨跌幅（未走满时用）
    current_ret_pct: float | None = None
    #: 窗口内**最低价**相对信号日收盘的涨跌幅（负数 = 中途最多浮亏）
    worst_pct: float | None = None
    max_gain_date: str = ""
    #: 窗口内实际用到的K线根数
    bars_used: int = 0
    #: 还差几个交易日走满（0 = 已走满）
    remaining_days: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"days": self.days, "status": self.status,
                "max_gain_pct": self.max_gain_pct,
                "current_gain_pct": self.current_gain_pct,
                "ret_pct": self.ret_pct,
                "current_ret_pct": self.current_ret_pct,
                "worst_pct": self.worst_pct,
                "max_gain_date": self.max_gain_date,
                "bars_used": self.bars_used,
                "remaining_days": self.remaining_days}


@dataclass
class LeaderStock:
    """领涨成分股（区间涨幅最好的一只）。"""

    code: str
    name: str = ""
    gain_pct: float | None = None
    entry_close: float | None = None
    high: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name,
                "gain_pct": self.gain_pct, "entry_close": self.entry_close,
                "high": self.high}


@dataclass
class AlertReturnRow:
    """面板的一行 = 一个信号周期（同概念 10 个交易日内的重复触发折进 `alert_count`）。"""

    signal_date: str
    board_code: str
    board_name: str
    level: str
    level_label: str
    score: float | None
    #: 折叠进来的触发次数：1 = 单次；2 = "X2"；3 = "X3" …
    alert_count: int = 1
    #: 折叠进来的全部触发日（升序），用于"这一天其实连续报了 3 次"
    alert_dates: list[str] = field(default_factory=list)
    segment: str = "history"
    entry_close: float | None = None
    #: 信号日距最新数据的交易日数
    days_elapsed: int = 0
    latest_date: str = ""
    latest_close: float | None = None
    #: 信号日收盘 → 最新收盘的涨跌幅（"当前收益"）
    current_pct: float | None = None
    #: 各周期追踪结果，键是周期天数
    windows: dict[int, WindowStat] = field(default_factory=dict)
    leaders: list[LeaderStock] = field(default_factory=list)
    #: 领涨股取自哪个口径：pure=提纯股池 / member=原始成员表 / ""=没算
    leaders_source: str = ""
    #: 告警 payload 里**回测当时记录**的收益（5/10/20/60 日与最大涨幅），
    #: 用于和这里重算的口径互相校验（对不上就是数据或口径变了）
    recorded: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_date": self.signal_date, "board_code": self.board_code,
            "board_name": self.board_name, "level": self.level,
            "level_label": self.level_label, "score": self.score,
            "alert_count": self.alert_count,
            "alert_dates": list(self.alert_dates), "segment": self.segment,
            "entry_close": self.entry_close, "days_elapsed": self.days_elapsed,
            "latest_date": self.latest_date, "latest_close": self.latest_close,
            "current_pct": self.current_pct,
            "windows": {str(days): stat.to_dict()
                        for days, stat in sorted(self.windows.items())},
            "leaders": [item.to_dict() for item in self.leaders],
            "leaders_source": self.leaders_source,
            "recorded": self.recorded,
        }


def _trim_calendar(days: Sequence[str], start: str, end: str) -> list[str]:
    out = list(days)
    if start:
        out = [day for day in out if day >= start]
    if end:
        out = [day for day in out if day <= end]
    return out


def cluster_alerts(alerts: Iterable[dict[str, Any]], calendar: Sequence[str], *,
                   dedup_days: int = DEFAULT_DEDUP_DAYS
                   ) -> list[dict[str, Any]]:
    """把告警按「同一概念 + `dedup_days` 个交易日内」折叠成信号周期。

    规则（用户口径）：*"如果同一概念信号在最近 10 个交易日触发过了，就以
    X2 X3 记录告警次数，不用重复记录"*。

    两个刻意的选择：

    * **折叠基准是周期内的第一次触发，不是上一次触发。** 若按"距上一次 ≤10 日"
      链式折叠，一个每 8 天报一次的概念会**无限折叠成一行**（跨度半年也合成一条），
      面板上就再也看不到"最近又起了一波"。按首次触发算，周期长度有硬上限。
    * 周期保留**首次**触发的日期与等级：第一根信号才是可执行的那个点；
      后续触发只累加次数（`alert_count`），不改信号日。

    输入 `alerts` **顺序不限**：本函数自己按 `trade_date` 升序排。

    ⚠️ 这里踩过一次：仓储的 `load_alerts` 是 `ORDER BY trade_date DESC`
    （"新的在前"是给流水页签用的），而折叠必须**按时间顺序**推进。
    照传入顺序处理时，每个板块遇到的第一个告警是**最新**的那条，
    后续（更早的）告警与它的日期间隔是负数 → 折叠条件恒不成立 →
    **去重静默失效**（1965 条一条都没折，`alert_total == signals`）。
    所以顺序由本函数保证，不依赖调用方 —— 这种"静默不生效"最难发现：
    界面上只是"X2/X3"从不出现，看不出是 bug。

    返回同样是升序的周期列表。
    """
    ordered = sorted(
        (item for item in alerts if item.get("trade_date")),
        key=lambda item: (str(item.get("trade_date")), str(item.get("board_code"))))
    index = {day: position for position, day in enumerate(calendar)}
    periods: list[dict[str, Any]] = []
    open_slot: dict[str, int] = {}      # board_code -> 当前周期在 periods 里的下标
    for alert in ordered:
        date = str(alert.get("trade_date") or "")
        board = str(alert.get("board_code") or "")
        if not date or not board or date not in index:
            continue
        position = index[date]
        slot = open_slot.get(board)
        if slot is not None:
            current = periods[slot]
            opened = index.get(str(current["signal_date"]))
            if opened is not None and 0 <= position - opened <= max(int(dedup_days), 0):
                current["alert_count"] += 1
                current["alert_dates"].append(date)
                score = _num(alert.get("score"))
                if score is not None and (current["score"] is None
                                          or score > current["score"]):
                    current["score"] = score
                continue
        open_slot[board] = len(periods)
        periods.append({
            "signal_date": date, "board_code": board,
            "board_name": str(alert.get("board_name") or ""),
            "level": str(alert.get("level") or ""),
            "level_label": str((alert.get("payload") or {}).get("level_label")
                               or _LEVEL_LABELS.get(str(alert.get("level")), "")),
            "score": _num(alert.get("score")),
            "alert_count": 1, "alert_dates": [date],
            "entry_close_alert": _num(alert.get("entry_close")),
            "payload": alert.get("payload") or {},
        })
    return periods


# ==================================================================
# 板块级：各周期最大涨幅
# ==================================================================


def build_rows(alerts: Sequence[dict[str, Any]], store: Any, *,
               calendar: Sequence[str], horizons: Sequence[int] = DEFAULT_HORIZONS,
               dedup_days: int = DEFAULT_DEDUP_DAYS, split: str = DEFAULT_SPLIT,
               ) -> tuple[list[AlertReturnRow], list[str]]:
    """算每个信号周期的 7/20/60 日最大涨幅（只读板块指数，不碰个股行情）。"""
    gaps: list[str] = []
    periods = cluster_alerts(alerts, calendar, dedup_days=dedup_days)
    if not periods:
        return [], ["本地没有中/强信号告警记录 —— 先跑一次当日评分"]

    codes = sorted({item["board_code"] for item in periods})
    series = store.board_bars(codes, start=calendar[0], end=calendar[-1])
    index = {day: position for position, day in enumerate(calendar)}
    rows: list[AlertReturnRow] = []
    missing_bars: list[str] = []

    for period in periods:
        board = period["board_code"]
        bars = series.get(board)
        # (日期, 最高, 最低, 收盘)：最高算"理论空间"，最低算"中途浮亏"，
        # 收盘算"持有到底"。三个口径都从同一批K线取，避免不同源对不上。
        history = [(bar.date, bar.high, bar.low, bar.close) for bar in bars.bars] \
            if bars is not None else []
        if not history:
            missing_bars.append(board)
        by_date = {item[0]: item for item in history}
        signal_date = period["signal_date"]
        row = AlertReturnRow(
            signal_date=signal_date, board_code=board,
            board_name=period["board_name"], level=period["level"],
            level_label=period["level_label"], score=period["score"],
            alert_count=period["alert_count"],
            alert_dates=list(period["alert_dates"]),
            segment="history" if signal_date <= split else "recent",
        )
        bar = by_date.get(signal_date)
        entry = bar[3] if bar is not None and bar[3] > 0 else period["entry_close_alert"]
        row.entry_close = entry if entry and entry > 0 else None
        start_at = index.get(signal_date)
        future = [item for item in history if item[0] > signal_date] \
            if start_at is not None else []
        row.days_elapsed = len(future)
        if future:
            row.latest_date = future[-1][0]
            row.latest_close = future[-1][3]
            row.current_pct = _pct(row.latest_close, row.entry_close)
        else:
            row.latest_date = signal_date
            row.latest_close = bar[3] if bar is not None else None

        for horizon in horizons:
            stat = WindowStat(days=int(horizon))
            window = future[: int(horizon)]
            stat.bars_used = len(window)
            stat.remaining_days = max(int(horizon) - len(window), 0)
            if not window or row.entry_close is None:
                stat.status = "pending"
            else:
                best_date, best_high = "", None
                lowest = None
                for date, high, low, _close in window:
                    if high is not None and (best_high is None or high > best_high):
                        best_date, best_high = date, high
                    if low is not None and low > 0 and (lowest is None or low < lowest):
                        lowest = low
                stat.current_gain_pct = _pct(best_high, row.entry_close)
                stat.worst_pct = _pct(lowest, row.entry_close)
                stat.max_gain_date = best_date
                stat.current_ret_pct = _pct(window[-1][3], row.entry_close)
                if stat.remaining_days == 0:
                    stat.status = "done"
                    stat.max_gain_pct = stat.current_gain_pct
                    stat.ret_pct = stat.current_ret_pct
                else:
                    stat.status = "partial"
            row.windows[int(horizon)] = stat

        payload = period["payload"]
        # 只带**数值**：`reasons` 是三条长中文句子（占这一行载荷的近一半），
        # 而它的用途只是"和重算口径互相校验"，正文里有完整版本。
        row.recorded = {
            "ret_5d": _num(payload.get("ret_5d")),
            "ret_10d": _num(payload.get("ret_10d")),
            "ret_20d": _num(payload.get("ret_20d")),
            "ret_60d": _num(payload.get("ret_60d")),
            "max_gain_pct": _num(payload.get("max_gain_pct")),
            "max_gain_date": str(payload.get("max_gain_date") or ""),
            "confirmed": bool(payload.get("confirmed")),
        }
        rows.append(row)

    if missing_bars:
        unique = sorted(set(missing_bars))
        gaps.append(f"{len(unique)} 个板块在本地没有指数日线（无法追踪收益）："
                    + "、".join(unique[:6]) + ("…" if len(unique) > 6 else ""))
    rows.sort(key=lambda item: (item.signal_date, item.board_code), reverse=True)
    return rows, gaps


# ==================================================================
# 个股级：区间领涨成分股
# ==================================================================

_LEADER_SQL = """
    SELECT p.board_code, p.signal_date, p.code,
           e.close AS entry, m.mx AS fwd
    FROM pairs p
    JOIN quant_daily e ON e.code = p.code AND e.trade_date = p.signal_date
    JOIN (SELECT p2.board_code AS bc, p2.signal_date AS sd, p2.code AS c,
                 MAX(q.high) AS mx
            FROM pairs p2
            JOIN quant_daily q ON q.code = p2.code
             AND q.trade_date > p2.signal_date
             AND q.trade_date <= p2.end_date
           GROUP BY p2.board_code, p2.signal_date, p2.code) m
      ON m.bc = p.board_code AND m.sd = p.signal_date AND m.c = p.code
"""


def _member_pools(store: Any, boards: Sequence[str]
                  ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """返回 `(提纯股池, 原始成员)` —— 两个口径分别取，由调用方决定回落。"""
    raw: dict[str, list[str]] = {}
    for board in boards:
        for item in store.members(board):
            code = str(item.get("code") or "")
            if code:
                raw.setdefault(board, []).append(code)
    pure: dict[str, list[str]] = {}
    try:
        relevance = store.pure_member_relevance()
    except Exception as exc:  # noqa: BLE001 提纯表缺失不该让整个面板失败
        logger.warning("信号收益面板：读提纯股池失败（回落到原始成员）：%s",
                       brief(exc, BRIEF_TIGHT))
        relevance = {}
    for board in boards:
        pool = [code for code, flag in (relevance.get(board) or {}).items() if flag]
        if pool:
            pure[board] = pool
    return pure, raw


def _leader_pairs(rows: Sequence[AlertReturnRow], pure: dict[str, list[str]],
                  raw: dict[str, list[str]], calendar: Sequence[str], *,
                  stock_window: int) -> tuple[list[tuple[str, str, str, str]], dict[str, str]]:
    """构造 `(板块, 成分股, 信号日, 窗口末日)` 三元组，并记录每行用了哪个股池口径。"""
    index = {day: position for position, day in enumerate(calendar)}
    last = len(calendar) - 1
    pairs: list[tuple[str, str, str, str]] = []
    source: dict[str, str] = {}
    for row in rows:
        position = index.get(row.signal_date)
        if position is None:
            continue
        end = calendar[min(position + int(stock_window), last)]
        pool = pure.get(row.board_code) or raw.get(row.board_code) or []
        if not pool:
            continue
        source[row.board_code] = "pure" if pure.get(row.board_code) else "member"
        for code in pool:
            pairs.append((row.board_code, code, row.signal_date, end))
    return pairs, source


def attach_leaders(rows: Sequence[AlertReturnRow], store: Any, *,
                   calendar: Sequence[str], stock_window: int = DEFAULT_STOCK_WINDOW,
                   top: int = DEFAULT_LEADERS, member_limit: int = 0,
                   workers: int = _LEADER_WORKERS) -> list[str]:
    """给每行补上"区间涨幅最好的 2~3 只成分股"（就地修改 `rows`）。

    ⚠️ **行情仓必须走 `store.warehouse`**（`WarehouseSource` → `warehouse.open_warehouse`
    这唯一入口）：15 GiB 只读库在"有别的进程并发访问"时会坏 WAL，项目里已经因此
    踩过三次坑（其中一次让提纯链路静默失效）。这里绝不自己 `sqlite3.connect`。
    """
    gaps: list[str] = []
    warehouse = getattr(store, "warehouse", None)
    if warehouse is None or not getattr(warehouse, "available", lambda: False)():
        return ["本地行情仓不可用，领涨成分股无法计算（板块级收益不受影响）"]

    boards = sorted({row.board_code for row in rows})
    pure, raw = _member_pools(store, boards)
    if member_limit > 0:
        pure = {key: value[:member_limit] for key, value in pure.items()}
        raw = {key: value[:member_limit] for key, value in raw.items()}
    pairs, source = _leader_pairs(rows, pure, raw, calendar,
                                 stock_window=stock_window)
    if not pairs:
        return ["没有可用的成分股名单，领涨成分股列为空"]

    chunks = _split(pairs, max(int(workers), 1) * 2)
    collected: list[tuple] = []
    try:
        if len(chunks) <= 1:
            collected = _run_leader_chunk(warehouse, pairs)
        else:
            with ThreadPoolExecutor(max_workers=max(int(workers), 1)) as pool:
                for part in pool.map(
                        lambda chunk: _run_leader_chunk(warehouse, chunk), chunks):
                    collected.extend(part)
    except sqlite3.Error as exc:
        logger.warning("信号收益面板：算领涨成分股失败：%s", brief(exc, BRIEF_TIGHT))
        return [f"领涨成分股计算失败（{brief(exc, BRIEF_TIGHT)}）"]

    best: dict[tuple[str, str], list[LeaderStock]] = {}
    for board, signal_date, code, entry, high in collected:
        gain = _pct(_num(high), _num(entry))
        if gain is None:
            continue
        best.setdefault((board, signal_date), []).append(
            LeaderStock(code=str(code), gain_pct=gain,
                        entry_close=_num(entry), high=_num(high)))
    wanted = sorted({item.code for group in best.values() for item in group})
    names = _stock_names(warehouse, wanted)
    for row in rows:
        group = best.get((row.board_code, row.signal_date)) or []
        group.sort(key=lambda item: (item.gain_pct is None, -(item.gain_pct or 0.0)))
        for item in group[: max(int(top), 1)]:
            item.name = names.get(item.code, "")
        row.leaders = group[: max(int(top), 1)]
        row.leaders_source = source.get(row.board_code, "")
    missing = [row for row in rows if not row.leaders]
    if missing:
        gaps.append(f"{len(missing)} 个信号周期没有算出领涨成分股"
                    "（成分股名单缺失，或信号后还没有个股K线）")
    return gaps


def _split(items: Sequence[Any], parts: int) -> list[list[Any]]:
    """按数量均分（保持顺序）。片数与每片内容都可复现，便于对账。"""
    if parts <= 1 or len(items) <= 1:
        return [list(items)]
    size = max(1, len(items) // parts)
    return [list(items[start:start + size])
            for start in range(0, len(items), size)]


def _run_leader_chunk(warehouse: Any, pairs: Sequence[tuple]) -> list[tuple]:
    """一个分片：建临时表 → 库内聚合。每片独享连接（SQLite 连接不是线程安全的）。"""
    connection = _open_warehouse(warehouse)
    try:
        connection.execute(
            "CREATE TEMP TABLE pairs(board_code TEXT, code TEXT,"
            " signal_date TEXT, end_date TEXT)")
        connection.executemany("INSERT INTO pairs VALUES(?,?,?,?)", pairs)
        connection.execute(
            "CREATE INDEX temp.idx_pairs ON pairs(code, signal_date)")
        return connection.execute(_LEADER_SQL).fetchall()
    finally:
        connection.close()


def _open_warehouse(warehouse: Any) -> sqlite3.Connection:
    """拿一个行情仓只读连接（复用 `WarehouseSource` 的唯一入口与退化策略）。"""
    path = getattr(warehouse, "path", None)
    if path is None:
        raise sqlite3.OperationalError("WarehouseSource 没有 path 属性")
    from src.mainline.warehouse import open_warehouse
    connection = open_warehouse(path, timeout=30.0)
    connection.row_factory = None      # 元组取值比 Row 快，这里只要位置
    return connection


def _stock_names(warehouse: Any, codes: Sequence[str]) -> dict[str, str]:
    """取股票简称（分块，避免 SQL 变量数超限）。"""
    out: dict[str, str] = {}
    for start in range(0, len(codes), 800):
        part = list(codes[start:start + 800])
        marks = ",".join("?" for _ in part)
        try:
            for row in warehouse.query(
                    f"SELECT code, name FROM quant_stock_basic"
                    f" WHERE code IN ({marks})", part):
                name = str(row["name"] or "")
                if name:
                    out[str(row["code"])] = name
        except sqlite3.Error as exc:
            logger.warning("信号收益面板：取股票简称失败：%s", brief(exc, BRIEF_TIGHT))
            break
    return out


# ==================================================================
# 板块汇总：哪些概念的钱好赚
# ==================================================================


def summarize_boards(rows: Sequence[AlertReturnRow], *,
                     horizons: Sequence[int] = DEFAULT_HORIZONS,
                     min_win_rate: float = DEFAULT_MIN_WIN_RATE
                     ) -> list[dict[str, Any]]:
    """按概念板块汇总，并给每个板块一个「显示 / 隐藏」判定（`passed`）。

    ## `passed` 的口径：**20 日胜率**，不是最大涨幅，也不是平均收益

    判据（用户口径 *"只显示 20 日胜率**大于** 40% 的概念板块"*）：

        win_rate_20d = 该板块**已走满**的 20 日窗口里「窗口末收盘 > 信号日收盘」的比例
        passed       = win_rate_20d > min_win_rate（默认 0.4）

    ⚠️ **严格大于**，不是"大于等于"：恰好等于门槛的板块**不显示** ——
    门槛两侧只差一个窗口，必须写死是哪一侧。

    **不能用最大涨幅判。** 见 `WindowStat` 的说明：按"20 日内最大涨幅 > 0"筛，
    实测通过率是 133/134 —— 窗口内最高价几乎必然高于信号日收盘，等于没筛，
    还会让人以为"这个系统几乎不亏钱"。

    **也不再叠加平均收益条件。** 早先的判据是"20 日**平均实际收益**（`ret_pct`）
    ≥ 0"；现在只留胜率这一条 —— 两者**不是同一件事**：5 个样本
    `+10/+1/+1/-6/-7` 胜率 60%、均值 −0.2%（**显示**，旧口径会隐藏它），
    而 `+30/-1/-2` 均值 +9%、胜率只有 33%（**隐藏**，旧口径会显示它）。

    ## `gate` 的三态，以及"窗口没走满"为什么不隐藏

    板块（概念）可能一条 20 日窗口都没走满 —— 最新的信号距数据末端不足
    20 个交易日。这时**它没有胜率可言**，也就谈不上"未过门槛"，因此不隐藏：

    * `pass`    —— 有已走满窗口且胜率 **>** 门槛 → 显示
    * `hidden`  —— 有已走满窗口但胜率 **≤** 门槛 → **隐藏**（含恰好等于门槛）
    * `pending` —— 没有任何已走满的 20 日窗口 → 无从判定，**显示**（在 `gate` 里标明）

    这一条同时避免了早先踩过的坑：若把"判不出"当"不通过"，最近一个月的板块会
    被整片剔除，面板上只剩历史。`passed` 是给前端用的布尔（`pending` 也算通过），
    `gate` 是给"为什么显示/隐藏"留的可解释字段。

    `quality` 仍按 20 日实际收益分档（≥5% good、≥0 ok、<0 poor），只是**展示**用，
    不参与筛选 —— 它回答"这个板块赚得多不多"，筛选回答"这个板块稳不稳"。
    """
    buckets: dict[str, list[AlertReturnRow]] = {}
    for row in rows:
        buckets.setdefault(row.board_code, []).append(row)

    def values_of(group: Sequence[AlertReturnRow], horizon: int, field: str
                  ) -> list[float]:
        out: list[float] = []
        for item in group:
            stat = item.windows.get(int(horizon))
            if stat is None:
                continue
            value = getattr(stat, field, None)
            if value is not None:
                out.append(float(value))
        return out

    # `group` 显式当参数传，不在循环里定义闭包去捕获它：闭包捕获循环变量是
    # 典型的"今天能跑、明天改一行就静默算错另一个板块"的写法（ruff B023）。
    def mean(group: Sequence[AlertReturnRow], horizon: int, field: str
             ) -> float | None:
        values = values_of(group, horizon, field)
        return round(sum(values) / len(values), 2) if values else None

    def share(group: Sequence[AlertReturnRow], horizon: int, field: str, *,
              above: float) -> float | None:
        values = values_of(group, horizon, field)
        if not values:
            return None
        return round(sum(1 for value in values if value > above) / len(values), 4)

    out: list[dict[str, Any]] = []
    for board, group in buckets.items():
        avg_ret_20 = mean(group, 20, "ret_pct")
        avg_ret_20_now = mean(group, 20, "current_ret_pct")
        # `basis` 仍按"走满的 20 日实际收益优先、未走满回落到至今收益"取，
        # 但它现在只用于展示（排序与 quality），**不再**决定是否隐藏板块。
        basis = avg_ret_20 if avg_ret_20 is not None else avg_ret_20_now
        win_rate = share(group, 20, "ret_pct", above=0.0)
        # 三态判定：判不出来的（没有任何已走满窗口）不隐藏，见函数文档。
        # 比较**严格大于** —— 恰好等于门槛算"抛硬币"，不显示。
        gate = ("pending" if win_rate is None
                else "pass" if win_rate > min_win_rate else "hidden")
        current = [item.current_pct for item in group
                   if item.current_pct is not None]
        levels: dict[str, int] = {}
        for item in group:
            levels[item.level] = levels.get(item.level, 0) + 1
        leaders: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in sorted(group, key=lambda row: row.signal_date, reverse=True):
            for stock in item.leaders:
                if stock.code in seen or stock.gain_pct is None:
                    continue
                seen.add(stock.code)
                leaders.append({"code": stock.code, "name": stock.name,
                                "gain_pct": stock.gain_pct,
                                "signal_date": item.signal_date})
                if len(leaders) >= 3:
                    break
            if len(leaders) >= 3:
                break
        out.append({
            "board_code": board,
            "board_name": group[0].board_name,
            "signals": len(group),
            "alert_total": sum(item.alert_count for item in group),
            "first_date": min(item.signal_date for item in group),
            "last_date": max(item.signal_date for item in group),
            "strong": levels.get("strong", 0), "medium": levels.get("medium", 0),
            # —— 用户口径的表头：各周期最大涨幅（走满才给） ——
            "avg_gain_7d": mean(group, 7, "max_gain_pct"),
            "avg_gain_20d": mean(group, 20, "max_gain_pct"),
            "avg_gain_60d": mean(group, 60, "max_gain_pct"),
            # —— 判断"赚不赚钱/稳不稳"的口径：实际收益、胜率与浮亏 ——
            "avg_ret_7d": mean(group, 7, "ret_pct"),
            "avg_ret_20d": avg_ret_20,
            "avg_ret_60d": mean(group, 60, "ret_pct"),
            "avg_ret_20d_now": avg_ret_20_now,
            "win_rate_20d": win_rate,
            "hit_rate_20d": share(group, 20, "current_gain_pct", above=5.0),
            "worst_20d": (round(min(values_of(group, 20, "worst_pct")), 2)
                          if values_of(group, 20, "worst_pct") else None),
            "done_20d": sum(1 for item in group
                            if item.windows.get(20) is not None
                            and item.windows[20].status == "done"),
            "done_60d": sum(1 for item in group
                            if item.windows.get(60) is not None
                            and item.windows[60].status == "done"),
            "avg_current": round(sum(current) / len(current), 2) if current else None,
            "quality": ("good" if (basis or 0) >= 5
                        else "ok" if (basis or 0) >= 0 else "poor"),
            "basis": "ret_20d" if avg_ret_20 is not None else "ret_20d_now",
            # —— 显示 / 隐藏判定（口径见函数文档） ——
            "gate": gate,
            # `pending` 也算"显示"：判不出胜率不等于胜率低于门槛。
            "passed": gate != "hidden",
            "leaders": leaders,
        })
    out.sort(key=lambda item: (
        item["avg_ret_20d"] if item["avg_ret_20d"] is not None
        else (item["avg_ret_20d_now"] if item["avg_ret_20d_now"] is not None else -999)),
        reverse=True)
    return out


# ==================================================================
# 组装
# ==================================================================


def build(alerts: Sequence[dict[str, Any]], store: Any, *,
          start: str = "", end: str = "",
          levels: Sequence[str] = DEFAULT_LEVELS,
          horizons: Sequence[int] = DEFAULT_HORIZONS,
          dedup_days: int = DEFAULT_DEDUP_DAYS,
          split: str = DEFAULT_SPLIT,
          leaders: bool = False,
          stock_window: int = DEFAULT_STOCK_WINDOW,
          top_leaders: int = DEFAULT_LEADERS,
          min_win_rate: float = DEFAULT_MIN_WIN_RATE,
          history_only: bool = False,
          excluded_boards: Collection[str] = (),
          progress: Any = None) -> dict[str, Any]:
    """组装整个面板的载荷（同步；调用方负责丢进线程 / 缓存）。

    `excluded_boards` 是**池级剔除清单**里的板块代码（`theme_gate` 按 20 日胜率
    生成，路由层从 config 读出来传进来）。它们在这个面板里**也不出现** ——
    否则"清单外的题材不显示"只在打分与告警上生效、报表里却还列着，
    用户只会认为这条规则没生效。

    刻意做成**入参**而不是在这里读配置文件：本模块至今不依赖 config，
    保持它是"给数据就算"的纯计算层，测试也就不用造配置文件。
    """
    def note(text: str) -> None:
        if callable(progress):
            progress(text)

    gaps: list[str] = []
    note("读取交易日历…")
    calendar = _trim_calendar(store.calendar(start, ""), start, end)
    if not calendar:
        return {"rows": [], "history": [], "recent": [], "boards": [],
                "gaps": ["本地没有交易日历数据"], "note": ""}

    note(f"算 {len(alerts)} 条告警的 {('/'.join(str(h) for h in horizons))} 日最大涨幅…")
    rows, row_gaps = build_rows(alerts, store, calendar=calendar,
                               horizons=horizons, dedup_days=dedup_days,
                               split=split)
    gaps.extend(row_gaps)
    # 「截至最新数据」以**板块指数**的最后一根K线为准：这个面板的每个数都从它算出来。
    # 交易日历可能比它多一天（日历已排到 20260924，而板块K线到 20260923）——
    # 若不截断，领涨股的窗口会比板块窗口多一天，同一行里两个 as-of 不一致。
    as_of = max((row.latest_date for row in rows if row.latest_date),
                default=calendar[-1])
    calendar = [day for day in calendar if day <= as_of]

    if leaders and rows:
        note(f"算 {len(rows)} 个信号周期的区间领涨成分股（约 5~10 秒）…")
        gaps.extend(attach_leaders(rows, store, calendar=calendar,
                                   stock_window=stock_window, top=top_leaders))
    elif rows and not leaders:
        gaps.append("领涨成分股列为空是因为本次没有请求它（`leaders=true` 才会计算）")

    boards = summarize_boards(rows, horizons=horizons, min_win_rate=min_win_rate)
    # 池级剔除的题材：连汇总行带逐条行一起摘掉（口径见 `build` 的 `excluded_boards`）
    dropped_boards = 0
    dropped_rows = 0
    if excluded_boards:
        drop = {str(code) for code in excluded_boards}
        kept = [item for item in boards if item["board_code"] not in drop]
        dropped_boards = len(boards) - len(kept)
        boards = kept
        kept_rows = [row for row in rows if row.board_code not in drop]
        dropped_rows = len(rows) - len(kept_rows)
        rows = kept_rows
    passed_boards = {item["board_code"] for item in boards if item["passed"]}
    hidden_boards = {item["board_code"] for item in boards if item["gate"] == "hidden"}
    pending_boards = {item["board_code"] for item in boards if item["gate"] == "pending"}
    if history_only:
        rows = [row for row in rows if row.board_code in passed_boards]

    # ⚠️ 载荷**只给一份 `rows`**，`history` / `recent` 不重复返回。
    #
    # 早先三个键都返回，于是 1305 行的面板要传 1305+1096+35 个行对象
    # （实测 3.1 MB，翻了一倍还多），而且三者**筛选口径并不一致**
    # （`rows` 未筛板块、`history` 已按 20 日胜率门槛筛过）——
    # 同一个载荷里两套筛选状态是最容易让前端显示错数的契约。
    # 现在：`rows` 是唯一事实来源，每行带 `segment`；
    # 前端按 `segment` 分视图、按 `boards[].passed` 做板块筛选（本地即时生效）。
    def stat_value(horizon: int, field: str) -> list[float]:
        out: list[float] = []
        for row in rows:
            stat = row.windows.get(horizon)
            value = getattr(stat, field, None) if stat is not None else None
            if value is not None:
                out.append(float(value))
        return out

    done_20 = stat_value(20, "max_gain_pct")
    ret_20 = stat_value(20, "ret_pct")
    now_20 = stat_value(20, "current_ret_pct")
    worst_20 = stat_value(20, "worst_pct")
    history_rows = [row for row in rows if row.segment == "history"]
    recent_rows = [row for row in rows if row.segment == "recent"]
    stats = {
        "signals": len(rows),
        "alert_total": sum(row.alert_count for row in rows),
        "folded": sum(row.alert_count - 1 for row in rows),
        "history": len(history_rows), "recent": len(recent_rows),
        # "不筛选就看不到多少" —— 前端用它显示"已按 20 日胜率 > 门槛挡掉 N 条"
        "history_hidden": sum(1 for row in history_rows
                              if row.board_code in hidden_boards),
        "win_rate_hidden": sum(1 for row in rows
                               if row.board_code in hidden_boards),
        "strong": sum(1 for row in rows if row.level == "strong"),
        "medium": sum(1 for row in rows if row.level == "medium"),
        "boards": len(boards),
        "boards_passed": len(passed_boards),
        "boards_hidden": len(hidden_boards),
        # 池级剔除（`theme_gate` 生成的那份清单）挡掉了多少 —— 与"胜率不达标"
        # 分开计数：前者是"这个题材已经决定不做了"，后者是"这次展示不达标"
        "pool_excluded_boards": dropped_boards,
        "pool_excluded_rows": dropped_rows,
        # 窗口一条都没走满、无从判定的板块（它们**不**被隐藏，见 summarize_boards）
        "boards_pending": len(pending_boards),
        "min_win_rate": float(min_win_rate),
        # 用户口径的表头：最大涨幅均值
        "avg_gain_20d": round(sum(done_20) / len(done_20), 2) if done_20 else None,
        # 判断赚不赚钱：实际收益与胜率
        "avg_ret_20d": round(sum(ret_20) / len(ret_20), 2) if ret_20 else None,
        "win_rate_20d": (round(sum(1 for value in ret_20 if value > 0) / len(ret_20), 4)
                         if ret_20 else None),
        "win_rate_20d_now": (round(sum(1 for value in now_20 if value > 0) / len(now_20), 4)
                             if now_20 else None),
        "worst_20d": round(min(worst_20), 2) if worst_20 else None,
        "partial_60d": sum(1 for row in rows
                           if row.windows.get(60) is not None
                           and row.windows[60].status != "done"),
        "last_signal_date": rows[0].signal_date if rows else "",
    }
    # ⚠️ `stats` 里的均值/胜率覆盖**全部**行（含被隐藏的板块）：它是"这套信号整体
    # 怎么样"的口径，不是"筛选后这张表怎么样"。前端必须把这个范围写在提示里，
    # 否则卡片上的胜率会和表里每一行都对不上（表里只剩 ≥ 门槛的板块）。
    return {
        "as_of": as_of,
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(
            timespec="seconds"),
        "horizons": [int(item) for item in horizons],
        "dedup_days": int(dedup_days), "split": split,
        "stock_window": int(stock_window),
        "levels": list(levels),
        "min_win_rate": float(min_win_rate),
        "history_only": bool(history_only),
        "leaders_computed": bool(leaders),
        "rows": [row.to_dict() for row in rows],
        "boards": boards,
        "stats": stats,
        "gaps": gaps,
        "note": ("「最大涨幅」= 信号日收盘 → 窗口内最高价的最大偏离（理论可捕获"
                 "空间，未扣择时/滑点/涨停买不进）；「实际收益」= 窗口末收盘，"
                 "「最多浮亏」= 窗口内最低价 —— 判断板块亏不亏钱看这两个。"
                 "窗口没走满的周期不填最终值，只在「至今」列给出截至最新数据的进度。"
                 f"板块筛选口径：20 日胜率 > {min_win_rate:.0%}"
                 "（已走满的 20 日窗口里实际收益 > 0 的比例）——不到这个数的板块不出，"
                 "恰好等于门槛的也不出。"
                 + (f"另有 {dropped_boards} 个题材已在**池级剔除清单**里"
                    f"（按历史 20 日胜率生成，见 configs/ 下的题材剔除台账），"
                    f"它们的 {dropped_rows} 条历史信号在本面板也不显示。"
                    if dropped_boards else "")),
    }


#: 进程内结果缓存：`{key: (写入时刻, payload)}`。
#:
#: 为什么必须有：算领涨股要 5~6 秒、走 15 GiB 行情仓。这是**按天变化**的数据，
#: 完全没有必要每次打开页签都重算一遍。TTL 与面板轮询周期无关（收益追踪不需要
#: 分钟级新鲜度），取 10 分钟；`refresh=true` 可绕过。
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL_SECONDS = 600.0


def cache_get(key: str, ttl: float = CACHE_TTL_SECONDS) -> dict[str, Any] | None:
    import time
    with _CACHE_LOCK:
        slot = _CACHE.get(key)
    if slot is None or time.monotonic() - slot[0] >= ttl:
        return None
    return slot[1]


def cache_put(key: str, payload: dict[str, Any]) -> None:
    import time
    with _CACHE_LOCK:
        if len(_CACHE) > 24:          # 键里带参数，防参数枚举把内存撑满
            _CACHE.clear()
        _CACHE[key] = (time.monotonic(), payload)


def cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = ["AlertReturnRow", "LeaderStock", "WindowStat", "attach_leaders",
           "build", "build_rows", "cache_clear", "cache_get", "cache_put",
           "cluster_alerts", "summarize_boards", "CACHE_TTL_SECONDS",
           "DEFAULT_DEDUP_DAYS", "DEFAULT_HORIZONS", "DEFAULT_LEADERS",
           "DEFAULT_LEVELS", "DEFAULT_MIN_WIN_RATE", "DEFAULT_SPLIT",
           "DEFAULT_STOCK_WINDOW"]
