"""Supervisor编排引擎（LangGraph StateGraph）。

管线：supervisor(规划) → A01采集 → A02清洗 → A03校验 → A04入库 →
      并行分析(A08宏观/A09中观/A10微观/A11风险) → A17综合建议 → A18审计封存。

Agent通过工厂注入（测试可全部替换为Fake）；节点级异常吞入state.errors，
不中断整图；计划外Agent节点直接跳过（空更新）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.agent_meta import agent_name, confidence_zh
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.state import ResearchState
from src.domain.skills.library import SkillLibrary
from src.orchestration.planner import LLMSupervisorPlanner

logger = logging.getLogger(__name__)

ANALYSIS_AGENTS = ("A08_macro", "A09_meso", "A10_micro", "A11_fin_risk", "A12_compliance")
INDUSTRY_AGENTS = ("A13_tech", "A14_consumer", "A15_cyclical", "A16_pharma")
# 行业路由表：target命中关键词 → 对应行业Agent（industry类型任务用）
INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "A13_tech": ("科技", "半导体", "芯片", "电子", "软件", "ai", "人工智能", "计算机",
                 "通信", "算力", "消费电子"),
    "A14_consumer": ("消费", "白酒", "食品", "饮料", "家电", "零售", "纺织", "服装",
                     "美妆", "农业", "旅游", "免税"),
    "A15_cyclical": ("周期", "煤炭", "有色", "钢铁", "化工", "建材", "石油", "原油",
                     "航运", "铜", "铝", "锂"),
    "A16_pharma": ("医药", "医疗", "创新药", "生物", "器械", "中药", "药店", "疫苗",
                   "cxo"),
}
INFO_AGENTS = ("A05_verifier", "A06_extractor", "A07_sentiment")
DATA_PIPELINE_AGENTS = ("A02_data_cleaner", "A03_data_validator", "A04_data_storage")
# 产出可被A17综合的全部分析类Agent
ALL_INSIGHT_AGENTS = ANALYSIS_AGENTS + INDUSTRY_AGENTS

# 主题新闻关键词组（stock_info_global_em 最近约200条全球快讯中过滤）：
# 宏观问题只取与问法直接相关的词；行业命中后放开整组，以覆盖"细分环节"类提问
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "macro_core": ("美联储", "加息", "降息", "美股", "美债", "非农", "通胀", "PCE",
                   "欧央行", "日本央行", "关税", "衰退", "利率决议", "鲍威尔", "就业数据",
                   # 补齐（2026-09-26）：用户原话常常只用这些词，
                   # 缺了它们会让"主题关键词为空 → 不拉任何新闻 → 信息层整体空跳过"。
                   # 实测故障：问题里写了"美国说年内还会再加息一次"，
                   # 但"加息"是"再加息"的子串、且没提"美联储"，
                   # 旧实现按**精确词**匹配导致命中为空，A05/A06/A07 全部拿到零输入。
                   "利率", "联邦基金", "FOMC", "货币政策", "缩表", "美债收益率",
                   "美联储主席", "点阵图", "CPI", "核心CPI", "失业率", "就业",
                   "经济数据", "加息预期", "降息预期", "美元", "美股三大指数"),
    "A13_tech": ("人工智能", "AI", "算力", "芯片", "半导体", "光模块", "CPO", "服务器",
                 "液冷", "PCB", "存储芯片", "消费电子", "数据中心", "国产替代", "先进封装",
                 "HBM", "铜缆", "机器人", "大模型",
                 # 补齐：用户说"AI产业链""产业链龙头"时也应命中科技组
                 "产业链", "AI产业链"),
    "A14_consumer": ("消费", "白酒", "家电", "零售", "食品饮料", "免税", "旅游", "餐饮"),
    "A15_cyclical": ("煤炭", "有色", "钢铁", "化工", "石油", "原油", "铜价", "铝价",
                     "锂电", "航运", "天然气"),
    "A16_pharma": ("医药", "创新药", "医疗器械", "中药", "疫苗", "CXO", "集采",
                   "GLP-1", "减肥药"),
}

# 行业专业指标目录（ind:前缀由模拟产业连接器提供，付费接口接入后保持id不变）
INDUSTRY_INDICATORS: dict[str, tuple[str, ...]] = {
    "A13_tech": (
        "ind:半导体销售额同比", "ind:芯片出货量同比", "ind:科技行业PE(TTM)",
    ),
    "A14_consumer": (
        "ind:社会消费品零售总额同比", "ind:白酒批价(元/瓶)", "ind:消费行业PE(TTM)",
    ),
    "A15_cyclical": (
        "ind:动力煤价格(元/吨)", "ind:重点电厂煤炭库存(万吨)", "ind:周期行业PE(TTM)",
    ),
    "A16_pharma": (
        "ind:创新药IND申报数量(个)", "ind:医保集采药品均价同比", "ind:医药行业PE(TTM)",
    ),
}

# analysis_type → (采集指标, 参与分析Agent)
# 个股指标带代码后缀（PE(TTM)/PB/财务比率由AkShare提供，喂饱A10/A11）：
# plan_run 中统一解析为 "{indicator}:{target}"。
_CODE_SUFFIX_INDICATORS = frozenset(
    {"stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率"})

# 美国宏观关键词：query命中时自动追加美国宏观指标
_US_MACRO_KEYWORDS = ("美股", "加息", "降息", "美联储", "FOMC", "联邦基金",
                      "美债", "非农", "美国CPI", "美国通胀", "PCE", "鲍威尔",
                      "利率决议", "就业数据", "美国宏观")

# A股大盘流动性指标：行业/个股买卖与持有期研判时自动补齐（免费实时源）
_MARKET_LIQUIDITY_INDICATORS: tuple[str, ...] = (
    "mkt:turnover:total",       # 两市成交额（腾讯/东财实时，extra含沪/深/创/科分项）
    "mkt:turnover:hist",        # 60交易日序列→MA5/MA10/MA50
    "mkt:turnover_rate:all_a",  # 全A加权换手率+前5%成交集中度
    "mkt:margin_balance",       # 两融余额
    "mkt:margin_balance:hist",  # 两融10日序列
    "mkt:north_flow",           # 北向（停披期诚实缺口）
    "idx_val:snapshot:all",     # 核心宽基PE/PB分位
)
# 科创板/创业板板块指标：中观轮动研判必备（三市分项成交额/板块PE分位/市场宽度）
_BOARD_LIQUIDITY_INDICATORS: tuple[str, ...] = (
    "mkt:cybkcb:turnover:all",      # 创业板指/科创50/科创综指实时成交额
    "mkt:cybkcb:val:all",           # 双创板块PE及1/3/5年/全历史分位
    "mkt:cybkcb:spot_summary",      # 两板涨跌家数/上涨占比/中位涨幅（情绪温度）
)
# 全球/海外流动性关键词：命中时追加CME FedWatch（外网不可达会自动降级为空）
_GLOBAL_LIQUIDITY_KEYWORDS = (
    "美联储", "加息", "降息", "FOMC", "FedWatch", "联邦基金", "宏观流动性",
    "全球流动性", "外资", "美债", "美元流动性", "海外流动性",
)
# A股语境词：宏观问题中出现这些词时也需要A股流动性数据（含交易意图触发词）
_A_SHARE_LIQUIDITY_KEYWORDS = (
    "A股", "两市", "大盘", "成交额", "两融", "北向", "换手率", "估值分位",
    "仓位", "放量", "缩量",
    # —— 条件触发：大盘预测/复盘类问法（skill 1.1 第0节）——
    "复盘", "明日", "后市", "股市", "上证", "创业板", "科创板", "双创",
    # —— 条件触发：短线/买卖/板块策略类问法 ——
    "短线", "买卖", "买入", "卖出", "加仓", "减仓", "建仓", "清仓",
    "止损", "止盈", "回踩", "追涨", "轮动", "操作建议", "交易策略",
)


def append_liquidity_indicators(
    analysis_type: str, route_text: str, resolved: list[str]
) -> list[str]:
    """按任务类型与问题文本补齐大盘/板块流动性、FedWatch指标（规则式确定性追加）。

    LLM规划可能漏掉流动性指标，此函数保证行业/个股/全量研判必然携带；
    macro类型仅在问题明确涉及A股（含大盘预测/复盘/短线买卖/板块策略等交易
    意图词）时追加。命中即同时补齐双创板块指标，供中观三市分项轮动研判。
    """
    text = (route_text or "").lower()
    need_market = analysis_type in ("stock", "industry", "full")
    if analysis_type == "macro" and any(
        kw in (route_text or "") for kw in _A_SHARE_LIQUIDITY_KEYWORDS
    ):
        need_market = True
    if need_market:
        resolved += [i for i in _MARKET_LIQUIDITY_INDICATORS if i not in resolved]
        resolved += [i for i in _BOARD_LIQUIDITY_INDICATORS if i not in resolved]
    if any(kw.lower() in text for kw in _GLOBAL_LIQUIDITY_KEYWORDS):
        # `fed:policy_range`（FRED 当前目标区间）是**可达**的那一个：
        # 实测 CME（`fed:rate_prob:next` 的数据源）在本机网络 TCP 443 不通，
        # 而 FRED 1.0~1.3s 可达且给到最新值（2026-09-25 区间 3.75%~4.00%）。
        # 所以优先给 policy_range，rate_prob 作为补充（通了更好，不通也有兜底）。
        for ind in ("fed:policy_range", "fed:rate_prob:next"):
            if ind not in resolved:
                resolved.append(ind)
    return list(dict.fromkeys(resolved))  # 保序去重（入参可能已含重复项）


_PLANNING: dict[str, tuple[list[str], list[str]]] = {
    "macro": (["CPI", "PPI"], ["A08_macro"]),
    "industry": (["CPI", "PPI"], ["A09_meso"]),
    "stock": (["stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率"],
              ["A10_micro", "A11_fin_risk", "A12_compliance"]),
    "news": ([], list(INFO_AGENTS)),
    "full": (["CPI", "PPI"], list(ANALYSIS_AGENTS)),
}


def route_industry(target: str) -> list[str]:
    """按行业名关键词把target路由到对应行业Agent（无匹配返回空列表）。"""
    lowered = (target or "").lower()
    return [aid for aid, kws in INDUSTRY_KEYWORDS.items()
            if any(k.lower() in lowered for k in kws)]


def extract_topic_keywords(
    analysis_type: str, target: str, query: str
) -> list[str]:
    """为宏观/行业问题提取主题新闻关键词（无命中返回空列表，不拉无关新闻）。

    - macro：仅取问题中实际出现的宏观词，命中后补充美联储/加息/降息核心词；
    - industry：按target+query路由行业组，命中后放开整组关键词以覆盖"细分环节"问法。
    """
    text = f"{target or ''} {query or ''}"
    low = text.lower()
    kws: list[str] = []

    def _add_macro() -> None:
        hits = [k for k in TOPIC_KEYWORDS["macro_core"] if k.lower() in low]
        if hits:
            # 加息相关报道常以"美联储/降息"表述，补充核心词提高召回
            for k in list(dict.fromkeys(hits + ["美联储", "加息", "降息"])):
                if k not in kws:
                    kws.append(k)

    def _add_industry() -> None:
        """按问题文本路由行业组，命中即放开整组（覆盖"细分环节"类提问）。"""
        for aid in route_industry(text):
            for k in TOPIC_KEYWORDS.get(aid, ()):  # type: ignore[arg-type]
                if k not in kws:
                    kws.append(k)

    if analysis_type == "macro":
        _add_macro()
    elif analysis_type == "industry":
        _add_industry()
    elif analysis_type in ("stock", "full"):
        # ⚠️ 综合问题（full）**不能只按 analysis_type 取词**（2026-09-26 实测故障）：
        # 用户问的是一句话里同时含美国加息 **和** A股AI产业链龙头的综合问题，
        # LLM 规划器把它定成 analysis_type="macro"，于是旧的 if/elif 只走
        # macro 分支、industry 组**根本没被评估**；又因为问题里写的是"再加息"
        # 而没有"美联储"字样，macro 分支也命中为空 ——
        # 最终 topic_keywords=[]，collect 节点据此**一个新闻都不拉**，
        # A05/A06/A07 信息层拿到零输入全部空跳过，舆情/事件维度整体缺席。
        #
        # 既然问题文本本身既谈宏观又谈行业，这里就**两组都取**。
        _add_macro()
        _add_industry()
    return kws


def plan_run(
    analysis_type: str,
    target: str,
    info_items: list | None = None,
    query: str = "",
) -> dict[str, Any]:
    """规则式Supervisor规划：决定采集指标与参与Agent（Demo用确定性路由）。

    query：用户原始问题。行业Agent路由与主题新闻关键词同时参考target与query，
    因此target留空、问题写在query中（如"当前AI产业链…"）也能正确路由。
    携带info_items（新闻/公告/研报文本）时自动追加信息层Agent，
    与analysis_type无关；news类型为纯信息层管线（不采集数据点）。
    """
    analysis_type = analysis_type if analysis_type in _PLANNING else "full"
    indicators, agents = _PLANNING[analysis_type]
    agents = list(agents)  # 拷贝：_PLANNING为模块级共享配置，禁止原地修改
    indicators = list(indicators)
    is_stock_code = bool(re.fullmatch(r"\d{6}", target or ""))
    # 个股类指标统一拼代码后缀（stock_close/PE/PB/财务比率）
    resolved = [
        f"{ind}:{target}" if ind in _CODE_SUFFIX_INDICATORS and is_stock_code else ind
        for ind in indicators
    ]
    route_text = f"{target or ''} {query or ''}"
    if analysis_type == "macro":
        # 美国宏观问题自动追加美国宏观指标（CPI/非农/利率/PCE等）
        low_text = route_text.lower()
        if any(kw.lower() in low_text for kw in _US_MACRO_KEYWORDS):
            us_inds = ["us_cpi_yoy", "us_fed_rate", "us_nonfarm", "us_pce",
                       "us_unemployment", "us_core_cpi"]
            resolved += [i for i in us_inds if i not in resolved]
    if analysis_type == "industry":
        # 通用产业链分析(A09) + 命中的专业行业Agent(A13-A16)，无匹配则仅A09
        routed = [a for a in route_industry(route_text) if a not in agents]
        agents += routed
        # 按命中行业下发专业产业指标（模拟连接器/未来付费接口）
        for aid in routed:
            resolved += [i for i in INDUSTRY_INDICATORS.get(aid, ()) if i not in resolved]
        # 申万行业估值截面（覆盖所有行业的PE/PB，不依赖命中行业路由）
        sw_indicators = [
            "ind:sw_third_pe_ttm:all", "ind:sw_third_pb:all",
            "ind:sw_first_pe_ttm:all",
        ]
        resolved += [i for i in sw_indicators if i not in resolved]
        # 渗透率数据（按query命中的核心赛道自动追加）
        _PENETRATION_TRACKS_MAP = {
            "新能源": "ind:penetration:新能源汽车",
            "汽车": "ind:penetration:新能源汽车",
            "机器人": "ind:penetration:人形机器人",
            "人形": "ind:penetration:人形机器人",
            "固态电池": "ind:penetration:固态电池",
            "电池": "ind:penetration:固态电池",
            "AI": "ind:penetration:AI大模型应用",
            "人工智能": "ind:penetration:AI大模型应用",
            "半导体": "ind:penetration:半导体国产替代",
            "国产替代": "ind:penetration:半导体国产替代",
            "HBM": "ind:penetration:HBM存储",
            "存储": "ind:penetration:HBM存储",
        }
        for kw, ind in _PENETRATION_TRACKS_MAP.items():
            if kw.lower() in route_text.lower() and ind not in resolved:
                resolved.append(ind)
    if analysis_type in ("stock", "full") and target:
        agents = list(dict.fromkeys(agents))  # 保序去重
    # 信息层参与条件：显式info_items / 个股(自动拉个股新闻) /
    # 宏观或行业问题能提取到主题关键词（自动拉全球财经快讯，无新闻时节点空跳过）
    auto_topic = extract_topic_keywords(analysis_type, target, query)
    if info_items or (is_stock_code and analysis_type in ("stock", "full")) or auto_topic:
        agents += [a for a in INFO_AGENTS if a not in agents]
    # 行业/个股/全量研判确定性补齐大盘流动性指标；海外流动性关键词补FedWatch
    resolved = append_liquidity_indicators(analysis_type, route_text, resolved)
    return {
        "analysis_type": analysis_type,
        "target": target,
        "indicators": resolved,
        "topic_keywords": auto_topic,
        "agents": agents + ["A17_recommend", "A18_audit"],
    }


def _needs_code_suffix(indicator: str) -> bool:
    """该指标是否必须带 6 位代码后缀才能被任何连接器支持。

    依据 ``_CODE_SUFFIX_INDICATORS``（个股类指标）。裸名字（如 ``"PE(TTM)"``）
    **没有任何连接器 supports** —— 连不上就是连不上，不是"数据缺失"。
    """
    bare = indicator.split(":", 1)[0]
    return bare in _CODE_SUFFIX_INDICATORS


def sanitize_indicators(
    indicators: list[str], target: str, analysis_type: str,
) -> tuple[list[str], list[str], list[str]]:
    """修正 LLM 规划产出的指标契约，返回 (可用指标, 被丢弃, 被替换说明)。

    修的是 2026-09-26 实测到的一个真实故障链：

    用户问"请对当前A股**AI产业链各细分行业龙头**的估值及10月走势分析预测"，
    LLM 规划器（light 层小模型）返回：

        analysis_type = "macro", target = ""
        indicators = [..., "stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率"]

    这些**裸个股指标**是契约违规 —— 个股指标必须拼 6 位代码
    （``PE(TTM):300308``）。而 target 为空，下游补不出后缀，于是 A01 去 fetch
    ``"PE(TTM)"``：**没有任何连接器 supports 它，瞬间 DataFetchError**。
    用户看到的"AI产业链龙头估值数据缺失 / 估值无任何输入数据 / valuation_calc=数据不足"
    全部源自这里 —— **本地库里明明躺着 82 只个股的 PE 与 80 只的 PB**（含最新交易日）。

    两类处理：
    1. **有 target 代码** → 补后缀（把 ``PE(TTM)`` 修成 ``PE(TTM):300308``）；
    2. **无 target** → 丢弃裸指标，并**换成行业级估值**（申万截面 PE/PB），
       因为"行业龙头的估值"在没有具体标的时，行业估值截面是唯一有数据可依的口径。
       换掉而不是静默丢弃，是为了让分析层仍有估值素材、而不是空手（否则 A10
       又会输出"估值数据不足"）。
    """
    code = target if re.fullmatch(r"\d{6}", target or "") else ""
    usable: list[str] = []
    dropped: list[str] = []
    notes: list[str] = []
    for ind in indicators:
        if not _needs_code_suffix(ind):
            usable.append(ind)
            continue
        # 已经带 6 位代码的指标**必须原样保留**：LLM 有时会在 query 里提到多只
        # 个股并给出带后缀的指标，这时不能用 target 覆盖它 —— 那会把"分析 A 股"
        # 悄悄改成"分析 target 那一只"（测试 `test_already_suffixed...` 守着这条）。
        _, _, existing = ind.partition(":")
        if re.fullmatch(r"\d{6}", existing):
            usable.append(ind)
            continue
        if code:
            fixed = f"{ind.split(':', 1)[0]}:{code}"
            usable.append(fixed)
            notes.append(f"{ind} → {fixed}（补标的代码）")
            continue
        dropped.append(ind)
    if dropped:
        # 无可解析标的时用行业估值截面兜底（这些指标有真实数据源与入库作业）
        fallback = ["ind:sw_third_pe_ttm:all", "ind:sw_third_pb:all",
                    "ind:sw_first_pe_ttm:all"]
        added = [i for i in fallback if i not in usable]
        usable += added
        notes.append(
            f"丢弃无标的的个股指标 {dropped}（缺 6 位代码，无连接器支持）"
            f"，改用行业估值截面 {added}")
    return usable, dropped, notes


def _summary(output: AgentOutput) -> dict[str, Any]:
    return {
        "agent_id": output.agent_id,
        "agent_name": agent_name(output.agent_id),
        "conclusion": output.conclusion,
        "confidence": output.confidence.value,
        "confidence_zh": confidence_zh(output.confidence.value),
        "data_refs": output.data_refs,
        "result": output.result,
    }


def _verified_texts(state: ResearchState, limit: int = 10) -> list[str]:
    """A05判可信的info_items原文摘要：A06事件提取偶发为空时，给分析层兜底事实。"""
    verified = state.get("verified_items") or {}
    items_review = verified.get("items") or []
    # A05把LLM verdict合并进items：verified=True/verdict∈{可信,存疑}为放行，"拒绝"剔除
    trusted = {
        str(e.get("item_id"))
        for e in items_review
        if isinstance(e, dict) and e.get("verified") is True
    }
    if not trusted:
        return []
    texts: list[str] = []
    for entry in items_review:
        if not isinstance(entry, dict) or str(entry.get("item_id")) not in trusted:
            continue
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("text") or "").strip().replace("\n", " ")
        source = str(entry.get("source_name") or "").strip()
        when = str(entry.get("publish_time") or "").strip()
        head = f"【{source} {when}】{title}".strip()
        snippet = body[:300] if body else title
        texts.append(f"{head} {snippet}".strip())
        if len(texts) >= limit:
            break
    return texts


def build_research_graph(agents: dict[str, Any], *, chain_path: str, llm_audit_path: str,
                         news_fetcher: Any = None, planner: LLMSupervisorPlanner | None = None,
                         repo: Any = None, skill_library: SkillLibrary | None = None):
    """构建并编译投研StateGraph。

    agents: agent_id → Agent实例（必须含A01-A04、A17、A18；
    A08-A11缺失时对应分支自动跳过）。
    news_fetcher: 可选个股新闻抓取器（fetch_news(code,limit)），
    个股任务采集阶段自动拉取新闻注入info_items，打通A05-A07信息层。
    planner: 可选LLM驱动的Supervisor规划器；传入时优先用LLM动态规划，
    失败回退到规则式plan_run()。
    repo: 可选DataPointRepository；实时采集失败/为空时，从入库快照降级读取
    （定时作业已落库的最近值），extra标storage_fallback，保证研判连续性。
    skill_library: 可选技能库（PTD三级加载）；缺省时按仓库skills/目录构造，
    目录不存在时技能功能静默关闭，不影响主链路。
    """
    if skill_library is None:
        skill_library = SkillLibrary("skills")
    # 实时源失败时从存储降级读取的行数（序列类指标保留趋势所需长度）
    _STORAGE_FALLBACK_LIMITS = {
        "mkt:turnover:hist": 60,
        "mkt:margin_balance:hist": 10,
        "mkt:north_flow": 30,
        "idx_val:snapshot:all": 8,
    }

    async def _storage_fallback(indicator: str) -> list[dict[str, Any]]:
        """实时采集无数据时，读统一数据层最近入库快照（禁止连接器直连数据库）。"""
        if repo is None:
            return []
        try:
            rows = await repo.query_points(indicator)
        except Exception as exc:  # noqa: BLE001 降级失败不阻断主链路
            logger.warning("存储降级读取失败(%s): %s", indicator, exc)
            return []
        if not rows:
            return []
        limit = _STORAGE_FALLBACK_LIMITS.get(indicator, 1)
        stale = [p.model_dump() for p in rows[-limit:]]
        for p in stale:
            p.setdefault("extra", {})
            p["extra"]["storage_fallback"] = "live_empty_or_failed"
        return stale

    # ========== 数据缺口自修复 ==========

    _gap_resolver: Any | None = None  # 延迟初始化（需要 gateway）

    def _get_gap_resolver() -> Any | None:
        """延迟创建 DataGapResolverAgent（需要 gateway 从 agents 获取）。"""
        nonlocal _gap_resolver
        if _gap_resolver is not None:
            return _gap_resolver
        a17 = agents.get("A17_recommend")
        if a17 is None:
            return None
        gw = getattr(a17, "_gateway", None)  # noqa: SLF001
        if gw is None:
            return None
        from src.domain.agents.data_gap_resolver import DataGapResolverAgent
        _gap_resolver = DataGapResolverAgent(gw)
        return _gap_resolver

    async def _try_self_heal(
        indicator: str, error_ctx: str,
    ) -> list[dict[str, Any]]:
        """数据缺口自修复：LLM 生成连接器 → 沙箱验证 → 热加载 → 重试 fetch。

        失败不阻断主链路——返回空列表，指标缺口照样上报给 A17。
        """
        resolver = _get_gap_resolver()
        if resolver is None:
            return []

        # fetch 重试函数（让 resolver 验证新连接器能真的拿到数据）
        collector = agents.get("A01_data_collector")
        fetch_fn = None
        if collector is not None:
            async def _retry_fetch(ind: str) -> list:
                from src.core.models import AgentInput
                out = await collector.execute(AgentInput(
                    task_id=f"heal_{int(time.time())}",
                    tenant_id="tenant_001",
                    payload={"indicator": ind},
                ))
                return list(out.result.get("data_points", []))
            fetch_fn = _retry_fetch

        try:
            result = await resolver.resolve(indicator, error_ctx, fetch_fn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自修复异常(%s): %s", indicator, exc)
            return []

        if not result.success:
            logger.info("自修复未成功(%s): %s", indicator, result.error or "?")
            return []

        # 自修复成功 → 注册定时调度（沉淀为持久采集任务）
        try:
            from src.scheduler.registry import register_dynamic_job
            sched_cfg = type(resolver).get_dynamic_schedule_config(
                indicator, result)
            job_name = f"dynamic_{hashlib.md5(indicator.encode()).hexdigest()[:8]}"
            register_dynamic_job(
                name=job_name,
                cron=sched_cfg["cron"],
                indicator=indicator,
                connector_path=result.connector_path,
            )
            logger.info("自修复成功→已注册定时采集: %s (%s)", job_name, sched_cfg["cron"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("动态调度注册失败(%s): %s", indicator, exc)

        # 自修复成功——重试 fetch 拿数据
        if fetch_fn is not None:
            try:
                points = await fetch_fn(indicator)
                if points:
                    logger.info(
                        "自修复成功(%s)：新连接器 %s 获取 %d 条数据",
                        indicator, result.connector_class, len(points))
                    return [p.model_dump() if hasattr(p, "model_dump")
                            else p for p in points]
            except Exception as exc:  # noqa: BLE001
                logger.warning("自修复后重试fetch失败(%s): %s", indicator, exc)

        return []

    async def _run_agent(
        agent: Any, state: ResearchState, payload: dict[str, Any]
    ) -> dict[str, Any]:
        output = await agent.execute(AgentInput(
            task_id=state["task_id"], tenant_id=state["tenant_id"], payload=payload,
        ))
        # agent_id取自output（Fake替身可能未挂agent_id属性）
        aid = getattr(agent, "agent_id", None) or output.agent_id
        update: dict[str, Any] = {
            "agent_outputs": [_summary(output)],
            "data_refs": list(output.data_refs),
            "trace_ids": [output.trace_id],
            "progress": [f"{aid} 已完成"],
        }
        return update, output

    def _node(agent_id: str, payload_fn, *, collect: bool = False):
        agent = agents.get(agent_id)

        async def node(state: ResearchState) -> dict[str, Any]:
            if agent is None:
                return {}  # 未注册的Agent直接跳过（可选分支）
            # 取消检查：用户点停止时立即终止
            token = state.get("cancellation_token")
            if token is not None:
                token.check()
            # 写入实时进度（通过return值让LangGraph更新state）
            if agent_id in ALL_INSIGHT_AGENTS and agent_id not in state["plan"]:
                return {"progress": [f"跳过 {agent_id}（不在本次规划中）"]}
            if agent_id in INFO_AGENTS and (
                agent_id not in state["plan"] or not state.get("info_items")
            ):
                return {}  # 信息层：计划未包含或无输入文本时跳过
            if agent_id in DATA_PIPELINE_AGENTS and not state.get("raw_points"):
                return {}  # 无采集数据时数据管线空转无意义（如news管线）
            # 实时进度：节点内通过cancel_token共享通道追加，前端立即可见
            if token is not None:
                token.push_progress(f"正在执行 {agent_id}…")
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
        """Supervisor规划：优先LLM动态规划，失败回退规则路由。"""
        llm_plan = None
        if planner is not None:
            try:
                llm_plan = await planner.plan(
                    state["user_query"], state.get("target", ""), agents,
                )
            except Exception:  # noqa: BLE001 LLM规划异常不阻断
                llm_plan = None
        if llm_plan:
            # 契约修正：LLM 常返回**裸个股指标**（"PE(TTM)"）且 target 为空，
            # 而个股指标必须带 6 位代码后缀，否则没有任何连接器 supports 它 ——
            # A01 会瞬间 DataFetchError，用户看到"估值数据缺失"。
            # 详见 sanitize_indicators 的说明（2026-09-26 实测故障）。
            requested = list(llm_plan["indicators"])
            indicators, dropped, fix_notes = sanitize_indicators(
                requested, llm_plan.get("target") or state.get("target", ""),
                llm_plan["analysis_type"],
            )
            topic_kw = extract_topic_keywords(
                llm_plan["analysis_type"], llm_plan["target"], state["user_query"],
            )
            # LLM可能漏掉大盘流动性指标，按任务类型/问题关键词确定性补齐
            route_text = f"{llm_plan.get('target') or ''} {state['user_query']}"
            indicators = append_liquidity_indicators(
                llm_plan["analysis_type"], route_text, indicators,
            )
            progress = ["Supervisor规划完成，开始数据采集…"]
            if fix_notes:
                progress = [f"规划修正：{n}" for n in fix_notes] + progress
                logger.info("规划契约修正：%s", "；".join(fix_notes))
            return {
                "plan": llm_plan["agents"],
                "analysis_type": llm_plan["analysis_type"],
                "target": llm_plan["target"] or state.get("target", ""),
                "_planned_indicators": indicators,
                "topic_keywords": topic_kw,
                "progress": progress,
            }
        # 规则路由fallback
        plan = plan_run(state["analysis_type"], state["target"],
                        state.get("info_items"), query=state["user_query"])
        return {
            "plan": plan["agents"],
            "analysis_type": plan["analysis_type"],
            "target": plan["target"],
            "_planned_indicators": plan["indicators"],
            "topic_keywords": plan.get("topic_keywords", []),
            "progress": ["Supervisor规划完成，开始数据采集…"],
        }

    def _collect_payload(state: ResearchState) -> dict[str, Any]:
        indicators = state.get("_planned_indicators")
        if indicators is None:
            indicators = plan_run(
                state["analysis_type"], state["target"],
                query=state.get("user_query", ""))["indicators"]
        return {"indicators": indicators}

    async def _collect_one(indicator: str, state: ResearchState,
                           updates_sink: dict[str, Any],
                           lock: asyncio.Lock) -> None:
        """单指标采集（异常隔离 + 存储降级），所有共享写入走lock。"""
        collector = agents["A01_data_collector"]
        live_points: list = []
        live_failed = False
        try:
            update, output = await _run_agent(
                collector, state, {"indicator": indicator}
            )
        except AgentExecutionError as exc:
            async with lock:
                updates_sink["errors"].append(
                    f"A01_data_collector({indicator}): {exc}")
            live_failed = True
        except Exception as exc:  # noqa: BLE001
            async with lock:
                updates_sink["errors"].append(
                    f"A01_data_collector({indicator}): 意外异常 {exc}")
            live_failed = True
        else:
            live_points = list(output.result.get("data_points", []))
            async with lock:
                updates_sink["agent_outputs"] += update["agent_outputs"]
                updates_sink["data_refs"] += update["data_refs"]
                updates_sink["trace_ids"] += update["trace_ids"]

        if not live_points:
            stale = await _storage_fallback(indicator)
            if stale:
                live_points = stale
                async with lock:
                    if live_failed:
                        updates_sink["errors"] = [
                            e for e in updates_sink["errors"]
                            if f"({indicator})" not in e]
                    logger.info(
                        "指标%s实时采集无数据，存储降级使用最近%d条快照",
                        indicator, len(stale))

        # 存储降级也无数据 → 触发自修复（LLM 生成连接器）
        if not live_points:
            err_ctx = ""
            async with lock:
                for e in updates_sink["errors"]:
                    if f"({indicator})" in e:
                        err_ctx = e
                        break
            logger.info("指标%s采集+降级均无数据，启动自修复…", indicator)
            healed = await _try_self_heal(indicator, err_ctx)
            if healed:
                live_points = healed
                async with lock:
                    if live_failed:
                        updates_sink["errors"] = [
                            e for e in updates_sink["errors"]
                            if f"({indicator})" not in e]
                    updates_sink["progress"] = (
                        updates_sink.get("progress", []) + [
                            f"指标 {indicator} 自修复成功（动态连接器已注册）"
                        ])

        async with lock:
            updates_sink["raw_points"] += live_points

    async def collect_node(state: ResearchState) -> dict[str, Any]:
        """A01：指标并发采集（asyncio.gather），失败隔离 + 存储降级兜底。

        指标间无依赖，并发跑比串行快 3-10×；结果顺序不影响后续 A02-A04 管线。
        """
        indicators = _collect_payload(state)["indicators"]
        ct = state.get("cancellation_token")
        if ct is not None:
            ct.push_progress(
                f"数据采集层启动（{len(indicators)}个指标并发，A01→A02→A03→A04）…"
            )
        updates: dict[str, Any] = {
            "raw_points": [], "agent_outputs": [],
            "data_refs": [], "trace_ids": [], "errors": [],
            "progress": ["数据采集层执行中（并发 A01 → A02 → A03 → A04）…"],
        }
        shared_lock = asyncio.Lock()
        await asyncio.gather(
            *[_collect_one(ind, state, updates, shared_lock) for ind in indicators]
        )

        if news_fetcher is not None and not state.get("info_items"):
            items = []
            target = state.get("target") or ""
            try:
                if re.fullmatch(r"\d{6}", target):
                    items = await news_fetcher.fetch_news(target)
                else:
                    keywords = state.get("topic_keywords") or extract_topic_keywords(
                        state.get("analysis_type", ""), target,
                        state.get("user_query", ""))
                    if keywords:
                        items = await news_fetcher.fetch_topic_news(keywords)
            except Exception as exc:  # noqa: BLE001 新闻为增强链路
                updates["errors"].append(f"news_fetcher: 意外异常 {exc}")
            if items:
                updates["info_items"] = items
        updates["progress"] = ["数据采集完成，开始分析与行业研判…"]
        return updates

    async def recommend_node(state: ResearchState) -> dict[str, Any]:
        # 取消检查
        token = state.get("cancellation_token")
        if token is not None:
            token.check()
        # 实时进度：A17开始ReAct综合分析（通过cancel_token共享通道，前端立即可见）
        if token is not None:
            token.push_progress("正在执行 A17_recommend 综合分析（ReAct循环）…")
        analyses = [o for o in state["agent_outputs"] if o["agent_id"] in ALL_INSIGHT_AGENTS]
        if not analyses:  # 纯信息层管线（news）时以信息层结论综合
            analyses = [o for o in state["agent_outputs"] if o["agent_id"] in INFO_AGENTS]
        if not analyses:
            return {"errors": ["A17_recommend: 无上游分析结论"], "final_report": None}

        from src.domain.agents.analysis.react import ReActExecutor, ToolRegistry
        from src.orchestration.message_bus import ask_agent, normalize_question

        # ReAct工具集：A17可主动追问上游Agent或查询已采集数据
        tools = ToolRegistry()
        collected_messages: list[dict[str, Any]] = []
        # 列出当前plan中可接受定性追问的分析/行业层Agent，避免追问数据层（契约不匹配）
        askable_prefixes = tuple(f"A{n:02d}" for n in range(8, 17))
        available_agents = [a for a in state.get("plan", [])
                            if a.startswith(askable_prefixes)]
        # 幂等：同一Agent同一问题只真正追问一次；每Agent追问次数封顶，防止LLM绕着重问
        MAX_ASKS_PER_AGENT = 1  # 收紧：每Agent最多追问1次
        asked_cache: dict[tuple[str, str], str] = {}
        ask_counts: dict[str, int] = {}

        async def _ask_agent(receiver: str, question: str) -> str:
            if receiver not in available_agents:
                return (f"{receiver}不支持直接问答；可追问对象："
                        f"{', '.join(available_agents) or '无'}；"
                        "数据层指标请改用 query_data 工具。")
            key = (receiver, normalize_question(question))
            if key in asked_cache:
                return ("（该问题已询问过，请勿重复提问；此前回答：）\n"
                        + asked_cache[key])
            if ask_counts.get(receiver, 0) >= MAX_ASKS_PER_AGENT:
                return (f"（已向{receiver}追问{MAX_ASKS_PER_AGENT}次，"
                        "请基于已有回答直接给出final_answer，不要再追问同一Agent）")
            answer, msgs = await ask_agent(
                "A17_recommend", receiver, question, agents, state,
            )
            asked_cache[key] = answer
            ask_counts[receiver] = ask_counts.get(receiver, 0) + 1
            collected_messages.extend(msgs)
            # 实时追加到live_state（通过cancel_token共享通道，前端轮询立即可见）
            if token is not None:
                token.push_messages(msgs)
            return answer

        async def _query_data(indicator: str, limit: int = 10) -> str:
            points = [p for p in state.get("validated_points", [])
                      if str(p.get("indicator", "")) == str(indicator)]
            if not points:
                return f"未找到指标 {indicator} 的数据"
            points = sorted(points, key=lambda p: str(p.get("period_date", "")),
                            reverse=True)[:limit]
            return "\n".join(
                f"- {p.get('period_date')}: {p.get('value')} (来源{p.get('source_name')})"
                for p in points
            )

        tools.register("ask_agent", _ask_agent,
                       f"向指定上游分析Agent提问。可用Agent：{', '.join(available_agents)}。"
                       "参数receiver=上述agent_id之一, question=具体问题。"
                       "规则：每个Agent最多追问2次，不要对同一Agent重复提出相同或"
                       "高度相似的问题；数据层Agent(A01-A04)不可提问，指标数值用query_data。")
        tools.register("query_data", _query_data,
                       "查询已采集的数据点，参数indicator=指标名, limit=条数")

        # 技能工具（PTD）：LLM按L0索引自主决定是否加载技能正文/参考
        async def _load_skill(name: str) -> str:
            return skill_library.load_skill("A17_recommend", name)

        async def _load_reference(skill_name: str, ref_name: str) -> str:
            return skill_library.load_reference("A17_recommend", skill_name, ref_name)

        skill_indexes = skill_library.list_skills("A17_recommend")
        if skill_indexes:
            tools.register("load_skill", _load_skill,
                           "加载指定技能的完整正文（执行步骤/输出格式），参数name=技能名。")
            tools.register("load_reference", _load_reference,
                           "加载技能声明的参考文件，参数skill_name=技能名, ref_name=参考路径。")
        skill_index_block = ""
        if skill_indexes:
            lines = "\n".join(
                f"- {i['name']}：{str(i['description'])[:120]}" for i in skill_indexes
            )
            skill_index_block = (
                f"\n\n## 可用专业技能（L0索引）\n{lines}\n"
                "若某技能与当前综合任务相关，先调用 load_skill 获取其Phase步骤与输出格式，"
                "按技能要求组织final_answer；技能正文声明的references可用load_reference查阅。"
                "无关技能不要加载，避免浪费步数。"
            )

        a17 = agents["A17_recommend"]
        payload = a17._parse_payload({  # noqa: SLF001 复用payload解析与prompt构建
            "analyses": analyses,
            "focus": state.get("target_display") or state["target"],
            "user_query": state["user_query"],
            "hint": state.get("analysis_hint", {}),
        })
        react = ReActExecutor(a17._gateway, tools, max_steps=3, task_tier="decision")  # noqa: SLF001
        cancel_token = state.get("cancellation_token")
        try:
            data = await react.run(
                a17.system_prompt + skill_index_block,
                a17.build_prompt(payload, react_mode=True),
                agent_id=a17.agent_id, trace_id=state["task_id"],
                cancel_token=cancel_token,
            )
        except Exception:  # noqa: BLE001 ReAct失败回退单次调用
            data = None
        # ReAct返回空conclusion（如输出被max_tokens截断导致JSON残缺）→
        # 回退单次直答，避免A17产出空结论被审计判完整性缺失
        if not data or not str(data.get("conclusion", "")).strip():
            logger.warning("A17 ReAct结论为空，回退单次直答（trace=%s）",
                           state["task_id"])
            try:
                update, output = await _run_agent(a17, state, {
                    "analyses": analyses,
                    "focus": state.get("target_display") or state["target"],
                    "user_query": state["user_query"],
                    "hint": state.get("analysis_hint", {}),
                })
                return {**update, "agent_messages": collected_messages,
                        "final_report": None}
            except AgentExecutionError as exc2:
                return {"errors": [f"A17_recommend: {exc2}"], "final_report": None}

        from src.core.models import AgentOutput
        from src.core.schemas import TraceStep, coerce_confidence
        output = AgentOutput(
            task_id=state["task_id"], agent_id=a17.agent_id,
            conclusion=str(data.get("conclusion", "")),
            confidence=coerce_confidence(data.get("confidence", "medium")),
            data_refs=[a.get("agent_id", "?") for a in analyses],
            trace_id=state["task_id"],
            reasoning_steps=[TraceStep(step=1, step_type="llm_inference",
                                       description="ReAct循环综合（含工具调用）")],
            result={**data, "stance": data.get("stance", "中性"),
                    "disclaimer": "以上信息仅供研究参考，不构成投资建议。"},
        )
        update = {
            "agent_outputs": [_summary(output)],
            "data_refs": list(output.data_refs),
            "trace_ids": [output.trace_id],
            "agent_messages": collected_messages,
            "progress": ["A17_recommend 综合分析完成"],
        }
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
    g.add_node("clean", _node(
        "A02_data_cleaner", lambda s: {"data_points": s.get("raw_points", [])}))
    g.add_node("validate", _node(
        "A03_data_validator", lambda s: {"data_points": s.get("cleaned_points", [])}))
    g.add_node("store", _node(
        "A04_data_storage", lambda s: {"data_points": s.get("validated_points", [])}))
    g.add_node("verify_info", _node(
        "A05_verifier", lambda s: {"info_items": s.get("info_items", [])}))
    g.add_node("extract_events", _node("A06_extractor", lambda s: {
        "info_items": [i for i in (s.get("verified_items") or {}).get("items", [])
                       if i.get("verified")],
    }))
    g.add_node("sentiment", _node("A07_sentiment", lambda s: {
        "events": (s.get("extracted_events") or {}).get("events", []),
    }))

    async def liquidity_ctx_node(state: ResearchState) -> dict[str, Any]:
        """流动性周期skill：基于已校验数据点本地计算量能/换手/两融/估值分位/FedWatch研判。

        输出 analysis_hint.market_liquidity，分析层(A08-A16)与A17共享同一份量化参考；
        无流动性类数据点时空跳过；纯计算失败不阻断整图。
        """
        pts = state.get("validated_points", [])
        if not any(str(p.get("indicator", "")).startswith(("mkt:", "idx_val:", "fed:"))
                   for p in pts):
            return {}
        from src.domain.skills.liquidity_cycle import assess_liquidity

        try:
            assessment = assess_liquidity(pts)
        except Exception as exc:  # noqa: BLE001
            return {"errors": [f"liquidity_cycle_skill: 意外异常 {exc}"]}
        return {
            "analysis_hint": {"market_liquidity": assessment},
            "progress": [
                "流动性周期研判完成（量能分层/三市分项/市场宽度/板块估值分位/"
                "换手率/两融/FedWatch）"
            ],
        }

    g.add_node("liquidity_ctx", liquidity_ctx_node)
    for aid in ALL_INSIGHT_AGENTS:
        g.add_node(f"analyze_{aid}", _node(
            aid,
            lambda s, _a=aid: {
                "focus": s.get("target_display") or s["target"],
                "user_query": s["user_query"],
                "data_points": s.get("validated_points", []),
                "hint": s.get("analysis_hint", {}),
                "events": (s.get("extracted_events") or {}).get("events", []),
                "verified_texts": _verified_texts(s),
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
    # 流动性周期本地研判（无流动性数据时空跳过），再扇出分析层
    g.add_edge("sentiment", "liquidity_ctx")
    for aid in ALL_INSIGHT_AGENTS:
        g.add_edge("liquidity_ctx", f"analyze_{aid}")
        g.add_edge(f"analyze_{aid}", "recommend")
    g.add_edge("recommend", "audit")
    g.add_edge("audit", END)

    return g.compile()


def _render_report(state: ResearchState, audit_output: AgentOutput) -> str:
    """把各Agent结论拼装成带溯源与免责声明的最终Markdown报告。"""
    title = state.get("target_display") or state["target"] or state["user_query"]
    lines = [f"# 投研分析报告：{title}", ""]
    info_outs = [o for o in state["agent_outputs"] if o["agent_id"] in INFO_AGENTS]
    if info_outs:
        lines += ["## 信息核验与舆情"]
        for o in info_outs:
            lines.append(
                f"- **{o.get('agent_name') or agent_name(o['agent_id'])}**"
                f"（置信度 {o.get('confidence_zh') or confidence_zh(o['confidence'])}）"
                f"：{o['conclusion']}"
            )
        lines.append("")
    for o in state["agent_outputs"]:
        if o["agent_id"] in ALL_INSIGHT_AGENTS or o["agent_id"] == "A17_recommend":
            lines += [
                f"## {o.get('agent_name') or agent_name(o['agent_id'])}"
                f"（置信度 {o.get('confidence_zh') or confidence_zh(o['confidence'])}）",
                o["conclusion"],
                "",
            ]
    # Agent间多轮对话（A2A协作过程）
    messages = state.get("agent_messages") or []
    if messages:
        lines += ["## Agent协作对话"]
        for m in messages:
            role = "问" if m.get("message_type") == "question" else "答"
            lines.append(
                f"- **{agent_name(m.get('sender'))}** → "
                f"**{agent_name(m.get('receiver'))}** [{role}]: "
                f"{m.get('content','')[:300]}"
            )
        lines.append("")
    stats = state.get("storage_stats") or {}
    if stats:
        lines += ["## 数据入库", f"- 新增 {stats.get('inserted', 0)} 条，"
                  f"重复跳过 {stats.get('skipped', 0)} 条", ""]
    lines += ["## 审计", audit_output.conclusion, "",
              "---",
              "⚠️ 以上信息来自互联网公开资料，仅供研究参考，不构成投资建议。"
              "投资有风险，入市需谨慎，盈亏自负。"]
    return "\n".join(lines)
