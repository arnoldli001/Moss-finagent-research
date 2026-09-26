"""A12 合规爆雷Agent（合规风险与爆雷预警，PRD A12，P2）。

本地规则引擎（logic.py）计算合规旗标与爆雷等级，覆盖比率异常、存贷双高、
信息层诉讼/监管事件三类信号；LLM只负责综合定性，禁止自造风险事实。
"""

from __future__ import annotations

from typing import Any

from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload
from src.domain.agents.analysis.compliance.logic import evaluate_compliance


class ComplianceAnalysisAgent(AnalysisAgentBase):
    """合规风险、爆雷风险预警（PRD A12，P2）。"""

    system_prompt = (
        "严谨合规风控专员，负责上市公司合规排雷与爆雷预警。依据财务比率、本地规则旗标"
        "与诉讼/监管事件研判；未提及的违规/诉讼不断言。爆雷等级须与旗标数量、"
        "严重程度自洽，不得弱化严重旗标。"
    )

    def __init__(self, gateway, agent_id: str = "A12_compliance",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["compliance_risk", "fraud_burst_warning",
                             "litigation_monitoring"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _prepare(self, payload: AnalysisPayload) -> None:
        payload.hint["compliance_calc"] = evaluate_compliance(
            payload.data_points, payload.events
        )

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        calc = payload.hint.get("compliance_calc", {})
        data["compliance_flags_calc"] = calc.get("compliance_flags")
        data["severe_flag_count"] = calc.get("severe_flag_count")
        data["compliance_level_calc"] = calc.get("compliance_level_calc")
        return data

    def _requirements(self, payload: AnalysisPayload) -> str:
        calc_level = payload.hint.get("compliance_calc", {}).get("compliance_level_calc", "无")
        return (
            f"本地规则爆雷等级为「{calc_level}」，LLM结论须与此自洽。\n"
            "输出JSON：\n"
            '- "conclusion": 合规与爆雷可能性评估，120字内，须引用具体旗标/事件\n'
            '- "confidence": high|medium|low\n'
            '- "compliance_level": 高|中|无（须与本地规则一致）\n'
            '- "burst_risk": 爆雷路径简述（质押平仓/商誉减值/立案处罚；无则填"未见明确爆雷路径"）\n'
            '- "red_flags": 风险明细数组（与本地旗标呼应）\n'
            '- "key_points": 2-4条'
        )
