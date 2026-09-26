#!/usr/bin/env python
"""Moss-FinAgent-Research 统一管理入口（仅标准库，零新增依赖）。

用法：
  python manage.py start [--port 8100] [--reload] [--daemon] [--with-frontend] [--replace]
  python manage.py stop [--all]
  python manage.py status
  python manage.py test [pytest参数...]      # 隔离临时数据目录，绝不污染生产 data/
  python manage.py frontend                  # 仅启动前端 dev server (5173)
  python manage.py build                     # 构建前端到 web/dist（后端可同服务托管）

安全原则（参考 moss-finance-assistant 进程管理）：
  1. 只处理 LISTENING 占用（TIME_WAIT 不影响新 bind，不误判）；
  2. 仅当 /health 签名或进程命令行证实是"本项目旧实例"时才允许停止，
     其他程序占用端口一律拒绝并给出诊断，绝不全局 taskkill；
  3. 停止按 PID 精确树杀（taskkill /T /F /PID），不用进程名通杀。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "data" / "run"
WEB_DIR = ROOT / "web"

SERVICE_SIGNATURE = "moss-finagent-research"

#: Windows 上让子进程不弹控制台窗口（等价 CREATE_NO_WINDOW 0x08000000）。
#: 只取输出的探测类调用必须带上它：父进程（DSH/服务/守护进程）没有可继承的
#: 控制台时，Windows 会给子进程**新分配一个可见控制台**，窗口标题就是
#: python.exe 的全路径 —— 表现为桌面反复弹出黑窗。
#: 前台可交互命令（uvicorn 前台、npm dev server）不要带，它们需要用户的控制台。
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
DEFAULT_BACKEND_PORT = 8100
DEFAULT_FRONTEND_PORT = 5173
OLLAMA_PORT = 11434
QMT_PORT = 58610


# ======================================================================
# 纯函数（可单测，不做真实网络/进程操作）
# ======================================================================

def is_our_cmdline(cmdline: str | None) -> bool:
    """命令行是否属于本项目后端实例（uvicorn src.api.main:app）。"""
    if not cmdline:
        return False
    low = cmdline.lower()
    return "src.api.main:app" in low or "src\\api\\main" in low


def build_test_env(base_dir: str) -> dict[str, str]:
    """构造测试隔离环境变量：DB/缓存/审计/调度记录全部重定向到临时目录。

    pydantic-settings 按字段名（不区分大小写）读取这些环境变量，
    EventSqliteRepository 也经 settings.sqlite_path 构造，因此一处注入、
    全链路隔离；conftest 显式临时路径仍然优先，互不冲突。
    """
    return {
        "MOSS_ENV": "test",
        "SQLITE_PATH": f"{base_dir}/test.db",
        "REDIS_CACHE_ENABLED": "false",
        "LLM_CACHE_ENABLED": "false",  # 测试禁止跨运行复用缓存，保证可重复
        "LLM_AUDIT_DIR": f"{base_dir}/audit",
        "SCHEDULER_DIR": f"{base_dir}/scheduler",
    }


def read_pid_file(path: Path) -> int | None:
    """读取 PID 文件；内容非法/进程不存在时返回 None。"""
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or not _pid_alive(pid):
        return None
    return pid


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
            errors="replace",  # Chinese Windows tasklist output is GBK
            creationflags=_NO_CONSOLE,
        ).stdout or ""
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    """TCP 连接探测：True=端口可连（有服务监听）。"""
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((target, port)) == 0


def find_free_port(start: int, limit: int = 100) -> int:
    """从 start 起找第一个空闲端口。"""
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"{start}~{start + limit - 1} 无空闲端口")


# ======================================================================
# 端口占用诊断（Windows netstat + /health 签名双重识别）
# ======================================================================

def find_listening_pid(port: int) -> int | None:
    """netstat 查 LISTENING 端口对应 PID（跨平台尽力实现）。"""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=8,
                errors="replace",  # lenient decode: no exception, no lost lines
                creationflags=_NO_CONSOLE,
            ).stdout or ""
            targets = {f"0.0.0.0:{port}", f"127.0.0.1:{port}", f"[::]:{port}"}
            for line in out.splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[-2].upper() == "LISTENING":
                    local = cols[1].replace("::", "0.0.0.0")
                    # IPv6 [::]:8100 归一化后比对
                    norm = local.replace("[::]", "0.0.0.0")
                    if norm in targets:
                        return int(cols[-1])
        else:
            out = subprocess.run(
                ["bash", "-c", f"lsof -ti tcp:{port} -sTCP:LISTEN"],
                capture_output=True, text=True, timeout=8,
                creationflags=_NO_CONSOLE,
            ).stdout or ""
            if out.strip().isdigit():
                return int(out.strip().splitlines()[0])
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    return None


def get_cmdline(pid: int) -> str | None:
    """Windows 下取进程命令行（用于确认是否本项目实例）。"""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_CONSOLE,
        ).stdout.strip()
        return out or None
    except (subprocess.SubprocessError, OSError):
        return None


def health_signature(port: int) -> str | None:
    """判断该端口上的服务是不是**本项目**，是则返回 service 签名。

    两条证据，任一成立即可：

    ① `/api/v1/health`（深度健康检查，返回 `service` 字段）；
    ② `/api/v1/health/live`（**免鉴权**的 0 I/O 存活探针）。

    为什么必须加 ②（2026-09-23 加公网试点实例时踩到）：`/api/v1/health`
    会返回模型配置与数据源状态，属于内部拓扑，所以公网环境把它纳入了
    **登录门槛** → 未登录访问返回 **401**。如果只认 ①，那么在这台机器上
    `manage.py status` 会把一个**完全正常**的试点实例报成"被其他程序占用"，
    而 `manage.py stop` 会**拒绝停止它**（"非本项目实例"）——
    一个纯粹因为"健康检查需要登录"导致的运维故障，且症状指向完全错误的方向。

    ② 之所以也能当签名：`/api/v1/health/live` 精确返回
    `{"ok": true, "ts": ...}`，只有本项目的这个端点长这样；
    再叠加 `diagnose_port` 里的**命令行**证据（`uvicorn src.api.main:app`），
    两把钥匙同时要对上才会动手杀进程。
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v1/health", timeout=1.0
        ) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        svc = body.get("service")
        return svc if isinstance(svc, str) else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        pass
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v1/health/live", timeout=1.0
        ) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        if body.get("ok") is True and "ts" in body:
            return SERVICE_SIGNATURE
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def diagnose_port(port: int) -> dict[str, object]:
    """返回端口占用诊断：{occupied, pid, is_ours, cmdline}。"""
    if not port_open("127.0.0.1", port):
        return {"occupied": False, "pid": None, "is_ours": False}
    pid = find_listening_pid(port)
    cmdline = get_cmdline(pid) if pid else None
    is_ours = health_signature(port) == SERVICE_SIGNATURE or is_our_cmdline(cmdline)
    return {"occupied": True, "pid": pid, "is_ours": is_ours, "cmdline": cmdline}


def kill_pid_tree(pid: int) -> bool:
    """按 PID 精确树杀（/T 连带 uvicorn reload 子进程/npm 派生 node）。"""
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10,
                errors="replace",  # same: localized messages must not kill the reader
                creationflags=_NO_CONSOLE,
            )
            return r.returncode == 0
        import signal
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def request_graceful_stop(pid: int) -> bool:
    """先请进程自己优雅退出（Windows: CTRL_BREAK_EVENT 送给进程组）。

    为什么不能一上来就 `taskkill /F`（2026-09-17 血案）：
    `--daemon` 用 `CREATE_NEW_PROCESS_GROUP` 起进程，`kill_pid_tree` 的 `/F` 是
    硬杀 —— FastAPI 的 lifespan 关停段根本不会执行，于是：

    - SQLite 没有机会 checkpoint，`data/moss_finagent.db-wal` 被截断成 0 字节，
      而 `-shm` 仍是上一轮的 64KB 索引；下次启动**每次**打开该库都立刻
      `disk I/O error`，仓储 fail-open 穿透网络链 → 首个请求 182.9 秒。
    - 端口监听者被杀掉了，但**端口之外**的历史实例活着变孤儿，一直占着
      `-wal`/`-shm` 的文件句柄（实测有个 12:28 起的实例活到 16:51，CPU 0.0s）。

    优雅退出让 uvicorn 正常走 lifespan 关停（checkpoint + 关闭连接），
    拿不到才由调用方硬杀兜底。
    """
    if os.name != "nt":
        try:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            return True
        except OSError:
            return False
    try:
        import signal as _signal
        os.kill(pid, _signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        return True
    except (OSError, AttributeError, ValueError):
        return False


def wait_port_closed(port: int, timeout: float = 12.0) -> bool:
    """等到端口不再可连；超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open("127.0.0.1", port):
            return True
        time.sleep(0.3)
    return not port_open("127.0.0.1", port)


def list_our_backend_pids() -> list[int]:
    """枚举本机**所有**属于本项目后端的进程（不限端口）。

    为什么必须按命令行全量枚举：`--replace` 原先只杀"端口监听者"，端口之外
    的历史实例（换了端口的调试实例、上一个 PID 文件遗漏的实例）会成为孤儿，
    一直持有 SQLite 的 `-wal`/`-shm` 句柄，导致新实例读到 `disk I/O error`。
    只按命令行匹配 `is_our_cmdline`，不会碰到同机其他 Python/Node 服务。

    ⚠️ 子进程一律按 **UTF-8 + errors="replace"** 解码：Windows 上
    `capture_output=True, text=True` 会用系统 ANSI 代码页（本机 GBK）解码，
    而进程命令行里的中文路径（如 `D:\\quantTrader\\东莞证券QMT实盘交易端`）
    会让 `subprocess` 在读取线程里抛 UnicodeDecodeError、`stdout` 变成空串 ——
    症状是**枚举结果静默为空**（实测踩到：`stop` 因此漏掉孤儿实例）。
    """
    pids: list[int] = []
    for candidate in _iter_our_pids():
        pids.append(candidate)
    return sorted(set(pids))


def _iter_our_pids() -> list[int]:
    """按命令行枚举本项目的 Python 进程（多套手段依次兜底）。"""
    if os.name != "nt":
        out = _run_text(["ps", "-eo", "pid=,args="], timeout=10)
        found: list[int] = []
        for line in out.splitlines():
            pid_text, _, cmdline = line.strip().partition(" ")
            if pid_text.isdigit() and is_our_cmdline(cmdline):
                found.append(int(pid_text))
        return found

    found = []
    # 手段 1：PowerShell + CIM（能拿到完整命令行）
    script = ("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
              "ForEach-Object { \"$($_.ProcessId)\t$($_.CommandLine)\" }")
    out = _run_text(["powershell", "-NoProfile", "-NonInteractive",
                     "-Command", script], timeout=25)
    for line in out.splitlines():
        pid_text, _, cmdline = line.partition("\t")
        if pid_text.strip().isdigit() and is_our_cmdline(cmdline):
            found.append(int(pid_text.strip()))
    if found:
        return found

    # 手段 2：wmic（老系统 / PowerShell 被策略限制时）
    out = _run_text(["wmic", "process", "where", "name like '%python%'",
                     "get", "ProcessId,CommandLine", "/format:csv"], timeout=25)
    for line in out.splitlines():
        if not is_our_cmdline(line):
            continue
        for token in reversed(line.strip().split(",")):
            if token.strip().isdigit():
                found.append(int(token.strip()))
                break
    return found


def _run_text(cmd: list[str], *, timeout: float) -> str:
    """跑子进程并**强制 UTF-8** 解码（见 `list_our_backend_pids` 的说明）。

    解码失败不抛异常、也不丢行（`errors="replace"`）；子进程起不来/超时
    返回空串，由调用方走兜底路径。
    """
    try:
        completed = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False,
            creationflags=_NO_CONSOLE)
    except (subprocess.SubprocessError, OSError):
        return ""
    raw = completed.stdout or b""
    if isinstance(raw, str):        # 理论上不会（上面没给 text=True）
        return raw
    return raw.decode("utf-8", errors="replace")


def stop_backend_processes(port: int, *, timeout: float = 12.0) -> list[tuple[int, bool]]:
    """优雅停止所有本项目后端进程；返回 [(pid, 是否成功)]。

    顺序：先给每个实例发优雅退出请求 → 等端口释放 → 仍有存活才硬杀。
    这样 SQLite 有机会 checkpoint，下次启动不会再读到陈旧的 `-wal`/`-shm`。
    """
    targets = list_our_backend_pids()
    if not targets:
        pid = find_listening_pid(port)
        if pid is not None:
            info = diagnose_port(port)
            if info["is_ours"]:
                targets = [pid]
    results: list[tuple[int, bool]] = []
    if not targets:
        return results
    for pid in targets:
        request_graceful_stop(pid)
    wait_port_closed(port, timeout=timeout)
    for pid in targets:
        if not _pid_alive(pid):
            results.append((pid, True))
            continue
        results.append((pid, kill_pid_tree(pid)))
    return results



# ======================================================================
# start / stop
# ======================================================================

def _write_pid(name: str, pid: int) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / f"{name}.pid"
    path.write_text(str(pid), encoding="utf-8")
    return path


def _spawn_daemon(
    name: str, cmd: list[str] | str, cwd: Path, *, shell: bool = False,
    env: dict[str, str] | None = None,
) -> int:
    """后台启动子进程，返回 PID；日志与 PID 文件落 data/run/。

    `env` 给定时**叠加**到当前环境上（不是替换）—— 用于把 `--env dev` 的
    隔离路径传给子进程。不传则完全继承（保持原有行为）。
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUN_DIR / f"{name}.log"
    # 后台守护：新进程组让 Ctrl+C 只打当前前台，不误伤服务；
    # **不能用 DETACHED_PROCESS** —— 它会让 Windows 给子进程新分配一个可见控制台
    # （conhost 窗口，标题为 python.exe 全路径），桌面就会反复弹黑窗；
    # 而 CREATE_NO_WINDOW 既不分配控制台、又不影响父进程退出后的存活（已实测）。
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | _NO_CONSOLE if os.name == "nt" else 0
    log_fh = log_path.open("ab")  # noqa: SIM115 守护进程生命周期独立，不关闭
    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd), "stdout": log_fh, "stderr": subprocess.STDOUT,
        "creationflags": flags,
    }
    if env:
        popen_kwargs["env"] = {**os.environ, **env}
    if shell:
        popen_kwargs["shell"] = True
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    _write_pid(name, proc.pid)
    return proc.pid


#: dev 环境的隔离根目录（相对项目根）。刻意与生产完全分开：
#: 生产用 data/moss_finagent.db（实测 6.4GB）+ data/quant（31GB），
#: 调试若共用同一份，"改配置/写库"就直接作用于线上了。
DEV_ROOT = ROOT / "data" / "dev"

#: 对外试点实例的**约定端口**。
#:
#: 为什么必须与 dev 分开端口：两个实例各自用独立的库
#: （`data/dev/moss_dev.db` vs `data/pilot/moss_pilot.db`），
#: 而 SQLite 的单写者限制只约束**同一个库**，所以可以并存 ——
#: 本地继续用 8100 调试，同时对外开 8110 让客户登录。
#:
#: 但有一条**必须记住**：`--replace` 与 `stop` 是按**命令行**枚举本项目
#: 全部后端进程的（见 `list_our_backend_pids` 里"端口外孤儿会占住
#: -wal/-shm 导致 disk I/O error"那段血案记录）。所以它们会**连另一个
#: 实例一起停掉**。跨端口并存时不要用 `--replace`。
PILOT_BACKEND_PORT = 8110


def _env_choices() -> tuple[str, ...]:
    """`--env` 的合法取值，**以 `src.core.config.ENVS` 为唯一权威**。

    为什么要绕这一下而不是直接 `from src.core.config import ENVS`：
    `manage.py` 要求"依赖没装好时也能给出可执行的提示"，所以顶层不做
    重量级导入。这里做一次防御性导入，拿不到就用兜底值 ——
    兜底值只在"连配置模块都 import 不了"时生效，而那种情况下
    启动本来就会失败，所以不会掩盖任何问题。
    """
    try:
        from src.core.config import ENVS

        return tuple(ENVS)
    except Exception:  # noqa: BLE001 见 docstring
        return ("dev", "test", "pilot", "prod")


@contextlib.contextmanager
def _temporary_environ(overrides: dict[str, str]):
    """临时把 `overrides` 写进 `os.environ`，退出时**逐键还原**。

    为什么需要它：`Settings()`（pydantic-settings）在**实例化那一刻**
    从 `os.environ` + `.env` 读值，没有"传入一份 env 字典"的入口。
    而启动自检必须针对**即将生效**的那份环境（`--env` 注入后的），
    否则 `--env prod/pilot` 的目标环境规则整段不执行 —— 实测就是这么失效的
    （见 `_prepare_environment` 里的说明）。

    还原要精确到"原本不存在"与"原本存在"的差别：只 `pop` 会误删父进程
    本来就有的变量，只赋值又会把父进程的值污染成注入值。
    """
    missing = object()
    saved: dict[str, object] = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key, missing)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(old)


#: 对外试点（pilot）的数据根目录。**必须与 dev 和生产都分开** ——
#: 这一条不只是"整洁"：pilot 里是**客户的真实账号**，
#: 与调试数据混在一起意味着一次 `reset`/清库就把客户删了。
#: `assert_environment_consistency` 会强制路径里含 "pilot" 字样。
PILOT_ROOT = ROOT / "data" / "pilot"


def dev_isolation_env() -> dict[str, str]:
    """`--env dev` 的隔离环境变量：数据/调度/审计/通知通道全部改道。

    设计要点（见 docs/PLATFORM_MULTI_TENANCY_DESIGN.md §8.7.5）：
    - **复用现成开关**：`MOSS_SQLITE_PATH` 本来就是"让任务跑在副本上"的口子，
      不需要新造机制；`SCHEDULER_DIR`/`LLM_AUDIT_DIR` 同理。
    - **禁用定时任务**：否则调试实例会跑 `quant_data_sync`（16:40），
      与生产**同时写 31GB 行情仓库**。用 scheduler 的隔离目录 + 显式开关双保险。
    - **通知通道默认走 console**：dev 里验证码直接打日志，省掉 SMTP 配置；
      这一条在 prod 会被自检拦下（§8.7.2）。

    ## ★ 但**已经配好 SMTP 时不要覆盖它**（实测踩到的坑）

    原来这里**无条件**写 `MOSS_NOTIFY_CHANNEL=console`。后果是：
    用户 `.env` 里明明配好了真实 QQ 邮箱凭据（`ALERT_SMTP_USER` +
    `ALERT_SMTP_AUTH_CODE`），却因为 dev 隔离被强制降级成"打日志"——
    界面上点「发送验证码」提示**已发送**，而用户**永远收不到邮件**，
    服务端也没有任何报错（`ConsoleNotifier` 返回 ok=True）。

    症状极具误导性：提示成功 + 日志里能看到验证码 → 看起来像"邮件被邮箱
    拦了"，实际根本没发。排查成本很高。

    正确做法：**有真实凭据就让真实凭据生效**，只在没有任何凭据时才回退
    console（那才是"省掉 SMTP 配置"的原意）。显式设了
    `MOSS_NOTIFY_CHANNEL` 则一律尊重（让人能强制指定）。
    """
    root = str(DEV_ROOT)
    env = {
        "MOSS_ENV": "dev",
        "MOSS_SQLITE_PATH": f"{root}/moss_dev.db",
        "SCHEDULER_DIR": f"{root}/scheduler",
        "LLM_AUDIT_DIR": f"{root}/audit",
        # ⚠️ 这里原来设 `"MOSS_SCHEDULER_ENABLED": "0"`，**已删除**。
        # 那个变量全仓库零处读取（`grep MOSS_SCHEDULER_ENABLED` 只有写入处），
        # 所以它从来没关掉过任何东西，`CronScheduler` 照旧无条件启动 ——
        # 实测该实例跑了 25 个任务。留一个"设了但不生效"的开关比没有开关更糟：
        # 它会让人以为某个风险已经被防住了，而实际没有。
        # 真正的约束是"同一份 data/ 只能有一个写者"，那条写在启动横幅里。
    }
    explicit = str(os.environ.get("MOSS_NOTIFY_CHANNEL", "")).strip().lower()
    has_smtp = bool(str(os.environ.get("ALERT_SMTP_USER", "")).strip()
                    and str(os.environ.get("ALERT_SMTP_AUTH_CODE", "")).strip())
    if explicit:
        env["MOSS_NOTIFY_CHANNEL"] = explicit
    elif not has_smtp:
        # 没有任何真实凭据 → 回退 console（dev 便利，验证码打日志）
        env["MOSS_NOTIFY_CHANNEL"] = "console"
    else:
        print("📧 dev 隔离实例：检测到真实 SMTP 凭据，验证码将**真实发送邮件**"
              "（不再只打日志）。若想改回打日志：设 MOSS_NOTIFY_CHANNEL=console",
              file=sys.stderr)
    return env


def pilot_isolation_env() -> dict[str, str]:
    """`--env pilot` 的隔离环境变量：对外试点，**数据与 dev/生产彻底分开**。

    与 `dev_isolation_env` 的区别（这不是"复制一份改个路径"）：

    | | dev | pilot |
    |---|---|---|
    | 数据目录 | `data/dev/` | `data/pilot/` |
    | 谁能访问 | 只应本机 | **客户（公网，经 Cloudflare）** |
    | 通知通道 | 无凭据时打日志 | **必须有真实 SMTP 凭据**（自检强制） |
    | 登录门槛 | 关（本地调试方便） | **自动强制**（`LoginGateMiddleware`，见 `is_public`） |
    | 定时任务 | 关 | 关（理由见下） |

    ★ **定时任务同样关闭**，理由是实测过的具体冲突而不是"保守起见"：
    调度任务会写 `data/quant`（31GB 行情仓库）。本机已经有一个主实例在跑
    同一批任务，两个调度器同时写同一个仓库会造成重复下载与行级竞争。
    所以试点的定位是"**只读行情 + 独立账号库**"，行情数据由既有实例刷新。
    这条限制写在启动输出里，别让它变成"客户说数据没更新"才被发现。

    ★ **`MOSS_TENANCY_ENFORCE` 故意不在这里设置**：多租户中间件只认
    Bearer 令牌、不认会话 Cookie，打开它会让所有浏览器请求 401
    （连登录页都打不开）。pilot 的访问控制由 `LoginGateMiddleware` 承担。
    自检里对"pilot 上打开了 TENANCY_ENFORCE"是**报错**而不是放行 ——
    见 `assert_environment_consistency` 里的说明。
    """
    root = str(PILOT_ROOT)
    return {
        "MOSS_ENV": "pilot",
        "MOSS_PILOT_SINGLE_INSTANCE_ACK": "1",
        "MOSS_SQLITE_PATH": f"{root}/moss_pilot.db",
        "SCHEDULER_DIR": f"{root}/scheduler",
        "LLM_AUDIT_DIR": f"{root}/audit",
        "MOSS_AUDIT_DIR": f"{root}/access_audit",
        # ⚠️ 原来这里的 `"MOSS_SCHEDULER_ENABLED": "0"` **已删除**：零处读取，
        # 从来没关掉过任何任务（实测本实例跑了 25 个）。理由同 `dev_isolation_env`
        # 里那段注释 —— 一个"设了但不生效"的开关会伪造安全感。
        # 真要按实例裁剪任务，用 `MOSS_SCHEDULER_DENY`（按任务名，默认不拒任何东西）。
        # ★ 前端资源指向**冻结副本**，不指向 dev 正在用的 `web/dist`。
        #   否则本地 `npm run build` 一跑，客户刷新就拿到那份还没验过的界面。
        #   同步方式见 `manage.py ship-frontend`：
        #     验证（dev 8100）→ ship-frontend → 客户刷新即见。
        "MOSS_WEB_DIST": str(ROOT / "web" / "dist-pilot"),
    }


#: 后端**必备**的第三方依赖（缺一个都会让某条业务链静默降级）。
#:
#: 目前只列了会让"看起来像数据/文件丢失"的那几个：
#:   - `lightgbm` + `sklearn`：`moss_selector` 的模型是 LightGBM 模型，
#:     `joblib.load` 在反序列化时要 `import lightgbm`。缺了它 → 6 个模型文件
#:     全部加载失败被跳过 → `load_model_set` 抛「没有可用的模型文件」→
#:     界面报"模型丢了"并自动重训（2026-09-23 实际踩到）。
_RUNTIME_REQUIRED = ("lightgbm", "sklearn")


def missing_runtime_dependencies() -> list[str]:
    """当前解释器缺哪些必备依赖（导入探测，不装东西）。"""
    missing: list[str] = []
    for module in _RUNTIME_REQUIRED:
        try:
            __import__(module)
        except Exception:  # noqa: BLE001 任何导入失败都算缺
            missing.append(module)
    return missing


def _prepare_environment(env_name: str | None) -> tuple[dict[str, str], int]:
    """解析 `--env`：返回 (要注入子进程的环境变量, 退出码)。退出码非 0 表示拒绝启动。

    四件事，顺序不能换：
      1. 归一化环境名；
      2. dev 时生成隔离路径；
      3. **在起进程之前**跑自检 —— 配置不自洽就别启动；
      4. **依赖自检** —— 解释器缺 lightgbm 时拒绝启动（否则会伪装成"模型丢失"）。
    """
    # 延迟导入：manage.py 要能在没装依赖时也给出友好提示
    try:
        from src.core.config import (
            ENVS,
            Settings,
            assert_environment_consistency,
        )
    except Exception as exc:  # noqa: BLE001 依赖缺失时给可执行的提示
        print(f"❌ 无法加载配置模块（请先 uv sync）：{exc}", file=sys.stderr)
        return {}, 1

    name = str(env_name or os.environ.get("MOSS_ENV") or "dev").strip().lower()
    if name == "production":
        name = "prod"
    if name not in ENVS:
        print(f"❌ --env={env_name!r} 非法，可选：{' / '.join(ENVS)}", file=sys.stderr)
        return {}, 1

    extra: dict[str, str] = {"MOSS_ENV": name}
    if name == "dev":
        extra = dev_isolation_env()
    elif name == "pilot":
        extra = pilot_isolation_env()

    # 自检用"即将生效的环境变量"，而不是当前进程的（否则 --env 白给）
    effective = {**os.environ, **extra}
    # ★★ 这里必须**真的把 effective 装进 os.environ** 再构造 Settings。
    #
    # 原来是 `Settings()` 直接用**父进程**的环境变量构造，只把 `effective`
    # 传给 `environ=` 参数当"标志位视图"。后果（2026-09-23 实测复现）：
    #
    #   父 shell 里没有 MOSS_ENV  →  Settings().env == "dev"
    #   → settings.is_pilot / is_prod 都是 False
    #   → 目标环境的那一组规则**整段不执行**
    #   → problems == []  → "自检通过"
    #
    # 也就是说 `manage.py start --env prod`（或 pilot）**从来没跑过生产自检**，
    # 而它打印的还是"自检通过"。自检之所以存在，就是为了防止
    # "以为连的是测试、实际连的是生产"——结果它自己在这种调用方式下
    # 完全空转，而且**没有任何迹象**（这正是最难发现的一类失效）。
    #
    # `environ=` 参数仍然传（保留纯函数语义与既有测试），
    # 但 Settings 本身必须从 effective 构造，两边看到的才是同一份配置。
    with _temporary_environ(extra):
        try:
            settings = Settings()
        except Exception as exc:  # noqa: BLE001 配置非法（如环境名写错）直接拒绝
            print(f"❌ 配置加载失败，拒绝启动：{exc}", file=sys.stderr)
            return extra, 1

    problems = assert_environment_consistency(settings, environ=effective)
    if problems:
        print(f"❌ 环境自检未通过（MOSS_ENV={name}），**拒绝启动**：", file=sys.stderr)
        for item in problems:
            print(f"   - {item}", file=sys.stderr)
        print("   参考：docs/PLATFORM_MULTI_TENANCY_DESIGN.md §8.7.2", file=sys.stderr)
        return extra, 1

    missing = missing_runtime_dependencies()
    if missing:
        print("❌ 依赖自检未通过，**拒绝启动**：当前解释器缺少 "
              f"{'、'.join(missing)}", file=sys.stderr)
        print(f"   当前解释器：{sys.executable}", file=sys.stderr)
        print("   这会让 moss_selector 的模型**全部加载失败**（joblib 反序列化要 "
              "import lightgbm），界面表现是「moss_selector/models 下没有可用的"
              "模型文件」并反复自动重训 —— 但模型文件其实一直都在。", file=sys.stderr)
        print("   修法：用项目虚拟环境启动（Windows）：", file=sys.stderr)
        print(r"     .\.venv\Scripts\python.exe manage.py start --replace --daemon",
              file=sys.stderr)
        print("   或用 uv：uv run python manage.py start --replace --daemon",
              file=sys.stderr)
        return extra, 1

    if name == "dev":
        DEV_ROOT.mkdir(parents=True, exist_ok=True)
        print(f"🔧 dev 隔离实例：数据目录 {DEV_ROOT}（不触碰生产库）", file=sys.stderr)
    elif name == "pilot":
        PILOT_ROOT.mkdir(parents=True, exist_ok=True)
        print("=" * 68, file=sys.stderr)
        print("🌐 **对外试点实例（pilot）** —— 客户可经公网访问，但它不是生产：",
              file=sys.stderr)
        print(f"   · 数据目录：{PILOT_ROOT}（与 dev / 本机库彻底分开）",
              file=sys.stderr)
        print("   · 登录门槛：**已自动强制**（除登录/注册/存活探针外都要会话）",
              file=sys.stderr)
        print("   · 单实例：SQLite 单写者，**不要**起第二个副本；"
              "重启期间对外不可用", file=sys.stderr)
        # ⚠️ 这行原来印的是"**已关闭** —— 行情由既有实例刷新"。
        # 那是**假的**，而且骗了很久：`MOSS_SCHEDULER_ENABLED` 这个环境变量
        # 全仓库**从来没有任何地方读它**（`grep` 只有写入处，零处读取），
        # 而 `src/api/main.py` 里 `CronScheduler(...).start()` 是**无条件**执行的。
        # 实测：试点实例上跑了 25 个定时任务，含 `quant_data_sync`
        # （`data/pilot/scheduler/runs.jsonl` 里有它的成功记录）。
        #
        # 后果不是"少跑几个任务"，而是**反向的**：横幅说关着，于是没人会想到
        # 两个实例正在同写 `data/quant/warehouse.db`。实测 09-23/09-24 两个实例
        # 的 `quant_data_sync` 有 **17 对时间区间重叠**。
        #
        # 所以这里改为**印真实状态**，并且说清真正的约束（单写者），而不是
        # 继续复述一个已经不成立的设计假设。
        print("   · 定时任务：**已启用**（进程内调度，无条件启动）——"
              " 见下方「单写者」约束：同一份 data/ 只能有一个实例在跑任务",
              file=sys.stderr)
        print(f"   · 建议端口：--port {PILOT_BACKEND_PORT}"
              f"（与 dev 的 8100 并存；两实例各用独立的库）", file=sys.stderr)
        print("   · ⚠️ **不要用 --replace**：它按命令行枚举本项目**全部**后端正"
              "进程，会把正在跑的 dev/主实例一起停掉", file=sys.stderr)
        print("   · 不承诺 SLA；多租户 DataClass 平面尚未与会话打通"
              "（见设计文档 §13.8）", file=sys.stderr)
        print("=" * 68, file=sys.stderr)
    elif name == "prod":
        print("🔒 prod 环境：自检通过", file=sys.stderr)
    return extra, 0


def cmd_start(args: argparse.Namespace) -> int:
    host, port = args.host, int(args.port)

    # ---- 环境解析与自检（必须最先做，且失败即拒绝启动）----
    # 为什么先做：环境标记决定"用什么库、要不要强制鉴权、数据落到哪"。
    # 配置不自洽时**拒绝启动**而不是打警告 —— 警告会被日志淹没，
    # 启动失败是唯一 100% 会被看见的提示（与 rls.py "让漏配表现为失败" 同源）。
    extra_env, rc = _prepare_environment(getattr(args, "env", None))
    if rc != 0:
        return rc

    # 前端端口（如需）
    if args.with_frontend and not port_open("127.0.0.1", DEFAULT_FRONTEND_PORT):
        _start_frontend(daemon=args.daemon)

    info = diagnose_port(port)
    if info["occupied"]:
        if info["is_ours"]:
            if args.replace:
                pid = info["pid"]
                print(f"检测到本项目旧实例 (PID={pid})，--replace 精确停止中…",
                      file=sys.stderr)
                # 优雅停止**所有**实例：只杀端口监听者会留下端口外的孤儿进程，
                # 它们持有 SQLite 的 `-wal`/`-shm` 句柄 → 新实例读到
                # `disk I/O error` → 每个请求穿透网络（实测首请求 182.9s）。
                for old_pid, ok in stop_backend_processes(port):
                    if old_pid != pid:
                        print(f"  额外清理孤儿后端实例 PID={old_pid} "
                              f"({'成功' if ok else '失败'})", file=sys.stderr)
                for _ in range(10):
                    if not port_open("127.0.0.1", port):
                        break
                    time.sleep(0.3)
            else:
                others = [p for p in list_our_backend_pids() if p != info["pid"]]
                print(f"✅ 本项目后端已在运行：http://127.0.0.1:{port} "
                      f"(PID={info['pid']})，无需重复启动。", file=sys.stderr)
                if others and not is_single_instance([info["pid"], *others]):
                    # 孤儿实例不会响应端口探测，但会占着 SQLite 的伴生文件，
                    # 让新实例的每个请求都 disk I/O error —— 必须让用户看见。
                    print(f"⚠️ 另有 {len(others)} 个端口外后端进程仍在运行："
                          f"{others}。它们会占用数据库的 -wal/-shm 文件，"
                          f"建议先 `python manage.py stop` 清理。", file=sys.stderr)
                print("   如需重启：python manage.py start --replace", file=sys.stderr)
                return 0
        else:
            # 非本项目占用：拒绝，不碰其他程序
            print("=" * 68, file=sys.stderr)
            print(f"❌ 端口 {port} 被其他程序占用（非本项目实例），已拒绝启动：",
                  file=sys.stderr)
            print(f"   PID: {info['pid']}", file=sys.stderr)
            print(f"   命令行: {info.get('cmdline') or '(无法获取)'}", file=sys.stderr)
            print("=" * 68, file=sys.stderr)
            print("可选：--auto-port 自动换端口，或 --port 指定其他端口。"
                  "请勿手动 taskkill 其他业务进程。", file=sys.stderr)
            return 2

    if args.auto_port and info["occupied"]:
        port = find_free_port(port + 1)
        print(f"--auto-port：改用空闲端口 {port}", file=sys.stderr)

    uvicorn_cmd = [
        sys.executable, "-m", "uvicorn", "src.api.main:app",
        "--host", host, "--port", str(port),
        # ★★ `--timeout-keep-alive` 必须显式调大，**不能**吃 uvicorn 的默认 5 秒。
        #
        # 症状（实测，2026-09-23 管理员点「直接开号 → 创建」）：浏览器只报
        # `TypeError: Failed to fetch`，而服务端日志里**根本没有这条请求**
        # —— 也就是说请求在到达应用之前就没了，看起来像"后端没启动"，
        # 于是往"服务没运行/端口不对"方向白查很久。
        #
        # 真实原因：uvicorn 默认 5 秒空闲就主动关 keep-alive 连接，而浏览器
        # 的空闲连接复用窗口更长（通常 60~300 秒）。两边计时器交错时会出现
        # "浏览器刚把请求写到一个服务端正在关闭的 socket 上"的竞态，服务端
        # 直接关连接 → 浏览器报 `ERR_EMPTY_RESPONSE`。
        #   · **GET/HEAD 会被浏览器静默重试**，所以看起来"刷页面没问题"；
        #   · **POST/PATCH/PUT 不会自动重试**（非幂等），于是只有写操作暴露
        #     —— 恰好就是管理后台那些按钮。
        #   · 后端刚重启过时最严重：所有旧连接同时变成半开连接，一次全炸。
        #
        # 65 秒 > 浏览器常见空闲复用窗口，让**服务端**的连接在浏览器放弃它
        # 之前一直有效，竞态窗口消失。这是一个后端配置问题，不该指望前端兜底。
        "--timeout-keep-alive", "65",
    ]
    if args.reload:
        uvicorn_cmd.append("--reload")

    if args.daemon:
        # 名字与日志按 env 区分：否则 `manage.py logs` 会把两个实例的输出混在一起
        pid = _spawn_daemon(
            "backend" if extra_env.get("MOSS_ENV") != "dev" else "backend-dev",
            uvicorn_cmd, ROOT, env=extra_env)
        # 等待端口就绪（最多 ~10s）
        for _ in range(50):
            if port_open("127.0.0.1", port):
                break
            if not _pid_alive(pid):
                print("❌ 后端进程已退出，见日志 data/run/backend.log", file=sys.stderr)
                return 1
            time.sleep(0.2)
        print(f"✅ 后端已后台启动：http://127.0.0.1:{port} (PID={pid})")
        print("   日志: data/run/backend.log｜停止: python manage.py stop")
        return 0

    # 前台模式（开发最常用，Ctrl+C 退出）
    print(f"▶ 启动后端 http://{host}:{port}（前台运行，Ctrl+C 停止）…")
    try:
        subprocess.run(uvicorn_cmd, cwd=str(ROOT), check=False,
                       env={**os.environ, **extra_env})
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


def _start_frontend(daemon: bool) -> int:
    if not (WEB_DIR / "package.json").exists():
        print("⚠️ 未找到 web/package.json，跳过前端。", file=sys.stderr)
        return 1
    npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm"
    if daemon:
        pid = _spawn_daemon(
            "frontend", f'"{npm}" run dev', WEB_DIR, shell=True,
        )
        print(f"✅ 前端 dev server 后台启动：http://127.0.0.1:{DEFAULT_FRONTEND_PORT} "
              f"(PID={pid})")
        return 0
    print(f"▶ 启动前端 dev server（端口 {DEFAULT_FRONTEND_PORT}）…")
    subprocess.run([npm, "run", "dev"], cwd=str(WEB_DIR), check=False, shell=True)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    stopped = []
    for name, port in (("backend", DEFAULT_BACKEND_PORT),
                       ("frontend", DEFAULT_FRONTEND_PORT)):
        if name == "backend":
            # 后端：按命令行枚举**所有**实例（含端口外的孤儿），先优雅后硬杀。
            # 只按 PID 文件/端口监听者停会漏掉孤儿实例，而孤儿实例占着 SQLite
            # 的 `-wal`/`-shm`，是"重启后前端几十秒没数据"的根因之一。
            results = stop_backend_processes(port)
            for pid, ok in results:
                stopped.append((f"{name}", pid, ok))
        else:
            pid = (read_pid_file(RUN_DIR / f"{name}.pid")
                   if RUN_DIR.exists() else None)
            if pid is None and port_open("127.0.0.1", port):
                # PID 文件丢失：仅当确认是本项目实例时才按端口兜底停止
                info = diagnose_port(port)
                pid = info["pid"] if info["is_ours"] else None
                if info["occupied"] and not info["is_ours"]:
                    print(f"⚠️ 端口 {port} 非本项目实例，跳过（不停止其他程序）。",
                          file=sys.stderr)
            if pid is not None:
                stopped.append((name, pid, kill_pid_tree(pid)))
        pid_file = RUN_DIR / f"{name}.pid"
        if pid_file.exists():
            pid_file.unlink(missing_ok=True)

    # Celery worker 无端口，仅按 PID 文件停止（树杀连带 beat 子进程）
    wpid = read_pid_file(RUN_DIR / "worker.pid") if RUN_DIR.exists() else None
    if wpid is not None:
        stopped.append(("worker", wpid, kill_pid_tree(wpid)))
    wpid_file = RUN_DIR / "worker.pid"
    if wpid_file.exists():
        wpid_file.unlink(missing_ok=True)

    if not stopped:
        print("未发现运行中的本项目实例。")
        return 0
    for name, pid, ok in stopped:
        print(f"{'✅' if ok else '❌'} 已停止 {name} (PID={pid})")
    return 0 if all(ok for _, _, ok in stopped) else 1


# ======================================================================
# status / test / frontend / build
# ======================================================================

def cmd_status(_args: argparse.Namespace) -> int:
    rows = []
    # 后端
    if port_open("127.0.0.1", DEFAULT_BACKEND_PORT):
        # 判定"是不是本项目"用 `diagnose_port`（服务签名 **或** 命令行两条证据），
        # 不能只看 `health_signature`：它只有 1 秒超时，而 /health 在冷启动、
        # 或仓库/Tushare 统计正在生成时要几十秒才回 —— 只看签名会把**正在正常
        # 运行**的本项目实例误报成"被其他程序占用"（实测踩到）。
        info = diagnose_port(DEFAULT_BACKEND_PORT)
        if info["is_ours"]:
            extra = ""
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}/api/v1/health",
                    timeout=10.0,
                ) as resp:
                    h = json.loads(resp.read().decode("utf-8", errors="replace"))
                gw = h.get("model_gateway", {})
                extra = (
                    f"status={h.get('status')} ollama={gw.get('ollama')} "
                    f"deepseek={gw.get('deepseek')}"
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    # 公网环境（prod/pilot）把 /health 纳入了登录门槛，
                    # 这是**预期行为**而不是故障 —— 不能说成"未返回"，
                    # 否则运维会去查一个根本不存在的启动问题。
                    extra = ("（已强制登录门槛：深度健康检查 /health 需登录；"
                             "进程存活正常，存活探针见 /api/v1/health/live）")
                else:
                    extra = f"（/health 返回 HTTP {exc.code}）"
            except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
                extra = "（/health 未在 10 秒内返回：可能仍在预热，稍后再试）"
            rows.append(("后端 API", DEFAULT_BACKEND_PORT, "本项目运行中", extra))
        else:
            rows.append(("后端端口", DEFAULT_BACKEND_PORT, "被其他程序占用",
                         f"PID={info.get('pid')}"))
    else:
        rows.append(("后端 API", DEFAULT_BACKEND_PORT, "未运行",
                     "python manage.py start"))
    # 对外试点实例（独立端口 + 独立库，可与 dev 并存）。
    # 不加这一行的话，`manage.py status` 对一个正在给客户提供服务的实例
    # 完全无感 —— 运维会以为"只有一个实例在跑"。
    if port_open("127.0.0.1", PILOT_BACKEND_PORT):
        p_info = diagnose_port(PILOT_BACKEND_PORT)
        rows.append((
            "对外试点", PILOT_BACKEND_PORT,
            "本项目运行中" if p_info["is_ours"] else "被其他程序占用",
            f"客户可经公网访问（单实例，无 HA）｜"
            f"启动：python manage.py start --env pilot --port {PILOT_BACKEND_PORT}"
            if p_info["is_ours"] else f"PID={p_info.get('pid')}",
        ))
    # 前端
    rows.append((
        "前端 dev", DEFAULT_FRONTEND_PORT,
        "运行中" if port_open("127.0.0.1", DEFAULT_FRONTEND_PORT) else "未运行",
        "python manage.py frontend" if not port_open("127.0.0.1", DEFAULT_FRONTEND_PORT)
        else "vite dev server",
    ))
    # Celery worker（无端口，按 PID 文件判定）
    wpid = read_pid_file(RUN_DIR / "worker.pid") if RUN_DIR.exists() else None
    rows.append((
        "Celery调度", "—",
        f"运行中(PID={wpid})" if wpid else "未运行(演示用内置调度)",
        "python manage.py worker（需Redis）" if not wpid else "data/run/worker.log",
    ))
    # 外部依赖
    rows.append((
        "Ollama", OLLAMA_PORT,
        "在线" if port_open("127.0.0.1", OLLAMA_PORT) else "不可达",
        "本地模型依赖（medium/light 层）",
    ))
    rows.append((
        "XtMiniQmt", QMT_PORT,
        "在线" if port_open("127.0.0.1", QMT_PORT) else "未运行",
        "行情服务，不可用时降级CSV/AkShare",
    ))

    print(f"{'服务':<12}{'端口':<8}{'状态':<16}说明")
    print("-" * 72)
    for name, p, state, note in rows:
        print(f"{name:<12}{p:<8}{state:<16}{note}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """隔离环境运行 pytest：所有数据目录重定向到临时目录。"""
    import pytest

    tmp_dir = tempfile.mkdtemp(prefix="moss_finagent_test_env_")
    env = build_test_env(tmp_dir)
    os.environ.update(env)
    # 清空 settings 单例缓存，确保新环境变量生效
    try:
        from src.core.config import get_settings
        get_settings.cache_clear()
    except Exception:  # noqa: BLE001 防御性：尚未导入时无需清理
        pass

    print(f"[test] 隔离数据目录：{tmp_dir}", file=sys.stderr)
    print("[test] 隔离项：SQLite/LLM缓存(关)/审计日志/调度记录", file=sys.stderr)
    return int(pytest.main(list(getattr(args, "pytest_args", []))))


def cmd_frontend(args: argparse.Namespace) -> int:
    return _start_frontend(daemon=getattr(args, "daemon", False))


def cmd_build(_args: argparse.Namespace) -> int:
    if not (WEB_DIR / "package.json").exists():
        print("❌ 未找到 web/package.json", file=sys.stderr)
        return 1
    npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm"
    return subprocess.run(
        [npm, "run", "build"], cwd=str(WEB_DIR), shell=True, check=False,
        creationflags=_NO_CONSOLE,
    ).returncode


def cmd_ship_frontend(_args: argparse.Namespace) -> int:
    """把 `web/dist` 的构建**同步**到对外试点用的 `web/dist-pilot`。

    ## 为什么要有这一步（而不是让 pilot 直接用 web/dist）

    `StaticFiles` 每次请求都从磁盘读，而 dev 与 pilot 跑的是同一个 checkout。
    如果 pilot 直接用 `web/dist`，那么本地 `npm run build` 一跑完，
    **客户刷新一下就是那份还没验过的界面** —— 调试前端时这等于把半成品
    推给客户，而且没有"回滚到上一版界面"的手段。

    所以对外实例指向 `web/dist-pilot`（见 `pilot_isolation_env` 里的
    `MOSS_WEB_DIST`），发布流程变成显式一步：

        1. `python manage.py build`            # 构建到 web/dist
        2. 在 dev（8100）上验一遍
        3. `python manage.py ship-frontend`    # 同步到 dist-pilot（本命令）
        4. 客户刷新即可见

    ## 为什么"先清空再复制"而不是覆盖式复制

    构建产物带**内容哈希文件名**（`index-abc123.js`）。覆盖式复制会留下
    历史版本的 `index-*.js`/`*.css`，目录越来越大、审计时也分不清
    "线上到底是哪一份"。先删后拷保证 `dist-pilot` 与本次构建**逐文件一致**。
    """
    src = WEB_DIR / "dist"
    dst = WEB_DIR / "dist-pilot"
    if not (src / "index.html").exists():
        print(f"❌ {src} 里没有 index.html —— 先跑 `python manage.py build`",
              file=sys.stderr)
        return 1
    try:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    except OSError as exc:
        print(f"❌ 同步失败：{exc}", file=sys.stderr)
        return 1
    files = sum(1 for _ in dst.rglob("*") if _.is_file())
    print(f"✅ 已同步 {files} 个文件 → {dst}")
    print("   对外试点实例（8110）直接读该目录，**无需重启**；"
          "客户刷新即可看到本次界面。")
    print("   回滚：把上一版 dist 重新 ship 一次即可（建议每次发布前留副本）。")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Celery worker + beat（生产/分布式调度，需 Redis）。

    本地演示默认由 FastAPI lifespan 进程内 CronScheduler 调度，无需启动本命令；
    仅当 REDIS_CACHE_ENABLED/分布式部署时使用。Windows 用 solo 池。
    """
    wpid = read_pid_file(RUN_DIR / "worker.pid") if RUN_DIR.exists() else None
    if wpid is not None and not args.replace:
        print(f"✅ Celery worker 已在运行 (PID={wpid})。重启请加 --replace。",
              file=sys.stderr)
        return 0
    if wpid is not None and args.replace:
        kill_pid_tree(wpid)
        (RUN_DIR / "worker.pid").unlink(missing_ok=True)
        time.sleep(0.5)

    pool = args.pool or ("solo" if os.name == "nt" else "prefork")
    cmd = [
        sys.executable, "-m", "celery",
        "-A", "src.scheduler.celery_app.celery_app",
        "worker", "-B", "--pool", pool, "-l", args.loglevel,
    ]
    if args.daemon:
        pid = _spawn_daemon("worker", cmd, ROOT)
        print(f"✅ Celery worker(+beat) 后台启动 (PID={pid}, pool={pool})")
        print("   日志: data/run/worker.log｜停止: python manage.py stop")
        return 0
    print(f"▶ Celery worker(+beat) 前台运行 pool={pool}，Ctrl+C 停止…")
    try:
        subprocess.run(cmd, cwd=str(ROOT), check=False)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


def cmd_vacuum(args: argparse.Namespace) -> int:
    """回收 SQLite 空闲页（`VACUUM`）。

    ## 为什么需要它

    2026-09-25 数据库审计实测：主库 6.32GB 中 **3.96GB（63%）是 freelist
    空闲页** —— 保留清理删掉了行、页被标记为空闲，但 `auto_vacuum=0`
    意味着**文件永不缩小**。"清理了"不等于"磁盘回来了"。

    ## 为什么必须手动、必须先停服

    1. `VACUUM` 会重写整个库，期间持有**排他锁**，所有读写全部阻塞
       （6GB 级库是分钟级）；
    2. 需要**约等于库大小的临时空间**（与库同目录）；
    3. 这是唯一会让正在服务的实例整段不可用的操作，所以**不放进每日作业**。

    ## 安全措施

    - 默认要求确认（`--yes` 跳过）；
    - 执行前检查是否还有本项目后端在跑，**不让 VACUUM 与写操作并发**；
    - 先 `wal_checkpoint(TRUNCATE)`，否则 `.db-wal` 可能仍占着空间。
    """
    from src.core.config import get_settings
    from src.infrastructure.retention_service import vacuum_sync

    settings = get_settings()
    db_path = str(settings.sqlite_path)
    before = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    print(f"目标库：{db_path}（{before / 1024 ** 3:.2f} GB）")

    pids = list_our_backend_pids()
    if pids:
        print(f"❌ 检测到本项目后端仍在运行（PID {pids}）—— 请先执行："
              f"python manage.py stop")
        print("   VACUUM 需要排他锁；与正在写库的实例并发会互相阻塞，")
        print("   且耗时长到看起来像服务卡死。")
        return 1

    free = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    if free < before * 1.2:
        print(f"❌ 同一磁盘可用空间不足：需约 {before * 1.2 / 1024 ** 3:.2f} GB"
              f"（库大小 + 余量），当前 {free / 1024 ** 3:.2f} GB。")
        print("   VACUUM 的临时文件与库同目录，空间不足会失败。")
        return 1

    if not args.yes:
        print("VACUUM 会重写整个库，期间库不可用（分钟级）。")
        if input("确认继续？输入 yes：").strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 0

    print("正在 VACUUM（可能要几分钟）…")
    outcome = vacuum_sync(settings)
    if not outcome.get("ok"):
        print(f"❌ 失败：{outcome.get('error')}")
        return 1
    print(f"✅ 完成：{outcome['before'] / 1024 ** 3:.2f} GB → "
          f"{outcome['after'] / 1024 ** 3:.2f} GB"
          f"（回收 {outcome['freed'] / 1024 ** 3:.2f} GB）")
    return 0


#: 值守事件流水（JSONL）。每一条 = 一次"发现后端不在 → 重启"的现场记录。
#:
#: ## 为什么必须有它（2026-09-26 实测事故）
#:
#: 对外实例（8110）**被静默终止过一次**：端口无监听、进程全无，
#: 而 `backend.log` 尾部只到最后一个 `200 OK`，没有任何异常或退出痕迹；
#: Windows 事件日志在同一时间窗内也没有崩溃/关机记录。
#: 也就是说它既不是崩的、也不是正常关停的 —— 是被外部强杀的。
#:
#: 强杀（`taskkill /F`、Job Object 关闭、OOM killer 之类）**不会走 lifespan**，
#: 所以"事后从日志找原因"这条路本身就是断的。唯一的出路是**当时就把现场记下来**：
#: 最后一次活动时刻、当时的 PID、端口状态。下次再发生，这份流水能直接告诉
#: 我们"死在哪一刻、当时还有没有进程"，而不是靠推测。
INCIDENT_LOG = RUN_DIR / "backend_incidents.jsonl"


def _record_incident(event: str, **fields: object) -> None:
    """追加一条值守事件（失败只警告，绝不影响值守本身）。"""
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now().isoformat(timespec="seconds"),
               "event": event, **fields}
        with INCIDENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 记不上账不该让值守失败
        print(f"⚠️ 值守事件写入失败：{exc}", file=sys.stderr)


def cmd_ensure(args: argparse.Namespace) -> int:
    """**值守**：后端不在就拉起来；在就什么都不做。

    ## 为什么需要它（以及它**不是**什么）

    它不是高可用、不做负载均衡、不管数据库迁移。它只回答一个问题：
    "对外那个端口上，我们的进程还在吗？不在就起回来。"

    2026-09-26 的静默终止事故说明：**没有值守时，服务消失是无声的** ——
    客户先发现"打不开"，我们才发现"进程没了"，而且日志里查不到任何原因
    （强杀不走 lifespan，什么都不留）。有了它，同样的事故变成
    "一分钟内自动恢复 + 流水里留一条现场"。

    ## 判据（三条，顺序不能换）

    1. 端口**没被占用** → 直接重启（这是最常见的形态：进程整棵树消失）。
    2. 端口被**本项目**实例占用 → 什么都不做（这是绝大多数 tick 的路径，
       必须零副作用：不重启、不写日志、不改 pid 文件）。
    3. 端口被**别的程序**占用 → **拒绝启动**并记一条事件。

       ⚠️ 绝不能在这里"先杀掉再起"：`kill_pid_tree` 是硬杀，会打断别的
       业务程序；而且端口被非本项目占用时，我们根本不知道对方是什么。
       宁可让值守报错让人来看。

    ## 为什么不做"健康检查失败就重启"

    因为 `/health` 在冷启动、仓库统计生成时会**几十秒**才回（`cmd_status`
    的注释记过这条实测），把"慢"判成"死"会导致值守自己反复重启一个
    正在正常工作的实例 —— 那是把可用性问题变成自己制造的故障。
    所以判据只看**端口有没有人听**：进程死了端口必然空，这个判据不会误报。
    """
    port = args.port
    env = args.env or "pilot"
    info = diagnose_port(port)

    if info.get("occupied") and info.get("is_ours"):
        if args.verbose:
            print(f"✅ {port} 上本项目实例运行中（PID={info.get('pid')}），无需处理。")
        return 0

    if info.get("occupied"):
        # 非本项目占用：拒绝，并留下现场
        _record_incident(
            "foreign_port_owner", port=port,
            pid=info.get("pid"), cmdline=str(info.get("cmdline") or "")[:200])
        print(f"❌ 端口 {port} 被**其他程序**占用（PID={info.get('pid')}），"
              f"值守拒绝启动。命令行：{info.get('cmdline')}", file=sys.stderr)
        return 2

    # 端口空 → 起回来。先把"最后一次活动"记下来，这是判断死亡时刻的唯一线索。
    last_activity = ""
    log_path = RUN_DIR / f"{args.name}.log"
    if log_path.exists():
        last_activity = datetime.fromtimestamp(
            log_path.stat().st_mtime).isoformat(timespec="seconds")
    _record_incident("restart", port=port, env=env,
                     last_activity=last_activity,
                     reason="端口无监听（进程已消失）")

    code = _ensure_restart(env=env, port=port, name=args.name)
    if code == 0:
        _record_incident("restart_ok", port=port)
        print(f"✅ 值守已重启 {env} 实例（{port}）。上次活动：{last_activity or '未知'}")
    else:
        _record_incident("restart_failed", port=port, exit_code=code)
        print(f"❌ 值守重启失败（退出码 {code}），详见 {INCIDENT_LOG}", file=sys.stderr)
    return code


def _ensure_restart(*, env: str, port: int, name: str) -> int:
    """值守专用重启：**复用 `cmd_start`**，不重新拼一遍 uvicorn 命令。

    为什么必须复用（而不是在这里自己 `_spawn_daemon`）：
    启动后端要同时做对一整套事 —— 环境自检、隔离环境变量（pilot 的
    `MOSS_SQLITE_PATH` / `MOSS_WEB_DIST` / 单实例 ack）、端口占用判定、
    `--timeout-keep-alive 65`、日志与 pid 文件命名、就绪等待、失败回滚。
    自己再拼一份，早晚会漏（本项目已有"两处各拼一遍必然漂移"的多次记录）。

    `--replace` 一律**不传**：它在端口已被占用的场景才有用，而本函数只在
    "端口空"时被调用；更关键的是它按命令行枚举本项目**全部**后端进程，
    会把正在跑的另一个实例（dev 8100）一起停掉。
    """
    args = argparse.Namespace(
        env=env, host="127.0.0.1", port=port,
        reload=False, daemon=True, with_frontend=False,
        replace=False, auto_port=False,
    )
    return cmd_start(args)


def cmd_logs(args: argparse.Namespace) -> int:
    """查看后台服务日志（默认 backend，可 frontend/worker）。"""
    log_path = RUN_DIR / f"{args.name}.log"
    if not log_path.exists():
        print(f"未找到日志 {log_path}（该服务可能未以 --daemon 方式启动）。",
              file=sys.stderr)
        return 1
    if not args.follow:
        lines = log_path.read_text(
            encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.lines:]))
        return 0
    # --follow：先打印尾部，再轮询增量
    import threading

    size = log_path.stat().st_size
    print("\n".join(log_path.read_text(
        encoding="utf-8", errors="replace").splitlines()[-args.lines:]))
    stop = threading.Event()
    try:
        while not stop.wait(0.5):
            new_size = log_path.stat().st_size
            if new_size < size:  # 日志被轮转/重建
                size = 0
            if new_size > size:
                with log_path.open("rb") as fh:
                    fh.seek(size)
                    sys.stdout.buffer.write(fh.read())
                    sys.stdout.flush()
                size = new_size
    except KeyboardInterrupt:
        print("\n退出日志跟随。")
    return 0


# ======================================================================
# argparse 注册
# ======================================================================

def _parent_of(pid: int) -> int | None:
    """取父进程 PID（取不到返回 None）。"""
    if os.name != "nt":
        return None
    out = _run_text(["wmic", "process", "where", f"ProcessId={pid}",
                     "get", "ParentProcessId", "/format:list"], timeout=10)
    for line in out.splitlines():
        _, _, value = line.partition("=")
        if value.strip().isdigit() and int(value.strip()) > 0:
            return int(value.strip())
    return None


def is_single_instance(pids: list[int]) -> bool:
    """这些进程是否**属于同一个实例**（父进程也在列表里 → 是 reload 父子对）。

    `uvicorn --reload` 会派生一个子进程（reloader 看门狗 + 真正的 worker），
    两者命令行都含 `src.api.main:app`，从命令行看就像"两个实例"。
    实测（2026-09-17）：`doctor` 因此报"存在多个后端实例（含端口外孤儿）"，
    把一个**正常的** `--reload` 实例说成孤儿 —— 这种假告警会让人去清理根本
    不该清理的东西。真正的孤儿是**父进程不在列表里**的那些。
    """
    if len(pids) <= 1:
        return True
    parents = {_parent_of(pid) for pid in pids}
    # 只要有一个进程的父进程也在这个集合里，就说明是同一棵树
    return any(parent in set(pids) for parent in parents if parent)


def cmd_doctor(_args: argparse.Namespace) -> int:
    """体检并自愈本地 SQLite（陈旧 `-wal`/`-shm` → disk I/O error）。

    为什么需要这个命令：硬杀进程（`taskkill /F`）会让 `-wal` 被截断成 0 字节、
    `-shm` 停在上一轮，此后**每次**打开该库都立刻 `disk I/O error`，仓储
    fail-open 穿透网络 → 前端重启后几十秒没数据。启动时 lifespan 已会自动
    自愈，这个命令用于"服务正跑着、想单独确认一下"或排查时的现场取证。
    """
    sys.path.insert(0, str(ROOT))
    try:
        from src.core.config import get_settings
        from src.core.sqlite_recovery import ensure_sqlite_usable, sidecar_sizes
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 无法导入自愈模块：{exc}", file=sys.stderr)
        return 1

    settings = get_settings()
    paths = [str(settings.sqlite_path), "data/quant/warehouse.db"]
    print("=" * 72)
    print("SQLite 体检（只对可写库做自愈；仓库只读检查）")
    print("=" * 72)
    exit_code = 0
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            print(f"○ {raw}：不存在（跳过）")
            continue
        wal, shm = sidecar_sizes(path)
        writable = "warehouse" not in path.name
        before = "?"
        try:
            import sqlite3
            with sqlite3.connect(str(path), timeout=3) as con:
                before = str(con.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            before = f"打不开（{exc}）"
            if writable:
                exit_code = 1
        print(f"\n● {raw}")
        print(f"    主库大小 : {path.stat().st_size / 1e6:.1f}MB")
        print(f"    -wal/-shm: {wal}B / {shm}B")
        print(f"    表数量   : {before}")
        if writable:
            result = ensure_sqlite_usable(path)
            if result.healthy:
                print("    结论     : ✅ 正常")
            elif result.recovered:
                print(f"    结论     : 🔧 已自愈（{result.reason}）")
                print(f"               挪走 {result.quarantined}，"
                      f"备份在 data/recovery/")
                exit_code = 0
            else:
                print(f"    结论     : ❌ 不可用（{result.reason}）")
                exit_code = 1

    others = list_our_backend_pids()
    listener = find_listening_pid(DEFAULT_BACKEND_PORT)
    print("\n" + "=" * 72)
    print(f"后端进程：端口监听者 PID={listener}，命令行匹配到的进程 {others}")
    if len(others) > 1 and not is_single_instance(others):
        print("⚠️ 存在多个**互相独立**的后端实例（含端口外孤儿）。孤儿会占着 "
              "SQLite 的 -wal/-shm，请执行 `python manage.py stop` 清理。")
    elif len(others) > 1:
        print("（以上是同一个实例的 reload 父子进程，非孤儿）")
    print("=" * 72)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Moss-FinAgent-Research 统一管理（启动/停止/状态/隔离测试）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="启动后端（默认127.0.0.1:8100）")
    p_start.add_argument(
        # ⚠️ choices 必须与 `src.core.config.ENVS` 一致。这里曾经写死
        # `["dev","test","prod"]`，加了 pilot 之后 `--env pilot` 直接被
        # argparse 拦下（"invalid choice"），而自检那时还看不到它 ——
        # 表现为"新环境明明加好了却起不来"，且报错指向 argparse 而不是配置。
        "--env", choices=list(_env_choices()), default=None,
        help="运行环境（默认取 MOSS_ENV，未设则 dev）。"
             "prod 会跑启动自检并拒绝不自洽配置；"
             "dev 会把数据/调度/审计改道到 data/dev/ 且禁用定时任务；"
             "pilot = **对外试点**：客户可访问，登录门槛自动强制、"
             "必须有真实 SMTP 凭据、数据改道到 data/pilot/，"
             "但单实例、无高可用、不承诺 SLA")
    p_start.add_argument("--host", default="127.0.0.1")
    p_start.add_argument("--port", type=int, default=DEFAULT_BACKEND_PORT)
    p_start.add_argument("--reload", action="store_true", help="开发热重载")
    p_start.add_argument("--daemon", action="store_true", help="后台运行")
    p_start.add_argument("--with-frontend", action="store_true",
                         help="同时启动前端 dev server")
    p_start.add_argument("--replace", action="store_true",
                         help="端口被本项目旧实例占用时，精确停止旧实例后重启")
    p_start.add_argument("--auto-port", action="store_true",
                         help="端口冲突时自动 +1 寻找空闲端口")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="停止本项目后台实例（不碰其他程序）")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="查看服务与依赖状态")
    p_status.set_defaults(func=cmd_status)

    p_ensure = sub.add_parser(
        "ensure",
        help="值守：后端不在就拉起来（配计划任务每分钟跑一次）")
    p_ensure.add_argument("--env", choices=list(_env_choices()), default="pilot",
                          help="要值守的环境（默认 pilot）")
    p_ensure.add_argument("--port", type=int, default=PILOT_BACKEND_PORT,
                          help=f"要值守的端口（默认 {PILOT_BACKEND_PORT}）")
    p_ensure.add_argument("--name", default="backend",
                          help="日志名（与 start 的命名一致，默认 backend）")
    p_ensure.add_argument("--verbose", action="store_true",
                          help="正常时也打印一行（默认静默，避免计划任务刷日志）")
    p_ensure.set_defaults(func=cmd_ensure)

    p_doctor = sub.add_parser(
        "doctor", help="体检/自愈本地 SQLite（陈旧 -wal/-shm 导致 disk I/O error）")
    p_doctor.set_defaults(func=cmd_doctor)

    p_test = sub.add_parser(
        "test", help="隔离临时数据目录运行 pytest；后续参数原样透传，如 test -q -k x",
    )
    p_test.set_defaults(func=cmd_test)

    p_fe = sub.add_parser("frontend", help="启动前端 dev server (5173)")
    p_fe.add_argument("--daemon", action="store_true")
    p_fe.set_defaults(func=cmd_frontend)

    p_build = sub.add_parser("build", help="构建前端到 web/dist")
    p_build.set_defaults(func=cmd_build)

    p_ship = sub.add_parser(
        "ship-frontend",
        help="把 web/dist 的**已验证**构建同步到对外试点实例（web/dist-pilot）")
    p_ship.set_defaults(func=cmd_ship_frontend)

    p_worker = sub.add_parser(
        "worker", help="Celery worker+beat（生产调度，需Redis；演示模式无需启动）")
    p_worker.add_argument("--daemon", action="store_true", help="后台运行")
    p_worker.add_argument("--replace", action="store_true", help="停止旧worker后重启")
    p_worker.add_argument("--pool", default="", help="执行池（Windows默认solo）")
    p_worker.add_argument("--loglevel", default="info", help="日志级别")
    p_worker.set_defaults(func=cmd_worker)

    p_logs = sub.add_parser("logs", help="查看后台服务日志 data/run/*.log")
    p_logs.add_argument("name", nargs="?", default="backend",
                        choices=["backend", "frontend", "worker"],
                        help="服务名（默认 backend）")
    p_logs.add_argument("--lines", type=int, default=200, help="打印尾部行数")
    p_logs.add_argument("-f", "--follow", action="store_true", help="跟随新日志")
    p_logs.set_defaults(func=cmd_logs)

    p_vacuum = sub.add_parser(
        "vacuum",
        help="回收 SQLite 空闲页（**必须先停服**；会重写整个库）")
    p_vacuum.add_argument(
        "--yes", action="store_true",
        help="跳过确认（脚本化时用）")
    p_vacuum.set_defaults(func=cmd_vacuum)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK：`start`/`stop` 成功后会打印 ✅/❌，
    # 直接抛 UnicodeEncodeError —— **命令本身已经执行了**，却在最后一步崩掉并
    # 返回非 0，用起来像"启动失败"。这里统一把输出改成 UTF-8（不可改的流跳过）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 被重定向 / 非文本流
            pass
    # parse_known_args：test 子命令后的 -q/-k 等 pytest 参数原样透传
    args, extras = build_parser().parse_known_args(argv)
    if args.command == "test":
        args.pytest_args = extras
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
