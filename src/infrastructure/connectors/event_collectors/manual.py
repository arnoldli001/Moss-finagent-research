"""手工事件采集器：API导入的兜底/数据源（FR-10）。"""

from __future__ import annotations

from src.infrastructure.connectors.event_collectors.base import make_raw_item

_VALID_TYPE_HINTS = {"policy", "sector", "stock", "calendar", ""}


class ManualEventCollector:
    """把 POST /events/import 的条目转为标准原始条目。"""

    source_name = "manual_import"

    def __init__(self, items: list[dict] | None = None) -> None:
        self._items = items or []

    async def collect(self) -> tuple[list[dict], list[str]]:
        out: list[dict] = []
        for raw in self._items:
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            type_hint = str(raw.get("event_type") or raw.get("type_hint") or "")
            if type_hint not in _VALID_TYPE_HINTS:
                type_hint = ""
            out.append(make_raw_item(
                title=title[:200],
                content=str(raw.get("content") or "")[:2000],
                source_name=str(raw.get("source_name") or self.source_name)[:60],
                source_url=str(raw.get("source_url") or "")[:500],
                publish_time=str(raw.get("publish_time") or "")[:40],
                type_hint=type_hint,
                extra={"source_tag": "manual"},
            ))
        return out, []
