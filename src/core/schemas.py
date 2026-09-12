"""core层共享Schema：置信度、数据引用、推理步骤。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    """LLM输出与分析结论的置信度分级。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


StepType = Literal[
    "data_retrieval",
    "indicator_calculation",
    "signal_trigger",
    "llm_inference",
    "cross_validation",
    "final_conclusion",
]
"""推理步骤类型（OBSERVABILITY.md 二）。"""


class DataSourceRef(BaseModel):
    """数据溯源引用（最小集，完整元数据契约见 docs/DATA_CONTRACT.md）。"""

    data_id: str = Field(default_factory=lambda: f"d_{uuid4().hex}")
    source_name: str
    source_url: str


class TraceStep(BaseModel):
    """推理路径单步记录（docs/OBSERVABILITY.md 一）。"""

    step: int
    step_type: StepType
    description: str
    data_refs: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int | None = None
