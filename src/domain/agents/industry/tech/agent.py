"""A13 科技行业Agent。"""

from __future__ import annotations

from src.domain.agents.industry.base import IndustryAgentBase


class TechIndustryAgent(IndustryAgentBase):
    """科技行业深度分析（PRD A13，P2）：渗透率S曲线 + 技术成熟度 + 研发驱动。"""

    industry_name = "科技"
    framework = "技术成熟度曲线与渗透率S曲线（导入期→成长期→成熟期→衰退期）"
    watch_keywords = ("半导体", "芯片", "出货量", "研发", "渗透率", "算力",
                       "AI", "电子", "ind:sw_", "ind:penetration")
    pe_high_watermark = 50.0  # 成长板块容忍更高估值
    capabilities_names = ("tech_cycle_analysis", "penetration_rate_tracking")

    system_prompt = (
        "资深科技行业分析师，熟悉半导体/消费电子/软件/AI产业链。"
        "关注技术替代风险与渗透率天花板；禁脱离数据预测具体涨跌幅。"
    )

    def __init__(self, gateway, agent_id: str = "A13_tech") -> None:
        super().__init__(agent_id, gateway)
