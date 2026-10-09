"""自定义板块的**归属迁移**（老表 → 带 `(user_id, name)` 唯一键的新表）。

`CHG-0223`。三条要钉住的性质：

1. **存量行归到该环境最早的管理员**（政策裁定，不是取证 —— 老表没有归属列，
   登录痕迹只能给出"那一刻谁登录着"，实测 pilot 的严格读数只命中一个 VIP 客户）；
2. **幂等**：`ensure_schema()` 每次进程启动都调它，第二次必须是零副作用的空操作；
3. **没有管理员 → 一行都不迁移**（留在留档表 + WARNING），**绝不产生无主行**，
   也绝不把某个客户的私货随手发给另一个客户。

顺带钉住"老表还在时 `CREATE TABLE IF NOT EXISTS` 会静默跳过"这个陷阱：
迁移必须发生在 `executescript(_SCHEMA)` **之前**，否则查询会报
`no such column: user_id`（而"加没加归属列"在代码形状上看不出来）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

#: 同 `test_quant_sector_ownership.py`：私有资产不在公开 checkout 里 ⇒ 显式 skip。
_PRIVATE_MODULE = (Path(__file__).resolve().parents[2]
                   / "src" / "quant" / "quant_select_repo.py")
if not _PRIVATE_MODULE.exists():  # pragma: no cover - 只在公开 checkout 走到
    pytest.skip("量化选股模块未随仓库发布（私有资产，见 .gitignore:74）",
                allow_module_level=True)

from src.quant.quant_select_repo import (  # noqa: E402
    LEGACY_SECTOR_TABLE,
    SECTOR_MAP_TABLE,
    SECTOR_TABLE,
    QuantSelectSqliteRepository,
    migrate_sector_ownership,
    resolve_legacy_owner,
)

LEGACY_DDL = f"""
CREATE TABLE {SECTOR_TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    kind        TEXT    NOT NULL DEFAULT 'manual',
    note        TEXT    NOT NULL DEFAULT '',
    rule        TEXT    NOT NULL DEFAULT '{{}}',
    color       TEXT    NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE TABLE {SECTOR_MAP_TABLE} (
    sector_id   INTEGER NOT NULL,
    code        TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    added_at    TEXT    NOT NULL,
    PRIMARY KEY (sector_id, code)
);
"""

USER_DDL = """
CREATE TABLE dim_user (
    user_id      TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    applied_tier TEXT NOT NULL DEFAULT 'trial',
    created_at   TEXT NOT NULL
);
"""


def _legacy_db(tmp_path, *,
               users: list[tuple[str, str, str, str]] | None = None,
               with_user_table: bool = True,
               sectors: tuple[tuple[str, list[str]], ...] = (
                   ("端侧算力", ["300124", "002472"]),
                   ("每日预选", ["600150"]),
               )) -> str:
    """手搓一个"迁移前"的库（老 DDL + 存量板块 + 用户表）。"""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_DDL)
        if with_user_table:
            conn.executescript(USER_DDL)
            for user_id, username, _status, created in (users or []):
                conn.execute(
                    "INSERT INTO dim_user(user_id, username, status,"
                    " applied_tier, created_at) VALUES (?,?,'active','admin',?)",
                    (user_id, username, created))
        for name, codes in sectors:
            cur = conn.execute(
                f"INSERT INTO {SECTOR_TABLE}"
                "(name, kind, note, rule, color, sort_order, created_at, updated_at)"
                " VALUES (?,'manual','','{}','',0,'2026-09-24T13:33:08+08:00',"
                "'2026-09-24T13:33:08+08:00')", (name,))
            sector_id = int(cur.lastrowid or 0)
            conn.executemany(
                f"INSERT INTO {SECTOR_MAP_TABLE}(sector_id, code, name, added_at)"
                " VALUES (?,?,'','2026-09-24T13:33:08+08:00')",
                [(sector_id, code) for code in codes])
        conn.commit()
    finally:
        conn.close()
    return path


def _rows(path: str, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _table_exists(path: str, table: str) -> bool:
    return bool(_rows(path, "SELECT 1 FROM sqlite_master WHERE name=?", (table,)))


# ======================================================================
# 一、迁移本身
# ======================================================================

def test_legacy_rows_move_to_earliest_admin(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MOSS_LEGACY_SECTOR_OWNER", raising=False)
    path = _legacy_db(tmp_path, users=[
        ("u_admin_old", "admin", "active", "2026-09-23T15:44:16+00:00"),
        ("u_vip_new", "arni", "active", "2026-09-24T04:09:46+00:00"),
    ])
    repo = QuantSelectSqliteRepository(path)
    asyncio.run(repo.ensure_schema())

    owners = {row[0] for row in _rows(path, f"SELECT user_id FROM {SECTOR_TABLE}")}
    assert owners == {"u_admin_old"}, f"存量行没归到最早的管理员：{owners}"
    assert len(_rows(path, f"SELECT id FROM {SECTOR_TABLE}")) == 2
    # 留档表也还在，且**一行不少**（可回滚、可事后重新归属）
    assert len(_rows(path, f"SELECT id FROM {LEGACY_SECTOR_TABLE}")) == 2
    # 成分表没被动过：板块 id 沿用老 id ⇒ 成分天然跟着
    assert len(_rows(path, f"SELECT code FROM {SECTOR_MAP_TABLE}")) == 3
    # 新表真的带归属列（否则查询会 no such column）
    cols = {row[1] for row in _rows(path, f"PRAGMA table_info({SECTOR_TABLE})")}
    assert {"user_id", "tenant_id"} <= cols


def test_migration_is_idempotent(tmp_path, monkeypatch) -> None:
    """★ `ensure_schema()` 每次启动都会跑它 —— 第二次必须是零副作用空操作。"""
    monkeypatch.delenv("MOSS_LEGACY_SECTOR_OWNER", raising=False)
    path = _legacy_db(tmp_path, users=[
        ("u_admin_old", "admin", "active", "2026-09-23T15:44:16+00:00")])
    repo = QuantSelectSqliteRepository(path)
    asyncio.run(repo.ensure_schema())
    snapshot = (
        len(_rows(path, f"SELECT id FROM {SECTOR_TABLE}")),
        len(_rows(path, f"SELECT id FROM {LEGACY_SECTOR_TABLE}")),
        len(_rows(path, f"SELECT sector_id FROM {SECTOR_MAP_TABLE}")),
        _rows(path, f"SELECT id, user_id, name FROM {SECTOR_TABLE} ORDER BY id"),
    )
    asyncio.run(repo.ensure_schema())          # 第二次
    asyncio.run(repo.ensure_schema())          # 第三次
    assert snapshot == (
        len(_rows(path, f"SELECT id FROM {SECTOR_TABLE}")),
        len(_rows(path, f"SELECT id FROM {LEGACY_SECTOR_TABLE}")),
        len(_rows(path, f"SELECT sector_id FROM {SECTOR_MAP_TABLE}")),
        _rows(path, f"SELECT id, user_id, name FROM {SECTOR_TABLE} ORDER BY id"),
    ), "重复迁移改了数据"


def test_no_admin_means_no_migration(tmp_path, monkeypatch) -> None:
    """★ 没有管理员 ⇒ **一行都不迁移**（宁可空，也不把某个客户的板块发给别人）。"""
    monkeypatch.delenv("MOSS_LEGACY_SECTOR_OWNER", raising=False)
    path = _legacy_db(tmp_path, with_user_table=False)      # 连用户表都没有
    repo = QuantSelectSqliteRepository(path)
    asyncio.run(repo.ensure_schema())

    assert _rows(path, f"SELECT id FROM {SECTOR_TABLE}") == []
    assert len(_rows(path, f"SELECT id FROM {LEGACY_SECTOR_TABLE}")) == 2
    assert _table_exists(path, LEGACY_SECTOR_TABLE)


def test_needs_owner_flag_is_not_a_misleading_alarm(tmp_path, monkeypatch) -> None:
    """★ `needs_owner` 只在"真的需要人工决定"时为真。

    为什么专门钉它：运维脚本用它决定退出码，而 `rows_copied == 0` 在
    "已经迁过（幂等空跑）"与"没有账号可归"两种情况下都成立 ——
    用后者判退出码会造出一条**假警报**（实测：第三次运行报
    "有存量板块但没有可归属的账号"，把已经成功的迁移说成没做成）。
    """
    monkeypatch.delenv("MOSS_LEGACY_SECTOR_OWNER", raising=False)
    # ① 没有管理员：需要人工决定
    no_admin_dir = tmp_path / "no_admin"
    no_admin_dir.mkdir()
    path = _legacy_db(no_admin_dir, with_user_table=False)
    conn = sqlite3.connect(path)
    try:
        assert migrate_sector_ownership(conn)["needs_owner"] is True
    finally:
        conn.close()
    # ② 已经迁过：不再需要（幂等空跑不该报警）
    done_dir = tmp_path / "done"
    done_dir.mkdir()
    path2 = _legacy_db(done_dir, users=[
        ("u_admin_old", "admin", "active", "2026-09-23T15:44:16+00:00")])
    repo = QuantSelectSqliteRepository(path2)
    asyncio.run(repo.ensure_schema())
    conn = sqlite3.connect(path2)
    try:
        report = migrate_sector_ownership(conn)
    finally:
        conn.close()
    assert report["migrated"] is False
    assert report["needs_owner"] is False, report


def test_owner_env_override(tmp_path, monkeypatch) -> None:
    """归属判错了不用改代码：`MOSS_LEGACY_SECTOR_OWNER` 支持 user_id 或 username。"""
    path = _legacy_db(tmp_path, users=[
        ("u_admin_old", "admin", "active", "2026-09-23T15:44:16+00:00"),
        ("u_real_owner", "vip_legacy_account", "active", "2026-09-24T04:44:47+00:00"),
    ])
    monkeypatch.setenv("MOSS_LEGACY_SECTOR_OWNER", "vip_legacy_account")
    repo = QuantSelectSqliteRepository(path)
    asyncio.run(repo.ensure_schema())
    owners = {row[0] for row in _rows(path, f"SELECT user_id FROM {SECTOR_TABLE}")}
    assert owners == {"u_real_owner"}, owners


def test_fresh_db_needs_no_migration(tmp_path) -> None:
    """全新库：`_SCHEMA` 直接按新形态建，迁移是空操作（不该留下留档表）。"""
    path = str(tmp_path / "fresh.db")
    asyncio.run(QuantSelectSqliteRepository(path).ensure_schema())
    assert not _table_exists(path, LEGACY_SECTOR_TABLE)
    assert _table_exists(path, SECTOR_TABLE)


def test_resolve_legacy_owner_prefers_active_admin(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MOSS_LEGACY_SECTOR_OWNER", raising=False)
    path = _legacy_db(tmp_path, users=[
        ("u_disabled", "dead_admin", "deleted", "2026-01-01T00:00:00+00:00"),
        ("u_active", "live_admin", "active", "2026-02-01T00:00:00+00:00"),
    ])
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE dim_user SET applied_tier='trial'"
                     " WHERE user_id='u_disabled'")
        conn.execute("UPDATE dim_user SET status='deleted' WHERE user_id='u_disabled'")
        conn.commit()
        assert resolve_legacy_owner(conn) == ("u_active", "admin")
    finally:
        conn.close()


def test_migration_report_is_evidence_not_a_boolean(tmp_path, monkeypatch) -> None:
    """迁移返回的是**证据**（行数/归属/去哪了），不是 `True`。"""
    monkeypatch.delenv("MOSS_LEGACY_SECTOR_OWNER", raising=False)
    path = _legacy_db(tmp_path, users=[
        ("u_admin_old", "admin", "active", "2026-09-23T15:44:16+00:00")])
    conn = sqlite3.connect(path)
    try:
        report = migrate_sector_ownership(conn)
    finally:
        conn.close()
    assert report["migrated"] is True
    assert report["rows_copied"] == 2
    assert report["rows_left_in_legacy"] == 0
    assert "u_admin_old" in report["owner"]
    assert report["legacy_table"] == LEGACY_SECTOR_TABLE


def test_dry_run_changes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MOSS_LEGACY_SECTOR_OWNER", raising=False)
    path = _legacy_db(tmp_path, users=[
        ("u_admin_old", "admin", "active", "2026-09-23T15:44:16+00:00")])
    conn = sqlite3.connect(path)
    try:
        report = migrate_sector_ownership(conn, dry_run=True)
    finally:
        conn.close()
    assert report["migrated"] is False and "[dry-run]" in report["reason"]
    assert not _table_exists(path, LEGACY_SECTOR_TABLE), "dry-run 动了库"
