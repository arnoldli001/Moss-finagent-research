"""去重/冷却纯函数测试（T3，FR-3）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.alerts.dedup import (
    alert_dedup_key,
    dedupe_events,
    is_in_cooldown,
)
from src.domain.alerts.models import Event, EventType


def _event(key: str, source: str = "s", title: str = "t") -> Event:
    return Event(event_id=f"evt_{key}", event_key=key, event_type=EventType.SECTOR,
                 title=title, source_name=source, fetch_time="2026-09-14T10:00:00")


def test_alert_dedup_key_differs_by_type():
    assert alert_dedup_key("k1", "risk") == "k1:risk"
    assert alert_dedup_key("k1", "opportunity") != "k1:risk"


def test_cooldown_boundaries():
    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 9, 14, 17, 30, tzinfo=tz)
    last = (now - timedelta(hours=23, minutes=59)).isoformat()
    assert is_in_cooldown(last, now=now, hours=24) is True
    last25 = (now - timedelta(hours=25)).isoformat()
    assert is_in_cooldown(last25, now=now, hours=24) is False


def test_cooldown_bad_time_means_not_in_cooldown():
    assert is_in_cooldown(None) is False
    assert is_in_cooldown("") is False
    assert is_in_cooldown("未知") is False


def test_dedupe_events_preserves_first():
    e1, e2 = _event("dup", source="s1"), _event("dup", source="s2")
    e3 = _event("other")
    out = dedupe_events([e1, e2, e3])
    assert [e.event_key for e in out] == ["dup", "other"]
    assert out[0].source_name == "s1"
