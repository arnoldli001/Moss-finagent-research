"""回归测试：manage.py 起的子进程不得在桌面弹出可见控制台窗口。

背景（2026-09-18 实测）：`_spawn_daemon` 原先用
`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` 起后台服务。`DETACHED_PROCESS`
让子进程不继承任何控制台，Windows 于是给它**新分配一个可见控制台**（conhost），
窗口标题就是解释器全路径 —— 现场实测到的窗口：

    ConsoleWindowClass  title = 'D:\\code\\Moss-finagent-research\\.venv\\Scripts\\python.exe'

DSH 里的 Agent 每执行一次 `manage.py start --daemon --replace` 就弹一个，用户看到
的就是"桌面一直弹 cmd 黑窗"。实测五种标志组合后的结论：

    DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP          -> 弹窗
    DETACHED_PROCESS | CREATE_NO_WINDOW                  -> 仍弹窗（DETACHED 抵消 NO_WINDOW）
    CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW          -> 不弹，且父进程退出后守护仍存活

另有两类坑一并锁住：
  1. 前台可交互命令（`uvicorn` 前台、`npm run dev`）**必须保留**控制台给用户看日志；
  2. Windows 上 `text=True` 会按系统 ANSI(GBK) 解码，遇到非 UTF-8 字节会在读取
     线程抛 UnicodeDecodeError（与 `_run_text` 文档记录的是同一个坑），故这几个
     取输出的调用必须带 `errors="replace"`。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import manage

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="仅在 Windows 上成立")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


# ------------------------------------------------------------ 守护进程标志

@WINDOWS_ONLY
def test_spawn_daemon_never_uses_detached_process(monkeypatch) -> None:
    """**核心回归**：DETACHED_PROCESS 会让 Windows 分配一个可见控制台窗口。"""
    captured: dict[str, object] = {}

    class FakeProc:
        pid = 12345

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(manage.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manage, "_write_pid", lambda name, pid: None)

    pid = manage._spawn_daemon("probe", [sys.executable, "-c", "pass"], manage.ROOT)

    assert pid == 12345
    flags = int(captured["creationflags"])  # type: ignore[arg-type]
    assert not flags & DETACHED_PROCESS, (
        "绝不能再用 DETACHED_PROCESS：它会新开一个可见控制台窗口（用户会看到弹黑窗）")
    assert flags & CREATE_NO_WINDOW, "必须带 CREATE_NO_WINDOW 才能不分配控制台"
    assert flags & CREATE_NEW_PROCESS_GROUP, "必须保留进程组隔离（Ctrl+C 不误伤服务）"


@WINDOWS_ONLY
def test_no_console_constant_matches_win32_flag() -> None:
    assert manage._NO_CONSOLE == CREATE_NO_WINDOW  # noqa: SLF001


def test_spawn_daemon_still_redirects_to_log_file(monkeypatch, tmp_path) -> None:
    """守护进程的 stdout/stderr 必须仍落 data/run/*.log（去掉控制台不能丢日志）。"""
    captured: dict[str, object] = {}

    class FakeProc:
        pid = 1

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(manage, "RUN_DIR", tmp_path)
    monkeypatch.setattr(manage.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manage, "_write_pid", lambda name, pid: None)

    manage._spawn_daemon("probe", [sys.executable, "-c", "pass"], tmp_path)

    assert captured["stdout"] is not None, "stdout 必须重定向到日志文件"
    assert captured["stderr"] is subprocess.STDOUT
    assert captured["cwd"] == str(tmp_path)


# ------------------------------------------------------------ 前台命令保留控制台

def _manage_source() -> str:
    return (manage.ROOT / "manage.py").read_text(encoding="utf-8")


def test_foreground_uvicorn_keeps_console() -> None:
    """前台 uvicorn 是给用户看日志的，绝不能加 _NO_CONSOLE。"""
    src = _manage_source()
    assert "subprocess.run(uvicorn_cmd, cwd=str(ROOT), check=False)\n" in src
    assert "subprocess.run(uvicorn_cmd, cwd=str(ROOT), check=False, creationflags" not in src


def test_foreground_frontend_keeps_console() -> None:
    """前台 npm dev server 同理。"""
    src = _manage_source()
    assert '[npm, "run", "dev"], cwd=str(WEB_DIR), check=False, shell=True)' in src
    assert 'shell=True, creationflags=_NO_CONSOLE)' not in src


# ------------------------------------------------------------ 输出解码

@WINDOWS_ONLY
def test_tasklist_output_survives_non_utf8(monkeypatch) -> None:
    """tasklist 的中文提示语是 GBK 字节；带 errors=replace 后不得抛异常。"""

    class FakeCompleted:
        stdout = "信息: 没有运行的任务匹配指定标准。"
        returncode = 0

    monkeypatch.setattr(manage.subprocess, "run",
                        lambda *a, **k: FakeCompleted())
    assert manage._pid_alive(999999) is False


@WINDOWS_ONLY
def test_probe_calls_pass_errors_replace() -> None:
    """三处会输出中文的取输出调用必须带 errors="replace"（否则读取线程会炸）。"""
    src = _manage_source()
    assert src.count('errors="replace"') >= 3


def test_run_text_still_decodes_utf8() -> None:
    """_run_text 的 UTF-8 强制解码不能被本次改动破坏。"""
    out = manage._run_text(  # noqa: SLF001
        [sys.executable, "-X", "utf8", "-c",
         "import sys; sys.stdout.buffer.write('东莞证券QMT'.encode('utf-8'))"],
        timeout=30)
    assert out == "东莞证券QMT"


@WINDOWS_ONLY
def test_run_text_passes_no_console_flag(monkeypatch) -> None:
    class FakeCompleted:
        stdout = b"ok\n"
        returncode = 0

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return FakeCompleted()

    monkeypatch.setattr(manage.subprocess, "run", fake_run)
    assert manage._run_text(["echo", "ok"], timeout=5) == "ok\n"
    assert int(captured["creationflags"]) & CREATE_NO_WINDOW  # type: ignore[arg-type]
