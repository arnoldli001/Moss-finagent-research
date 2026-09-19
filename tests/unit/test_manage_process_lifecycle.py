"""manage.py 进程生命周期辅助函数的单测（不真的杀进程、不起服务）。

背景（2026-09-17 实测）：`--replace` 只按**端口监听者**找旧实例，端口之外的
历史实例会变成孤儿；而 `kill_pid_tree` 的 `/F` 是硬杀，进程来不及在 lifespan
关停段 checkpoint，于是 `data/moss_finagent.db-wal` 被截断成 0 字节、`-shm`
停在上一轮 —— 此后**每次**打开该库都立刻 `disk I/O error`，仓储 fail-open
穿透网络，首个请求 182.9 秒。现场：一个 12:28 启动的实例活到 16:51（CPU
累计 0.0s，纯占位），`-wal`/`-shm` 被它锁住，连 `Remove-Item` 都失败。
"""

from __future__ import annotations

import manage

# ---------------------------------------------------------------- 实例识别

def test_is_our_cmdline_matches_uvicorn_app() -> None:
    assert manage.is_our_cmdline(
        "python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8100") is True


def test_is_our_cmdline_matches_windows_separators() -> None:
    assert manage.is_our_cmdline(
        r"python -m uvicorn src\api\main:app --port 8100") is True


def test_is_our_cmdline_rejects_other_projects() -> None:
    """同机其它项目（如 media2text 的 main:app）绝不能被当成我们的实例。"""
    assert manage.is_our_cmdline(
        "python -m uvicorn main:app --host 0.0.0.0 --port 8200") is False


def test_is_our_cmdline_handles_none_and_empty() -> None:
    assert manage.is_our_cmdline(None) is False
    assert manage.is_our_cmdline("") is False


# ---------------------------------------------------------------- 枚举

def test_list_our_backend_pids_returns_ints() -> None:
    """真机枚举：只要求类型正确、不重复（不假设一定有几个实例）。"""
    pids = manage.list_our_backend_pids()
    assert isinstance(pids, list)
    assert all(isinstance(pid, int) and pid > 0 for pid in pids)
    assert pids == sorted(set(pids))


def test_run_text_decodes_utf8_instead_of_ansi() -> None:
    """**关键回归**：子进程输出必须按 UTF-8 解码，不能用系统 ANSI 代码页。

    实测事故（2026-09-17）：进程命令行里有中文路径
    （`D:\\quantTrader\\东莞证券QMT实盘交易端`），而 `subprocess.run(..., text=True)`
    在 Windows 上按 GBK 解码 → 读取线程抛 UnicodeDecodeError → `stdout` 变空串
    → **枚举结果静默为空**：`stop` 因此漏掉孤儿实例，`doctor` 显示"没有实例"。
    """
    import sys as _sys

    out = manage._run_text(  # noqa: SLF001
        [_sys.executable, "-X", "utf8", "-c",
         "import sys; sys.stdout.buffer.write('东莞证券QMT'.encode('utf-8'))"],
        timeout=30)
    assert out == "东莞证券QMT"


def test_run_text_returns_empty_on_bad_command() -> None:
    assert manage._run_text(["definitely-not-a-real-exe-xyz"], timeout=5) == ""  # noqa: SLF001


# ---------------------------------------------------------------- 父子进程

def test_single_pid_is_single_instance() -> None:
    assert manage.is_single_instance([123]) is True
    assert manage.is_single_instance([]) is True


def test_parent_child_pair_is_one_instance(monkeypatch) -> None:
    """`uvicorn --reload` 会派生一个子进程，两者命令行都含 src.api.main:app。

    从命令行看像"两个实例"，其实是同一个实例（reloader + worker）。
    实测踩到：`doctor` 因此报"存在多个后端实例（含端口外孤儿）"，
    把正常的 --reload 实例说成孤儿，会让人去清理不该清理的东西。
    """
    monkeypatch.setattr(manage, "_parent_of", lambda pid: 100 if pid == 101 else 1)
    assert manage.is_single_instance([100, 101]) is True


def test_independent_instances_are_not_single(monkeypatch) -> None:
    """父进程不在列表里 = 真正互相独立的实例（端口外孤儿就是这种）。"""
    monkeypatch.setattr(manage, "_parent_of", lambda _pid: 999)   # 父进程已退出
    assert manage.is_single_instance([100, 101]) is False


def test_parent_of_returns_none_when_unknown(monkeypatch) -> None:
    monkeypatch.setattr(manage, "_run_text", lambda cmd, timeout: "")
    assert manage._parent_of(123) is None   # noqa: SLF001


def test_stop_backend_processes_with_no_targets_is_noop(
    monkeypatch,
) -> None:
    """没有目标实例时不能误杀、不能报错。"""
    monkeypatch.setattr(manage, "list_our_backend_pids", lambda: [])
    monkeypatch.setattr(manage, "find_listening_pid", lambda _port: None)
    killed: list[int] = []
    monkeypatch.setattr(manage, "kill_pid_tree", lambda pid: killed.append(pid) or True)
    assert manage.stop_backend_processes(8100) == []
    assert killed == []


def test_stop_backend_processes_skips_foreign_port_owner(monkeypatch) -> None:
    """端口被别的程序占用时，绝不去停它。"""
    monkeypatch.setattr(manage, "list_our_backend_pids", lambda: [])
    monkeypatch.setattr(manage, "find_listening_pid", lambda _port: 4242)
    monkeypatch.setattr(manage, "diagnose_port", lambda _port: {
        "occupied": True, "pid": 4242, "is_ours": False, "cmdline": "other"})
    killed: list[int] = []
    monkeypatch.setattr(manage, "kill_pid_tree", lambda pid: killed.append(pid) or True)
    assert manage.stop_backend_processes(8100) == []
    assert killed == []


def test_stop_backend_processes_requests_graceful_then_hard_kills(
    monkeypatch,
) -> None:
    """先发优雅退出请求；到点仍存活才硬杀（硬杀会丢 checkpoint）。"""
    monkeypatch.setattr(manage, "list_our_backend_pids", lambda: [111, 222])
    graceful: list[int] = []
    hard: list[int] = []
    monkeypatch.setattr(manage, "request_graceful_stop",
                        lambda pid: graceful.append(pid) or True)
    monkeypatch.setattr(manage, "wait_port_closed", lambda port, timeout=12.0: True)
    # 111 优雅退出成功，222 还活着
    monkeypatch.setattr(manage, "_pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(manage, "kill_pid_tree", lambda pid: hard.append(pid) or True)

    results = manage.stop_backend_processes(8100)

    assert graceful == [111, 222]          # 两个都先请求优雅退出
    assert hard == [222]                   # 只有赖着不走的才硬杀
    assert results == [(111, True), (222, True)]


def test_stop_backend_processes_falls_back_to_port_listener(monkeypatch) -> None:
    """命令行枚举不到（权限受限等）时，退回按端口找本项目实例。"""
    monkeypatch.setattr(manage, "list_our_backend_pids", lambda: [])
    monkeypatch.setattr(manage, "find_listening_pid", lambda _port: 999)
    monkeypatch.setattr(manage, "diagnose_port", lambda _port: {
        "occupied": True, "pid": 999, "is_ours": True, "cmdline": "uvicorn ..."})
    graceful: list[int] = []
    monkeypatch.setattr(manage, "request_graceful_stop",
                        lambda pid: graceful.append(pid) or True)
    monkeypatch.setattr(manage, "wait_port_closed", lambda port, timeout=12.0: True)
    monkeypatch.setattr(manage, "_pid_alive", lambda _pid: False)

    results = manage.stop_backend_processes(8100)

    assert graceful == [999]
    assert results == [(999, True)]


# ---------------------------------------------------------------- 优雅请求

def test_request_graceful_stop_never_raises(monkeypatch) -> None:
    """进程已死/无权限/平台不支持时返回 False，不抛异常。"""
    monkeypatch.setattr(manage.os, "name", "nt", raising=False)

    def _boom(*_args, **_kwargs):
        raise OSError("no such process")

    monkeypatch.setattr(manage.os, "kill", _boom, raising=False)
    assert manage.request_graceful_stop(123456) is False


def test_wait_port_closed_returns_true_when_port_free(monkeypatch) -> None:
    monkeypatch.setattr(manage, "port_open", lambda host, port, timeout=0.4: False)
    assert manage.wait_port_closed(8100, timeout=0.1) is True


# ---------------------------------------------------------------- doctor 命令

def test_doctor_is_registered_as_subcommand() -> None:
    parser = manage.build_parser()
    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"
    assert args.func is manage.cmd_doctor


def test_doctor_reports_unavailable_db_without_raising(monkeypatch, tmp_path,
                                                      capsys) -> None:
    """库打不开时 doctor 要给出非零退出码与原因，而不是抛异常。"""
    from src.core import sqlite_recovery as rec

    db = tmp_path / "broken.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(manage, "list_our_backend_pids", lambda: [])
    monkeypatch.setattr(manage, "find_listening_pid", lambda _port: None)
    monkeypatch.setattr(rec, "ensure_sqlite_usable", lambda _p, **_k: rec.RecoveryResult(
        path=db, checked=True, healthy=False, reason="disk I/O error"))

    class _Settings:
        sqlite_path = str(db)

    import src.core.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: _Settings())

    code = manage.cmd_doctor(type("A", (), {})())
    out = capsys.readouterr().out
    assert code == 1
    assert "不可用" in out or "打不开" in out
