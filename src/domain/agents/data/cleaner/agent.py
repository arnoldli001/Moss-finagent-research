"""A02 数据清洗Agent：标准化、去重、格式转换。

无LLM（configs/agents.yaml model=local_light，清洗为确定性规则，无需模型）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, DataPoint, TraceStep
from src.domain.agents.data.cleaner.logic import deduplicate_points, normalize_points
from src.domain.agents.data.cleaner.models import CleanerPayload


class DataCleanerAgent(BaseAgent):
    """A02：输入A01产出的DataPoint字典列表，输出标准化去重后的数据集。"""

    def __init__(self, agent_id: str = "A02_data_cleaner") -> None:
        super().__init__(agent_id)

    def get_capabilities(self) -> dict[str, Any]:
        return {"capabilities": ["normalize", "deduplicate", "format_convert"]}

    def health_check(self) -> bool:
        return True

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = CleanerPayload.model_validate(input.payload)
        try:
            raw = [DataPoint.model_validate(item) for item in payload.data_points]
        except Exception as exc:
            raise AgentExecutionError(f"A02输入DataPoint解析失败: {exc}") from exc

        normalized = normalize_points(raw)
        kept, removed = deduplicate_points(normalized)
        now = datetime.now(timezone.utc)
        # 期间缺失/值缺失的点降低置信度，供下游校验Agent重点复查
        cleaned = [
            p.model_copy(
                update={
                    "processed_by": self.agent_id,
                    "process_time": now,
                    "confidence": 0.9 if p.period_date and p.value is not None else 0.6,
                }
            )
            for p in kept
        ]

        steps = [
            TraceStep(
                step=1,
                step_type="indicator_calculation",
                description=f"标准化 {len(raw)} 条：期间ISO化、数值float化",
                data_refs=[p.data_id for p in raw],
            ),
            TraceStep(
                step=2,
                step_type="cross_validation",
                description=f"按(指标,期间,值)去重：保留 {len(cleaned)} 条，剔除 {len(removed)} 条",
                data_refs=[p.data_id for p in cleaned],
            ),
        ]

        return AgentOutput(
            task_id=input.task_id,
            agent_id=self.agent_id,
            conclusion=(
                f"清洗完成：输入 {len(raw)} 条 → 输出 {len(cleaned)} 条"
                f"（剔除 {len(removed)} 条重复）"
            ),
            confidence=Confidence.MEDIUM if cleaned else Confidence.LOW,
            data_refs=[p.data_id for p in cleaned],
            reasoning_steps=steps,
            result={
                "data_points": [p.model_dump(mode="json") for p in cleaned],
                "removed_ids": removed,
            },
        )
