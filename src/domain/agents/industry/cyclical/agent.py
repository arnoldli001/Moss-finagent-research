"""A15 周期行业Agent。"""

from __future__ import annotations

from src.domain.agents.industry.base import IndustryAgentBase


class CyclicalIndustryAgent(IndustryAgentBase):
    """周期行业深度分析（PRD A15，P2）：基钦库存周期 + 供需缺口 + 价格弹性。"""

    industry_name = "周期"
    framework = "基钦库存周期（被动去库→主动补库→被动补库→主动去库）与供需缺口"
    watch_keywords = ("PPI", "库存", "产能", "价格", "煤炭", "有色", "钢铁", "化工",
                      "原油", "水泥")
    pe_high_watermark = 20.0
    capabilities_names = ("inventory_cycle_analysis", "supply_gap_pricing")

    system_prompt = (
        "你是资深周期行业分析师，熟悉煤炭、有色、钢铁、化工与建材。基于给定数据点、"
        "本地景气信号（PPI/库存/价格方向、估值旗标）与信息层事件，按基钦库存周期"
        "定位当前阶段并评估价格弹性。要求：\n"
        "1. 明确库存周期四阶段定位，结合PPI与库存方向交叉验证；\n"
        "2. 注意周期股估值反身性：高盈利低PE常对应周期顶部，不得简单以低PE论便宜；\n"
        "3. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A15_cyclical") -> None:
        super().__init__(agent_id, gateway)
