"""A13 科技行业Agent。"""

from __future__ import annotations

from src.domain.agents.industry.base import IndustryAgentBase


class TechIndustryAgent(IndustryAgentBase):
    """科技行业深度分析（PRD A13，P2）：渗透率S曲线 + 技术成熟度 + 研发驱动。"""

    industry_name = "科技"
    framework = "技术成熟度曲线与渗透率S曲线（导入期→成长期→成熟期→衰退期）"
    watch_keywords = ("半导体", "芯片", "出货量", "研发", "渗透率", "算力", "AI", "电子")
    pe_high_watermark = 50.0  # 成长板块容忍更高估值
    capabilities_names = ("tech_cycle_analysis", "penetration_rate_tracking")

    system_prompt = (
        "你是资深科技行业分析师，熟悉半导体、消费电子、软件与AI产业链。基于给定"
        "数据点、本地景气信号（关注指标环比方向、估值旗标）与信息层事件，按技术"
        "成熟度曲线与渗透率S曲线判断行业所处阶段。要求：\n"
        "1. 景气判断必须引用本地信号数值，禁止脱离数据预测具体涨跌幅；\n"
        "2. 关注技术替代风险与渗透率天花板；\n"
        "3. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A13_tech") -> None:
        super().__init__(agent_id, gateway)
