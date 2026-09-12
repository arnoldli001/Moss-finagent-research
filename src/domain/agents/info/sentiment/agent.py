"""A07 舆情分析Agent：市场情绪量化 + 情绪周期定位（PRD A07，P2）。

防幻觉策略：情绪分/分布/热点主体全部由本地规则计算（logic.py），
LLM（reasoning层）只负责解读指标并做周期定位，禁止自造数值。
"""

from __future__ import annotations

from typing import Any

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import parse_llm_json
from src.domain.agents.info.sentiment.logic import compute_sentiment_metrics
from src.infrastructure.llm import LLMGateway, TaskTier

_PHASES = ("乐观", "分歧", "谨慎", "恐慌", "不明确")


class SentimentAgent:
    """A07_sentiment。"""

    task_tier: TaskTier = "reasoning"

    def __init__(self, gateway: LLMGateway, agent_id: str = "A07_sentiment") -> None:
        self.agent_id = agent_id
        self._gateway = gateway

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["sentiment_quantification", "sentiment_cycle_positioning"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    async def execute(self, input: AgentInput) -> AgentOutput:
        events = input.payload.get("events", [])
        if not events:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion="无事件可分析，跳过舆情量化",
                confidence=Confidence.LOW, trace_id=input.task_id,
                reasoning_steps=[TraceStep(step=1, step_type="data_retrieval",
                                           description="输入events为空，跳过LLM调用")],
                result={"sentiment_metrics": compute_sentiment_metrics([])},
            )

        metrics = compute_sentiment_metrics(events)
        event_lines = [
            f"- [{e.get('direction', 'neutral')}] {e.get('subject', '?')} "
            f"({e.get('event_type', 'other')}, 置信 {e.get('confidence', 0.5)}): "
            f"{e.get('evidence_quote', '')}"
            for e in events[:30]
        ]
        prompt = (
            f"## 本地计算的情绪指标（权威数值，禁止修改）\n"
            f"加权情绪分 {metrics['weighted_sentiment']}（[-1,1]，越大越乐观）\n"
            f"事件分布 {metrics['distribution']}\n"
            f"热点主体 {metrics['top_subjects']}\n\n"
            f"## 事件明细\n" + "\n".join(event_lines) +
            "\n\n## 任务要求\n基于上述指标与事件做情绪解读与周期定位。输出JSON对象：\n"
            '- "conclusion": 舆情综合研判（120字内，必须引用情绪分与分布数值）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "sentiment_phase": "乐观"|"分歧"|"谨慎"|"恐慌"|"不明确"\n'
            '- "narrative": 情绪主线与演变逻辑（80字内）\n'
            "注意：sentiment_phase必须与情绪分方向自洽（如情绪分-0.6不得判乐观）。"
            "本分析仅供研究参考，不构成投资建议。"
        )
        response = await self._gateway.complete(
            self.task_tier,
            "你是市场舆情分析师，负责把结构化事件表转译为情绪判读。"
            "只引用给定的指标数值，禁止编造数据。",
            prompt, agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
        )
        data = parse_llm_json(self.agent_id, response.content)

        phase = str(data.get("sentiment_phase", "不明确"))
        if phase not in _PHASES:
            phase = "不明确"
        conclusion = str(data.get("conclusion", ""))
        # 一致性防线：强空情绪不允许输出乐观周期（LLM自相矛盾时降级为不明确）
        if phase == "乐观" and metrics["weighted_sentiment"] <= -0.3:
            phase, conclusion = "不明确", f"{conclusion}（情绪分与周期定位矛盾，已置为不明确）"

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=conclusion or f"加权情绪分 {metrics['weighted_sentiment']}",
            confidence=Confidence(data.get("confidence", "medium")),
            data_refs=sorted({e.get("item_id", "") for e in events} - {""}),
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="indicator_calculation",
                          description=f"本地情绪量化：加权分 {metrics['weighted_sentiment']}，"
                                      f"分布 {metrics['distribution']}"),
                TraceStep(step=2, step_type="llm_inference",
                          description=f"model={response.model_used} cache={response.cache_kind} "
                                      f"fallback={response.fallback_used}"),
            ],
            result={
                "sentiment_metrics": metrics,
                "sentiment_phase": phase,
                "narrative": str(data.get("narrative", ""))[:120],
                "model_used": response.model_used,
            },
        )
