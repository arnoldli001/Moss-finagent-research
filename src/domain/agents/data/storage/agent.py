"""A04 数据存储Agent：数据入库、版本管理、血缘追踪。

存储后端通过StorageRepository结构化协议注入，domain不依赖infrastructure。
"""

from __future__ import annotations

from typing import Any, Protocol

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, DataPoint, TraceStep
from src.domain.agents.data.storage.models import StoragePayload


class StorageRepository(Protocol):
    """存储仓储结构化协议（duck typing）。"""

    async def save_points(self, points: list[DataPoint], task_id: str) -> dict[str, int]: ...


class DataStorageAgent(BaseAgent):
    """A04：将校验后数据入库（SQLite），返回存储快照统计。"""

    def __init__(self, repo: StorageRepository, agent_id: str = "A04_data_storage") -> None:
        super().__init__(agent_id)
        self._repo = repo

    def get_capabilities(self) -> dict[str, Any]:
        return {"capabilities": ["db_insert", "version_manage", "lineage_track"]}

    def health_check(self) -> bool:
        try:
            return self._repo is not None
        except Exception:
            return False

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = StoragePayload.model_validate(input.payload)
        try:
            points = [DataPoint.model_validate(item) for item in payload.data_points]
        except Exception as exc:
            raise AgentExecutionError(f"A04输入DataPoint解析失败: {exc}") from exc

        try:
            stats = await self._repo.save_points(points, input.task_id)
        except Exception as exc:
            raise AgentExecutionError(f"A04入库失败: {exc}") from exc

        steps = [
            TraceStep(
                step=1,
                step_type="data_retrieval",
                description=(
                    f"入库 {stats['inserted']}/{stats['total']} 条"
                    f"（幂等跳过 {stats['skipped']} 条），血缘task_id={input.task_id}"
                ),
                data_refs=[p.data_id for p in points],
            )
        ]

        return AgentOutput(
            task_id=input.task_id,
            agent_id=self.agent_id,
            conclusion=(
                f"存储完成：新增 {stats['inserted']} 条，幂等跳过 {stats['skipped']} 条"
                f"（共 {stats['total']} 条）"
            ),
            confidence=Confidence.HIGH,
            data_refs=[p.data_id for p in points],
            reasoning_steps=steps,
            result={"storage_stats": stats},
        )
