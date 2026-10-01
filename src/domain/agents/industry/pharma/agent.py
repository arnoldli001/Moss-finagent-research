"""A16 医药行业Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching
from src.domain.agents.industry.base import IndustryAgentBase


class PharmaIndustryAgent(IndustryAgentBase):
    """医药行业深度分析（PRD A16，P2）：政策周期（集采/医保）+ 研发管线兑现。"""

    industry_name = "医药"
    framework = "政策周期（集采/医保谈判）× 研发管线兑现节奏 双轮驱动"
    watch_keywords = ("集采", "医保", "研发费用", "管线", "医药", "医疗", "创新药",
                      "器械", "临床", "ind:sw_", "ind:penetration")
    pe_high_watermark = 45.0
    capabilities_names = ("policy_cycle_analysis", "pipeline_valuation")

    system_prompt = (

        render_unlock_teaching("A16_pharma")

        +
        "资深医药行业分析师，熟悉创新药/器械/医疗服务/中药。"
        "区分集采压制的仿制药与管线驱动的创新药；管线价值以临床/获批事件为锚，"
        "禁对在研产品收入做无据假设。"
    )

    def __init__(self, gateway, agent_id: str = "A16_pharma") -> None:
        super().__init__(agent_id, gateway)
