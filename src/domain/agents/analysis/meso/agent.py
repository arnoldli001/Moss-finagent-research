"""A09 中观分析Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.base import AnalysisAgentBase


class MesoAnalysisAgent(AnalysisAgentBase):
    """产业链分析与行业周期定位（PRD A09，P0）。"""

    system_prompt = (
        "你是资深行业研究员。基于给定的行业数据点（行业景气度、库存、产能利用率、"
        "产业链价格等）与宏观上下文，进行产业链分析与行业周期定位。要求：\n"
        "1. 判断需引用给定数据，禁止编造；\n"
        "2. 行业生命周期判断采用导入期/成长期/成熟期/衰退期框架；\n"
        "3. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A09_meso") -> None:
        super().__init__(agent_id, gateway)

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
            "请输出JSON对象，字段：\n"
            '- "conclusion": 行业综合研判（150字内，必须点名关键数据）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "industry_cycle": "导入期"|"成长期"|"成熟期"|"衰退期"|"不明确"\n'
            '- "prosperity": "高"|"中"|"低"（景气度）\n'
            '- "chain_position": 产业链上下游位置一句话\n'
            '- "key_points": 3-5条要点\n'
            '- "risks": 主要行业风险1-3条'
        )
