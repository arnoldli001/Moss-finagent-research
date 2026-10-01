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
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
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
    """命令行是否属于本项目**后端实例**（uvicorn src.api.main:app）。"""
    if not cmdline:
        return False
    low = cmdline.lower()
    return "src.api.main:app" in low or "src\\api\\main" in low


def is_our_worker_cmdline(cmdline: str | None) -> bool:
    """命令行是否属于本项目**调度 worker**（`python -m src.scheduler.worker`，`CHG-0139`）。

    ## 为什么要与 `is_our_cmdline` 分开（不能合成一个"是我们项目"的判据）

    两个调用点要的语义**不同**：

    * `diagnose_port(port)["is_ours"]` 问的是"**这个端口**上是不是我们的后端"
      —— worker **没有端口**，把它并进去凭空多出一种"端口被自己人占了"的假象；
    * `stop` / `--replace` 问的是"要停哪些进程" —— 这里**必须**包含 worker，
      否则拆分之后 `manage.py stop` 会留下一个**孤儿 worker**：
      它继续跑重作业、继续往行情仓写（正是 `CHG-0087` 那类双写事故的形状），
      而 `stop` 的输出会说"已停止"。

    所以：各问各的，需要"全部"的地方**显式并起来**（见 `stop_backend_processes`）。
    """
    if not cmdline:
        return False
    low = cmdline.lower()
    return "src.scheduler.worker" in low or "src\\scheduler\\worker" in low


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


#: "端口没人听"的复核次数 / 单次超时 / 间隔（秒）。
#:
#: ## ★ 为什么必须有重试（2026-09-27 P15 实测事故）
#:
#: `diagnose_port` 原来第一道门是一次 `port_open(timeout=0.4)`：**0.4 秒内连不上
#: 就返回 `occupied: False`**。这条判据把"忙"等同于"死"，后果是正反馈：
#:
#:     后端忙 >0.4s（冷启动 / 进程内调度跑数据作业 / 多实例争 SQLite 单写者）
#:       → 判"进程已消失" → `ensure` 再起一个
#:       → **Windows 允许重复绑定同一 LISTEN 端口**（与 `SO_REUSEADDR` 同一坑）
#:       → 两个实例争单写者 → 更慢 → 下一次探测更容易超时 → 继续复制
#:
#: 表现是"后端静默死亡、无崩溃日志、不走 lifespan、`in_job: none`"，
#: 而真相是**没有任何进程被杀**，只是有半秒没应答就被自己的值守判死并复制了一份。
#:
#: 所以单次超时**不许**作为结论；必须稳定复现才算"没人听"。
PORT_PROBE_ATTEMPTS = 3
PORT_PROBE_TIMEOUT = 1.0
PORT_PROBE_GAP_SECONDS = 0.5


def _connect_fails_consistently(port: int) -> bool:
    """连续多次 connect 都失败才算"连不上"（单次超时不作数，见上面的说明）。"""
    for attempt in range(PORT_PROBE_ATTEMPTS):
        if port_open("127.0.0.1", port, timeout=PORT_PROBE_TIMEOUT):
            return False
        if attempt + 1 < PORT_PROBE_ATTEMPTS:
            time.sleep(PORT_PROBE_GAP_SECONDS)
    return True


def diagnose_port(port: int) -> dict[str, object]:
    """返回端口占用诊断：{occupied, pid, is_ours, cmdline}。

    ## ★ 判据顺序（2026-09-27 修）：先问"有没有人 LISTEN"，再问"答不答"

    主判据是 **LISTEN 套接字**（`find_listening_pid`）：进程持有 LISTEN 就是活着，
    与它几毫秒内答不答无关。`port_open` 从"第一道门"降级为**复核手段**，
    且要求连续失败（`_connect_fails_consistently`）。

    这样 `ensure` 就不可能因为"后端忙了一下"而复制实例 —— 那正是 P15 的成因。

    ⚠️ **netstat 看不到 LISTEN、但 connect 能连上**是最矛盾的一种情形
    （netstat 解析失败 / 端口被识别不出归属的东西持有）。此时**按"有人在"处理**
    （`occupied: True` + `is_ours: False`）：`cmd_ensure` 会走"被别的程序占用"
    那条分支**拒绝启动**。宁可让值守报出来让人看，也绝不在可能已有实例的情况下
    再复制一个 —— 复制正是把可用性问题变成自己制造故障的那一步。
    """
    pid = find_listening_pid(port)
    if pid is None:
        if not _connect_fails_consistently(port):
            return {"occupied": True, "pid": None, "is_ours": False,
                    "cmdline": None, "note": "netstat 未见 LISTEN，但 connect 可连"}
        return {"occupied": False, "pid": None, "is_ours": False}

    cmdline = get_cmdline(pid)
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


def list_our_worker_pids() -> list[int]:
    """枚举本项目**调度 worker** 进程（`CHG-0139`）。

    判据与 API 实例完全同形：**按命令行匹配**，不看端口（worker 根本没有端口）、
    也不看心跳文件（心跳是"给人看的可见性"，不是"要不要重启"的判据 ——
    见 `cmd_ensure` 里"为什么不做健康检查失败就重启"那段）。
    """
    return _iter_our_pids(is_our_worker_cmdline)


def _iter_our_pids(match: Callable[[str | None], bool] = is_our_cmdline) -> list[int]:
    """按命令行枚举本项目的 Python 进程（多套手段依次兜底）。

    `match` 由调用方给：**API 实例**用 `is_our_cmdline`，**worker** 用
    `is_our_worker_cmdline`（两者刻意不合并，见后者的说明）。
    """
    if os.name != "nt":
        out = _run_text(["ps", "-eo", "pid=,args="], timeout=10)
        found: list[int] = []
        for line in out.splitlines():
            pid_text, _, cmdline = line.strip().partition(" ")
            if pid_text.isdigit() and match(cmdline):
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
        if pid_text.strip().isdigit() and match(cmdline):
            found.append(int(pid_text.strip()))
    if found:
        return found

    # 手段 2：wmic（老系统 / PowerShell 被策略限制时）
    out = _run_text(["wmic", "process", "where", "name like '%python%'",
                     "get", "ProcessId,CommandLine", "/format:csv"], timeout=25)
    for line in out.splitlines():
        if not match(line):
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

    ★ `CHG-0139`：**调度 worker 也算在内**。它没有端口，所以从前那套
    "按端口 + 按后端命令行"的枚举**看不到它** —— 拆分之后若漏掉它，
    `manage.py stop` 会留下一个孤儿 worker：它继续跑重作业（含往行情仓
    `upsert` 的 `quant_data_sync`），而命令输出说"已停止"。那正是
    `CHG-0087`（两个实例同时写同一个 14 GiB 库）的同一形状，只是这次
    第二写者是我们自己忘了停的进程。

    worker 的优雅路径与后端**同一条**（`request_graceful_stop` 发 CTRL_BREAK，
    `src/scheduler/worker.py` 里注册了 SIGBREAK 处理 ⇒ 会走 WAL checkpoint）；
    等不到才硬杀。它没有端口，所以额外等"进程真的退出"。
    """
    targets = list_our_backend_pids()
    workers = list_our_worker_pids()
    if not targets:
        pid = find_listening_pid(port)
        if pid is not None:
            info = diagnose_port(port)
            if info["is_ours"]:
                targets = [pid]
    results: list[tuple[int, bool]] = []
    if not targets and not workers:
        return results
    for pid in targets:
        request_graceful_stop(pid)
    # ★ worker 的优雅通道是**停止文件**，不是 CTRL_BREAK（2026-09-30 实测）：
    #   `request_graceful_stop` 要求调用方与目标**共享控制台**，而本项目所有
    #   守护进程都是 `CREATE_NO_WINDOW` 起的、值守又跑在计划任务里
    #   ⇒ 实测它**返回 False**，worker 只能被硬杀，而硬杀不 checkpoint
    #   （`sqlite_recovery.py` 开头那次 `disk I/O error` 的成因）。
    #   所以这里两条都发：CTRL_BREAK（万一有控制台）+ 停止文件（一定有效）。
    #
    #   `cmd_stop` 覆盖**所有**实例，所以停止文件也要覆盖所有已知环境 ——
    #   只写父进程那一档等于"看起来发了、其实发到别的目录"（实测踩过）。
    stop_files: list[Path] = []
    if workers:
        for pid in workers:
            request_graceful_stop(pid)
        for path in _worker_stop_files():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"stopped_by=manage.py pid={os.getpid()}\n", encoding="utf-8")
                stop_files.append(path)
                print(f"  已写停止文件 {path}")
            except Exception as exc:  # noqa: BLE001 拿不到停止通道不能挡住停止流程
                print(f"  ⚠️ 停止文件写入失败 {path}（{type(exc).__name__}: {exc}），"
                      f"将依赖硬杀兜底", file=sys.stderr)
    wait_port_closed(port, timeout=timeout)
    if workers:
        deadline = time.time() + timeout
        while time.time() < deadline and any(_pid_alive(p) for p in workers):
            time.sleep(0.2)
    for pid in [*targets, *workers]:
        if not _pid_alive(pid):
            results.append((pid, True))
            continue
        results.append((pid, kill_pid_tree(pid)))
    # 停止文件是"一次性"的：无论成功与否都清掉 —— 留着会让**下一次**启动的
    # worker 一睁眼就自杀（worker 侧还有"旧文件不许杀新进程"的第二道保险）
    for path in stop_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
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

#: 测试环境（`--env test`）的数据根目录。
#:
#: ★ **2026-09-28 新增（CHG-0063）**：在此之前 `--env test` **没有任何隔离分支** ——
#: `_prepare_environment` 只对 dev / pilot 注入路径，于是
#: `manage.py start --env test` 会静默落到 `MOSS_SQLITE_PATH` 的默认值
#: `data/moss_finagent.db`（本文件上方自己标注为**"生产用"**的那个库），
#: 缓存/审计/调度目录也一并共用。而同一个环境名在 `manage.py test` 里
#: 走的是 `build_test_env()`（重定向到临时目录）—— **同一个 `test` 有两套语义**，
#: 一个隔离、一个直连主库。这正是规则文档 §6 的反模式
#: 「同库同账号，靠 env 字段区分」。
TEST_ROOT = ROOT / "data" / "test"


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
        # ★ 访问审计也要按环境隔离（CHG-0063）：`MOSS_AUDIT_DIR` 的默认值是
        #   `data/audit`，而 `LLM_AUDIT_DIR` 的默认值**也是** `data/audit` ——
        #   dev 原来只重定向后者，于是访问审计一直写在三实例共用的目录里。
        "MOSS_AUDIT_DIR": f"{root}/access_audit",
        # ★ 缓存隔离（CHG-0063）：默认 `data/llm_cache` 是三实例共用的。
        "LLM_CACHE_DIR": f"{root}/llm_cache",
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


def test_isolation_env() -> dict[str, str]:
    """`--env test` 的隔离环境变量：**跑得起来的测试环境**，与 dev/pilot 平级。

    ## 为什么必须补这一档（2026-09-28 · CHG-0063）

    规则文档（数据库管理）§1 的"最低限度"要求是
    「同实例不同 database/schema + prod 账号只读 + dev 账号不能跨 schema 写」，
    §6 把「同库同账号，靠 `env` 字段区分」列为**反模式第一条**。

    本项目原先的实际情况比反模式更糟：`--env test` 连"不同 database"都没做到 ——
    `_prepare_environment` 只给 dev / pilot 注入路径，`test` 因此拿到
    `MOSS_SQLITE_PATH` 的**默认值** `data/moss_finagent.db`（本文件标注为"生产用"）。
    而 `manage.py test` 走的是 `build_test_env()`（临时目录）。
    **同一个 `test` 两套语义**，且其中一套直连主库、缓存与审计全部共用。

    ## 与 `build_test_env()` 的分工（不是重复实现）

    | 入口 | 用途 | 库的位置 | LLM 缓存 |
    |---|---|---|---|
    | `manage.py test` → `build_test_env(tmp)` | 跑单测 | **一次性临时目录** | **禁用** |
    | `manage.py start --env test` → 本函数 | 长期测试实例 | `data/test/` | 独立目录 |

    两者都满足"隔离"，差别只在生命周期：单测要"每次从零"，实例要"重启后数据还在"。
    """
    root = str(TEST_ROOT)
    return {
        "MOSS_ENV": "test",
        "MOSS_SQLITE_PATH": f"{root}/moss_test.db",
        "SCHEDULER_DIR": f"{root}/scheduler",
        "LLM_AUDIT_DIR": f"{root}/audit",
        "MOSS_AUDIT_DIR": f"{root}/access_audit",
        # ★ 缓存隔离（规则文档 §1「缓存必须隔离」/§6「dev 和 prod 共用缓存」）：
        #   `llm_cache_dir` 默认是 `data/llm_cache`，三个实例本来共用同一份 ——
        #   而本项目的 LLM 缓存 scope **不含 provider/model**（见 AGENTS.md
        #   "优化前后对比必须先清测量路径"），共用会让跨环境的对比结果直接失真。
        "LLM_CACHE_DIR": f"{root}/llm_cache",
    }


def _scheduler_scope_line(extra_env: dict[str, str]) -> str:
    """**目标环境**的调度作用域（横幅用）—— 只印**算得出来**的东西（`CHG-0087`）。

    为什么要印：写权限归属只在**一处**声明（`configs/data_stores.yaml` 的
    `warehouse.writer`），它连带决定"这台实例跑不跑行情更新作业"。
    运营者看不到这行字就只能靠记得 —— 而本项目已经为"靠记得"付过一次代价：
    pilot 的横幅长期印着"定时任务已关闭"，实际它跑了 38 个。

    算不出来时**如实说"未量到"**，绝不回退成一句听起来没问题的默认值
    （`AGENTS.md`：宁可不显示，也不显示假绿）。

    ## ★★ 为什么必须收 `extra_env`（2026-09-30 实测，此参数就是那次修复）

    本函数原先**不收参数**、直接 `scheduler_scope_report()` —— 于是它读的是
    **父进程**的 `os.environ`。而 `--env pilot` 只是一个**命令行参数**，
    父进程里并没有 `MOSS_ENV`，`data_stores.current_env()` 因此退化成 `'dev'`。

    后果是**反的**，而且恰好错在最要命的那一档：横幅对 pilot 印

        · 定时任务：39/42 个会触发；因**写权限归属**被裁：…quant_data_sync

    而 pilot **恰恰是行情仓唯一的写者**，这条作业在它上面**照跑** —— 同一个
    子进程 25 秒后自己在日志里印「48/48，env=pilot」，全库 `裁剪记录 0 条`。
    一个运营者若照横幅去"修"（把 `warehouse.writer` 改回 `main`），就会把
    dev 与 pilot 重新变回**两个写者**，正是 `CHG-0087` 要防的那次事故
    （两实例在同一分钟往同一个 14.36 GiB 库里 upsert）。

    为什么只有 pilot 中招：dev / prod 的目标环境名与"父进程退化值"算出来
    **恰好同解**（两者对 `warehouse` 都是只读），所以三个实例里只有 pilot
    这一行是错的 —— 这也正是它能活下来的原因。

    这与本文件 `_prepare_environment` 里 `Settings()` 踩过的是**同一个坑**
    （见那段"父 shell 里没有 MOSS_ENV → 目标环境规则整段不执行"的说明）：
    判据必须在**即将生效**的那份环境里求值。参数**故意不设默认值** ——
    留一个无参调用就等于给下一个调用点留了同一条错路。
    """
    try:
        from src.scheduler.catalog_jobs import install_catalog_jobs
        from src.scheduler.registry import load_dynamic_jobs, scheduler_scope_report

        # ★ 在目标环境的变量下求值（父进程的 MOSS_ENV 与 --env 无关）
        with _temporary_environ(extra_env):
            # ★★ 2026-09-30（`CHG-0129`）：**还要在"运行时"那一层求值**。
            #
            # 静态 `JOB_REGISTRY` 只有 **43** 个，而应用启动时
            # `install_catalog_jobs()` 会动态注册 **6** 个
            # （`catalog_daily/weekly/monthly/quarterly/calendar` + `gap_drain`）
            # ⇒ 真实的调度器跑的是 **49** 个（pilot 日志逐字为证：
            # 「进程内Cron调度器已启动（49/49个作业，环境=pilot）」）。
            #
            # 此前横幅印「43/43 个会触发（没有作业因写权限归属被裁）」——
            # 这行字**读起来是一句完整的健康结论**（分子分母相等、还声明没裁任何
            # 作业），实际却**少报了 6 个作业**，其中就包括 `gap_drain` ——
            # 我自己两次把它误判成"死分支"，正是因为静态视图里看不见它
            # （`CHG-0125` 记了第一次，`CHG-0128` 记了第二次）。
            #
            # 本函数 docstring 早就写了正确的原则："判据必须在**即将生效**的那份
            # 环境里求值"。这次是把**同一条原则**从"环境变量"这一层，
            # 推到"作业注册表"这一层。两次都是同一个形状：
            # **在错的层上求值，然后拿到一个看起来很确定的错答案。**
            install_catalog_jobs()  # 幂等（`CHG-0125` 有判据）
            load_dynamic_jobs()     # 读 `_schedule.json`；不存在则注册 0 个
            scope = scheduler_scope_report()
    except Exception as exc:  # noqa: BLE001 横幅不该因为登记表读不到就崩
        return (f"   · 定时任务：本实例作用域**未量到**"
                f"（{type(exc).__name__}: {exc}）")
    #: 带上 env：让这行字**自证**它描述的是哪一档。写错档位时运营者
    #: 当场就能看出来，而不是拿到一个"看起来很确定"的错答案。
    who = f"env={scope['env']}"
    role = scope.get("role") or "all"
    #: ★ `CHG-0139`：角色外的作业**必须单独说**，否则会被读成"这台实例裁掉了它们"，
    #: 而真相是"它们由独立 worker 进程负责"（没起 worker 时它们**确实不在跑**）。
    role_note = ""
    if scope.get("out_of_role"):
        role_note = (f"；本进程角色 role={role}，"
                     f"**不跑** {len(scope['out_of_role'])} 个重作业"
                     f"（{ '、'.join(scope['out_of_role'][:3]) }…，"
                     "由 `manage.py start-worker` 负责）")
    #: ★ `CHG-0139`：光说"移出了、由 worker 负责"是**半个结论** ——
    #: worker 到底在不在，才是"那几个作业此刻跑不跑"的答案。两者必须同一行。
    worker = scope.get("worker") or {}
    worker_note = ""
    if worker.get("needed"):
        age = worker.get("age_sec")
        # ★ 措辞必须与**证据强度**匹配（2026-09-30 上线实测踩到）：陈旧阈值是 90 秒，
        #   所以一个**已经死了 86 秒**的 worker 仍在阈值内 ⇒ 第一版横幅照旧印
        #   「调度 worker **在跑**」。那是**假绿**：读者拿到的是结论，不是证据。
        #   现在分三档说 —— 刚刚 / 偏旧 / 未在运行，且始终把**心跳年龄**印出来。
        #   `fresh` 由 `worker_requirement()` 给（阈值只有一处，见那里的说明）。
        if worker.get("alive") and worker.get("fresh"):
            worker_note = (f"；调度 worker **在跑**（PID={worker.get('pid')}，"
                           f"{age} 秒前心跳）")
        elif worker.get("alive"):
            worker_note = (f"；调度 worker 心跳**偏旧**（PID={worker.get('pid')}，"
                           f"{age} 秒前心跳，陈旧阈值 90 秒）"
                           "—— 请在下一轮值守后复查")
        else:
            worker_note = ("；⚠️ 调度 worker **未在运行**（"
                           f"{worker.get('verdict') or '心跳不可用'}）"
                           "⇒ 那 4 个重作业此刻**不会执行**")
    if not scope["pruned"]:
        return (f"   · 定时任务（{who}）：{scope['active']}/{scope['total']} "
                f"个会触发（没有作业因写权限归属被裁）{role_note}{worker_note}")
    names = "、".join(item["job"] for item in scope["pruned"])
    return (f"   · 定时任务（{who}）：{scope['active']}/{scope['total']} 个会触发；"
            f"因**写权限归属**被裁：{names}{role_note}{worker_note}")


def cmd_start_worker(args: argparse.Namespace) -> int:
    """启动**调度 worker 进程**：只跑那 4 个重作业（`CHG-0139`）。

    ## 它与 `manage.py start` 的三点区别

    ① **不提供 HTTP**（没有端口、没有前端）—— 它挂掉时前端不会报不可达；
    ② `MOSS_SCHEDULER_ROLE=worker` ⇒ `schedulable_jobs()` **只返回** `HEAVY_JOBS`
       （API 进程设 `api` ⇒ 反过来不跑它们）—— 两边**不相交**、合起来是全集；
    ③ 与 API 进程**共用同一套隔离环境**（同一个库、同一份 `runs.jsonl`），
       否则"谁跑了多久"会分成两份账，而这次事故正是靠那份账才定位到的。

    ⚠️ **它必须与 API 进程成对起**：只起 API（role=api）而 worker 没起
    ⇒ 那 4 个重作业**不会有人跑**（启动横幅会如实说"不跑 N 个重作业…由
    start-worker 负责"，`/health` 的 scope 里也带 `out_of_role`）。
    判据：`tests/unit/test_scheduler_role_split.py`。
    """
    env_name = args.env
    extra = worker_env(env_name)
    if extra is None:
        print(f"❌ --env {env_name} 没有隔离环境定义（可选：pilot / dev）", file=sys.stderr)
        return 2
    cmd = [sys.executable, "-m", "src.scheduler.worker"]
    if args.daemon:
        pid = _spawn_worker(env_name)
        if pid is None:
            print(f"❌ --env {env_name} 没有隔离环境定义（可选：pilot / dev）", file=sys.stderr)
            return 2
        print(f"✅ 调度 worker 已后台启动（env={env_name}, role=worker, PID={pid}）")
        print("   日志: data/run/scheduler-worker.log｜停止: python manage.py stop")
        print("   ⚠️ 重作业（intel_tone_extract / event_alert_intraday / "
              "quant_data_sync / mainline_daily）现在只由它执行")
        return 0
    print(f"▶ 调度 worker 前台运行（env={env_name}, role=worker），Ctrl+C 停止…")
    try:
        return subprocess.run(cmd, cwd=str(ROOT), check=False,
                              env={**os.environ, **extra}).returncode
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


def worker_env(env_name: str) -> dict[str, str] | None:
    """worker 进程要用的隔离环境：**与 API 进程同一套** + `role=worker`。

    为什么必须复用 API 那套 maker（而不是在这里自己拼一份）：两边只要有一项
    不同（库路径 / 审计目录 / 调度目录），"谁跑了多久"就会分成两份账，
    而这次事故正是靠那一份账（`runs.jsonl`）才定位到的。
    """
    maker = {
        "pilot": pilot_isolation_env,
        "dev": dev_isolation_env,
    }.get(env_name)
    if maker is None:
        return None
    extra = dict(maker())
    extra["MOSS_SCHEDULER_ROLE"] = "worker"
    return extra


def env_needs_worker(env_name: str) -> bool:
    """该环境的 **API 进程**是否已把重作业移出进程（⇒ 必须有 worker 在跑）。

    ★ 判据**从 API 的隔离环境派生**（`MOSS_SCHEDULER_ROLE == "api"`），
    不在任何地方写第二份"哪些环境要 worker"的清单 ——
    两份清单必然漂移，而漂移的症状正是这次要消灭的那个：
    **API 不跑重作业，而没有人跑**。
    """
    maker = {
        "pilot": pilot_isolation_env,
        "dev": dev_isolation_env,
    }.get(env_name)
    if maker is None:
        return False
    return maker().get("MOSS_SCHEDULER_ROLE") == "api"


def _spawn_worker(env_name: str) -> int | None:
    """后台起一个 worker，返回 PID（环境不支持时 None）。**唯一的启动实现**。"""
    extra = worker_env(env_name)
    if extra is None:
        return None
    cmd = [sys.executable, "-m", "src.scheduler.worker"]
    return _spawn_daemon("scheduler-worker", cmd, ROOT, env=extra)


def _probe_worker_lock() -> bool | None:
    """调度 worker 的单实例锁**现在被持有吗**？→ `True`/`False`／探测失败 `None`。

    ★ 为什么必须是三态而不是 bool：它有两个用途，而两者的"读不懂"处置不同 ——
      · `worker_present()` 把 `True` 当**操作系统级证据**（有人在跑）；
      · 取证附注用它把"记录未标记"分成「还在跑」与「已死于无痕」
        （见 `src/scheduler/worker.py::last_run_report` 的 `run_ended`）；
        这里**探测失败绝不能读成"没人跑"**（那会凭空报一次"上次异常终止"）。
    """
    try:
        from src.scheduler.worker_lock import WorkerLock

        lock = WorkerLock()
        if lock.acquire(env="probe"):
            lock.release()
            return False
        return True
    except Exception:  # noqa: BLE001 锁探测失败按"不知道"处理（后面还有兜底）
        return None


def _worker_last_run_note(lock_held: bool | None) -> str:
    """最近一次 worker 是**怎么结束**的 → 一行附注（`""` = 无需提）。

    ## 为什么要在存活报告里带上这个（2026-09-30 已核实的事故）

    `py_mini_racer`(V8) 并发导致**原生崩溃**（退出码 `0x80000003`、**没有 Python
    traceback**），pilot 的 worker 就这样"无痕死亡"过一次 —— 值守把它拉起来之后
    公网 502 数分钟。也就是说：**"拉起来"救不了下一次**，而"上次是怎么死的"
    当时一个字都没留下。取证记录（`src/scheduler/worker.py::last_run_report`）
    就是那份痕迹，这里是它被人看到的入口之一（另一处是 worker 自己的启动日志）。

    `lock_held`：本进程刚探到的锁状态（三态）。记录未被标记时，**只有**它能区分
    「这次还在跑」与「它已死于无痕」—— 不知道（`None`）时**不猜**。

    ⚠️ 它是**附注，不是第四路存活证据**：
      · "上次异常终止"**不能**证明"现在没人跑"（很可能已经有人拉起来了）；
      · "上次正常"也**不能**证明"现在有人跑"。
    所以它只加在证据句后面，绝不参与 `worker_present()` 的判在/判不在
    —— 那三路（进程/锁/心跳）的语义一个字都不动。
    """
    try:
        from src.scheduler.worker import last_run_report

        rep = last_run_report(run_ended=None if lock_held is None else (not lock_held))
    except Exception:  # noqa: BLE001 取证读不到不该影响存活判断
        return ""
    if not rep.get("needs_attention"):
        return ""
    return str(rep.get("note") or "")


def worker_present() -> tuple[bool, str]:
    """worker 到底在不在？→ `(在不在, 证据)`。**三路证据**，任一成立即算在。

    ## 为什么要三路（2026-09-30 实测：枚举会抖动）

    第一版只用 `list_our_worker_pids()`（命令行枚举，经 PowerShell CIM）。
    实测在**机器负载高**时它会有一次返回空（CIM 查询超时 → 回退 wmic →
    也没有）⇒ `ensure` 判成"没在跑" → 白起一个 worker（被单实例锁拦住，
    日志里留下一句 ERROR，值守还报了一条"启动后立即退出"的假警报）。
    锁救了它，但"判据抖动 ⇒ 报假警"本身必须修掉。

    ## 三路证据（从强到弱）

    1. **进程**：命令行枚举（正常路径，也是"要不要拉起来"的判据）；
    2. **单实例锁**：**操作系统级**，拿不到锁就 100% 说明有人持着它 ——
       这条路不依赖任何枚举、不会抖动（代价：要真的开一次文件句柄）；
    3. **心跳**：由 worker 的**独立线程**写，新鲜即说明进程活着
       （阈值与陈旧判据同源，见 `worker_heartbeat`）。

    三路**任一**成立即算"在"，并把这句证据带出去 —— 让"枚举没看见但心跳在"
    这种情况**留痕**（它说明第一路仪器不可靠，值得知道）。

    ★ 另附**最近一次跑完的是怎么结束的**（`_worker_last_run_note()`）：
    异常终止/异常退出过就缀在证据后面（判"在"时）或单独作为理由（判"不在"时）
    —— 这两种情况下运维要做的第一件事都是"看上次怎么死的"，而不是"再拉一次"。
    ⚠️ **调用方不许把 `why` 的"非空"当成"在跑"**：判断一律只看第一个返回值
    （失败路径的 `why` 只装那句附注）。
    """
    pids = list_our_worker_pids()
    #: 锁探测顺带供取证附注使用（枚举到进程时不必再探：那次运行显然还在跑）
    held = None if pids else _probe_worker_lock()
    note = _worker_last_run_note(held)
    if pids:
        return True, f"命令行枚举到进程 {pids}" + (f"；{note}" if note else "")
    try:
        from src.scheduler.worker_heartbeat import read_status

        st = read_status()
        if st["alive"]:
            return True, (f"命令行**没枚举到**进程，但心跳 {st['age_sec']} 秒前"
                          f"（PID={st['pid']}）—— 第一路仪器这次不可靠"
                          + (f"；{note}" if note else ""))
    except Exception:  # noqa: BLE001 心跳读不到不该影响判断
        pass
    if held is True:
        return True, ("单实例锁被持有（操作系统级证据，不依赖枚举）"
                      + (f"；{note}" if note else ""))
    return False, note


def ensure_worker(env_name: str, *, verbose: bool = False) -> int:
    """**重作业有没有人在跑**：没有就起一个（`CHG-0139`）。返回 0 即为正常。

    退出码：`0` 正常/已恢复；`4` 该有 worker 而起不来（**必须让人看见**）。

    ## 为什么判据是"进程在不在"，不是"心跳新不新"

    与 `cmd_ensure` 同一条纪律（那段 docstring 里有完整论证）：**不拿"忙"当"死"**。
    心跳由 worker 的**独立线程**写（`worker_heartbeat`），所以它确实很准；
    但"心跳旧了"仍然可能是启动瞬间（`build_runtime` 还没跑完）、磁盘卡顿、
    或计划任务与手工启动撞车。用"进程在不在"当重启判据，这些情形都不会误判成
    "需要再起一个"；而心跳的用途是**给人看**（`/health` 的 `worker` 段、
    启动横幅、`cmd_status`）。

    ## 为什么它挂在 `cmd_ensure` 里（而不是新写一个值守）

    `scripts/pilot_watchdog.ps1` 的文件头写明它是**全项目唯一的运行期值守入口**
    （每分钟一轮）。重作业停摆与后端停摆是同一个量级的事故（都是"服务看起来
    正常、实际有东西不再更新"），所以走同一个入口、同一份事件流水
    （`backend_incidents.jsonl`），不再造第二个值守。
    """
    if not env_needs_worker(env_name):
        return 0
    present, why = worker_present()
    if present:
        if verbose:
            print(f"✅ 调度 worker 在跑（{why}），重作业有进程负责。")
        return 0
    if why:
        # ★ 上次是**异常终止/异常退出**：这句必须说在"再拉一次"**之前** ——
        #   拉起来只是止血，而"上次怎么死的"才是下次不再死的唯一线索
        #   （原生崩溃连 traceback 都没有，见 `src/scheduler/worker.py` 的取证一节）。
        #   它也是值守日志里唯一会出现的取证结论（值守每轮都会走到这里）。
        print(why, file=sys.stderr)

    pid = _spawn_worker(env_name)
    if pid is None:
        return 4
    _record_incident("worker_restart", env=env_name, pid=pid,
                     reason="重作业无人执行：未发现调度 worker 进程")
    # 起来之后**等一小下**再判活：worker 可能立刻退出（例如拿不到单实例锁，
    # 退出码 3；或 import 失败）。"启动了"与"在跑"必须分开说 ——
    # 只报"已启动"而它 0.5 秒后就死了，等于把事故又藏起来。
    #
    # ⚠️ 等待时长是**用户停机窗口**的一部分（`restart-pilot` 的启动段实测 15.7 s，
    #    其中 4 秒是这个循环）。取 1 秒足够抓"立刻退出"（实测那种失败在 <0.5 秒内
    #    就 exit），而 4 秒纯粹是在替用户多等。
    for _ in range(5):
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    if not _pid_alive(pid):
        # ★ 再问一次"到底在不在"：它可能是被**单实例锁**挡下的（说明本来就有
        #   一个在跑，只是第一路仪器没看见）—— 那种情况不是故障，别报假警。
        present2, why2 = worker_present()
        if present2:
            print(f"✅ 调度 worker 已在运行（{why2}）；本次启动的新进程自行退出"
                  f"（PID={pid}），未重复执行")
            return 0
        print(f"❌ 调度 worker 启动后立即退出（PID={pid}）⇒ 重作业仍无人执行；"
              f"见 data/run/scheduler-worker.log", file=sys.stderr)
        _record_incident("worker_restart_failed", env=env_name, pid=pid)
        return 4
    print(f"✅ 已拉起调度 worker（env={env_name}, PID={pid}）—— 重作业恢复执行")
    return 0


def pilot_isolation_env() -> dict[str, str]:
    """`--env pilot` 的隔离环境变量：对外试点，**数据与 dev/生产彻底分开**。

    与 `dev_isolation_env` 的区别（这不是"复制一份改个路径"）：

    | | dev | pilot |
    |---|---|---|
    | 数据目录 | `data/dev/` | `data/pilot/` |
    | 谁能访问 | 只应本机 | **客户（公网，经 Cloudflare）** |
    | 通知通道 | 无凭据时打日志 | **必须有真实 SMTP 凭据**（自检强制） |
    | 登录门槛 | 关（本地调试方便） | **自动强制**（`LoginGateMiddleware`，见 `is_public`） |
    | 定时任务 | **只有行情更新作业被裁** | **全部触发**（含行情更新） |

    ★ 上表最后一行是 **2026-09-29（`CHG-0087`）** 落定的写权限归属，它取代了
    此前一段**从未生效**的写法：

    > ~~「定时任务同样关闭……两个调度器同时写同一个仓库会造成重复下载与
    >   行级竞争。所以试点的定位是『只读行情 + 独立账号库』，行情数据由既有
    >   实例刷新。这条限制写在启动输出里。」~~

    那段话的**判断是对的、实现是空的**：它靠 `MOSS_SCHEDULER_ENABLED=0` 表达，
    而那个环境变量全仓库**零处读取**（`src/api/main.py` 里
    `CronScheduler(...).start()` 是无条件的）。实测后果：pilot 跑了 **43** 条
    `quant_data_sync` 记录，`data/dev/scheduler` **33** 条、`data/scheduler`
    **7** 条 —— **三个实例在同一个 14.36 GiB 文件上写**，其中 dev 与 pilot 的
    最后一班落在**同一分钟**（23:30:0x）。"写在启动输出里"的那句提醒也没兑现
    （横幅印的是"已关闭"，与实际相反）。

    现在的实现是**声明式 + 派生**，不是开关：
      · 归属写在 `configs/data_stores.yaml` 的 `warehouse.writer: pilot`（一处）；
      · 写闸门在 `QuantWarehouse`（`assert_writable()` + 连接级
        `PRAGMA query_only=1`），非写者实例**写不进去**；
      · 更新作业（`JobSpec.updates` 声明了 `warehouse` 的那些）在非写者实例上
        **根本不会被触发**（`scheduler.registry.schedulable_jobs()`）。
    实测四档：main / dev / test 一律只读（读 15,426,322 行照常），pilot 可写。

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
        # ★ 2026-09-29（CHG-0087）起，按实例裁剪任务**已经真的实现了**，而且是
        #   派生的（不需要在这里写任何东西）：
        #     `JobSpec.updates`（作业声明它会写哪些存储）
        #     × `data_stores.writable_here()`（本实例能不能写）
        #     → `scheduler.registry.schedulable_jobs()`
        #   pilot 是行情仓的写者（`warehouse.writer: pilot`），所以它的
        #   `quant_data_sync` **照跑**；dev/主实例那一份会被自动裁掉。
        #   `MOSS_SCHEDULER_DENY`（逗号分隔的**作业名**）只作为临时开关保留，
        #   长期差异必须落进上面那两处声明 —— 否则"两台机器为什么不一样"没人说得清。
        # ★ 前端资源指向**冻结副本**，不指向 dev 正在用的 `web/dist`。
        #   否则本地 `npm run build` 一跑，客户刷新就拿到那份还没验过的界面。
        #   同步方式见 `manage.py ship-frontend`：
        #     验证（dev 8100）→ ship-frontend → 客户刷新即见。
        "MOSS_WEB_DIST": str(ROOT / "web" / "dist-pilot"),
        # ★ 缓存隔离（CHG-0063）：与 dev/test 分开，避免跨环境污染。
        "LLM_CACHE_DIR": f"{root}/llm_cache",
        # ★★ 2026-09-30（`CHG-0139`）：**pilot 的 API 进程不跑那 4 个重作业**。
        #
        # 为什么只给 pilot：dev 是本机开发实例（没人在用、也不对外），
        # 拆进程只会让"起服务"多一步；pilot 是**对外**实例，用户点一下就撞上
        # 事件循环被重活占住（实测三次报"后端不可达"，请求最长排队 64.4 秒）。
        #
        # 重作业由 `python manage.py start-worker --env pilot` 起独立进程负责；
        # 角色未设置时行为与从前逐字一致（全跑）—— 所以这里**显式**设置，
        # 而不是靠"默认值恰好是对的"。
        "MOSS_SCHEDULER_ROLE": "api",
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
    elif name == "test":
        # ★ 2026-09-28（CHG-0063）：原先没有这一支 → `--env test` 静默落
        #   `data/moss_finagent.db`（"生产用"）。现在与 dev/pilot 平级隔离。
        extra = test_isolation_env()

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
        print(_scheduler_scope_line(extra), file=sys.stderr)
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
        # ★ 2026-09-29（CHG-0087）**改成机制，而不是继续印提醒**：
        # 用户裁定「共享行情仓，dev 读，pilot 写和读；谁负责更新数据谁有写权限」，
        # 于是 pilot **就是**行情仓的写者 —— 它的 `quant_data_sync` 应当跑，
        # dev/主实例的那一份会被**派生裁剪**（`JobSpec.updates` ×
        # `data_stores.writable_here()`，见 `src/scheduler/registry.py`）。
        # 所以这里印的是**算出来的作用域**，不是一句希望。
        # ⚠️ 必须把 `extra` 传进去：不传就会拿**父进程**的环境问，
        #    而父进程没有 MOSS_ENV ⇒ 退化成 dev ⇒ 印出"pilot 裁掉了
        #    quant_data_sync"这个**与事实相反**的结论（2026-09-30 实测）。
        print(_scheduler_scope_line(extra), file=sys.stderr)
        print(f"   · 建议端口：--port {PILOT_BACKEND_PORT}"
              f"（与 dev 的 8100 并存；两实例各用独立的库）", file=sys.stderr)
        print("   · ⚠️ **不要用 --replace**：它按命令行枚举本项目**全部**后端正"
              "进程，会把正在跑的 dev/主实例一起停掉", file=sys.stderr)
        print("   · 不承诺 SLA；多租户 DataClass 平面尚未与会话打通"
              "（见设计文档 §13.8）", file=sys.stderr)
        print("=" * 68, file=sys.stderr)
    elif name == "test":
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        print(f"🧪 test 隔离实例：数据目录 {TEST_ROOT}"
              "（与 dev / pilot / 主库分开；单测请用 `manage.py test` 的临时库）",
              file=sys.stderr)
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
                others = [p for p in list_our_backend_pids() if p != pid]
                refusal = replace_refusal_reason(others)
                if refusal:
                    print(f"❌ {refusal}", file=sys.stderr)
                    _record_incident("replace_refused", port=port, pid=pid,
                                     others=others[:10])
                    return 2
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
                # ★ 这条早退分支**也必须**过一遍 worker：运维最可能的动作是
                #   "服务已经在跑，那我就再敲一次 start" —— 如果这一支不管 worker，
                #   那"重作业无人执行"就恰好藏在最常走的那条路径后面。
                return ensure_worker(extra_env.get("MOSS_ENV", "dev"))
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
        # ★ `CHG-0139`：该环境的 API 若已把重作业移出进程（`role=api`），
        #   那"起后端"就必须**连带**保证 worker 在跑 —— 否则这一条命令的
        #   净效果是"服务起来了、4 个重作业从此不再执行"，而输出全是 ✅。
        #   判据从 API 的隔离环境派生（`env_needs_worker`），不写第二份清单。
        worker_code = ensure_worker(extra_env.get("MOSS_ENV", "dev"))
        if worker_code != 0:
            print("⚠️ 调度 worker 未能拉起 ⇒ 那 4 个重作业不会执行，"
                  "见上面的报错", file=sys.stderr)
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
            # ★ `CHG-0139`：`stop_backend_processes` 现在**也**会停调度 worker
            #   （见那里的说明）。这里先记下哪些 PID 是 worker，只为了把输出
            #   说对 —— 把 worker 印成"backend"会让人以为后端起了两个。
            workers_before = set(list_our_worker_pids())
            results = stop_backend_processes(port)
            for pid, ok in results:
                label = "调度worker" if pid in workers_before else name
                stopped.append((label, pid, ok))
            (RUN_DIR / "scheduler-worker.pid").unlink(missing_ok=True)
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
    # 调度 worker（无端口 ⇒ 只能看进程 + 心跳；★ `CHG-0141`）
    #
    # 为什么必须单独一行：它跑的是**被移出在线进程的那 4 个重作业**。
    # 不印这一行的话，`status` 对一个"后端全绿、而那 4 个作业其实没人跑"
    # 的现场完全无感 —— 这正是这次拆分引入的新静默失效。
    #
    # ★ 为什么要在 **pilot 的环境里**求值（第一版没做，于是这行字恒为
    #   「本实例不需要」——`status` 跑在运维的 shell 里，父进程没有
    #   `MOSS_SCHEDULER_ROLE`）：与 `_scheduler_scope_line()` 的 `CHG-0112`
    #   是同一个坑 —— **判据必须在"即将生效"的那份环境里求值**。
    #   要在没有角色的父进程里回答"对外试点需不需要 worker"，只能借它的隔离环境。
    wk_pids = list_our_worker_pids()
    try:
        from src.scheduler.registry import worker_requirement

        with _temporary_environ(pilot_isolation_env()):
            req = worker_requirement()
        if not req["needed"]:
            rows.append(("调度 worker", "—", "本实例不需要",
                         f"（{req['verdict']}）"))
        elif wk_pids and req["fresh"]:
            rows.append(("调度 worker", "—", f"运行中(PID={wk_pids[0]})",
                         f"为对外试点(pilot)而设｜{req['age_sec']} 秒前心跳｜"
                         f"负责 {len(req['heavy_jobs'])} 个重作业"))
        elif wk_pids:
            rows.append(("调度 worker", "—", "心跳偏旧",
                         f"PID={wk_pids[0]}，{req['age_sec']} 秒前心跳"
                         "（陈旧阈值 90 秒）｜data/run/scheduler-worker.log"))
        else:
            rows.append(("调度 worker", "—", "❌ 未在运行",
                         f"pilot 的 {len(req['heavy_jobs'])} 个重作业当前**无人执行**｜"
                         "python manage.py start-worker --env pilot --daemon"))
    except Exception as exc:  # noqa: BLE001 状态查询不该因为心跳读不到就崩
        rows.append(("调度 worker", "—", "**未量到**",
                     f"（{type(exc).__name__}: {exc}）"))
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
    """隔离环境运行 pytest：所有数据目录重定向到临时目录。

    ## 为什么**经子进程 + Job Object** 跑，而不是进程内 `pytest.main()`

    原来是进程内调用。那样本身不会产生孤儿（pytest 与本进程同生共死），
    但实测**有人/AI 直接调 pytest**（`python -m pytest tests/unit ...`），
    于是留下了一个卡死的孤儿：父进程已退出、CPU = 0、HandleCount = 0、
    **一天多没动过**，却一直占着资源与数据库句柄。

    根因两条（缺一不可）：

      1. **Windows 没有 `PR_SET_PDEATHSIG`** —— 内核不会在父进程死亡时
          自动回收子进程（Linux 上的默认配套机制，Windows 上不存在）；
      2. **pytest 自己不检测父进程** —— 父一死它就成了无主进程继续挂着。

    所以这里统一走 `scripts/run_tests_in_job.py`：它把 pytest 放进一个
    `KILL_ON_JOB_CLOSE` 的 **Job Object**，父进程一旦消失，
    **内核**就连带终止整棵测试进程树。机制保证，不依赖谁记得清理。

    ⚠️ 用 `sys.executable` 而不是拼 `python`：必须与当前解释器**同一个**，
    否则可能跑到另一个环境（实测过 VN Studio 自带的 python 与项目 `.venv`
    两套并存，跑出完全不同的结果）。
    """
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
    print("[test] 经 Job Object 运行：父进程消失时测试进程树一并被终止"
          "（杜绝孤儿）", file=sys.stderr)

    runner = ROOT / "scripts" / "run_tests_in_job.py"
    if not runner.exists():
        # 兜底：运行器不在就退回进程内（行为与改动前一致，不会更差）
        import pytest

        return int(pytest.main(list(getattr(args, "pytest_args", []))))
    return subprocess.run(
        [sys.executable, str(runner), *getattr(args, "pytest_args", [])],
        cwd=str(ROOT), check=False).returncode


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


def memory_snapshot() -> dict[str, object]:
    """当前内存/提交量快照（**只读**，用于给事故现场留证据）。

    ## 为什么要记这个数

    "服务被静默终止"最常见的两类诱因都与内存有关：
    **提交量耗尽**（Windows 真正会杀进程的条件，物理内存不够只会去换页）
    和**页面文件太小**。事后追查时，如果流水里只有"某时刻端口空了"，
    根本判断不了当时是不是内存触顶 —— 而那一刻的数值**过去了就没了**。

    所以每次发现进程消失都顺手记一份，下一次就能直接回答
    "是不是内存问题"，而不是靠推测。
    """
    out: dict[str, object] = {}
    try:
        # ⚠️ 让 PowerShell 只回**字节数**，单位换算放在 Python 里做：
        #    第一版在 PS 里除 `1KB`（应是 1MB），于是"可用内存"算出 2909 GB
        #    这种离谱值 —— 而它看起来只是个数字，不核对就发现不了。
        info = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$o=Get-CimInstance Win32_OperatingSystem;"
             "$p=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory;"
             "'{0}|{1}|{2}|{3}' -f "
             "[int64]$o.FreePhysicalMemory, [int64]$o.TotalVisibleMemorySize,"
             "[int64]$p.CommittedBytes, [int64]$p.CommitLimit"],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
            creationflags=_NO_CONSOLE).stdout.strip()
        free_kb, total_kb, committed, limit = (int(x) for x in info.split("|"))
        out["mem_free_gb"] = round(free_kb / 1024 ** 2, 2)
        out["mem_total_gb"] = round(total_kb / 1024 ** 2, 1)
        out["commit_used_gb"] = round(committed / 1024 ** 3, 2)
        out["commit_limit_gb"] = round(limit / 1024 ** 3, 2)
        out["commit_pct"] = round(100 * committed / limit) if limit else 0
    except Exception:  # noqa: BLE001 拿不到就少记几个字段，不该影响值守
        out["mem_error"] = "无法读取内存快照"

    # ── ★ 2026-09-27 修复：`job_membership()` 曾是**死代码** ──
    #
    # 实测（2026-09-27 后端静默终止取证）：`job_membership` 全文件只出现
    # 2 次 —— 定义处 + 文档提及，**零调用**。于是 `backend_incidents.jsonl`
    # 里 20 条事故记录**没有一条**带 job 字段，而它的 docstring 明确写着
    # "每次事故现场都记一次"。这是最典型的静默失效：字段看着有实现，
    # 实际上从来没被采集过。
    #
    # `KILL_ON_JOB_CLOSE` 的 Job 一关闭，成员进程被**内核**终止且不留日志 ——
    # 与实测的静默终止表现完全一致，是本项目最需要排除的一条杀因。
    # 记的是 `os.getpid()`（**值守进程自己**）而不是那个已经死掉的 pid：
    # 死进程的 job 状态事后取不到，而"值守自己是否被 Job 兜着"同样重要 ——
    # 若值守本身跑在一个 kill-on-close 的 Job 里，连它拉起的新实例也会一起陪葬。
    try:
        out["in_job"] = job_membership(os.getpid())
    except Exception:  # noqa: BLE001 取不到就少记一个字段
        out["in_job"] = "unknown"
    return out


def job_membership(pid: int) -> str:
    """`pid` 是否在某个 Job Object 里 → `"none"` / `"in_job"` / `"unknown"`。

    ## 为什么值守要记这一项

    `KILL_ON_JOB_CLOSE` 的 Job 一关闭，成员进程会被**内核**终止且
    **不留任何日志** —— 与实测的静默终止表现一致。但"在不在 Job 里"
    是**运行期状态**，事后无法回溯。所以每次事故现场都记一次：
    下次再发生，就能直接排除或确认这条杀因。
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "job_status.py"),
             "--pid", str(pid)],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT),
            # ⚠️ 必须显式 utf-8：子进程的输出是 UTF-8（它自己 reconfigure 过），
            #    而中文 Windows 上 `text=True` 默认按 **GBK** 解码 →
            #    `UnicodeDecodeError` 直接抛出来。没写这一行时
            #    `job_membership` 永远返回 "unknown"，而那是**静默失效**：
            #    字段看着有值，其实每次都拿不到。
            encoding="utf-8", errors="replace",
            creationflags=_NO_CONSOLE)
        if proc.returncode != 0:
            return "unknown"
        return "none" if "[OK]" in proc.stdout else "in_job"
    except Exception:  # noqa: BLE001
        return "unknown"


def _service_state(name: str) -> str | None:
    """Windows 服务的状态（`Running` / `Stopped` / …）；服务不存在返回 `None`。

    ## 为什么用服务而不是 PID

    `cloudflared` 在本机是**以服务方式运行**的（`Cloudflared`，
    `SERVICE_START_NAME=LocalSystem`）—— 也就是说它的生命周期由
    **SCM** 管，我们不该自己去 `taskkill` + `Start-Process`。
    那样会和 SCM 的自动恢复（`RESTART -- Delay = 20000ms`）打架，
    造出**两个实例**（实测事故，见 `cmd_tunnel_ensure` 的注释）。

    问 SCM"服务在不在跑"比自己枚举进程可靠：进程名可能相同但归属不同，
    而服务状态是唯一权威。
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Service -Name '{name}' -ErrorAction SilentlyContinue).Status"],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace", creationflags=_NO_CONSOLE)
        state = proc.stdout.strip()
        return state or None
    except Exception:  # noqa: BLE001 查不到就当服务不存在（调用方会如实报错）
        return None


def cmd_crowding_refresh(args: argparse.Namespace) -> int:
    """**拥挤度自动定时刷新**（计划任务用）：同步跑完一轮，进度落日志。

    ## 为什么不能复用 API 的 `start_refresh_all()`

    那个是"起后台线程 + 立即返回 task_id"（给前端轮询用的），
    **线程随进程退出而死** —— 计划任务进程调它等于什么都没刷。
    所以这里直接调**同步核心** `refresh.refresh_all_incremental()`。
    线程注册表与进度查询留在 API 侧不动（那是另一条路径的职责）。

    ## 为什么必须**在目标环境里**求值（`CHG-0112` 的老形状）

    `manage.py` 是计划任务/运维 shell 起的：**父进程里没有 `MOSS_ENV`、
    也没有 `MOSS_SQLITE_PATH`** ⇒ `data_stores.current_env()` 退化成 `dev`、
    `is_main_instance()` 判成 True。实测这个坑在本项目出现过**至少三次**
    （启动横幅的作业数、worker 停止文件路径、`_scheduler_scope_line`）。
    所以整段逻辑包在 `_temporary_environ(extra_env)` **之内**，
    写者判定与实际写库用的是**同一份环境**。

    ## 退出码（计划任务 `LastTaskResult` 唯一能表达的东西）

        0 = 刷新成功（或本环境不是写者 ⇒ 无事可做，见下）
        1 = 刷新抛异常
        2 = 本环境**不是写者**，拒绝执行
        3 = 未预期的异常

    ⚠️ **"不是写者"返回 2 而不是 0**：与 `cmd_ensure` 的"端口被别的程序
    占用 → 2"同一哲学 —— 那是一件**需要人来看的事**（计划任务配错了环境）。
    若返回 0，它会和"刷成功"长得一模一样，而这正是本项目反复记录的
    "静默失效"形状。
    """
    env_name = (args.env or "").strip().lower() or "dev"
    # ★ 复用 `_prepare_environment`，不自己拼隔离变量：
    #   它同时做"归一化环境名 + 生成隔离路径 + 跑启动自检"，
    #   自己再拼一份必然漂移（本项目已有多次"两处各拼一遍"的记录）。
    extra_env, rc = _prepare_environment(env_name)
    if rc != 0:
        return rc        # 自检未通过 → 原样透出退出码（拒绝执行）

    with _temporary_environ(extra_env):
        from src.infrastructure.catalog.data_stores import writable_here

        decision = writable_here("crowding_shared")
        if not decision.allowed:
            print(f"❌ 本环境({env_name})不是拥挤度参考数据的写者，拒绝刷新。\n"
                  f"   原因：{decision.reason}\n"
                  f"   要改归属就改 configs/data_stores.yaml 的 "
                  f"crowding_shared.writer 字段。", file=sys.stderr)
            return 2

        from src.sector_crowding import refresh

        try:
            task = refresh.refresh_all_incremental(
                task_id=f"scheduled-{datetime.now():%Y%m%d-%H%M%S}",
                concepts_only=bool(args.concepts_only),
                max_sectors=int(args.max_sectors or 0),
                pool_only=not bool(args.full),
            )
        except Exception as exc:  # noqa: BLE001 计划任务要一个退出码，不要栈
            logging.getLogger("manage").exception("拥挤度定时刷新失败")
            print(f"❌ 拥挤度刷新失败：{type(exc).__name__}: "
                  f"{str(exc)[:200]}", file=sys.stderr)
            return 1

    status = str(getattr(task, "status", "") or "")
    done = int(getattr(task, "processed", 0) or 0)
    total = int(getattr(task, "total", 0) or 0)
    failed = list(getattr(task, "failed_sectors", []) or [])
    seconds = float(getattr(task, "seconds", 0.0) or 0.0)
    error = str(getattr(task, "error", "") or "")

    # 成功路径**才打印** —— 若这个任务将来改成高频，输出会把事故淹没。
    # 但"每周两次"的量级下，留一行是有价值的运行台账。
    print(f"✅ 拥挤度刷新完成（env={env_name}）：{done}/{total} 个板块，"
          f"失败 {len(failed)} 个，耗时 {seconds:.1f}s"
          + (f"｜{error}" if error else ""))
    return 0 if status != "failed" else 1


#: "正在启动"的宽限窗口（秒）。`CHG-0145`。
#:
#: ## 它修的是什么（2026-09-30 实测事故）
#:
#: `cmd_ensure` 的判据是"**端口有没有人听**"，而启动期端口本来就是空的
#: （uvicorn 要到 lifespan 跑完才 bind）⇒ 值守每分钟一跳，会在**上一个实例
#: 正在启动**时再拉一个 ⇒ 两份实例互踩（SQLite `database is locked`、
#: 各自重复跑启动预热）⇒ 谁也起不来。实测现场：
#:
#:   · 16:34–16:41 每次拉起后 ~70 s 就"端口无监听"，公网 **502**；
#:   · 同一时刻抓到 **4 个 `--port 8110` 进程 = 2 个并发实例**；
#:   · 停值守 → 清残留 → **单实例启动 2 秒就绑定**（不是"启动要 4~5 分钟"）。
#:
#: 取 600 s：老实例的启动实测最坏 2~3 分钟（catalog 注册 + 指标索引重建 +
#: 各预热），600 s 留了一倍余量；而"真死了"的场景（进程整棵树消失）不受影响
#: —— 那时 `our_backend_processes()` 是空的，宽限不生效。
STARTUP_GRACE_SEC = 600.0


def our_backend_processes() -> list[tuple[int, float]]:
    """本项目的后端进程 → `[(pid, 已运行秒数)]`。

    只用来回答一个问题："**有没有一个我们自己的后端正在启动？**"
    匹配纪律复用 `is_our_cmdline`（与 `list_our_backend_pids` / `stop`
    同一套判据，不另写一份）；拿不到启动时间时**返回空**（宁可让值守照常
    重启，也不要因为探测失败把服务永久留在"没人管"的状态）。
    """
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
        "ForEach-Object { \"$($_.ProcessId)\t"
        "$([int]((Get-Date) - $_.CreationDate).TotalSeconds)\t"
        "$($_.CommandLine)\" }")
    out = _run_text(["powershell", "-NoProfile", "-NonInteractive",
                     "-Command", script], timeout=25)
    found: list[tuple[int, float]] = []
    for line in out.splitlines():
        pid_text, _, rest = line.partition("\t")
        age_text, _, cmdline = rest.partition("\t")
        if not pid_text.strip().isdigit():
            continue
        try:
            age = float(age_text.strip())
        except ValueError:
            continue
        if is_our_cmdline(cmdline):
            found.append((int(pid_text.strip()), age))
    return found


def starting_instance(instances: list[tuple[int, float]], *,
                      grace_sec: float = STARTUP_GRACE_SEC,
                      ) -> tuple[int, float] | None:
    """`[(pid, 已运行秒数)]` → **正在启动**的那个（最年轻的、仍在宽限内）。

    纯函数（不碰进程表），所以"端口空但实例在启动"这条判据可以被单测钉住 ——
    这正是 2026-09-30 那次事故缺的那一层。
    """
    fresh = [(pid, age) for pid, age in instances if 0 <= age < grace_sec]
    if not fresh:
        return None
    return min(fresh, key=lambda item: item[1])


def replace_refusal_reason(other_pids: list[int]) -> str:
    """`--replace` 此刻是否**必须拒绝**（`""` = 允许）。`CHG-0145`。

    ## 判据为什么是"进程表里还有别的实例"，而不是"环境名是不是 pilot"

    `--replace` 走 `stop_backend_processes(port)`，而那个函数**按命令行枚举
    本项目全部后端进程**（为了清端口外的孤儿，见它的 docstring）⇒
    只要**除了本端口那个实例之外还有别的实例在跑**，用 `--replace` 就会把它们
    一起停掉 —— 那正是危险条件本身，与环境名无关（写环境名判据等于给
    未来新增的环境留同一个坑，而且 `pilot` 这个名字会漂移）。

    原先只靠启动横幅印一行"⚠️ 不要用 --replace"：那是**提醒**不是机制，
    而本项目已多次证明提醒挡不住（`MOSS_SCHEDULER_ENABLED` 只被注释提到、
    全仓库零处读取，就是先例）。

    要重启 pilot 的正确路径（错误消息里照抄这条 —— **拒绝必须给出路**）：
    ① `Disable-ScheduledTask MossPilotWatchdog`；② 只停目标端口的监听进程；
    ③ `python manage.py start --env pilot --port 8110 --daemon`；
    ④ `Enable-ScheduledTask MossPilotWatchdog`。
    """
    if not other_pids:
        return ""
    return ("--replace 被**拒绝**：本端口之外还有 "
            f"{len(other_pids)} 个本项目后端实例在跑（PID={other_pids[:6]}）"
            "—— `--replace` 会把它们一起停掉（它按命令行枚举**全部**后端进程）。\n"
            "   正确步骤：① 只停目标端口的监听进程；② 需要 pilot 时先 "
            "Disable-ScheduledTask MossPilotWatchdog；\n"
            "   ③ python manage.py start --env pilot --port 8110 --daemon；"
            "④ 起来后再 Enable-ScheduledTask MossPilotWatchdog")


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
        # ★ `CHG-0139`：后端健康**不等于**"该跑的都在跑" —— 重作业已经移出
        #   本进程，所以这一轮值守还要问一句"worker 在不在"。两件事同一入口、
        #   同一份事件流水（`scripts/pilot_watchdog.ps1` 是全项目唯一的值守入口）。
        worker_code = ensure_worker(env, verbose=args.verbose)
        if args.verbose:
            print(f"✅ {port} 上本项目实例运行中（PID={info.get('pid')}），无需处理。")
        return worker_code

    if info.get("occupied"):
        # 非本项目占用：拒绝，并留下现场
        _record_incident(
            "foreign_port_owner", port=port,
            pid=info.get("pid"), cmdline=str(info.get("cmdline") or "")[:200])
        print(f"❌ 端口 {port} 被**其他程序**占用（PID={info.get('pid')}），"
              f"值守拒绝启动。命令行：{info.get('cmdline')}", file=sys.stderr)
        return 2

    # ★ `CHG-0145`：**"端口空"不等于"进程已消失"** —— 启动期端口本来就是空的。
    #   不加这一层，值守会在上一个实例正在启动时再拉一个（2026-09-30 实测：
    #   2 个并发实例互踩、每次拉起 ~70 s 就死、公网 502 持续 7 分钟）。
    starting = starting_instance(our_backend_processes())
    if starting is not None:
        pid_s, age = starting
        print(f"⏳ {port} 端口暂时没有监听，但**有一个本项目实例正在启动**"
              f"（PID={pid_s}，已运行 {age:.0f}s / 宽限 {STARTUP_GRACE_SEC:.0f}s）"
              f"—— 值守本轮不重启，避免拉出第二个实例抢库。", file=sys.stderr)
        _record_incident("startup_grace_skip", port=port, env=env,
                         pid=pid_s, age_sec=round(age, 1))
        return 0

    # 端口空 → 起回来。先把"最后一次活动"记下来，这是判断死亡时刻的唯一线索。
    last_activity = ""
    log_path = RUN_DIR / f"{args.name}.log"
    if log_path.exists():
        last_activity = datetime.fromtimestamp(
            log_path.stat().st_mtime).isoformat(timespec="seconds")
    _record_incident("restart", port=port, env=env,
                     last_activity=last_activity,
                     reason="端口无监听（进程已消失）",
                     # ★ 事故现场：内存/提交量 + 是否在 Job 里。
                     #   这两项都是**运行期状态、事后无法回溯**的，
                     #   不当时记下来，下次又只能靠推测。
                     **memory_snapshot())

    code = _ensure_restart(env=env, port=port, name=args.name)
    if code == 0:
        _record_incident("restart_ok", port=port)
        print(f"✅ 值守已重启 {env} 实例（{port}）。上次活动：{last_activity or '未知'}")
        # 重启后端的同时把 worker 也确认一遍：两者是**一对**（API 设 role=api
        # ⇒ 重作业只由 worker 跑）。只恢复一半比两个都不起更隐蔽 ——
        # 前端一切正常，而那几个作业不再更新。
        worker_code = ensure_worker(env)
        if worker_code != 0 and code == 0:
            return worker_code
    else:
        _record_incident("restart_failed", port=port, exit_code=code)
        print(f"❌ 值守重启失败（退出码 {code}），详见 {INCIDENT_LOG}", file=sys.stderr)
    return code


#: 隧道值守的事件流水（与 `INCIDENT_LOG` 分开：一个管后端，一个管隧道）。
TUNNEL_INCIDENT_LOG = RUN_DIR / "tunnel_incidents.jsonl"

#: 隧道健康探测：连续几次失败才判定"需要重启"。
#:
#: ⚠️ **不能一次失败就重启**：实测单次失败率约 10~15%，而重启隧道本身会
#: 断连几秒 —— 一次抖动就重启等于自己制造故障。
#:
#: ## 为什么是 4 次 × 5 秒（2026-09-26 实测调整）
#:
#: 第一版是 3 次 × 2 秒（总窗口 12 秒），**实测太短**：CF 这条路径
#: 会有 15~30 秒的瞬时超时（同一时段实测到一次 8.3 秒、一次 15.9 秒），
#: 于是"一次抖动"就被判成故障，触发了一次不必要的重启。
#:
#: 现在总窗口约 **60 秒**：足以跨过一次瞬时抖动，又能在真故障时
#: 一分钟内恢复（对一个"客户打不开"的场景是可以接受的响应时间）。
TUNNEL_PROBE_ATTEMPTS = 4
TUNNEL_PROBE_GAP_SECONDS = 5.0
TUNNEL_PROBE_TIMEOUT = 12.0


def cmd_tunnel_ensure(args: argparse.Namespace) -> int:
    """**隧道值守**：公网入口连续探测失败就重启 cloudflared。

    ## 为什么要它（2026-09-26 实测）

    后端本机稳定在 **1.9~3.4 毫秒、5/5 成功**，而**同一时刻走公网**是
    **1.14~11.5 秒、9/10 成功**（1 次直接挂死）。用户看到的是"后台挂了"，
    实际挂在隧道上。

    更关键的是实测到**重启隧道能恢复**：

        | | 重启前 | 重启后 |
        |---|---|---|
        | 成功率 | 9/10（1 次挂死） | **10/10** |
        | 中位延迟 | 3.34 s | **1.60 s** |

    也就是说这条链路会**随时间劣化**（前一次是连续跑了 35 小时后劣化，
    这一次是 23 分钟后重新测到同样的形态）。把它做成自动的，
    用户就不必等"下次不知道什么时候来的一波抖动"。

    ## 判据：连续 N 次失败（不是一次就重启）

    见 `TUNNEL_PROBE_ATTEMPTS` 的说明。**宁可不重启，也不要误重启** ——
    重启隧道本身会中断所有在途请求。

    ## 它不做的事

    · **不改隧道配置**：换线路（国内中转 frp）需要决策与主机，不在自动化范围。
    · **不重启后端**：那是 `manage.py ensure` 的职责，两者互不干扰。
    """
    url = args.url or "https://moss.wujiaitool.cn/api/v1/health/live"

    # ⚠️ 判据：**只有"连不上/超时/5xx"才算失败，4xx 一律算成功**。
    #
    # 踩过的坑（2026-09-26 实测）：第一版把任何非 200 都当失败，于是在
    # "隧道完全正常、但探测这个路径被登录门槛拦下"时误判为故障，
    # **白白重启了一次隧道**（重启本身会中断所有在途请求）。
    #
    # 而 4xx 恰恰是**链路通的证据**：能拿到业务错误码，说明请求走完了
    # cloudflared → 后端 → 再回来整条路。401/403/404 都只说明
    # "这个路径要登录/不存在"，与隧道健康无关。
    #
    # 所以探测地址最好选**公开路径**（如 `/api/v1/health/live`，它连登录
    # 门槛都在白名单里）；但即使选错了，下面的判据也不会误重启。
    # 记录每次探测的**真实延迟/错误**：事后追查"为什么重启了"时，
    # "4 次都是 TimeoutError"与"4 次都很快但 500"是完全不同的两件事。
    probe_log: list[str] = []
    last = ""
    for attempt in range(1, TUNNEL_PROBE_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url), timeout=TUNNEL_PROBE_TIMEOUT) as r:
                ms = int((time.monotonic() - t0) * 1000)
                probe_log.append(f"{attempt}:{r.status}/{ms}ms")
                if args.verbose:
                    print(f"[tunnel] 第 {attempt} 次探测成功（HTTP {r.status}，{ms}ms）。")
                return 0
        except urllib.error.HTTPError as exc:
            ms = int((time.monotonic() - t0) * 1000)
            # 4xx = 链路通（只是这个路径要登录/不存在）→ 成功
            if 400 <= exc.code < 500:
                probe_log.append(f"{attempt}:{exc.code}(4xx=通)/{ms}ms")
                if args.verbose:
                    print(f"[tunnel] 第 {attempt} 次探测成功"
                          f"（HTTP {exc.code} 属 4xx：链路通，仅该路径需登录）。")
                return 0
            last = f"HTTP {exc.code}"
            probe_log.append(f"{attempt}:{exc.code}/{ms}ms")
            if args.verbose:
                print(f"[tunnel] 第 {attempt} 次失败：{last}")
        except Exception as exc:  # noqa: BLE001 连接层异常才算真失败
            ms = int((time.monotonic() - t0) * 1000)
            last = type(exc).__name__
            probe_log.append(f"{attempt}:{last}/{ms}ms")
            if args.verbose:
                print(f"[tunnel] 第 {attempt} 次失败：{last}（{ms}ms）")
        if attempt < TUNNEL_PROBE_ATTEMPTS:
            time.sleep(TUNNEL_PROBE_GAP_SECONDS)

    # 连续失败 → 重启 cloudflared
    #
    # ★★ 必须走 **Windows 服务**，不能自己起进程（2026-09-26 实测事故）
    #
    # 这台机器上 cloudflared 是**以服务方式运行**的：
    #
    #     Cloudflared 服务  state=Running  StartMode=Auto
    #     SERVICE_START_NAME = LocalSystem
    #     FAILURE_ACTIONS    = RESTART -- Delay = 20000 milliseconds
    #
    # 也就是说 **SCM 自己会在进程挂掉 20 秒后把它拉起来**。
    # 而原来这里做的是"taskkill 全部 cloudflared + 自己 Start-Process 一个"，
    # 于是形成死循环：
    #
    #     我杀掉服务进程 → SCM 20 秒后拉起一个（PID A）
    #                    → 我又起一个（PID B）＝ **两个实例**
    #
    # 两个实例各自向 CF 注册连接器（实测 `tunnel info` 里出现两个
    # connector，创建时间相差 16 秒），互相抢同一个隧道 —— 不但没修好，
    # 还让"重启"本身成了不稳定源。
    #
    # 正确做法：**用 `sc.exe` / `Restart-Service` 让 SCM 去管**。
    # 这样永远只有一个实例，且不依赖我们自己去拼命令行。
    service = "Cloudflared"
    svc_state = _service_state(service)
    _record_tunnel_incident(
        "restart", url=url, attempts=TUNNEL_PROBE_ATTEMPTS,
        probe_log=probe_log, last_error=last,
        service=service, service_state=svc_state,
        reason="公网入口连续探测失败",
        **memory_snapshot())

    if svc_state is None:
        # 服务不存在 → 说明这套部署不是服务形态，退回"自己起一个"，
        # 但**先确保没有残留实例**（否则又会造出两个）
        _record_tunnel_incident("restart_failed", reason="找不到 Cloudflared 服务")
        print("❌ 找不到 Cloudflared 服务；本机可能不是服务方式部署。"
              "请确认 `sc query Cloudflared`，或改用 cloudflared 官方服务安装。",
              file=sys.stderr)
        return 1

    if svc_state.lower() == "running":
        # 服务在跑但公网不通 → 重启服务（SCM 会先停干净再起，不会留重复）
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Restart-Service -Name {service} -Force"],
                       capture_output=True, creationflags=_NO_CONSOLE)
    else:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Start-Service -Name {service}"],
                       capture_output=True, creationflags=_NO_CONSOLE)
    time.sleep(30)   # 等它建连（SCM 恢复策略本身还带 20 秒延迟）

    # 重启后再探一次，如实记账（成功与否都要记 —— "重启了但没用"是最需要知道的情况）
    healthy = False
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url), timeout=TUNNEL_PROBE_TIMEOUT) as r:
            healthy = r.status == 200
    except urllib.error.HTTPError as exc:
        healthy = 400 <= exc.code < 500      # 4xx 同样证明链路已通
    except Exception:  # noqa: BLE001
        healthy = False
    _record_tunnel_incident("restart_ok" if healthy else "restart_ineffective")
    print(("✅ 隧道已重启并恢复（%s）" if healthy else "⚠️ 隧道已重启但仍不通（%s）")
          % url)
    return 0 if healthy else 1


def _record_tunnel_incident(event: str, **fields: object) -> None:
    """追加一条隧道值守事件（失败只警告）。"""
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now().isoformat(timespec="seconds"),
               "event": event, **fields}
        with TUNNEL_INCIDENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 隧道值守事件写入失败：{exc}", file=sys.stderr)


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


def _worker_stop_files(env_name: str | None = None) -> list[Path]:
    """worker 停止文件的路径 —— **必须在目标环境里求值**（`CHG-0112` 的老形状）。

    ## 为什么不能直接 `stop_file_path()`

    它按 `SCHEDULER_DIR` 环境变量解析，而 `manage.py` 是运维的 shell 起的：
    **父进程里没有 `SCHEDULER_DIR`、也没有 `MOSS_ENV`** ⇒ 解析出来是
    `data/scheduler/worker.stop`（默认档），而 pilot 的 worker 看的是
    `data/pilot/scheduler/worker.stop`。实测后果：2026-09-30 我第一次用
    `restart-pilot` 时"发了停止文件"却**没有任何 worker 优雅退出** ——
    信号发到了另一个目录，两边都**静默**。

    这与 `_scheduler_scope_line()`（`CHG-0112`）与 `manage.py status` 的 worker 行
    是**同一个坑的第三次出现**：**判据/路径必须在"即将生效"的那份环境里求值。**

    `env_name` 给定时只返回那一个环境的路径；不给（`cmd_stop` 要覆盖全部实例）
    返回**所有已知环境 + 默认档**的并集，去重保序。
    """
    from src.scheduler.worker import stop_file_path

    makers = {"pilot": pilot_isolation_env, "dev": dev_isolation_env}
    if env_name is not None:
        maker = makers.get(env_name)
        with _temporary_environ(maker() if maker else {}):
            return [stop_file_path()]
    out: list[Path] = []
    for maker in makers.values():
        with _temporary_environ(maker()):
            out.append(stop_file_path())
    out.append(stop_file_path())          # 父进程默认档（可能是 data/scheduler）
    return list(dict.fromkeys(out))


def cmd_restart_pilot(args: argparse.Namespace) -> int:
    """**精确重启 pilot**（8110）与它的调度 worker —— 不碰 dev(8100)。

    ## 为什么需要它（而不是用 `stop` + `start`）

    `stop` / `--replace` 按**命令行**枚举本项目**全部**后端进程（含 8100 上的 dev
    实例）—— 跨端口并存时用它们会把 dev 一起停掉（见 `PILOT_BACKEND_PORT` 的注释）。
    本命令只动 **pilot 那一棵树 + worker**。

    ## ★ 目标怎么取（第一版取错了，这里记下来）

    第一版只读 PID 文件，实测两个坑：

    1. `data/run/backend.pid` 里的进程**早就没了**（启动器外壳退出后 PID 文件不会更新）
       ⇒ 报"无有效进程"，而 pilot 明明在跑（8110 监听者 / 它的父进程）；
    2. PID 文件里的 worker 是 **uv 的 `python.exe` 外壳**，真解释器是它的**子进程**
       （`.venv\\Scripts\\python.exe` → `uv\\python\\…\\python.exe`）。给外壳发
       CTRL_BREAK **到不了**子进程 ⇒ 第一版把 worker **硬杀**了（日志里没有任何
       关停痕迹），而硬杀不 checkpoint —— 正是 `src/core/sqlite_recovery.py`
       开头那次 `disk I/O error` 的成因。

    所以答案是**并集**：PID 文件 ∪ 端口监听者 ∪ 监听者的父进程 ∪ 命令行枚举到的
    worker；每个都发一次优雅停止请求（CTRL_BREAK + worker 额外写停止文件），
    等不到才树杀。

    ## 顺序（每一步都必须先做完再做下一步）

    停（并等端口释放）→ 起（`cmd_start`）→ 校验（端口/存活探针/worker）。
    起的那一步会**连带**拉起 worker（见 `ensure_worker`），
    所以"重启后端却忘了 worker"这个静默失效在这里不可能发生。
    """
    port = args.port or PILOT_BACKEND_PORT
    env = args.env or "pilot"
    t_phase = time.time()

    def _phase(label: str) -> None:
        """打印上一段的墙钟 —— 重启窗口是**用户可感知的停机时间**，
        没有分相位计时就只能猜"这 36 秒花在哪"（实测踩过：改错了地方）。"""
        nonlocal t_phase
        now = time.time()
        print(f"  ⏱ {label}：{now - t_phase:.1f} s")
        t_phase = now

    targets: dict[int, str] = {}
    for label, pid_file in (("pilot后端(PID文件)", RUN_DIR / "backend.pid"),
                            ("worker(PID文件)", RUN_DIR / "scheduler-worker.pid")):
        pid = read_pid_file(pid_file)
        if pid is not None:
            targets[pid] = label
    listener = find_listening_pid(port)
    if listener is not None:
        targets[listener] = f"pilot后端({port} 监听者)"
        parent = _parent_of(listener)
        if parent is not None and parent in list_our_backend_pids():
            targets[parent] = "pilot后端(监听者的父进程/启动器)"
    for pid in list_our_worker_pids():
        targets.setdefault(pid, "worker(命令行枚举)")

    if not targets:
        print(f"（没有发现 {port} 上的 pilot 后端或 worker，直接启动）")
    else:
        # ★ 优雅请求的**返回值必须用起来**（2026-09-30 实测白等 20 秒）：
        #   `request_graceful_stop` 走 CTRL_BREAK，而它要求调用方与目标**共享控制台**
        #   —— 本项目所有守护进程都是 `CREATE_NO_WINDOW` 起的 ⇒ 实测**恒返回 False**
        #   ⇒ "请求优雅退出"其实什么都没发生，而后面那个"等端口空"的循环
        #   因此**白等满 `--timeout`（20 秒）**，用户多挨 20 秒停机。
        #   现在：只要优雅请求**没人接**，就只给 1 秒缓冲直接树杀。
        accepted = 0
        for pid, why in sorted(targets.items()):
            print(f"  停止中：{why} PID={pid}")
            if request_graceful_stop(pid):
                accepted += 1
        # worker 额外走停止文件（CTRL_BREAK 到不了无控制台的守护进程）。
        # ★ 路径必须在**目标环境**里求值，否则会发到另一个目录、两边都静默
        #   （见 `_worker_stop_files` 的实测记录）。
        worker_pids = list_our_worker_pids()
        stop_file = None
        if worker_pids:
            try:
                stop_file = _worker_stop_files(env)[0]
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.write_text(
                    f"restart_by=manage.py pid={os.getpid()}\n", encoding="utf-8")
                print(f"  已写停止文件 {stop_file}")
            except Exception as exc:  # noqa: BLE001 拿不到通道不挡流程
                print(f"  ⚠️ 停止文件写入失败（{type(exc).__name__}）", file=sys.stderr)
                stop_file = None
        # ★ 等待判据用「**端口空了 + worker 走了**」，不是「所有目标进程都死」——
        #   2026-09-30 实测：`manage.py` 用 uv 起进程时会留一个**外壳进程**
        #   （`.venv\Scripts\python.exe` 是 shim，真解释器是它的子进程），
        #   外壳可能比真进程多活很久/一直不退出 ⇒ 按"全部死亡"等，会**白等满
        #   `--timeout`（默认 20 秒）**，而这段时间里用户看到的是整站不可用。
        #   端口空了就说明监听者已退；worker 单独等（它没有端口）。
        worker_targets = [p for p, why in targets.items() if "worker" in why]
        # 优雅请求没人接 ⇒ 不必等（见上面那段说明）；有人接才按 `--timeout` 等
        wait_budget = args.timeout if accepted else 1.0
        if not accepted:
            print("  （优雅通道无人接收：CTRL_BREAK 到不了无控制台的守护进程 "
                  "⇒ 直接走树杀，省掉白等的 20 秒）")
        deadline = time.time() + wait_budget
        while time.time() < deadline:
            port_free = not port_open("127.0.0.1", port)
            workers_gone = not any(_pid_alive(p) for p in worker_targets)
            if port_free and workers_gone:
                break
            time.sleep(0.3)
        # 仍未退的才树杀（含那个可能赖着不走的外壳）
        for pid in sorted(targets):
            if _pid_alive(pid):  # noqa: SLF001
                kill_pid_tree(pid)          # 结果不打印：外壳残留不是故障，别刷屏
        if stop_file is not None:
            try:
                stop_file.unlink(missing_ok=True)
            except OSError:
                pass
        _phase("停止段（信号 + 等端口空/worker 退 + 兜底树杀）")
        for _ in range(20):
            if not port_open("127.0.0.1", port):
                break
            time.sleep(0.5)
        _phase("等端口彻底释放")

    start_args = argparse.Namespace(
        env=env, host=args.host or "127.0.0.1", port=port, reload=False,
        daemon=True, with_frontend=False, replace=False, auto_port=False)
    code = cmd_start(start_args)
    _phase("启动命令（含等端口就绪）")
    if code != 0:
        return code

    # 校验：端口 + 存活探针 + worker（三件都查，缺一件就等于没验）
    #
    # ⚠️ **必须带重试**（2026-09-30 实测踩到）：这个应用的启动段不只是 uvicorn
    #    起来就完事 —— 它还要重建指标索引（实测 1216 条元数据 / 回填 2173 个指标）、
    #    对 1038 个自动登记指标做频率推断、扫资产家底，**合计 25 秒以上**。
    #    第一版只探一次，于是刚启动就报「端口=❌ 存活探针=❌」——
    #    一条**假故障**，而且出现在最需要可信的位置（重启后的校验）。
    deadline = time.time() + getattr(args, "verify_timeout", 90.0)
    ok_port = live = False
    while True:
        ok_port = port_open("127.0.0.1", port)
        if ok_port:
            try:
                with urllib.request.urlopen(  # noqa: S310 本机回环
                        f"http://127.0.0.1:{port}/api/v1/health/live",
                        timeout=5) as resp:
                    live = resp.status == 200
            except (urllib.error.URLError, OSError):
                live = False
        if (ok_port and live) or time.time() >= deadline:
            break
        time.sleep(1.0)
    # ⚠️ worker 只在**该环境需要它**时才要求（`role=api` 的环境才需要）。
    #    dev 是"全跑"（不设角色）⇒ 它本来就不该有 worker，无条件检查会印
    #    「worker=❌」并让命令返回 1 —— 一条**假警报**（实测踩到）。
    if env_needs_worker(env):
        present, why = worker_present()
        worker_txt = ("✅ " + why) if present else "❌ 未在运行"
    else:
        present, worker_txt = True, "（本环境不需要：未设角色 ⇒ 全表在本进程跑）"
    print(f"  校验：端口={'✅' if ok_port else '❌'} "
          f"存活探针={'✅' if live else '❌'} worker={worker_txt}")
    return 0 if (ok_port and live and present) else 1


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
    worker_pids = list_our_worker_pids()
    listener = find_listening_pid(DEFAULT_BACKEND_PORT)
    print("\n" + "=" * 72)
    print(f"后端进程：端口监听者 PID={listener}，命令行匹配到的进程 {others}")
    if len(others) > 1 and not is_single_instance(others):
        print("⚠️ 存在多个**互相独立**的后端实例（含端口外孤儿）。孤儿会占着 "
              "SQLite 的 -wal/-shm，请执行 `python manage.py stop` 清理。")
    elif len(others) > 1:
        print("（以上是同一个实例的 reload 父子进程，非孤儿）")
    # ★ `CHG-0139`：worker 单独一行 —— 它没有端口，混进上面那句会让人读成
    #   "又一个孤儿后端实例"。同时把心跳（它是**独立线程**写的，所以"旧"就是
    #   真死了）与"到底要不要它"一起印出来，避免"没起也没人发现"。
    print(f"调度 worker：进程 {worker_pids or '未发现'}")
    try:
        from src.scheduler.registry import worker_requirement

        req = worker_requirement()
        if req["needed"]:
            flag = "✅ 在跑" if req["alive"] else "❌ 未在运行"
            print(f"             {flag}｜{req['verdict']}")
            if not req["alive"]:
                print(f"             重作业（{len(req['heavy_jobs'])} 个）当前"
                      f"**无人执行**；用 `python manage.py start-worker --env "
                      f"{os.environ.get('MOSS_ENV') or 'pilot'} --daemon` 启动")
        else:
            print(f"             本进程不需要它（{req['verdict']}）")
    except Exception as exc:  # noqa: BLE001 状态查询不该因为心跳读不到就崩
        print(f"             心跳状态**未量到**（{type(exc).__name__}: {exc}）")
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

    # 调度 worker（`CHG-0139`）：只跑 4 个重作业，与 API 进程分开。
    p_worker = sub.add_parser(
        "start-worker",
        help="启动调度 worker 进程（只跑重作业；与 API 进程成对使用）")
    p_worker.add_argument("--env", default="pilot", choices=("pilot", "dev"),
                          help="与 API 进程用同一个（默认 pilot）")
    p_worker.add_argument("--daemon", action="store_true", help="后台运行")
    p_worker.set_defaults(func=cmd_start_worker)

    p_stop = sub.add_parser("stop", help="停止本项目后台实例（不碰其他程序）")
    p_stop.set_defaults(func=cmd_stop)

    p_restart = sub.add_parser(
        "restart-pilot",
        help="精确重启对外试点（8110）+ 它的调度 worker；**不碰 dev(8100)**")
    p_restart.add_argument("--env", choices=("pilot", "dev"), default="pilot",
                           help="目标环境（默认 pilot）")
    p_restart.add_argument("--port", type=int, default=PILOT_BACKEND_PORT,
                           help=f"目标端口（默认 {PILOT_BACKEND_PORT}）")
    p_restart.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_restart.add_argument("--timeout", type=float, default=20.0,
                           help="等优雅退出的秒数（超时才树杀）")
    p_restart.add_argument("--verify-timeout", type=float, default=90.0,
                           help="重启后等就绪的秒数（实测启动段 25 秒以上）")
    p_restart.set_defaults(func=cmd_restart_pilot)

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

    p_cr = sub.add_parser(
        "crowding-refresh",
        help="拥挤度一键刷新（**同步**跑完一轮；供计划任务每周两次调用）")
    p_cr.add_argument(
        "--env", choices=list(_env_choices()), default="dev",
        help="用哪个环境跑（默认 dev —— 它是 crowding_shared 声明的写者；"
             "非写者会拒绝执行并返回退出码 2）")
    p_cr.add_argument(
        "--full", action="store_true",
        help="全量（2517 个板块含行业/地区）。默认只刷**关注板块池**，"
             "与前端「一键刷新」按钮同口径")
    p_cr.add_argument(
        "--concepts-only", action="store_true",
        help="只刷概念板块（在全量/池子之上再过滤一层）")
    p_cr.add_argument(
        "--max-sectors", type=int, default=0,
        help="最多刷几个板块（0=不限；用于演练）")
    p_cr.set_defaults(func=cmd_crowding_refresh)

    p_tunnel = sub.add_parser(
        "tunnel-ensure",
        help="隧道值守：公网入口连续探测失败就重启 cloudflared")
    p_tunnel.add_argument("--url", default="",
                          help="探测地址（默认公网健康检查）")
    p_tunnel.add_argument("--exe", default="", help="cloudflared 可执行文件路径")
    p_tunnel.add_argument("--config", default="", help="cloudflared 配置路径")
    p_tunnel.add_argument("--verbose", action="store_true",
                          help="正常时也打印探测过程")
    p_tunnel.set_defaults(func=cmd_tunnel_ensure)

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
