"""A17投研建议 + A18审计Agent + 哈希链存储测试。"""

import json

import pytest

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput
from src.domain.agents.audit.verifier.agent import AuditAgent
from src.domain.agents.decision.recommend.agent import RecommendationAgent
from src.infrastructure.repositories.audit_chain import AuditChainWriter, ChainVerifier


class FakeGateway:
    def __init__(self, reply: dict) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        from src.infrastructure.llm.models import LLMResponse

        self.calls.append({"task_tier": task_tier, "prompt": prompt, **kwargs})
        return LLMResponse(
            content=json.dumps(self._reply, ensure_ascii=False),
            model_used="fake-pro", provider="fake",
            tokens_in=200, tokens_out=80, prompt_hash="ph", response_hash="rh",
        )


def _ainput(payload: dict, task_id: str = "t17") -> AgentInput:
    return AgentInput(task_id=task_id, tenant_id="tenant_001", payload=payload)


ANALYSES = [
    {"agent_id": "A08_macro", "conclusion": "宏观复苏", "confidence": "medium",
     "data_refs": ["d_CPI", "d_PMI"], "result": {"cycle_position": "复苏"}},
    {"agent_id": "A10_micro", "conclusion": "估值低估", "confidence": "high",
     "data_refs": ["d_PE"], "result": {"valuation_calc": {"valuation": "低估"}}},
    {"agent_id": "A11_fin_risk", "conclusion": "财务无雷", "confidence": "high",
     "data_refs": ["d_debt"], "result": {"red_flag_calc": []}},
]

REPLY = {
    "conclusion": "综合看多：复苏+低估+无雷",
    "confidence": "high", "stance": "看多",
    "key_logic": ["[宏观] 复苏", "[微观] 低估"],
    "catalysts": ["政策发力"], "risks": ["外需波动"],
    "monitoring_points": ["PMI", "PE分位"],
    "conflicts_resolved": [],
}


async def test_recommend_happy_path():
    gw = FakeGateway(REPLY)
    out = await RecommendationAgent(gw).execute(_ainput({
        "analyses": ANALYSES, "focus": "贵州茅台",
    }))

    assert out.agent_id == "A17_recommend"
    assert out.result["stance"] == "看多"
    assert "不构成投资建议" in out.result["disclaimer"]
    assert gw.calls[0]["task_tier"] == "decision"  # 决策层走decision路由
    assert "A08_macro" in gw.calls[0]["prompt"]     # 上游结论在上下文中


async def test_recommend_conflict_arbitration_prompted():
    conflicting = ANALYSES + [
        {"agent_id": "A09_meso", "conclusion": "行业进入衰退期", "confidence": "medium",
         "result": {"industry_cycle": "衰退期"}},
    ]
    gw = FakeGateway(REPLY)
    await RecommendationAgent(gw).execute(_ainput({"analyses": conflicting}))
    assert "冲突" in gw.calls[0]["prompt"] and "仲裁" in gw.calls[0]["prompt"]


async def test_recommend_empty_analyses_skips_llm():
    gw = FakeGateway(REPLY)
    out = await RecommendationAgent(gw).execute(_ainput({"analyses": []}))
    assert out.confidence.value == "low"
    assert gw.calls == []


async def test_recommend_invalid_payload():
    with pytest.raises(AgentExecutionError, match="不合法"):
        await RecommendationAgent(FakeGateway(REPLY)).execute(_ainput({"analyses": 42}))


# ---------- A18 + 哈希链 ----------

def _chain(tmp_dir):
    path = f"{tmp_dir}/chain.jsonl"
    writer = AuditChainWriter(path)
    for i in range(3):
        writer.append({"kind": "llm_call", "n": i})
    return path, writer


async def test_chain_roundtrip_and_verify(tmp_dir):
    path, writer = _chain(tmp_dir)
    verdict = ChainVerifier(path).verify()
    assert verdict["valid"] and verdict["count"] == 3
    assert verdict["head"] == writer.head


async def test_chain_detects_tampering(tmp_dir):
    path, _ = _chain(tmp_dir)
    # 篡改中间记录的entry
    lines = open(path, encoding="utf-8").read().splitlines()
    rec = json.loads(lines[1])
    rec["entry"]["n"] = 999
    lines[1] = json.dumps(rec, ensure_ascii=False)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    verdict = ChainVerifier(path).verify()
    assert not verdict["valid"] and verdict["broken_at"] == 2


async def test_chain_detects_deletion(tmp_dir):
    path, _ = _chain(tmp_dir)
    lines = open(path, encoding="utf-8").read().splitlines()
    del lines[0]  # 删首条 → 后续prev_hash断裂
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    assert not ChainVerifier(path).verify()["valid"]


async def test_chain_writer_resume(tmp_dir):
    path, first = _chain(tmp_dir)
    resumed = AuditChainWriter(path)  # 新实例从文件续链
    rec = resumed.append({"kind": "seal"})
    assert rec["seq"] == 4 and rec["prev_hash"] == first.head
    assert ChainVerifier(path).verify()["count"] == 4


async def test_audit_agent_pass_and_seal(tmp_dir):
    chain_path, writer = _chain(tmp_dir)
    agent = AuditAgent()
    out = await agent.execute(_ainput({
        "trace_id": "tr_x", "agent_outputs": ANALYSES,
        "chain_path": chain_path, "llm_audit_path": f"{tmp_dir}/none.jsonl",
    }, task_id="t18"))

    assert out.result["verdict"] == "通过"
    assert out.result["chain_valid"] is True
    assert out.result["sealed_seq"] == 4
    assert out.result["chain_head"] == out.result["sealed_hash"]
    assert out.confidence.value == "high"


async def test_audit_agent_flags_incomplete_outputs(tmp_dir):
    chain_path, _ = _chain(tmp_dir)
    bad = [{"agent_id": "A08_macro", "conclusion": "x"}]  # 缺confidence与data_refs
    out = await AuditAgent().execute(_ainput({
        "trace_id": "tr_y", "agent_outputs": bad, "chain_path": chain_path,
    }))

    assert out.result["verdict"] == "不通过"
    assert len(out.result["completeness_issues"]) == 2
    assert out.confidence.value == "medium"


async def test_audit_agent_detects_broken_chain(tmp_dir):
    chain_path, _ = _chain(tmp_dir)
    lines = open(chain_path, encoding="utf-8").read().splitlines()
    rec = json.loads(lines[0])
    rec["entry"]["n"] = -1
    lines[0] = json.dumps(rec, ensure_ascii=False)
    open(chain_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    out = await AuditAgent().execute(_ainput({
        "agent_outputs": ANALYSES, "chain_path": chain_path, "seal_report": False,
    }))
    assert out.result["verdict"] == "不通过"
    assert out.result["chain_valid"] is False
    assert out.result["sealed_seq"] is None
