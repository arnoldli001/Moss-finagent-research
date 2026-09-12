"""core.base_agent 契约测试。"""

import pytest

from src.core.base_agent import BaseAgent
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence


class DummyAgent(BaseAgent):
    """最小可用Agent实现，用于验证BaseAgent契约。"""

    async def execute(self, input: AgentInput) -> AgentOutput:
        return AgentOutput(
            task_id=input.task_id,
            agent_id=self.agent_id,
            conclusion="ok",
            confidence=Confidence.HIGH,
        )

    def get_capabilities(self) -> dict:
        return {"name": "dummy"}

    def health_check(self) -> bool:
        return True


async def test_execute_returns_structured_output():
    agent = DummyAgent("A00_dummy")
    result = await agent.execute(AgentInput(task_id="t1", tenant_id="tenant_001"))

    assert result.agent_id == "A00_dummy"
    assert result.task_id == "t1"
    assert result.confidence == Confidence.HIGH
    assert result.trace_id.startswith("trace_")


def test_base_agent_cannot_instantiate():
    with pytest.raises(TypeError):
        BaseAgent("X")  # type: ignore[abstract]


def test_capabilities_and_health():
    agent = DummyAgent("A00_dummy")
    assert agent.get_capabilities() == {"name": "dummy"}
    assert agent.health_check() is True
