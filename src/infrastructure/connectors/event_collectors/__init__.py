"""事件采集器包：快讯多源/投资日历/手工导入。"""

from src.infrastructure.connectors.event_collectors.base import (
    BaseEventCollector,
    make_raw_item,
)
from src.infrastructure.connectors.event_collectors.calendar_events import (
    CalendarCollector,
)
from src.infrastructure.connectors.event_collectors.manual import ManualEventCollector
from src.infrastructure.connectors.event_collectors.news_flash import (
    NewsFlashCollector,
)

__all__ = [
    "BaseEventCollector",
    "CalendarCollector",
    "ManualEventCollector",
    "NewsFlashCollector",
    "make_raw_item",
]
