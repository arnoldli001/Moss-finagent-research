"""分析层Agent共享基类。

统一职责：payload解析 → 数据点上下文化 → LLM(reasoning层,JSON模式) →
输出解析与置信度融合。子类只需提供system_prompt与_requirements钩子。
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.infrastructure.llm import LLMGateway, TaskTier


class AnalysisPayload(BaseModel):
    """分析层统一输入契约（由Supervisor从ResearchState组装）。"""

    data_points: list[dict[str, Any]] = Field(default_factory=list)
    focus: str = ""
    """分析焦点：行业名 / 股票代码 / 宏观主题"""
    hint: dict[str, Any] = Field(default_factory=dict)
    """本地计算参考值（如估值对比、风险比率），LLM只解读不计算"""


def parse_llm_json(agent_id: str, content: str) -> dict[str, Any]:
    """解析LLM输出JSON（容忍```json围栏、<think>块与尾随文本）。

    依次尝试：围栏提取 → 整体loads → raw_decode取首个JSON对象。
    """
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        raise AgentExecutionError(f"{agent_id} LLM输出JSON非对象: {type(data)}")
    except json.JSONDecodeError:
        pass
    try:
        data, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise AgentExecutionError(f"{agent_id} LLM输出非合法JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentExecutionError(f"{agent_id} LLM输出JSON非对象: {type(data)}")
    return data


class AnalysisAgentBase(BaseAgent):
    """分析层四Agent（A08/A09/A10/A11）的公共骨架。"""

    task_tier: ClassVar[TaskTier] = "reasoning"
    system_prompt: ClassVar[str] = ""

    def __init__(self, agent_id: str, gateway: LLMGateway) -> None:
        super().__init__(agent_id)
        self._gateway = gateway

    def _requirements(self, payload: AnalysisPayload) -> str:
        """子类覆盖：任务要求与输出JSON schema说明。"""
        raise NotImplementedError

    def _parse_payload(self, payload: dict[str, Any]) -> AnalysisPayload:
        try:
            return AnalysisPayload.model_validate(payload)
        except ValidationError as exc:
            raise AgentExecutionError(f"{self.agent_id}输入payload不合法: {exc}") from exc

    def _build_context(self, payload: AnalysisPayload) -> str:
        """数据点 → LLM可读上下文（带溯源与置信度）。"""
        lines = []
        for p in payload.data_points:
            value = p.get("value", "缺失")
            lines.append(
                f"- {p.get('indicator', '?')} | 期间 {p.get('period_date', '?')} "
                f"| 值 {value} | 来源 {p.get('source_name', '?')} "
                f"| 数据置信度 {p.get('confidence', '?')}"
            )
        return "\n".join(lines) if lines else "（无数据点）"

    def _parse_llm_json(self, content: str) -> dict[str, Any]:
        return parse_llm_json(self.agent_id, content)

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        """子类可覆盖：向result追加本地计算字段。"""
        return data

    def _prepare(self, payload: AnalysisPayload) -> None:
        """子类可覆盖：prompt构建前的本地计算（可回填payload.hint）。"""


    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = self._parse_payload(input.payload)
        self._prepare(payload)
        if not payload.data_points:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion=f"未获取到{payload.focus or '相关'}数据点，无法完成分析",
                confidence=Confidence.LOW, trace_id=input.task_id,
                reasoning_steps=[TraceStep(step=1, step_type="data_retrieval",
                                           description="输入数据点为空，跳过LLM调用")],
            )

        hint = json.dumps(payload.hint, ensure_ascii=False) if payload.hint else "无"
        prompt = (
            f"## 分析焦点\n{payload.focus or '综合分析'}\n\n"
            f"## 输入数据（含溯源）\n{self._build_context(payload)}\n\n"
            f"## 本地计算参考\n{hint}\n\n"
            f"## 任务要求\n{self._requirements(payload)}\n\n"
            "注意：本分析仅供研究参考，不构成投资建议。"
        )
        response = await self._gateway.complete(
            self.task_tier, self.system_prompt, prompt,
            agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
        )
        data = self._parse_llm_json(response.content)
        result = self._enrich_result(payload, data)

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=str(data.get("conclusion", "")),
            confidence=Confidence(data.get("confidence", "medium")),
            data_refs=self._collect_refs(payload),
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="data_retrieval",
                          description=f"上下文化 {len(payload.data_points)} 个数据点"),
                TraceStep(step=2, step_type="llm_inference",
                          description=(
                              f"model={response.model_used} cache={response.cache_kind} "
                              f"fallback={response.fallback_used}"
                          )),
            ],
            result={**result, "model_used": response.model_used,
                    "tokens_in": response.tokens_in, "tokens_out": response.tokens_out},
        )

    @staticmethod
    def _collect_refs(payload: AnalysisPayload) -> list[str]:
        refs = []
        for p in payload.data_points:
            refs.append(
                p.get("data_id") or p.get("raw_content_hash")
                or f"{p.get('indicator', '?')}:{p.get('period_date', '?')}"
            )
        return refs
