"""云隧道值守的**回归测试** —— 钉住"绝不自己起 cloudflared 进程"。

## 2026-09-26 实测事故

`manage.py tunnel-ensure` 原来在探测失败后做的是：

    taskkill 全部 cloudflared  →  自己 Start-Process 起一个

而本机 `cloudflared` 是**以 Windows 服务方式运行**的：

    Cloudflared 服务   StartMode = Auto
    SERVICE_START_NAME = LocalSystem
    FAILURE_ACTIONS    = RESTART -- Delay = 20000 milliseconds

于是形成死循环：

    我们杀掉服务进程 → SCM 20 秒后拉起一个（PID A）
                     → 我们又起一个（PID B）＝ **两个实例**

两个实例各自向 Cloudflare 注册连接器（实测 `tunnel info` 里两个 connector
创建时间相差 16 秒），互相抢同一个隧道。结果：

- **重启没有修好问题**（两次 `restart_ineffective`）
- 而且**重启本身成了不稳定源**：干掉一个、生出两个

## 这个文件钉住什么

1. 重启路径**只走服务**（`Restart-Service` / `Start-Service`）。
2. 代码里**不许再出现**"枚举 cloudflared 进程再逐个杀"的写法。
3. 探测判据：**4xx 算链路通**（能拿到业务错误码说明请求走完了全程），
   只有连不上/超时/5xx 才算失败 —— 否则探测路径被登录门槛拦下时会误重启。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import manage


def _tunnel_ensure_source() -> str:
    return inspect.getsource(manage.cmd_tunnel_ensure)


# ======================================================================
# ① 重启必须走服务，不许自己起进程
# ======================================================================

def test_restart_uses_windows_service_not_own_process() -> None:
    """★ 重启路径必须用 `Restart-Service` / `Start-Service`。

    自己 `Start-Process` 起 cloudflared 会与 SCM 的自动恢复打架，
    造出两个实例（实测）。这条断言是防止那次事故复发的第一道闸。
    """
    src = _tunnel_ensure_source()
    assert "Restart-Service" in src, "服务在跑但不通时应 Restart-Service"
    assert "Start-Service" in src, "服务停了时应 Start-Service"
    assert "_service_state" in src, "应先问 SCM 服务状态，而不是枚举进程"


def test_no_process_kill_or_spawn_in_tunnel_restart() -> None:
    """★ 重启路径里**不许**出现杀进程 / 自起进程。

    用 AST 提取函数体、剔除注释与字符串后检查可执行代码 ——
    注释里会解释"为什么不能这么做"，把注释一起查会误判。
    """
    tree = ast.parse(pathlib.Path("manage.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_tunnel_ensure")
    # 剔除所有字符串常量（注释不在 AST 里，但 docstring 与提示文案在）
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                child.value = ""
    code = ast.unparse(fn)

    assert "taskkill" not in code, (
        "tunnel-ensure 里不许再杀 cloudflared 进程 —— 那会与 SCM 的自动恢复"
        "打架，造出两个实例（2026-09-26 实测事故）")
    assert "Popen" not in code, (
        "tunnel-ensure 不许自己起 cloudflared —— 它的生命周期归 SCM 管")
    assert "old_pids" not in code, "不要再去枚举 cloudflared 的 PID"


def test_service_state_helper_queries_scm() -> None:
    """`_service_state` 必须问 SCM（`Get-Service`），而不是枚举同名进程。"""
    src = inspect.getsource(manage._service_state)
    assert "Get-Service" in src
    assert "Get-Process" not in src


# ======================================================================
# ② 探测判据：4xx 算通
# ======================================================================

def test_probe_treats_4xx_as_reachable() -> None:
    """★ 4xx 必须算"链路通"。

    能拿到业务错误码，说明请求走完了 cloudflared → 后端 → 回来整条路。
    把 401/403 当失败会在"探测路径被登录门槛拦下"时**误重启隧道**，
    而重启会中断所有在途请求（实测踩到，白重启了一次）。
    """
    src = _tunnel_ensure_source()
    assert "HTTPError" in src, "必须单独捕获 HTTPError 才能区分 4xx/5xx"
    assert "400 <= exc.code < 500" in src, "4xx 应判为可达"


def test_probe_uses_public_path_by_default() -> None:
    """默认探测地址应当是公开路径（登录门槛白名单里那条）。"""
    src = _tunnel_ensure_source()
    assert "/api/v1/health/live" in src


# ======================================================================
# ③ 事故必须留痕
# ======================================================================

def test_restart_is_recorded_with_memory_snapshot() -> None:
    """每次重启都要记流水 + 内存现场（事后无法回溯的运行期状态）。"""
    src = _tunnel_ensure_source()
    assert "_record_tunnel_incident" in src
    assert "memory_snapshot" in src
    assert "restart_ineffective" in src, (
        "重启后仍不通要如实记 —— '重启了但没用'是最需要知道的情况")


def test_service_name_is_cloudflared() -> None:
    """服务名必须与实测一致（`sc query Cloudflared`）。"""
    assert 'service = "Cloudflared"' in _tunnel_ensure_source()
