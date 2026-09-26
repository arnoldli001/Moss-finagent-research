"""API运行时：默认Agent注册表与图的组装（DI工厂）。"""

from __future__ import annotations

import logging
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
from src.infrastructure.connectors.baostock_connector import BaostockConnector
from src.infrastructure.connectors.cached_news_fetcher import CachedNewsFetcher
from src.infrastructure.connectors.dynamic_loader import get_dynamic_loader
from src.infrastructure.connectors.fedwatch_connector import FedWatchConnector
from src.infrastructure.connectors.coal_inventory_connector import (
    CoalInventoryConnector,
)
from src.infrastructure.connectors.index_valuation_connector import (
    IndexValuationConnector,
)
from src.infrastructure.connectors.industry_valuation_connector import (
    IndustryValuationConnector,
)
from src.infrastructure.connectors.liquor_price_connector import (
    LiquorPriceConnector,
)
from src.infrastructure.connectors.local_csv_connector import LocalCsvConnector
from src.infrastructure.connectors.margin_trading_connector import (
    MarginTradingConnector,
)
from src.infrastructure.connectors.mock_industry_connector import MockIndustryConnector
from src.infrastructure.connectors.news_fetcher import LocalFallbackNewsFetcher
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
    build_news_cache_repository,
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


def build_daily_connector_chain(
    *,
    repo: DataPointRepository | None = None,
    disable_cache: bool = False,
    disable_db: bool = False,
) -> ConnectorRouter:
    """只含**日线行情**指标的采集链（`stock/index/etf_close:{code}`）。

    ## 为什么单独抽出来

    `build_runtime()` 里那条 15+ 源的链是给 Agent 做**全指标**采集用的；
    但「价格面板 / 全量下载 / 回测取数」只需要日线三件套，而且**不经过 Agent** ——
    它们原来是各自 new 一个 `XtQuantConnector` 直连 QMT 的。2026-09 QMT 失去
    行情权限后，那些直连点全部取不到数，且完全绕过了项目已经配好的容灾链
    （AkShare → 腾讯 → Tushare → baostock）与失败冷却。

    所以把日线这 6 条路由抽成一个函数，让 runtime 与这些离线调用方**共用同一份
    顺序与同一套口径** —— 顺序只存在一处，不会再出现"runtime 改了、价格面板没改"
    这种漂移。顺序与理由见 `build_runtime()` 的注释：
    在线源 → 停更的本地CSV → [QMT 开关]，**QMT 在最末**（终端短期内无法恢复
    行情权限，放在任何位置之前都只会贡献一次必然失败的连接等待）。

    `repo` 不传时用**无 DB 短路**的纯网络链：批量下载要的就是"取到最新的"，
    库里的旧区间会把请求遮掉。
    """
    settings = get_settings()
    routes: list[tuple[Any, Any]] = []
    routes.append((AkshareConnector(), AkshareConnector.supports))
    routes.append((TencentDailyConnector(), TencentDailyConnector.supports))
    routes.append((TushareConnector(), TushareConnector.supports))
    routes.append((BaostockConnector(), BaostockConnector.supports))
    if settings.local_quote_dir:
        routes.append((
            LocalCsvConnector(settings.local_quote_dir), LocalCsvConnector.supports))
    if settings.qmt_enabled:
        routes.append((XtQuantConnector(), XtQuantConnector.supports))
    return ConnectorRouter(
        routes, repo=repo, disable_cache=disable_cache,
        disable_db=disable_db or repo is None)


def build_runtime() -> Runtime:
    """按生产默认配置组装全部Agent与StateGraph。"""
    settings = get_settings()
    gateway = LLMGateway(settings=settings)
    repo = build_repository(settings)
    # 采集后端（有序路由，首个supports命中者处理，失败抛错由上层记录）。
    #
    # ⚠️ 链序在 2026-09 做过一次**整体重排**，原因是本机 QMT 终端已失去行情权限
    # 且不在运行（127.0.0.1:58610 不通），连带它导出的本地CSV也停在 2026-08-31：
    #   旧序 QMT → 本地CSV → AkShare → 腾讯 → Tushare
    #     ① QMT 在链首时每次取数都要先等一次必然失败的连接；
    #     ② 本地CSV（已停更）排在**在线源之前**，会把 AkShare/腾讯/Tushare 挡在门外
    #        —— 实测就出现过"日K面板停在 2026-08-31，而腾讯当天明明有数据"。
    #   新序 AkShare → 腾讯 → Tushare → baostock → 本地CSV → [QMT 仅在开关打开时]
    #     ① 先打在线的活源；② 停更的本地CSV退到最后；③ QMT 默认关闭且排**全链最后**
    #        —— 终端短期内无法恢复行情权限，放在任何位置之前都只会贡献一次
    #        必然失败的连接等待（实测 4~5s）。将来权限恢复时改一个环境变量即可兜底。
    routes: list[tuple[Any, Any]] = []

    # 1) AkShare 在线：CPI/PPI/M2/社融/行情/个股PE/PB/财务比率/社零/煤价真实序列。
    #    放在链首是因为它的覆盖面最广（宏观 + 行情 + 估值一个连接器全包）。
    akshare = AkshareConnector()
    routes.append((akshare, AkshareConnector.supports))

    # 2) 腾讯财经日K：**独立于东财/新浪的通道**。实测（2026-09-17）AkShare 的东财主源
    #    被阻断、新浪回退断连**同时**发生，而腾讯通道正常（做T面板的实时链路一直走它）。
    #    实测（2026-09-22）腾讯对个股/ETF/指数都可用；一次最多约 641 根，
    #    长区间由连接器用 end 锚点自动翻页补齐。
    tencent_daily = TencentDailyConnector()
    routes.append((tencent_daily, TencentDailyConnector.supports))

    # 3) Tushare Pro：全历史在线源（个股前复权 pro_bar / 指数 index_daily / ETF fund_daily）。
    #    实测个股 2845 行 0.33s、指数 5998 行、ETF 3483 行，都是一次调用取全量。
    #    排在腾讯之后：需要 token 与积分，属于"有凭据才可用"的一跳。
    tushare_connector = TushareConnector()
    routes.append((tushare_connector, TushareConnector.supports))

    # 4) baostock：**免 token 的第四个独立故障域**，一次调用返回全历史。
    #    排在这里的唯一原因是它慢（实测约 3.8s/只、指数约 8.2s/只）——
    #    前面三个源都拿不到时才值得付这个时间。
    baostock_connector = BaostockConnector()
    routes.append((baostock_connector, BaostockConnector.supports))

    # 5) 本地QMT导出CSV：**退到最后**。它是 QMT 的导出文件，本机已停在 2026-08-31，
    #    排在在线源前面会把更新的在线数据挡掉（实测踩过：面板停在两周前）。
    #    放在在线源之后，它的角色变成"断网时唯一还能给点东西的源"。
    if settings.local_quote_dir:
        csv_connector = LocalCsvConnector(settings.local_quote_dir)
        routes.append((csv_connector, LocalCsvConnector.supports))

    # 6) 迅投QMT：**整个日线链的最后一位**，且默认关闭（QMT_ENABLED）。
    #    终端已无行情权限且不运行，**短期内无法恢复**，所以它既排在在线源之后、
    #    也排在停更的本地CSV之后 —— 放在 CSV 之前没有意义（CSV 至少是本地文件，
    #    读它不付网络超时；而 QMT 每次都要先等一次必然失败的 xtquant 连接，
    #    实测 4~5s）。将来权限恢复时打开开关即可兜底。
    if settings.qmt_enabled:
        qmt = XtQuantConnector()
        routes.append((qmt, XtQuantConnector.supports))

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
    # 消费/周期/医药行业真实估值：中证指数官网行业指数PE-TTM（主）→申万一级（备）
    industry_valuation = IndustryValuationConnector()
    routes.append((industry_valuation, IndustryValuationConnector.supports))
    # 白酒价格：酒排名「酒价内参」终端零售均价（口径是零售均价、非批价，source_name 已披露）
    liquor_price = LiquorPriceConnector()
    routes.append((liquor_price, LiquorPriceConnector.supports))
    # 电厂煤炭库存：中电联CECI周报「纳入统计的发电企业煤炭库存」（官方JSON接口）
    coal_inventory = CoalInventoryConnector()
    routes.append((coal_inventory, CoalInventoryConnector.supports))
    # ⚠️ 模拟产业连接器：**只作为尚未接入真实源的指标的兜底**，必须排在所有真实源之后。
    #    已被真实源覆盖的指标（科技三指标、消费/周期/医药行业PE、白酒价格、电厂煤炭库存、
    #    社零、煤价）永远不该走到这里 —— 排在真实源之前会把真数据挡在门外
    #    （与 QMT/本地CSV 的链序教训同一条）。
    mock_industry = MockIndustryConnector()
    routes.append((mock_industry, MockIndustryConnector.supports))
    # 动态连接器（自修复生成的，热加载；优先级最低，不覆盖已有静态指标）
    # 同时恢复动态调度作业
    from src.scheduler.registry import load_dynamic_jobs
    load_dynamic_jobs()  # 从 _schedule.json 恢复自修复注册的定时任务
    dynamic_routes = get_dynamic_loader().load_all()
    routes.extend(dynamic_routes)
    backend = ConnectorRouter(routes, repo=repo)
    # 个股新闻自动抓取（akshare缺失/失败时fetch_news返回空列表，不阻断主链路）。
    # 用组合版：akshare 取不到时退本地私有直连源（公开仓库无该来源 → 行为同原实现），
    # 否则本机 `ak.stock_news_em()` 恒为空，做T「消息面情绪」整块没数据。
    news_fetcher = LocalFallbackNewsFetcher()
    # 新闻缓存：TTL 内重复分析/做T扫描直接读库，避免每次网络取数。
    # 仓储装配失败降级为纯内存 TTL，再不行退回原 fetcher（增强链路不阻断）。
    if settings.news_cache_enabled:
        try:
            news_cache_repo = build_news_cache_repository(settings)
            news_fetcher = CachedNewsFetcher(
                news_fetcher, repo=news_cache_repo,
                ttl_seconds=settings.news_cache_ttl_seconds)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "新闻缓存仓储装配失败（降级：仅进程内/无缓存）", exc_info=True)
            news_fetcher = CachedNewsFetcher(
                LocalFallbackNewsFetcher(),
                ttl_seconds=settings.news_cache_ttl_seconds)
    # LLM驱动的Supervisor规划器（动态选择Agent，失败回退规则路由）
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
