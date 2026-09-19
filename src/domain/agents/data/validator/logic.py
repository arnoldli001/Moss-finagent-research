"""A03 校验逻辑（纯函数）：范围检查、逻辑检查、离群检测，不依赖外部服务。"""

from __future__ import annotations

import statistics

from pydantic import BaseModel

from src.core.schemas import DataPoint

# 指标同比合理界（|value|超过视为可疑，单位%）
RANGE_LIMITS: dict[str, float] = {
    "CPI": 15.0,
    "PPI": 20.0,
}
DEFAULT_RANGE_LIMIT = 50.0
ANOMALY_MIN_SAMPLES = 5
ANOMALY_SIGMA = 3.0


class ValidationIssue(BaseModel):
    """单条校验问题记录。"""

    data_id: str
    check: str  # range/logic/anomaly
    detail: str


class ValidationReport(BaseModel):
    """校验汇总报告。"""

    passed_ids: list[str]
    issues: list[ValidationIssue]
    quality_score: float
    """通过率 0.0-1.0"""


def check_range(points: list[DataPoint]) -> list[ValidationIssue]:
    """值域检查：超出指标合理界标记可疑。"""
    issues: list[ValidationIssue] = []
    for point in points:
        if point.value is None:
            continue
        limit = RANGE_LIMITS.get(point.indicator.split(":")[0], DEFAULT_RANGE_LIMIT)
        if abs(point.value) > limit:
            issues.append(
                ValidationIssue(
                    data_id=point.data_id,
                    check="range",
                    detail=f"{point.indicator} 值 {point.value} 超出合理界 ±{limit}",
                )
            )
    return issues


def check_logic(points: list[DataPoint]) -> list[ValidationIssue]:
    """逻辑检查：数值型指标缺少value或period_date。"""
    issues: list[ValidationIssue] = []
    for point in points:
        if point.value is None:
            issues.append(
                ValidationIssue(data_id=point.data_id, check="logic", detail="主数值缺失")
            )
        elif point.period_date is None:
            issues.append(
                ValidationIssue(data_id=point.data_id, check="logic", detail="期间缺失")
            )
    return issues


def check_anomaly(points: list[DataPoint]) -> list[ValidationIssue]:
    """离群检测：同指标内偏离中位数超过N倍标准差（样本不足时跳过）。"""
    issues: list[ValidationIssue] = []
    by_indicator: dict[str, list[float]] = {}
    for point in points:
        if point.value is not None:
            by_indicator.setdefault(point.indicator, []).append(point.value)

    for indicator, values in by_indicator.items():
        if len(values) < ANOMALY_MIN_SAMPLES:
            continue
        median = statistics.median(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        if stdev == 0.0:
            continue
        for point in points:
            if point.indicator != indicator or point.value is None:
                continue
            z = abs(point.value - median) / stdev
            if z > ANOMALY_SIGMA:
                issues.append(
                    ValidationIssue(
                        data_id=point.data_id,
                        check="anomaly",
                        detail=f"{indicator} 值 {point.value} 偏离中位数 {z:.1f}σ",
                    )
                )
    return issues


def run_validation(points: list[DataPoint]) -> ValidationReport:
    """执行全部校验，返回报告。"""
    issues = check_range(points) + check_logic(points) + check_anomaly(points)
    bad_ids = {issue.data_id for issue in issues}
    passed = [p.data_id for p in points if p.data_id not in bad_ids]
    total = len(points) or 1
    return ValidationReport(
        passed_ids=passed,
        issues=issues,
        quality_score=round(len(passed) / total, 4),
    )
