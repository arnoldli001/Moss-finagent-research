"""A15 周期行业Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching
from src.domain.agents.industry.base import IndustryAgentBase


class CyclicalIndustryAgent(IndustryAgentBase):
    """周期行业深度分析（PRD A15，P2）：基钦库存周期 + 供需缺口 + 价格弹性。"""

    industry_name = "周期"
    framework = "基钦库存周期（被动去库→主动补库→被动补库→主动去库）与供需缺口"
    watch_keywords = ("PPI", "库存", "产能", "价格", "煤炭", "有色", "钢铁", "化工",
                      "原油", "水泥", "ind:sw_", "ind:penetration")
    pe_high_watermark = 20.0
    capabilities_names = ("inventory_cycle_analysis", "supply_gap_pricing")

    system_prompt = (

        render_unlock_teaching("A15_cyclical")

        +
        "资深周期行业分析师，熟悉煤炭/有色/钢铁/化工/建材。"
        "库存四阶段定位须结合PPI与库存方向交叉验证；警惕估值反身性："
        "高盈利低PE常为周期顶部，勿以低PE论便宜。"
    )

    def __init__(self, gateway, agent_id: str = "A15_cyclical") -> None:
        super().__init__(agent_id, gateway)
