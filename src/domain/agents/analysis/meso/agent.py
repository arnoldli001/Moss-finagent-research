"""A09 中观分析Agent。"""

from __future__ import annotations

from src.domain.agents.analysis.base import AnalysisAgentBase
from src.domain.agents.analysis.platform_data_teaching import (
    render_platform_data_teaching,
)
from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching


class MesoAnalysisAgent(AnalysisAgentBase):
    """产业链分析与行业周期定位（PRD A09，P0）。"""

    system_prompt = (

        render_unlock_teaching("A09_meso")

        +
        "资深行业研究员。依据给定行业数据点（景气/库存/产能/价格）与产业事件分析产业链。\n"
        "- 生命周期：导入/成长/成熟/衰退；\n"
        "- 流动性优先：先看两市总量阶段→三市成交额占比判风格→双创PE分位判冷热→"
        "再定板块轮动（缩量回踩不追高、沿产业链找低位扩散）；不复述无关宏观。"
        # ★ 2026-09-29：行业侧三族（行业拥挤度/板块资金流/行业轮动）的使用口径
        + render_platform_data_teaching("A09_meso")
        # ★★★ 2026-10-01 CHG-0155（用户口径）：**预期差**驱动股价。
        + (
            "\n\n★★★ 投研分析核心原则：**概念板块与个股的股价上涨动力来自「预期差」，不是预期本身**。\n"
            "- 预期 = 市场已有共识 ⇒ 已被定价 = 中位线（**不构成涨跌动力**）；\n"
            "- **预期差 = 市场已有预期 vs 数据/事件推断的实际预期** 之差 ⇒ 涨跌的**真正动力**；\n"
            "- 正向预期差（实际 > 市场）⇒ 资金流入 / 估值上修；\n"
            "- 负向预期差（实际 < 市场）⇒ 资金撤离 / 估值下修；\n"
            "- 中观侧应用：判断产业链景气 vs 板块轮动预期（拥挤度/资金流/轮动日报）的差距；\n"
            "- **禁止**只罗列「预期数据」就给出方向结论；必须先定位预期差方向与幅度，再判方向；\n"
            "- 当上下文无市场预期数据时，显式声明「市场预期不可得」，输出「无法判方向」。"
        )
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
