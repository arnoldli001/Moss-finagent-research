#!/usr/bin/env python
"""Moss 自启/值守计划任务 —— **唯一事实源 + 存在性自检**（`CHG-0168`）。

## 为什么需要这个文件（2026-10-05 真实事故）

对外入口 `hk.wujiaitool.cn` 全站 502，客户先发现。根因**不是**"自启失败"，
而是**自启任务本身已经不在了**：

| 任务 | 应有 | 当天实测 |
|---|---|---|
| `MossPilotAutostart`（开机拉起 8110） | 有 | **不存在** |
| `MossPilotWatchdog`（每 1 分钟 ensure） | 有 | **不存在** |
| `MossFrpEnsure`（每 5 分钟修 hk 链路） | 有 | 在，且一直正常工作 |

于是链路变成：`MossFrpEnsure` 每 5 分钟探到"本机后端 8110 返回 000"，
按设计**主动放手**（"跳过隧道处置（归 PilotWatchdog）"）—— 而它托付的那个值守
**不存在**。日志里写着一句看起来完全正常的话，实际无人负责。

**谁删的查不到**：`Microsoft-Windows-TaskScheduler/Operational` 当时是**关闭**的
（本次已开启，见 `CHG-0168` 的处置）。这是本项目反复出现的同一个形状：
**结构变更自带静默失效**。

## 这个文件解决什么

把"开机自启到底还在不在"从**仓库之外、无人核对的状态**，变成**一条可跑的判据**：

    uv run python scripts/moss_autostart.py --check     # 三态体检（退出码见下）
    uv run python scripts/moss_autostart.py --install   # 按本表幂等重建（需人点头）
    uv run python scripts/moss_autostart.py --self-test # 检查器自证（喂已知答案）

## ★ 三态判据：**"读不到" ≠ "不存在"**

这是本文件最重要的一条，也是本项目付过代价的地方（"没量到"与"量到 0"必须分开显示）：

* 探针跑不起来（非 Windows / PowerShell 缺失 / 超时 / JSON 解析失败）
  ⇒ 每个任务记 **`UNKNOWN`**，退出码 **2**（"无法判定，需要人来看"）；
* 探针成功且任务确实没登记 ⇒ **`MISSING`**，退出码 1；
* 探针成功、任务在、但被停用 / 动作指向别的脚本 / 间隔被改
  ⇒ **`DISABLED` / `ACTION_MISMATCH` / `INTERVAL_MISMATCH` / `TRIGGER_MISMATCH`**，退出码 1。

把 UNKNOWN 塌缩成 MISSING（或塌缩成 OK）都会造出**假绿**：
前者会让人去重建一个本来好好的任务，后者正是本次事故的形态。

## 唯一的注册入口（不写第二份清单）

`REQUIRED_TASKS` 是本表；`--install` 从**同一张表**注册，`--check` 从**同一张表**
核对 —— 所以"装了什么"和"查什么"不可能漂移（本项目实测过同一个 key 写在 3 处，
只改一处 ⇒ 情报 5 个端点对所有人 403）。

`installable=False` 的任务只**核对**、不代装：`MossFrpEnsure` 的动作必须是
`wscript.exe` + 交互式 Administrator 身份（原因见 `scripts/frp_watchdog.ps1` 头部：
SYSTEM 身份下 `Path.home()` 变了、SSH 密钥找不到、隧道直接起不来）——
这些是**承重细节**，通用安装器代装只会把它装坏。

## 与外部监控的分工（诚实说明本文件的能力边界）

本文件**只能看见本机**。它覆盖"任务被删/被停用"；**不覆盖**"整机没开、
没网、Windows 起不来"。后者归 VPS 上的外部监控（`scripts/moss_vps_monitor.py`
部署到 VPS，与本文件互相独立）—— 两个方向都覆盖，才叫"不会又是客户先发现"。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "data" / "run"
STATE_PATH = RUN_DIR / "autostart-guard-state.json"
GUARD_LOG = RUN_DIR / "autostart-guard.log"

#: 本模块自己的任务名（自检任务**也要被自检**：它同样是会被删掉的持久对象）。
GUARD_TASK_NAME = "MossAutostartGuard"

#: `--check` 的退出码。2 与 1 分开是刻意的：**"需要人来看"与"确认坏了"不是一回事**。
EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_UNKNOWN = 2


# ======================================================================
# 需求表：**唯一事实源**
# ======================================================================
@dataclass(frozen=True)
class TaskSpec:
    """一个必须存在的持久对象。"""

    name: str
    #: 相对仓库根的脚本路径（`""` = 本模块自己，见 `GUARD_TASK_NAME`）
    script: str
    #: 动作如何启动：`powershell` / `wscript` / `python`
    kind: str
    #: 重复间隔（秒）；`None` = 只在开机/登录触发，没有周期
    interval_sec: int | None
    #: 是否必须带开机触发器（**"开机自启"这四个字就落在这个字段上**）
    needs_boot_trigger: bool
    #: 是否允许本安装器代装（承重细节多的任务不代装，见模块头）
    installable: bool
    #: 它为什么必须存在（写进输出，让下一个人不用读这段对话）
    why: str
    #: 缺失时的恢复指引
    restore: str


REQUIRED_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        name="MossPilotAutostart",
        script="scripts/pilot_autostart.ps1",
        kind="powershell",
        interval_sec=None,
        needs_boot_trigger=True,
        installable=True,
        why="开机把对外实例 pilot(8110) 拉起来；没有它，重启后没有任何东西启动后端",
        restore="uv run python scripts/moss_autostart.py --install",
    ),
    TaskSpec(
        name="MossPilotWatchdog",
        script="scripts/pilot_watchdog.ps1",
        kind="powershell",
        interval_sec=60,
        needs_boot_trigger=False,
        installable=True,
        why="运行期每分钟一轮 ensure：后端进程消失后 ≤60s 自动恢复（全项目唯一运行期值守入口）",
        restore="uv run python scripts/moss_autostart.py --install",
    ),
    TaskSpec(
        name="MossFrpEnsure",
        script="scripts/frp_watchdog_hidden.vbs",
        kind="wscript",
        interval_sec=300,
        needs_boot_trigger=False,
        installable=False,
        why="hk 公网链路（SSH 隧道 + frpc，5 跳）每 5 分钟自愈一次",
        restore="见 docs/HK_VPS_MIGRATION.md §9 与 docs/OPS_GUIDE.md §2.8（动作必须是 "
                "wscript.exe + 交互式 Administrator 身份，不要用通用安装器代装）",
    ),
    TaskSpec(
        name=GUARD_TASK_NAME,
        script="",                      # 本模块自己
        kind="python",
        interval_sec=1800,
        needs_boot_trigger=True,
        installable=True,
        why="每 30 分钟核对上表；发现任何一个任务缺失/停用/动作不符就发信（本文件的存在理由）",
        restore="uv run python scripts/moss_autostart.py --install",
    ),
)

#: 判据取值（**判据只认机器可读的状态**，不认给人看的文案）
OK = "OK"
MISSING = "MISSING"
DISABLED = "DISABLED"
ACTION_MISMATCH = "ACTION_MISMATCH"
INTERVAL_MISMATCH = "INTERVAL_MISMATCH"
TRIGGER_MISMATCH = "TRIGGER_MISMATCH"
#: ★★ `CHG-0239`（2026-10-08 实测）：**任务在、启用、动作与间隔都对，但它已经不再触发**。
#:
#: ## 为什么这一态必须单独存在
#:
#: 前五种坏形态都能从"任务定义"上看出来；这一种**定义完全正确**，
#: 坏的是它的**运行史**：上一个实例挂住 ⇒ `MultipleInstances=IgnoreNew`
#: 让后续每分钟的触发**全部被丢弃**，而 `ExecutionTimeLimit` 若是一小时，
#: 就要**挂满一小时**才被系统收掉。
#:
#: 实测（`MossPilotWatchdog`）：`LastRunTime` 卡在 22:23、`LastTaskResult=267009`
#: （`SCHED_S_TASK_RUNNING`）、日志最后一条是前一天 —— 而 `--check` 当时报 **OK**。
#: 同一个洞里，`MossFrpEnsure` 正按设计"放手（归 PilotWatchdog）"，
#: 于是**两个值守互相托付、谁都没动**，对外全站 502 半小时。
#: ⇒ "任务在不在"与"任务还在不在干活"是**两个判据**，缺后者就是假绿。
STALE = "STALE"
UNKNOWN = "UNKNOWN"

BROKEN_STATES = (MISSING, DISABLED, ACTION_MISMATCH, INTERVAL_MISMATCH,
                 TRIGGER_MISMATCH, STALE)


# ======================================================================
# 纯函数层（可离线单测：不碰进程表、不碰 Windows）
# ======================================================================
@dataclass(frozen=True)
class Observed:
    """一次探测看到的**事实**。`exists=None` 表示**读不到**（不是不存在）。"""

    name: str
    exists: bool | None
    enabled: bool | None = None
    execute: str = ""
    arguments: str = ""
    intervals_sec: tuple[int, ...] = ()
    trigger_kinds: tuple[str, ...] = ()
    #: ★ `CHG-0239`：**运行史**。`None` = 读不到（不是"从没跑过"）——
    #: 两者的区别决定了要不要报 STALE（读不到就不许猜）。
    last_run_at: datetime | None = None
    #: `LastTaskResult`；`267009` = `SCHED_S_TASK_RUNNING`（查询时正在跑），
    #: `267011` = `SCHED_S_TASK_HAS_NOT_RUN`（从未跑过）。
    last_result: int | None = None
    #: `NumberOfMissedRuns`：计划要跑但被错过的次数（挂住的直接证据）。
    missed_runs: int | None = None
    note: str = ""


@dataclass(frozen=True)
class Verdict:
    name: str
    state: str
    detail: str
    why: str = ""
    restore: str = ""

    @property
    def ok(self) -> bool:
        return self.state == OK


@dataclass
class Report:
    verdicts: list[Verdict] = field(default_factory=list)
    platform_note: str = ""

    @property
    def exit_code(self) -> int:
        if any(v.state == UNKNOWN for v in self.verdicts):
            return EXIT_UNKNOWN
        if any(v.state in BROKEN_STATES for v in self.verdicts):
            return EXIT_BROKEN
        return EXIT_OK

    @property
    def broken(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.state in BROKEN_STATES]

    @property
    def unknown(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.state == UNKNOWN]


def _normalize(text: str) -> str:
    """路径比较前的归一：大小写 + 斜杠方向（Windows 两者都合法）。"""
    return text.replace("\\", "/").lower().strip()


def evaluate(spec: TaskSpec, obs: Observed, *, now: datetime | None = None) -> Verdict:
    """一条判据：期望（`spec`）vs 事实（`obs`）→ 结论。

    ## 判据顺序不能换

    1. **读不到**（`obs.exists is None`）→ `UNKNOWN`，**立刻返回**。
       先判"不存在"会把探针故障说成任务被删 —— 那会让人去重建一个好好的任务，
       而真正的问题（探针坏了）被掩盖。
    2. 不存在 → `MISSING`。
    3. 被停用 → `DISABLED`（**存在但不生效**，是最像"已配置"的坏形态）。
    4. 动作指向别的脚本 → `ACTION_MISMATCH`（例：任务还在，但脚本被改名/搬走）。
    5. 间隔不符 → `INTERVAL_MISMATCH`（例：每分钟被改成每 5 分钟 ⇒ 停机窗口 ×5）。
    6. 缺开机触发器 → `TRIGGER_MISMATCH`（"开机自启"名存实亡）。
    7. ★ `CHG-0239`：**定义全对、但它已经不再触发** → `STALE`（见 `STALE` 的注释）。
       放在最后是因为**只有定义都对时，"没在跑"才是一个新信息**：
       定义错了先修定义，报"没在跑"只会把方向带偏。

    `now` 可注入（纯函数便于单测）；不给就取当前时间。
    """
    def bad(state: str, detail: str) -> Verdict:
        return Verdict(name=spec.name, state=state, detail=detail,
                       why=spec.why, restore=spec.restore)

    if obs.exists is None:
        return bad(UNKNOWN, f"**读不到**（{obs.note or '探针未给出原因'}）"
                            "—— 无法判定，不要当成'任务不存在'")

    if not obs.exists:
        return bad(MISSING, "计划任务不存在（持久对象已被删除）")

    if obs.enabled is False:
        return bad(DISABLED, "任务存在但已被**停用**：装着不看，等于没有")

    want_script = _normalize(spec.script or Path(__file__).name)
    have = _normalize(obs.arguments)
    if want_script not in have:
        return bad(ACTION_MISMATCH,
                   f"动作指向的不是本表登记的脚本：期望含 `{spec.script or Path(__file__).name}`，"
                   f"实际 `{obs.arguments[:120]}`")

    if spec.interval_sec is not None:
        if not obs.intervals_sec:
            return bad(INTERVAL_MISMATCH,
                       f"期望每 {spec.interval_sec}s 触发一次，实际**没有重复间隔**（只触发一次）")
        if spec.interval_sec not in obs.intervals_sec:
            got = "、".join(f"{i}s" for i in obs.intervals_sec)
            return bad(INTERVAL_MISMATCH,
                       f"触发间隔被改过：期望 {spec.interval_sec}s，实际 {got}"
                       "（间隔 = 故障后最长不可用时间）")

    if spec.needs_boot_trigger and "Boot" not in obs.trigger_kinds:
        return bad(TRIGGER_MISMATCH,
                   f"缺**开机触发器**：实际只有 {'、'.join(obs.trigger_kinds) or '无'} "
                   "⇒ 重启后不会自己起来")

    stale = staleness_of(spec, obs, now=now)
    if stale is not None:
        return bad(STALE, stale)

    extra = ""
    if spec.interval_sec is not None and obs.last_run_at is not None:
        age = int(((now or datetime.now()) - obs.last_run_at).total_seconds())
        extra = f"，最近一次 {age}s 前"
    return Verdict(name=spec.name, state=OK,
                   detail=f"在且启用（间隔 {spec.interval_sec or '—'}s，"
                          f"触发器 {'、'.join(obs.trigger_kinds) or '—'}{extra}）",
                   why=spec.why, restore=spec.restore)


#: 判定 STALE 的宽限倍数与下限：**慢一点不算死**，但也不能等到下一个小时。
#: 3 倍间隔是"允许一次超时 + 一次抖动"；600s 下限是给 30 分钟一轮的自检任务留余地。
STALE_INTERVAL_FACTOR = 3
STALE_MIN_GRACE_SEC = 600


def staleness_of(spec: TaskSpec, obs: Observed, *,
                 now: datetime | None = None) -> str | None:
    """「在、启用、定义都对，但**已经不再触发**」→ 人话原因；判不出来 → `None`。

    ## 判据（三条全部满足才报）

      ① 任务**有周期**（`interval_sec` 不是 None）—— 只在开机触发的任务
         "很久没跑"是正常的（机器没重启过），报它就是**假红**；
      ② `last_run_at` **读得到** —— 读不到就不许猜（`None` 不是"从没跑过"）；
      ③ 距上次运行 > `max(3×间隔, 600s)`。

    ## 为什么不用 `LastTaskResult` 单独判

    `267009`（`SCHED_S_TASK_RUNNING`）**在正常情况也会出现**（查询的那一刻它正好在跑）。
    单独拿它报警会把"正在正常工作"报成故障 —— 所以它只作为 **STALE 的佐证**出现在
    文案里，判据本身仍然是"时间"。
    """
    if not spec.interval_sec or obs.last_run_at is None:
        return None
    now = now or datetime.now()
    age = (now - obs.last_run_at).total_seconds()
    limit = max(spec.interval_sec * STALE_INTERVAL_FACTOR, STALE_MIN_GRACE_SEC)
    if age <= limit:
        return None
    hints = [f"距上次运行 **{int(age)}s**（阈值 {int(limit)}s = max({STALE_INTERVAL_FACTOR}×"
             f"{spec.interval_sec}s, {STALE_MIN_GRACE_SEC}s)）"]
    if obs.last_result == 267009:
        hints.append("`LastTaskResult=267009`（`SCHED_S_TASK_RUNNING`）"
                     "⇒ **上一个实例还挂着**，`MultipleInstances=IgnoreNew` 会让后续"
                     "触发全部被丢弃")
    elif obs.last_result is not None:
        hints.append(f"`LastTaskResult={obs.last_result}`")
    if obs.missed_runs:
        hints.append(f"`NumberOfMissedRuns={obs.missed_runs}`")
    hints.append("结果就是**看着在、其实没人干活**（2026-10-08 实测：对外全站 502 半小时）")
    return "；".join(hints)


def execution_time_limit_sec(spec: TaskSpec) -> int:
    """该任务的 `ExecutionTimeLimit`（秒）——**唯一算法**，`--install` 用它。

    ## 为什么不能统一写 1 小时（原实现就是）

    `MultipleInstances=IgnoreNew` + `ExecutionTimeLimit=1h` 的组合意味着：
    **一个挂住的实例能让后续 60 个 tick 全部作废，而且要挂满一小时才被系统收掉。**
    对每分钟一轮的值守，这等于"值守可以合法地停摆一小时"。

    取值 = `max(3×间隔, 120s)`，上限 15 分钟：既容得下一次超时重试，
    又不至于把停机窗口放大到分钟级之上（`MossFrpEnsure` 的 300s ⇒ 15 分钟）。
    """
    if not spec.interval_sec:
        return 900
    return min(max(spec.interval_sec * STALE_INTERVAL_FACTOR, 120), 900)


def _parse_ts(text: str) -> datetime | None:
    """`Get-ScheduledTaskInfo` 的 `LastRunTime` → `datetime`；读不到/哨兵值 → `None`。

    ⚠️ 任务计划用 **1999-11-30** 表示"从未运行过"（`SCHED_S_TASK_HAS_NOT_RUN`）。
    把它当成一个真实时间会算出"距上次运行 26 年" ⇒ **假红**。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # `.ToString('o')` 在 Windows 上给 7 位小数，Python 只吃到 6 位
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?", raw)
    if not m:
        return None
    frac = (m.group(2) or "")[:6].ljust(6, "0")
    try:
        ts = datetime.fromisoformat(f"{m.group(1)}.{frac}")
    except ValueError:
        return None
    return None if ts.year < 2000 else ts


def parse_iso_duration(text: str) -> int | None:
    """`PT1M` / `PT5M` / `PT30M` / `PT1H` / `P1DT2H` → 秒。看不懂 → `None`。

    任务计划把 `Repetition/Interval` 写成 ISO-8601 时长。**看不懂就返回 None**
    （由调用方当"读不到"处理），不要猜 —— 猜错的代价是判据说"间隔对"而实际不对。
    """
    if not text:
        return None
    m = re.fullmatch(
        r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?",
        text.strip().upper())
    if not m or not any(m.groupdict().values()):
        return None
    d = int(m.group("d") or 0)
    h = int(m.group("h") or 0)
    mi = int(m.group("m") or 0)
    s = int(m.group("s") or 0)
    return d * 86400 + h * 3600 + mi * 60 + s


# ======================================================================
# 入口层：探测（一次 PowerShell 拿全部，探针失败 = UNKNOWN）
# ======================================================================
#: ⚠️ 占位符用 `__NAMES__` 而不是 `str.format` —— PowerShell 里满屏花括号，
#:   走 `.format()` 就得把每个 `{` 写成 `{{`，漏一个就是 `KeyError`
#:   （本项目实测：这类"成对分隔符"错误一次改不对就会连错三次）。
_PROBE_PS = r"""
$ErrorActionPreference = 'Stop'
$names = @(__NAMES__)
$out = foreach ($n in $names) {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($null -eq $t) {
    [pscustomobject]@{ name = $n; exists = $false }
    continue
  }
  $iv = @()
  $tk = @()
  foreach ($tr in @($t.Triggers)) {
    $tk += ($tr.CimClass.CimClassName -replace '^MSFT_Task', '' -replace 'Trigger$', '')
    if ($tr.Repetition -and $tr.Repetition.Interval) { $iv += [string]$tr.Repetition.Interval }
  }
  $a = @($t.Actions)[0]
  # ★ `CHG-0239`：**运行史**（定义之外的第二个判据）。
  #   任务定义全对但"已经不再触发"这一态，只能从这里看出来。
  $info = Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue
  $lastRun = $null
  $lastResult = $null
  $missed = $null
  if ($null -ne $info) {
    if ($info.LastRunTime) { $lastRun = ([datetime]$info.LastRunTime).ToString('o') }
    $lastResult = [int]$info.LastTaskResult
    $missed = [int]$info.NumberOfMissedRuns
  }
  [pscustomobject]@{
    name = $t.TaskName
    exists = $true
    enabled = [bool]$t.Settings.Enabled
    execute = [string]$a.Execute
    arguments = [string]$a.Arguments
    intervals = $iv
    trigger_kinds = $tk
    last_run = $lastRun
    last_result = $lastResult
    missed_runs = $missed
  }
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 5
"""


def _run_probe(names: list[str], *, timeout: float = 60.0) -> tuple[str, str]:
    """跑一次 PowerShell。返回 `(stdout, 失败原因)`；失败原因非空即"读不到"。"""
    quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
    script = _PROBE_PS.replace("__NAMES__", quoted)
    try:
        done = subprocess.run(  # noqa: S603 固定 powershell + 固定脚本
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return "", "本机没有 powershell（非 Windows？）"
    except subprocess.TimeoutExpired:
        return "", f"探测超时（{timeout:g}s）"
    if done.returncode != 0:
        err = (done.stderr or b"").decode("utf-8", "replace").strip()
        return "", f"powershell 退出码 {done.returncode}：{err[:200] or '无 stderr'}"
    return (done.stdout or b"").decode("utf-8", "replace"), ""


def observe(names: list[str], *, runner=None) -> dict[str, Observed]:
    """探测全部任务。**任何一步失败都不许塌缩成 exists=False。**"""
    runner = runner or _run_probe
    raw, err = runner(names)
    if err:
        return {n: Observed(name=n, exists=None, note=err) for n in names}
    try:
        data = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        return {n: Observed(name=n, exists=None,
                            note=f"探测输出不是合法 JSON：{exc}") for n in names}
    if isinstance(data, dict):          # 单元素时 ConvertTo-Json 会退化成对象
        data = [data]
    out: dict[str, Observed] = {}
    for item in data:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        intervals = []
        for text in item.get("intervals") or []:
            sec = parse_iso_duration(str(text))
            if sec is None:
                # ★ 看不懂的间隔 = 读不到 ⇒ 整条记 UNKNOWN，别当"没有间隔"
                return {n: Observed(name=n, exists=None,
                                    note=f"看不懂的重复间隔 `{text}`")
                        for n in names}
            intervals.append(sec)
        out[name] = Observed(
            name=name,
            exists=bool(item.get("exists")),
            enabled=item.get("enabled"),
            execute=str(item.get("execute") or ""),
            arguments=str(item.get("arguments") or ""),
            intervals_sec=tuple(intervals),
            trigger_kinds=tuple(str(k) for k in (item.get("trigger_kinds") or [])),
            # ★ `CHG-0239`：运行史。**读不到就是 None**（不是"从没跑过"）——
            #   `_parse_ts` 同时把 1999-11-30 那个哨兵值也归成 None。
            last_run_at=_parse_ts(str(item.get("last_run") or "")),
            last_result=(int(item["last_result"])
                         if isinstance(item.get("last_result"), int) else None),
            missed_runs=(int(item["missed_runs"])
                         if isinstance(item.get("missed_runs"), int) else None),
        )
    # 探针没提到的任务算"读不到"（不是"不存在"）：只说看见了什么，不替它断言
    for name in names:
        out.setdefault(name, Observed(name=name, exists=None,
                                      note="本次探测输出里没有这个任务"))
    return out


def check(*, runner=None) -> Report:
    """体检：期望表 × 探测结果 → 报告。"""
    if os.name != "nt":
        return Report(platform_note="不适用：计划任务是 Windows 概念，本机不是 Windows")
    specs = list(REQUIRED_TASKS)
    obs = observe([s.name for s in specs], runner=runner)
    return Report(verdicts=[evaluate(s, obs[s.name]) for s in specs])


# ======================================================================
# 告警闸门 + 邮件通道 + 流水：**共用件**（`scripts/moss_ops_alert.py`）
# # ======================================================================
#
# 本文件与 VPS 上的外部监控（`scripts/moss_vps_monitor.py`）是**两个互相独立的
# 发送方**，必须用**同一套闸门**。写成两份的代价本项目付过很多次
# （同一个 key 写在 3 处，只改一处 ⇒ 情报 5 个端点对所有人 403），
# 所以三件事都放在共用件里、两边 import 同一份代码，并由
# `tests/unit/test_moss_autostart.py::test_both_senders_share_one_gate`
# 钉住"它们其实是同一个函数对象"（不是"两份长得一样"）。
from moss_ops_alert import (  # noqa: E402 同目录模块：脚本直接运行时由 sys.path[0] 提供
    GateState,
    MailConfig,
    append_line,
    decide_notify,
    load_env_file,
    mail_config,
    send_mail,
)


# ======================================================================
# 报告与日志
# ======================================================================
def render(report: Report, *, now: datetime | None = None) -> str:
    now = now or datetime.now()
    lines = [f"自启/值守任务体检 —— {now.strftime('%Y-%m-%d %H:%M:%S')}"]
    if report.platform_note:
        lines.append(f"  {report.platform_note}")
        return "\n".join(lines)
    width = max(len(v.name) for v in report.verdicts) if report.verdicts else 10
    for v in report.verdicts:
        mark = {OK: "✅", UNKNOWN: "❓"}.get(v.state, "❌")
        lines.append(f"  {mark} {v.name.ljust(width)}  {v.state:<17} {v.detail}")
    if report.broken or report.unknown:
        lines.append("")
        for v in [*report.broken, *report.unknown]:
            lines.append(f"  · {v.name}：它为什么必须存在 —— {v.why}")
            lines.append(f"    恢复：{v.restore}")
    lines.append("")
    if report.exit_code == EXIT_OK:
        lines.append("  结论：OK（表里每个持久对象都在、都启用、动作与间隔都对）")
    elif report.exit_code == EXIT_UNKNOWN:
        lines.append("  结论：**无法判定**（读不到探测结果）—— 这不是'通过'，"
                     "请人工确认；把它当 0 就是假绿")
    else:
        lines.append(f"  结论：**{len(report.broken)} 个持久对象不达标**"
                     "（缺失/停用/动作或间隔不符 ⇒ 重启后不会自己起来）")
    return "\n".join(lines)


def append_log(text: str, *, keep_lines: int = 500, max_bytes: int = 512_000) -> None:
    """落一行自检流水 —— 转发到共用件（路径是本模块的 `GUARD_LOG`）。

    **健康时也要写**，否则"值守在跑且没事"与"值守早就没了"在文件里长得一模一样
    （本项目实测过这个形状）。
    """
    append_line(GUARD_LOG, text, keep_lines=keep_lines, max_bytes=max_bytes)


def fingerprint_of(report: Report) -> str:
    return "+".join(sorted(f"{v.name}:{v.state}" for v in report.verdicts
                           if not v.ok)) or "none"


# ======================================================================
# 安装（从同一张表；幂等）
# ======================================================================
def _ps(script: str, *, timeout: float = 120.0) -> str:
    done = subprocess.run(  # noqa: S603 固定 powershell + 固定脚本
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=timeout)
    return ((done.stdout or b"") + (done.stderr or b"")).decode("utf-8", "replace").strip()


def _action_for(spec: TaskSpec) -> tuple[str, str]:
    """→ `(execute, arguments)`。**动作与既有任务逐字同形**，不新造写法。"""
    if spec.kind == "python":
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        script = ROOT / "scripts" / "moss_autostart.py"
        # ⚠️ 路径只用**双引号**，绝不能用单引号：这段参数最终被写成
        #   `-Argument '<参数串>'`（外层是 PowerShell 单引号字面量），
        #   里面再出现单引号会**直接把字面量截断**，症状是
        #   `New-ScheduledTaskAction : A positional parameter cannot be found`
        #   —— 报错信息里那串路径显示"引号没了"，就是这个原因（实测两次）。
        return "powershell.exe", (
            "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass "
            f'-Command & "{py}" "{script}" --check --notify --log')
    ps1 = ROOT / spec.script
    if spec.kind == "wscript":
        return "wscript.exe", f'"{ps1}"'
    return "powershell.exe", (
        "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass "
        f'-File "{ps1}"')


def install(*, dry_run: bool = False) -> int:
    """按 `REQUIRED_TASKS` 幂等注册可代装的任务（**会在本机留下持久对象**）。"""
    if os.name != "nt":
        print("✗ 本命令只在 Windows 上有意义")
        return 2
    rc = 0
    for spec in REQUIRED_TASKS:
        if not spec.installable:
            print(f"⏭  {spec.name}：本表只核对、不代装（承重细节多）—— {spec.restore}")
            continue
        execute, arguments = _action_for(spec)
        # ★ `CHG-0243`：`limit_sec` 必须先算出来 —— `CHG-0239` 改这里时我只换了
        #   字符串里的 `{limit_sec}` 却没定义它，于是 `--install` **每一次都会
        #   `NameError`**（2026-10-11 重启后要重建任务时才暴露）。
        #   ⇒ 教训：`--install` 的判据必须是**能跑的**（dry-run 也算），
        #     只测 `execution_time_limit_sec()` 这个纯函数是测不到"接线断了"的。
        limit_sec = execution_time_limit_sec(spec)
        trig = ["$triggers += New-ScheduledTaskTrigger -AtStartup"]
        if spec.needs_boot_trigger:
            # 开机 + 登录两处都补：开机触发若因故没跑成（磁盘就绪、依赖没起来），
            # 登录还能补一次。两者都是幂等的（`manage.py start` 已在跑时是 no-op）。
            trig.append("$triggers += New-ScheduledTaskTrigger -AtLogOn")
        if spec.interval_sec:
            trig.append(
                "$tick = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
                f"-RepetitionInterval (New-TimeSpan -Seconds {spec.interval_sec})")
            trig.append("$triggers += $tick")
        script = "; ".join([
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute '{execute}' "
            f"-Argument '{arguments}' -WorkingDirectory '{ROOT}'",
            "$triggers = @()", *trig,
            "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' "
            "-LogonType ServiceAccount -RunLevel Highest",
            # ★ `CHG-0239`：`ExecutionTimeLimit` 改成**按间隔算**（`execution_time_limit_sec`）。
            #   原实现统一写 1 小时 —— 对每分钟一轮的值守，那等于"挂住后合法停摆一小时"
            #   （实测：上一实例挂着，后续 30+ 分钟的触发全被 `IgnoreNew` 丢弃）。
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-MultipleInstances IgnoreNew "
            f"-ExecutionTimeLimit (New-TimeSpan -Seconds {limit_sec})",
            f"Register-ScheduledTask -TaskName '{spec.name}' -Action $action "
            "-Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null",
            f"Write-Output 'REGISTERED {spec.name}'",
        ])
        if dry_run:
            print(f"— {spec.name}（dry-run）\n{script}\n")
            continue
        out = _ps(script)
        ok = f"REGISTERED {spec.name}" in out
        print(("✅ " if ok else "❌ ") + f"{spec.name}（{spec.kind}，"
              f"间隔 {spec.interval_sec or '—'}s）" + ("" if ok else f" → {out}"))
        rc = rc or (0 if ok else 1)
    return rc


# ======================================================================
# self-test：喂已知答案，确认检查器本身还可信
# ======================================================================
def self_test() -> int:
    """**检查器先自证**：喂"已知答案"的输入，看它报不报得出来。"""
    spec = REQUIRED_TASKS[1]          # MossPilotWatchdog：有间隔、无需开机触发器
    cases: list[tuple[str, Observed, str]] = [
        ("读不到必须是 UNKNOWN（不是 MISSING）",
         Observed(name=spec.name, exists=None, note="探针炸了"), UNKNOWN),
        ("不存在必须是 MISSING",
         Observed(name=spec.name, exists=False), MISSING),
        ("停用必须是 DISABLED",
         Observed(name=spec.name, exists=True, enabled=False), DISABLED),
        ("动作指向别的脚本必须是 ACTION_MISMATCH",
         Observed(name=spec.name, exists=True, enabled=True,
                  arguments='-File "D:\\x\\other.ps1"'), ACTION_MISMATCH),
        ("间隔被改必须是 INTERVAL_MISMATCH",
         Observed(name=spec.name, exists=True, enabled=True,
                  arguments=f'-File "{ROOT / spec.script}"', intervals_sec=(300,)),
         INTERVAL_MISMATCH),
        ("全都对必须是 OK",
         Observed(name=spec.name, exists=True, enabled=True,
                  arguments=f'-File "{ROOT / spec.script}"', intervals_sec=(60,)),
         OK),
        # ★ `CHG-0239`：**定义全对但不再触发**（2026-10-08 实测形态）
        ("挂住的实例 ⇒ 必须是 STALE",
         Observed(name=spec.name, exists=True, enabled=True,
                  arguments=f'-File "{ROOT / spec.script}"', intervals_sec=(60,),
                  last_run_at=datetime(2026, 10, 8, 22, 23, 2), last_result=267009),
         STALE),
        ("刚刚跑过 ⇒ 仍然 OK（不能假红）",
         Observed(name=spec.name, exists=True, enabled=True,
                  arguments=f'-File "{ROOT / spec.script}"', intervals_sec=(60,),
                  last_run_at=datetime(2026, 10, 8, 22, 47, 30), last_result=0),
         OK),
        ("运行史**读不到** ⇒ 不许猜，仍然 OK",
         Observed(name=spec.name, exists=True, enabled=True,
                  arguments=f'-File "{ROOT / spec.script}"', intervals_sec=(60,),
                  last_run_at=None),
         OK),
    ]
    fails = []
    for title, obs, want in cases:
        got = evaluate(spec, obs, now=datetime(2026, 10, 8, 22, 48, 0)).state
        flag = "✅" if got == want else "❌"
        print(f"  {flag} {title}：期望 {want}，实得 {got}")
        if got != want:
            fails.append(title)

    # ★ `CHG-0239`：`ExecutionTimeLimit` 的算法（原实现统一 1 小时 ⇒ 挂住可合法停摆一小时）
    limits = [(REQUIRED_TASKS[1], 180),      # 60s 一轮的看守 ⇒ 3 分钟
              (REQUIRED_TASKS[2], 900),      # 300s 一轮的链路自愈 ⇒ 15 分钟（上限）
              (REQUIRED_TASKS[3], 900),      # 30 分钟一轮的自检 ⇒ 15 分钟
              (REQUIRED_TASKS[0], 900)]      # 只在开机触发 ⇒ 15 分钟
    for task, want in limits:
        got = execution_time_limit_sec(task)
        flag = "✅" if got == want else "❌"
        print(f"  {flag} 执行时限 {task.name}：期望 {want}s，实得 {got}s")
        if got != want:
            fails.append(f"limit {task.name}")

    # ★ 时间戳解析：1999-11-30 是"从未运行"的哨兵值，**不能**当成真实时间
    ts = [("2026-10-08T22:25:02.0000000", datetime(2026, 10, 8, 22, 25, 2)),
          ("1999-11-30T00:00:00.0000000", None),
          ("", None), ("垃圾", None)]
    for text, want in ts:
        got = _parse_ts(text)
        flag = "✅" if got == want else "❌"
        print(f"  {flag} 时间戳 {text!r}：期望 {want}，实得 {got}")
        if got != want:
            fails.append(f"ts {text!r}")

    iso = [("PT1M", 60), ("PT5M", 300), ("PT30M", 1800), ("PT1H", 3600),
           ("P1DT2H", 93600), ("PT1H30M", 5400), ("", None), ("垃圾", None)]
    for text, want in iso:
        got = parse_iso_duration(text)
        flag = "✅" if got == want else "❌"
        print(f"  {flag} ISO 时长 {text!r}：期望 {want}，实得 {got}")
        if got != want:
            fails.append(f"ISO {text!r}")

    # 闸门：同一组故障第二次必须被冷却挡住，且原因是人话 + 剩余时间
    now = datetime(2026, 10, 5, 10, 0, 0)
    state = GateState()
    send1, _, state = decide_notify("A:MISSING", now, state)
    send2, why2, _ = decide_notify("A:MISSING", now + timedelta(minutes=5), state)
    checks = [
        ("首次故障必须发", send1 is True),
        ("5 分钟后同一故障必须被冷却挡住", send2 is False),
        ("抑制原因必须是人话并带剩余时间", "还剩" in why2 and "分钟" in why2),
        ("Quiet hours 必须能挡住",
         decide_notify("B:MISSING", datetime(2026, 10, 5, 2, 0, 0),
                       GateState(), quiet_hours=(23, 7))[0] is False),
    ]
    for title, ok in checks:
        print(f"  {'✅' if ok else '❌'} {title}")
        if not ok:
            fails.append(title)

    print(f"\n  self-test：{'全部通过' if not fails else f'{len(fails)} 项失败 → {fails}'}")
    return 1 if fails else 0


# ======================================================================
# CLI
# ======================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Moss 自启/值守计划任务的唯一事实源与存在性自检")
    ap.add_argument("--check", action="store_true", help="体检（默认动作）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--notify", action="store_true",
                    help="不达标时按三层闸门发邮件（未配置通道→明确说 unconfigured）")
    ap.add_argument("--log", action="store_true", help="把本轮结论追加到自检流水")
    ap.add_argument("--install", action="store_true",
                    help="按本表幂等注册（会在本机留下持久对象，需人确认）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的注册脚本")
    ap.add_argument("--self-test", action="store_true", help="检查器自证")
    ap.add_argument("--print-required", action="store_true", help="打印本表")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.install or args.dry_run:
        return install(dry_run=args.dry_run)
    if args.print_required:
        for spec in REQUIRED_TASKS:
            print(f"{spec.name}\t{spec.script or '(本模块)'}\t{spec.kind}\t"
                  f"{spec.interval_sec or '—'}\tinstallable={spec.installable}\t{spec.why}")
        return 0

    report = check()

    if args.json:
        print(json.dumps({
            "platform_note": report.platform_note,
            "exit_code": report.exit_code,
            "verdicts": [asdict(v) for v in report.verdicts],
        }, ensure_ascii=False, indent=2))
    else:
        print(render(report))

    if args.log:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary = "；".join(f"{v.name}={v.state}" for v in report.verdicts)
        append_log(f"{stamp} rc={report.exit_code} {summary or report.platform_note}")

    if args.notify and report.exit_code != EXIT_OK:
        cfg = mail_config(ROOT / ".env")
        fp = fingerprint_of(report)
        state = GateState.load(STATE_PATH)
        send, why, state = decide_notify(fp, datetime.now(), state)
        if not send:
            print(f"\n  ⏸ 未发通知：{why}")
            if args.log:
                append_log(f"{datetime.now():%Y-%m-%d %H:%M:%S} notify_suppressed {why}")
        else:
            state.save(STATE_PATH)
            subject = ("【Moss 运维】自启/值守任务不达标"
                       if report.exit_code == EXIT_BROKEN
                       else "【Moss 运维】**无法判定**自启/值守任务是否达标")
            body = render(report) + "\n\n（本邮件由 scripts/moss_autostart.py 发出）"
            status, detail = send_mail(subject, body, cfg)
            print(f"\n  通知：{status} —— {detail}")
            if args.log:
                append_log(f"{datetime.now():%Y-%m-%d %H:%M:%S} notify_{status} {detail}")
            if status != "sent":
                # 告警发不出去**本身**就是事故：不能只看退出码就宣布"已通知"
                return EXIT_UNKNOWN
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
