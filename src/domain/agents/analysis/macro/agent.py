"""A08 宏观分析Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.base import AnalysisAgentBase


class MacroAnalysisAgent(AnalysisAgentBase):
    """宏观经济周期定位与流动性分析（PRD A08，P0）。"""

    # 月度宏观指标最近12期足以判断趋势，全量历史会冲淡用户问题焦点
    context_max_periods = 12

    system_prompt = (
        "你是资深宏观分析师。基于给定的宏观经济数据点（中国CPI/PPI/M2/社融、"
        "美国CPI/核心CPI/非农/失业率/美联储利率/PCE等）与信息层财经事件，"
        "回答用户的宏观/海外市场提问并做经济周期与流动性判断。要求：\n"
        "1. conclusion必须先正面回答用户提问，再引用数据佐证，禁止脱离给定数据编造数值；\n"
        "2. 问加息/降息概率时：基于已采集的CPI/PCE/非农/利率数据做趋势判断，"
        "如未接入CME FedWatch期货隐含概率，须明确声明数据缺口，"
        "但必须基于已给数据给出方向性判断（如通胀回落→降息概率上升），禁止回答'无法判断'；\n"
        "3. 周期判断遵循美林时钟框架（复苏/过热/滞胀/衰退）；\n"
        "4. 输出仅为研究参考，不构成投资建议。"
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
        answer_rule = (
            f"conclusion首句必须直接回答用户问题「{payload.user_query[:80]}」"
            "（问概率/数值而系统未接入对应量化数据时，须明确说明数据缺口"
            "（如未接入CME FedWatch）并给方向性判断，禁止杜撰百分比），再做综合研判；"
            if payload.user_query else
            "宏观综合研判（150字内，必须点名关键数据）；"
        )
        return (
            "请输出JSON对象，字段：\n"
            f'- "conclusion": {answer_rule}（200字内）\n'
            '- "confidence": "high"|"medium"|"low"（数据充分且方向一致为high）\n'
            '- "cycle_position": "复苏"|"过热"|"滞胀"|"衰退"|"不明确"\n'
            '- "liquidity": "宽松"|"中性"|"收紧"|"不明确"\n'
            '- "key_points": 3-5条要点\n'
            '- "risks": 主要宏观风险1-3条'
        )
