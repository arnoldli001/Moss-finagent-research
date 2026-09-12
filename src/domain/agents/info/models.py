"""信息层共享数据模型（A05/A06/A07）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class InfoItem(BaseModel):
    """待核验的信息条目（新闻/公告/研报摘要）。

    text为必填正文；source_name参与来源可信度分级；
    publish_time支持ISO格式，无法解析时不做时效衰减。
    """

    item_id: str = ""
    """条目ID，缺省时由A05按序自动生成"""
    title: str = ""
    text: str = Field(min_length=1)
    source_name: str = ""
    source_url: str = ""
    publish_time: str = ""


def normalize_direction(value: str) -> str:
    """方向归一化到 positive/negative/neutral 三态。"""
    v = (value or "").strip().lower()
    if v in ("positive", "pos", "利好", "正面", "利好/正面"):
        return "positive"
    if v in ("negative", "neg", "利空", "负面", "利空/负面"):
        return "negative"
    return "neutral"


EVENT_TYPES = (
    "earnings", "merger", "policy", "product",
    "management", "litigation", "financing", "other",
)


def normalize_event_type(value: str) -> str:
    """事件类型归一化到白名单，未知归other。"""
    v = (value or "").strip().lower()
    return v if v in EVENT_TYPES else "other"
