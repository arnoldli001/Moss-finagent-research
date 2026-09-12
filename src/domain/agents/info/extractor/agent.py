"""A06 信息提取Agent：从非结构化文本提取结构化事件表（PRD A06，P1）。

防幻觉策略：事件必须带evidence_quote（原文证据）且item_id可溯源到具体条目；
方向/类型/置信度由本地代码规范化到枚举白名单，LLM输出的越界值不透传。
"""

from __future__ import annotations

from typing import Any

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import parse_llm_json
from src.domain.agents.info.models import normalize_direction, normalize_event_type
from src.infrastructure.llm import LLMGateway, TaskTier

_ITEM_TEXT_CHARS = 500
"""单条目正文注入prompt的最大字符数（控制上下文规模）"""

_MAX_EVENTS = 30
"""单次提取事件数上限（超出部分丢弃，防止幻觉刷屏）"""


class ExtractorAgent:
    """A06_extractor。"""

    task_tier: TaskTier = "medium"

    def __init__(self, gateway: LLMGateway, agent_id: str = "A06_extractor") -> None:
        self.agent_id = agent_id
        self._gateway = gateway

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["event_extraction", "structured_information"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    async def execute(self, input: AgentInput) -> AgentOutput:
        raw_items = input.payload.get("info_items", [])
        if not raw_items:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion="无可信信息可提取，跳过事件提取",
                confidence=Confidence.LOW, trace_id=input.task_id,
                reasoning_steps=[TraceStep(step=1, step_type="data_retrieval",
                                           description="输入info_items为空，跳过LLM调用")],
                result={"events": [], "stats": {"total": 0, "positive": 0,
                                                "negative": 0, "neutral": 0}},
            )

        lines = []
        for item in raw_items:
            text = str(item.get("text", ""))[:_ITEM_TEXT_CHARS]
            title = item.get("title", "")
            lines.append(
                f"- [{item.get('item_id', '?')}] {title} | {text}"
            )

        prompt = (
            "## 待提取信息条目\n" + "\n".join(lines) +
            "\n\n## 任务要求\n"
            "从上述条目中提取全部可确认的事件（每条信息可提取0-3个事件）。输出JSON对象：\n"
            '- "events": [{"item_id": "来源条目ID", "event_type": "earnings|merger|policy|'
            'product|management|litigation|financing|other", "subject": "事件主体（公司/行业/宏观）", '
            '"direction": "positive|negative|neutral", "magnitude": "影响程度简述（20字内，无量化则留空）", '
            '"event_date": "事件发生日期YYYY-MM-DD（未知留空）", '
            '"evidence_quote": "支撑该事件的原文片段（必须逐字来自输入，30字内）", '
            '"confidence": 0到1之间的数值}]\n'
            "硬性要求：evidence_quote禁止改写或编造；宁缺毋滥，无法确认的事件不要输出。"
            "本提取仅供研究参考，不构成投资建议。"
        )
        response = await self._gateway.complete(
            self.task_tier,
            "你是财经信息结构化专家，负责从新闻/公告/研报中提取可验证的事件。"
            "严格依据给定文本，禁止编造事件或数值。",
            prompt, agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
        )
        data = parse_llm_json(self.agent_id, response.content)

        valid_ids = {str(i.get("item_id", "")) for i in raw_items}
        events: list[dict[str, Any]] = []
        for raw in data.get("events", [])[:_MAX_EVENTS]:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("item_id", ""))
            if item_id not in valid_ids:
                continue  # item_id不可溯源的事件直接丢弃
            try:
                confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            events.append({
                "item_id": item_id,
                "event_type": normalize_event_type(str(raw.get("event_type", ""))),
                "subject": str(raw.get("subject", ""))[:60],
                "direction": normalize_direction(str(raw.get("direction", ""))),
                "magnitude": str(raw.get("magnitude", ""))[:40],
                "event_date": str(raw.get("event_date", ""))[:10],
                "evidence_quote": str(raw.get("evidence_quote", ""))[:60],
                "confidence": round(confidence, 3),
            })

        stats = {
            "total": len(events),
            "positive": sum(1 for e in events if e["direction"] == "positive"),
            "negative": sum(1 for e in events if e["direction"] == "negative"),
            "neutral": sum(1 for e in events if e["direction"] == "neutral"),
        }
        ratio_ok = (stats["total"] / len(raw_items)) if raw_items else 0.0
        confidence_level = (Confidence.HIGH if ratio_ok >= 1.5
                            else Confidence.MEDIUM if stats["total"] > 0 else Confidence.LOW)
        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=f"从 {len(raw_items)} 条可信信息中提取 {stats['total']} 个事件"
                       f"（利好 {stats['positive']} / 利空 {stats['negative']} / 中性 {stats['neutral']}）",
            confidence=confidence_level,
            data_refs=sorted(valid_ids - {""}),
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="data_retrieval",
                          description=f"接收 {len(raw_items)} 条已核验信息"),
                TraceStep(step=2, step_type="llm_inference",
                          description=f"model={response.model_used} cache={response.cache_kind} "
                                      f"fallback={response.fallback_used}"),
                TraceStep(step=3, step_type="indicator_calculation",
                          description="事件规范化：direction/event_type白名单校验，"
                                      "无溯源item_id的事件丢弃"),
            ],
            result={"events": events, "stats": stats, "model_used": response.model_used},
        )
