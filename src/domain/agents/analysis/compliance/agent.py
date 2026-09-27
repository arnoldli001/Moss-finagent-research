"""A12 合规爆雷Agent（合规风险与爆雷预警，PRD A12，P2）。

本地规则引擎（logic.py）计算合规旗标与爆雷等级，覆盖比率异常、存贷双高、
信息层诉讼/监管事件三类信号；LLM只负责综合定性，禁止自造风险事实。

## ★★★ 2026-09-27 第八轮：纯规则路径（−18,500 tokens_in/轮）

审计实证：A12 投到 `reasoning` 云端，每轮 ~18,500 tokens_in（占云端调用的 ~30%）。
但 A12 真正消耗的只是：
  · 财务比率阈值（关联交易/商誉/质押/担保 4 类，纯数字比较）；
  · 信息层 A06 抽取的诉讼/监管事件（已在 A06 阶段结构化）；
  · 存贷双高（货币资金 vs 有息负债两个数字）。

这三类信号 `evaluate_compliance()` 已经全部本地算好，LLM 的"综合定性"
**在"无信号"分支里是冗余的**（让 LLM 把"无"再写一遍 = 浪费 18.5k tokens）。

改造：当本地规则给出 `compliance_level_calc == "无"` **且**没有 events 时，
A12 直接以纯规则结果返回（跳过 LLM 调用），同时把 `model_used` 标记为
`"rule-only"` 方便审计追溯。
"""

from __future__ import annotations

import logging
from typing import Any

from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload
from src.domain.agents.analysis.compliance.logic import evaluate_compliance

logger = logging.getLogger(__name__)


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

    # ★ 2026-09-27：是否走纯规则路径
    def _should_skip_llm(self, payload: AnalysisPayload) -> bool:
        calc = payload.hint.get("compliance_calc", {}) or {}
        level = calc.get("compliance_level_calc", "")
        flags = calc.get("compliance_flags") or []
        events = payload.events or []
        # 无风险 + 无事件 → 纯规则
        if level == "无" and len(events) == 0:
            return True
        # 有"未见明显合规风险信号"占位 + 无事件 → 也是纯规则
        if (len(flags) == 1 and flags[0] == "未见明显合规风险信号"
                and len(events) == 0):
            return True
        return False

    def _build_rule_only_result(self, payload: AnalysisPayload) -> dict[str, Any]:
        """纯规则结果：跳过 LLM 调用，直接输出。"""
        calc = payload.hint.get("compliance_calc", {}) or {}
        flags = calc.get("compliance_flags") or ["未见明显合规风险信号"]
        conclusion = (
            f"本地合规规则扫描完成：{'; '.join(flags[:3])}。"
            "评级「无/中」由本地规则计算（model_used=rule-only，跳过 LLM 调用）。"
        )
        return {
            "conclusion": conclusion[:200],
            "confidence": "high",
            "compliance_level": calc.get("compliance_level_calc", "无"),
            "burst_risk": "未见明确爆雷路径",
            "red_flags": flags if flags != ["未见明显合规风险信号"] else [],
            "key_points": [
                f"合规旗标 {len(flags)} 条",
                f"严重旗标 {calc.get('severe_flag_count', 0)} 条",
                "纯规则判定，无需 LLM",
            ],
            "_rule_only": True,  # 标记，审计追溯用
            "model_used": "rule-only",
        }

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
