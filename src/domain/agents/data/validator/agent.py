"""A03 数据校验Agent：数据准确性校验、异常值检测。

无LLM（确定性规则校验）；对未通过项降低置信度供下游参考。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, DataPoint, TraceStep
from src.domain.agents.data.validator.logic import run_validation
from src.domain.agents.data.validator.models import ValidatorPayload


class DataValidatorAgent(BaseAgent):
    """A03：输入A02产出的数据集，输出校验报告与质量评分。"""

    def __init__(self, agent_id: str = "A03_data_validator") -> None:
        super().__init__(agent_id)

    def get_capabilities(self) -> dict[str, Any]:
        return {"capabilities": ["range_check", "logic_check", "anomaly_detect"]}

    def health_check(self) -> bool:
        return True

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = ValidatorPayload.model_validate(input.payload)
        try:
            points = [DataPoint.model_validate(item) for item in payload.data_points]
        except Exception as exc:
            raise AgentExecutionError(f"A03输入DataPoint解析失败: {exc}") from exc

        report = run_validation(points)
        issue_map: dict[str, list[str]] = {}
        for issue in report.issues:
            issue_map.setdefault(issue.data_id, []).append(f"[{issue.check}] {issue.detail}")

        now = datetime.now(timezone.utc)
        validated: list[DataPoint] = []
        for point in points:
            if point.data_id in issue_map:
                # 未通过校验：置信度减半，问题明细写入extra
                validated.append(
                    point.model_copy(
                        update={
                            "confidence": round(point.confidence * 0.5, 4),
                            "processed_by": self.agent_id,
                            "process_time": now,
                            "extra": {
                                **point.extra,
                                "validation_issues": issue_map[point.data_id],
                            },
                        }
                    )
                )
            else:
                validated.append(
                    point.model_copy(
                        update={"processed_by": self.agent_id, "process_time": now}
                    )
                )

        steps = [
            TraceStep(
                step=1,
                step_type="cross_validation",
                description=(
                    f"校验 {len(points)} 条（范围/逻辑/离群）："
                    f"通过 {len(report.passed_ids)} 条，问题 {len(report.issues)} 项，"
                    f"质量评分 {report.quality_score}"
                ),
                data_refs=report.passed_ids,
            )
        ]

        return AgentOutput(
            task_id=input.task_id,
            agent_id=self.agent_id,
            conclusion=(
                f"校验完成：{len(points)} 条数据质量评分 {report.quality_score}，"
                f"发现 {len(report.issues)} 项问题"
            ),
            confidence=Confidence.HIGH if report.quality_score >= 0.9 else Confidence.MEDIUM,
            data_refs=[p.data_id for p in validated],
            reasoning_steps=steps,
            result={
                "data_points": [p.model_dump(mode="json") for p in validated],
                "quality_score": report.quality_score,
                "issues": [i.model_dump(mode="json") for i in report.issues],
            },
        )
