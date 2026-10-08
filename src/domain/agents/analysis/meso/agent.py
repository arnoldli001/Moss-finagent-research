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
            "\n\n★★★ 投研分析核心原则：**概念板块与个股的股价上涨动力来自「预期差」，"
            "不是预期本身**。\n"
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
        """任务要求。★ 多行业问句时**逐行业各一节**（`CHG-0217` 的并集清单）。

        ## 为什么（同 `CHG-0216`/`CHG-0217` 的用户原话）

        > 「…基于当前板块拥挤度和能源重点项目与新业态投资20万亿的政策，
        >   未来半年能否持有高股息的**宁波银行**和**中国神华**？标的 601088」

        本 Agent 的输出契约只有**一组** `industry_cycle`/`chain_position`
        ⇒ 一条问句里两个行业时，模型只会挑一个写，**另一个行业的中观结论
        在交付里根本不存在**（用户读到"只有中国神华有行业结论"）。

        ## 行业清单**不由本层解析**

        `hint["focus_industries"]` 由编排层下发（`_build_analyze_payload_fn`，
        取自 `supervisor.resolve_focus_industries` —— 与"挂哪些行业 Agent"
        同一条**唯一**判据）。本层只**渲染**：再解析一次必然漂移，
        而漂移的表现是"挂的是 A 行业、写的是 B 行业"，**不报错**。

        ⚠️ **单行业（或无清单）时返回的字符串与修复前逐字相同**
        —— 编排层只在 ≥2 个行业时才写这个键（`tests/unit/…` 有冻结断言）。
        """
        base = (
            "输出JSON：\n"
            '- "conclusion": 首句直接答问并点出具体细分环节/方向，200字内\n'
            '- "confidence": high|medium|low\n'
            '- "industry_cycle": 导入期|成长期|成熟期|衰退期|不明确\n'
            '- "prosperity": 高|中|低\n'
            '- "chain_position": 产业链位置一句话\n'
            '- "key_points": 3-5条\n'
            '- "risks": 1-3条'
        )
        industries = [str(x) for x in (payload.hint.get("focus_industries") or [])
                      if x]
        if len(industries) < 2:
            return base
        listing = "、".join(industries)
        return base + (
            f"\n\n★★ 本次问句涉及 **{len(industries)} 个行业**（{listing}）—— "
            "上面每个字段都必须**逐行业各写一份**，不许只写其中一个行业：\n"
            f'- "conclusion"：按行业分段，**每段首句点名行业**，'
            f"{listing} 每个行业至少一段；\n"
            '- "industry_cycle"：逐行业写「行业名=导入期|成长期|成熟期|衰退期|不明确」'
            "（用「；」分隔），每个行业一个，不许只给一个行业；\n"
            '- "prosperity"：同上，逐行业写「行业名=高|中|低」；\n'
            '- "chain_position"：逐行业各一句，写清该行业在产业链中的位置；\n'
            '- "key_points"/"risks"：**按行业分组**，每条注明属于哪个行业；\n'
            f"某个行业（{listing} 中的任一个）在输入数据里没有对应数据点时，"
            "**为该行业单独写明数据缺口**，禁止用另一个行业的数据代填。"
        )
