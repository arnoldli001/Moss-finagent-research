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
    """解析后的标的标识（个股为6位代码，如300308；宏观/行业为主题词）"""
    target_display: str
    """标的展示名（如中文简称'中际旭创'），报告标题与Agent focus使用；无则回退target"""

    # Supervisor计划（本run要执行的agent_id列表）
    plan: list[str]

    # 数据管线流转（collector→cleaner→validator→storage各写一次，add聚合）
    raw_points: Annotated[list[dict[str, Any]], operator.add]
    cleaned_points: Annotated[list[dict[str, Any]], operator.add]
    validated_points: Annotated[list[dict[str, Any]], operator.add]
    validation_report: dict[str, Any]
    storage_stats: dict[str, Any]

    # 信息层管线（A05→A06→A07串行链；info_items为任务创建时直接注入）
    info_items: list[dict[str, Any]]
    verified_items: dict[str, Any]
    extracted_events: dict[str, Any]

    # 本地量化研判参考（流动性周期skill产出，分析层与A17共享）
    analysis_hint: dict[str, Any]

    # 并行Agent输出聚合
    agent_outputs: Annotated[list[dict[str, Any]], operator.add]
    data_refs: Annotated[list[str], operator.add]
    trace_ids: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]

    # Agent间多轮对话（A2A）：sender→receiver的问答记录，供前端展示协作过程
    agent_messages: Annotated[list[dict[str, Any]], operator.add]

    # 实时进度（running时前端展示，node返回值通过add累积，读取取最后一项）
    progress: Annotated[list[str], operator.add]

    # 取消令牌（注入CancellationToken实例，各节点入口检查）
    cancellation_token: Any

    final_report: str | None
