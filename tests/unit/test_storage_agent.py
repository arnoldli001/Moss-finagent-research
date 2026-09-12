"""A04 存储Agent + SQLite仓储测试（tmp真实库）。"""

import pytest

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput
from src.core.schemas import DataPoint
from src.domain.agents.data.storage.agent import DataStorageAgent


def _points() -> list[DataPoint]:
    return [
        DataPoint(
            indicator="CPI", value=2.1, period_date="2026-08-01",
            source_name="AkShare", source_url="https://akshare.akfamily.xyz",
        ),
        DataPoint(
            indicator="CPI", value=2.0, period_date="2026-07-01",
            source_name="AkShare", source_url="https://akshare.akfamily.xyz",
        ),
    ]


def _payload_input(points: list[DataPoint], task_id: str = "t4") -> AgentInput:
    return AgentInput(
        task_id=task_id,
        tenant_id="tenant_001",
        payload={"data_points": [p.model_dump(mode="json") for p in points]},
    )


async def test_save_and_query_roundtrip(repo):
    await repo.ensure_schema()
    stats = await repo.save_points(_points(), "task_1")
    assert stats == {"inserted": 2, "skipped": 0, "total": 2}

    loaded = await repo.query_points("CPI", start_date="2026-07-01", end_date="2026-08-01")
    assert len(loaded) == 2
    assert loaded[0].period_date == "2026-07-01"
    assert loaded[0].value == 2.0
    assert loaded[0].source_name  # 溯源字段完整


async def test_idempotent_save(repo):
    await repo.ensure_schema()
    await repo.save_points(_points(), "task_1")
    stats = await repo.save_points(_points(), "task_2")  # 同键重复
    assert stats["inserted"] == 0
    assert stats["skipped"] == 2


async def test_storage_agent_end_to_end(repo):
    agent = DataStorageAgent(repo)
    output = await agent.execute(_payload_input(_points()))
    assert "新增 2 条" in output.conclusion
    assert output.result["storage_stats"]["inserted"] == 2

    # 血缘：存储的行带task_id（通过重复入库验证幂等即血缘保留）
    again = await agent.execute(_payload_input(_points(), task_id="t4b"))
    assert again.result["storage_stats"]["skipped"] == 2


async def test_storage_agent_invalid_payload(repo):
    agent = DataStorageAgent(repo)
    bad = AgentInput(task_id="t5", tenant_id="x", payload={"data_points": [{"no_indicator": 1}]})
    with pytest.raises(AgentExecutionError):
        await agent.execute(bad)
