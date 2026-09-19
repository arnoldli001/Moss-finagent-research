"""A01 分析逻辑（纯函数）：推理步骤与结论摘要构造，不依赖外部服务。"""

from __future__ import annotations

from src.core.schemas import DataPoint, TraceStep


def build_reasoning_steps(
    indicator: str, count: int, data_refs: list[str], duration_ms: int
) -> list[TraceStep]:
    """构造采集步骤Trace。"""
    return [
        TraceStep(
            step=1,
            step_type="data_retrieval",
            description=f"从数据源获取 {indicator} 共 {count} 条数据",
            data_refs=data_refs,
            duration_ms=duration_ms,
        )
    ]


def build_collection_summary(indicator: str, points: list[DataPoint]) -> str:
    """生成采集结论摘要（含最新期间与来源，便于上层聚合）。"""
    if not points:
        return f"未获取到 {indicator} 数据"
    periods = [p.period_date for p in points if p.period_date]
    latest = max(periods) if periods else "未知期间"
    source = points[0].source_name or "未知来源"
    return f"成功采集 {indicator} 共 {len(points)} 条，最新期间 {latest}，来源 {source}"
