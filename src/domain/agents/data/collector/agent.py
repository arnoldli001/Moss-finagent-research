"""A01 数据采集Agent：从数据源获取原始数据并携带溯源元数据。

无LLM（configs/agents.yaml model=none）；数据后端通过FetchBackend结构化
协议注入（infrastructure.connectors实现），domain不直接依赖infrastructure。
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, DataPoint
from src.domain.agents.data.collector.logic import (
    build_collection_summary,
    build_reasoning_steps,
)
from src.domain.agents.data.collector.models import CollectorPayload


class FetchBackend(Protocol):
    """采集后端结构化协议（duck typing，避免domain→infrastructure反向依赖）。"""

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


class DataCollectorAgent(BaseAgent):
    """A01：按指标拉取原始数据，输出DataPoint列表（含溯源元数据）。"""

    def __init__(self, backend: FetchBackend, agent_id: str = "A01_data_collector") -> None:
        super().__init__(agent_id)
        self._backend = backend

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "capabilities": ["fetch_api", "web_crawl", "file_download"],
            "backend": self._backend.get_capabilities(),
        }

    def health_check(self) -> bool:
        try:
            return bool(self._backend.get_capabilities())
        except Exception:
            return False

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = CollectorPayload.model_validate(input.payload)
        started = time.perf_counter()
        try:
            points = await self._backend.fetch(
                payload.indicator, payload.start_date, payload.end_date
            )
        except Exception as exc:
            raise AgentExecutionError(f"A01采集失败({payload.indicator}): {exc}") from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        data_refs = [p.data_id for p in points]
        has_publish_time = all(p.publish_time for p in points)
        confidence = (
            Confidence.HIGH
            if points and has_publish_time
            else (Confidence.MEDIUM if points else Confidence.LOW)
        )

        return AgentOutput(
            task_id=input.task_id,
            agent_id=self.agent_id,
            conclusion=build_collection_summary(payload.indicator, points),
            confidence=confidence,
            data_refs=data_refs,
            reasoning_steps=build_reasoning_steps(
                payload.indicator, len(points), data_refs, duration_ms
            ),
            result={
                "indicator": payload.indicator,
                "data_points": [p.model_dump(mode="json") for p in points],
            },
        )
