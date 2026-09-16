"""QMT 访问护栏单元测试（离线）。

背景：服务进程曾在「用户新加一只票 → 触发 QMT 补下载」时无 traceback 猝死。
本文件锁住两条护栏：进程级互斥锁、补下载子进程隔离。
"""

from __future__ import annotations

import subprocess
import threading

import pytest

from src.core import qmt_guard


def test_qmt_lock_is_process_wide_singleton() -> None:
    """两个模块必须共用同一把锁 —— 各自一把等于没锁。"""
    from src.core.qmt_guard import qmt_lock

    assert qmt_lock() is qmt_lock()
    assert qmt_lock() is qmt_guard.qmt_lock()
    # 可重入：同线程嵌套获取不能自锁死
    with qmt_lock():
        with qmt_lock():
            assert qmt_lock()._is_owned() is True


def test_qmt_lock_serializes_threads() -> None:
    """锁必须真的互斥（并发访问 QMT 是要消除的那个变量）。"""
    from src.core.qmt_guard import qmt_lock

    order: list[str] = []
    started = threading.Event()

    def worker(name: str) -> None:
        with qmt_lock():
            order.append(f"{name}-in")
            started.set()
            threading.Event().wait(0.05)
            order.append(f"{name}-out")

    first = threading.Thread(target=worker, args=("a",))
    second = threading.Thread(target=worker, args=("b",))
    first.start()
    started.wait(2.0)
    second.start()
    first.join(5.0)
    second.join(5.0)
    # a 完整进出后 b 才能进（不会出现 a-in, b-in, a-out, b-out 的交错）
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_download_history_isolated_success(monkeypatch) -> None:
    """成功路径：子进程返回 0 → (True, "")。"""
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, b"OK\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = qmt_guard.download_history_isolated(
        "600176.SH", "1d", start_time="20250101", end_time="20260915")
    assert ok is True and detail == ""
    script = captured["args"][-1]  # type: ignore[index]
    assert "download_history_data('600176.SH', '1d'" in script
    assert "incrementally=True" in script
    assert captured["kwargs"]["timeout"] == qmt_guard.DEFAULT_DOWNLOAD_TIMEOUT


def test_download_history_isolated_survives_native_crash(monkeypatch) -> None:
    """子进程原生崩溃（非零退出）→ 转成失败原因，不抛异常、不带走服务进程。"""
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, -1, b"", b"native crash")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = qmt_guard.download_history_isolated("600176.SH", "1m")
    assert ok is False
    assert "-1" in detail and "native crash" in detail


def test_download_history_isolated_handles_timeout(monkeypatch) -> None:
    """超时必须降级为返回值，而不是把异常抛进请求链路。"""
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = qmt_guard.download_history_isolated("600176.SH", "1m", timeout=1.0)
    assert ok is False and "超时" in detail


def test_download_history_isolated_never_raises(monkeypatch) -> None:
    """连子进程都起不来（权限/环境问题）也不能抛。"""
    def fake_run(args, **kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = qmt_guard.download_history_isolated("600176.SH", "1m")
    assert ok is False and "启动失败" in detail


def test_intraday_qmt_loader_uses_isolated_download(monkeypatch) -> None:
    """做T模块遇到「本地无数据」时必须走隔离下载，而不是进程内 download。"""
    import pandas as pd

    from src.intraday import sources as intraday_sources

    calls: dict[str, object] = {"in_process_download": 0}

    class FakeXt:
        @staticmethod
        def get_market_data_ex(*args, **kwargs):
            return {"600176.SH": pd.DataFrame()}

        @staticmethod
        def download_history_data(*args, **kwargs):  # 进程内下载：绝不允许被调用
            calls["in_process_download"] = calls["in_process_download"] + 1

    monkeypatch.setattr(
        qmt_guard, "download_history_isolated",
        lambda *a, **k: (calls.__setitem__("isolated", (a, k)), (False, "崩溃"))[1])
    loader = intraday_sources.QmtMinuteSource()
    monkeypatch.setattr(loader, "_client", lambda: FakeXt())
    with pytest.raises(intraday_sources.DataFetchError):
        loader._load("600176", "1d", 1)
    assert calls["in_process_download"] == 0
    assert "isolated" in calls
