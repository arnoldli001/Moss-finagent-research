"""A11 财务风险Agent（财务排雷）。"""

from __future__ import annotations

from typing import Any

from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload


def _find_value(payload: AnalysisPayload, keyword: str) -> float | None:
    for p in payload.data_points:
        if keyword in str(p.get("indicator", "")) and isinstance(p.get("value"), (int, float)):
            return float(p["value"])
    return None


class RiskAnalysisAgent(AnalysisAgentBase):
    """财务排雷与造假预警（PRD A11，P1）。

    经典排雷比率（资产负债率/流动比率/毛利率异动）在本地计算并给出
    旗标，LLM只负责综合解读与定性。
    """

    system_prompt = (
        "严谨风控专员，负责财务排雷。依据财务数据点与本地风险旗标识别造假征兆与"
        "偿债风险；未提及的风险不得断言。"
    )

    def __init__(self, gateway, agent_id: str = "A11_fin_risk",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["financial_mining", "fraud_warning"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _prepare(self, payload: AnalysisPayload) -> None:
        debt_ratio = _find_value(payload, "资产负债率")
        current_ratio = _find_value(payload, "流动比率")
        flags: list[str] = []
        if debt_ratio is not None and debt_ratio > 70:
            flags.append(f"资产负债率{debt_ratio:g}%超70%警戒线")
        if current_ratio is not None and current_ratio < 1.0:
            flags.append(f"流动比率{current_ratio:g}低于1，短期偿债压力大")
        payload.hint["red_flag_calc"] = flags or ["常规财务比率未见明显异常"]

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        data["red_flag_calc"] = payload.hint.get("red_flag_calc")
        return data

    def _requirements(self, payload: AnalysisPayload) -> str:
        return (
            "输出JSON：\n"
            '- "conclusion": 风险评估，120字内\n'
            '- "confidence": high|medium|low\n'
            '- "risk_level": 低|中|高\n'
            '- "red_flags": 与red_flag_calc呼应的风险明细数组\n'
            '- "key_points": 2-4条'
        )
