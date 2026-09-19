"""新闻抓取器：AkShare → 信息层info_items。

两类来源：
1. 个股新闻 stock_news_em（按6位代码，标题/正文/来源/链接齐全）
2. 全球财经快讯 stock_info_global_em（最近约200条，按主题关键词过滤），
   为宏观（美联储/加息/美股）与行业（AI产业链细分环节）问题提供真实信息素材，
   使A05-A07信息层与分析Agent能基于近期事实回答非结构化提问。

输出契约：[{text, source_name, publish_time, title?, source_url?}]。
akshare为可选依赖，缺失/失败时返回空列表（增强链路，不阻断研究主流程）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.core.exceptions import DataFetchError

logger = logging.getLogger(__name__)

NEWS_LIMIT = 10
TOPIC_NEWS_LIMIT = 15


class NewsFetcher(Protocol):
    async def fetch_news(self, code: str, limit: int = NEWS_LIMIT) -> list[dict]: ...

    async def fetch_topic_news(
        self, keywords: list[str], limit: int = TOPIC_NEWS_LIMIT
    ) -> list[dict]: ...


def news_df_to_items(df: Any, limit: int = NEWS_LIMIT) -> list[dict]:
    """东财个股新闻DataFrame → info_items（纯函数，便于单测）。"""
    if df is None or len(df) == 0:
        return []
    items: list[dict] = []
    for row in df.head(limit).itertuples(index=False):
        mapping = row._asdict() if hasattr(row, "_asdict") else dict(
            zip(df.columns, row, strict=True))
        title = str(mapping.get("新闻标题") or "")
        content = str(mapping.get("新闻内容") or "")
        text = f"{title}\n{content}".strip()
        if not text:
            continue
        items.append({
            "text": text[:2000],
            "title": title[:200],
            "source_name": str(mapping.get("文章来源") or "东方财富"),
            "source_url": str(mapping.get("新闻链接") or ""),
            "publish_time": str(mapping.get("发布时间") or ""),
            "stock_code": str(mapping.get("关键词") or ""),
        })
    return items


def topic_df_to_items(
    df: Any, keywords: list[str], limit: int = TOPIC_NEWS_LIMIT
) -> list[dict]:
    """东财全球财经快讯 → 关键词命中过滤 → info_items（纯函数）。

    标题或摘要命中任一关键词即保留；大小写不敏感；按DataFrame原有时间顺序。
    """
    if df is None or len(df) == 0 or not keywords:
        return []
    kws = [k.lower() for k in keywords if k]
    items: list[dict] = []
    for row in df.itertuples(index=False):
        if len(items) >= limit:
            break
        mapping = row._asdict() if hasattr(row, "_asdict") else dict(
            zip(df.columns, row, strict=True))
        title = str(mapping.get("标题") or "")
        summary = str(mapping.get("摘要") or mapping.get("内容") or "")
        haystack = f"{title}\n{summary}".lower()
        if not any(k in haystack for k in kws):
            continue
        text = f"{title}\n{summary}".strip()
        if not text:
            continue
        items.append({
            "text": text[:2000],
            "title": title[:200],
            "source_name": "东方财富-全球财经",
            "source_url": str(mapping.get("链接") or ""),
            "publish_time": str(mapping.get("发布时间") or ""),
            "matched_keywords": ",".join(
                sorted({k for k in keywords if k.lower() in haystack})),
        })
    return items


class AkshareNewsFetcher:
    """个股新闻 + 全球财经主题新闻（阻塞库，统一线程池化）。"""

    def __init__(self) -> None:
        self._ak = None

    def _load(self) -> Any:
        if self._ak is None:
            try:
                import akshare as ak
            except ImportError as exc:
                raise DataFetchError(
                    "akshare未安装，请执行: uv sync --extra data") from exc
            self._ak = ak
        return self._ak

    def _fetch_sync(self, code: str, limit: int) -> list[dict]:
        ak = self._load()
        df = ak.stock_news_em(symbol=code)
        return news_df_to_items(df, limit)

    def _fetch_topic_sync(self, keywords: list[str], limit: int) -> list[dict]:
        ak = self._load()
        df = ak.stock_info_global_em()
        return topic_df_to_items(df, keywords, limit)

    async def fetch_news(self, code: str, limit: int = NEWS_LIMIT) -> list[dict]:
        try:
            return await asyncio.to_thread(self._fetch_sync, code, limit)
        except Exception as exc:  # noqa: BLE001 新闻是增强链路，失败不阻断研究主流程
            logger.warning("个股新闻拉取失败(%s): %s", code, brief(exc, BRIEF_DEFAULT))
            return []

    async def fetch_topic_news(
        self, keywords: list[str], limit: int = TOPIC_NEWS_LIMIT
    ) -> list[dict]:
        try:
            return await asyncio.to_thread(self._fetch_topic_sync, keywords, limit)
        except Exception as exc:  # noqa: BLE001 同上，失败不阻断
            logger.warning("主题新闻拉取失败(%s): %s", keywords[:5], brief(exc, BRIEF_DEFAULT))
            return []
