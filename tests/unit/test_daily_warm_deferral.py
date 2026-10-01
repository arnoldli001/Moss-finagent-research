"""日K预热的「失败降级」护栏（`CHG-0145`）。

## 它修的是 pilot 实测的那个形态

慢实例上**总有同一批票每轮都撞单只超时**（跨轮次重复出现：
`688825 / 300308 / 603083 / 600667 / 301511 …`），每只都要吃掉一次 12 s
⇒ 90 s 预算里约 1/4 被"注定失败的票"占掉，整轮只热了 **22~37 / 56**，
**健康的多数被少数拖累**。

处置：失败的票**降到队尾**（不剔除）—— 预算按顺序截断，于是预算先给健康的票；
窗口过后（`FAILURE_DEFER_SEC`）自动回到常规优先级。

⚠️ 本文件用 autouse 夹具**每个用例重置**模块级降级表：它是进程内状态，
不重置会让用例互相污染（本项目"测试隔离泄漏"踩过）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

import src.intraday.warm as warm_module
from src.intraday.warm import (
    FAILURE_DEFER_SEC,
    deferred_codes,
    note_failure,
    note_success,
    warm_targets,
)

WINDOW = datetime(2026, 9, 28, 10, 0)


class _Watch:
    def __init__(self, code: str) -> None:
        self.code = code


class _FakeConfig:
    def __init__(self, codes: list[str]) -> None:
        self.watchlist = [_Watch(code) for code in codes]


class _FakeService:
    def __init__(self, codes: list[str]) -> None:
        self.config = _FakeConfig(codes)

    def recent_daily_codes(self, limit: int | None = None) -> list[str]:
        return []


@pytest.fixture(autouse=True)
def _clean_deferral_table():
    """每个用例都从"没人被降级"开始（模块级状态必须显式隔离）。"""
    warm_module._recent_failures.clear()  # noqa: SLF001
    yield
    warm_module._recent_failures.clear()  # noqa: SLF001


def test_failed_code_moves_to_the_tail_next_round() -> None:
    """★ 上一轮超时的票，下一轮**排在最后**（预算先给健康的票）。"""
    service = _FakeService(["600036", "000001", "300308", "600519"])
    note_failure("000001")

    targets, _watch_count = warm_targets(service)
    assert targets == ["600036", "300308", "600519", "000001"], targets


def test_deferral_keeps_the_code_it_does_not_drop_it() -> None:
    """降级 ≠ 剔除：票仍在目标里（只是排队尾），否则它会**永久**冷。"""
    service = _FakeService(["600036", "000001"])
    note_failure("000001")
    targets, _ = warm_targets(service)
    assert "000001" in targets and len(targets) == 2


def test_deferral_expires_after_the_window() -> None:
    """窗口过后自动回到常规优先级（上游抖动是常见的，不能一直罚它）。"""
    service = _FakeService(["600036", "000001"])
    later = 10_000.0
    note_failure("000001", now=later)

    assert deferred_codes(now=later + 1.0) == {"000001"}
    assert deferred_codes(now=later + FAILURE_DEFER_SEC + 1.0) == set()

    targets, _ = warm_targets(service)
    assert targets == ["600036", "000001"], "过窗口后顺序应恢复"


def test_success_clears_the_deferral_immediately() -> None:
    """热成功 ⇒ 立刻恢复常规优先级（不必等窗口）。"""
    service = _FakeService(["600036", "000001"])
    note_failure("000001")
    assert deferred_codes() == {"000001"}

    note_success("000001")
    assert deferred_codes() == set()
    targets, _ = warm_targets(service)
    assert targets == ["600036", "000001"]


def test_deferral_table_only_keeps_entries_inside_the_window() -> None:
    """降级表是进程内字典 ⇒ 不变量是「**只留窗口内的**」，否则长跑就是内存泄漏。

    ⚠️ 第一版判据写错了（把 200 条按 1 s 一条插进去，然后断言"少于 200 条"）——
    它们本来就全在 900 s 窗口内，**不该**被清。判据要对着不变量写，
    而不是对着"我以为会发生的清理"写。
    """
    base = 1_000.0
    for index in range(200):
        note_failure(f"3000{index:02d}", now=base + index * 10.0)   # 跨 2000 s
    moment = base + 199 * 10.0

    assert warm_module._recent_failures, "至少最近几条必须在表里"  # noqa: SLF001
    assert all(moment - ts < FAILURE_DEFER_SEC
               for ts in warm_module._recent_failures.values()), (  # noqa: SLF001
        "表里出现了超出降级窗口的陈旧条目 ⇒ 会无限增长")
    # 900 s / 10 s + 1 ≈ 91 条上界
    assert len(warm_module._recent_failures) <= 92  # noqa: SLF001
