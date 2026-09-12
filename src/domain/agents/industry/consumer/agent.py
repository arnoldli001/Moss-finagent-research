"""A14 消费行业Agent。"""

from __future__ import annotations

from src.domain.agents.industry.base import IndustryAgentBase


class ConsumerIndustryAgent(IndustryAgentBase):
    """消费行业深度分析（PRD A14，P2）：消费升降级 + 渠道库存 + 品牌力。"""

    industry_name = "消费"
    framework = "消费升级/降级分层 + 渠道库存周期 + 品牌溢价持续性"
    watch_keywords = ("CPI", "社零", "零售", "客单价", "毛利", "白酒", "食品", "消费")
    pe_high_watermark = 35.0
    capabilities_names = ("consumer_demand_analysis", "channel_inventory_tracking")

    system_prompt = (
        "你是资深消费行业分析师，熟悉食品饮料、家电、零售与可选消费。基于给定"
        "数据点、本地景气信号（社零/CPI方向、渠道与估值旗标）与信息层事件，判断"
        "消费需求处于升级、分级还是降级阶段。要求：\n"
        "1. 区分必选消费与可选消费的需求韧性差异；\n"
        "2. 关注渠道去库存进度与品牌提价能力，结论必须引用数据；\n"
        "3. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A14_consumer") -> None:
        super().__init__(agent_id, gateway)
