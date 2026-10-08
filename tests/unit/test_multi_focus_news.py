"""★★★ 多标的问句的**信息层**：逐只取新闻 + A06/A07 按标的出分（单标的逐字不变）。

## 报障原话（用户 2026-10-08，与 `CHG-0216`/`CHG-0217` 同一条）

> 「…未来半年能否持有高股息的**宁波银行**和**中国神华**？**标的 601088**」
> —— 反馈：中国神华缺个股估值与股息、**宁波银行没有任何可引用的估值**。

估值那一半由 `CHG-0216`/`CHG-0229` 修掉（每只票各采一份、各算一份）；
**新闻/事件/舆情这一半**当时仍只按**单值** `target` 取：

    target = state.get("target") or ""
    if re.fullmatch(r"\\d{6}", target):
        await news_fetcher.fetch_news(target)      # ← 只有一只票

⇒ 第二只票的信息层输入**静默为 0**（A05/A06/A07 拿到零输入整体空跳过），
用户读到"该股近期无消息"——**看起来像数据源坏了，其实是根本没去取**。
而即使两条新闻都取到了，A07 仍会把**全部事件混算成一个情绪分**
（`info/sentiment/logic.py`）⇒ 两只票的利好/利空互相抵消。

## 本文件守的三件事（对应任务书的 ①②③）

1. **逐只取新闻**：`focus_stock_codes` 里每只票各 `fetch_news(code)` 一次，
   条目上带 `stock_code`（采集层盖的章，可核对）；
2. **单标的逐字不变**：`len(codes) < 2` ⇒ 走**原文那条** `fetch_news(target)`
   分支，条目一个字段都不改（图级护栏钉住整条链路）；
3. **A06 带归属、A07 分组出分**：多标的时事件带 `stock_code`、A07 逐只给情绪分
   与周期；**单标的时 prompt / schema / result 键与修复前逐字相同**。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence
from src.domain.agents.audit.verifier.agent import AuditAgent
from src.domain.agents.data.cleaner.agent import DataCleanerAgent
from src.domain.agents.data.storage.agent import DataStorageAgent
from src.domain.agents.data.validator.agent import DataValidatorAgent
from src.domain.agents.decision.recommend.agent import RecommendationAgent
from src.domain.agents.info.extractor.agent import ExtractorAgent
from src.domain.agents.info.sentiment.agent import SentimentAgent
from src.domain.agents.info.sentiment.logic import (
    compute_sentiment_metrics,
    compute_sentiment_metrics_by_group,
    group_events,
)
from src.domain.agents.info.verifier.agent import VerifierAgent
from src.infrastructure.repositories.macro_repo import MacroRepository
from src.orchestration.supervisor import (
    _fetch_news_per_code,
    _news_focus_codes,
    build_research_graph,
)

_ACCEPTANCE_QUERY = (
    "当前宏观环境如何，预测下未来一年美国的加息预期下，基于当前板块拥挤度和"
    "能源重点项目与新业态投资20万亿的政策，未来半年能否持有高股息的"
    "宁波银行和中国神华？"
)

#: 单标的问句（同一只票）—— 用于"逐字不变"的那一半
_SINGLE_QUERY = "未来半年能否持有高股息的中国神华？"

#: A06 单标的时的条目段**冻结**（修复前原文；多标的只在前面多一个 `(代码)` 前缀）
_FROZEN_SINGLE_ITEM_BLOCK = "- [info_1] 中国神华新闻 | 中国神华正文"

#: A07 单标的时的 result 键**冻结**（多标的才追加 by_group/multi_subject）
_FROZEN_SINGLE_RESULT_KEYS = {
    "sentiment_metrics", "sentiment_phase", "narrative", "model_used"}

#: A07 单标的时的 json_schema 属性**冻结**（约束解码的契约，不许悄悄多/少键）
_FROZEN_SINGLE_SCHEMA_KEYS = ["conclusion", "confidence", "sentiment_phase",
                              "narrative"]


# ============================================================
# 替身
# ============================================================


class _StubGateway:
    """按调用记录 + 固定 JSON 回复（可指定每次调用的回复）。"""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def complete(self, tier: str, system_prompt: str, prompt: str,
                       **kwargs: Any) -> Any:
        from src.infrastructure.llm.models import LLMResponse

        self.calls.append({"tier": tier, "system": system_prompt,
                           "prompt": prompt, **kwargs})
        return LLMResponse(content=json.dumps(self.reply, ensure_ascii=False),
                           model_used="stub", provider="stub",
                           tokens_in=3, tokens_out=4)


class _FakeNewsFetcher:
    """逐只返回一条新闻；**故意**把 `stock_code` 填成股票名（上游真实行为）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.topic_calls: list[list[str]] = []

    async def fetch_news(self, code: str, limit: int = 10) -> list[dict]:
        self.calls.append(code)
        return [{"title": f"{code} 新闻", "text": f"{code} 正文",
                 "source_name": "东方财富", "source_url": f"http://x/{code}",
                 "publish_time": "2026-10-08", "stock_code": "关键词列的值"}]

    async def fetch_topic_news(self, keywords: list[str],
                               limit: int = 15) -> list[dict]:
        self.topic_calls.append(list(keywords))
        return []


class _GraphGateway:
    """图级替身：按系统提示里的标记返回预置回复（未预置 ⇒ 断言失败）。"""

    def __init__(self) -> None:
        self.replies: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def set(self, marker: str, reply: dict[str, Any]) -> None:
        self.replies[marker] = reply

    async def complete(self, task_tier, system, prompt, **kwargs):
        from src.infrastructure.llm.models import LLMResponse

        for marker, reply in self.replies.items():
            if marker in system:
                self.calls.append(marker)
                return LLMResponse(
                    content=json.dumps(reply, ensure_ascii=False),
                    model_used="fake", provider="fake",
                    prompt_hash="ph", response_hash="rh")
        raise AssertionError(f"未预置的系统提示: {system[:40]}")


class _EmptyCollector:
    """A01 替身：不联网、返回空（专注验证新闻链路）。"""

    async def execute(self, input: AgentInput) -> AgentOutput:
        indicator = input.payload["indicator"]
        return AgentOutput(task_id=input.task_id, agent_id="A01_data_collector",
                           conclusion=f"采集 {indicator} 0 条",
                           confidence=Confidence.LOW, data_refs=[],
                           result={"indicator": indicator, "data_points": []})


_INFO_REPLY5 = {"reviews": [
    {"item_id": "info_1", "verdict": "可信", "red_flags": [], "reasoning": "官方"},
    {"item_id": "info_2", "verdict": "可信", "red_flags": [], "reasoning": "官方"},
]}
_INFO_REPLY6 = {"events": [
    {"item_id": "info_1", "event_type": "earnings", "subject": "中国神华",
     "direction": "negative", "magnitude": "利空", "event_date": "2026-10-07",
     "evidence_quote": "601088 正文", "confidence": 0.8},
    {"item_id": "info_2", "event_type": "policy", "subject": "宁波银行",
     "direction": "positive", "magnitude": "利好", "event_date": "2026-10-07",
     "evidence_quote": "002142 正文", "confidence": 0.9},
]}
_INFO_REPLY7 = {"conclusion": "宁波银行偏暖、中国神华偏冷", "confidence": "medium",
                "sentiment_phase": "分歧", "narrative": "分化",
                "per_subject": [
                    {"subject": "601088", "sentiment_phase": "谨慎",
                     "conclusion": "中国神华偏冷"},
                    {"subject": "002142", "sentiment_phase": "乐观",
                     "conclusion": "宁波银行偏暖"}]}
_REPLY17 = {"conclusion": "综合看多", "confidence": "high", "stance": "看多",
            "key_logic": [], "catalysts": [], "risks": [],
            "monitoring_points": [], "conflicts_resolved": []}


def _graph_state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "task_multi_news", "tenant_id": "tenant_001",
        "user_query": _ACCEPTANCE_QUERY, "analysis_type": "stock",
        "target": "601088", "target_display": "中国神华",
        "plan": [], "raw_points": [], "cleaned_points": [], "validated_points": [],
        "validation_report": {}, "storage_stats": {},
        "info_items": [], "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }
    return {**base, **over}


def _build_graph(tmp_dir: str, fetcher: _FakeNewsFetcher):
    gw = _GraphGateway()
    gw.set("信息核查员", _INFO_REPLY5)
    gw.set("财经信息结构化专家", _INFO_REPLY6)
    gw.set("市场舆情分析师", _INFO_REPLY7)
    gw.set("投研委员会主席", _REPLY17)
    agents = {
        "A01_data_collector": _EmptyCollector(),
        "A02_data_cleaner": DataCleanerAgent(),
        "A03_data_validator": DataValidatorAgent(),
        "A04_data_storage": DataStorageAgent(MacroRepository(f"{tmp_dir}/mn.db")),
        "A05_verifier": VerifierAgent(gw),
        "A06_extractor": ExtractorAgent(gw),
        "A07_sentiment": SentimentAgent(gw),
        "A17_recommend": RecommendationAgent(gw),
        "A18_audit": AuditAgent(),
    }
    graph = build_research_graph(
        agents, chain_path=f"{tmp_dir}/mn_c.jsonl",
        llm_audit_path=f"{tmp_dir}/mn_l.jsonl", news_fetcher=fetcher)
    return graph, gw


# ============================================================
# ① 逐只取新闻（判据 + 条目归属）
# ============================================================


class TestFocusCodesForNews:
    def test_codes_are_deduped_and_non_codes_dropped(self) -> None:
        assert _news_focus_codes(
            {"focus_stock_codes": ("002142", "601088", "002142", "茅台", "")}
        ) == ["002142", "601088"]

    def test_missing_key_yields_no_codes(self) -> None:
        """没有 `focus_stock_codes`（既有调用点/单标的场景）⇒ 空列表 ⇒ 走原文路径。"""
        assert _news_focus_codes({}) == []
        assert _news_focus_codes({"target": "601088"}) == []

    @pytest.mark.asyncio
    async def test_each_code_is_fetched_once_and_tagged(self) -> None:
        fetcher = _FakeNewsFetcher()
        items = await _fetch_news_per_code(fetcher, ["601088", "002142"])
        assert fetcher.calls == ["601088", "002142"]
        assert [i["stock_code"] for i in items] == ["601088", "002142"], (
            "条目上的 `stock_code` 必须是**我们要的那个 6 位代码**"
            "（上游按东财『关键词』列填这个字段，值可能是股票名，不能当代码用）")
        assert len(items) == 2, "每只票的条目都要留下（一只取空不许影响另一只）"

    @pytest.mark.asyncio
    async def test_one_code_returning_empty_does_not_drop_the_other(self) -> None:
        class _HalfFetcher(_FakeNewsFetcher):
            async def fetch_news(self, code: str, limit: int = 10) -> list[dict]:
                self.calls.append(code)
                if code == "002142":
                    return []
                return await super().fetch_news(code, limit)

        fetcher = _HalfFetcher()
        items = await _fetch_news_per_code(fetcher, ["002142", "601088"])
        assert [i["stock_code"] for i in items] == ["601088"]


# ============================================================
# ② 采集节点的接线（图级）—— "我改了" ≠ "它生效了"
# ============================================================


class TestCollectNodeWiring:
    @pytest.mark.asyncio
    async def test_multi_target_fetches_every_code(self, tmp_dir: str) -> None:
        """★★ 两个 `focus_stock_codes` ⇒ **每只各取一次**，条目带 code。"""
        fetcher = _FakeNewsFetcher()
        graph, _gw = _build_graph(tmp_dir, fetcher)
        final = await graph.ainvoke(_graph_state(
            focus_stock_code="601088",
            focus_stock_codes=("601088", "002142"),
            user_query=_ACCEPTANCE_QUERY))

        assert fetcher.calls == ["601088", "002142"], (
            "第二条新闻**静默为 0** —— 这正是报障的形态")
        items = final["info_items"]
        assert len(items) == 2
        assert {i["stock_code"] for i in items} == {"601088", "002142"}
        # 事件表继承归属（A06 的产物在 state 里）
        events = final["extracted_events"]["events"]
        assert {e["stock_code"] for e in events} == {"601088", "002142"}, events
        # 信息层真的跑了（不是"拿到零输入全部空跳过"）
        aids = {o["agent_id"] for o in final["agent_outputs"]}
        assert {"A05_verifier", "A06_extractor", "A07_sentiment"} <= aids
        # A07 逐只出分（本地计算，两只票各一份）
        senti = next(o for o in final["agent_outputs"]
                     if o["agent_id"] == "A07_sentiment")
        by_group = senti["result"]["sentiment_metrics_by_group"]
        assert set(by_group) == {"601088", "002142"}
        assert by_group["601088"]["weighted_sentiment"] == -1.0
        assert by_group["002142"]["weighted_sentiment"] == 1.0
        assert senti["result"]["sentiment_phase_by_group"] == {
            "601088": "谨慎", "002142": "乐观"}
        # 报告里同时有两只票的舆情（用户要能引用）
        assert "宁波银行" in final["final_report"]

    @pytest.mark.asyncio
    async def test_single_target_path_is_byte_identical(self, tmp_dir: str) -> None:
        """★★ 单标的：仍然只调一次 `fetch_news(target)`，且条目**一个字段都不改**。

        判据取"条目 == 采集器原样返回的 dict"（不是"包含某键"）——
        这才叫逐字不变；同时钉住调用参数（不带 `limit=`，与修复前同）。
        """
        fetcher = _FakeNewsFetcher()
        graph, _gw = _build_graph(tmp_dir, fetcher)
        final = await graph.ainvoke(_graph_state(
            analysis_type="stock", target="601088", target_display="中国神华",
            focus_stock_code="601088", user_query=_SINGLE_QUERY))

        assert fetcher.calls == ["601088"], "单标的多取一次就是回归"
        assert final["info_items"] == [{
            "title": "601088 新闻", "text": "601088 正文",
            "source_name": "东方财富", "source_url": "http://x/601088",
            "publish_time": "2026-10-08", "stock_code": "关键词列的值"}], (
            "单标的路径的条目被改动了（这条路上采集层不该碰任何字段）")


# ============================================================
# ③ A06：多标的带归属，单标的 prompt / 事件字段逐字不变
# ============================================================


class TestExtractorAttribution:
    def _agent_input(self, items: list[dict[str, Any]]) -> AgentInput:
        return AgentInput(task_id="t-a06", tenant_id="tenant_001",
                          payload={"info_items": items})

    @pytest.mark.asyncio
    async def test_single_target_prompt_and_event_shape_unchanged(self) -> None:
        gw = _StubGateway({"events": [
            {"item_id": "info_1", "event_type": "policy", "subject": "中国神华",
             "direction": "positive", "evidence_quote": "x", "confidence": 0.9}]})
        out = await ExtractorAgent(gw).execute(self._agent_input([
            {"item_id": "info_1", "title": "中国神华新闻", "text": "中国神华正文"}]))
        prompt = gw.calls[0]["prompt"]
        # 条目段与修复前**逐字相同**（既没有 `(代码)` 前缀，也没有多标的说明）
        assert _FROZEN_SINGLE_ITEM_BLOCK in prompt
        assert "多标的" not in prompt
        assert "(" + "601088" + ")" not in prompt
        # 事件字段与修复前相同（**没有** stock_code）
        assert "stock_code" not in out.result["events"][0]

    @pytest.mark.asyncio
    async def test_multi_target_items_carry_their_code(self) -> None:
        gw = _StubGateway({"events": [
            {"item_id": "info_1", "event_type": "policy", "subject": "宁波银行",
             "direction": "positive", "evidence_quote": "x", "confidence": 0.9},
            {"item_id": "info_2", "event_type": "earnings", "subject": "中国神华",
             "direction": "negative", "evidence_quote": "y", "confidence": 0.8}]})
        out = await ExtractorAgent(gw).execute(self._agent_input([
            {"item_id": "info_1", "title": "宁波银行新闻", "text": "宁波银行正文",
             "stock_code": "002142"},
            {"item_id": "info_2", "title": "中国神华新闻", "text": "中国神华正文",
             "stock_code": "601088"},
        ]))
        prompt = gw.calls[0]["prompt"]
        assert "- [info_1] (002142) 宁波银行新闻 | 宁波银行正文" in prompt
        assert "- [info_2] (601088) 中国神华新闻 | 中国神华正文" in prompt
        assert "多标的" in prompt
        assert [(e["stock_code"], e["subject"]) for e in out.result["events"]] == [
            ("002142", "宁波银行"), ("601088", "中国神华")], (
            "归属必须继承**来源条目**上的代码（采集层盖的章），不许让模型自己写")


# ============================================================
# ④ A07：多标的分组出分，单标的仍是原来那一个情绪分
# ============================================================


def _events_multi() -> list[dict[str, Any]]:
    return [
        {"item_id": "info_1", "subject": "宁波银行", "stock_code": "002142",
         "direction": "positive", "confidence": 0.9, "event_type": "policy",
         "evidence_quote": "x"},
        {"item_id": "info_2", "subject": "中国神华", "stock_code": "601088",
         "direction": "negative", "confidence": 0.8, "event_type": "earnings",
         "evidence_quote": "y"},
    ]


class TestSentimentGrouping:
    def test_grouping_reuses_the_same_metric_function(self) -> None:
        """逐组口径必须**复用**总体分那一个实现（不写第二套算法）。"""
        events = _events_multi()
        by_group = compute_sentiment_metrics_by_group(events)
        assert set(by_group) == {"002142", "601088"}
        for code, group in group_events(events).items():
            assert by_group[code] == compute_sentiment_metrics(group)
        # 混算 vs 分组：混算把利好利空**互相抵消**（≈0）—— 这正是要修的形态
        assert abs(compute_sentiment_metrics(events)["weighted_sentiment"]) < 0.1
        assert by_group["002142"]["weighted_sentiment"] == 1.0
        assert by_group["601088"]["weighted_sentiment"] == -1.0

    def test_events_without_code_group_by_subject(self) -> None:
        """没有代码的事件（宏观/行业）按 `subject` 分组，且**不与个股混在一组**。"""
        events = _events_multi() + [
            {"item_id": "info_3", "subject": "宏观政策", "direction": "neutral",
             "confidence": 0.5}]
        groups = group_events(events)
        assert set(groups) == {"002142", "601088", "宏观政策"}

    @pytest.mark.asyncio
    async def test_single_target_result_schema_prompt_unchanged(self) -> None:
        """★★ 单标的：result 键、json_schema 属性、prompt 指标段**逐字不变**。"""
        gw = _StubGateway({"conclusion": "情绪偏暖", "confidence": "medium",
                           "sentiment_phase": "乐观", "narrative": "n"})
        out = await SentimentAgent(gw).execute(AgentInput(
            task_id="t-a07", tenant_id="tenant_001", payload={"events": [
                {"item_id": "info_1", "subject": "中国神华",
                 "direction": "positive", "confidence": 0.9,
                 "evidence_quote": "x"}]}))
        assert set(out.result) == _FROZEN_SINGLE_RESULT_KEYS
        call = gw.calls[0]
        assert list(call["json_schema"]["properties"]) == _FROZEN_SINGLE_SCHEMA_KEYS
        assert "逐标的" not in call["prompt"]
        assert "per_subject" not in call["prompt"]
        # 指标段与修复前逐字相同（多标的块只会在它后面插入）
        assert ("热点主体 [{'subject': '中国神华', 'net_score': 0.9}]"
                "\n\n## 事件明细\n") in call["prompt"]

    @pytest.mark.asyncio
    async def test_multi_target_gives_per_code_scores_and_phases(self) -> None:
        gw = _StubGateway({"conclusion": "两只标的分化", "confidence": "medium",
                           "sentiment_phase": "分歧", "narrative": "n",
                           "per_subject": [
                               {"subject": "002142", "sentiment_phase": "乐观",
                                "conclusion": "宁波银行偏暖"},
                               {"subject": "601088", "sentiment_phase": "谨慎",
                                "conclusion": "中国神华偏冷"}]})
        out = await SentimentAgent(gw).execute(AgentInput(
            task_id="t-a07", tenant_id="tenant_001",
            payload={"events": _events_multi()}))
        assert out.result["multi_subject"] is True
        assert {k: v["weighted_sentiment"]
                for k, v in out.result["sentiment_metrics_by_group"].items()} == {
            "002142": 1.0, "601088": -1.0}
        assert out.result["sentiment_phase_by_group"] == {
            "002142": "乐观", "601088": "谨慎"}
        # 总体分照旧（形状兼容：老键没被拿掉）
        assert out.result["sentiment_metrics"]["event_count"] == 2
        # 约束解码的 schema 必须**含**逐只字段（否则 1.5B 根本输出不出来）
        assert "per_subject" in gw.calls[0]["json_schema"]["properties"]
        assert "逐标的" in gw.calls[0]["prompt"]

    @pytest.mark.asyncio
    async def test_missing_or_bogus_per_subject_degrades_to_unclear(self) -> None:
        """★ 模型漏写/写错时：逐只周期置「不明确」，**不许静默丢掉一只票**。"""
        gw = _StubGateway({"conclusion": "只看了一只", "confidence": "medium",
                           "sentiment_phase": "分歧", "narrative": "n",
                           "per_subject": [{"subject": "002142",
                                            "sentiment_phase": "看不懂"}]})
        out = await SentimentAgent(gw).execute(AgentInput(
            task_id="t-a07", tenant_id="tenant_001",
            payload={"events": _events_multi()}))
        assert out.result["sentiment_phase_by_group"] == {
            "002142": "不明确", "601088": "不明确"}

    @pytest.mark.asyncio
    async def test_per_code_phase_respects_the_consistency_guard(self) -> None:
        """逐只也走同一条一致性防线：情绪分为负却判「乐观」⇒ 降为不明确。"""
        gw = _StubGateway({"conclusion": "x", "confidence": "low",
                           "sentiment_phase": "分歧", "narrative": "n",
                           "per_subject": [
                               {"subject": "601088", "sentiment_phase": "乐观",
                                "conclusion": "?"}]})
        out = await SentimentAgent(gw).execute(AgentInput(
            task_id="t-a07", tenant_id="tenant_001",
            payload={"events": _events_multi()}))
        assert out.result["sentiment_phase_by_group"]["601088"] == "不明确"
