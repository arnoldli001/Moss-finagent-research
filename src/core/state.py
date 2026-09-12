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

    # 并行Agent输出聚合
    agent_outputs: Annotated[list[dict[str, Any]], operator.add]
    data_refs: Annotated[list[str], operator.add]
    trace_ids: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]

    final_report: str | None
