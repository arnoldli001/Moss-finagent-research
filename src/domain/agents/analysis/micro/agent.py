"""A10 微观分析Agent（个股深度研究 + 本地估值计算）。"""

from __future__ import annotations

from typing import Any

from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload


def _find_value(payload: AnalysisPayload, indicator: str) -> float | None:
    for p in payload.data_points:
        if p.get("indicator") == indicator and isinstance(p.get("value"), (int, float)):
            return float(p["value"])
    return None


class MicroAnalysisAgent(AnalysisAgentBase):
    """个股深度研究与估值判断（PRD A10，P0）。

    PE/PB与行业均值的对比在本地计算（数字计算不走LLM，防幻觉），
    结论仅交由LLM解读定性。
    """

    system_prompt = (
        "你是资深股票分析师。基于给定的个股数据点与本地计算的估值参考，"
        "进行个股深度研究。护城河评估必须覆盖品牌/技术/成本/网络效应/转换成本"
        "五个维度并逐项打分（0-10）。要求：\n"
        "1. 估值判断以本地计算参考为准，禁止另编数值；\n"
        "2. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A10_micro") -> None:
        super().__init__(agent_id, gateway)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["stock_research", "valuation_check", "moat_assessment"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _prepare(self, payload: AnalysisPayload) -> None:
        pe = _find_value(payload, "PE")
        pb = _find_value(payload, "PB")
        ind_pe = _find_value(payload, "industry_pe")
        ind_pb = _find_value(payload, "industry_pb")
        payload.hint.setdefault("pe", pe)
        payload.hint.setdefault("pb", pb)
        payload.hint.setdefault("industry_pe", ind_pe)
        payload.hint.setdefault("industry_pb", ind_pb)
        payload.hint["valuation_calc"] = self._calc_valuation(pe, pb, ind_pe, ind_pb)

    @staticmethod
    def _calc_valuation(
        pe: float | None, pb: float | None,
        ind_pe: float | None, ind_pb: float | None,
    ) -> dict[str, Any]:
        """与行业均值对比：PE/PB均低于均值→低估，均高于→高估，否则合理/数据不足。"""
        checks: list[bool | None] = []
        if pe is not None and ind_pe:
            checks.append(pe < ind_pe)
        if pb is not None and ind_pb:
            checks.append(pb < ind_pb)
        if not checks:
            return {"valuation": "数据不足", "detail": "缺少PE/PB或行业均值数据点"}
        if all(checks):
            verdict = "低估"
        elif not any(checks):
            verdict = "高估"
        else:
            verdict = "合理"
        detail = (
            f"PE {pe if pe is not None else '—'} vs 行业 {ind_pe if ind_pe else '—'}；"
            f"PB {pb if pb is not None else '—'} vs 行业 {ind_pb if ind_pb else '—'}"
        )
        return {"valuation": verdict, "detail": detail}

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        data["valuation_calc"] = payload.hint.get("valuation_calc")
        return data

    def _requirements(self, payload: AnalysisPayload) -> str:
        return (
            "请输出JSON对象，字段：\n"
            '- "conclusion": 个股综合研判（150字内，估值结论必须与valuation_calc一致）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "moat_scores": {"brand": 0-10, "technology": 0-10, "cost": 0-10, '
            '"network_effect": 0-10, "switching_cost": 0-10}\n'
            '- "key_points": 3-5条要点\n'
            '- "risks": 主要个股风险1-3条'
        )
