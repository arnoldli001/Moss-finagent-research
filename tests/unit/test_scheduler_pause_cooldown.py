"""暂停冷却期（`RunLog.is_paused` 的时间维度）的判据。

## 为什么这一组必须存在（2026-10-30 真实事故）

用户问「只要开机，定期自动刷新的脚本是否按预期执行了？」时，实测答案里有
一条最难解释的事实：

> `mainline_daily` 在 **2026-10-12** 连续第三次 failed 之后被调度器自动暂停，
> 一直到 **2026-10-30**（**18 天**）都没有再被尝试过一次。

中间发生了什么并不重要 —— **根因早就修好了、机器重启过十几次、进程换过好几茬**，
但"暂停"这个状态只看"最后 N 次是不是全失败"，**没有时间维度**，于是它永远暂停。
一个"防止每 30 分钟撞同一堵墙"的保护机制，退化成了"永久停机"。

修复是给暂停加**冷却期**（`PAUSE_COOLDOWN_DAYS = 7`）：超过 7 天自动放行一次。
这样既保住原意（不狂撞），又给出"自己长回来"的机会。

下面的用例把**边界**钉死，因为这是"时间"相关的判据，最容易写成
"今天绿、下周红"的形状：所有时间戳都**相对 `now` 生成**。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.scheduler.registry import (
    PAUSE_AFTER_CONSECUTIVE_FAILURES, PAUSE_COOLDOWN_DAYS,
)
from src.scheduler.run_log import RunLog


def _log(tmp_path: Path) -> RunLog:
    return RunLog(tmp_path / "runs.jsonl")


def _stamp(days_ago: float) -> str:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat()


def _write(log: RunLog, job: str, statuses: list[str], *, days_ago: float) -> None:
    """直接追加成品记录（`finish` 会覆盖 end_time，这里要指定历史时刻）。"""
    for status in statuses:
        log.finish({"run_id": "x", "job_name": job,
                    "start_time": _stamp(days_ago), "retries": 0},
                   status=status, records_processed=1)
    # `finish` 会把 end_time 写成**现在** —— 判据读的是 `end_time or start_time`，
    # 所以要把文件里刚写的这几条的 end_time 改成历史时刻（否则冷却期永远不过期，
    # 测试就永远绿 —— 那正是"假绿"）。
    path = log._path
    lines = path.read_text(encoding="utf-8").splitlines()
    fixed = []
    for line in lines:
        record = __import__("json").loads(line)
        if record["job_name"] == job:
            record["end_time"] = _stamp(days_ago)
        fixed.append(__import__("json").dumps(record, ensure_ascii=False))
    path.write_text("\n".join(fixed) + "\n", encoding="utf-8", newline="")


def test_recent_failures_pause_the_job(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _write(log, "mainline_daily", ["failed"] * PAUSE_AFTER_CONSECUTIVE_FAILURES,
           days_ago=0)
    assert log.is_paused("mainline_daily") is True


def test_pause_expires_after_cooldown(tmp_path: Path) -> None:
    """核心用例：18 天前失败三次 ⇒ **不再**暂停（当年那次事故的形状）。"""
    log = _log(tmp_path)
    _write(log, "mainline_daily", ["failed"] * PAUSE_AFTER_CONSECUTIVE_FAILURES,
           days_ago=PAUSE_COOLDOWN_DAYS + 11)
    assert log.is_paused("mainline_daily") is False


def test_pause_not_expired_just_before_cooldown(tmp_path: Path) -> None:
    """边界：冷却期还差一天 ⇒ 仍暂停（不要提前放行）。"""
    log = _log(tmp_path)
    _write(log, "mainline_daily", ["failed"] * PAUSE_AFTER_CONSECUTIVE_FAILURES,
           days_ago=PAUSE_COOLDOWN_DAYS - 1)
    assert log.is_paused("mainline_daily") is True


def test_success_breaks_the_streak_within_cooldown(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _write(log, "x", ["failed", "failed", "success", "failed"], days_ago=0)
    assert log.is_paused("x") is False


def test_paused_since_reports_the_last_failure(tmp_path: Path) -> None:
    """`paused_since` 与 `is_paused` 必须同源：一个说"暂停"、一个给得出时间。"""
    log = _log(tmp_path)
    _write(log, "x", ["failed"] * PAUSE_AFTER_CONSECUTIVE_FAILURES, days_ago=2)
    assert log.is_paused("x") is True
    assert log.paused_since("x") is not None


def test_paused_since_is_none_when_not_paused(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _write(log, "x", ["success"], days_ago=0)
    assert log.paused_since("x") is None
    assert log.is_paused("x") is False


def test_cooldown_is_at_least_one_week() -> None:
    """冷却期不能太短：短于数据作业的周频周期就等于"没有暂停"。"""
    assert PAUSE_COOLDOWN_DAYS >= 7
