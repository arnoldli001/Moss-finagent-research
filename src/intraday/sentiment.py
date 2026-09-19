"""消息面NLP情绪打分（DeepSeek/本地模型经统一LLM网关）。

实现方式（对应用户需求「消息面」维度）：
  1. 取近24小时个股新闻（复用项目既有 AkshareNewsFetcher.stock_news_em）；
  2. 一次性喂给 LLM，要求输出 -1~1 的情绪分 + 偏多/偏空计数 + 逐条极性；
  3. 情绪分与计数分在 factors.news_score_from 中融合为 [-1,1] 因子得分。

工程约束（与项目既有约定保持一致）：
  - 只经 LLMGateway 调用（自带语义缓存、熔断降级、审计与token预算）；
  - 任务层级用 medium（本地 qwen3:8b 主、deepseek-flash 备），新闻打分是分类任务，
    本地模型足够且零token费，云端不可达时仍可用；
  - LLM 返回分数越界（非 [-1,1]）时不静默钳制，而是判为不可用并记录缺口，
    自动降级为「纯计数口径」打分（与 domain/alerts 的 ScoreOutOfRange 约定一致）；
  - LLM 或新闻源任一不可用都不阻断主流程，只在 news.gap 中如实说明。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import AgentExecutionError
from src.domain.agents.analysis.base import parse_llm_json
from src.intraday.models import NewsItem, NewsSentiment

logger = logging.getLogger(__name__)

AGENT_ID = "intraday_news_sentiment"
TASK_TIER = "medium"

SYSTEM_PROMPT = (
    "你是A股短线交易的消息面分析师。你的任务是判断新闻对【做T短线交易】的方向性影响，"
    "而不是长期投资价值。只输出JSON，不要任何解释文字。"
)

_PROMPT_TEMPLATE = """分析以下与 {name}({code}) 相关的新闻对该股票**短线（当日~3日）**的影响。

要求：
1. polarity：逐条判定 positive(偏多) / negative(偏空) / neutral(中性)；
2. positive / negative / neutral：三类新闻的条数；
3. score：综合情绪得分，浮点数，范围 -1.0（极度偏空）~ 1.0（极度偏多），0 为中性；
   注意：已被市场充分预期的利好应降低分值；纯公告类无方向信息记 0；
4. summary：一句话说明主导逻辑（不超过60字，简体中文）。

只输出如下JSON（不要markdown围栏）：
{{"items":[{{"i":0,"polarity":"positive"}}],"positive":0,"negative":0,"neutral":0,"score":0.0,"summary":""}}

新闻列表：
{news}
"""


def _fingerprint_of_raw(raw_items: list[dict[str, Any]] | None) -> str:
    """新闻原始条目 → 内容指纹（标题+发布时间；顺序敏感，新条目出现即变）。

    只取"决定 prompt 内容"的字段：标题与发布时间。同一批新闻反复抓到的顺序稳定，
    因此指纹稳定 → 可以安全地复用上次的 LLM 打分。
    """
    if not raw_items:
        return ""
    parts = [
        f"{str(item.get('publish_time') or '')[:16]}|{str(item.get('title') or '')[:120]}"
        for item in raw_items
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _fingerprint_of(result: NewsSentiment) -> str:
    """已分析结果 → 指纹（用于回填缓存，保证下次能比对上）。"""
    if not result.items:
        return ""
    parts = [
        f"{str(item.publish_time or '')[:16]}|{str(item.title or '')[:120]}"
        for item in result.items
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def build_news_block(items: list[dict[str, Any]], limit: int,
                     text_chars: int = 180) -> str:
    """新闻条目 → prompt 文本块（序号 + 标题 + 摘要截断）。

    正文截断长度直接影响推理模型的思维链长度：喂得越长，模型思考越久，
    越容易把 max_tokens 全耗在思维链上而正文为空（实测 qwen3:8b 有此问题）。
    """
    lines: list[str] = []
    for index, item in enumerate(items[:limit]):
        title = str(item.get("title") or "").strip()[:120]
        text = str(item.get("text") or "").strip().replace("\n", " ")[:text_chars]
        published = str(item.get("publish_time") or "").strip()[:16]
        source = str(item.get("source_name") or "").strip()[:20]
        lines.append(
            f"[{index}] {published} {source} | {title}\n    {text}")
    return "\n".join(lines) if lines else "（无新闻）"


# 重试时追加的严格指令：明确禁止输出推理过程（部分推理模型会把额度全用在思考上）
_STRICT_SUFFIX = (
    "\n\n【严格模式】上次未产出正文。请**直接输出JSON**，"
    "禁止输出任何思考过程/解释/前后缀文字，第一个字符必须是 { 。"
)


def parse_sentiment_payload(payload: dict[str, Any]) -> tuple[
        float | None, int, int, int, str, str | None]:
    """解析 LLM 返回 → (score, pos, neg, neutral, summary, 越界说明)。

    score 越界/非数值 → 返回 None 并给出说明（由调用方降级为计数口径）。
    """
    raw_score = payload.get("score")
    score: float | None
    note: str | None = None
    try:
        score = float(raw_score)
        if score != score:  # NaN
            raise ValueError("NaN")
        if score < -1.0 or score > 1.0:
            note = f"LLM情绪分越界({score})已丢弃，降级为计数口径"
            score = None
    except (TypeError, ValueError):
        note = "LLM未返回有效情绪分，降级为计数口径"
        score = None

    def _count(key: str, items: list[Any], polarity: str) -> int:
        value = payload.get(key)
        try:
            result = int(value)
            if result >= 0:
                return result
        except (TypeError, ValueError):
            pass
        # 计数字段缺失 → 由 items 极性统计兜底
        return sum(
            1 for item in items
            if isinstance(item, dict) and str(item.get("polarity")) == polarity
        )

    items = payload.get("items")
    items = items if isinstance(items, list) else []
    positive = _count("positive", items, "positive")
    negative = _count("negative", items, "negative")
    neutral = _count("neutral", items, "neutral")
    summary = str(payload.get("summary") or "").strip()[:120]
    return score, positive, negative, neutral, summary, note


def polarities_from_payload(payload: dict[str, Any], count: int) -> list[str]:
    """逐条极性（缺省 neutral），供前端新闻列表着色。"""
    items = payload.get("items")
    mapping: dict[int, str] = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            polarity = str(item.get("polarity") or "neutral").lower()
            if polarity in ("positive", "negative", "neutral"):
                mapping[index] = polarity
    return [mapping.get(i, "neutral") for i in range(count)]


class NewsSentimentAnalyzer:
    """消息面情绪分析（新闻抓取 + LLM打分 + 降级）。

    带进程内 TTL 缓存（config.data.news_cache_ttl）：消息面是七因子里变化最慢的维度，
    但 LLM 调用是本模块最慢的一环（实测本地 qwen3:8b 单次 5~20s）。不缓存时每次快照
    都要重跑一遍，会把「切换标的」这类交互拖到十几秒（实测首个快照曾达 54s）。
    """

    def __init__(self, gateway: Any = None, news_fetcher: Any = None,
                 cache_ttl: int = 0, task_tier: str = TASK_TIER,
                 llm_retries: int = 1, item_text_chars: int = 180,
                 use_gateway_cache: bool = False,
                 reuse_when_unchanged: bool = True) -> None:
        self._gateway = gateway
        self._news_fetcher = news_fetcher
        self._cache_ttl = max(0, int(cache_ttl))
        self._task_tier = task_tier or TASK_TIER
        self._llm_retries = max(0, int(llm_retries))
        self._item_text_chars = max(40, int(item_text_chars))
        # 默认**不**使用网关层LLM缓存：网关会把「空返回」也缓存24h，
        # 一旦某次推理模型把token全耗在思维链上（正文为空），同一prompt之后
        # 每次都会被这条「毒缓存」命中，永远拿不到结果。
        self._use_gateway_cache = use_gateway_cache
        # 新闻内容未变时**不重跑 LLM**（见 analyze 的说明）。
        self._reuse_when_unchanged = bool(reuse_when_unchanged)
        # code → (时间戳, 新闻内容指纹, 结果)
        self._cache: dict[str, tuple[float, str, NewsSentiment]] = {}
        # 可观测：省掉了多少次 LLM 调用（供 /health 或诊断脚本查看）
        self.reused_calls = 0
        self.llm_calls = 0

    def invalidate(self) -> None:
        """清空缓存（强制刷新/测试用）。"""
        self._cache.clear()

    async def analyze(
        self, *, code: str, name: str = "", limit: int = 10,
        max_items: int = 10,
    ) -> NewsSentiment:
        """取新闻并打分；任何环节不可用都返回带 gap 的降级结果。

        ## 为什么 TTL 到期后还要比一次"新闻内容指纹"（2026-09-16 实测）

        原来只按 `(code, news_cache_ttl)` 缓存，TTL 一到就重新调 LLM —— **哪怕新闻
        一条都没变**。审计日志（`data/audit/llm_audit.jsonl`）显示做T消息面 278 次
        调用里只有 **28 个不同的 prompt**（重复率 90%，最热的一个 prompt 被推理了
        97 次），而其中 161 次走的是本地推理模型 qwen3:8b（中位 **53.7 秒**/次）——
        纯属白烧本地算力。

        改成：TTL 到期后照常抓一次新闻（本来也要抓），然后比内容指纹。
        新闻没变 → 复用上次分数、**不调 LLM**；变了 → 才重跑。
        新闻抓取频率与之前完全一致，所以这是纯收益。
        """
        cached = self._cache.get(code)
        if (cached is not None and self._cache_ttl > 0
                and time.monotonic() - cached[0] < self._cache_ttl):
            logger.debug("消息面缓存命中 %s（TTL %ds）", code, self._cache_ttl)
            return cached[2]
        if cached is not None and self._reuse_when_unchanged:
            fingerprint = await self._news_fingerprint(code, limit=limit)
            if fingerprint and fingerprint == cached[1]:
                # 内容没变：滑动 TTL 并复用结果，省掉一次 LLM
                self._cache[code] = (time.monotonic(), fingerprint, cached[2])
                self.reused_calls += 1
                logger.debug("消息面内容未变，复用上次打分 %s（省一次LLM）", code)
                return cached[2]
        result = await self._analyze_uncached(
            code=code, name=name, limit=limit, max_items=max_items)
        if self._cache_ttl > 0 or self._reuse_when_unchanged:
            fingerprint = _fingerprint_of(result) or (cached[1] if cached else "")
            self._cache[code] = (time.monotonic(), fingerprint, result)
        return result

    async def _news_fingerprint(self, code: str, *, limit: int) -> str:
        """抓一次新闻并算内容指纹（抓不到返回 ""，调用方据此走正常流程）。"""
        if self._news_fetcher is None:
            return ""
        try:
            raw_items = await self._news_fetcher.fetch_news(code, limit=limit)
        except Exception as exc:  # noqa: BLE001 新闻是增强链路，失败不阻断
            logger.debug("消息面指纹抓取失败(%s): %s", code, brief(exc, BRIEF_TIGHT))
            return ""
        return _fingerprint_of_raw(raw_items)

    async def _analyze_uncached(
        self, *, code: str, name: str = "", limit: int = 10,
        max_items: int = 10,
    ) -> NewsSentiment:
        if self._news_fetcher is None:
            return NewsSentiment(
                available=False, gap="新闻源未装配（news_fetcher 缺失）")
        try:
            raw_items = await self._news_fetcher.fetch_news(code, limit=limit)
        except Exception as exc:  # noqa: BLE001 新闻是增强链路，失败不阻断
            logger.warning("个股新闻抓取异常(%s): %s", code, brief(exc, BRIEF_DEFAULT))
            raw_items = []
        if not raw_items:
            return NewsSentiment(
                available=False, news_count=0,
                gap="近24小时未取到个股新闻（新闻源不可用或确无新闻）")

        items = [
            NewsItem(
                title=str(item.get("title") or "")[:200],
                source_name=str(item.get("source_name") or "")[:40],
                source_url=str(item.get("source_url") or "")[:500],
                publish_time=str(item.get("publish_time") or "")[:32],
            )
            for item in raw_items[:max_items]
        ]
        base = NewsSentiment(
            available=True, news_count=len(items), items=items,
            source_name=str(raw_items[0].get("source_name") or "东方财富"),
        )
        if self._gateway is None:
            base.gap = "LLM网关未装配，仅按关键词极性统计"
            return self._fallback_counts(base, raw_items)

        news_block = build_news_block(
            raw_items, max_items, self._item_text_chars)
        prompt = _PROMPT_TEMPLATE.format(
            name=name or code, code=code, news=news_block)

        response = None
        payload: dict[str, Any] | None = None
        failure = ""
        # 推理模型偶发把 max_tokens 全耗在思维链上、正文一个字都不输出
        # （实测 qwen3:8b：同一 prompt 一次返回合法JSON，另一次 tokens_out=max_tokens
        #  且 content 为空）。这种失败与 prompt 无关、纯随机，重试一次即可；
        # 重试时 prompt 追加严格指令（同时因 prompt 变化而绕开可能已被污染的LLM缓存条目）。
        for attempt in range(self._llm_retries + 1):
            current_prompt = prompt + (_STRICT_SUFFIX if attempt > 0 else "")
            try:
                self.llm_calls += 1
                response = await self._gateway.complete(
                    self._task_tier, SYSTEM_PROMPT, current_prompt,
                    agent_id=AGENT_ID, trace_id=f"intraday_{code}",
                    json_mode=True, use_cache=self._use_gateway_cache)
            except Exception as exc:  # noqa: BLE001 模型不可用 → 计数口径
                logger.warning("消息面LLM调用失败(%s): %s", code, brief(exc, BRIEF_DEFAULT))
                base.gap = f"LLM调用失败（{brief(exc, BRIEF_TIGHT)}），降级为关键词计数口径"
                return self._fallback_counts(base, raw_items)
            content = getattr(response, "content", "") or ""
            if not content.strip():
                tokens_out = int(getattr(response, "tokens_out", 0) or 0)
                failure = (
                    f"模型本次未产出正文（输出tokens={tokens_out}，疑似思维链耗尽额度）"
                    if tokens_out else "模型本次返回空内容")
                logger.warning("消息面LLM空返回(%s) 第%d次: tokens_out=%s",
                               code, attempt + 1, tokens_out)
                continue
            try:
                payload = parse_llm_json(AGENT_ID, content)
                break
            except AgentExecutionError as exc:
                failure = f"LLM输出非合法JSON（{brief(exc, BRIEF_TIGHT)}）"
                logger.warning("消息面LLM输出解析失败(%s) 第%d次: %s",
                               code, attempt + 1, brief(exc, BRIEF_DEFAULT))
                payload = None
                continue
        if payload is None or response is None:
            base.gap = f"{failure}，已重试{self._llm_retries}次，降级为关键词计数口径"
            return self._fallback_counts(base, raw_items)

        score, positive, negative, neutral, summary, note = (
            parse_sentiment_payload(payload))
        polarities = polarities_from_payload(payload, len(items))
        for item, polarity in zip(items, polarities, strict=True):
            item.polarity = polarity  # type: ignore[assignment]
        base.llm_score = score
        base.llm_used = score is not None
        base.positive_count = positive
        base.negative_count = negative
        base.neutral_count = neutral
        base.summary = summary
        base.model = str(getattr(response, "model_used", "") or "")
        base.score = 0.0 if score is None else score
        base.gap = note
        return base

    @staticmethod
    def _fallback_counts(base: NewsSentiment,
                         raw_items: list[dict[str, Any]]) -> NewsSentiment:
        """无LLM时的关键词极性兜底（不做情绪分，只给计数，由计数口径打分）。"""
        positive = negative = neutral = 0
        for item, news in zip(base.items, raw_items, strict=False):
            text = f"{item.title} {news.get('text', '')}"
            polarity = _keyword_polarity(text)
            item.polarity = polarity  # type: ignore[assignment]
            if polarity == "positive":
                positive += 1
            elif polarity == "negative":
                negative += 1
            else:
                neutral += 1
        base.positive_count = positive
        base.negative_count = negative
        base.neutral_count = neutral
        base.llm_score = None
        base.llm_used = False
        return base


# 关键词极性兜底词表（仅用于LLM不可用时的计数，不参与情绪分）
_POSITIVE_WORDS = (
    "中标", "订单", "增长", "超预期", "扭亏", "盈利", "涨价", "提价", "回购",
    "增持", "突破", "获批", "签约", "扩产", "涨停", "利好", "上调", "新高",
)
_NEGATIVE_WORDS = (
    "减持", "亏损", "下滑", "低于预期", "处罚", "问询", "违规", "诉讼", "质押",
    "商誉", "退市", "跌停", "利空", "下调", "终止", "解禁", "爆雷", "停产",
)


def _keyword_polarity(text: str) -> str:
    pos = sum(1 for word in _POSITIVE_WORDS if word in text)
    neg = sum(1 for word in _NEGATIVE_WORDS if word in text)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def news_items_as_dicts(sentiment: NewsSentiment) -> str:
    """调试用：新闻条目转JSON文本。"""
    return json.dumps(
        [item.model_dump() for item in sentiment.items], ensure_ascii=False)
