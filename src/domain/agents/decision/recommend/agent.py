"""A17 投研建议Agent（决策层）。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import parse_llm_json
from src.infrastructure.llm import LLMGateway


class RecommendationPayload(BaseModel):
    """A17输入：上游分析Agent输出的聚合。"""

    analyses: list[dict[str, Any]] = Field(default_factory=list)
    """各分析Agent的AgentOutput摘要（agent_id/conclusion/confidence/result）"""
    focus: str = ""
    user_query: str = ""


class RecommendationAgent(BaseAgent):
    """综合宏观/中观/微观/财务风险四维分析，仲裁冲突，输出投研建议（PRD A17，P0）。"""

    system_prompt = (
        "你是投研委员会主席。综合宏观（A08）、行业（A09）、个股（A10）、财务风险（A11）"
        "四维分析结论，生成投研建议。要求：\n"
        "1. 结论间冲突时必须显式仲裁并给出取舍依据（写入conflicts_resolved）；\n"
        "2. 立场（stance）必须与四维证据强度一致，风险维度为高时禁止看多；\n"
        "3. 每条逻辑必须能回溯到对应分析维度；\n"
        "4. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway: LLMGateway, agent_id: str = "A17_recommend") -> None:
        super().__init__(agent_id)
        self._gateway = gateway

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["synthesize", "conflict_arbitration", "stance_decision"],
            "task_tier": "decision",
        }

    def health_check(self) -> bool:
        return True

    def _parse_payload(self, payload: dict[str, Any]) -> RecommendationPayload:
        try:
            return RecommendationPayload.model_validate(payload)
        except ValidationError as exc:
            raise AgentExecutionError(f"{self.agent_id}输入不合法: {exc}") from exc

    def _build_context(self, payload: RecommendationPayload) -> str:
        blocks = []
        for a in payload.analyses:
            result = {
                k: v for k, v in (a.get("result") or {}).items()
                if k not in ("tokens_in", "tokens_out", "model_used")
            }
            blocks.append(
                f"### {a.get('agent_id', '?')}（置信度: {a.get('confidence', '?')}）\n"
                f"结论: {a.get('conclusion', '')}\n"
                f"结构化: {json.dumps(result, ensure_ascii=False)}"
            )
        return "\n\n".join(blocks) if blocks else "（无上游分析）"

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = self._parse_payload(input.payload)
        if not payload.analyses:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion="无上游分析结论可综合，跳过建议生成",
                confidence=Confidence.LOW, trace_id=input.task_id,
            )

        prompt = (
            f"## 投研标的/主题\n{payload.focus or '综合'}\n"
            f"## 用户问题\n{payload.user_query or '无'}\n\n"
            f"## 上游分析结论\n{self._build_context(payload)}\n\n"
            "## 任务要求\n请输出JSON对象，字段：\n"
            '- "conclusion": 综合投研建议（200字内）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "stance": "看多"|"中性"|"谨慎"\n'
            '- "key_logic": 核心逻辑2-4条（标注来源维度，如[宏观]）\n'
            '- "catalysts": 潜在催化1-3条\n'
            '- "risks": 主要风险1-3条\n'
            '- "monitoring_points": 后续跟踪指标2-4条\n'
            '- "conflicts_resolved": 冲突仲裁说明（无冲突则空数组）\n\n'
            "注意：本分析仅供研究参考，不构成投资建议。"
        )
        response = await self._gateway.complete(
            "decision", self.system_prompt, prompt,
            agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
        )
        data = parse_llm_json(self.agent_id, response.content)

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=str(data.get("conclusion", "")),
            confidence=Confidence(data.get("confidence", "medium")),
            data_refs=[a.get("agent_id", "?") for a in payload.analyses],
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="llm_inference",
                          description=f"综合{len(payload.analyses)}维分析 model={response.model_used}"),
            ],
            result={**data, "stance": data.get("stance", "中性"),
                    "model_used": response.model_used,
                    "disclaimer": "以上信息仅供研究参考，不构成投资建议。投资有风险，入市需谨慎，盈亏自负。"},
        )
