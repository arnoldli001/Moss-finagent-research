"""A05 信息去伪Agent：信息真实性验证 + 来源可信度评分（PRD A05，P1）。

防幻觉策略：可信度分数完全由本地规则计算（来源分级×时效衰减±文本特征），
LLM只做定性复核（verdict/red_flags），不修改分数；
verdict为"不可信"时直接拒绝该条目。
"""

from __future__ import annotations

from typing import Any

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import parse_llm_json
from src.domain.agents.info.models import InfoItem
from src.domain.agents.info.verifier.logic import score_item
from src.infrastructure.llm import LLMGateway, TaskTier

_VERIFIED_THRESHOLD = 0.5
"""规则分低于此值直接拒绝（无论LLM verdict如何）"""


class VerifierAgent:
    """A05_verifier。"""

    task_tier: TaskTier = "medium"

    def __init__(self, gateway: LLMGateway, agent_id: str = "A05_verifier") -> None:
        self.agent_id = agent_id
        self._gateway = gateway

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["credibility_scoring", "source_verification"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _parse_items(self, payload: dict[str, Any]) -> list[InfoItem]:
        try:
            items = []
            for index, raw in enumerate(payload.get("info_items", [])):
                item = InfoItem.model_validate(raw)
                if not item.item_id:
                    item.item_id = f"info_{index + 1}"
                items.append(item)
            return items
        except Exception as exc:
            from src.core.exceptions import AgentExecutionError

            raise AgentExecutionError(f"{self.agent_id}输入info_items不合法: {exc}") from exc

    async def execute(self, input: AgentInput) -> AgentOutput:
        items = self._parse_items(input.payload)
        if not items:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion="无待核验信息，跳过去伪",
                confidence=Confidence.LOW, trace_id=input.task_id,
                reasoning_steps=[TraceStep(step=1, step_type="data_retrieval",
                                           description="输入info_items为空，跳过LLM调用")],
                result={"items": [], "stats": {"total": 0, "verified": 0, "rejected": 0,
                                               "avg_score": 0.0}},
            )

        # 1) 本地规则评分（权威分值来源）
        scored: list[dict[str, Any]] = []
        for item in items:
            entry = item.model_dump()
            entry |= score_item(entry)
            scored.append(entry)

        # 2) LLM定性复核（不改分数，只给verdict与红旗）
        lines = [
            f"- [{s['item_id']}] 来源 {s.get('source_name') or '未知'} "
            f"(规则分 {s['rule_score']}) | {s.get('title', '')} | "
            f"{s.get('text', '')[:300]}"
            for s in scored
        ]
        prompt = (
            f"## 待核验信息（含本地规则分）\n" + "\n".join(lines) +
            "\n\n## 任务要求\n"
            "请逐条复核信息可信度（规则分仅作参考）。输出JSON对象：\n"
            '- "reviews": [{"item_id": "...", "verdict": "可信"|"存疑"|"不可信", '
            '"red_flags": ["红旗特征，无则空数组"], "reasoning": "30字内核验理由"}]\n'
            "核验要点：信源能否交叉印证、表述是否夸大、是否含无法证实的绝对化断言。"
            "本核验仅供研究参考，不构成投资建议。"
        )
        response = await self._gateway.complete(
            self.task_tier,
            "你是信息核查员，负责甄别财经信息的真实性与可靠性。只依据给定文本判断，"
            "禁止编造未提及的事实。",
            prompt, agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
        )
        data = parse_llm_json(self.agent_id, response.content)
        reviews = {
            str(r.get("item_id")): r
            for r in data.get("reviews", []) if isinstance(r, dict)
        }

        # 3) 合并：规则分定分数，LLM verdict定生死
        verified_count = 0
        total_score = 0.0
        for s in scored:
            review = reviews.get(s["item_id"], {})
            verdict = str(review.get("verdict", "存疑"))
            rejected = s["rule_score"] < _VERIFIED_THRESHOLD or verdict == "不可信"
            s["verdict"] = "拒绝" if rejected else verdict
            s["verified"] = not rejected
            s["red_flags"] = [str(f) for f in review.get("red_flags", [])][:5]
            s["review_reason"] = str(review.get("reasoning", ""))[:120]
            verified_count += int(s["verified"])
            total_score += s["rule_score"]

        count = len(scored)
        avg = round(total_score / count, 3)
        ratio = verified_count / count
        confidence = (Confidence.HIGH if ratio >= 0.8
                      else Confidence.MEDIUM if ratio >= 0.5 else Confidence.LOW)
        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=f"核验 {count} 条信息：可信 {verified_count} 条，"
                       f"拒绝 {count - verified_count} 条（规则均分 {avg}）",
            confidence=confidence, data_refs=[i.item_id for i in items],
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="indicator_calculation",
                          description=f"本地规则评分 {count} 条（来源分级×时效衰减±文本特征）"),
                TraceStep(step=2, step_type="llm_inference",
                          description=f"model={response.model_used} cache={response.cache_kind} "
                                      f"fallback={response.fallback_used}"),
            ],
            result={
                "items": scored,
                "stats": {"total": count, "verified": verified_count,
                          "rejected": count - verified_count, "avg_score": avg},
                "model_used": response.model_used,
            },
        )
