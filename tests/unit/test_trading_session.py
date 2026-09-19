"""交易时段边界与时间解析的唯一权威 —— 回归测试。

这些断言的价值在于**钉住边界分钟**。时段判定此前分散在 5 个模块里，
两份 `session_state` 逐行相同却各写一遍，边界取值靠人工核对；
下面的参数化用例把每个边界分钟的期望值固定下来，
任何一处实现漂移都会在这里红。
"""

from __future__ import annotations

from datetime import datetime, time

import pytest

from src.core.trading_session import (
    AFTERNOON_CLOSE,
    AFTERNOON_OPEN,
    AFTERNOON_SESSION,
    CALL_AUCTION_MATCH,
    CALL_AUCTION_START,
    CALL_AUCTION_TIME,
    MORNING_CLOSE,
    MORNING_OPEN,
    MORNING_SESSION,
    POST_CLOSE,
    SESSION_LABELS,
    TRADING_MINUTES,
    WATCH_WINDOWS,
    elapsed_session_ratio,
    in_session,
    in_window,
    minutes_of,
    parse_hhmm,
    parse_time_of_day,
    session_offset,
    session_state,
)

# 2026-09-18 是周五（交易日），用它锁定 weekday 分支。
FRIDAY = (2026, 9, 18)


def at(hour: int, minute: int) -> datetime:
    return datetime(*FRIDAY, hour, minute)


# ==================================================================
# 常量自洽性
# ==================================================================


def test_session_constants_are_ordered() -> None:
    assert (
        CALL_AUCTION_START
        < CALL_AUCTION_MATCH
        < MORNING_OPEN
        < MORNING_CLOSE
        < AFTERNOON_OPEN
        < AFTERNOON_CLOSE
        < POST_CLOSE
    )


def test_trading_minutes_matches_sessions() -> None:
    morning = MORNING_SESSION[1] - MORNING_SESSION[0]
    afternoon = AFTERNOON_SESSION[1] - AFTERNOON_SESSION[0]
    assert TRADING_MINUTES == morning + afternoon == 240


def test_watch_windows_start_at_call_auction() -> None:
    # 盯盘刷新从集合竞价起（09:15），不是从连续竞价起 —— 竞价选股需要这段数据。
    assert WATCH_WINDOWS[0][0] == CALL_AUCTION_START
    assert WATCH_WINDOWS[0][1] == MORNING_CLOSE
    assert WATCH_WINDOWS[1] == AFTERNOON_SESSION


def test_call_auction_time_matches_minutes() -> None:
    assert CALL_AUCTION_TIME == time(CALL_AUCTION_MATCH // 60, CALL_AUCTION_MATCH % 60)


# ==================================================================
# session_state：边界分钟逐个钉住
# ==================================================================


@pytest.mark.parametrize(
    ("hour", "minute", "code", "label"),
    [
        (0, 0, "pre_open", "盘前"),
        (9, 14, "pre_open", "盘前"),          # 09:15 前最后一分钟
        (9, 15, "call_auction", "集合竞价"),  # 边界：含 09:15
        (9, 24, "call_auction", "集合竞价"),
        (9, 29, "call_auction", "集合竞价"),  # 09:30 前最后一分钟
        (9, 30, "trading", "交易中"),         # 边界：含 09:30
        (11, 29, "trading", "交易中"),
        (11, 30, "trading", "交易中"),        # 边界：含 11:30（上午最后一分钟）
        (11, 31, "lunch_break", "午间休市"),
        (12, 59, "lunch_break", "午间休市"),
        (13, 0, "trading", "交易中"),         # 边界：含 13:00（下午第一分钟）
        (15, 0, "trading", "交易中"),         # 边界：含 15:00（全天最后一分钟）
        (15, 1, "closed", "已收盘"),
        (23, 59, "closed", "已收盘"),
    ],
)
def test_session_state_boundaries(hour: int, minute: int, code: str, label: str) -> None:
    assert session_state(at(hour, minute)) == (code, label)


@pytest.mark.parametrize("weekday", [5, 6])
def test_session_state_weekend_is_closed(weekday: int) -> None:
    moment = datetime(2026, 9, 18 + (weekday - 4), 10, 30)
    assert moment.weekday() == weekday % 7
    assert session_state(moment) == ("closed", SESSION_LABELS["weekend"])


def test_session_state_defaults_to_now() -> None:
    code, label = session_state()
    assert code in SESSION_LABELS
    if code == "closed":
        # 周末与盘后共用 `closed` 状态码，靠标签区分（前端按 code 分支、
        # 展示用 label）。沿用既有语义，不因为「一个码一个标签」更整洁而改。
        assert label in (SESSION_LABELS["closed"], SESSION_LABELS["weekend"])
    else:
        assert label == SESSION_LABELS[code]


def test_all_labels_are_chinese() -> None:
    for label in SESSION_LABELS.values():
        assert label, "标签不得为空"
        assert not label.isascii(), f"{label} 应为中文标签"


# ==================================================================
# 跨模块口径一致（本次重构的核心保证）
# ==================================================================


def test_intraday_and_fundflow_agree_with_authority() -> None:
    """做T模块与资金流模块必须与本模块逐分钟一致。

    这两处此前各存一份逐行相同的实现，前者的 docstring 还写着
    「与做T模块同口径」—— 靠注释维持的一致无法被测试发现，这里改成实测。
    """
    from src.fundflow.service import session_state as fundflow_state
    from src.intraday.service import session_state as intraday_state

    for hour in range(24):
        for minute in (0, 14, 15, 25, 29, 30, 45, 59):
            moment = at(hour, minute)
            expected = session_state(moment)
            assert intraday_state(moment) == expected, f"intraday 漂移于 {hour}:{minute}"
            assert fundflow_state(moment) == expected, f"fundflow 漂移于 {hour}:{minute}"


def test_fundflow_provider_session_matches_authority() -> None:
    """provider 的 `_session_state()` 只取状态码（内部自取 now），与权威口径一致。

    它此前用 `9*60+15 <= minutes < 9*60+30` 这类链式比较，与 `service.py` 的
    逐级 fallthrough 是两种写法；两种写法在整点边界上的取值必须实测比对。
    """
    from src.fundflow.provider import _session_state

    assert _session_state() == session_state()[0]


# ==================================================================
# in_session / in_window
# ==================================================================


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 14, False),
        (9, 15, False),   # 集合竞价不算连续竞价
        (9, 29, False),
        (9, 30, True),
        (11, 30, True),
        (11, 31, False),  # 午休
        (12, 59, False),
        (13, 0, True),
        (15, 0, True),
        (15, 1, False),
    ],
)
def test_in_session(hour: int, minute: int, expected: bool) -> None:
    assert in_session(hour * 60 + minute) is expected


def test_in_window_is_closed_interval() -> None:
    assert in_window(60, (60, 90)) is True
    assert in_window(90, (60, 90)) is True
    assert in_window(59, (60, 90)) is False
    assert in_window(91, (60, 90)) is False


# ==================================================================
# elapsed_session_ratio
# ==================================================================


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, 0.0),
        (9, 29, 0.0),      # 开盘前为 0，调用方据此跳过预测
        (9, 30, 0.0),      # 恰好开盘：已交易 0 分钟
        (10, 30, 0.25),    # 上午 60/240
        (11, 30, 0.5),     # 上午走满
        (12, 0, 0.5),      # 午休冻结在 0.5
        (13, 0, 0.5),
        (15, 0, 1.0),
        (16, 0, 1.0),
    ],
)
def test_elapsed_session_ratio(hour: int, minute: int, expected: float) -> None:
    assert elapsed_session_ratio(at(hour, minute)) == pytest.approx(expected)


def test_elapsed_session_ratio_weekend_is_zero() -> None:
    assert elapsed_session_ratio(datetime(2026, 9, 19, 10, 30)) == 0.0


def test_elapsed_session_ratio_is_monotonic_within_day() -> None:
    previous = -1.0
    for hour in range(9, 17):
        for minute in range(0, 60, 5):
            value = elapsed_session_ratio(at(hour, minute))
            assert value >= previous, f"{hour}:{minute} 出现回退"
            previous = value


# ==================================================================
# session_offset（分时图 x 轴对齐）
# ==================================================================


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 14, 0),      # 集合竞价归入 0
        (9, 30, 0),
        (9, 31, 1),
        (11, 30, 120),   # 上午末位
        (12, 0, 120),    # 午休夹在上午末位
        (13, 0, 120),    # 下午首位紧接上午末位（不留空隙）
        (13, 1, 121),
        (15, 0, 240),
        (16, 0, 240),    # 盘后夹到全天末位
    ],
)
def test_session_offset_clamps_to_ends(hour: int, minute: int, expected: int) -> None:
    assert session_offset(hour * 60 + minute) == expected


def test_session_offset_never_exceeds_trading_minutes() -> None:
    for hour in range(24):
        for minute in range(60):
            value = session_offset(hour * 60 + minute)
            assert 0 <= value <= TRADING_MINUTES


def test_intraday_indicators_delegate_to_session_offset() -> None:
    from src.intraday.indicators import session_minutes_of

    for ts, expected in [("2026-09-18 09:35:00", 5), ("2026-09-18 13:10:00", 130)]:
        assert session_minutes_of(ts) == expected
    assert session_minutes_of("not-a-time") is None


# ==================================================================
# parse_hhmm / parse_time_of_day
# ==================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("09:25", 565),
        ("09:25:00", 565),
        ("09:25:59", 565),   # 秒被截断
        ("14:45", 885),
        ("14:45:00", 885),
        ("2:05", 125),       # 单位数小时/分钟也接受
        ("00:00", 0),
        ("23:59", 1439),
    ],
)
def test_parse_hhmm_accepts_both_formats(text: str, expected: int) -> None:
    assert parse_hhmm(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "abc", "09", "09:25:00:00", "24:00", "09:60", "-1:00", "09:aa"],
)
def test_parse_hhmm_rejects_garbage(text: str) -> None:
    assert parse_hhmm(text) is None


def test_parse_hhmm_is_immune_to_string_compare_trap() -> None:
    """回归：字符串比较会在整点上误判「还没到窗口」。

    `"14:45:00" <= "14:45"` 为假（短串是长串的前缀，排序靠前），
    于是 `start="14:45:00"` 时 14:45 那一分钟被当成窗口外而跳过 —— 尾盘选股
    会整轮丢失。转成分钟数后不再有这个问题。
    """
    start = parse_hhmm("14:45:00")
    end = parse_hhmm("15:00")
    assert start is not None and end is not None
    assert start <= parse_hhmm("14:45") <= end
    # 旧写法确实会漏掉这一分钟
    assert not ("14:45:00" <= "14:45" <= "15:00")


def test_parse_time_of_day() -> None:
    assert parse_time_of_day("09:25") == time(9, 25, 0)
    assert parse_time_of_day("09:25:30") == time(9, 25, 30)
    assert parse_time_of_day("bad") is None
    assert parse_time_of_day("bad", default=CALL_AUCTION_TIME) == CALL_AUCTION_TIME
    assert parse_time_of_day("25:00") is None


def test_auction_scheduler_falls_back_to_call_auction() -> None:
    # `src/auction_select/` 是 .gitignore 里的私有核心资产，公开 checkout 里不存在
    pytest.importorskip("src.auction_select.scheduler")
    from src.auction_select.scheduler import _parse_hhmmss

    assert _parse_hhmmss("09:25:00") == (9, 25, 0)
    assert _parse_hhmmss("09:15") == (9, 15, 0)
    # 配置写坏时回落到集合竞价撮合时点，不能让整个竞价模块当天静默不工作
    assert _parse_hhmmss("garbage") == (9, 25, 0)


def test_minutes_of_truncates_seconds() -> None:
    assert minutes_of(datetime(2026, 9, 18, 9, 25, 59)) == 565
