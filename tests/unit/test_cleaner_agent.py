"""A02 数据清洗Agent测试（纯逻辑，无外部依赖）。"""

from src.core.models import AgentInput
from src.core.schemas import DataPoint
from src.domain.agents.data.cleaner.agent import DataCleanerAgent
from src.domain.agents.data.cleaner.logic import normalize_period


def _payload_input(points: list[DataPoint], task_id: str = "t2") -> AgentInput:
    return AgentInput(
        task_id=task_id,
        tenant_id="tenant_001",
        payload={"data_points": [p.model_dump(mode="json") for p in points]},
    )


def test_normalize_period_formats():
    assert normalize_period("2026-08") == "2026-08-01"
    assert normalize_period("20260801") == "2026-08-01"
    assert normalize_period("2026年8月") == "2026-08-01"
    assert normalize_period("2026-08-01") == "2026-08-01"
    assert normalize_period("未知") == "未知"
    assert normalize_period(None) is None


async def test_execute_normalizes_and_dedupes():
    raw = [
        DataPoint(indicator="CPI", value=2.1, period_date="2026-08"),
        DataPoint(indicator="CPI", value=2.1, period_date="2026年8月"),  # 同期重复
        DataPoint(indicator="CPI", value=2.0, period_date="20260701"),
    ]
    output = await DataCleanerAgent().execute(_payload_input(raw))

    cleaned = output.result["data_points"]
    assert len(cleaned) == 2  # 3进2出
    assert len(output.result["removed_ids"]) == 1
    assert cleaned[0]["period_date"] == "2026-08-01"  # ISO标准化
    assert cleaned[0]["processed_by"] == "A02_data_cleaner"
    assert "剔除 1 条" in output.conclusion
    assert {s.step_type for s in output.reasoning_steps} == {
        "indicator_calculation",
        "cross_validation",
    }


async def test_execute_missing_value_gets_lower_confidence():
    raw = [DataPoint(indicator="CPI", value=None, period_date="2026-08")]
    output = await DataCleanerAgent().execute(_payload_input(raw))
    assert output.result["data_points"][0]["confidence"] == 0.6


async def test_execute_empty_input():
    output = await DataCleanerAgent().execute(_payload_input([]))
    assert output.result["data_points"] == []
    assert output.confidence.value == "low"
