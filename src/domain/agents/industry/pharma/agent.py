"""A16 医药行业Agent。"""

from __future__ import annotations

from src.domain.agents.industry.base import IndustryAgentBase


class PharmaIndustryAgent(IndustryAgentBase):
    """医药行业深度分析（PRD A16，P2）：政策周期（集采/医保）+ 研发管线兑现。"""

    industry_name = "医药"
    framework = "政策周期（集采/医保谈判）× 研发管线兑现节奏 双轮驱动"
    watch_keywords = ("集采", "医保", "研发费用", "管线", "医药", "医疗", "创新药",
                      "器械", "临床")
    pe_high_watermark = 45.0
    capabilities_names = ("policy_cycle_analysis", "pipeline_valuation")

    system_prompt = (
        "你是资深医药行业分析师，熟悉创新药、医疗器械、医疗服务与中药。基于给定"
        "数据点、本地景气信号（研发投入/政策相关指标方向、估值旗标）与信息层事件，"
        "按政策周期与研发管线兑现双轮框架研判。要求：\n"
        "1. 区分集采政策压制的仿制药板块与管线驱动的创新药板块；\n"
        "2. 管线价值须以临床进展/获批事件为锚，禁止对在研产品收入做无依据假设；\n"
        "3. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A16_pharma") -> None:
        super().__init__(agent_id, gateway)
