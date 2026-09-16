"""做T权重档案的 SQLite 实现（原生 sqlite3 + asyncio.to_thread，与 fact_* 三张表同范式）。

## 为什么是这张表、这个库

- 落在 `settings.sqlite_path`（默认 `data/moss_finagent.db`），与
  `fact_data_points` / `fact_events` / `fact_alerts` 同一个库 ——
  「新增一张表」就是往这个库加，项目没有 alembic/migrations，
  建表方式统一是「模块内联 `_SCHEMA` + `CREATE TABLE IF NOT EXISTS` + PRAGMA 补列」。
- **不放进** `data/quant/warehouse.db`：那是 14 GiB 的行情仓库，
  换库等于换数据源；把用户偏好混进去，会出现"我换了个数据库，做T参数全丢了"。

## 为什么手写 DDL 而不是 SQLAlchemy

`src/quant/warehouse.py` 那条线用 SQLAlchemy 是因为它要支持 MySQL/PG/SQLite
三种方言；本表只在 SQLite 上落地（`build_intraday_profile_repository` 对
postgres 后端直接报错而不是静默降级），用标准库 sqlite3 少一层依赖，
也与事件/告警两条线保持一致。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime

from src.domain.intraday.models import IntradayProfile
from src.domain.intraday.repository import IntradayProfileRepository

logger = logging.getLogger(__name__)

TABLE = "dim_intraday_profile"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    weights_json TEXT NOT NULL DEFAULT '{{}}',
    daily_weights_json TEXT NOT NULL DEFAULT '{{}}',
    thresholds_json TEXT NOT NULL DEFAULT '{{}}',
    daily_thresholds_json TEXT NOT NULL DEFAULT '{{}}',
    levels_json TEXT NOT NULL DEFAULT '{{}}',
    character_profile_json TEXT NOT NULL DEFAULT '{{}}',
    template TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_intraday_profile_updated
    ON {TABLE}(updated_at);
"""

# 列名 → DDL：老库缺列时自动 ALTER 补上（"字段会长大的档案"必备，
# 参照 `event_sqlite_base.EventSqliteStore._schema_sync` 的做法）
_ADDABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("daily_weights_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("daily_thresholds_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("character_profile_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("template", "TEXT NOT NULL DEFAULT ''"),
    ("note", "TEXT NOT NULL DEFAULT ''"),
)

_COLUMNS = ("code", "name", "weights_json", "daily_weights_json",
            "thresholds_json", "daily_thresholds_json", "levels_json",
            "character_profile_json", "template", "source", "note",
            "created_at", "updated_at")


def _dump(payload: dict) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)


def _load(raw: object) -> dict:
    """JSON 列 → dict（脏数据返回空 dict 并告警，不让一行坏数据打断整个列表）。"""
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        logger.warning("权重档案含无法解析的JSON列，已按空处理：%s", str(raw)[:120])
        return {}
    return value if isinstance(value, dict) else {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class IntradayProfileStore:
    """SQLite 连接与轻量 schema 迁移（进程内只执行一次 DDL 同步）。"""

    def __init__(self, db_path: str = "data/moss_finagent.db") -> None:
        self._db_path = db_path
        self._synced = False

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _schema_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            existing = {row["name"] for row in conn.execute(
                f"PRAGMA table_info({TABLE})").fetchall()}
            for name, ddl in _ADDABLE_COLUMNS:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}")
        self._synced = True

    def _sync_once(self) -> None:
        """写入/读取路径幂等同步：进程内只做一次 PRAGMA（避免每查必扫表结构）。"""
        if not self._synced:
            self._schema_sync()


class IntradayProfileSqliteRepository(IntradayProfileStore,
                                      IntradayProfileRepository):
    """SQLite 权重档案仓储。"""

    # ---- schema ----

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._schema_sync)

    # ---- 行映射 ----

    @staticmethod
    def _to_row(profile: IntradayProfile, created_at: str) -> tuple:
        stamp = _now()
        return (
            profile.code, profile.name,
            _dump(profile.weights), _dump(profile.daily_weights),
            _dump(profile.thresholds), _dump(profile.daily_thresholds),
            _dump(profile.levels), _dump(profile.character_profile),
            profile.template, profile.source, profile.note,
            created_at or stamp, stamp,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IntradayProfile:
        return IntradayProfile(
            code=str(row["code"]), name=str(row["name"] or ""),
            weights=_load(row["weights_json"]),
            daily_weights=_load(row["daily_weights_json"]),
            thresholds=_load(row["thresholds_json"]),
            daily_thresholds=_load(row["daily_thresholds_json"]),
            levels=_load(row["levels_json"]),
            character_profile=_load(row["character_profile_json"]),
            template=str(row["template"] or ""),
            source=str(row["source"] or "manual"),
            note=str(row["note"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    # ---- 同步实现 ----

    def _get_sync(self, code: str) -> IntradayProfile | None:
        self._sync_once()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE} WHERE code = ?", (str(code).strip(),)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def _upsert_sync(self, profile: IntradayProfile) -> IntradayProfile:
        self._sync_once()
        existing = self._get_sync(profile.code)
        created_at = existing.created_at if existing is not None else ""
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(
            f"{name}=excluded.{name}" for name in _COLUMNS
            if name not in ("code", "created_at"))
        sql = (
            f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(code) DO UPDATE SET {updates}"
        )
        with self._connect() as conn:
            conn.execute(sql, self._to_row(profile, created_at))
        saved = self._get_sync(profile.code)
        return saved if saved is not None else profile

    def _list_sync(self, limit: int) -> list[IntradayProfile]:
        self._sync_once()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {TABLE} ORDER BY updated_at DESC, code ASC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        return [self._from_row(row) for row in rows]

    def _delete_sync(self, code: str) -> bool:
        self._sync_once()
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {TABLE} WHERE code = ?", (str(code).strip(),))
            return bool(cursor.rowcount)

    # ---- 异步端口 ----

    async def get(self, code: str) -> IntradayProfile | None:
        return await asyncio.to_thread(self._get_sync, code)

    async def upsert(self, profile: IntradayProfile) -> IntradayProfile:
        return await asyncio.to_thread(self._upsert_sync, profile)

    async def list(self, *, limit: int = 200) -> list[IntradayProfile]:
        return await asyncio.to_thread(self._list_sync, limit)

    async def delete(self, code: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, code)
