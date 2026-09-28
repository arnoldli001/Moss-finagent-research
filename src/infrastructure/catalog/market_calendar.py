"""A 股交易时段判定（freshness 语义修正专用）。

## 为什么需要它（2026-09-28 实测暴露的缺陷）

原 `FreshnessState.is_fresh()` 只看 `now - last_fetch_time <= freshness_hours`。
对 `realtime` 指标（`freshness_hours = 0.5`）这意味着：

    · 今天 09:30 采过一次 → 10:01 就判 stale
    · 今天 15:00 收盘后采过一次 → 15:31 就判 stale
    · **周末/节假日照样判 stale** —— 但市场根本没开，数据一个字都不会变

后果（实测）：用户问"今天没开盘，库里明明有最新数据"，
SmartFetcher 仍判 stale → 走网络 → `mkt:cybkcb:spot_summary` 白等 **13.1 秒**。

## 修正语义

对 `realtime` / `intraday` 两类指标：
  · **盘中**（交易日 09:30-11:30 / 13:00-15:00）→ 按 `freshness_hours` 判
  · **收盘后 / 非交易日** → 只要数据日期 == **最近一个交易日**，就一直算 fresh，
    直到下一个交易时段开始

这样"今天没开盘"时，库里的数据就是最新的，**不再联网**。

## 边界与诚实说明

`is_trading_day()` 复用 `src/intraday/auto_select.py` 的实现（周末 + 市场时钟），
**节假日判定依赖市场时钟**（`live_session_date()`）。时钟不可用时它按"交易日"处理
—— 那是**保守**方向（多跑一轮网络，不会给出过期数据当新鲜数据）。

本模块不做更复杂的节假日表：A 股调休每年变，硬编码的表必然过期，
而"时钟取不到就按交易日"已经保证不会把旧数据当新数据用。
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime

#: 上午/下午交易时段（北京时间，A 股）
MORNING_OPEN = dtime(9, 30)
MORNING_CLOSE = dtime(11, 30)
AFTERNOON_OPEN = dtime(13, 0)
AFTERNOON_CLOSE = dtime(15, 0)

#: 属于"盘中会变"的频率（收盘后即冻结）
LIVE_FREQUENCIES: frozenset[str] = frozenset({"realtime", "intraday"})


def is_trading_day(moment: datetime | None = None) -> bool:
    """是否为交易日（周末 + 市场时钟排除节假日）。

    ⚠️ **只对"今天"有意义** —— 市场时钟 `live_session_date()` 回答的是
    "当前这个自然日是不是交易日"，拿它去判 2026-09-28 这样的历史/未来日期
    会得到错误的 False。

    复用 `src.intraday.auto_select.is_trading_day` —— **同一判断只允许一份实现**
    （AGENTS.md 硬约束）。这里只做转发，避免两处判定分叉。

    用途：**调度作业**（"今天该不该跑"）。
    不要用它做 freshness 判定 —— 那条路径要用 `is_market_open`（纯确定性）。
    """
    try:
        from src.intraday.auto_select import is_trading_day as _impl

        return _impl(moment)
    except Exception:  # noqa: BLE001 导入/时钟异常 → 保守按交易日处理
        now = moment or datetime.now()
        return now.weekday() < 5


def is_market_open(moment: datetime | None = None) -> bool:
    """当前是否处于**连续竞价时段**（交易日 09:30-11:30 / 13:00-15:00）。

    ## ★ 只用"星期 + 时刻"判定，**不查市场时钟**（2026-09-28 修正）

    原实现调了带时钟的 `is_trading_day`，导致两个问题：
      1. **不可测** —— 任何非"今天"的日期都被判非交易日（测试直接红）
      2. **语义错位** —— `live_session_date()` 回答的是"今天是不是交易日"，
         不是"这个 datetime 是不是交易日"

    现在改成纯确定性判定。**节假日的处理方向是安全的**：
    节假日若恰逢工作日，本函数会误判"盘中" → `session_frozen` 返回 False
    → 不给 freshness 豁免 → 老老实实联网（保守，不会把旧数据当新数据）。

    集合竞价（09:15-09:25）不计入 —— 此期间快照仍在变，
    按"非盘中"处理会**多取一次**，不会漏更新。

    用途：**freshness 判定**（"能不能跳过联网"）。
    """
    now = moment or datetime.now()
    if now.weekday() >= 5:
        return False          # 周末确定不开盘
    t = now.time()
    return (MORNING_OPEN <= t <= MORNING_CLOSE) or (
        AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE)


def last_trading_day(moment: datetime | None = None) -> date | None:
    """最近一个交易日（含今天，若今天是交易日）。

    ## ★ 只用"星期"判定，**不查市场时钟**（与 `is_market_open` 同一理由）

    向前最多回溯 10 天（覆盖春节/国庆长假）—— 找不到返回 None。

    **节假日的方向性**：工作日节假日会被误判为交易日，
    于是"最近交易日"= 今天，而 `last_period_date` 是上一个真交易日
    → 两者不等 → **不给豁免 → 联网**（保守 ✅）。
    """
    from datetime import timedelta

    now = moment or datetime.now()
    for back in range(0, 11):
        cand = now - timedelta(days=back)
        if cand.weekday() < 5:
            return cand.date()
    return None


def session_frozen(moment: datetime | None = None) -> bool:
    """行情是否处于"已冻结"状态（非盘中）—— 收盘后 / 周末。

    冻结时：`realtime` / `intraday` 指标只要数据日期是最近交易日，
    就不再判 stale（因为下一个交易时段之前它不会变）。
    """
    return not is_market_open(moment)


__all__ = [
    "AFTERNOON_CLOSE",
    "AFTERNOON_OPEN",
    "LIVE_FREQUENCIES",
    "MORNING_CLOSE",
    "MORNING_OPEN",
    "is_market_open",
    "is_trading_day",
    "last_trading_day",
    "session_frozen",
]
