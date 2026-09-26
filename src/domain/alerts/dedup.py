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


def _aware(dt: datetime | None) -> datetime | None:
    """naive/混格式统一成本地时区（时间比较的前提）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.now(timezone.utc).astimezone().tzinfo)
    return dt


def event_recency_key(event: Event) -> tuple[datetime, int]:
    """事件"新旧"排序键：`(有效时间, 是否真新闻)`。

    两个调用方共用这一份规则：`AlertScanService._merge_backlog`（本轮候选排序）
    与 `EventSqliteRepository._list_unanalyzed_sync`（积压队列出队顺序）。

    ## ★ 未来时间必须收敛到抓取时间（2026-09-24 实测：告警页永远 0 条的真因）

    「百度财经-财报披露日程」这类**日程型**事件，`publish_time` 写的是**披露日**
    （实测库里全是 09-28 / 09-29 / 09-30 / 10-01，**都在未来**），却是每天批量入库的。
    按 publish_time 倒序截候选，这批"未来日程"**永远压在今天刚发生的快讯前面**，
    配合 `alert_scan_candidate_limit`（默认 30）形成饥饿 —— 实测 dev 库：

    - 87 条已分析事件，**来源 100% 是"财报披露日程"**，publish_time 全在未来；
    - 同一时刻 184 条当天的 policy/sector 快讯（东财/财联社/同花顺）**一条都没轮到**。

    于是扫描天天 `success`、`扫描176 新增22 告警0`，`fact_alerts` 恒为 0 ——
    看起来像"LLM 不打分"，实际是**候选池被日程型事件占满**。

    两条规则：
    1. `publish_time` 在未来时按 `fetch_time`（"什么时候看到的"）参与排序；
    2. 同刻时**真新闻优先于未来日程**（序号 1 > 0）：日程只是"还没发生的预告"，
       不该和"刚刚发生的快讯"抢同一个 LLM 候选位。
    """
    publish = _aware(_parse_dt(event.publish_time))
    fetched = _aware(_parse_dt(event.fetch_time))
    future = publish is not None and publish > datetime.now(timezone.utc)
    if publish is None:
        effective = fetched or datetime.min.replace(tzinfo=timezone.utc)
    elif future:
        effective = fetched or publish
    else:
        effective = publish
    return effective, 0 if future else 1


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
