"""全球财经快讯多主备采集器：东财→同花顺→财联社→新浪。

四源独立拉取（某源阻断不影响其余），结果合并后由领域层跨源去重；
关键词预筛在采集器内完成（FR-4），单源最多保留 _PER_SOURCE_LIMIT 条。
阻塞akshare调用统一 to_thread；任何异常不抛出，转为错误条目。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.domain.alerts.keywords import is_relevant
from src.infrastructure.connectors.event_collectors.base import (
    iter_rows,
    make_raw_item,
)

logger = logging.getLogger(__name__)

_PER_SOURCE_LIMIT = 25  # 单源候选上限（4源合并后服务层再按总量截断）


def _row_get(row: dict, *names: str) -> str:
    for name in names:
        val = row.get(name)
        if val is not None and str(val) != "nan":
            return str(val).strip()
    return ""


def _map_em(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        mapping = row._asdict()
        items.append(make_raw_item(
            title=_row_get(mapping, "标题"),
            content=_row_get(mapping, "摘要", "内容"),
            source_name="东方财富-全球快讯",
            source_url=_row_get(mapping, "链接", "新闻链接"),
            publish_time=_row_get(mapping, "发布时间"),
            extra={"source_tag": "em"},
        ))
    return items


def _map_ths(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        mapping = row._asdict()
        items.append(make_raw_item(
            title=_row_get(mapping, "标题"),
            content=_row_get(mapping, "内容", "摘要"),
            source_name="同花顺-全球直播",
            source_url=_row_get(mapping, "链接"),
            publish_time=_row_get(mapping, "发布时间"),
            extra={"source_tag": "ths"},
        ))
    return items


def _map_cls(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        mapping = row._asdict()
        date = _row_get(mapping, "发布日期", "日期")
        tm = _row_get(mapping, "发布时间", "时间")
        items.append(make_raw_item(
            title=_row_get(mapping, "标题"),
            content=_row_get(mapping, "内容", "摘要"),
            source_name="财联社-电报",
            publish_time=f"{date} {tm}".strip(),
            extra={"source_tag": "cls"},
        ))
    return items


def _map_sina(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        mapping = row._asdict()
        items.append(make_raw_item(
            title="",  # 新浪无标题字段，normalizer从正文【】提要提取
            content=_row_get(mapping, "内容", "摘要"),
            source_name="新浪-7x24快讯",
            publish_time=_row_get(mapping, "时间", "发布时间"),
            extra={"source_tag": "sina"},
        ))
    return items


# (源标记, akshare函数名, 行映射器)
_SOURCE_SPECS: list[tuple[str, str, Callable[[Any], list[dict]]]] = [
    ("em", "stock_info_global_em", _map_em),
    ("ths", "stock_info_global_ths", _map_ths),
    ("cls", "stock_info_global_cls", _map_cls),
    ("sina", "stock_info_global_sina", _map_sina),
]


class NewsFlashCollector:
    """多主备全球财经快讯采集器。"""

    source_name = "news_flash"

    def __init__(self, akshare_module: Any | None = None) -> None:
        self._ak = akshare_module  # 测试可注入假akshare

    def _load_ak(self) -> Any:
        if self._ak is not None:
            return self._ak
        import akshare as ak  # 延迟导入：akshare为可选数据依赖

        self._ak = ak
        return ak

    def _fetch_source_sync(
        self, tag: str, fn_name: str, mapper: Callable[[Any], list[dict]]
    ) -> list[dict]:
        ak = self._load_ak()
        fn = getattr(ak, fn_name)
        df = fn(symbol="全部") if tag == "cls" else fn()
        return mapper(df)

    async def _fetch_source(
        self, tag: str, fn_name: str, mapper: Callable[[Any], list[dict]]
    ) -> tuple[list[dict], str | None]:
        try:
            items = await asyncio.to_thread(
                self._fetch_source_sync, tag, fn_name, mapper)
            return items, None
        except Exception as exc:  # noqa: BLE001 单源失败降级，不阻断其余源
            msg = f"快讯源 {tag} 采集失败: {brief(exc, BRIEF_TIGHT)}"
            logger.warning(msg)
            return [], msg

    async def collect(self) -> tuple[list[dict], list[str]]:
        """返回 (预筛后的原始条目, 各源错误)。"""
        kept: list[dict] = []
        errors: list[str] = []
        for tag, fn_name, mapper in _SOURCE_SPECS:
            items, err = await self._fetch_source(tag, fn_name, mapper)
            if err:
                errors.append(err)
                continue
            hits = [it for it in items if is_relevant(f"{it['title']}\n{it['content']}")]
            kept.extend(hits[:_PER_SOURCE_LIMIT])
        if not kept and errors:
            errors.append("全部快讯源无数据或全部被阻断")
        return kept, errors
