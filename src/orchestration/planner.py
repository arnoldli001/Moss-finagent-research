"""LLM驱动的Supervisor规划器：把用户问题分解为执行计划。

PRD要求"Supervisor通过LLM动态决策任务执行顺序"。本模块用LLM理解用户意图，
从18个Agent能力目录中选择参与Agent与采集指标；LLM失败时由supervisor回退
到规则式plan_run()，保证链路不中断。
"""

from __future__ import annotations

from typing import Any

from src.domain.agents.analysis.base import parse_llm_json
from src.infrastructure.llm import LLMGateway

# 可用指标目录（与supervisor._PLANNING/INDUSTRY_INDICATORS对齐），
# LLM规划时据此选择需要采集的指标，避免凭空编造不存在的indicator id。
INDICATOR_CATALOG: list[dict[str, str]] = [
    {"id": "CPI", "desc": "中国居民消费价格指数同比（月度）"},
    {"id": "PPI", "desc": "中国工业生产者出厂价格指数同比（月度）"},
    {"id": "us_cpi_yoy", "desc": "美国CPI同比（月度，AkShare macro_usa_cpi_yoy）"},
    {"id": "us_core_cpi", "desc": "美国核心CPI同比（月度，AkShare macro_usa_core_cpi_monthly）"},
    {"id": "us_nonfarm", "desc": "美国非农就业新增（月度，AkShare macro_usa_non_farm）"},
    {"id": "us_unemployment", "desc": "美国失业率（月度，AkShare macro_usa_unemployment_rate）"},
    {"id": "us_fed_rate", "desc": "美联储利率决议（FOMC，AkShare macro_bank_usa_interest_rate）"},
    {"id": "us_pce", "desc": "美国核心PCE物价指数（月度，AkShare macro_usa_core_pce_price）"},
    {"id": "stock_close", "desc": "个股日收盘价（需带6位代码后缀，如stock_close:300308）"},
    {"id": "PE(TTM)", "desc": "个股滚动市盈率（需带代码后缀）"},
    {"id": "PB", "desc": "个股市净率（需带代码后缀）"},
    {"id": "资产负债率", "desc": "个股资产负债率（需带代码后缀）"},
    {"id": "流动比率", "desc": "个股流动比率（需带代码后缀）"},
    {"id": "ind:半导体销售额同比", "desc": "半导体行业销售额同比（科技行业）"},
    {"id": "ind:芯片出货量同比", "desc": "芯片出货量同比（科技行业）"},
    {"id": "ind:科技行业PE(TTM)", "desc": "科技行业PE估值"},
    {"id": "ind:社会消费品零售总额同比", "desc": "社零同比（消费行业）"},
    {"id": "ind:白酒批价(元/瓶)", "desc": "白酒批价（消费行业）"},
    {"id": "ind:消费行业PE(TTM)", "desc": "消费行业PE估值"},
    {"id": "ind:动力煤价格(元/吨)", "desc": "动力煤价格（周期行业）"},
    {"id": "ind:重点电厂煤炭库存(万吨)", "desc": "电厂煤炭库存（周期行业）"},
    {"id": "ind:周期行业PE(TTM)", "desc": "周期行业PE估值"},
    {"id": "ind:创新药IND申报数量(个)", "desc": "创新药IND申报件数（医药行业，CDE受理，月度）"},
    {"id": "ind:医药行业PE(TTM)", "desc": "医药行业PE估值"},
    # 申万行业估值截面（AKShare sw_index_third_info，335个三级行业）
    {"id": "ind:sw_third_pe_ttm:all", "desc": "申万三级行业PE-TTM截面（335个行业，日频）"},
    {"id": "ind:sw_third_pb:all", "desc": "申万三级行业PB截面（335个行业，日频）"},
    {"id": "ind:sw_second_pe_ttm:all", "desc": "申万二级行业PE-TTM截面（131个行业）"},
    {"id": "ind:sw_first_pe_ttm:all", "desc": "申万一级行业PE-TTM截面（31个行业）"},
    {"id": "ind:sw_third_dividend_yield:all", "desc": "申万三级行业股息率截面"},
    # 渗透率数据（多源采集：静态基准+新闻提取+研报搜索）
    {"id": "ind:penetration:新能源汽车", "desc": "新能源汽车渗透率（%）"},
    {"id": "ind:penetration:人形机器人", "desc": "人形机器人渗透率（%）"},
    {"id": "ind:penetration:固态电池", "desc": "固态电池渗透率（%）"},
    {"id": "ind:penetration:AI大模型应用", "desc": "AI大模型应用渗透率（%）"},
    {"id": "ind:penetration:半导体国产替代", "desc": "半导体国产替代渗透率（%）"},
    {"id": "ind:penetration:HBM存储", "desc": "HBM存储渗透率（%）"},
    # A股大盘流动性（免费实时源，行业/个股买卖与持有期研判必选）
    {"id": "mkt:turnover:total",
     "desc": "两市合计成交额（亿元，腾讯/东财实时，盘中快照）"},
    {"id": "mkt:turnover:hist",
     "desc": "两市成交额近60交易日序列（亿元，用于MA5/10/50与量能分层）"},
    {"id": "mkt:turnover_rate:all_a",
     "desc": "全A加权换手率（%）与前5%个股成交集中度（东财clist聚合）"},
    {"id": "mkt:margin_balance",
     "desc": "两市融资融券余额合计（亿元，交易所T+1）"},
    {"id": "mkt:margin_balance:hist",
     "desc": "两融余额近10交易日序列（亿元，杠杆趋势）"},
    {"id": "mkt:north_flow",
     "desc": "北向资金日度净买额（亿元；2024-08起停披，返回停披前最后值）"},
    # 科创板/创业板板块数据（四类数据全主备冗余：腾讯/东财/AKShare/乐咕/新浪）
    {"id": "mkt:cybkcb:turnover:all",
     "desc": "创业板指/科创50/科创综指实时成交额（亿元，腾讯→东财双源；中观三市分项判风格）"},
    {"id": "mkt:cybkcb:turnover_hist",
     "desc": "三指数近60交易日成交额序列（亿元，value=创业板+科创板全板口径）"},
    {"id": "mkt:cybkcb:val:all",
     "desc": "创业板/科创板板块整体PE现值及1/3/5年/全历史分位（乐咕→东财/中证兜底）"},
    {"id": "mkt:cybkcb:spot_summary",
     "desc": "两板个股截面市场宽度：涨跌家数/上涨占比/涨跌幅中位数/成交合计/涨幅前5"},
    {"id": "idx_val:snapshot:all",
     "desc": ("6只核心宽基PE/PB及1/3/5年/全历史分位（沪深300/中证500/中证1000/"
              "上证50/创业板50走乐咕全历史，科创50走中证官网现值PE）")},
    {"id": "fed:rate_prob:next",
     "desc": "CME FedWatch下次FOMC各利率区间概率（外网不可用降级为空）"},
]

SYSTEM_PROMPT = (
    "你是投研系统的总调度Supervisor。根据用户提问，从能力目录中选择需要参与的Agent"
    "与需要采集的数据指标，生成执行计划。要求：\n"
    "1. 只从给定的Agent能力目录中选择，禁止编造未列出的Agent；\n"
    "2. 只从给定的指标目录中选择，个股类指标(stock_close/PE(TTM)/PB/资产负债率/流动比率)"
    "必须拼接6位代码后缀（如PE(TTM):300308）；\n"
    "3. 选择必须与用户问题直接相关：\n"
    "   - 问加息/降息/美联储/FOMC/利率/美债/美股宏观等→analysis_type='macro',"
    "选A08_macro + 美国宏观指标(us_cpi_yoy/us_fed_rate/us_nonfarm/us_pce等)；\n"
    "   - 问中国宏观(CPI/PPI/社融)→analysis_type='macro',选A08_macro + CPI/PPI；\n"
    "   - 问大盘预测/明日A股/后市/复盘今日股市/行情展望→analysis_type='macro',"
    "选A08_macro；\n"
    "   - 问行业/产业链/板块交易策略/板块还能不能追/板块轮动→"
    "analysis_type='industry',选A09_meso + 对应行业Agent(A13-A16) + 行业指标 + "
    "申万行业估值(ind:sw_third_pe_ttm:all等) + 渗透率(ind:penetration:赛道名)；\n"
    "   - 问个股→选A10_micro/A11_fin_risk/A12_compliance + 个股指标(带代码后缀)；\n"
    "   - 凡问大盘预测/复盘/个股或行业的买入卖出/短线操作建议/加仓减仓/止损/"
    "仓位/持有期收益/板块交易策略/当前A股流动性或成交额/两融/北向/换手率/估值分位"
    "→除行业/个股/宏观指标外，必须同时选大盘流动性指标"
    "(mkt:turnover:total/mkt:turnover:hist/mkt:turnover_rate:all_a/"
    "mkt:margin_balance/mkt:margin_balance:hist/mkt:north_flow/"
    "idx_val:snapshot:all)与科创板/创业板中观指标"
    "(mkt:cybkcb:turnover:all/mkt:cybkcb:val:all/mkt:cybkcb:spot_summary)；"
    "问题同时涉及宏观/全球流动性、美联储、降息时加选fed:rate_prob:next；\n"
    "4. 信息层Agent(A05/A06/A07)在有新闻文本时才需要，否则不选；\n"
    "5. 输出必须为合法JSON，不要输出推理过程。"
)

_PLAN_SCHEMA = (
    '输出JSON对象，字段：\n'
    '- "analysis_type": "macro"|"industry"|"stock"|"news"|"full"\n'
    '- "target": 标的标识（个股为6位代码，宏观/行业为主题词，无则空串）\n'
    '- "agents": 参与Agent的agent_id列表（必须来自能力目录，含A17_recommend与A18_audit）\n'
    '- "indicators": 需要采集的指标id列表（必须来自指标目录）\n'
    '- "reasoning": 规划理由（50字内）'
)


class LLMSupervisorPlanner:
    """用LLM把用户问题分解为Agent执行计划。"""

    def __init__(self, gateway: LLMGateway, *, max_agents: int = 12) -> None:
        self._gateway = gateway
        self._max_agents = max_agents

    def _build_capabilities_text(self, agents: dict[str, Any]) -> str:
        lines = []
        for aid, agent in agents.items():
            try:
                cap = agent.get_capabilities()
            except Exception:  # noqa: BLE001 规划阶段不阻断
                cap = {"capabilities": []}
            caps = cap.get("capabilities", []) if isinstance(cap, dict) else []
            lines.append(f"- {aid}: {', '.join(str(c) for c in caps) or '未声明'}")
        return "\n".join(lines)

    def _build_indicators_text(self) -> str:
        return "\n".join(f"- {i['id']}: {i['desc']}" for i in INDICATOR_CATALOG)

    async def plan(
        self,
        user_query: str,
        target: str,
        agents: dict[str, Any],
    ) -> dict[str, Any] | None:
        """LLM规划，失败返回None（由调用方回退规则路由）。"""
        if not user_query and not target:
            return None
        prompt = (
            f"## 用户提问\n{user_query or '（无）'}\n"
            f"## 用户指定标的/主题\n{target or '（无）'}\n\n"
            f"## Agent能力目录\n{self._build_capabilities_text(agents)}\n\n"
            f"## 可用指标目录\n{self._build_indicators_text()}\n\n"
            f"## 任务要求\n{_PLAN_SCHEMA}"
        )
        try:
            # 规划是从指标目录+Agent目录里挑子集的简单任务，light层(qwen2.5:1.5b)足够，
            # 本地 2-5s vs deepseek-flash 18s，省大头
            response = await self._gateway.complete(
                "light", SYSTEM_PROMPT, prompt,
                agent_id="supervisor_planner", trace_id="planner", json_mode=True,
            )
            data = parse_llm_json("supervisor_planner", response.content)
        except Exception:  # noqa: BLE001 LLM规划失败不阻断，回退规则
            return None
        agents_plan = data.get("agents") or []
        if not isinstance(agents_plan, list) or not agents_plan:
            return None
        # 必须包含决策与审计（否则链路不完整）
        for required in ("A17_recommend", "A18_audit"):
            if required not in agents_plan:
                agents_plan.append(required)
        # 去重保序，限制数量
        seen: list[str] = []
        for a in agents_plan:
            if a not in seen and isinstance(a, str):
                seen.append(a)
        agents_plan = seen[: self._max_agents]
        indicators = data.get("indicators") or []
        if not isinstance(indicators, list):
            indicators = []
        indicators = [str(i) for i in indicators if isinstance(i, str)][:20]
        return {
            "analysis_type": str(data.get("analysis_type", "full")),
            "target": str(data.get("target", target or "")),
            "agents": agents_plan,
            "indicators": indicators,
            "reasoning": str(data.get("reasoning", ""))[:200],
        }
