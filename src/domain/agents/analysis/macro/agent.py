"""A08 宏观分析Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.base import AnalysisAgentBase


class MacroAnalysisAgent(AnalysisAgentBase):
    """宏观经济周期定位与流动性分析（PRD A08，P0）。"""

    system_prompt = (
        "你是资深宏观分析师。基于给定的宏观经济数据点（CPI/PPI/PMI/GDP/社融/M2等），"
        "进行经济周期定位与流动性环境判断。要求：\n"
        "1. 结论必须引用数据佐证，禁止脱离给定数据编造数值；\n"
        "2. 周期判断遵循美林时钟框架（复苏/过热/滞胀/衰退）；\n"
        "3. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A08_macro") -> None:
        super().__init__(agent_id, gateway)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["cycle_positioning", "liquidity_analysis"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _requirements(self, payload) -> str:
        return (
            "请输出JSON对象，字段：\n"
            '- "conclusion": 宏观综合研判（150字内，必须点名关键数据）\n'
            '- "confidence": "high"|"medium"|"low"（数据充分且方向一致为high）\n'
            '- "cycle_position": "复苏"|"过热"|"滞胀"|"衰退"|"不明确"\n'
            '- "liquidity": "宽松"|"中性"|"收紧"|"不明确"\n'
            '- "key_points": 3-5条要点\n'
            '- "risks": 主要宏观风险1-3条'
        )
