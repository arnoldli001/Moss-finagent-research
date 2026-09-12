"""API运行时：默认Agent注册表与图的组装（DI工厂）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.config import get_settings
from src.domain.agents.analysis.compliance import ComplianceAnalysisAgent
from src.domain.agents.analysis.macro.agent import MacroAnalysisAgent
from src.domain.agents.analysis.meso.agent import MesoAnalysisAgent
from src.domain.agents.analysis.micro.agent import MicroAnalysisAgent
from src.domain.agents.analysis.risk.agent import RiskAnalysisAgent
from src.domain.agents.audit.verifier.agent import AuditAgent
from src.domain.agents.data.collector.agent import DataCollectorAgent
from src.domain.agents.data.cleaner.agent import DataCleanerAgent
from src.domain.agents.data.storage.agent import DataStorageAgent
from src.domain.agents.data.validator.agent import DataValidatorAgent
from src.domain.agents.decision.recommend.agent import RecommendationAgent
from src.domain.agents.info.extractor import ExtractorAgent
from src.domain.agents.info.sentiment import SentimentAgent
from src.domain.agents.info.verifier import VerifierAgent
from src.infrastructure.connectors.akshare_connector import AkshareConnector
from src.infrastructure.llm import LLMGateway
from src.infrastructure.repositories.macro_repo import MacroRepository
from src.orchestration.supervisor import build_research_graph


@dataclass
class Runtime:
    """应用运行时句柄（挂在app.state上）。"""

    gateway: LLMGateway
    repo: MacroRepository
    agents: dict[str, Any]
    graph: Any


def build_runtime() -> Runtime:
    """按生产默认配置组装全部Agent与StateGraph。"""
    settings = get_settings()
    gateway = LLMGateway(settings=settings)
    repo = MacroRepository(db_path="data/finagent.db")
    agents: dict[str, Any] = {
        "A01_data_collector": DataCollectorAgent(AkshareConnector()),
        "A02_data_cleaner": DataCleanerAgent(),
        "A03_data_validator": DataValidatorAgent(),
        "A04_data_storage": DataStorageAgent(repo),
        "A05_verifier": VerifierAgent(gateway),
        "A06_extractor": ExtractorAgent(gateway),
        "A07_sentiment": SentimentAgent(gateway),
        "A08_macro": MacroAnalysisAgent(gateway),
        "A09_meso": MesoAnalysisAgent(gateway),
        "A10_micro": MicroAnalysisAgent(gateway),
        "A11_fin_risk": RiskAnalysisAgent(gateway),
        "A12_compliance": ComplianceAnalysisAgent(gateway),
        "A17_recommend": RecommendationAgent(gateway),
        "A18_audit": AuditAgent(),
    }
    graph = build_research_graph(
        agents,
        chain_path=f"{settings.llm_audit_dir}/audit_chain.jsonl",
        llm_audit_path=f"{settings.llm_audit_dir}/llm_audit.jsonl",
    )
    return Runtime(gateway=gateway, repo=repo, agents=agents, graph=graph)


def agent_health(agents: dict[str, Any]) -> dict[str, str]:
    """聚合全部Agent的health_check。"""
    return {
        agent_id: ("healthy" if agent.health_check() else "unhealthy")
        for agent_id, agent in sorted(agents.items())
    }
