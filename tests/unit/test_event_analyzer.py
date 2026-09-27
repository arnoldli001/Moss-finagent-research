"""两阶段事件分析器测试（T5/FR-5）：FakeGateway，不调真实LLM。"""

from __future__ import annotations

import json
import re

import pytest

from src.core.exceptions import LLMGatewayError
from src.domain.alerts.analyzer import (
    AGENT_ID,
    ALLOW_CLOUD_ENV,
    STAGE1_ATTEMPTS,
    STAGE1_BATCH,
    STAGE1_TIER,
    STAGE2_BATCH,
    STAGE2_TIER,
    EventAnalyzer,
    salvage_assessments,
)
from src.domain.alerts.models import Event, EventType
from src.infrastructure.llm.models import LLMResponse

#: 阶段二的 system 标识（用来把两次调用分开 —— 两个阶段现在**同层**
#: `medium`，所以不能再靠 task_tier 区分）。
_STAGE2_MARK = "风险与机会评估引擎"


def _event(eid: str, etype: EventType = EventType.POLICY,
           title: str = "政策事件") -> Event:
    return Event(
        event_id=eid, event_key=f"k_{eid}", event_type=etype, title=title,
        content="正文内容", source_name="测试源", source_url="https://x/1",
        publish_time="2026-09-14 08:00:00", fetch_time="2026-09-14T17:30:00+08:00")


class FakeGateway:
    """假网关：记录每次调用的 `(层级, json模式, agent_id, local_only)`。"""

    def __init__(self, stage1: dict | None = None, stage2: dict | None = None,
                 fail: tuple[str, ...] = (), bad_json_tier: str = "",
                 fail_batches: tuple[int, ...] = (),
                 fail_stage1_batches: tuple[int, ...] = ()) -> None:
        self.stage1 = stage1
        self.stage2 = stage2
        #: 让哪些阶段/层级失败。可写阶段名（`stage1`/`stage2`）或层级名。
        self.fail = fail
        self.bad_json_tier = bad_json_tier
        #: 让第 N 个**阶段二**调用失败（1 起）—— 验证"单批失败只丢那一批"
        self.fail_batches = fail_batches
        #: 同上，针对阶段一
        self.fail_stage1_batches = fail_stage1_batches
        self.stage2_seen = 0
        #: 第一次阶段一调用故意返回不可解析内容（验证重试）
        self.stage1_bad_once = False
        self.stage1_seen = 0
        self.calls: list[tuple[str, bool, str, bool]] = []
        self.schemas: list[object] = []

    @staticmethod
    def _stage(system: str) -> str:
        return "stage2" if _STAGE2_MARK in system else "stage1"

    async def complete(self, task_tier, system, prompt, *, agent_id="",
                       trace_id="", json_mode=False, use_cache=True,
                       cancel_token=None, local_only=False, json_schema=None,
                       **kwargs):
        stage = self._stage(system)
        self.calls.append((task_tier, json_mode, agent_id, local_only))
        self.schemas.append(json_schema)
        if stage == "stage1":
            self.stage1_seen += 1
        if stage == "stage2":
            self.stage2_seen += 1
        if task_tier in self.fail or stage in self.fail:
            raise LLMGatewayError(f"{stage}/{task_tier} 不可用")
        if stage == "stage1" and self.stage1_seen in self.fail_stage1_batches:
            raise LLMGatewayError(f"stage1 第 {self.stage1_seen} 批不可用")
        if stage == "stage2" and self.stage2_seen in self.fail_batches:
            raise LLMGatewayError(f"stage2 第 {self.stage2_seen} 批不可用")
        if (task_tier == self.bad_json_tier or stage == self.bad_json_tier
                or (stage == "stage1" and self.stage1_bad_once
                    and self.stage1_seen == 1)):
            content = "不是JSON"
        elif stage == "stage1" and self.stage1 is None:
            # 按 prompt 里出现的事件 id 逐条产出（分批正确性靠它验证）
            ids = list(dict.fromkeys(re.findall(r"\b(e\d+)\b", prompt)))
            content = json.dumps({"events": [
                {"event_id": i, "event_type": "policy", "sentiment": "neutral",
                 "entities": {"industries": [], "companies": [], "regions": []},
                 "summary": f"摘要{i}"} for i in ids]}, ensure_ascii=False)
        elif stage == "stage2" and self.stage2 is None:
            # 按 prompt 里出现的事件 id 逐条产出 —— 这样才能验证"分批后
            # 每条事件都由它所在那批给出评估"（固定 payload 会掩盖分批错误）
            ids = list(dict.fromkeys(re.findall(r"\b(e\d+)\b", prompt)))
            content = json.dumps({"assessments": [
                {"event_id": i, "risk_score": 20, "opportunity_score": 30,
                 "confidence": 0.8, "affected_stocks": []} for i in ids]},
                ensure_ascii=False)
        else:
            payload = self.stage2 if stage == "stage2" else self.stage1
            content = json.dumps(payload or {}, ensure_ascii=False)
        return LLMResponse(content=content, model_used=f"fake-{stage}",
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
    assert a1.model_used == "fake-stage2"
    # 非法枚举归一
    a2 = by_id["e2"]
    assert a2.event_type == EventType.SECTOR and a2.sentiment == "neutral"
    # 每次扫描 2 次调用（阶段一 1 次 + 阶段二 1 批），都走**本地层**、
    # json 模式、带上 agent_id，且**不允许付费云端**。
    assert [c[0] for c in gateway.calls] == [STAGE1_TIER, STAGE2_TIER]
    assert [c[0] for c in gateway.calls] == ["medium", "medium"]
    assert all(c[1] is True and c[2] == AGENT_ID for c in gateway.calls)
    assert all(c[3] is True for c in gateway.calls), \
        "有调用没有钉住本地模型（local_only）—— 那会花钱"


@pytest.mark.asyncio
async def test_empty_events_zero_llm_calls():
    gateway = FakeGateway()
    assert await EventAnalyzer(gateway).analyze([]) == []
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_stage1_failure_falls_back_to_local_type():
    gateway = FakeGateway(
        fail=("stage1",),
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
    gateway = FakeGateway(fail=("stage2",))
    assert await EventAnalyzer(gateway).analyze([_event("e1")]) == []
    assert [c[0] for c in gateway.calls] == ["medium", "medium"]


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
    gateway = FakeGateway(stage1={"events": []}, bad_json_tier="stage2")
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
            # 阶段二才返回截断内容（阶段划分看 system，不能再看层级 ——
            # 两个阶段现在同层 `medium`）
            system = args[0] if args else kwargs.get("system", "")
            if self._stage(system) != "stage2":
                return await super().complete(task_tier, *args, **kwargs)
            self.calls.append((task_tier, True, AGENT_ID,
                               kwargs.get("local_only", False)))
            return LLMResponse(
                content='{"assessments":[{"event_id":"e1","risk_score":12,'
                        '"opportunity_score":90,"confidence":0.91,'
                        '"affected_stocks":[],"impact_path":"政策→业绩"},'
                        '{"event_id":"e2","risk_',  # 截断
                model_used="fake-stage2", provider="fake")

    results = await EventAnalyzer(
        TruncatedGateway(), resolver=_resolver).analyze(
            [_event("e1"), _event("e2")])
    assert [r.event_id for r in results] == ["e1"]
    assert results[0].opportunity_score == 90


def test_stage2_prompt_carries_disclaimer():
    """TR-5.1/D8：打分层system须含免责定位约束。"""
    from src.domain.alerts.prompts import STAGE2_SYSTEM

    assert "不构成投资建议" in STAGE2_SYSTEM


# ======================================================================
# 成本口径（2026-09-26）：两个阶段都只用本地模型
# ======================================================================

@pytest.mark.asyncio
async def test_both_stages_are_pinned_to_local_models(monkeypatch):
    """★ 两个阶段都必须 `local_only=True` —— 绝不降级到付费云端。

    背景（审计实测）：阶段二原先写死 `reasoning`，而 `configs/models.yaml`
    里 `reasoning` 的 **primary 是 deepseek-flash（付费）**、本机 Ollama 只是
    fallback —— 于是 53 次调用花了 0.32 元，而本地 qwen3:8b 完全够用。
    """
    monkeypatch.delenv(ALLOW_CLOUD_ENV, raising=False)
    gateway = FakeGateway(stage1={"events": []})
    await EventAnalyzer(gateway).analyze([_event("e1")])

    assert [c[0] for c in gateway.calls] == [STAGE1_TIER, STAGE2_TIER]
    assert STAGE2_TIER == "medium", "阶段二不能再回到 reasoning（那条链是付费的）"
    assert all(c[3] is True for c in gateway.calls), \
        "有调用允许了付费云端：本地模型一挂就会真的去调 DeepSeek"


@pytest.mark.asyncio
async def test_cloud_requires_an_explicit_opt_in(monkeypatch):
    """要花钱必须**显式**打开 `MOSS_ALERT_ALLOW_CLOUD=1`（默认关闭）。"""
    from src.domain.alerts.analyzer import allow_cloud

    monkeypatch.delenv(ALLOW_CLOUD_ENV, raising=False)
    assert allow_cloud() is False
    gateway = FakeGateway(stage1={"events": []})
    await EventAnalyzer(gateway).analyze([_event("e1")])
    assert all(c[3] is True for c in gateway.calls)

    monkeypatch.setenv(ALLOW_CLOUD_ENV, "1")
    assert allow_cloud() is True
    gateway2 = FakeGateway(stage1={"events": []})
    await EventAnalyzer(gateway2).analyze([_event("e1")])
    assert all(c[3] is False for c in gateway2.calls)


@pytest.mark.asyncio
async def test_stage2_is_batched_to_fit_the_local_output_budget():
    """★ 分批：本地 `max_tokens=4096`、实测 ~43 token/s。

    一次塞 30 条 → 输出被截断（尾部事件丢评估）且易撞 120 秒超时。
    所以按 `STAGE2_BATCH` 分批，并保证**每条事件都由它所在那批**给出评估。
    """
    n = STAGE2_BATCH * 2 + 1              # 17 条 → 阶段二 3 批（8/8/1）
    events = [_event(f"e{i}", title=f"事件{i}") for i in range(1, n + 1)]
    gateway = FakeGateway(stage1={"events": []})
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(events)

    assert gateway.stage2_seen == 3, f"阶段二应有 3 批，实际 {gateway.stage2_seen}"
    assert len(results) == n, "分批后每条事件都要有评估（不能只出第一批）"
    assert {a.event_id for a in results} == {e.event_id for e in events}


@pytest.mark.asyncio
async def test_one_failed_batch_does_not_drop_the_others():
    """单批失败只丢那一批 —— 其余批次的事件仍然出告警。

    原先的实现是"一次调用失败 → 整轮无评估"，本地模型偶发抖动就会
    让**整轮**事件告警消失（下轮才重试）。
    """
    n = STAGE2_BATCH * 2
    events = [_event(f"e{i}", title=f"事件{i}") for i in range(1, n + 1)]
    gateway = FakeGateway(stage1={"events": []}, fail_batches=(2,))
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(events)
    assert len(results) == STAGE2_BATCH, "第 2 批失败不该影响第 1 批"
    assert all(a.event_id.startswith("e") for a in results)
    assert len(results) < n, "第 2 批确实丢了（否则这条测试没覆盖到失败路径）"


def test_stage2_batch_fits_the_local_output_budget():
    """分批大小必须是**算得出来的**，不是随手写的数字。

    本地实测每条事件约 330 个输出 token（含思考），而 `local_medium` 的
    `max_tokens` 是 4096 —— 首批大小 × 330 必须留在预算内。
    """
    assert 0 < STAGE2_BATCH <= 10, "太大：撞输出上限/超时"
    assert STAGE2_BATCH * 330 < 4096, "首批输出会超过 local_medium 的 max_tokens"


@pytest.mark.asyncio
async def test_stage1_is_batched_too():
    """★ 阶段一也要分批（生产日志实证的截断）。

    实测：140 条事件的一轮扫描里，阶段一一条调用要输出全部事件的
    `{event_id,event_type,sentiment,entities,summary}`，而 qwen3:8b 是思考型
    模型（一次调用约 560 token 花在思考上）→ 被 `max_tokens` **从字符串中间
    截断**（`Unterminated string starting at: line 76`），整批退回关键词兜底。
    schema 只保证结构合法，**不保证生成跑得完** —— 预算得靠分批。
    """
    n = STAGE1_BATCH * 2 + 1                 # 21 条 → 3 批（10/10/1）
    events = [_event(f"e{i}", title=f"事件{i}") for i in range(1, n + 1)]
    gateway = FakeGateway()
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(events)
    assert gateway.stage1_seen == 3, f"阶段一应分 3 批，实际 {gateway.stage1_seen}"
    assert len(results) == n, "分批后每条事件都要有评估"


@pytest.mark.asyncio
async def test_one_failed_stage1_batch_keeps_the_others():
    """阶段一单批失败只影响那一批：失败的批次退关键词兜底，另一批照常。

    注意失败的表现是**降级**而不是丢事件：兜底仍会给每条事件一个
    「类型沿用 + 情感 neutral + 摘要空」的占位，所以评估条数不变 ——
    但不该让整个扫描退回兜底（那是修这个 bug 之前的行为）。
    """
    n = STAGE1_BATCH * 2
    events = [_event(f"e{i}", title=f"事件{i}") for i in range(1, n + 1)]
    # 让第 1 批的**两次尝试**都失败（只失败一次的话重试会救回来 ——
    # 那也是一种正确行为，但验不到"单批失败不拖累其它批"）
    gateway = FakeGateway(fail_stage1_batches=(1, 2))
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(events)

    assert gateway.stage1_seen == 1 + STAGE1_ATTEMPTS, \
        "第 1 批两次尝试都失败后才兜底，第 2 批再调一次"
    assert len(results) == n
    by_id = {a.event_id: a for a in results}
    last = f"e{n}"                      # 第 2 批的最后一条
    # 第 2 批（e7..e12）拿到真正的分类结果
    assert by_id[last].summary, "第 2 批不该被第 1 批的失败带下水"
    # 第 1 批（e1..e6）是兜底：摘要为空、情感中性
    assert by_id["e1"].summary == ""
    assert by_id["e1"].sentiment == "neutral"


@pytest.mark.asyncio
async def test_output_structure_is_enforced_by_schema():
    """★ 两个阶段都传 `json_schema`（Ollama 受约束解码）。

    只靠提示词里写"只输出JSON"对本地小模型不够：实测 qwen3:8b 有一次
    返回 `{ }` → 整批退回关键词兜底。schema 是"本地模型能不能顶上来"
    的前提，所以它必须**真的一直在传**（漏传不会报错，只会悄悄降质）。
    """
    from src.domain.alerts.prompts import STAGE1_SCHEMA, STAGE2_SCHEMA

    gateway = FakeGateway(stage1={"events": []})
    await EventAnalyzer(gateway).analyze([_event("e1")])
    assert gateway.schemas == [STAGE1_SCHEMA, STAGE2_SCHEMA]
    # schema 里的输出格式必须与提示词里写死的一致（尤其 entities）
    stage1_props = STAGE1_SCHEMA["properties"]["events"]["items"]["properties"]
    assert {"event_id", "event_type", "sentiment", "entities",
            "summary"} == set(stage1_props)
    assert stage1_props["entities"]["required"] == [
        "industries", "companies", "regions"]


@pytest.mark.asyncio
async def test_stage1_retries_then_succeeds():
    """阶段一第一次返回不可解析内容 → **重试**（本地免费），不立刻降级。"""
    gateway = FakeGateway(stage1={"events": [
        {"event_id": "e1", "event_type": "stock", "sentiment": "negative",
         "entities": {"industries": ["光伏"], "companies": [], "regions": []},
         "summary": "净利下滑"}]})
    gateway.stage1_bad_once = True
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(
        [_event("e1")])
    assert gateway.stage1_seen == 2, "第一次坏输出后应当重试一次"
    assert results and results[0].sentiment == "negative", \
        "重试成功后不该退化成关键词兜底（那会恒为 neutral）"
    assert results[0].affected_industries == ["光伏"]


@pytest.mark.asyncio
async def test_stage1_falls_back_only_after_the_retry():
    """两次都坏才退回关键词兜底（功能不中断，但摘要为空、情感中性）。"""
    gateway = FakeGateway(stage1=None, bad_json_tier="stage1")
    results = await EventAnalyzer(gateway, resolver=_resolver).analyze(
        [_event("e1", EventType.STOCK)])
    assert gateway.stage1_seen == 2
    assert results, "阶段二仍然给出了评估"
