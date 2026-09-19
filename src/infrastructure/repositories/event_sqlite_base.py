"""事件/告警 SQLite 仓储共享件（schema、行映射、连接管理）。

仅被同目录仓储模块复用，不对领域层暴露；阻塞SQLite IO一律经
asyncio.to_thread 卸载，值参数全部使用 ? 占位。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypeVar

from src.domain.alerts.models import (
    Alert,
    AlertLevel,
    AlertStatus,
    AlertType,
    Event,
    EventEntities,
    EventType,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_events (
    event_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    content_key TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    publish_time TEXT NOT NULL DEFAULT '',
    fetch_time TEXT NOT NULL,
    entities_json TEXT NOT NULL DEFAULT '{}',
    raw_data_json TEXT NOT NULL DEFAULT '{}',
    analyzed INTEGER NOT NULL DEFAULT 0,
    tenant_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_events_tenant_type ON fact_events(tenant_id, event_type);

CREATE TABLE IF NOT EXISTS fact_alerts (
    alert_id TEXT PRIMARY KEY,
    alert_key TEXT NOT NULL UNIQUE,
    content_key TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    alert_level TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    risk_score REAL NOT NULL DEFAULT 0,
    opportunity_score REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    affected_stocks_json TEXT NOT NULL DEFAULT '[]',
    affected_industries_json TEXT NOT NULL DEFAULT '[]',
    impact_path TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    event_publish_time TEXT NOT NULL DEFAULT '',
    trigger_time TEXT NOT NULL,
    expire_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    tenant_id TEXT NOT NULL,
    disclaimer TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON fact_alerts(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_alerts_trigger ON fact_alerts(trigger_time);
"""

_EVENT_COLS = (
    "event_id", "event_key", "content_key", "event_type", "title", "content",
    "source_name", "source_url", "publish_time", "fetch_time",
    "entities_json", "raw_data_json", "tenant_id",
)
_ALERT_COLS = (
    "alert_id", "alert_key", "content_key", "event_id", "alert_type",
    "alert_level", "title", "description", "risk_score", "opportunity_score",
    "confidence", "affected_stocks_json", "affected_industries_json",
    "impact_path", "source_name", "source_url", "event_publish_time",
    "trigger_time", "expire_time", "status", "tenant_id", "disclaimer",
)


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def event_to_row(event: Event) -> tuple:
    return (
        event.event_id, event.event_key, event.content_key,
        event.event_type.value, event.title, event.content,
        event.source_name, event.source_url, event.publish_time,
        event.fetch_time, event.entities.model_dump_json(),
        json.dumps(event.raw_data, ensure_ascii=False), event.tenant_id,
    )


def row_to_event(row: sqlite3.Row) -> Event:
    entities = _loads(row["entities_json"], {})
    return Event(
        event_id=row["event_id"], event_key=row["event_key"],
        content_key=row["content_key"],
        event_type=EventType(row["event_type"]), title=row["title"],
        content=row["content"], source_name=row["source_name"],
        source_url=row["source_url"], publish_time=row["publish_time"],
        fetch_time=row["fetch_time"],
        entities=EventEntities.model_validate(entities),
        raw_data=_loads(row["raw_data_json"], {}),
        tenant_id=row["tenant_id"],
    )


def alert_to_row(alert: Alert) -> tuple:
    stocks = [s.model_dump() for s in alert.affected_stocks]
    return (
        alert.alert_id, alert.alert_key, alert.content_key, alert.event_id,
        alert.alert_type.value, alert.alert_level.value, alert.title,
        alert.description, alert.risk_score, alert.opportunity_score,
        alert.confidence, json.dumps(stocks, ensure_ascii=False),
        json.dumps(alert.affected_industries, ensure_ascii=False),
        alert.impact_path, alert.source_name, alert.source_url,
        alert.event_publish_time, alert.trigger_time, alert.expire_time,
        alert.status.value, alert.tenant_id, alert.disclaimer,
    )


def row_to_alert(row: sqlite3.Row) -> Alert:
    return Alert(
        alert_id=row["alert_id"], alert_key=row["alert_key"],
        content_key=row["content_key"], event_id=row["event_id"],
        alert_type=AlertType(row["alert_type"]),
        alert_level=AlertLevel(row["alert_level"]), title=row["title"],
        description=row["description"], risk_score=row["risk_score"],
        opportunity_score=row["opportunity_score"], confidence=row["confidence"],
        affected_stocks=_loads(row["affected_stocks_json"], []),
        affected_industries=_loads(row["affected_industries_json"], []),
        impact_path=row["impact_path"], source_name=row["source_name"],
        source_url=row["source_url"], event_publish_time=row["event_publish_time"],
        trigger_time=row["trigger_time"], expire_time=row["expire_time"],
        status=AlertStatus(row["status"]), tenant_id=row["tenant_id"],
        disclaimer=row["disclaimer"],
    )


def insert_sql(table: str, cols: tuple[str, ...]) -> str:
    return (
        f"INSERT OR IGNORE INTO {table} (" + ", ".join(cols) + ") VALUES ("
        + ", ".join("?" for _ in cols) + ")"
    )


#: 与项目其它仓储（fund_flow / intraday_profile / quant / auction_select）同范式：
#: 主库 data/moss_finagent.db 有做T采集、量化仓库、资金流等多条链并发写入，
#: 单靠 WAL 不足以保证每个短连接都能排到队 —— 必须显式给锁等待时间。
#:
#: 但等待时间**按代价分级**（2026-09-18 实测教训）：服务是单事件循环 +
#: to_thread 默认线程池，一个请求阻塞在写锁上就占住一个线程。把"读路径里
#: 顺带的清理写"也设成 20s 排队，主库被占时线程池会被打满，连看列表都超时，
#: 比原来的 500 更糟（实测：占锁 12s 时 4 次请求全部超时）。
#:   - 关键写（事件/告警入库）值得排队：CONNECT_TIMEOUT_S
#:   - 顺带写（懒过期）/用户点击写：秒级失败即降级：FAST_TIMEOUT_S
CONNECT_TIMEOUT_S = 20.0
BUSY_TIMEOUT_MS = 20000
FAST_TIMEOUT_S = 2.0
FAST_BUSY_TIMEOUT_MS = 2000
#: 读路径里懒过期的单次抢锁上限：必须远小于前端轮询间隔(1.5s)，
#: 否则"清理"会把列表接口本身拖慢。
EXPIRE_TIMEOUT_S = 0.4


def connect_sqlite(db_path: str, timeout_s: float | None = None,
                   busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    """打开主库连接（显式锁等待 + WAL + busy_timeout + synchronous=NORMAL）。

    事件/告警仓储与宏观数据点仓储共用。历史实现用裸 `sqlite3.connect()`：
    Python 默认只有 5s 锁等待，实测在主库被做T/量化写入者占用时会抛
    `sqlite3.OperationalError: database is locked`，告警列表接口直接 500
    （见 2026-09-18 backend.log traceback）。

    等待参数在调用点读取模块常量（不用默认参数），用例可 monkeypatch 成 0
    立刻复现锁竞争。
    """
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(
        db_path, timeout=CONNECT_TIMEOUT_S if timeout_s is None else timeout_s)
    conn.row_factory = sqlite3.Row
    busy = BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms
    with suppress(sqlite3.Error):  # journal_mode 需要写锁，失败不阻断本次连接
        conn.execute(f"PRAGMA busy_timeout={int(busy)}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_T = TypeVar("_T")


def is_locked_error(exc: BaseException) -> bool:
    """是否为 SQLite 锁竞争类错误（busy / locked）。"""
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc).lower() or "busy" in str(exc).lower())


def retry_on_locked(fn: Callable[[], _T], attempts: int = 3,
                    delay_s: float = 0.2) -> _T:
    """写路径的锁重试：并发写入者短暂持锁时不丢一次采集/告警入库。

    仅对锁竞争重试（其它 SQLite 错误立即上抛），最多 attempts 次，
    退避 delay_s 线性递增。busy_timeout 已经排过一轮队，这里是兜底。
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc) or attempt == attempts:
                raise
            time.sleep(delay_s * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


class EventSqliteStore:
    """SQLite连接与轻量schema迁移（进程内只执行一次DDL同步）。"""

    def __init__(self, db_path: str = "data/moss_finagent.db") -> None:
        self._db_path = db_path
        self._synced = False

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path)

    def _connect_fast(self) -> sqlite3.Connection:
        """交互式短超时连接：用户点击（已读等）不必为写锁排队 20s。"""
        return connect_sqlite(
            self._db_path, timeout_s=FAST_TIMEOUT_S,
            busy_timeout_ms=FAST_BUSY_TIMEOUT_MS)

    def _schema_sync(self) -> None:
        # DDL 需要写锁：用短超时连接重试（幂等 DDL），不让首个读请求为建表排队。
        retry_on_locked(self._schema_sync_locked)
        self._synced = True

    def _schema_sync_locked(self) -> None:
        with self._connect_fast() as conn:
            conn.executescript(_SCHEMA)
            event_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(fact_events)").fetchall()}
            if "analyzed" not in event_cols:  # 旧库轻量迁移
                conn.execute(
                    "ALTER TABLE fact_events "
                    "ADD COLUMN analyzed INTEGER NOT NULL DEFAULT 0")
            if "content_key" not in event_cols:
                conn.execute(
                    "ALTER TABLE fact_events ADD COLUMN content_key "
                    "TEXT NOT NULL DEFAULT ''")
            alert_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(fact_alerts)").fetchall()}
            if "content_key" not in alert_cols:
                conn.execute(
                    "ALTER TABLE fact_alerts ADD COLUMN content_key "
                    "TEXT NOT NULL DEFAULT ''")
            # content_key索引必须在旧库补列之后再建
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_content "
                "ON fact_events(tenant_id, content_key)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_content "
                "ON fact_alerts(tenant_id, content_key)")

    def _sync_once(self) -> None:
        """写入路径幂等同步：进程内只做一次DDL/迁移（S2：避免每写必PRAGMA）。"""
        if not self._synced:
            self._schema_sync()
