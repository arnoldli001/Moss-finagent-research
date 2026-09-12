"""LangGraph共享状态定义（Supervisor编排用）。"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class ResearchState(TypedDict):
    """投研任务的LangGraph共享状态。

    列表字段使用 add reducer，支持无依赖Agent并行执行后自动聚合输出。
    """

    task_id: str
    tenant_id: str
    user_query: str
    analysis_type: str
    target: str

    # Supervisor计划（本run要执行的agent_id列表）
    plan: list[str]

    # 数据管线流转（collector→cleaner→validator→storage各写一次，add聚合）
    raw_points: Annotated[list[dict[str, Any]], operator.add]
    cleaned_points: Annotated[list[dict[str, Any]], operator.add]
    validated_points: Annotated[list[dict[str, Any]], operator.add]
    validation_report: dict[str, Any]
    storage_stats: dict[str, Any]

    # 并行Agent输出聚合
    agent_outputs: Annotated[list[dict[str, Any]], operator.add]
    data_refs: Annotated[list[str], operator.add]
    trace_ids: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]

    final_report: str | None
