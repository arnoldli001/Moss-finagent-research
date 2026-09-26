"""行情仓唯一入口（`src/mainline/warehouse.py`）的单元测试。

## 为什么必须钉住它

这个模块是为一次**咬了三口**的事故建的：

    第一次  `sources.WarehouseSource`      → 就地加 immutable 兜底
    第二次  `relevance.py` 4 处自建连接     → 提纯链路静默失效，
            导致「锂电池概念」把 MLCC 的风华高科选成龙头（用户报障）
    第三次  `member_pure.load_market_caps` → 提纯重建直接崩

根因不是"漏了某处"，而是**没有唯一入口**。所以这里测的不是"某个函数对不对"，
而是三条**不变量**：

1. **惰性打开必须被探测**：`sqlite3.connect` 不真正打开文件，故障要到第一次
   `execute` 才暴露。如果 `open_warehouse` 不探一次，兜底永远不会触发 ——
   这正是我前两次修复都差点漏掉的点。
2. **失败后要用 `immutable=1` 重试**，并且**记住**这个决定（进程级），
   否则每次查询都要重付一次失败连接的代价。
3. **非 I/O 类错误必须原样抛出**，不能被"兜底"吞掉 ——
   把 `no such table` 也当成"环境故障"会掩盖真正的 bug。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.mainline import warehouse  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mode(monkeypatch):
    """每个用例重置进程级记忆，避免互相污染。"""
    monkeypatch.setattr(warehouse, "_IMMUTABLE", False, raising=False)
    yield


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(code TEXT)")
    conn.execute("INSERT INTO t VALUES('000636')")
    conn.commit()
    conn.close()


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.Error):
        warehouse.open_warehouse(tmp_path / "nope.db")


def test_reads_normally(tmp_path: Path) -> None:
    _make_db(tmp_path / "w.db")
    conn = warehouse.open_warehouse(tmp_path / "w.db")
    try:
        assert conn.execute("SELECT code FROM t").fetchone()["code"] == "000636"
    finally:
        conn.close()
    assert warehouse.prefer_immutable() is False, "正常路径不该切换模式"


def test_falls_back_to_immutable_and_remembers(monkeypatch,
                                               tmp_path: Path) -> None:
    """第一次 `disk I/O error` → 用 `immutable=1` 重试，并记住这次失败。

    ⚠️ 关键在"第一次就探测到"：真实驱动是惰性的，`connect()` 本身不报错，
    报错发生在 `_connect_probe` 之后。这里用 monkeypatch 模拟
    "connect 成功但第一次 execute 抛 disk I/O error"。
    """
    _make_db(tmp_path / "w.db")
    seen: list[str] = []
    real_connect = sqlite3.connect

    class _Boom:
        row_factory = None

        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            pass

    def fake_connect(dsn, *args, **kwargs):
        seen.append(str(dsn))
        if "immutable=1" in str(dsn):
            return real_connect(dsn.replace("&immutable=1", ""),
                                *args, **kwargs)
        return _Boom()

    monkeypatch.setattr(warehouse.sqlite3, "connect", fake_connect)
    conn = warehouse.open_warehouse(tmp_path / "w.db")
    try:
        assert conn.execute("SELECT code FROM t").fetchone()["code"] == "000636"
    finally:
        conn.close()
    assert warehouse.prefer_immutable() is True, "必须记住这次降级"
    assert any("immutable=1" in dsn for dsn in seen), "必须用 immutable 重试"

    # 第二次调用应**直接**走 immutable（不再尝试普通只读）
    before = len(seen)
    conn = warehouse.open_warehouse(tmp_path / "w.db")
    conn.close()
    assert seen[before].endswith("?mode=ro&immutable=1"), \
        "记住之后应当直接用可用路径，不再重复失败"


def test_non_io_error_is_not_swallowed(monkeypatch, tmp_path: Path) -> None:
    """非 I/O 类错误必须原样抛出 —— 兜底不能掩盖真正的 bug。"""
    _make_db(tmp_path / "w.db")

    class _Bad:
        row_factory = None

        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("no such table: nope")

        def close(self):
            pass

    monkeypatch.setattr(warehouse.sqlite3, "connect",
                        lambda *a, **k: _Bad())
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        warehouse.open_warehouse(tmp_path / "w.db")
    assert warehouse.prefer_immutable() is False, \
        "语法/结构类错误不该触发模式切换"


def test_no_module_opens_warehouse_directly() -> None:
    """**不变量**：`src/mainline/` 下不允许再出现自建行情仓连接。

    这条是本模块存在的理由。三类 bug 都是"某个模块觉得自己
    `sqlite3.connect(..., mode=ro)` 一下就行"。用源码扫描钉死它，
    比在代码评审里靠人眼可靠。
    """
    offenders: list[str] = []
    for path in (ROOT / "src" / "mainline").glob("*.py"):
        if path.name == "warehouse.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "sqlite3.connect(" not in line:
                continue
            # 只允许连**主线自己的** SQLite（缓存/结果库），不允许连行情仓
            if "warehouse" in line.lower():
                offenders.append(f"{path.name}:{lineno} {line.strip()}")
    assert not offenders, (
        "以下位置仍在自行打开行情仓，应改用 warehouse.open_warehouse：\n"
        + "\n".join(offenders))
