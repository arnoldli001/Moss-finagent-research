"""A19编码实现Agent单测（mock LLM，不联网）。"""

from __future__ import annotations

import pytest

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput
from src.domain.agents.engineering.code_engineer.agent import CodeEngineerAgent

# 模拟LLM返回的_fetch_raw方法体（返回静态数据，不发起真实HTTP请求）
MOCK_FETCH_RAW_BODY = '''return [{"date": "2026-09-14", "value": 75.5,
         "unit": "美元/桶", "extra": {"benchmark": "WTI"}}]'''


class FakeGateway:
    """模拟LLM网关，直接返回预设_fetch_raw方法体。"""

    def __init__(self, response_content: str = MOCK_FETCH_RAW_BODY):
        self._content = response_content
        self.calls = []

    async def complete(self, *args, **kwargs):
        self.calls.append(kwargs)
        from src.infrastructure.llm.models import LLMResponse

        return LLMResponse(
            content=self._content, model_used="fake", provider="fake",
            tokens_in=10, tokens_out=len(self._content),
            cache_kind="none", cache_hit=False, fallback_used=False,
        )


@pytest.mark.asyncio
async def test_code_engineer_generates_and_registers(tmp_path, monkeypatch):
    # 用临时目录作为动态连接器目录
    monkeypatch.setattr(
        "src.domain.agents.engineering.code_engineer.agent.DYNAMIC_DIR", tmp_path)
    monkeypatch.setattr(
        "src.infrastructure.connectors.dynamic_loader.DYNAMIC_DIR", tmp_path)

    gateway = FakeGateway()
    agent = CodeEngineerAgent(gateway)
    output = await agent.execute(AgentInput(
        task_id="t1", tenant_id="tenant_001",
        payload={
            "gap_description": "未接入国际原油价格数据",
            "indicator": "comm:oil_price",
        },
    ))
    assert output.confidence.value == "high"
    assert output.result["connector_class"] == "CommOilPriceConnector"
    assert output.result["sample_count"] == 1
    assert output.result["sample_points"][0]["value"] == 75.5
    assert output.result["indicator_prefix"] == "comm:"
    # 文件已写入
    assert any(tmp_path.glob("*.py"))


@pytest.mark.asyncio
async def test_code_engineer_rejects_unsafe_code(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.domain.agents.engineering.code_engineer.agent.DYNAMIC_DIR", tmp_path)
    unsafe_body = MOCK_FETCH_RAW_BODY + "\nexec('print(1)')\n"
    gateway = FakeGateway(response_content=unsafe_body)
    agent = CodeEngineerAgent(gateway)
    with pytest.raises(AgentExecutionError, match="3轮修复未通过|exec"):
        await agent.execute(AgentInput(
            task_id="t2", tenant_id="tenant_001",
            payload={"gap_description": "x", "indicator": "comm:y"},
        ))


@pytest.mark.asyncio
async def test_code_engineer_requires_gap_description(tmp_path):
    agent = CodeEngineerAgent(FakeGateway())
    with pytest.raises(AgentExecutionError, match="gap_description"):
        await agent.execute(AgentInput(
            task_id="t3", tenant_id="tenant_001", payload={},
        ))


def test_recommend_source_keywords():
    agent = CodeEngineerAgent(FakeGateway())
    src = agent._recommend_source("未接入两市成交额", "mkt:turnover:total")
    assert "腾讯" in src
    src = agent._recommend_source("美联储利率概率", "fed:rate_prob:next")
    assert "fed" in src.lower() or "cme" in src.lower()


def test_recommend_schedule():
    assert "5min" in CodeEngineerAgent._recommend_schedule("mkt:turnover:total")
    assert "daily" in CodeEngineerAgent._recommend_schedule("idx_val:snapshot:all")
