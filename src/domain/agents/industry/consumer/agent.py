"""A14 消费行业Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching
from src.domain.agents.industry.base import IndustryAgentBase


class ConsumerIndustryAgent(IndustryAgentBase):
    """消费行业深度分析（PRD A14，P2）：消费升降级 + 渠道库存 + 品牌力。"""

    industry_name = "消费"
    framework = "消费升级/降级分层 + 渠道库存周期 + 品牌溢价持续性"
    watch_keywords = ("CPI", "社零", "零售", "客单价", "毛利", "白酒", "食品",
                      "消费", "ind:sw_", "ind:penetration")
    pe_high_watermark = 35.0
    capabilities_names = ("consumer_demand_analysis", "channel_inventory_tracking")

    system_prompt = (

        render_unlock_teaching("A14_consumer")

        +
        "资深消费行业分析师，熟悉食品饮料/家电/零售/可选消费。"
        "区分必选/可选需求韧性；关注渠道去库存进度与品牌提价能力。"
    )

    def __init__(self, gateway, agent_id: str = "A14_consumer") -> None:
        super().__init__(agent_id, gateway)
