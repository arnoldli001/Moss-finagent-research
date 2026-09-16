"""事件/告警 SQLite 仓储共享件（schema、行映射、连接管理）。

仅被同目录仓储模块复用，不对领域层暴露；阻塞SQLite IO一律经
asyncio.to_thread 卸载，值参数全部使用 ? 占位。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

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


class EventSqliteStore:
    """SQLite连接与轻量schema迁移（进程内只执行一次DDL同步）。"""

    def __init__(self, db_path: str = "data/moss_finagent.db") -> None:
        self._db_path = db_path
        self._synced = False

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _schema_sync(self) -> None:
        with self._connect() as conn:
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
        self._synced = True

    def _sync_once(self) -> None:
        """写入路径幂等同步：进程内只做一次DDL/迁移（S2：避免每写必PRAGMA）。"""
        if not self._synced:
            self._schema_sync()
