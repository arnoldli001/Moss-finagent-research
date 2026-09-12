"""Supervisor编排引擎集成测试（全离线：仅A01用Fake，其余用真实Agent）。"""

import json

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence
from src.domain.agents.analysis.macro.agent import MacroAnalysisAgent
from src.domain.agents.analysis.micro.agent import MicroAnalysisAgent
from src.domain.agents.audit.verifier.agent import AuditAgent
from src.domain.agents.data.cleaner.agent import DataCleanerAgent
from src.domain.agents.data.storage.agent import DataStorageAgent
from src.domain.agents.data.validator.agent import DataValidatorAgent
from src.domain.agents.decision.recommend.agent import RecommendationAgent
from src.domain.agents.info.extractor import ExtractorAgent
from src.domain.agents.info.sentiment import SentimentAgent
from src.domain.agents.info.verifier import VerifierAgent
from src.infrastructure.repositories.audit_chain import ChainVerifier
from src.infrastructure.repositories.macro_repo import MacroRepository
from src.orchestration.supervisor import build_research_graph, plan_run


class FakeCollector:
    """A01替身：返回预置数据点，避免联网。"""

    def __init__(self, points_per_indicator: dict) -> None:
        self._points = points_per_indicator

    async def execute(self, input: AgentInput) -> AgentOutput:
        indicator = input.payload["indicator"]
        pts = self._points.get(indicator, [])
        return AgentOutput(
            task_id=input.task_id, agent_id="A01_data_collector",
            conclusion=f"采集 {indicator} {len(pts)} 条",
            confidence=Confidence.HIGH,
            data_refs=[p["data_id"] for p in pts],
            result={"indicator": indicator, "data_points": pts},
        )


def _dp(indicator: str, value: float | None, period: str) -> dict:
    return {"indicator": indicator, "value": value, "period_date": period,
            "source_name": "AkShare", "source_url": "https://x",
            "data_id": f"d_{indicator}_{period}"}


POINTS = {
    "CPI": [_dp("CPI", 2.1, "2026-08-01"), _dp("CPI", 2.0, "2026-07-01")],
    "PPI": [_dp("PPI", -0.8, "2026-08-01")],
}

MACRO_REPLY = {
    "conclusion": "CPI温和复苏", "confidence": "medium",
    "cycle_position": "复苏", "liquidity": "中性",
    "key_points": ["CPI 2.1%"], "risks": ["外需"],
}
MICRO_REPLY = {
    "conclusion": "估值处于低位", "confidence": "high",
    "moat_scores": {"brand": 9, "technology": 8, "cost": 7,
                    "network_effect": 8, "switching_cost": 8},
    "key_points": [], "risks": [],
}
REPLY17 = {
    "conclusion": "综合看多", "confidence": "high", "stance": "看多",
    "key_logic": ["[宏观] 复苏", "[微观] 低估"], "catalysts": [], "risks": [],
    "monitoring_points": ["PMI"], "conflicts_resolved": [],
}
INFO_REPLY5 = {"reviews": [{"item_id": "info_1", "verdict": "可信",
                            "red_flags": [], "reasoning": "官方数据"}]}
INFO_REPLY6 = {"events": [{"item_id": "info_1", "event_type": "policy",
                           "subject": "中国宏观", "direction": "positive",
                           "magnitude": "通胀温和", "event_date": "2026-09-09",
                           "evidence_quote": "CPI同比上涨0.4%", "confidence": 0.9}]}
INFO_REPLY7 = {"conclusion": "情绪偏暖", "confidence": "medium",
               "sentiment_phase": "乐观", "narrative": "官方数据提振"}


class FakeGateway:
    def __init__(self) -> None:
        self.replies: dict[str, dict] = {}
        self.calls: list[str] = []

    def set(self, system_marker: str, reply: dict) -> None:
        self.replies[system_marker] = reply

    async def complete(self, task_tier, system, prompt, **kwargs):
        from src.infrastructure.llm.models import LLMResponse

        for marker, reply in self.replies.items():
            if marker in system:
                self.calls.append(marker)
                return LLMResponse(
                    content=json.dumps(reply, ensure_ascii=False),
                    model_used="fake", provider="fake",
                    prompt_hash="ph", response_hash="rh",
                )
        raise AssertionError(f"未预置的系统提示: {system[:30]}")


def _build(tmp_dir):
    gw = FakeGateway()
    gw.set("宏观分析师", MACRO_REPLY)
    gw.set("股票分析师", MICRO_REPLY)
    gw.set("投研委员会主席", REPLY17)
    repo = MacroRepository(db_path=f"{tmp_dir}/test.db")
    agents = {
        "A01_data_collector": FakeCollector(POINTS),
        "A02_data_cleaner": DataCleanerAgent(),
        "A03_data_validator": DataValidatorAgent(),
        "A04_data_storage": DataStorageAgent(repo),
        "A08_macro": MacroAnalysisAgent(gw),
        "A10_micro": MicroAnalysisAgent(gw),
        "A17_recommend": RecommendationAgent(gw),
        "A18_audit": AuditAgent(),
    }
    graph = build_research_graph(
        agents, chain_path=f"{tmp_dir}/chain.jsonl",
        llm_audit_path=f"{tmp_dir}/llm.jsonl",
    )
    return graph, gw, repo


def _state(**over):
    base = {
        "task_id": "task_e2e", "tenant_id": "tenant_001",
        "user_query": "分析当前宏观与茅台", "analysis_type": "full",
        "target": "600519",
        "plan": [], "raw_points": [], "cleaned_points": [], "validated_points": [],
        "validation_report": {}, "storage_stats": {},
        "info_items": [], "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }
    return {**base, **over}


async def test_full_graph_pipeline(tmp_dir):
    graph, gw, repo = _build(tmp_dir)
    final = await graph.ainvoke(_state())

    # 数据管线
    assert len(final["raw_points"]) == 3
    assert final["storage_stats"]["inserted"] == 3
    assert (await repo.count_by_indicator()).get("CPI", 0) >= 1
    # 并行分析 + 综合 + 审计全聚合
    aids = {o["agent_id"] for o in final["agent_outputs"]}
    assert {"A01_data_collector", "A02_data_cleaner", "A03_data_validator",
            "A04_data_storage", "A08_macro", "A10_micro",
            "A17_recommend", "A18_audit"} <= aids
    # LLM按层级被调用（宏观+微观+建议=3次fake调用）
    assert len(gw.calls) == 3
    # 报告带免责声明与审计结论
    assert final["final_report"]
    assert "不构成投资建议" in final["final_report"]
    assert "审计" in final["final_report"]
    # 审计已封存上链且链完整
    assert ChainVerifier(f"{tmp_dir}/chain.jsonl").verify()["valid"]
    assert not final["errors"]


async def test_plan_routes_by_analysis_type():
    stock_plan = plan_run("stock", "600519")
    assert stock_plan["indicators"] == ["stock_close:600519"]
    assert "A10_micro" in stock_plan["agents"] and "A08_macro" not in stock_plan["agents"]

    macro_plan = plan_run("macro", "")
    assert macro_plan["indicators"] == ["CPI", "PPI"]
    assert "A10_micro" not in macro_plan["agents"]

    unknown = plan_run("whatever", "x")
    assert unknown["analysis_type"] == "full"  # 未知类型兜底full


async def test_collector_error_is_contained(tmp_dir):
    class ExplodingCollector:
        async def execute(self, input: AgentInput) -> AgentOutput:
            raise RuntimeError("网络炸了")

    gw = FakeGateway()
    gw.set("投研委员会主席", REPLY17)
    agents = {
        "A01_data_collector": ExplodingCollector(),
        "A02_data_cleaner": DataCleanerAgent(),
        "A03_data_validator": DataValidatorAgent(),
        "A04_data_storage": DataStorageAgent(MacroRepository(f"{tmp_dir}/x.db")),
        "A08_macro": MacroAnalysisAgent(gw),
        "A17_recommend": RecommendationAgent(gw),
        "A18_audit": AuditAgent(),
    }
    graph = build_research_graph(agents, chain_path=f"{tmp_dir}/c.jsonl",
                                 llm_audit_path=f"{tmp_dir}/l.jsonl")
    final = await graph.ainvoke(_state(analysis_type="macro"))

    assert any("A01" in e for e in final["errors"])      # 采集失败被吞入errors
    assert final["final_report"] is not None             # 图仍跑完
    assert "审计" in final["final_report"]
    # 宏观分析走空数据短路；仅A17对"无数据"摘要做了一次综合
    assert "宏观分析师" not in gw.calls
    assert "投研委员会主席" in gw.calls


async def test_news_graph_info_pipeline(tmp_dir):
    """news管线：信息层串行链全跑，数据管线因无raw_points跳过，审计无完整性问题。"""
    gw = FakeGateway()
    gw.set("信息核查员", INFO_REPLY5)
    gw.set("财经信息结构化专家", INFO_REPLY6)
    gw.set("市场舆情分析师", INFO_REPLY7)
    gw.set("投研委员会主席", REPLY17)
    agents = {
        "A01_data_collector": FakeCollector(POINTS),
        "A02_data_cleaner": DataCleanerAgent(),
        "A03_data_validator": DataValidatorAgent(),
        "A04_data_storage": DataStorageAgent(MacroRepository(f"{tmp_dir}/news.db")),
        "A05_verifier": VerifierAgent(gw),
        "A06_extractor": ExtractorAgent(gw),
        "A07_sentiment": SentimentAgent(gw),
        "A17_recommend": RecommendationAgent(gw),
        "A18_audit": AuditAgent(),
    }
    graph = build_research_graph(agents, chain_path=f"{tmp_dir}/nc.jsonl",
                                 llm_audit_path=f"{tmp_dir}/nl.jsonl")
    final = await graph.ainvoke(_state(
        analysis_type="news", target="",
        info_items=[{"source_name": "国家统计局",
                     "publish_time": "2026-09-10T09:00:00+08:00",
                     "text": "8月CPI同比上涨0.4%"}],
    ))

    aids = {o["agent_id"] for o in final["agent_outputs"]}
    assert {"A05_verifier", "A06_extractor", "A07_sentiment",
            "A17_recommend", "A18_audit"} <= aids
    # 数据管线与A01在news管线下整体跳过
    assert not ({"A01_data_collector", "A02_data_cleaner", "A03_data_validator",
                 "A04_data_storage"} & aids)
    assert final["verified_items"]["stats"]["verified"] == 1
    assert final["extracted_events"]["stats"]["total"] == 1
    # 报告含信息层区块与免责声明
    assert "信息核验与舆情" in final["final_report"]
    assert "不构成投资建议" in final["final_report"]
    # A18无completeness问题 → 审计通过
    audit = next(o for o in final["agent_outputs"] if o["agent_id"] == "A18_audit")
    assert "审计通过" in audit["conclusion"]
    assert not final["errors"]
    assert ChainVerifier(f"{tmp_dir}/nc.jsonl").verify()["valid"]
