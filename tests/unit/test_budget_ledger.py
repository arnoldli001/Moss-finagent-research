"""★ 「全局日预算硬闸」的判据 —— 跨进程可见性 + 硬上限（2026-10-01）。

## 现场（本轮补的短板）

`CostBudget` 的账本原本是**进程内**的，于是：

    api 进程今天已花 18 / 20 元
    worker 进程（4 个重作业）与任何新起的脚本 → 看到 0 ⇒ remaining = 20 元

也就是说 **"全局日预算"在跨进程时根本不存在**：该拦的一个都没拦。
这不是"数字略不准"，而是护栏的形状问题。

修法：`DaySpendLedger` —— 一天一个 append-only JSONL，所有进程追加、
所有进程读同一份（`<run_dir>/llm_spend-YYYY-MM-DD.jsonl`），
`remaining` / `reserve_task` / `ScriptCostGuard.check_entry` 一律看**全局**值，
并新增 `check_hard_cap()` 硬闸。

## 判据怎么保证不是假的

* 用**两个独立的 `CostBudget` 实例**模拟两个进程（同一个 ledger 目录）；
* **自证**那条反过来跑：`ledger_dir=None`（旧行为）时两个实例**必须对不上** ——
  如果连这都"看得见"，说明判据测的不是这套机制。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.core.budget import (
    BudgetExhaustedError,
    CostBudget,
    DaySpendLedger,
    ScriptCostError,
    ScriptCostGuard,
)

#: 一次真实调用的价（读真实 models.yaml，避免把价格写死在测试里）
CALL = dict(provider="deepseek", model="deepseek-flash",
            tokens_in=10_000, tokens_out=10_000)


def _budget(tmp_path: Path, *, daily: float = 20.0, ledger: bool = True,
            hard_cap: float | None = None, reserve: float = 0.25) -> CostBudget:
    return CostBudget(
        daily_budget=daily,
        task_reserve=reserve,
        ledger_dir=(tmp_path if ledger else None),
        hard_cap_cny=hard_cap,
    )


# ======================================================================
# 一、跨进程可见：另一个"进程"花的钱，这个进程必须看得见
# ======================================================================

def test_two_processes_share_one_daily_total(tmp_path: Path) -> None:
    """★ api 进程花的钱，worker 进程（新实例）必须看得见。"""
    api = _budget(tmp_path)
    worker = _budget(tmp_path)          # 模拟另一个进程：新实例、同一个账本目录

    assert worker.day_total() == 0.0, "新进程一开始就该是 0（还没人花钱）"
    for _ in range(5):
        api.record(**CALL)
    spent = api.snapshot()["spent_cny"]
    assert spent > 0

    total = worker.day_total()
    assert total == pytest.approx(spent, abs=1e-9), (
        f"worker 看到的全局花费 {total} 与 api 实际花的 {spent} 不一致 ⇒ "
        f"跨进程账本没生效")
    assert worker.remaining < worker.budget, "全局花费必须挤占剩余额度"


def test_cross_process_spend_blocks_research_reservation(tmp_path: Path) -> None:
    """另一个进程把钱花光后，本进程的预扣必须被拒（在线准入的全局化）。"""
    api = _budget(tmp_path, daily=0.05, reserve=0.02)
    worker = _budget(tmp_path, daily=0.05, reserve=0.02)
    assert worker.reserve_task("research") is True      # 一开始有额度
    api.record(**CALL)                                  # 另一个进程花掉
    fresh = _budget(tmp_path, daily=0.05, reserve=0.02)
    assert fresh.reserve_task("research") is False, (
        "全局额度已耗尽，新进程仍放行 ⇒ 预扣看的是本进程账本，不是全局")


# ======================================================================
# 二、硬闸：达上限就拒绝，而且是**跨进程**拒绝
# ======================================================================

def test_hard_cap_blocks_even_a_fresh_process(tmp_path: Path) -> None:
    api = _budget(tmp_path, daily=20.0, hard_cap=0.01)
    while not api.hard_cap_exceeded():
        api.record(**CALL)
    fresh = _budget(tmp_path, daily=20.0, hard_cap=0.01)     # 新进程
    assert fresh.hard_cap_exceeded() is True
    with pytest.raises(BudgetExhaustedError):
        fresh.check_hard_cap("mainline_relevance")


def test_script_guard_entry_refuses_when_the_day_is_used_up(tmp_path: Path) -> None:
    """★ 脚本入口：另一个进程已把额度花完 ⇒ 入口必须拒绝启动（一次调用都不发）。"""
    api = _budget(tmp_path, daily=0.05, hard_cap=0.05)
    while not api.hard_cap_exceeded():
        api.record(**CALL)
    guard = ScriptCostGuard("mainline_relevance", cap_cny=1.0,
                            budget=_budget(tmp_path, daily=0.05, hard_cap=0.05))
    with pytest.raises((BudgetExhaustedError, ScriptCostError)) as exc:
        guard.check_entry(0.5)
    assert "全局" in str(exc.value), "错误信息必须点明这是全局额度（否则排障会找错进程)"


def test_hard_cap_disabled_when_non_positive(tmp_path: Path) -> None:
    """`hard_cap<=0` = 不限：不许把"没设上限"变成"全部拒绝"。"""
    b = _budget(tmp_path, daily=20.0, hard_cap=0)
    b.record(**CALL)
    b.check_hard_cap("x")            # 不抛
    assert b.hard_cap_exceeded() is False


# ======================================================================
# 三、账本自身的健壮性（它坏掉不能拖垮业务）
# ======================================================================

def test_yesterday_file_is_not_counted(tmp_path: Path) -> None:
    """只认当天文件：昨天花掉的不许算进今天（否则"跨日归零"就是假的）。"""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (tmp_path / f"llm_spend-{yesterday}.jsonl").write_text(
        json.dumps({"day": yesterday, "cny": 999.0}) + "\n", encoding="utf-8")
    b = _budget(tmp_path)
    assert b.day_total() == 0.0


def test_corrupt_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """单行损坏只跳过它 —— 账本要能自愈，不能因为一行坏数据把护栏打挂。"""
    today = date.today().isoformat()
    path = tmp_path / f"llm_spend-{today}.jsonl"
    path.write_text(
        "{ 这不是 JSON\n"
        + json.dumps({"day": today, "cny": 0.25, "source": "t"}) + "\n",
        encoding="utf-8")
    b = _budget(tmp_path)
    assert b.day_total() == pytest.approx(0.25)


def test_ledger_write_failure_does_not_raise(tmp_path: Path) -> None:
    """落盘失败**不许**影响调用结果（钱已经花了，账记不下来也不能让调用失败）。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我是个文件，不是目录", encoding="utf-8")
    b = _budget(blocked)             # 把"目录"指到一个文件上 ⇒ mkdir 必失败
    b.record(**CALL)                 # 不抛
    assert b.snapshot()["spent_cny"] > 0, "内存账本仍应记上"


def test_snapshot_surfaces_the_ledger_state(tmp_path: Path) -> None:
    """★ 可见性：快照必须能区分"本进程花的"与"全局花的"，否则运维页会读错。"""
    b = _budget(tmp_path)
    snap = b.snapshot()
    for key in ("day_total_cny", "hard_cap_cny", "hard_cap_exceeded",
                "ledger_enabled", "ledger_path", "ledger_calls"):
        assert key in snap, f"快照缺字段 {key}"
    assert snap["ledger_enabled"] is True and snap["ledger_path"]
    b.record(**CALL)
    assert b.snapshot()["day_total_cny"] == pytest.approx(b.snapshot()["spent_cny"])


def test_ledger_dir_can_be_switched_off_explicitly() -> None:
    """`ledger_dir=None` 时 `ledger_enabled=False` —— 退化必须**看得见**。"""
    b = CostBudget(daily_budget=20.0, ledger_dir=None)
    assert b.snapshot()["ledger_enabled"] is False


# ======================================================================
# 四、★ 自证：没有这份落盘账本，两个实例**必须**对不上
# ======================================================================

def test_without_the_durable_ledger_two_processes_diverge(tmp_path: Path) -> None:
    """★★ 自证：把账本关掉（旧行为）⇒ 另一个实例**看不到**这笔钱。

    没有这一条，上面那些"看得见"的断言可能在"两个实例共享同一个进程内单例"
    之类的巧合下**假绿** —— 本项目纪律：一条从没红过的判据要先怀疑它坏了。
    """
    api = _budget(tmp_path, ledger=False)
    worker = _budget(tmp_path, ledger=False)
    api.record(**CALL)
    assert api.snapshot()["spent_cny"] > 0
    assert worker.day_total() == 0.0, (
        "关掉落盘账本后仍能看到对方的钱 ⇒ 说明可见性另有来源，判据测错了东西")
    assert worker.snapshot()["ledger_enabled"] is False


def test_day_spend_ledger_records_are_self_describing(tmp_path: Path) -> None:
    """账本行要能自解释（排障时不需要猜是谁写的）。"""
    led = DaySpendLedger(tmp_path)
    led.add(0.5, "mainline_relevance")
    line = (tmp_path / f"llm_spend-{date.today().isoformat()}.jsonl") \
        .read_text(encoding="utf-8").strip()
    row = json.loads(line)
    for key in ("ts", "day", "cny", "source", "pid"):
        assert key in row, f"账本行缺 {key}"
    assert row["source"] == "mainline_relevance" and row["cny"] == 0.5
