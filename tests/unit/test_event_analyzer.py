"""两阶段事件分析器测试（T5/FR-5）：FakeGateway，不调真实LLM。"""

from __future__ import annotations

import json

import pytest

from src.core.exceptions import LLMGatewayError
from src.domain.alerts.analyzer import (
    AGENT_ID,
    EventAnalyzer,
    salvage_assessments,
)
from src.domain.alerts.models import Event, EventType
from src.infrastructure.llm.models import LLMResponse


def _event(eid: str, etype: EventType = EventType.POLICY,
           title: str = "政策事件") -> Event:
    return Event(
        event_id=eid, event_key=f"k_{eid}", event_type=etype, title=title,
        content="正文内容", source_name="测试源", source_url="https://x/1",
        publish_time="2026-09-14 08:00:00", fetch_time="2026-09-14T17:30:00+08:00")


class FakeGateway:
    def __init__(self, stage1: dict | None = None, stage2: dict | None = None,
                 fail: tuple[str, ...] = (), bad_json_tier: str = "") -> None:
        self.stage1 = stage1
        self.stage2 = stage2
        self.fail = fail
        self.bad_json_tier = bad_json_tier
        self.calls: list[tuple[str, bool, str]] = []

    async def complete(self, task_tier, system, prompt, *, agent_id="",
                       trace_id="", json_mode=False, use_cache=True,
                       cancel_token=None):
        self.calls.append((task_tier, json_mode, agent_id))
        if task_tier in self.fail:
            raise LLMGatewayError(f"{task_tier} 不可用")
        payload = self.stage1 if task_tier == "medium" else self.stage2
        content = "不是JSON" if task_tier == self.bad_json_tier \
            else json.dumps(payload or {}, ensure_ascii=False)
        return LLMResponse(content=content, model_used=f"fake-{task_tier}",
                           provider="fake")


async def _resolver(name: str):
    table = {"示例科技": ("300308", "示例科技"), "龙头股份": ("600001", "龙头股份")}
    return table.get(name)


@pytest.mark.asyncio
async def test_happy_path_two_events_with_codes():
    gateway = FakeGateway(
        stage1={"events": [
            {"event_id": "e1", "event_type": "policy", "sentiment": "positive",
             "entities": {"industries": ["固态电池"], "companies": ["示例科技"],
                          "regions": []},
             "summary": "政策利好固态电池"},
            {"event_id": "e2", "event_type": "weird", "sentiment": "panic",
             "entities": {}, "summary": "类型与情感非法→归一"},
            {"event_id": "ghost", "event_type": "stock"},
        ]},
        stage2={"assessments": [
            {"event_id": "e1", "risk_score": 20, "opportunity_score": 92,
             "confidence": 0.91,
             "affected_stocks": [{"name": "示例科技", "impact": "positive",
                                  "reason": "政策受益"}],
             "impact_path": "补贴→需求→业绩"},
            {"event_id": "e2", "risk_score": 70, "opportunity_score": 10,
             "confidence": 0.8, "affected_stocks": []},
        ]})
    analyzer = EventAnalyzer(gateway, resolver=_resolver)
    results = await analyzer.analyze([_event("e1"), _event("e2", EventType.SECTOR,
                                                           "板块事件")])

    assert len(results) == 2
    by_id = {a.event_id: a for a in results}
    a1 = by_id["e1"]
    assert a1.opportunity_score == 92 and a1.confidence == 0.91
    assert a1.affected_industries == ["固态电池"]
    assert a1.affected_stocks[0].code == "300308"
    assert a1.impact_path == "补贴→需求→业绩" and a1.summary
    assert a1.model_used == "fake-reasoning"
    # 非法枚举归一
    a2 = by_id["e2"]
    assert a2.event_type == EventType.SECTOR and a2.sentiment == "neutral"
    # 每次扫描恰好2次调用，medium/reasoning + json模式 + agent_id
    assert [c[0] for c in gateway.calls] == ["medium", "reasoning"]
    assert all(c[1] is True and c[2] == AGENT_ID for c in gateway.calls)


@pytest.mark.asyncio
async def test_empty_events_zero_llm_calls():
    gateway = FakeGateway()
    assert await EventAnalyzer(gateway).analyze([]) == []
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_stage1_failure_falls_back_to_local_type():
    gateway = FakeGateway(
        fail=("medium",),
        stage2={"assessments": [
            {"event_id": "e1", "risk_score": 80, "opportunity_score": 5,
             "confidence": 0.88, "affected_stocks": []}]})
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(
        [_event("e1", EventType.STOCK, "中际旭创业绩")])
    assert len(results) == 1
    assert results[0].event_type == EventType.STOCK
    assert results[0].risk_score == 80


@pytest.mark.asyncio
async def test_stage2_failure_returns_empty():
    gateway = FakeGateway(fail=("reasoning",))
    assert await EventAnalyzer(gateway).analyze([_event("e1")]) == []
    assert [c[0] for c in gateway.calls] == ["medium", "reasoning"]


@pytest.mark.asyncio
async def test_out_of_range_scores_drop_event():
    """FR-6/D3：risk/opp/confidence越界直接丢弃该事件评估（不做clamp饱和）。"""
    gateway = FakeGateway(
        stage1={"events": []},  # 空数组→全部走本地兜底
        stage2={"assessments": [
            {"event_id": "e1", "risk_score": 150, "opportunity_score": -20,
             "confidence": 2.0, "affected_stocks": [
                 {"name": "龙头股份", "impact": "positive", "reason": "x"}]},
        ]})
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(
        [_event("e1")])
    assert results == []  # 越界评估整体丢弃，事件保持未分析下轮重试


@pytest.mark.asyncio
async def test_one_invalid_event_does_not_drop_batch():
    """单事件越界不影响同批其他事件。"""
    gateway = FakeGateway(
        stage1={"events": []},
        stage2={"assessments": [
            {"event_id": "bad", "risk_score": 999, "confidence": 0.9},
            {"event_id": "ok", "risk_score": 20, "opportunity_score": 90,
             "confidence": 0.9, "affected_stocks": []},
        ]})
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(
        [_event("bad"), _event("ok", title="政策事件二")])
    assert [a.event_id for a in results] == ["ok"]


@pytest.mark.asyncio
async def test_stage2_bad_json_drops_batch():
    gateway = FakeGateway(stage1={"events": []}, bad_json_tier="reasoning")
    assert await EventAnalyzer(gateway).analyze([_event("e1")]) == []


@pytest.mark.asyncio
async def test_non_numeric_scores_default_zero():
    gateway = FakeGateway(
        stage1={"events": []},
        stage2={"assessments": [
            {"event_id": "e1", "risk_score": "高",
             "opportunity_score": None, "confidence": "abc"}]})
    a = (await EventAnalyzer(gateway).analyze([_event("e1")]))[0]
    assert a.risk_score == 0 and a.opportunity_score == 0 and a.confidence == 0


def test_salvage_assessments_truncated_tail():
    """reasoning输出被max_tokens截断时，抢救完整前缀，坏对象之后全部放弃。"""
    content = (
        '思考过程略 {"assessments":['
        '{"event_id":"e1","risk_score":10,"opportunity_score":88,'
        '"confidence":0.9,"affected_stocks":[],"impact_path":"x"},'
        '{"event_id":"e2","risk_score":7'  # 尾部被硬截断
    )
    out = salvage_assessments(content)
    assert [o["event_id"] for o in out] == ["e1"]
    assert salvage_assessments("不是JSON") == []


@pytest.mark.asyncio
async def test_analyzer_uses_salvaged_prefix_on_truncation():
    class TruncatedGateway(FakeGateway):
        def __init__(self):
            super().__init__(stage1={"events": []})

        async def complete(self, task_tier, *args, **kwargs):  # type: ignore[override]
            if task_tier != "reasoning":
                return await super().complete(task_tier, *args, **kwargs)
            return LLMResponse(
                content='{"assessments":[{"event_id":"e1","risk_score":12,'
                        '"opportunity_score":90,"confidence":0.91,'
                        '"affected_stocks":[],"impact_path":"政策→业绩"},'
                        '{"event_id":"e2","risk_',  # 截断
                model_used="fake-reasoning", provider="fake")

    results = await EventAnalyzer(
        TruncatedGateway(), resolver=_resolver).analyze(
            [_event("e1"), _event("e2")])
    assert [r.event_id for r in results] == ["e1"]
    assert results[0].opportunity_score == 90


def test_stage2_prompt_carries_disclaimer():
    """TR-5.1/D8：打分层system须含免责定位约束。"""
    from src.domain.alerts.prompts import STAGE2_SYSTEM

    assert "不构成投资建议" in STAGE2_SYSTEM
