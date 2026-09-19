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
from src.domain.agents.data.cleaner.agent import DataCleanerAgent
from src.domain.agents.data.collector.agent import DataCollectorAgent
from src.domain.agents.data.storage.agent import DataStorageAgent
from src.domain.agents.data.validator.agent import DataValidatorAgent
from src.domain.agents.decision.recommend.agent import RecommendationAgent
from src.domain.agents.engineering.code_engineer import CodeEngineerAgent
from src.domain.agents.industry import (
    ConsumerIndustryAgent,
    CyclicalIndustryAgent,
    PharmaIndustryAgent,
    TechIndustryAgent,
)
from src.domain.agents.info.extractor import ExtractorAgent
from src.domain.agents.info.sentiment import SentimentAgent
from src.domain.agents.info.verifier import VerifierAgent
from src.domain.skills.library import SkillLibrary
from src.infrastructure.connectors.a_share_liquidity_connector import (
    AShareLiquidityConnector,
)
from src.infrastructure.connectors.akshare_connector import AkshareConnector
from src.infrastructure.connectors.dynamic_loader import get_dynamic_loader
from src.infrastructure.connectors.fedwatch_connector import FedWatchConnector
from src.infrastructure.connectors.index_valuation_connector import (
    IndexValuationConnector,
)
from src.infrastructure.connectors.local_csv_connector import LocalCsvConnector
from src.infrastructure.connectors.margin_trading_connector import (
    MarginTradingConnector,
)
from src.infrastructure.connectors.mock_industry_connector import MockIndustryConnector
from src.infrastructure.connectors.news_fetcher import AkshareNewsFetcher
from src.infrastructure.connectors.northbound_flow_connector import (
    NorthboundFlowConnector,
)
from src.infrastructure.connectors.penetration_rate_connector import (
    PenetrationRateConnector,
)
from src.infrastructure.connectors.real_industry_connector import RealTechIndustryConnector
from src.infrastructure.connectors.router import ConnectorRouter
from src.infrastructure.connectors.star_chinext_connector import (
    StarChinextConnector,
)
from src.infrastructure.connectors.sw_industry_valuation_connector import (
    SWIndustryValuationConnector,
)
from src.infrastructure.connectors.tencent_daily_connector import (
    TencentDailyConnector,
)
from src.infrastructure.connectors.tushare_connector import TushareConnector
from src.infrastructure.connectors.xtquant_connector import XtQuantConnector
from src.infrastructure.llm import LLMGateway
from src.infrastructure.repositories.base import DataPointRepository
from src.infrastructure.repositories.fund_flow_sqlite_repo import (
    build_fund_flow_repository,
)
from src.infrastructure.repositories.repository_factory import (
    build_intraday_profile_repository,
    build_repository,
)
from src.intraday.service import IntradayService
from src.orchestration.planner import LLMSupervisorPlanner
from src.orchestration.supervisor import build_research_graph


@dataclass
class Runtime:
    """应用运行时句柄（挂在app.state上）。"""

    gateway: LLMGateway
    repo: DataPointRepository
    agents: dict[str, Any]
    graph: Any
    backend: Any
    news_fetcher: Any = None
    # 事件告警子系统（postgres后端或装配失败时为None，接口降级标注）
    event_repo: Any = None
    event_service: Any = None
    alert_hub: Any = None
    email_notifier: Any = None
    # 做T辅助子系统（多因子打分 + 做T信号；装配失败时为None，接口返回503）
    intraday: Any = None
    # 做T权重档案仓储（用户按个股股性保存的权重/档位；装配失败时为None）
    intraday_profile_repo: Any = None
    # 资金流监控（板块/个股大资金动向；装配失败时为None，接口返回503）
    fundflow: Any = None
    fundflow_repo: Any = None
    # 量化选股（3 档模型定时选股 + 自定义板块；装配失败时为None，接口返回503）
    quant_select: Any = None


def build_runtime() -> Runtime:
    """按生产默认配置组装全部Agent与StateGraph。"""
    settings = get_settings()
    gateway = LLMGateway(settings=settings)
    repo = build_repository(settings)
    # 采集后端（有序路由，首个supports命中者处理，失败抛错由上层记录）：
    # 1) QMT本地终端日线（全历史，XtMiniQmt需运行；未启动自动回退）
    # 2) 本地QMT导出CSV（LOCAL_QUOTE_DIR配置后启用，QMT服务未开时的本地兜底）
    # 3) AkShare在线：CPI/PPI/M2/社融/行情兜底/个股PE/PB/财务比率/社零/煤价真实序列
    # 4) 腾讯财经日K（独立于东财/新浪的通道，前复权）
    # 5) Tushare Pro个股日线（在线兜底；前4个源都拿不到或都比本地DB旧时才用）
    # 6) 模拟产业数据(其余ind:前缀，付费产业接口接入前占位，三重模拟标记)
    qmt = XtQuantConnector()
    routes: list[tuple[Any, Any]] = [(qmt, XtQuantConnector.supports)]
    if settings.local_quote_dir:
        csv_connector = LocalCsvConnector(settings.local_quote_dir)
        routes.append((csv_connector, LocalCsvConnector.supports))
    akshare = AkshareConnector()
    routes.append((akshare, AkshareConnector.supports))
    # 腾讯财经日K：**独立于东财/新浪的通道**。实测（2026-09-17）AkShare 的东财主源
    # 被阻断、新浪回退断连**同时**发生，而腾讯通道正常（做T面板的实时链路一直走它）。
    tencent_daily = TencentDailyConnector()
    routes.append((tencent_daily, TencentDailyConnector.supports))
    # Tushare Pro 个股日线：**日线链的最后一道在线兜底**。
    # 实测 QMT 一掉线，链上就没有源能给出 300308 的日线（本地CSV没有这只票的文件、
    # AkShare 两个子源同时失败）；Tushare 0.14 秒返回最新 2026-09-16（前复权，与 QMT 一致）。
    # 放在最后：前面的源够新就不会打它（不增加常态延迟）。
    tushare_connector = TushareConnector()
    routes.append((tushare_connector, TushareConnector.supports))
    # 申万行业估值（一级/二级/三级PE/PB/股息率截面，AKShare免费接口）
    sw_valuation = SWIndustryValuationConnector()
    routes.append((sw_valuation, SWIndustryValuationConnector.supports))
    # 渗透率多源采集（静态基准+新闻提取+研报搜索）
    penetration = PenetrationRateConnector()
    routes.append((penetration, PenetrationRateConnector.supports))
    # A股大盘流动性：两市成交额/全A换手率（腾讯→东财双源，盘中实时）
    a_share_liq = AShareLiquidityConnector()
    routes.append((a_share_liq, AShareLiquidityConnector.supports))
    # 科创板/创业板板块数据：成交额/估值分位/个股截面（四类数据全主备冗余）
    star_chinext = StarChinextConnector()
    routes.append((star_chinext, StarChinextConnector.supports))
    # 核心宽基指数PE/PB历史分位（AKShare乐咕，日频）
    index_valuation = IndexValuationConnector()
    routes.append((index_valuation, IndexValuationConnector.supports))
    # 两市两融余额（交易所SSE/SZSE，T+1）
    margin_trading = MarginTradingConnector()
    routes.append((margin_trading, MarginTradingConnector.supports))
    # 北向资金（2024-08起日度净买额停披，诚实标注缺口）
    northbound = NorthboundFlowConnector()
    routes.append((northbound, NorthboundFlowConnector.supports))
    # CME FedWatch下次FOMC利率概率（外网不可达自动降级为空）
    fedwatch = FedWatchConnector()
    routes.append((fedwatch, FedWatchConnector.supports))
    # 科技行业真实产业数据：WSTS半导体销售额同比/统计局集成电路产量同比/中证全指半导体PE-TTM
    real_tech = RealTechIndustryConnector()
    routes.append((real_tech, RealTechIndustryConnector.supports))
    mock_industry = MockIndustryConnector()
    routes.append((mock_industry, MockIndustryConnector.supports))
    # 动态连接器（自修复生成的，热加载；优先级最低，不覆盖已有静态指标）
    # 同时恢复动态调度作业
    from src.scheduler.registry import load_dynamic_jobs
    load_dynamic_jobs()  # 从 _schedule.json 恢复自修复注册的定时任务
    dynamic_routes = get_dynamic_loader().load_all()
    routes.extend(dynamic_routes)
    backend = ConnectorRouter(routes, repo=repo)
    # 个股新闻自动抓取（akshare缺失/失败时fetch_news返回空列表，不阻断主链路）
    news_fetcher = AkshareNewsFetcher()
    # LLM驱动的Supervisor规划器（动态选择Agent与指标，失败回退规则路由）
    planner = LLMSupervisorPlanner(gateway)
    agents: dict[str, Any] = {
        "A01_data_collector": DataCollectorAgent(backend),
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
        "A13_tech": TechIndustryAgent(gateway),
        "A14_consumer": ConsumerIndustryAgent(gateway),
        "A15_cyclical": CyclicalIndustryAgent(gateway),
        "A16_pharma": PharmaIndustryAgent(gateway),
        "A17_recommend": RecommendationAgent(gateway),
        "A18_audit": AuditAgent(),
        "A19_code_engineer": CodeEngineerAgent(gateway),
    }
    # 技能库（PTD三级加载）：注入到支持技能的分析层/信息层Agent（_skill_library属性），
    # A17由图内ReAct工具按L0索引自主加载；技能目录缺失时全部静默降级。
    skill_library = SkillLibrary("skills")
    for agent in agents.values():
        if getattr(agent, "_skill_library", "missing") is None:
            agent._skill_library = skill_library  # noqa: SLF001 装配层属性注入
    graph = build_research_graph(
        agents,
        chain_path=f"{settings.llm_audit_dir}/audit_chain.jsonl",
        llm_audit_path=f"{settings.llm_audit_dir}/llm_audit.jsonl",
        news_fetcher=news_fetcher,
        planner=planner,
        repo=repo,
        skill_library=skill_library,
    )
    # 做T辅助子系统：复用日线采集链（backend）取PE/PB与日线上下文，
    # 复用LLM网关做消息面情绪打分，复用新闻抓取器取个股新闻。
    # 装配失败仅降级（接口返回503），不影响研究主链路。
    # 做T辅助子系统：复用日线采集链（backend）取PE/PB与日线上下文，
    # 复用LLM网关做消息面情绪打分，复用新闻抓取器取个股新闻。
    # 装配失败仅降级（接口返回503），不影响研究主链路。
    #
    # 做T权重档案仓储：**前端可编辑、按个股股性保存的权重/档位**主档案。
    # 用 SQLite（与 fact_* 三张表同库），仓储不可用只让档案接口降级，
    # 做T主链路继续按 YAML overrides / 全局口径出分。
    intraday_profile_repo = None
    try:
        intraday_profile_repo = build_intraday_profile_repository(settings)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "做T权重档案仓储装配失败（降级：档案接口不可用）", exc_info=True)
    intraday = None
    if settings.intraday_enabled:
        try:
            intraday = IntradayService(
                backend=backend,
                gateway=gateway,
                news_fetcher=news_fetcher,
                config_path=settings.intraday_config_path,
                profile_repo=intraday_profile_repo,
            )
        except Exception:  # noqa: BLE001 配置损坏等不应阻断主服务启动
            import logging

            logging.getLogger(__name__).exception(
                "做T辅助子系统装配失败（降级：接口将返回503）")
    # 资金流监控（板块/个股大资金动向）：不依赖做T是否启用 ——
    # 它读的是同花顺即时板块资金流 + 本地 Tushare 仓库，两条链都是独立的。
    # 仓储不可用时"选择列表"无法持久化（接口会 503），但榜单/走势仍可用。
    fundflow = None
    fundflow_repo = None
    try:
        from src.fundflow.provider import FundFlowProvider
        from src.fundflow.service import FundFlowService

        fundflow_repo = build_fund_flow_repository(settings)
        fundflow = FundFlowService(provider=FundFlowProvider(), repo=fundflow_repo)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "资金流监控装配失败（降级：该页签接口返回503）", exc_info=True)
    # 量化选股：3 档 LightGBM 模型 + 自定义板块。它不依赖做T/资金流是否启用
    # （模型与板块都在本地 SQLite + moss_selector 目录里），装配失败只影响该页签。
    quant_select = None
    try:
        from src.quant.quant_select_repo import QuantSelectSqliteRepository
        from src.quant.quant_select_service import QuantSelectService

        quant_select = QuantSelectService(
            repo=QuantSelectSqliteRepository(settings.sqlite_path))
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "量化选股装配失败（降级：该模块接口返回503）", exc_info=True)
    return Runtime(
        gateway=gateway, repo=repo, agents=agents, graph=graph, backend=backend,
        news_fetcher=news_fetcher, intraday=intraday,
        intraday_profile_repo=intraday_profile_repo,
        fundflow=fundflow, fundflow_repo=fundflow_repo,
        quant_select=quant_select,
    )


def agent_health(agents: dict[str, Any]) -> dict[str, str]:
    """聚合全部Agent的health_check。"""
    return {
        agent_id: ("healthy" if agent.health_check() else "unhealthy")
        for agent_id, agent in sorted(agents.items())
    }
