"""拥挤度**自动定时刷新**的判据（`CHG-0144`）。

## 背景

`sector_crowding_daily` 此前**没有任何调度作业更新它** —— 实测数据停在
`20260924`、最后写入 2026-09-27T18:52，而仓库里最新交易日是 `20260929`。
也就是说「一键刷新」**只能靠人手动点**，没人点就一直是旧的。

用户要求（2026-09-30 两轮）：

> 「一键刷新，应该自动在闲时，搞个自动定时调度脚本（**不依赖服务是否启动**），
>   一键刷新，每周在服务启动时后端自动刷两次，避开早上 9 点到 10:30 交易时间。」
>
> 「**设置一周最大调用 2 次**」（成本上限）

## 这组判据守什么

| # | 判据 | 防的失效 |
|---|---|---|
| ① | ★ **一周调用次数 ≤ 2** | 成本上限被无声突破（`EveryDay` 一加就是 7 次） |
| ② | ★ **没有任何触发落在 09:00~10:30** | 用户点名的交易时段被占用 |
| ③ | ★ `.ps1` 必须调 `manage.py crowding-refresh --env dev` | 包装指向别的命令 / 别的环境 |
| ④ | ★ `manage.py` 里**必须**有 `crowding-refresh` 子命令 | 计划任务指向一个不存在的子命令（静默失败） |
| ⑤ | ★ 写者判定在**目标环境里**求值、非写者 fail-closed | 计划任务配错环境却"看起来成功" |
| ⑥ | ★ 任务**真的注册上了**（查不到则 `skip`） | 改了脚本却忘了 `--register` |

## 关于 ⑥ 为什么是 `skip` 而不是红灯

计划任务是**机器本地状态**，克隆仓库的 CI 上必然没有它。
这与本项目既有的"知名降级"约定一致（`docs/PRD.md` §21 的交付完整性硬约束：
未随仓库发布的东西要**显式 skip**，不是静默绿灯、也不是必红）。
`--register` 之后本机应当**不再是 skip** —— 那才是它有意义的地方。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PS1 = ROOT / "scripts" / "crowding_refresh.ps1"
SETUP = ROOT / "scripts" / "setup_crowding_refresh_task.py"
MANAGE = ROOT / "manage.py"
TASK_NAME = "MossCrowdingRefresh"

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _load_setup():
    """把 `setup_crowding_refresh_task.py` 当模块导入（它是脚本，不在包里）。"""
    spec = importlib.util.spec_from_file_location("_setup_crowding_task", SETUP)
    assert spec and spec.loader, f"无法加载 {SETUP}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ====================================================== ① 成本上限

def test_weekly_run_budget_is_respected() -> None:
    """★ **成本硬约束**：默认触发配置一周不得超过 2 次。

    用户原话：「拥挤度会花费 tokens 和搜索用量，**设置一周最大调用 2 次**」。
    实测更正一处口径：拥挤度**不调 LLM、不花 token**，真实成本是
    **Tushare 外部配额**（每板块 1 次 `ths_daily`）。但上限是用户的决定，
    所以这里按"次数"守。
    """
    module = _load_setup()
    count = module.weekly_run_count()
    assert count <= module.MAX_RUNS_PER_WEEK, (
        f"默认触发配置折算后是**一周 {count} 次**，超过上限 "
        f"{module.MAX_RUNS_PER_WEEK} 次 —— 用户明确要求最多 2 次")
    assert module.MAX_RUNS_PER_WEEK == 2, (
        "上限本身被改动了；改它必须同步 PRD 与用户确认")


def test_daily_trigger_would_break_the_budget() -> None:
    """★ **反向断言**：把 `EveryDay` 加进去，预算守卫**必须**拦住。

    这条是"判据自己还可信"的证明 —— 只会报 OK 的检查等于没有检查。
    （实测：加一个 `EveryDay` 从 2 次变成 9 次。）
    """
    module = _load_setup()
    greedy = (("Tuesday", 11, 0), ("Saturday", 11, 0), ("EveryDay", 21, 0))
    assert module.weekly_run_count(greedy) == 9, "折算逻辑不对"
    with pytest.raises(ValueError) as excinfo:
        module._enforce_weekly_budget(greedy)
    assert "一周 9 次" in str(excinfo.value)


def test_every_day_counts_as_seven() -> None:
    """`EveryDay` 必须折算成 7 次 —— 这是"每天一次"没被当成"一次"的关键。"""
    module = _load_setup()
    assert module.weekly_run_count((("EveryDay", 11, 0),)) == 7


# ====================================================== ② 避开交易时段

def test_no_trigger_inside_the_blocked_trading_window() -> None:
    """★ 没有任何触发落在用户点名的 **09:00~10:30** 内。"""
    module = _load_setup()
    module._enforce_blocked_window()          # 默认配置必须通过
    fh, fm = module.BLOCKED_FROM
    th, tm = module.BLOCKED_TO
    assert (fh, fm) == (9, 0) and (th, tm) == (10, 30), (
        "被避开的时段被改了；那是用户点名的口径（「避开早上 9 点到 10:30」）")


def test_blocked_window_guard_actually_fires() -> None:
    """★ **反向断言**：把一个触发放进 09:30，守卫必须拦住。"""
    module = _load_setup()
    with pytest.raises(ValueError) as excinfo:
        module._enforce_blocked_window((("Tuesday", 9, 30),))
    assert "交易时段" in str(excinfo.value)


def test_triggers_are_exactly_two_per_week() -> None:
    """默认配置就是**每周两次**（周二 + 周六），不是一次也不是三次。"""
    module = _load_setup()
    assert len(module.TRIGGERS) == 2, f"触发个数不是 2：{module.TRIGGERS}"
    days = [d for d, _h, _m in module.TRIGGERS]
    assert days == ["Tuesday", "Saturday"], f"触发的星期不是周二+周六：{days}"
    for _d, hour, minute in module.TRIGGERS:
        assert (hour, minute) == (11, 0), f"触发时刻不是 11:00：{hour}:{minute}"


# ====================================================== ③ .ps1 包装

def test_ps1_calls_the_right_manage_subcommand() -> None:
    """★ `.ps1` 必须调 `manage.py crowding-refresh --env dev`。

    为什么钉 `--env dev`：拥挤度参考数据的写者在 registry 里点名的就是
    **dev**（`crowding_shared.writer: dev`）。用别的环境跑会被写者闸门
    fail-closed 拒掉（退出码 2）—— 那正是我们想要的，但**不该由计划任务触发**。
    """
    src = PS1.read_text(encoding="utf-8")
    assert "crowding-refresh" in src, "包装没有调 crowding-refresh"
    assert "--env dev" in src, (
        "包装没有显式指定 --env dev —— 拥挤度参考数据的写者是 dev")
    # 必须走 .venv 的解释器（不能用系统 python：依赖不在那儿）
    assert ".venv\\Scripts\\python.exe" in src or ".venv/Scripts/python.exe" in src
    assert "manage.py" in src


def test_ps1_has_single_instance_lock() -> None:
    """必须有单实例锁 —— 两个触发撞上时不能并发写同一个库。"""
    src = PS1.read_text(encoding="utf-8")
    assert "File]::Open" in src, "包装没有单实例锁"
    assert "OpenOrCreate" in src


def test_ps1_does_not_swallow_failures() -> None:
    """★ 退出码必须**透出去**，不能一律 `exit 0`。

    `pilot_watchdog.ps1` 的头注释记着这条教训：原来一律 `exit 0`，
    于是"值守坏了"和"值守没事"在计划任务里长得一模一样
    （`LastTaskResult` 都是 0）—— 那是最难发现的静默失效。
    """
    src = PS1.read_text(encoding="utf-8")
    assert re.search(r"exit\s+\$code", src), (
        "包装没有把退出码透出去 —— 失败会伪装成成功")


# ====================================================== ④ manage 子命令存在

def test_manage_has_the_crowding_refresh_subcommand() -> None:
    """★ `manage.py` 里必须真的有 `crowding-refresh` 子命令。

    计划任务指向一个不存在的子命令时，`manage.py` 会**打印用法并以非 0 退出**，
    但计划任务只留一个退出码 —— 很容易被当成"偶发失败"。
    """
    src = MANAGE.read_text(encoding="utf-8")
    assert '"crowding-refresh"' in src, "manage.py 里没有 crowding-refresh 子命令"
    assert "def cmd_crowding_refresh" in src
    # 注册到 argparse（有子命令名还不够，得真的 set_defaults(func=...)）
    assert "func=cmd_crowding_refresh" in src, (
        "子命令没有绑定处理函数 —— argparse 会打印帮助并退出")


def test_manage_command_checks_writer_in_target_env() -> None:
    """★ 写者判定必须**在目标环境里**求值（`CHG-0112` 的老形状）。

    `manage.py` 是计划任务起的：父进程里没有 `MOSS_ENV`/`MOSS_SQLITE_PATH`
    ⇒ `current_env()` 退化成 dev、`is_main_instance()` 判成 True。
    这个坑本项目出现过**至少三次**。所以整段逻辑必须包在
    `_temporary_environ(extra_env)` 之内。
    """
    tree = ast.parse(MANAGE.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "cmd_crowding_refresh"), None)
    assert fn is not None, "找不到 cmd_crowding_refresh"

    withs = [n for n in ast.walk(fn) if isinstance(n, ast.With)]
    uses_temp_env = any(
        isinstance(item.context_expr, ast.Call)
        and getattr(item.context_expr.func, "id", "") == "_temporary_environ"
        for node in withs for item in node.items)
    assert uses_temp_env, (
        "cmd_crowding_refresh 没有在 _temporary_environ 里求值 —— "
        "写者判定会退化成父进程环境（CHG-0112 的老形状）")

    # 非写者必须 fail-closed（返回 2），不能静默当成功
    src = MANAGE.read_text(encoding="utf-8")
    seg = src[src.index("def cmd_crowding_refresh"):]
    seg = seg[:seg.index("\ndef ", 10)]
    assert "writable_here" in seg, "没有查写者归属"
    assert "return 2" in seg, "非写者没有 fail-closed（返回码）"


def test_manage_command_uses_sync_core_not_the_api_thread() -> None:
    """★ 必须调**同步核心** `refresh_all_incremental`，不能调 `start_refresh_all`。

    后者是"起后台线程 + 立即返回 task_id"（给前端轮询用的）——
    **线程随进程退出而死**，计划任务调它等于什么都没刷，而且**不报错**。

    ⚠️ 判据必须走 **AST**，不能用子串：`cmd_crowding_refresh` 的 docstring
    里**正要提到** `start_refresh_all`（说明"为什么不用它"），
    子串判据会把那句说明当成调用 —— 我第一次就是这么误报的。
    """
    tree = ast.parse(MANAGE.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "cmd_crowding_refresh"), None)
    assert fn is not None, "找不到 cmd_crowding_refresh"

    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    assert "refresh_all_incremental" in called, (
        "没有调同步核心 refresh_all_incremental")
    assert "start_refresh_all" not in called, (
        "调了 start_refresh_all —— 那是后台线程版，计划任务进程退出就没了")


# ====================================================== ⑥ 真机注册

def _query_task() -> dict | None:
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' "
        "-ErrorAction SilentlyContinue; "
        "if (-not $t) { Write-Output 'MISSING'; exit 0 }; "
        "$trg = $t.Triggers | ForEach-Object { "
        "  \"days=$($_.DaysOfWeek);at=$($_.StartBoundary)\" }; "
        "$o = [ordered]@{ triggers = @($trg); "
        "action = \"$($t.Actions[0].Arguments)\"; "
        "start_when_available = $t.Settings.StartWhenAvailable }; "
        "$o | ConvertTo-Json -Compress"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=90, creationflags=_NO_CONSOLE)
    out = (done.stdout or b"").decode("utf-8", errors="replace").strip()
    if not out or out == "MISSING":
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


@pytest.mark.skipif(sys.platform != "win32", reason="计划任务只在 Windows 上")
def test_task_is_registered_on_this_machine() -> None:
    """★ 任务**真的注册上了**吗（本机状态；未注册则显式 skip）。

    这条把"改了脚本但忘了 `--register`"变成可见的 —— 那是本任务最可能的
    操作遗漏（脚本改了、任务还是旧的，或根本没建）。

    ⚠️ CI / 克隆仓库上必然没有这个任务，所以是 **skip** 而不是红 ——
    与本项目"未随仓库发布的东西要显式降级"的约定一致。
    """
    info = _query_task()
    if info is None:
        pytest.skip(
            f"计划任务 {TASK_NAME} 未在本机注册 —— 这是**没装**，不是坏了。\n"
            f"注册：python scripts/setup_crowding_refresh_task.py --register")
    assert info.get("start_when_available") is True, (
        "缺少 StartWhenAvailable —— 错过的触发不会在开机后补跑，"
        "而'电脑大概率没开机'正是要防的场景")
    assert "crowding_refresh.ps1" in str(info.get("action") or ""), (
        f"任务动作指向的不是本包装脚本：{info.get('action')}")


@pytest.mark.skipif(sys.platform != "win32", reason="计划任务只在 Windows 上")
def test_registered_task_has_exactly_two_weekly_triggers() -> None:
    """★ 注册后的任务**真的**是每周两次、11:00，且都不在 09:00~10:30。

    只断言脚本里的常量还不够 —— 任务可能还是用旧的触发配置注册的
    （`Register-ScheduledTask -Force` 之前留下的）。
    """
    info = _query_task()
    if info is None:
        pytest.skip(f"计划任务 {TASK_NAME} 未注册（见上一条）")
    triggers = info.get("triggers") or []
    assert len(triggers) == 2, (
        f"注册的触发不是 2 个而是 {len(triggers)} 个：{triggers}")
    # `DaysOfWeek` 是位掩码：1<<2=4 周二，1<<6=64 周六
    days = sorted(int(str(t).split("days=")[1].split(";")[0])
                  for t in triggers)
    assert days == [4, 64], (
        f"触发的星期不是周二(4)+周六(64)：{days}（原始 {triggers}）")
    for t in triggers:
        at = str(t).split("at=")[1]
        hour, minute = int(at[11:13]), int(at[14:16])
        assert (hour, minute) == (11, 0), f"触发时刻不是 11:00：{at}"
        assert not (9, 0) <= (hour, minute) <= (10, 30), (
            f"触发落在用户点名的交易时段内：{at}")
