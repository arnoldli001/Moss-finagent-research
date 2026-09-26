"""A09 中观分析Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.base import AnalysisAgentBase


class MesoAnalysisAgent(AnalysisAgentBase):
    """产业链分析与行业周期定位（PRD A09，P0）。"""

    system_prompt = (
        "资深行业研究员。依据给定行业数据点（景气/库存/产能/价格）与产业事件分析产业链。\n"
        "- 生命周期：导入/成长/成熟/衰退；\n"
        "- 流动性优先：先看两市总量阶段→三市成交额占比判风格→双创PE分位判冷热→"
        "再定板块轮动（缩量回踩不追高、沿产业链找低位扩散）；不复述无关宏观。"
    )

    def __init__(self, gateway, agent_id: str = "A09_meso",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["industry_cycle", "chain_analysis"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _requirements(self, payload) -> str:
        return (
            "输出JSON：\n"
            '- "conclusion": 首句直接答问并点出具体细分环节/方向，200字内\n'
            '- "confidence": high|medium|low\n'
            '- "industry_cycle": 导入期|成长期|成熟期|衰退期|不明确\n'
            '- "prosperity": 高|中|低\n'
            '- "chain_position": 产业链位置一句话\n'
            '- "key_points": 3-5条\n'
            '- "risks": 1-3条'
        )
