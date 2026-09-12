"""模拟产业数据连接器 + 连接器路由测试（全离线、无外部依赖）。"""

import json
from datetime import date

import pytest

from src.core.exceptions import DataFetchError
from src.core.models import AgentInput
from src.domain.agents.data.collector.agent import DataCollectorAgent
from src.domain.agents.industry.tech.agent import TechIndustryAgent
from src.infrastructure.connectors.mock_industry_connector import (
    MockIndustryConnector,
    _month_iter,
    synthesize_series,
)
from src.infrastructure.connectors.router import ConnectorRouter

TECH_INDICATORS = ("ind:半导体销售额同比", "ind:芯片出货量同比", "ind:科技行业PE(TTM)")


async def test_mock_connector_returns_24_marked_monthly_points():
    points = await MockIndustryConnector().fetch("ind:半导体销售额同比")
    assert len(points) == 24
    periods = [p.period_date for p in points]
    assert periods == sorted(periods)  # 升序
    assert len(set(periods)) == 24
    for p in points:
        assert p.source_name == "模拟产业数据(Demo)"
        assert p.source_url == "mock://industry-demo"
        assert p.extra["simulated"] is True
        assert p.confidence == 0.5
        assert isinstance(p.value, float)


async def test_mock_connector_deterministic_across_calls():
    a = await MockIndustryConnector().fetch("ind:白酒批价(元/瓶)")
    b = await MockIndustryConnector().fetch("ind:白酒批价(元/瓶)")
    assert [p.value for p in a] == [p.value for p in b]
    # 直接函数：同月份序列两次一致（crc32播种，不依赖进程hash盐）
    months = _month_iter(date(2026, 8, 1), 24)
    assert synthesize_series("白酒批价(元/瓶)", months) == \
        synthesize_series("白酒批价(元/瓶)", months)


async def test_mock_connector_unknown_indicator_raises():
    with pytest.raises(DataFetchError):
        await MockIndustryConnector().fetch("ind:不存在的指标")


def test_mock_connector_capabilities_declare_simulated():
    caps = MockIndustryConnector().get_capabilities()
    assert caps["simulated"] is True
    assert set(TECH_INDICATORS) <= set(caps["indicators"])


async def test_router_dispatches_by_predicate():
    class _Fake:
        def __init__(self, name):
            self.name = name
            self.fetched = []

        async def fetch(self, indicator, start_date=None, end_date=None):
            self.fetched.append(indicator)
            return []

        def get_capabilities(self):
            return {"name": self.name, "indicators": [self.name]}

    mock_side, ak_side = _Fake("mock"), _Fake("ak")
    router = ConnectorRouter([
        (mock_side, MockIndustryConnector.supports),
        (ak_side, lambda i: i in ("CPI", "PPI")),
    ])
    await router.fetch("ind:科技行业PE(TTM)")
    await router.fetch("CPI")
    assert mock_side.fetched == ["ind:科技行业PE(TTM)"]
    assert ak_side.fetched == ["CPI"]
    with pytest.raises(DataFetchError):
        await router.fetch("UNKNOWN_X")


class _FakeGateway:
    def __init__(self):
        self.prompts = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        self.prompts.append(prompt)
        from src.infrastructure.llm.models import LLMResponse

        reply = {"conclusion": "半导体行业景气向上", "confidence": "medium",
                 "outlook": "向好", "cycle_position": "成长期",
                 "drivers": ["国产替代"], "risks": ["出口管制"]}
        return LLMResponse(
            content=json.dumps(reply, ensure_ascii=False),
            model_used="fake", provider="fake", prompt_hash="ph", response_hash="rh",
        )


async def test_mock_feed_powers_tech_agent_end_to_end():
    """模拟产业数据经A01采集后真实驱动A13（不再因零关注指标skip）。"""
    collector = DataCollectorAgent(MockIndustryConnector())
    all_points = []
    for ind in TECH_INDICATORS:
        out = await collector.execute(AgentInput(
            task_id="t_mock", tenant_id="tenant_001", payload={"indicator": ind}))
        all_points += out.result["data_points"]

    gateway = _FakeGateway()
    result = await TechIndustryAgent(gateway).execute(AgentInput(
        task_id="t_mock", tenant_id="tenant_001",
        payload={"focus": "半导体", "data_points": all_points}))

    calc = result.result["industry_signal_calc"]
    assert calc["watched_indicator_count"] == 48  # 销售额/出货量 各24（PE不入watch）
    assert calc["trend"] != "数据不足"
    assert calc["valuation"] != "未提供PE数据，估值维度不评价"
    # 模拟数据披露必须进入LLM上下文（防把模拟当真实）
    assert "模拟产业数据(Demo)" in gateway.prompts[0]
    assert result.result.get("skipped") is not True
