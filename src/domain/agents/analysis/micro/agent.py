"""A10 微观分析Agent（个股深度研究 + 本地估值计算）。"""

from __future__ import annotations

from typing import Any

from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload


def _find_value(payload: AnalysisPayload, keyword: str) -> float | None:
    """按冒号段精确/关键词子串匹配indicator，取**最新期**数值点。"""
    matched: list[tuple[str, float]] = []
    for p in payload.data_points:
        ind = str(p.get("indicator", ""))
        segment = ind.split(":", 1)[0]
        hit = segment == keyword or keyword in ind
        if hit and isinstance(p.get("value"), (int, float)):
            matched.append((str(p.get("period_date", "")), float(p["value"])))
    return max(matched, key=lambda x: x[0])[1] if matched else None


def _segment_series(payload: AnalysisPayload, segment: str) -> list[float]:
    """按冒号段精确匹配取全量数值序列（按时序排序），用于历史分位本地计算。"""
    vals = [
        float(p["value"])
        for p in payload.data_points
        if str(p.get("indicator", "")).split(":", 1)[0] == segment
        and isinstance(p.get("value"), (int, float))
    ]
    return sorted(vals)


_MIN_PERCENTILE_SAMPLE = 8


def _percentile_rank(series: list[float], current: float) -> float | None:
    """current在历史序列中的百分位（0-100，越小越便宜）。样本不足返回None。"""
    if len(series) < _MIN_PERCENTILE_SAMPLE:
        return None
    below = sum(1 for v in series if v <= current)
    return round(below / len(series) * 100, 1)


class MicroAnalysisAgent(AnalysisAgentBase):
    """个股深度研究与估值判断（PRD A10，P0）。

    PE/PB与行业均值的对比、以及PE/PB自身历史分位在本地计算（数字计算不走LLM，
    防幻觉），结论仅交由LLM解读定性。
    """

    # 个股日线/估值序列很长，prompt每指标只取最近60期；分位计算仍用全量
    context_max_periods = 60

    system_prompt = (
        "你是资深股票分析师。基于给定的个股数据点与本地计算的估值参考，"
        "进行个股深度研究。护城河评估必须覆盖品牌/技术/成本/网络效应/转换成本"
        "五个维度并逐项打分（0-10）。要求：\n"
        "1. 估值判断以本地计算参考为准，禁止另编数值；\n"
        "2. 输出仅为研究参考，不构成投资建议。"
    )

    def __init__(self, gateway, agent_id: str = "A10_micro",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

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
        pe_pct = _percentile_rank(_segment_series(payload, "PE(TTM)"), pe) if pe else None
        pb_pct = _percentile_rank(_segment_series(payload, "PB"), pb) if pb else None
        payload.hint["valuation_calc"] = self._calc_valuation(
            pe, pb, ind_pe, ind_pb, pe_pct, pb_pct,
        )

    @staticmethod
    def _calc_valuation(
        pe: float | None, pb: float | None,
        ind_pe: float | None, ind_pb: float | None,
        pe_pct: float | None = None, pb_pct: float | None = None,
    ) -> dict[str, Any]:
        """估值判断：优先行业均值对比；无行业均值时用PE/PB自身历史分位。

        - 行业均值：PE/PB均低于均值→低估，均高于→高估，否则合理；
        - 历史分位（0=历史最便宜，100=最贵）：双低<30%→历史低位（洼地信号），
          双高>70%→历史偏高，其余为历史区间内。
        """
        checks: list[bool | None] = []
        if pe is not None and ind_pe:
            checks.append(pe < ind_pe)
        if pb is not None and ind_pb:
            checks.append(pb < ind_pb)
        if checks:
            verdict = "低估" if all(checks) else ("高估" if not any(checks) else "合理")
            return {
                "valuation": verdict, "basis": "行业均值对比",
                "detail": (
                    f"PE {pe if pe is not None else '—'} vs 行业 {ind_pe or '—'}；"
                    f"PB {pb if pb is not None else '—'} vs 行业 {ind_pb or '—'}"
                ),
            }

        pcts = [p for p in (pe_pct, pb_pct) if p is not None]
        if pcts:
            if all(p < 30 for p in pcts):
                verdict = "历史低位"
            elif all(p > 70 for p in pcts):
                verdict = "历史偏高"
            else:
                verdict = "历史区间内"
            return {
                "valuation": verdict, "basis": "自身历史分位",
                "pe_percentile": pe_pct, "pb_percentile": pb_pct,
                "detail": (
                    f"当前PE历史分位{pe_pct if pe_pct is not None else '—'}%、"
                    f"PB历史分位{pb_pct if pb_pct is not None else '—'}%（0%为历史最便宜）"
                ),
            }
        return {"valuation": "数据不足", "basis": "无",
                "detail": "缺少PE/PB或行业均值数据点"}

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        data["valuation_calc"] = payload.hint.get("valuation_calc")
        return data

    def _requirements(self, payload: AnalysisPayload) -> str:
        answer_rule = (
            f"conclusion首句必须直接回答用户问题「{payload.user_query[:80]}」"
            "（问是否价值洼地/值得持有时，必须依据valuation_calc给出明确观点："
            "历史低位+基本面支撑→洼地信号；历史偏高→谨慎；数据不足才允许不明确）；"
            if payload.user_query else
            "个股综合研判（150字内）；"
        )
        return (
            "请输出JSON对象，字段：\n"
            f'- "conclusion": {answer_rule}估值结论必须与valuation_calc一致（200字内）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "moat_scores": {"brand": 0-10, "technology": 0-10, "cost": 0-10, '
            '"network_effect": 0-10, "switching_cost": 0-10}\n'
            '- "key_points": 3-5条要点\n'
            '- "risks": 主要个股风险1-3条'
        )
