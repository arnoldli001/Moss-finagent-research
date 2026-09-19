"""SQLite 陈旧 WAL 索引自愈的单测。

现场证据（2026-09-17，真实故障，非构造）：服务重启后 `data/moss_finagent.db`
（729MB，主库完好）在**任何**普通打开方式下都在 0.000s 内失败：

    sqlite3.connect("data/moss_finagent.db")             -> disk I/O error
    sqlite3.connect("file:...?mode=ro", uri=True)        -> disk I/O error
    sqlite3.connect("file:...?mode=rw", uri=True)        -> disk I/O error
    sqlite3.connect("file:...?immutable=1", uri=True)    -> OK（104 万行 0.04s）

当时伴生文件是 `-wal` = **0 字节**、`-shm` = **64KB**（上一轮进程留下的索引）。
把这个主库单独复制一份（不带伴生文件）立刻可读 → 坏的只是伴生文件。

注意：这套"损坏状态"依赖被硬杀的进程留下的共享内存索引，**无法在单测里稳定
构造**（实测：只用 0 字节 `-wal` + 全零 `-shm` 手工摆出来时，SQLite 会重建索引
并正常打开）。因此这里测的是自愈模块的**确定部分**：健康库绝不被改动、
隔离动作是"改名备份"而非删除、主库数据在隔离后完好、错误判定与降级路径。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.core import sqlite_recovery as rec


def _make_db(path: Path, rows: int = 3) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE t(x INTEGER)")
        con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
        con.commit()
    finally:
        con.close()


def _count(path: Path) -> int:
    con = sqlite3.connect(str(path))
    try:
        return int(con.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        con.close()


# ---------------------------------------------------------------- 健康库

def test_healthy_db_is_left_completely_untouched(tmp_path: Path) -> None:
    """健康库只做一次计数就返回，磁盘上不应有任何新文件。"""
    db = tmp_path / "ok.db"
    _make_db(db, rows=5)
    before = {p.name for p in tmp_path.iterdir()}

    result = rec.ensure_sqlite_usable(db)

    assert result.healthy is True
    assert result.recovered is False
    assert result.quarantined == []
    assert result.tables == 1
    assert {p.name for p in tmp_path.iterdir()} == before
    assert not (tmp_path / "recovery").exists()
    assert _count(db) == 5


def test_missing_db_is_skipped_not_created(tmp_path: Path) -> None:
    """库不存在时直接跳过：不能在这里造一个空库（建表由各仓储负责）。"""
    db = tmp_path / "nope.db"
    result = rec.ensure_sqlite_usable(db)
    assert result.healthy is True
    assert result.checked is False
    assert "不存在" in result.reason
    assert not db.exists()


def test_recovery_can_be_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "ok.db"
    _make_db(db)
    monkeypatch.setenv("MOSS_SQLITE_RECOVERY", "0")
    result = rec.ensure_sqlite_usable(db)
    assert result.checked is True
    assert "关闭" in result.reason


def test_missing_db_is_allowed_even_when_recovery_disabled(tmp_path: Path) -> None:
    """关掉自愈也不能把"库不存在"报成故障。"""
    result = rec.ensure_sqlite_usable(tmp_path / "nope.db")
    assert result.healthy is True


# ---------------------------------------------------------------- 隔离动作

def test_quarantine_renames_sidecars_into_backup_dir(tmp_path: Path) -> None:
    """隔离 = **改名备份**，绝不直接删除（WAL 里可能有未 checkpoint 的事务）。"""
    db = tmp_path / "x.db"
    _make_db(db)
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    wal.write_bytes(b"stale-wal-bytes")
    shm.write_bytes(b"\x01" * 4096)
    backup = tmp_path / "recovery" / "x-20260101-000000"

    moved = rec._quarantine(db, backup)  # noqa: SLF001 直接测隔离动作

    assert sorted(moved) == ["x.db-shm", "x.db-wal"]
    assert not wal.exists() and not shm.exists()
    assert (backup / "x.db-wal").read_bytes() == b"stale-wal-bytes"
    assert (backup / "x.db-shm").stat().st_size == 4096
    # 主库文件本身绝不能被碰
    assert db.exists()
    assert _count(db) == 3


def test_quarantine_ignores_already_absent_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "y.db"
    _make_db(db)
    moved = rec._quarantine(db, tmp_path / "recovery" / "y-1")  # noqa: SLF001
    assert moved == []
    assert _count(db) == 3


def test_sidecar_sizes_reports_zero_when_absent(tmp_path: Path) -> None:
    db = tmp_path / "z.db"
    _make_db(db)
    assert rec.sidecar_sizes(db) == (0, 0)
    Path(str(db) + "-wal").write_bytes(b"")
    Path(str(db) + "-shm").write_bytes(b"\x00" * 32768)
    assert rec.sidecar_sizes(db) == (0, 32768)


# ---------------------------------------------------------------- 错误判定

@pytest.mark.parametrize("message", [
    "disk I/O error",
    "DISK I/O ERROR",
    "database disk image is malformed",
    "file is not a database",
    "unable to open database file",
])
def test_wal_error_hints_recognised(message: str) -> None:
    assert rec._looks_like_wal_error(message) is True  # noqa: SLF001


@pytest.mark.parametrize("message", [
    "no such table: t",
    "database is locked",
    "attempt to write a readonly database",
])
def test_other_errors_are_not_treated_as_wal_problems(message: str) -> None:
    """非伴生文件类错误不自动处理（例如权限、锁竞争）。"""
    assert rec._looks_like_wal_error(message) is False  # noqa: SLF001


# ---------------------------------------------------------------- 关停收尾

def test_checkpoint_and_close_drains_wal(tmp_path: Path) -> None:
    """关停时 checkpoint 一次：下次启动拿到的库是干净的（不再有 -wal 残留）。"""
    db = tmp_path / "w.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        # 连接不关，WAL 里就有内容
        assert rec.checkpoint_and_close(db) is True
    finally:
        con.close()
    assert _count(db) == 1


def test_checkpoint_and_close_returns_false_for_missing_db(tmp_path: Path) -> None:
    assert rec.checkpoint_and_close(tmp_path / "nope.db") is False


def test_release_all_connections_closes_everything(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t(x)")
    con.commit()
    rec.register_connection("unit-test", con)
    assert rec.release_all_connections() >= 1
    # 已关闭的连接再执行会抛 ProgrammingError，说明确实关了
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_is_healthy_matches_openability(tmp_path: Path) -> None:
    db = tmp_path / "h.db"
    _make_db(db)
    assert rec.is_healthy(db) is True


# ---------------------------------------------------------------- 批量

def test_ensure_all_usable_survives_one_bad_path(tmp_path: Path) -> None:
    good = tmp_path / "good.db"
    _make_db(good)
    results = rec.ensure_all_usable([good, tmp_path / "missing.db"])
    assert len(results) == 2
    assert all(r.healthy for r in results)


def test_ensure_all_usable_never_raises_on_garbage(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """自愈本身有异常也必须被吞掉 —— 它绝不能拦住启动。"""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rec, "ensure_sqlite_usable", _boom)
    assert rec.ensure_all_usable([tmp_path / "a.db"]) == []


def test_quarantine_dir_for_defaults_next_to_db(tmp_path: Path) -> None:
    db = tmp_path / "k.db"
    assert rec.quarantine_dir_for(db) == tmp_path / "recovery"
    assert rec.quarantine_dir_for(db, backup_root=tmp_path / "elsewhere") == \
        tmp_path / "elsewhere"
