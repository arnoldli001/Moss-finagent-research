"""两阶段LLM事件分析器（domain服务，仅经LLMGateway，FR-5）。

阶段一 medium：批量分类/情感/实体（失败回退本地关键词分类，不阻断）；
阶段二 reasoning：批量风险/机会打分、受影响个股、传导路径（失败返回空）。
单事件JSON非法或评分数值越界（FR-6）→ 丢弃该事件，不影响其他事件。
每次扫描网关调用 ≤ 2 次；空候选零调用。
证券名称解析器由装配层注入（domain不依赖infrastructure）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.domain.agents.analysis.base import parse_llm_json
from src.domain.alerts import prompts
from src.domain.alerts.models import (
    AffectedStock,
    Event,
    EventAssessment,
    EventType,
)
from src.infrastructure.llm import LLMGateway

logger = logging.getLogger(__name__)

AGENT_ID = "alert_analyzer"
_EVENT_TYPES = {t.value for t in EventType}
_SENTIMENTS = {"positive", "negative", "neutral"}
_IMPACTS = {"positive", "negative", "mixed"}
_MAX_STOCKS_PER_EVENT = 5
_RESOLVE_CONCURRENCY = 4

Resolver = Callable[[str], Awaitable[tuple[str, str] | None]]


class ScoreOutOfRange(ValueError):
    """LLM评分超出合法区间（FR-6：丢弃该事件评估，不做静默钳制）。"""


def _bounded(value: Any, lo: float, hi: float, default: float) -> float:
    """非数值/NaN→default；越界→ScoreOutOfRange（由调用方丢弃事件）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    if num < lo or num > hi:
        raise ScoreOutOfRange(f"score {num} out of [{lo}, {hi}]")
    return num


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:50] for v in value
            if v is not None and str(v).strip()][:10]


def _stage1_fallback(events: list[Event]) -> dict[str, dict[str, Any]]:
    """阶段一LLM不可用时的本地兜底：沿用normalizer的类型，情感中性。"""
    return {
        e.event_id: {
            "event_type": e.event_type.value, "sentiment": "neutral",
            "entities": e.entities.model_dump(), "summary": "",
        }
        for e in events
    }


def salvage_assessments(content: str) -> list[dict[str, Any]]:
    """从可能被max_tokens截断的输出中抢救assessments数组里完整的对象。

    reasoning模型思维链挤占输出预算时尾部JSON易截断；逐项raw_decode，
    遇到第一个不完整对象即停止（其余事件保持未分析，下轮重试）。
    """
    marker = content.find("assessments")
    start = content.find("[", marker) if marker >= 0 else -1
    if start < 0:
        return []
    decoder = json.JSONDecoder()
    pos, out = start + 1, []
    while pos < len(content):
        while pos < len(content) and content[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(content) or content[pos] == "]":
            break
        try:
            obj, end = decoder.raw_decode(content, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict) and obj.get("event_id"):
            out.append(obj)
        pos = end
    return out


class EventAnalyzer:
    """事件风险/机会分析器（无状态：每次analyze独立）。"""

    def __init__(
        self, gateway: LLMGateway,
        resolver: Resolver | None = None,
    ) -> None:
        self._gateway = gateway
        # 解析器由composition root注入；未注入时仅保留名称不补代码（D11）
        self._resolve = resolver

    async def analyze(self, events: list[Event]) -> list[EventAssessment]:
        if not events:
            return []
        stage1 = await self._run_stage1(events)
        stage2, model_used = await self._run_stage2(events, stage1)
        assessments: list[EventAssessment] = []
        for event in events:
            scored = stage2.get(event.event_id)
            if not scored:
                continue
            info = stage1.get(event.event_id, {})
            stocks = await self._resolve_stocks(scored.get("affected_stocks"))
            try:
                assessment = self._build_assessment(
                    event, info, scored, stocks, model_used)
            except ScoreOutOfRange:
                # FR-6：越界评分丢弃该事件（不静默钳制饱和成告警）
                logger.info("事件%s评分越界，丢弃该评估", event.event_id)
                continue
            assessments.append(assessment)
        logger.info(
            "alert_analyzer: %d 候选 -> %d 条有效评估", len(events), len(assessments))
        return assessments

    def _build_assessment(
        self, event: Event, info: dict, scored: dict,
        stocks: list[AffectedStock], model_used: str,
    ) -> EventAssessment:
        etype_value = info.get("event_type", event.event_type.value)
        try:
            etype = EventType(etype_value) if etype_value in _EVENT_TYPES \
                else event.event_type
        except ValueError:
            etype = event.event_type
        entities = info.get("entities") or {}
        return EventAssessment(
            event_id=event.event_id,
            event_type=etype,
            sentiment=info.get("sentiment", "neutral")
            if info.get("sentiment") in _SENTIMENTS else "neutral",
            risk_score=_bounded(scored.get("risk_score"), 0, 100, 0.0),
            opportunity_score=_bounded(
                scored.get("opportunity_score"), 0, 100, 0.0),
            confidence=round(_bounded(scored.get("confidence"), 0, 1, 0.0), 2),
            affected_stocks=stocks,
            affected_industries=_str_list(entities.get("industries")),
            impact_path=str(scored.get("impact_path", ""))[:200],
            summary=str(info.get("summary", ""))[:200],
            model_used=model_used,
        )

    async def _run_stage1(
        self, events: list[Event],
    ) -> dict[str, dict[str, Any]]:
        try:
            resp = await self._gateway.complete(
                "medium", prompts.STAGE1_SYSTEM,
                prompts.build_stage1_prompt(events),
                agent_id=AGENT_ID, json_mode=True, use_cache=False,
            )
            data = parse_llm_json(AGENT_ID, resp.content)
            return self._parse_stage1(data.get("events"), events)
        except Exception as exc:  # noqa: BLE001 降级为本地分类，扫描不中断
            logger.warning("阶段一分类失败，使用本地兜底: %s", str(exc)[:200])
            return _stage1_fallback(events)

    def _parse_stage1(
        self, raw: Any, events: list[Event],
    ) -> dict[str, dict[str, Any]]:
        valid_ids = {e.event_id for e in events}
        out: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, list):
            return _stage1_fallback(events)
        for item in raw:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id", ""))
            if event_id not in valid_ids:
                continue
            etype = item.get("event_type", "")
            out[event_id] = {
                "event_type": etype if etype in _EVENT_TYPES else "",
                "sentiment": item.get("sentiment", "neutral"),
                "entities": {
                    "industries": _str_list(
                        (item.get("entities") or {}).get("industries")),
                    "companies": _str_list(
                        (item.get("entities") or {}).get("companies")),
                    "regions": _str_list(
                        (item.get("entities") or {}).get("regions")),
                },
                "summary": str(item.get("summary", ""))[:200],
            }
        # 未覆盖的事件用本地兜底补齐
        for event in events:
            out.setdefault(event.event_id, _stage1_fallback([event])[event.event_id])
        return out

    async def _run_stage2(
        self, events: list[Event], stage1: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], str]:
        try:
            resp = await self._gateway.complete(
                "reasoning", prompts.STAGE2_SYSTEM,
                prompts.build_stage2_prompt(events, stage1),
                agent_id=AGENT_ID, json_mode=True, use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001 打分层不可用→本轮无告警
            logger.warning("阶段二评估失败，本轮不产生LLM评估: %s", str(exc)[:200])
            return {}, ""
        try:
            data = parse_llm_json(AGENT_ID, resp.content)
        except Exception:  # noqa: BLE001 截断/非法JSON→抢救完整前缀
            salvaged = salvage_assessments(resp.content)
            if salvaged:
                logger.info("阶段二JSON截断，抢救出%d/%d条评估",
                            len(salvaged), len(events))
                return self._parse_stage2(salvaged), resp.model_used
            logger.warning("阶段二输出非法JSON且无可抢救内容")
            return {}, resp.model_used
        return self._parse_stage2(data.get("assessments")), resp.model_used

    def _parse_stage2(self, raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, list):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict) or not item.get("event_id"):
                continue
            out[str(item["event_id"])] = item
        return out

    async def _resolve_stocks(self, raw: Any) -> list[AffectedStock]:
        if not isinstance(raw, list):
            return []
        semaphore = asyncio.Semaphore(_RESOLVE_CONCURRENCY)

        async def _one(item: Any) -> AffectedStock | None:
            if not isinstance(item, dict):
                return None
            name = str(item.get("name", "")).strip()
            if not name:
                return None
            impact = item.get("impact", "mixed")
            stock = AffectedStock(
                code="", name=name[:20],
                impact=impact if impact in _IMPACTS else "mixed",
                reason=str(item.get("reason", ""))[:100])
            async with semaphore:
                if self._resolve is None:
                    return stock
                try:
                    resolved = await self._resolve(name)
                except Exception:  # noqa: BLE001 名称解析尽力而为
                    resolved = None
            if resolved:
                stock.code = resolved[0]
            return stock

        results = await asyncio.gather(
            *[_one(it) for it in raw[:_MAX_STOCKS_PER_EVENT]])
        return [s for s in results if s is not None]
