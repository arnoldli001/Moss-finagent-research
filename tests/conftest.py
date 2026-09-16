"""共享fixtures。

不用pytest内置tmp_path：本机环境下其目录清理机制单次耗时30-60s
（已实测任意测试+tmp_path必现，与pytest版本/目录位置无关），
改用tempfile.mkdtemp自管理，实测<0.01s。
"""

import os
import shutil
import tempfile
from collections.abc import Generator

import pytest


@pytest.fixture
def tmp_dir() -> Generator[str, None, None]:
    """快速临时目录（str路径），测试结束自动清理。"""
    d = tempfile.mkdtemp(prefix="moss_finagent_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def repo(tmp_dir):
    from src.infrastructure.repositories.macro_repo import MacroRepository

    return MacroRepository(db_path=os.path.join(tmp_dir, "test.db"))


@pytest.fixture(autouse=True)
def _no_background_refresh(monkeypatch):
    """默认关掉"后台刷新"：它会在测试进程里**真发网络请求 / 起 akshare 子进程**。

    2026-09-16 实测踩到的坑：`test_light_snapshot_skips_slow_subsystems` 用真实
    `IntradayService` 跑冷启动快照，而板块冷取那时被改成了"放后台拉"，
    于是测试里起了真实子进程；用例结束时 `asyncio.run` 取消任务，子进程的
    stdio 管道却没被收掉，整个 pytest 卡在 53% 十几分钟。
    （同时暴露了"取消路径不 kill 子进程"的生产隐患，已在
    `subproc.run_json_subprocess` 里补上 CancelledError 分支。）

    需要验证后台刷新本身的用例，在自己的用例里 monkeypatch 回来即可。
    """
    from src.intraday import board as board_module
    from src.intraday import service as service_module

    monkeypatch.setattr(
        board_module.BoardContextProvider, "_schedule_refresh",
        lambda self, name, kind="concept": None)
    monkeypatch.setattr(
        service_module.IntradayService, "_schedule_watchlist_refresh",
        lambda self: None)
    yield
