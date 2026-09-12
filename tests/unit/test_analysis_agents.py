"""分析层四Agent测试（FakeGateway注入，不联网）。"""

import json

import pytest

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput
from src.domain.agents.analysis import (
    MacroAnalysisAgent,
    MesoAnalysisAgent,
    MicroAnalysisAgent,
    RiskAnalysisAgent,
)


class FakeGateway:
    """回放预置JSON的假网关，记录调用参数。"""

    def __init__(self, reply: dict) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        self.calls.append(
            {"task_tier": task_tier, "system": system, "prompt": prompt, **kwargs}
        )
        from src.infrastructure.llm.models import LLMResponse

        return LLMResponse(
            content=json.dumps(self._reply, ensure_ascii=False),
            model_used="fake-model", provider="fake",
            tokens_in=50, tokens_out=30, prompt_hash="ph", response_hash="rh",
        )


def _dp(indicator: str, value: float | None, period: str = "2026-08") -> dict:
    return {"indicator": indicator, "value": value, "period_date": period,
            "source_name": "AkShare", "data_id": f"d_{indicator}"}


def _make_input(payload: dict, task_id: str = "t8") -> AgentInput:
    return AgentInput(task_id=task_id, tenant_id="tenant_001", payload=payload)


MACRO_REPLY = {
    "conclusion": "CPI同比2.1%温和，流动性中性",
    "confidence": "medium", "cycle_position": "复苏", "liquidity": "中性",
    "key_points": ["CPI温和"], "risks": ["外需回落"],
}


async def test_macro_agent_happy_path():
    gw = FakeGateway(MACRO_REPLY)
    out = await MacroAnalysisAgent(gw).execute(_make_input({
        "focus": "中国宏观", "data_points": [_dp("CPI", 2.1), _dp("PPI", -0.8)],
    }))

    assert out.agent_id == "A08_macro"
    assert out.confidence.value == "medium"
    assert out.result["cycle_position"] == "复苏"
    assert out.data_refs == ["d_CPI", "d_PPI"]
    call = gw.calls[0]
    assert call["task_tier"] == "reasoning" and call["json_mode"] is True
    assert "CPI" in call["prompt"] and "不构成投资建议" in call["prompt"]


async def test_empty_data_points_skips_llm():
    gw = FakeGateway(MACRO_REPLY)
    out = await MacroAnalysisAgent(gw).execute(_make_input({"data_points": []}))
    assert out.confidence.value == "low"
    assert "未获取到" in out.conclusion
    assert gw.calls == []  # 未产生LLM调用


async def test_invalid_payload_raises():
    gw = FakeGateway(MACRO_REPLY)
    with pytest.raises(AgentExecutionError, match="不合法"):
        await MacroAnalysisAgent(gw).execute(_make_input({"data_points": "not-a-list"}))


async def test_llm_non_json_raises():
    class BadGateway:
        async def complete(self, *a, **k):
            from src.infrastructure.llm.models import LLMResponse

            return LLMResponse(content="这不是JSON", model_used="m", provider="p")

    with pytest.raises(AgentExecutionError, match="非合法JSON"):
        await MacroAnalysisAgent(BadGateway()).execute(_make_input(
            {"data_points": [_dp("CPI", 2.1)]}
        ))


async def test_fenced_json_is_parsed():
    class FencedGateway:
        async def complete(self, *a, **k):
            from src.infrastructure.llm.models import LLMResponse

            body = json.dumps(MACRO_REPLY, ensure_ascii=False)
            return LLMResponse(content=f"```json\n{body}\n```", model_used="m", provider="p")

    out = await MacroAnalysisAgent(FencedGateway()).execute(_make_input(
        {"data_points": [_dp("CPI", 2.1)]}
    ))
    assert out.result["cycle_position"] == "复苏"


async def test_micro_valuation_calc_local():
    gw = FakeGateway({**MACRO_REPLY, "moat_scores": {"brand": 9, "technology": 8,
                                                     "cost": 7, "network_effect": 8,
                                                     "switching_cost": 8}})
    out = await MicroAnalysisAgent(gw).execute(_make_input({
        "focus": "600519",
        "data_points": [_dp("PE", 18.0), _dp("PB", 5.0),
                        _dp("industry_pe", 25.0), _dp("industry_pb", 6.0)],
    }))

    calc = out.result["valuation_calc"]
    assert calc["valuation"] == "低估"  # PE/PB均低于行业，本地判定
    assert "18.0" in calc["detail"]
    # 本地估值参考已注入prompt
    assert "低估" in gw.calls[0]["prompt"]
    assert out.result["moat_scores"]["brand"] == 9


async def test_micro_valuation_insufficient_data():
    gw = FakeGateway(MACRO_REPLY)
    out = await MicroAnalysisAgent(gw).execute(_make_input({
        "data_points": [_dp("PE", 18.0)]  # 无行业均值
    }))
    assert out.result["valuation_calc"]["valuation"] == "数据不足"


async def test_risk_agent_flags_and_level():
    reply = {**MACRO_REPLY, "risk_level": "高",
             "red_flags": ["资产负债率75%超70%警戒线"]}
    gw = FakeGateway(reply)
    out = await RiskAnalysisAgent(gw).execute(_make_input({
        "data_points": [_dp("资产负债率", 75.0), _dp("流动比率", 0.8)],
    }))

    assert out.result["risk_level"] == "高"
    assert out.agent_id == "A11_fin_risk"
    prompt = gw.calls[0]["prompt"]
    assert "资产负债率75%超70%警戒线" in prompt   # 本地旗标注入
    assert "流动比率0.8低于1" in prompt


async def test_risk_agent_normal_finance():
    gw = FakeGateway({**MACRO_REPLY, "risk_level": "低", "red_flags": []})
    out = await RiskAnalysisAgent(gw).execute(_make_input({
        "data_points": [_dp("资产负债率", 40.0)]
    }))
    assert "未见明显异常" in out.result["red_flag_calc"][0]


def test_capabilities_surface():
    gw = FakeGateway(MACRO_REPLY)
    assert "cycle_positioning" in MacroAnalysisAgent(gw).get_capabilities()["capabilities"]
    assert "chain_analysis" in MesoAnalysisAgent(gw).get_capabilities()["capabilities"]
    assert "moat_assessment" in MicroAnalysisAgent(gw).get_capabilities()["capabilities"]
    assert "financial_mining" in RiskAnalysisAgent(gw).get_capabilities()["capabilities"]
