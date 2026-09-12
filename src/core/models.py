"""Agent输入输出模型（BaseAgent契约，agent-interface-spec.md）。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from src.core.schemas import Confidence, TraceStep


class AgentInput(BaseModel):
    """Agent任务输入。"""

    task_id: str
    tenant_id: str
    user_context: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    """Agent结构化输出。

    所有分析结论必须携带confidence与data_refs（可溯源），
    reasoning_steps记录完整推理路径。
    """

    task_id: str
    agent_id: str
    conclusion: str
    confidence: Confidence
    data_refs: list[str] = Field(default_factory=list)
    trace_id: str = Field(default_factory=lambda: f"trace_{uuid4().hex}")
    reasoning_steps: list[TraceStep] = Field(default_factory=list)
