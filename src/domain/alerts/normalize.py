"""原始采集条目 → 标准化 Event（纯函数，无外部IO）。

event_id/event_key 确定性生成：source_name + 规范化标题 + 发布日期，
保证跨扫描时同源事件幂等（FR-3）；另生成不含来源的 content_key，
供跨源同文事件的告警抑制（G3防刷屏）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from src.domain.alerts.keywords import (
    find_stock_codes,
    has_policy_keyword,
    has_sector_keyword,
)
from src.domain.alerts.models import DEFAULT_TENANT, Event, EventEntities, EventType

_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
# 标题归一：去空白与常见标点/来源符号（保留中英文数字与行业语义）
_STRIP_CHARS = " \t\r\n　【】[]（）()《》<>“”\"'‘’：:，,。.！!？?；;、—-~～|·"


def _strip_title(text: str) -> str:
    text = (text or "").strip()
    for ch in _STRIP_CHARS:
        text = text.replace(ch, "")
    return text.lower()


def extract_date(publish_time: str) -> str:
    """从容错时间串中提取 YYYY-MM-DD；无法解析返回空串。"""
    m = _DATE_RE.search(publish_time or "")
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def make_event_key(source_name: str, title: str, date_part: str) -> str:
    raw = f"{source_name.strip()}|{_strip_title(title)}|{date_part}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def make_content_key(title: str, date_part: str) -> str:
    """跨源内容键（不含来源名）：异源转载同一稿件时保持一致（G3）。"""
    raw = f"{_strip_title(title)}|{date_part}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def safe_source_url(url: str) -> str:
    """仅放行 http/https 链接，拦截 javascript:/data: 等注入面（D12）。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        scheme = urlsplit(text).scheme.lower()
    except ValueError:
        return ""
    return text if scheme in ("http", "https") else ""


def classify_event(title: str, content: str, type_hint: str = "") -> EventType:
    """事件类型判定：显式hint优先，其次政策>个股>板块。"""
    hint = (type_hint or "").strip().lower()
    if hint in (t.value for t in EventType):
        return EventType(hint)
    text = f"{title}\n{content}"
    if has_policy_keyword(text):
        return EventType.POLICY
    if find_stock_codes(title) or find_stock_codes(content):
        return EventType.STOCK
    if has_sector_keyword(text):
        return EventType.SECTOR
    return EventType.SECTOR  # 日历以外且无任何命中的条目理论上已被预筛过滤


def _clean_title(raw_title: str, content: str) -> str:
    title = (raw_title or "").strip()
    if title:
        return title[:200]
    # 新浪7x24无标题，取正文【...】中的提要，否则截断正文
    m = re.match(r"^【([^】]{4,80})】", content)
    if m:
        return m.group(1).strip()[:200]
    return content.strip()[:80]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_event(raw: dict, fetch_time: str | None = None) -> Event | None:
    """原始条目→Event；标题与正文均空时返回None（不可用条目）。"""
    source_name = str(raw.get("source_name") or "unknown")[:60]
    publish_time = str(raw.get("publish_time") or "")[:40]
    content = str(raw.get("content") or "").strip()[:2000]
    title = _clean_title(str(raw.get("title") or ""), content)
    if not title and not content:
        return None
    date_part = extract_date(publish_time) or extract_date(fetch_time or "")
    key = make_event_key(source_name, title, date_part)
    content_key = make_content_key(title, date_part)
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    entities = EventEntities(
        industries=list(extra.get("industries") or []),
        companies=list(extra.get("companies") or []),
        regions=list(extra.get("regions") or []),
    )
    return Event(
        event_id=f"evt_{key}",
        event_key=key,
        content_key=content_key,
        event_type=classify_event(title, content, str(raw.get("type_hint") or "")),
        title=title,
        content=content,
        source_name=source_name,
        source_url=safe_source_url(str(raw.get("source_url") or "")[:500]),
        publish_time=publish_time,
        fetch_time=(fetch_time or now_iso())[:40],
        entities=entities,
        raw_data={
            "source_tag": str(extra.get("source_tag") or ""),
            "importance": extra.get("importance"),
        },
        tenant_id=str(raw.get("tenant_id") or DEFAULT_TENANT),
    )


def normalize_events(items: list[dict], fetch_time: str | None = None) -> list[Event]:
    """批量标准化，丢弃不可用条目。"""
    ts = fetch_time or now_iso()
    out: list[Event] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        event = normalize_event(raw, ts)
        if event is not None:
            out.append(event)
    return out
