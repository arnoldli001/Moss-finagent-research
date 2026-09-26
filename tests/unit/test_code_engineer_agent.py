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


# ==================== 幂等守卫（2026-09-21 省 token）====================
#
# 背景：`POST /code-engineer/fix-gap` 是每个 HTTP 请求直接触发 execute，
# 且没有任何守卫；`data_gap_resolver` 还会对同一缺口每轮重试。实测 A19
# 五十次调用零缓存命中、输出 16.2 万 token。所以加了"该指标已有连接器就跳过"。


class _CoveringConnector:
    """假连接器：声明支持某个指标（`covering` 按这个列表精确比对）。"""

    def __init__(self, indicators: list[str]) -> None:
        self._indicators = indicators

    def supports(self, indicator: str) -> bool:  # 故意写成前缀正则
        return indicator.startswith("comm:")

    def get_capabilities(self) -> dict:
        return {"indicators": self._indicators}


def test_covering_matches_declared_indicator_exactly(monkeypatch):
    """`covering` 必须**精确**比对已声明指标。

    不能改用 `supports()`：生成出来的连接器的 supports 往往是**前缀正则**
    （实测 `comm_gold_price` 就是 `^comm:.*$`），拿它当"是否已覆盖"会让
    `comm:y` 也算命中 `comm:gold_price` 的连接器 —— 结果是任何同前缀的
    新指标都永远不会被生成，而且不报错。这个坑是先写错、被
    `test_code_engineer_rejects_unsafe_code` 逮到的。
    """
    from src.infrastructure.connectors.dynamic_loader import DynamicConnectorLoader

    loader = DynamicConnectorLoader(directory=".")
    loader._loaded = {  # noqa: SLF001 单测直接摆状态，免得真去加载磁盘文件
        "comm_gold": _CoveringConnector(["comm:gold_price"]),
    }
    assert loader.covering("comm:gold_price") == ["_CoveringConnector"]
    assert loader.covering("comm:y") == [], "同前缀的未声明指标不该算已覆盖"
    assert loader.covering("") == []


@pytest.mark.asyncio
async def test_code_engineer_skips_when_indicator_already_covered(
        tmp_path, monkeypatch):
    """已有连接器覆盖该指标 → 不调 LLM、直接返回 skipped。"""
    from src.infrastructure.connectors import dynamic_loader as dl

    monkeypatch.setattr(dl, "DYNAMIC_DIR", tmp_path)
    monkeypatch.setattr(
        "src.domain.agents.engineering.code_engineer.agent.DYNAMIC_DIR", tmp_path)

    loader = dl.get_dynamic_loader()
    monkeypatch.setattr(loader, "_loaded", {
        "comm_gold": _CoveringConnector(["comm:gold_price"]),
    })

    gateway = FakeGateway()
    agent = CodeEngineerAgent(gateway)
    output = await agent.execute(AgentInput(
        task_id="t_skip", tenant_id="tenant_001",
        payload={"gap_description": "金价", "indicator": "comm:gold_price"},
    ))
    assert output.result["skipped"] is True
    assert gateway.calls == [], "守卫命中时不该产生任何 LLM 调用"
    assert not list(tmp_path.glob("*.py")), "不该重复生成文件"


@pytest.mark.asyncio
async def test_code_engineer_force_bypasses_idempotency_guard(
        tmp_path, monkeypatch):
    """`force=True` 必须能穿透守卫重新生成（否则"强制修复"就失效了）。"""
    from src.infrastructure.connectors import dynamic_loader as dl

    loader = dl.get_dynamic_loader()
    # 单例的 `_dir` 是构造时定死的，得同时改实例属性，reload() 才会扫到 tmp_path
    monkeypatch.setattr(loader, "_dir", tmp_path)
    monkeypatch.setattr(
        "src.domain.agents.engineering.code_engineer.agent.DYNAMIC_DIR", tmp_path)
    monkeypatch.setattr(loader, "_loaded", {
        "comm_gold": _CoveringConnector(["comm:gold_price"]),
    })

    gateway = FakeGateway()
    agent = CodeEngineerAgent(gateway)
    try:
        output = await agent.execute(AgentInput(
            task_id="t_force", tenant_id="tenant_001",
            payload={"gap_description": "金价", "indicator": "comm:gold_price",
                     "force": True},
        ))
    except AgentExecutionError:
        # 生成链路的成功与否不是本用例的关注点（它依赖沙箱与文件加载）；
        # 这里只锁一件事：**守卫没有短路**，LLM 确实被调用了。
        output = None
    assert gateway.calls, "force=True 时必须穿透幂等守卫、真的重新生成"
    if output is not None:
        assert output.result.get("skipped") is not True
