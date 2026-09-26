"""A08 宏观分析Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.base import AnalysisAgentBase


class MacroAnalysisAgent(AnalysisAgentBase):
    """宏观经济周期定位与流动性分析（PRD A08，P0）。"""

    # 月度宏观指标最近12期足以判断趋势，全量历史会冲淡用户问题焦点
    context_max_periods = 12

    system_prompt = (
        "资深宏观分析师。依据给定中国(CPI/PPI/M2/社融)与美国(CPI/核心CPI/非农/失业率/"
        "联邦利率/PCE)数据点及财经事件，判断经济周期与流动性。\n"
        "- 问加息/降息而无FedWatch：声明数据缺口，按已有数据给方向"
        "（如通胀回落→降息概率升），禁答「无法判断」；\n"
        "- 周期用美林时钟：复苏/过热/滞胀/衰退。"
    )

    def __init__(self, gateway, agent_id: str = "A08_macro",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

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
            "输出JSON：\n"
            '- "conclusion": 首句直接答问（缺对应量化数据时声明缺口并给方向，'
            "禁杜撰百分比），200字内\n"
            '- "confidence": high|medium|low\n'
            '- "cycle_position": 复苏|过热|滞胀|衰退|不明确\n'
            '- "liquidity": 宽松|中性|收紧|不明确\n'
            '- "key_points": 3-5条\n'
            '- "risks": 1-3条'
        )
