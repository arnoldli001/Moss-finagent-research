"""A18 审计Agent（哈希链校验 + 完整性检查）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.infrastructure.repositories.audit_chain import AuditChainWriter, ChainVerifier


class AuditPayload(BaseModel):
    """A18输入：本次投研任务的产出摘要。"""

    trace_id: str = ""
    agent_outputs: list[dict[str, Any]] = Field(default_factory=list)
    llm_audit_path: str = "data/audit/llm_audit.jsonl"
    chain_path: str = "data/audit/audit_chain.jsonl"
    seal_report: bool = True
    """是否把本次审计结论追加进哈希链（封存）"""


class AuditAgent(BaseAgent):
    """审计校验（PRD A18，P0）：链完整性 + 产出完整性，结论封存上链。"""

    def __init__(self, agent_id: str = "A18_audit") -> None:
        super().__init__(agent_id)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["hash_chain_verify", "completeness_check", "seal_on_chain"],
        }

    def health_check(self) -> bool:
        return True

    def _parse_payload(self, payload: dict[str, Any]) -> AuditPayload:
        try:
            return AuditPayload.model_validate(payload)
        except ValidationError as exc:
            raise AgentExecutionError(f"{self.agent_id}输入不合法: {exc}") from exc

    def _check_completeness(self, outputs: list[dict[str, Any]]) -> list[str]:
        issues: list[str] = []
        for a in outputs:
            aid = a.get("agent_id", "?")
            if not a.get("conclusion"):
                issues.append(f"{aid}: 缺少conclusion")
            if not a.get("confidence"):
                issues.append(f"{aid}: 缺少confidence")
            if not a.get("data_refs"):
                issues.append(f"{aid}: 无数据溯源引用")
        return issues

    def _count_llm_calls(self, path: str, trace_id: str) -> int:
        from pathlib import Path

        p = Path(path)
        if not trace_id or not p.exists():
            return 0
        count = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() and f'"trace_id": "{trace_id}"' in line:
                count += 1
        return count

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = self._parse_payload(input.payload)

        chain = ChainVerifier(payload.chain_path)
        chain_result = chain.verify()
        issues = self._check_completeness(payload.agent_outputs)
        llm_calls = self._count_llm_calls(payload.llm_audit_path, payload.trace_id)

        chain_note = (
            "完整" if chain_result["valid"]
            else f"于seq={chain_result['broken_at']}断链"
        )
        verdict = "通过" if chain_result["valid"] and not issues else "不通过"
        conclusion = (
            f"审计{verdict}：哈希链{chain_result['count']}条记录"
            f"{chain_note}，"
            f"产出完整性问题{len(issues)}项，LLM调用{llm_calls}次。"
        )

        sealed = None
        if payload.seal_report:
            sealed = AuditChainWriter(payload.chain_path).append({
                "kind": "research_audit",
                "trace_id": payload.trace_id,
                "task_id": input.task_id,
                "verdict": verdict,
                "chain_valid": chain_result["valid"],
                "completeness_issues": issues,
                "llm_calls": llm_calls,
                "agent_ids": [a.get("agent_id", "?") for a in payload.agent_outputs],
            })

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=conclusion,
            confidence=Confidence.HIGH if verdict == "通过" else Confidence.MEDIUM,
            data_refs=[a.get("agent_id", "?") for a in payload.agent_outputs],
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="cross_validation",
                          description=(
                              f"哈希链校验 {chain_result['count']} 条, "
                              f"断链位={chain_result['broken_at']}"
                          )),
                TraceStep(step=2, step_type="final_conclusion",
                          description=f"封存seq={sealed['seq']} head={sealed['record_hash'][:16]}"
                                      if sealed else "未封存（seal_report=False）"),
            ],
            result={
                "verdict": verdict,
                "chain_valid": chain_result["valid"],
                "chain_count": chain_result["count"],
                "chain_head": sealed["record_hash"] if sealed else chain_result["head"],
                "completeness_issues": issues,
                "llm_calls": llm_calls,
                "sealed_seq": sealed["seq"] if sealed else None,
                "sealed_hash": sealed["record_hash"] if sealed else None,
            },
        )
