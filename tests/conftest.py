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
