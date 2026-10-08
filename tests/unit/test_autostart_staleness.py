"""`CHG-0239`：自启/值守自检的**第二个判据** —— 「任务在、但已经不再触发」。

## 这个文件在防什么

`scripts/moss_autostart.py` 原先只看"任务定义"（在不在 / 启用没 / 动作与间隔对不对）。
2026-10-08 实测撞到第六种坏形态：**定义全对，但运行史已经停了** ——
`MossPilotWatchdog` 的实例挂住 ⇒ `MultipleInstances=IgnoreNew` 把后续每分钟的触发
全部丢弃 ⇒ 对外全站 502 半小时，而当时的 `--check` 报的是 **OK**。

⇒ 判据必须覆盖"它还在不在干活"，而且必须满足三条**不能假红**的约束：
  ① 只在开机触发的任务**不参与**（机器没重启过就是"很久没跑"，报它就是假红）；
  ② 运行史**读不到**时不下判断（`None` ≠ "从没跑过"）；
  ③ `267009`（`SCHED_S_TASK_RUNNING`）**单独不足以报警**（正常查询时也会看到它）。

跑法：
    uv run python -m pytest tests/unit/test_autostart_staleness.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# ★ 与 `tests/unit/test_moss_autostart.py` **同一套加载方式**（不是"长得一样"）：
#   那个文件已经建立了"插入 scripts/ 再按名字导入"的约定，这里跟着用同一份做法，
#   避免出现第二个 loader（两份 loader 的下一站就是其中一个悄悄导错模块）。
sys.path.insert(0, str(ROOT / "scripts"))

import moss_autostart as m  # noqa: E402

WATCHDOG = next(t for t in m.REQUIRED_TASKS if t.name == "MossPilotWatchdog")
BOOT_ONLY = next(t for t in m.REQUIRED_TASKS if t.interval_sec is None)
NOW = datetime(2026, 10, 8, 22, 48, 0)


def _obs(**kw):
    base = dict(name=WATCHDOG.name, exists=True, enabled=True,
                arguments=f'-File "{ROOT / WATCHDOG.script}"', intervals_sec=(60,))
    base.update(kw)
    return m.Observed(**base)


# ===========================================================================
# ① STALE 判据本身
# ===========================================================================


def test_stuck_instance_is_stale() -> None:
    """★ 实测形态：`LastRunTime` 卡在 22:23、`LastTaskResult=267009`、25 分钟没动。"""
    obs = _obs(last_run_at=datetime(2026, 10, 8, 22, 23, 2), last_result=267009)
    v = m.evaluate(WATCHDOG, obs, now=NOW)
    assert v.state == m.STALE, v
    assert "267009" in v.detail and "挂着" in v.detail, v.detail
    assert v.restore, "STALE 必须给出恢复指引（否则看报告的人不知道下一步做什么）"


def test_fresh_run_is_ok_and_reports_the_age() -> None:
    v = m.evaluate(WATCHDOG, _obs(last_run_at=NOW - timedelta(seconds=45),
                                  last_result=0), now=NOW)
    assert v.state == m.OK, v
    assert "最近一次 45s 前" in v.detail, v.detail


def test_unknown_last_run_never_guesses() -> None:
    """★★ `last_run_at=None` = **读不到**。读不到就不许猜（不能报 STALE）。"""
    assert m.evaluate(WATCHDOG, _obs(last_run_at=None), now=NOW).state == m.OK


def test_boot_only_task_is_never_stale() -> None:
    """★★ 只在开机触发的任务**不参与**陈旧判定 —— 否则机器跑一个月就报一次假红。"""
    obs = m.Observed(name=BOOT_ONLY.name, exists=True, enabled=True,
                     arguments=f'-File "{ROOT / BOOT_ONLY.script}"',
                     intervals_sec=(), trigger_kinds=("Boot", "Logon"),
                     last_run_at=datetime(2026, 9, 1, 0, 0, 0))
    assert m.evaluate(BOOT_ONLY, obs, now=NOW).state == m.OK


def test_grace_window_scales_with_interval() -> None:
    """宽限 = `max(3×间隔, 600s)`：60s 一轮 ⇒ 600s（下限）；1800s 一轮 ⇒ 5400s。"""
    # 60s×3 = 180 < 600 ⇒ 取下限 600s：599s 前还算 OK，601s 前就是 STALE
    assert m.evaluate(WATCHDOG, _obs(last_run_at=NOW - timedelta(seconds=599)),
                      now=NOW).state == m.OK
    assert m.evaluate(WATCHDOG, _obs(last_run_at=NOW - timedelta(seconds=601)),
                      now=NOW).state == m.STALE
    guard = next(t for t in m.REQUIRED_TASKS if t.interval_sec == 1800)
    # ⚠️ 护栏任务自己的 `script` 是空串（它就是这个模块）—— 动作里必须出现
    #    `moss_autostart.py`，否则会先被判成 ACTION_MISMATCH（我第一次就写错了）。
    guard_args = f'-Command & "py" "{ROOT / "scripts" / "moss_autostart.py"}" --check'
    obs = m.Observed(name=guard.name, exists=True, enabled=True,
                     arguments=guard_args,
                     intervals_sec=(1800,), trigger_kinds=("Boot", "Time"),
                     last_run_at=NOW - timedelta(seconds=5399))
    assert m.evaluate(guard, obs, now=NOW).state == m.OK, "1800s 一轮不该按 600s 判"
    obs2 = m.Observed(**{**obs.__dict__, "last_run_at": NOW - timedelta(seconds=5401)})
    assert m.evaluate(guard, obs2, now=NOW).state == m.STALE


def test_stale_must_change_the_exit_code() -> None:
    """★★ 判据必须**改变退出码** —— 否则值守脚本仍然报"一切正常"。"""
    assert m.STALE in m.BROKEN_STATES
    report = m.Report(verdicts=[m.evaluate(WATCHDOG, _obs(
        last_run_at=NOW - timedelta(hours=5)), now=NOW)])
    assert report.exit_code == m.EXIT_BROKEN, report.exit_code
    assert report.broken and report.broken[0].state == m.STALE


def test_broken_definition_wins_over_stale() -> None:
    """定义错时先报定义错（报"没在跑"会把方向带偏）——判据顺序不能换。"""
    obs = m.Observed(name=WATCHDOG.name, exists=True, enabled=False,
                     arguments=f'-File "{ROOT / WATCHDOG.script}"',
                     intervals_sec=(60,), last_run_at=NOW - timedelta(days=1))
    assert m.evaluate(WATCHDOG, obs, now=NOW).state == m.DISABLED


# ===========================================================================
# ② 执行时限：**唯一算法**（原实现统一 1 小时 ⇒ 挂住可合法停摆一小时）
# ===========================================================================


@pytest.mark.parametrize(("name", "want"), [
    ("MossPilotWatchdog", 180),      # 60s 一轮 ⇒ 3 分钟
    ("MossFrpEnsure", 900),          # 300s 一轮 ⇒ 上限 15 分钟
    ("MossAutostartGuard", 900),     # 1800s 一轮 ⇒ 上限 15 分钟
    ("MossPilotAutostart", 900),     # 只在开机触发 ⇒ 15 分钟
])
def test_execution_time_limit(name: str, want: int) -> None:
    spec = next(t for t in m.REQUIRED_TASKS if t.name == name)
    assert m.execution_time_limit_sec(spec) == want


def test_time_limit_is_never_an_hour() -> None:
    """★★ 回归护栏：**任何任务都不许再出现 1 小时**（实测原值：看守 1h、链路自愈 72h）。"""
    for spec in m.REQUIRED_TASKS:
        assert m.execution_time_limit_sec(spec) <= 900, spec.name


# ===========================================================================
# ③ 时间戳解析：1999-11-30 是"从未运行"的哨兵，不是时间
# ===========================================================================


@pytest.mark.parametrize(("text", "want"), [
    ("2026-10-08T22:25:02.0000000", datetime(2026, 10, 8, 22, 25, 2)),  # 7 位小数
    ("2026-10-08T22:25:02", datetime(2026, 10, 8, 22, 25, 2)),
    ("1999-11-30T00:00:00.0000000", None),   # ★ SCHED_S_TASK_HAS_NOT_RUN
    ("", None), ("垃圾", None), (None, None),
])
def test_parse_ts(text, want) -> None:
    assert m._parse_ts(text or "") == want


def test_missed_runs_is_mentioned_when_present() -> None:
    """`NumberOfMissedRuns` 有值时要点出来（它是"被丢弃的触发次数"的直接证据）。"""
    v = m.evaluate(WATCHDOG, _obs(last_run_at=NOW - timedelta(hours=1),
                                  last_result=0, missed_runs=17), now=NOW)
    assert v.state == m.STALE and "17" in v.detail, v.detail
