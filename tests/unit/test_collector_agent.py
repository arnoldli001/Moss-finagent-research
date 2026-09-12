"""A01 数据采集Agent测试（FakeBackend注入，不联网）。"""

import pytest

from src.core.exceptions import AgentExecutionError, DataFetchError
from src.core.models import AgentInput
from src.core.schemas import DataPoint
from src.domain.agents.data.collector.agent import DataCollectorAgent


class FakeBackend:
    """内存后端：返回预置数据，可注入异常。"""

    def __init__(self, points: list[DataPoint] | None = None, error: Exception | None = None):
        self._points = points or []
        self._error = error

    async def fetch(self, indicator, start_date=None, end_date=None):
        if self._error:
            raise self._error
        return self._points

    def get_capabilities(self):
        return {"name": "fake", "indicators": ["CPI"]}


def _make_input(indicator: str = "CPI") -> AgentInput:
    return AgentInput(
        task_id="t1", tenant_id="tenant_001", payload={"indicator": indicator}
    )


async def test_execute_returns_data_points_in_result():
    backend = FakeBackend(
        points=[
            DataPoint(indicator="CPI", value=2.1, period_date="2026-08"),
            DataPoint(indicator="CPI", value=2.0, period_date="2026-07"),
        ]
    )
    output = await DataCollectorAgent(backend).execute(_make_input())

    assert output.agent_id == "A01_data_collector"
    assert len(output.data_refs) == 2
    assert len(output.result["data_points"]) == 2
    assert output.result["indicator"] == "CPI"
    assert "最新期间" in output.conclusion
    assert output.reasoning_steps[0].step_type == "data_retrieval"


async def test_execute_empty_result_low_confidence():
    output = await DataCollectorAgent(FakeBackend()).execute(_make_input())
    assert output.confidence.value == "low"
    assert "未获取到" in output.conclusion


async def test_execute_wraps_backend_error():
    backend = FakeBackend(error=DataFetchError("akshare未安装"))
    with pytest.raises(AgentExecutionError):
        await DataCollectorAgent(backend).execute(_make_input())


def test_capabilities_expose_backend():
    agent = DataCollectorAgent(FakeBackend())
    caps = agent.get_capabilities()
    assert "fetch_api" in caps["capabilities"]
    assert caps["backend"]["name"] == "fake"
    assert agent.health_check() is True
