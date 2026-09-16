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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "data" / "run"
WEB_DIR = ROOT / "web"

SERVICE_SIGNATURE = "moss-finagent-research"
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
        ).stdout.strip()
        return out or None
    except (subprocess.SubprocessError, OSError):
        return None


def health_signature(port: int) -> str | None:
    """请求 /health，返回 service 签名；非本项目或无响应返回 None。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v1/health", timeout=1.0
        ) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        svc = body.get("service")
        return svc if isinstance(svc, str) else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
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
            )
            return r.returncode == 0
        import signal
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


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
) -> int:
    """后台启动子进程，返回 PID；日志与 PID 文件落 data/run/。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUN_DIR / f"{name}.log"
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    log_fh = log_path.open("ab")  # noqa: SIM115 守护进程生命周期独立，不关闭
    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd), "stdout": log_fh, "stderr": subprocess.STDOUT,
        "creationflags": flags,
    }
    if shell:
        popen_kwargs["shell"] = True
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    _write_pid(name, proc.pid)
    return proc.pid


def cmd_start(args: argparse.Namespace) -> int:
    host, port = args.host, int(args.port)

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
                kill_pid_tree(pid)  # type: ignore[arg-type]
                for _ in range(10):
                    if not port_open("127.0.0.1", port):
                        break
                    time.sleep(0.3)
            else:
                print(f"✅ 本项目后端已在运行：http://127.0.0.1:{port} "
                      f"(PID={info['pid']})，无需重复启动。", file=sys.stderr)
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
    ]
    if args.reload:
        uvicorn_cmd.append("--reload")

    if args.daemon:
        pid = _spawn_daemon("backend", uvicorn_cmd, ROOT)
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
        subprocess.run(uvicorn_cmd, cwd=str(ROOT), check=False)
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
        pid = read_pid_file(RUN_DIR / f"{name}.pid") if RUN_DIR.exists() else None
        if pid is None and port_open("127.0.0.1", port):
            # PID 文件丢失：仅当确认是本项目实例时才按端口兜底停止
            info = diagnose_port(port)
            pid = info["pid"] if info["is_ours"] else None
            if info["occupied"] and not info["is_ours"]:
                print(f"⚠️ 端口 {port} 非本项目实例，跳过（不停止其他程序）。",
                      file=sys.stderr)
        if pid is not None:
            ok = kill_pid_tree(pid)
            stopped.append((name, pid, ok))
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
        sig = health_signature(DEFAULT_BACKEND_PORT)
        if sig == SERVICE_SIGNATURE:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}/api/v1/health",
                    timeout=1.5,
                ) as resp:
                    h = json.loads(resp.read().decode("utf-8"))
                gw = h.get("model_gateway", {})
                extra = (
                    f"status={h.get('status')} ollama={gw.get('ollama')} "
                    f"deepseek={gw.get('deepseek')}"
                )
            except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
                extra = ""
            rows.append(("后端 API", DEFAULT_BACKEND_PORT, "本项目运行中", extra))
        else:
            rows.append(("后端端口", DEFAULT_BACKEND_PORT, "被其他程序占用", ""))
    else:
        rows.append(("后端 API", DEFAULT_BACKEND_PORT, "未运行",
                     "python manage.py start"))
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
    ).returncode


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

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Moss-FinAgent-Research 统一管理（启动/停止/状态/隔离测试）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="启动后端（默认127.0.0.1:8100）")
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

    p_test = sub.add_parser(
        "test", help="隔离临时数据目录运行 pytest；后续参数原样透传，如 test -q -k x",
    )
    p_test.set_defaults(func=cmd_test)

    p_fe = sub.add_parser("frontend", help="启动前端 dev server (5173)")
    p_fe.add_argument("--daemon", action="store_true")
    p_fe.set_defaults(func=cmd_frontend)

    p_build = sub.add_parser("build", help="构建前端到 web/dist")
    p_build.set_defaults(func=cmd_build)

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
    return parser


def main(argv: list[str] | None = None) -> int:
    # parse_known_args：test 子命令后的 -q/-k 等 pytest 参数原样透传
    args, extras = build_parser().parse_known_args(argv)
    if args.command == "test":
        args.pytest_args = extras
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
