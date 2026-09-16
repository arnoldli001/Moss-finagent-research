"""事件/告警SQLite仓储测试：临时库+幂等+过滤+已读流（T4）。"""

from __future__ import annotations

import os

import pytest

from src.domain.alerts.models import (
    AffectedStock,
    Alert,
    AlertLevel,
    AlertStatus,
    AlertType,
    Event,
    EventEntities,
    EventType,
)
from src.infrastructure.repositories.event_sqlite_repo import EventSqliteRepository


@pytest.fixture
async def repo(tmp_path):
    db = os.path.join(str(tmp_path), "test_events.db")
    r = EventSqliteRepository(db)
    await r.ensure_schema()
    yield r
    await r.close()


def _event(key: str, etype: EventType = EventType.POLICY,
           source: str = "测试源", url: str = "https://src/1") -> Event:
    return Event(
        event_id=f"evt_{key}", event_key=key, event_type=etype,
        title=f"事件-{key}", content="正文", source_name=source,
        source_url=url, publish_time="2026-09-14 08:00:00",
        fetch_time="2026-09-14T17:30:00+08:00",
        entities=EventEntities(industries=["固态电池"], companies=["示例公司"]),
        raw_data={"source_tag": "em", "importance": 3},
    )


def _alert(key: str, atype: AlertType = AlertType.RISK,
           level: AlertLevel = AlertLevel.HIGH) -> Alert:
    return Alert(
        alert_id=f"al_{key}", alert_key=key, event_id="evt_p1",
        alert_type=atype, alert_level=level, title="政策风险告警",
        description="描述", risk_score=80.0, opportunity_score=10.0,
        confidence=0.9,
        affected_stocks=[AffectedStock(code="300308", name="示例科技",
                                       impact="negative", reason="估值承压")],
        affected_industries=["光模块"], impact_path="政策→订单→业绩",
        source_name="测试源", source_url="https://src/1",
        event_publish_time="2026-09-14 08:00:00",
        trigger_time="2026-09-14T17:30:00+08:00",
        expire_time="2026-09-21T17:30:00+08:00",
    )


@pytest.mark.asyncio
async def test_events_idempotent_by_event_key(repo):
    r1 = await repo.upsert_events([_event("p1")])
    assert r1 == {"inserted": 1, "skipped": 0}
    # 换source_url的同键事件仍跳过
    dup = _event("p1", url="https://src/2")
    r2 = await repo.upsert_events([dup])
    assert r2 == {"inserted": 0, "skipped": 1}
    rows = await repo.list_events()
    assert len(rows) == 1 and rows[0].source_url == "https://src/1"
    assert rows[0].entities.industries == ["固态电池"]
    assert rows[0].raw_data["source_tag"] == "em"


@pytest.mark.asyncio
async def test_list_events_type_filter(repo):
    await repo.upsert_events([_event("p1", EventType.POLICY),
                              _event("s1", EventType.SECTOR),
                              _event("c1", EventType.CALENDAR)])
    only_policy = await repo.list_events(event_type="policy")
    assert {e.event_key for e in only_policy} == {"p1"}
    assert len(await repo.list_events(limit=2)) == 2


@pytest.mark.asyncio
async def test_alerts_idempotent_and_filters(repo):
    await repo.upsert_alerts([
        _alert("p1:risk", AlertType.RISK, AlertLevel.HIGH),
        _alert("p1:opportunity", AlertType.OPPORTUNITY, AlertLevel.MEDIUM),
        _alert("p2:risk", AlertType.RISK, AlertLevel.LOW),
    ])
    again = await repo.upsert_alerts([_alert("p1:risk")])
    assert again["inserted"] == 0
    highs = await repo.list_alerts(alert_level="high")
    assert [a.alert_key for a in highs] == ["p1:risk"]
    opps = await repo.list_alerts(alert_type="opportunity")
    assert len(opps) == 1 and opps[0].affected_stocks[0].code == "300308"


@pytest.mark.asyncio
async def test_get_alert_tenant_isolation(repo):
    await repo.upsert_alerts([_alert("p1:risk")])
    assert await repo.get_alert("al_p1:risk") is not None
    assert await repo.get_alert("al_p1:risk", tenant_id="other") is None


@pytest.mark.asyncio
async def test_unread_count_and_mark_read_flow(repo):
    await repo.upsert_alerts([
        _alert("p1:risk"),
        _alert("p1:opportunity", AlertType.OPPORTUNITY, AlertLevel.MEDIUM),
    ])
    assert await repo.count_unread() == 2
    assert await repo.mark_read("al_p1:risk") is True
    assert await repo.mark_read("al_p1:risk") is False  # 幂等
    assert await repo.count_unread() == 1
    read_alerts = await repo.list_alerts(status="read")
    assert [a.status for a in read_alerts] == [AlertStatus.READ]
    n = await repo.mark_all_read()
    assert n == 1 and await repo.count_unread() == 0


@pytest.mark.asyncio
async def test_existing_event_keys_membership(repo):
    await repo.upsert_events([_event("p1"), _event("s1", EventType.SECTOR)])
    known = await repo.existing_event_keys(["p1", "s1", "c1"])
    assert known == {"p1", "s1"}
    assert await repo.existing_event_keys([]) == set()


@pytest.mark.asyncio
async def test_last_alert_time_for_cooldown(repo):
    assert await repo.last_alert_time("p1:risk") is None
    await repo.upsert_alerts([_alert("p1:risk")])
    assert await repo.last_alert_time("p1:risk") == "2026-09-14T17:30:00+08:00"


@pytest.mark.asyncio
async def test_unanalyzed_backlog_lifecycle(repo):
    """新事件默认未分析；mark后从积压队列消失（支撑手工导入闭环）。"""
    no_pub = _event("imported", EventType.POLICY, url="https://src/3")
    no_pub.publish_time = ""
    no_pub.fetch_time = "2026-09-15T10:00:00+08:00"  # 手工导入无发布时间
    await repo.upsert_events([
        _event("old", EventType.SECTOR),
        _event("new", EventType.POLICY, url="https://src/2"),
        no_pub])
    backlog = await repo.list_unanalyzed_events(limit=10)
    keys = {e.event_key for e in backlog}
    assert keys == {"old", "new", "imported"}  # 空发布时间事件不丢失
    assert backlog[0].event_key == "imported"  # fetch_time最新排首位
    changed = await repo.mark_events_analyzed(["evt_new", "evt_imported"])
    assert changed == 2
    backlog = await repo.list_unanalyzed_events(limit=10)
    assert [e.event_key for e in backlog] == ["old"]
    assert await repo.mark_events_analyzed([]) == 0


@pytest.mark.asyncio
async def test_event_alert_provenance_joinable(repo):
    """G2：告警经event_id可联查事件溯源四要素。"""
    await repo.upsert_events([_event("p1")])
    await repo.upsert_alerts([_alert("p1:risk")])
    alert = (await repo.list_alerts(alert_level="high"))[0]
    events = await repo.list_events(event_type="policy")
    event = next(e for e in events if e.event_id == alert.event_id)
    assert event.source_name and event.source_url
    assert event.publish_time and event.fetch_time


def _alert_expired(key: str) -> Alert:
    alert = _alert(key)
    alert.trigger_time = "2000-01-01T17:30:00+08:00"
    alert.expire_time = "2000-01-08T17:30:00+08:00"
    return alert


@pytest.mark.asyncio
async def test_expired_alerts_lazy_hidden_and_not_unread(repo):
    """D1/FR-8：到期告警懒置expired，默认列表隐藏、不计未读、可显式查询。"""
    await repo.upsert_alerts([
        _alert_expired("old:risk"),
        _alert("p1:opportunity", AlertType.OPPORTUNITY, AlertLevel.MEDIUM),
    ])
    # 默认列表不含过期
    visible = await repo.list_alerts()
    assert [a.alert_key for a in visible] == ["p1:opportunity"]
    # 未读红点不含过期
    assert await repo.count_unread() == 1
    # 显式查过期：状态已懒迁移
    expired = await repo.list_alerts(status="expired")
    assert len(expired) == 1
    assert expired[0].status == AlertStatus.EXPIRED
    # include_expired 返回全部
    both = await repo.list_alerts(include_expired=True)
    assert {a.alert_key for a in both} == {"old:risk", "p1:opportunity"}
    # 仅未读过滤不含过期；全部已读不影响过期行
    active = await repo.list_alerts(status="active")
    assert [a.alert_key for a in active] == ["p1:opportunity"]
    assert await repo.mark_all_read() == 1
    # 详情查询触发懒过期后可见历史告警
    detail = await repo.get_alert("al_old:risk")
    assert detail is not None and detail.status == AlertStatus.EXPIRED
    assert await repo.mark_read("al_old:risk") is False  # 过期不可再已读


@pytest.mark.asyncio
async def test_tenant_isolation_on_all_query_methods(repo):
    """D4：existing/mark/last_alert/last_content 全部按租户隔离。"""
    other = "tenant_other"
    await repo.upsert_events([_event("p1")])
    assert await repo.existing_event_keys(["p1"], other) == set()
    assert await repo.existing_event_keys(["p1"]) == {"p1"}
    assert await repo.mark_events_analyzed(["evt_p1"], other) == 0
    backlog = await repo.list_unanalyzed_events(limit=10, tenant_id=other)
    assert backlog == []

    a1 = _alert("p1:risk")
    a1.content_key = "ck1:risk"
    await repo.upsert_alerts([a1])
    assert await repo.last_alert_time("p1:risk", other) is None
    assert await repo.last_alert_time("p1:risk") == "2026-09-14T17:30:00+08:00"
    assert await repo.last_content_alert_time("ck1:risk", other) is None
    assert (await repo.last_content_alert_time("ck1:risk")
            == "2026-09-14T17:30:00+08:00")
    assert await repo.last_content_alert_time("") is None
    assert await repo.count_unread(other) == 0
    assert await repo.get_alert("al_p1:risk", other) is None


@pytest.mark.asyncio
async def test_cross_source_content_key_roundtrip(repo):
    """D2：两条不同alert_key但同content_key的告警可被抑制查询命中。"""
    a1 = _alert("src1:risk")
    a1.content_key = "same:risk"
    a2 = _alert("src2:risk")
    a2.content_key = "same:risk"
    await repo.upsert_alerts([a1, a2])
    assert await repo.last_content_alert_time("same:risk") is not None


@pytest.mark.asyncio
async def test_legacy_db_migration_adds_content_key(tmp_path):
    """D10：旧库（无content_key列）启动时自动ALTER迁移且数据不丢。"""
    import sqlite3

    db = os.path.join(str(tmp_path), "legacy.db")
    conn = sqlite3.connect(db)
    # 模拟整改前schema：无content_key
    conn.executescript(
        """
        CREATE TABLE fact_events (
            event_id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL, title TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            publish_time TEXT NOT NULL DEFAULT '',
            fetch_time TEXT NOT NULL, entities_json TEXT NOT NULL DEFAULT '{}',
            raw_data_json TEXT NOT NULL DEFAULT '{}',
            analyzed INTEGER NOT NULL DEFAULT 0, tenant_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE fact_alerts (
            alert_id TEXT PRIMARY KEY, alert_key TEXT NOT NULL UNIQUE,
            event_id TEXT NOT NULL, alert_type TEXT NOT NULL,
            alert_level TEXT NOT NULL, title TEXT NOT NULL,
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
            trigger_time TEXT NOT NULL, expire_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', tenant_id TEXT NOT NULL,
            disclaimer TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '');
        INSERT INTO fact_events (event_id, event_key, event_type, title,
            source_name, fetch_time, tenant_id)
            VALUES ('evt_old','old','policy','旧事件','旧源','t','tenant_001');
        """)
    conn.commit()
    conn.close()

    migrated = EventSqliteRepository(db)
    await migrated.ensure_schema()  # 不应抛no such column
    events = await migrated.list_events()
    assert len(events) == 1 and events[0].content_key == ""
    # 迁移后写入带content_key的新告警可正常读回
    alert = _alert("old:risk")
    alert.content_key = "ck:risk"
    await migrated.upsert_alerts([alert])
    assert await migrated.last_content_alert_time("ck:risk") is not None
    await migrated.close()
