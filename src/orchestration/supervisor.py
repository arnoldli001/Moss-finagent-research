"""Supervisor编排引擎（LangGraph StateGraph）。

管线：supervisor(规划) → A01采集 → A02清洗 → A03校验 → A04入库 →
      并行分析(A08宏观/A09中观/A10微观/A11风险) → A17综合建议 → A18审计封存。

Agent通过工厂注入（测试可全部替换为Fake）；节点级异常吞入state.errors，
不中断整图；计划外Agent节点直接跳过（空更新）。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.state import ResearchState

ANALYSIS_AGENTS = ("A08_macro", "A09_meso", "A10_micro", "A11_fin_risk", "A12_compliance")
INFO_AGENTS = ("A05_verifier", "A06_extractor", "A07_sentiment")
DATA_PIPELINE_AGENTS = ("A02_data_cleaner", "A03_data_validator", "A04_data_storage")

# analysis_type → (采集指标, 参与分析Agent)
_PLANNING: dict[str, tuple[list[str], list[str]]] = {
    "macro": (["CPI", "PPI"], ["A08_macro"]),
    "industry": (["CPI", "PPI"], ["A09_meso"]),
    "stock": (["stock_close"], ["A10_micro", "A11_fin_risk", "A12_compliance"]),
    "news": ([], list(INFO_AGENTS)),
    "full": (["CPI", "PPI"], list(ANALYSIS_AGENTS)),
}


def plan_run(analysis_type: str, target: str, info_items: list | None = None) -> dict[str, Any]:
    """规则式Supervisor规划：决定采集指标与参与Agent（Demo用确定性路由）。

    携带info_items（新闻/公告/研报文本）时自动追加信息层Agent，
    与analysis_type无关；news类型为纯信息层管线（不采集数据点）。
    """
    analysis_type = analysis_type if analysis_type in _PLANNING else "full"
    indicators, agents = _PLANNING[analysis_type]
    agents = list(agents)  # 拷贝：_PLANNING为模块级共享配置，禁止原地修改
    resolved = [f"stock_close:{target}" if ind == "stock_close" else ind for ind in indicators]
    if analysis_type in ("stock", "full") and target:
        agents = list(dict.fromkeys(agents))  # 保序去重
    if info_items:
        agents += [a for a in INFO_AGENTS if a not in agents]
    return {
        "analysis_type": analysis_type,
        "target": target,
        "indicators": resolved,
        "agents": agents + ["A17_recommend", "A18_audit"],
    }


def _summary(output: AgentOutput) -> dict[str, Any]:
    return {
        "agent_id": output.agent_id,
        "conclusion": output.conclusion,
        "confidence": output.confidence.value,
        "data_refs": output.data_refs,
        "result": output.result,
    }


def build_research_graph(agents: dict[str, Any], *, chain_path: str, llm_audit_path: str):
    """构建并编译投研StateGraph。

    agents: agent_id → Agent实例（必须含A01-A04、A17、A18；
    A08-A11缺失时对应分支自动跳过）。
    """

    async def _run_agent(agent: Any, state: ResearchState, payload: dict[str, Any]) -> dict[str, Any]:
        output = await agent.execute(AgentInput(
            task_id=state["task_id"], tenant_id=state["tenant_id"], payload=payload,
        ))
        update: dict[str, Any] = {
            "agent_outputs": [_summary(output)],
            "data_refs": list(output.data_refs),
            "trace_ids": [output.trace_id],
        }
        return update, output

    def _node(agent_id: str, payload_fn, *, collect: bool = False):
        agent = agents.get(agent_id)

        async def node(state: ResearchState) -> dict[str, Any]:
            if agent is None:
                return {}  # 未注册的Agent直接跳过（可选分支）
            if agent_id in ANALYSIS_AGENTS and agent_id not in state["plan"]:
                return {}
            if agent_id in INFO_AGENTS and (
                agent_id not in state["plan"] or not state.get("info_items")
            ):
                return {}  # 信息层：计划未包含或无输入文本时跳过
            if agent_id in DATA_PIPELINE_AGENTS and not state.get("raw_points"):
                return {}  # 无采集数据时数据管线空转无意义（如news管线）
            try:
                (update, output) = await _run_agent(agent, state, payload_fn(state))
            except AgentExecutionError as exc:
                return {"errors": [f"{agent_id}: {exc}"]}
            except Exception as exc:  # noqa: BLE001 单节点失败不拖垮整图
                return {"errors": [f"{agent_id}: 意外异常 {exc}"]}

            if agent_id == "A02_data_cleaner":
                update["cleaned_points"] = list(output.result.get("data_points", []))
            elif agent_id == "A03_data_validator":
                update["validated_points"] = list(output.result.get("data_points", []))
                update["validation_report"] = output.result
            elif agent_id == "A04_data_storage":
                update["storage_stats"] = output.result.get("storage_stats")
            elif agent_id == "A05_verifier":
                update["verified_items"] = output.result
            elif agent_id == "A06_extractor":
                update["extracted_events"] = output.result
            return update

        node.__name__ = f"node_{agent_id}"
        return node

    async def supervisor_node(state: ResearchState) -> dict[str, Any]:
        plan = plan_run(state["analysis_type"], state["target"],
                        state.get("info_items"))
        return {"plan": plan["agents"], "analysis_type": plan["analysis_type"]}

    def _collect_payload(state: ResearchState) -> dict[str, Any]:
        return {"indicators": plan_run(state["analysis_type"], state["target"])["indicators"]}

    async def collect_node(state: ResearchState) -> dict[str, Any]:
        """A01：逐指标采集（单Agent多指标，采集结果全部进入raw_points）。"""
        collector = agents["A01_data_collector"]
        indicators = _collect_payload(state)["indicators"]
        updates: dict[str, Any] = {"raw_points": [], "agent_outputs": [],
                                   "data_refs": [], "trace_ids": [], "errors": []}
        for indicator in indicators:
            try:
                update, output = await _run_agent(
                    collector, state, {"indicator": indicator}
                )
            except AgentExecutionError as exc:
                updates["errors"].append(f"A01_data_collector({indicator}): {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                updates["errors"].append(f"A01_data_collector({indicator}): 意外异常 {exc}")
                continue
            updates["raw_points"] += list(output.result.get("data_points", []))
            updates["agent_outputs"] += update["agent_outputs"]
            updates["data_refs"] += update["data_refs"]
            updates["trace_ids"] += update["trace_ids"]
        return updates

    async def recommend_node(state: ResearchState) -> dict[str, Any]:
        analyses = [o for o in state["agent_outputs"] if o["agent_id"] in ANALYSIS_AGENTS]
        if not analyses:  # 纯信息层管线（news）时以信息层结论综合
            analyses = [o for o in state["agent_outputs"] if o["agent_id"] in INFO_AGENTS]
        try:
            update, _ = await _run_agent(agents["A17_recommend"], state, {
                "analyses": analyses,
                "focus": state["target"],
                "user_query": state["user_query"],
            })
        except AgentExecutionError as exc:
            return {"errors": [f"A17_recommend: {exc}"], "final_report": None}
        return {**update, "final_report": None}

    async def audit_node(state: ResearchState) -> dict[str, Any]:
        try:
            update, output = await _run_agent(agents["A18_audit"], state, {
                "trace_id": state["task_id"],
                "agent_outputs": state["agent_outputs"],
                "chain_path": chain_path,
                "llm_audit_path": llm_audit_path,
            })
        except AgentExecutionError as exc:
            return {"errors": [f"A18_audit: {exc}"]}
        report = _render_report(state, output)
        return {**update, "final_report": report}

    g = StateGraph(ResearchState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("collect", collect_node)
    g.add_node("clean", _node("A02_data_cleaner", lambda s: {"data_points": s.get("raw_points", [])}))
    g.add_node("validate", _node("A03_data_validator", lambda s: {"data_points": s.get("cleaned_points", [])}))
    g.add_node("store", _node("A04_data_storage", lambda s: {"data_points": s.get("validated_points", [])}))
    g.add_node("verify_info", _node("A05_verifier", lambda s: {"info_items": s.get("info_items", [])}))
    g.add_node("extract_events", _node("A06_extractor", lambda s: {
        "info_items": [i for i in (s.get("verified_items") or {}).get("items", [])
                       if i.get("verified")],
    }))
    g.add_node("sentiment", _node("A07_sentiment", lambda s: {
        "events": (s.get("extracted_events") or {}).get("events", []),
    }))
    for aid in ANALYSIS_AGENTS:
        g.add_node(f"analyze_{aid}", _node(
            aid,
            lambda s, _a=aid: {
                "focus": s["target"],
                "data_points": s.get("validated_points", []),
                "hint": s.get("analysis_hint", {}),
                "events": (s.get("extracted_events") or {}).get("events", []),
            },
        ))
    g.add_node("recommend", recommend_node)
    g.add_node("audit", audit_node)

    g.add_edge(START, "supervisor")
    g.add_edge("supervisor", "collect")
    g.add_edge("collect", "clean")
    g.add_edge("clean", "validate")
    g.add_edge("validate", "store")
    # 信息层串行链：去伪 → 提取 → 舆情（无info_items时全部空跳过）
    g.add_edge("store", "verify_info")
    g.add_edge("verify_info", "extract_events")
    g.add_edge("extract_events", "sentiment")
    for aid in ANALYSIS_AGENTS:
        g.add_edge("sentiment", f"analyze_{aid}")
        g.add_edge(f"analyze_{aid}", "recommend")
    g.add_edge("recommend", "audit")
    g.add_edge("audit", END)

    return g.compile()


def _render_report(state: ResearchState, audit_output: AgentOutput) -> str:
    """把各Agent结论拼装成带溯源与免责声明的最终Markdown报告。"""
    lines = [f"# 投研分析报告：{state['target'] or state['user_query']}", ""]
    info_outs = [o for o in state["agent_outputs"] if o["agent_id"] in INFO_AGENTS]
    if info_outs:
        lines += ["## 信息核验与舆情"]
        for o in info_outs:
            lines.append(f"- **{o['agent_id']}**（{o['confidence']}）：{o['conclusion']}")
        lines.append("")
    for o in state["agent_outputs"]:
        if o["agent_id"] in ANALYSIS_AGENTS or o["agent_id"] == "A17_recommend":
            lines += [f"## {o['agent_id']}（置信度 {o['confidence']}）", o["conclusion"], ""]
    stats = state.get("storage_stats") or {}
    if stats:
        lines += ["## 数据入库", f"- 新增 {stats.get('inserted', 0)} 条，"
                  f"重复跳过 {stats.get('skipped', 0)} 条", ""]
    lines += ["## 审计", audit_output.conclusion, "",
              "---",
              "⚠️ 以上信息来自互联网公开资料，仅供研究参考，不构成投资建议。"
              "投资有风险，入市需谨慎，盈亏自负。"]
    return "\n".join(lines)
