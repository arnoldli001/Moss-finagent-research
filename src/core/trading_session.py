"""A 股交易时段边界的唯一权威定义（北京时间）。

## 为什么需要这个模块

交易时段边界（09:15 / 09:30 / 11:30 / 13:00 / 15:00）此前以 `9 * 60 + 15`
这类算术字面量散落在 5 个以上模块里：`intraday/service.py`、`intraday/market.py`、
`intraday/indicators.py`、`intraday/daily.py`、`fundflow/service.py`、
`fundflow/provider.py`。其中 `fundflow/service.py::session_state` 与
`intraday/service.py::session_state` 是**逐行相同**的两份实现，前者的 docstring
还写着「与做T模块同口径」—— 靠注释维持的口径一致，改一处漏一处就会让两个页面
对同一时刻显示不同状态。

`fundflow/provider.py` 用 `9 * 60 + 15 <= minutes < 9 * 60 + 30` 这类链式比较，
而 `service.py` 用 `if minutes < ...` 逐级 fallthrough —— 两种写法在边界分钟
（恰好 11:30、恰好 15:00）上的取值必须人工核对才能确认一致。

## 口径约定

本模块统一用「自 00:00 起的分钟数」表达边界，且**边界取闭区间**：

- 09:15 起为集合竞价（含 09:15 那一分钟）
- 09:30 起为连续竞价
- 11:30 为上午最后一分钟（含）
- 13:00 为下午第一分钟（含）
- 15:00 为全天最后一分钟（含）

`in_session(minutes)` 是判定连续竞价的唯一入口，含午休排除。
"""

from __future__ import annotations

from datetime import datetime, time

# ==================================================================
# 时段边界（自 00:00 起的分钟数）
# ==================================================================

#: 集合竞价开始 09:15
CALL_AUCTION_START = 9 * 60 + 15
#: 集合竞价撮合 09:25（开盘价在这一分钟定出，竞价选股以此为最晚触发点）
CALL_AUCTION_MATCH = 9 * 60 + 25
#: `CALL_AUCTION_MATCH` 的 `time` 形式，供配置解析失败时兜底
CALL_AUCTION_TIME = time(9, 25, 0)
#: 连续竞价开始 / 上午开盘 09:30
MORNING_OPEN = 9 * 60 + 30
#: 上午收盘 11:30（上午最后一分钟）
MORNING_CLOSE = 11 * 60 + 30
#: 下午开盘 13:00
AFTERNOON_OPEN = 13 * 60
#: 全天收盘 15:00（全天最后一分钟）
AFTERNOON_CLOSE = 15 * 60
#: 盘后固定触发点 15:05：日终数据这时才算落定
POST_CLOSE = 15 * 60 + 5

#: 连续竞价时段（不含集合竞价与午休）
MORNING_SESSION = (MORNING_OPEN, MORNING_CLOSE)
AFTERNOON_SESSION = (AFTERNOON_OPEN, AFTERNOON_CLOSE)
#: 需要盯盘刷新的时段：集合竞价起（09:15）到收盘，中间午休靠 `in_session` 排除
WATCH_WINDOWS = ((CALL_AUCTION_START, MORNING_CLOSE), (AFTERNOON_OPEN, AFTERNOON_CLOSE))

#: 全天连续竞价总分钟数（120 + 120 = 240），用于「已交易时间占比」折算
TRADING_MINUTES = (
    MORNING_CLOSE - MORNING_OPEN
) + (
    AFTERNOON_CLOSE - AFTERNOON_OPEN
)

#: 时段状态码 → 中文标签。前端措辞的唯一来源。
SESSION_LABELS: dict[str, str] = {
    "pre_open": "盘前",
    "call_auction": "集合竞价",
    "trading": "交易中",
    "lunch_break": "午间休市",
    "closed": "已收盘",
    "weekend": "周末休市",
}


def minutes_of(moment: datetime) -> int:
    """`datetime` → 自当日 00:00 起的分钟数（秒被截断）。"""
    return moment.hour * 60 + moment.minute


def parse_hhmm(text: str) -> int | None:
    """解析 `HH:MM` 或 `HH:MM:SS` → 分钟数；无法解析返回 `None`。

    调度参数里这两种写法混用过，直接做字符串比较会在整点上出错：
    `"14:45" >= "14:45:00"` 为假（短串是长串的前缀），于是 14:45 那一分钟
    被判成「还没到窗口」而跳过。统一转成分钟数再比就没有这个陷阱。
    """
    parts = str(text).strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def parse_time_of_day(text: str, *, default: time | None = None) -> time | None:
    """解析 `HH:MM[:SS]` → `datetime.time`；无法解析返回 `default`。"""
    parts = str(text).strip().split(":")
    if len(parts) not in (2, 3):
        return default
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return default
    try:
        return time(hour, minute, second)
    except ValueError:
        return default


def in_window(minutes: int, window: tuple[int, int]) -> bool:
    """分钟数是否落在 `[start, end]` 闭区间窗口内。"""
    return window[0] <= minutes <= window[1]


def in_session(minutes: int) -> bool:
    """是否处于连续竞价（不含集合竞价、不含午休、不含盘后）。"""
    return in_window(minutes, MORNING_SESSION) or in_window(minutes, AFTERNOON_SESSION)


def session_state(now: datetime | None = None) -> tuple[str, str]:
    """当前交易时段 → `(状态码, 中文标签)`。

    周末先判，其次按 `CALL_AUCTION_START` 逐级 fallthrough。边界取闭区间，
    与 `WATCH_WINDOWS` / `in_session` 同一套常量，改一处全站一致。
    """
    moment = now or datetime.now()
    if moment.weekday() >= 5:
        return "closed", SESSION_LABELS["weekend"]
    minutes = minutes_of(moment)
    if minutes < CALL_AUCTION_START:
        return "pre_open", SESSION_LABELS["pre_open"]
    if minutes < MORNING_OPEN:
        return "call_auction", SESSION_LABELS["call_auction"]
    if minutes <= MORNING_CLOSE:
        return "trading", SESSION_LABELS["trading"]
    if minutes < AFTERNOON_OPEN:
        return "lunch_break", SESSION_LABELS["lunch_break"]
    if minutes <= AFTERNOON_CLOSE:
        return "trading", SESSION_LABELS["trading"]
    return "closed", SESSION_LABELS["closed"]


def elapsed_session_ratio(now: datetime | None = None) -> float:
    """当前时点「已交易时间」占全天比例（0~1），用于折算全天预测量。

    非交易日/盘前返回 0（调用方据此跳过预测），收盘后返回 1.0。
    """
    moment = now or datetime.now()
    if moment.weekday() >= 5:
        return 0.0
    minutes = minutes_of(moment)
    if minutes < MORNING_OPEN:
        return 0.0
    if minutes <= MORNING_CLOSE:
        done = minutes - MORNING_OPEN
    elif minutes < AFTERNOON_OPEN:
        done = MORNING_CLOSE - MORNING_OPEN  # 午休：已完成上午全部
    elif minutes <= AFTERNOON_CLOSE:
        done = (MORNING_CLOSE - MORNING_OPEN) + (minutes - AFTERNOON_OPEN)
    else:
        done = TRADING_MINUTES
    return min(max(done / TRADING_MINUTES, 0.0), 1.0)


def session_offset(minutes: int) -> int:
    """当日分钟数 → **交易分钟序号**（0 起，跳过午休），越界则夹到两端。

    夹取语义是给分时图 x 轴用的：让 11:30 与 13:00 相邻而不留空隙，
    集合竞价点归到 0、盘后点归到全天末位，轴不会因为越界点被拉伸。
    """
    if minutes < MORNING_OPEN:
        return 0
    if minutes <= MORNING_CLOSE:
        return minutes - MORNING_OPEN
    if minutes < AFTERNOON_OPEN:
        return MORNING_CLOSE - MORNING_OPEN
    if minutes <= AFTERNOON_CLOSE:
        return (MORNING_CLOSE - MORNING_OPEN) + (minutes - AFTERNOON_OPEN)
    return TRADING_MINUTES
