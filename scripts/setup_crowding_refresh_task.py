"""注册 / 查看 / 撤销「板块拥挤度自动刷新」计划任务。

## 为什么这是 OS 级任务，而不是服务内调度

用户口径：「**不依赖服务是否启动**」。

服务内调度器（`src/scheduler/`）做不到这一点 —— 进程不在就什么都不跑。
而拥挤度参考数据的写者是 **dev 实例**（`configs/data_stores.yaml` 的
`crowding_shared.writer: dev`），dev 并不常驻。
所以走 Windows 计划任务：**开机后一直在**，与后端进程无关。

## 触发时刻（**每周两次 —— 用户定的成本上限**）

| 时刻 | 为什么是这个点 |
|---|---|
| 每周二 11:00 | 周中一次，拿到周一收盘；11:00 在午间休市窗口内 |
| 每周六 11:00 | 周末一次，拿到周五收盘；周六 11:00 **完全空闲**（那天唯一作业是 20:00 的 `model_retrain_weekly`） |

★ 用户 2026-09-30 原话：「拥挤度会花费 tokens 和搜索用量，
**设置一周最大调用 2 次**」。

实测更正一处口径：**拥挤度刷新完全不调用 LLM**（`src/sector_crowding/` 下
`llm|openai|deepseek|embedding` 零命中，`crowding_metrics_weekly` 同样），
所以它**不花 token**。真实成本是 **Tushare 外部配额**：
每板块 1 次 `ths_daily`（`sources.py:233`）+ 1 次 `ths_index` + 按需 `ths_member`。

**上限是用户的决定，所以做成了硬约束**：`_enforce_weekly_budget()` 在
**注册入口**直接拒绝超过 2 次的配置（`AGENTS.md`：「上限必须写进代码，
不能留在注释里」）。

> 曾经考虑过「每天 21:00」那一档（理由是"晚上机器大概率开着"），
> **已被这条上限否掉**：`EveryDay` 折算成周频就是 **7 次**。
> 若哪天机器在 11:00 常关着，正确做法是**把这两次挪到机器开着的时段**
> （例如周六 11:00 + 周六 21:00），而**不是**增加次数。

## 用法

```powershell
python scripts/setup_crowding_refresh_task.py            # 查看当前状态
python scripts/setup_crowding_refresh_task.py --register # 注册（幂等，覆盖旧的）
python scripts/setup_crowding_refresh_task.py --delete   # 撤销
python scripts/setup_crowding_refresh_task.py --run      # 立刻跑一次（验收用）
```

## 判据

`tests/unit/test_crowding_refresh_schedule.py` 钉住四件事：
① 一周调用次数 **≤ 2**（成本上限）；
② 没有任何触发落在 **09:00~10:30**；
③ `.ps1` 包装确实调 `manage.py crowding-refresh --env dev`（不是别的命令）；
④ 任务**真的注册上了**（查不到则 `skip` —— 那是"没装"而不是"坏了"，
   与项目"知名降级"的约定一致；`--register` 后应当不再是 skip）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "crowding_refresh.ps1"
TASK_NAME = "MossCrowdingRefresh"

#: 触发时刻：`(周几, 时, 分)`；`"EveryDay"` 表示每天。
#: **唯一事实源** —— `_enforce_weekly_budget()` 与判据都从这里读，不另抄一份。
#:
#: ## 为什么是"每周两次"（用户 2026-09-30 定的硬上限）
#:
#: 用户原话：「拥挤度会花费 **tokens 和搜索用量**，设置**一周最大调用 2 次**」。
#:
#: ★ 实测更正一处：**拥挤度刷新完全不调用 LLM**（`src/sector_crowding/` 下
#: `llm|openai|deepseek|embedding` **零命中**，`crowding_metrics_weekly` 同样），
#: 所以它**不花 token**。真实成本是 **Tushare 外部配额**：
#:
#:     sources.py:149  ths_index   （板块列表，1 次）
#:     sources.py:233  ths_daily   （每板块 1 次 —— 这是大头）
#:     sources.py:274  ths_member  （成分股，按需）
#:
#: 无论成本口径叫什么，**上限是用户的决定**，所以做成**可断言的硬约束**
#: （`_enforce_weekly_budget` 直接拒绝超过 2 次的配置），不是注释里的提醒。
TRIGGERS: tuple[tuple[str, int, int], ...] = (
    # 周二 11:00 —— 周中一次，拿到周一收盘；11:00 在午间休市窗口内
    ("Tuesday", 11, 0),
    # 周六 11:00 —— 周末一次，拿到周五收盘；周六 11:00 完全空闲
    ("Saturday", 11, 0),
)

#: 用户定的**每周最大调用次数**。超过即拒绝注册（`_enforce_weekly_budget`）。
MAX_RUNS_PER_WEEK = 2

#: 用户点名要避开的交易时段（含端点）。判据会断言**没有任何触发落进去**。
BLOCKED_FROM = (9, 0)
BLOCKED_TO = (10, 30)

#: 每天触发的权重（用于折算"一周几次"）。
_DAYS_PER_WEEK = 7

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def weekly_run_count(
        triggers: tuple[tuple[str, int, int], ...] = TRIGGERS) -> int:
    """把触发表折算成"一周跑几次"。

    `"EveryDay"` 记 7 次 —— 它是"每天一次"，折算成周频就是 7。
    （这正是用户要否掉的那一档：`EveryDay` 一加就是一星期 7 次调用。）
    """
    return sum(_DAYS_PER_WEEK if dow == "EveryDay" else 1
               for dow, _h, _m in triggers)


def _enforce_weekly_budget(
        triggers: tuple[tuple[str, int, int], ...] = TRIGGERS) -> None:
    """★ **成本硬约束**：一周调用次数不得超过 `MAX_RUNS_PER_WEEK`。

    ## 为什么做成"抛异常"而不是"打个警告"

    用户 2026-09-30 原话：「拥挤度会花费 tokens 和搜索用量，
    **设置一周最大调用 2 次**」——这是一个**上限**，而本项目的纪律是
    「**上限必须写进代码，不能留在注释里**」（`AGENTS.md`：
    「任何『一般不会超过…』都是缺陷」）。

    所以放在**注册入口**：超了就直接拒绝注册，而不是注册完再提醒。
    注册是本模块唯一能让调用真的发生的动作，卡在这里就是卡住了成本。
    """
    count = weekly_run_count(triggers)
    if count > MAX_RUNS_PER_WEEK:
        raise ValueError(
            f"触发配置折算后是**一周 {count} 次**，超过用户定的上限 "
            f"{MAX_RUNS_PER_WEEK} 次。\n"
            f"  当前触发表：{triggers}\n"
            f"  拥挤度刷新消耗 **Tushare 外部配额**（每板块 1 次 ths_daily），\n"
            f"  所以调用次数是有预算的，不是「多刷几次更保险」。\n"
            f"  要改上限请改本文件的 `MAX_RUNS_PER_WEEK` 并同步 PRD。")


def _enforce_blocked_window(
        triggers: tuple[tuple[str, int, int], ...] = TRIGGERS) -> None:
    """★ 没有任何触发可以落在用户点名的 09:00~10:30 交易时段内。"""
    fh, fm = BLOCKED_FROM
    th, tm = BLOCKED_TO
    for dow, hour, minute in triggers:
        if (fh, fm) <= (hour, minute) <= (th, tm):
            raise ValueError(
                f"触发 {dow} {hour:02d}:{minute:02d} 落在交易时段 "
                f"{fh:02d}:{fm:02d}~{th:02d}:{tm:02d} 内 —— 用户明确要求避开。")


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=120, creationflags=_NO_CONSOLE)


def _register_script() -> str:
    """生成 `Register-ScheduledTask` 的 PowerShell 片段。

    三个触发器 + 一个动作。动作与项目既有任务**逐字同形**
    （`MossTunnelWatchdog` 的 `powershell.exe -NoProfile -NonInteractive
    -WindowStyle Hidden -ExecutionPolicy Bypass -File …`）——
    新造一种写法就会多一份要维护的东西。
    """
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' "
        f"-Argument '-NoProfile -NonInteractive -WindowStyle Hidden "
        f"-ExecutionPolicy Bypass -File \"{SCRIPT}\"'",
        "$triggers = @()",
    ]
    for dow, hour, minute in TRIGGERS:
        at = f"{hour:02d}:{minute:02d}"
        if dow == "EveryDay":
            lines.append(
                f"$triggers += New-ScheduledTaskTrigger -Daily -At '{at}'")
        else:
            lines.append(
                f"$triggers += New-ScheduledTaskTrigger -Weekly "
                f"-DaysOfWeek {dow} -At '{at}'")
    lines += [
        # 与既有任务一致：SYSTEM 身份、最高权限（本项目全部后台任务都是这个组合）
        "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' "
        "-LogonType ServiceAccount -RunLevel Highest",
        # `StartWhenAvailable`：错过的触发在机器开机后**补跑**。
        # 这正是"电脑大概率没开机"的直接对策 —— 11:00 错过就开机后补上，
        # 而不是静默丢掉一整个窗口。
        "$settings = New-ScheduledTaskSettingsSet "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -MultipleInstances IgnoreNew",
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' "
        "-Action $action -Trigger $triggers -Principal $principal "
        "-Settings $settings -Force | Out-Null",
        f"Write-Output 'REGISTERED {TASK_NAME}'",
    ]
    return "; ".join(lines)


def _query() -> dict | None:
    """查任务当前状态；不存在返回 None。"""
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue; "
        "if (-not $t) { Write-Output 'MISSING'; exit 0 }; "
        "$i = Get-ScheduledTaskInfo -TaskName '" + TASK_NAME + "'; "
        "$trg = $t.Triggers | ForEach-Object { "
        "  if ($_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger') "
        "    { \"EveryDay $($_.StartBoundary)\" } "
        "  else { \"$($_.DaysOfWeek) $($_.StartBoundary)\" } }; "
        "$o = [ordered]@{ state = \"$($t.State)\"; "
        "last = \"$($i.LastRunTime)\"; result = $i.LastTaskResult; "
        "next = \"$($i.NextRunTime)\"; triggers = @($trg) }; "
        "$o | ConvertTo-Json -Compress"
    )
    done = _ps(script)
    out = (done.stdout or b"").decode("utf-8", errors="replace").strip()
    if not out or out == "MISSING":
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拥挤度自动刷新计划任务")
    parser.add_argument("--register", action="store_true", help="注册（幂等）")
    parser.add_argument("--delete", action="store_true", help="撤销")
    parser.add_argument("--run", action="store_true", help="立刻触发一次")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("❌ 仅 Windows 支持计划任务。", file=sys.stderr)
        return 1
    if not SCRIPT.is_file():
        print(f"❌ 包装脚本不存在：{SCRIPT}", file=sys.stderr)
        return 1

    if args.delete:
        done = _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
                   f"-Confirm:$false -ErrorAction SilentlyContinue; "
                   f"Write-Output 'DELETED'")
        print((done.stdout or b"").decode("utf-8", "replace").strip())
        return 0

    if args.register:
        # ★ 成本与时段约束在**注册这一步**先把关 —— 这是唯一能让调用真的发生的动作
        try:
            _enforce_weekly_budget()
            _enforce_blocked_window()
        except ValueError as exc:
            print(f"❌ 拒绝注册：{exc}", file=sys.stderr)
            return 1
        done = _ps(_register_script())
        if done.returncode != 0:
            print((done.stderr or b"").decode("utf-8", "replace")[-800:],
                  file=sys.stderr)
            return 1
        print((done.stdout or b"").decode("utf-8", "replace").strip())

    if args.run:
        _ps(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")
        print(f"▶ 已触发 {TASK_NAME}（结果见 data/run/crowding-refresh.log）")

    info = _query()
    print()
    if info is None:
        print(f"⚠️ 任务 {TASK_NAME} **未注册**。")
        print(f"   注册：python {Path(__file__).name} --register")
        return 0
    print(f"任务 {TASK_NAME}")
    print(f"  状态      : {info.get('state')}")
    print(f"  上次运行  : {info.get('last')}  结果={info.get('result')}")
    print(f"  下次运行  : {info.get('next')}")
    print(f"  触发时刻  : {info.get('triggers')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
