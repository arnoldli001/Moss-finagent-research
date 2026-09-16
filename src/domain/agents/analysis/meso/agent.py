"""A09 中观分析Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.base import AnalysisAgentBase


class MesoAnalysisAgent(AnalysisAgentBase):
    """产业链分析与行业周期定位（PRD A09，P0）。"""

    system_prompt = (
        "你是资深行业研究员。基于给定的行业数据点（行业景气度、库存、产能利用率、"
        "产业链价格等）与信息层产业事件，回答用户的行业/产业链提问。要求：\n"
        "1. conclusion必须先正面回答用户提问（如指出具体细分环节/标的），再引用给定数据，"
        "禁止编造，禁止复述与本行业无关的宏观数据；\n"
        "2. 行业生命周期判断采用导入期/成长期/成熟期/衰退期框架；\n"
        "3. 中观研判遵循流动性优先：先看两市总量与流动性阶段，再看上证/创业板/科创板"
        "三市分项成交额占比判断风格偏权重还是成长、双创板块PE分位判断冷热，"
        "最后才落到板块轮动方向与操作（缩量阶段低吸不追高、沿产业链找低位扩散）；\n"
        "4. 输出仅为研究参考，不构成投资建议。"
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
        answer_rule = (
            f"conclusion首句必须直接回答用户问题「{payload.user_query[:80]}」，"
            "明确点出具体细分环节/方向，再做行业综合研判；"
            if payload.user_query else
            "行业综合研判（150字内，必须点名关键数据）；"
        )
        return (
            "请输出JSON对象，字段：\n"
            f'- "conclusion": {answer_rule}（200字内）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "industry_cycle": "导入期"|"成长期"|"成熟期"|"衰退期"|"不明确"\n'
            '- "prosperity": "高"|"中"|"低"（景气度）\n'
            '- "chain_position": 产业链上下游位置一句话\n'
            '- "key_points": 3-5条要点\n'
            '- "risks": 主要行业风险1-3条'
        )
