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


def _to_info_item(row: dict[str, Any], code: str) -> dict:
    """私有直连新闻源的条目 → 本模块的 info_items 契约。

    两边字段名不同（对方是接口原样：`media`/`url`/`summary`），在这里一次性
    映射好，消费方（做T「消息面情绪」、投研信息层）不用改。
    """
    title = str(row.get("title") or "").strip()
    summary = str(row.get("summary") or "").strip()
    return {
        "text": f"{title}\n{summary}".strip()[:2000],
        "title": title[:200],
        "source_name": str(row.get("media") or "东方财富")[:40],
        "source_url": str(row.get("url") or ""),
        "publish_time": str(row.get("publish_time") or "")[:32],
        "stock_code": code,
    }


async def _local_news_fallback(code: str, limit: int) -> list[dict]:
    """akshare 取不到时，退到本地私有直连新闻源（公开仓库无此模块 → 返回空）。

    ## 为什么需要这一步

    `ak.stock_news_em()` 在本机**稳定取不到数据**（pyarrow 解析响应时抛
    `ArrowInvalid: Invalid regular expression: invalid escape sequence: \\u`，
    详见 `src/quant/stock_news.py` 的模块说明）。而做T「消息面情绪」与投研
    信息层都依赖个股新闻，不兜底就整块恒为空 —— 界面上表现为"近期无消息"，
    看不出是取数坏了。

    私有直连源打的是**同一个东财接口**，只是自己解析 JSON，本地实测可用。

    ## 为什么把导入写在函数里

    `src/quant/stock_news.py` 属私有资产、不进公开仓库。公开 checkout 里
    本文件必须照常可导入，所以只能 `except ImportError` 静默降级 ——
    与 `src/intraday/daily.py` 对 `niuline` 的处理是同一套路。
    """
    try:
        from src.quant.stock_news import fetch_many
    except ImportError:  # 公开版：无私有直连源，保持原行为（返回空列表）
        return []
    try:
        got = await fetch_many([code], limit=limit)
    except Exception as exc:  # noqa: BLE001 兜底失败也不能阻断调用方
        logger.debug("私有直连新闻源兜底失败(%s): %s", code, brief(exc, BRIEF_DEFAULT))
        return []
    payload = got.get(code)
    if payload is None and len(got) == 1:  # 代码被规范化过时按唯一键取
        payload = next(iter(got.values()))
    rows = (payload or {}).get("items") or []
    return [_to_info_item(row, code) for row in rows][:limit]


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


class LocalFallbackNewsFetcher:
    """组合新闻源：先走公开的 akshare 实现，取不到再退本地私有直连源。

    ## 为什么兜底放在这里，而不是改 `AkshareNewsFetcher.fetch_news`

    那个类的契约是"akshare 失败返回空列表"（`tests/unit/test_news_fetcher.py`
    的 `test_fetcher_failure_returns_empty` 有断言），是公开行为，不该为了本地
    开发环境去改。所以把兜底做成**装配层**的组合，由 `src/api/runtime.py` 选用：

      - 本地完整版：akshare 空 → 退私有直连源 → 做T「消息面情绪」/投研信息层
        拿得到新闻；
      - 公开仓库：私有源不存在 → `_local_news_fallback` 返回空 →
        与直接用 `AkshareNewsFetcher` 行为**完全一致**。

    这样公开行为与公开测试都不受影响，本地功能又是好的。
    """

    def __init__(self, primary: NewsFetcher | None = None) -> None:
        self._primary: NewsFetcher = (
            primary if primary is not None else AkshareNewsFetcher())

    async def fetch_news(self, code: str, limit: int = NEWS_LIMIT) -> list[dict]:
        items = await self._primary.fetch_news(code, limit=limit)
        if items:
            return items
        return await _local_news_fallback(code, limit)

    async def fetch_topic_news(
        self, keywords: list[str], limit: int = TOPIC_NEWS_LIMIT
    ) -> list[dict]:
        # 私有源只有个股新闻、没有全球财经快讯，这条保持原样（不做兜底）。
        return await self._primary.fetch_topic_news(keywords, limit=limit)
