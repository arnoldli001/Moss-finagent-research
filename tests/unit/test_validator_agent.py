"""A03 数据校验Agent测试。"""

from src.core.models import AgentInput
from src.core.schemas import DataPoint
from src.domain.agents.data.validator.agent import DataValidatorAgent
from src.domain.agents.data.validator.logic import check_range, run_validation


def _pt(value: float | None, indicator: str = "CPI", period: str | None = "2026-08") -> DataPoint:
    return DataPoint(indicator=indicator, value=value, period_date=period)


def test_check_range_flags_outlier():
    issues = check_range([_pt(99.0)])  # CPI合理界±15
    assert len(issues) == 1
    assert issues[0].check == "range"


def test_run_validation_mixed():
    points = [_pt(2.1), _pt(2.0), _pt(None), _pt(99.0)]
    report = run_validation(points)
    assert len(report.passed_ids) == 2
    assert report.quality_score == 0.5
    checks = {i.check for i in report.issues}
    assert "logic" in checks and "range" in checks


async def test_validator_agent_downgrades_bad_points():
    raw = [_pt(2.1), _pt(None)]
    agent_input = AgentInput(
        task_id="t3",
        tenant_id="tenant_001",
        payload={"data_points": [p.model_dump(mode="json") for p in raw]},
    )
    output = await DataValidatorAgent().execute(agent_input)

    assert "质量评分" in output.conclusion
    bad = next(p for p in output.result["data_points"] if p["value"] is None)
    good = next(p for p in output.result["data_points"] if p["value"] is not None)
    assert bad["confidence"] < good["confidence"]
    assert bad["extra"]["validation_issues"]
    assert output.result["quality_score"] == 0.5
