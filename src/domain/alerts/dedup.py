"""事件/告警去重与冷却纯函数（FR-3/G3，防刷屏）。

- 事件幂等：event_key = 来源+规范标题+日期（normalize中生成，各自保留溯源）
- 同源告警冷却：alert_key = event_key:alert_type，24小时窗内只产生一次
- 跨源告警抑制：content_alert_key = content_key:alert_type，异源同文只告一次
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.alerts.models import Event


def alert_dedup_key(event_key: str, alert_type: str) -> str:
    return f"{event_key}:{alert_type}"


def alert_content_key(content_key: str, alert_type: str) -> str:
    """跨源同文内容的告警抑制键；content_key为空时返回空串（不参与抑制）。"""
    return f"{content_key}:{alert_type}" if content_key else ""


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def is_in_cooldown(
    last_trigger_time: str | datetime | None,
    now: datetime | None = None,
    hours: int = 24,
) -> bool:
    """上次触发时间在 hours 小时内 → True（冷却中）；无法解析时间→ False。"""
    last = _parse_dt(last_trigger_time)
    if last is None:
        return False
    current = now or datetime.now(timezone.utc).astimezone()
    if last.tzinfo is None:
        last = last.replace(tzinfo=current.tzinfo)
    return current - last < timedelta(hours=hours)


def dedupe_events(events: list[Event]) -> list[Event]:
    """按 event_key 保序去重，保留首次出现的来源。"""
    seen: dict[str, None] = {}
    out: list[Event] = []
    for event in events:
        if event.event_key in seen:
            continue
        seen[event.event_key] = None
        out.append(event)
    return out
