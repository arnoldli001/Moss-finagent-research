"""信息层三Agent测试（A05去伪/A06提取/A07舆情，FakeGateway注入，不联网）。"""

import json

import pytest

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput
from src.domain.agents.info import ExtractorAgent, SentimentAgent, VerifierAgent
from src.domain.agents.info.sentiment.logic import compute_sentiment_metrics
from src.domain.agents.info.verifier.logic import score_item
from src.orchestration.supervisor import INFO_AGENTS, plan_run


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


def _make_input(payload: dict, task_id: str = "t_info") -> AgentInput:
    return AgentInput(task_id=task_id, tenant_id="tenant_001", payload=payload)


# ---------- A05 本地规则评分 ----------

def test_score_item_official_source_high():
    s = score_item({"source_name": "国家统计局", "publish_time": "2026-09-10T09:00:00+08:00",
                    "text": "8月CPI同比上涨0.4%，涨幅比上月扩大0.1个百分点"})
    assert s["source_score"] == 1.0
    assert s["recency_factor"] == 1.0  # 两天内
    assert s["rule_score"] >= 0.95     # 官方+新近+含数据


def test_score_item_guba_sensational_low():
    s = score_item({"source_name": "股吧", "publish_time": "2026-09-11T20:00:00+08:00",
                    "text": "内幕消息！必涨翻倍，满仓干，稳赚不赔，速看！"})
    assert s["source_score"] == 0.35
    assert s["text_adjustment"] == -0.40  # 5个夸张词触顶
    assert s["rule_score"] <= 0.1


def test_score_item_unknown_recency_penalty():
    s = score_item({"source_name": "某博客", "text": "普通内容"})
    assert s["source_score"] == 0.5
    assert s["recency_factor"] == 0.95  # 无发布时间轻微降权


def test_score_item_old_news_decayed():
    s = score_item({"source_name": "新浪财经", "publish_time": "2020-01-01T00:00:00+08:00",
                    "text": "旧闻内容"})
    assert s["recency_factor"] == 0.30


# ---------- A05 Agent ----------

VERIFIER_REPLY = {"reviews": [
    {"item_id": "info_1", "verdict": "可信", "red_flags": [], "reasoning": "官方数据"},
    {"item_id": "info_2", "verdict": "不可信", "red_flags": ["内幕消息无法证实"],
     "reasoning": "来源不可靠"},
]}


async def test_verifier_happy_path():
    gw = FakeGateway(VERIFIER_REPLY)
    out = await VerifierAgent(gw).execute(_make_input({"info_items": [
        {"source_name": "国家统计局", "publish_time": "2026-09-10T09:00:00+08:00",
         "text": "8月CPI同比上涨0.4%"},
        {"source_name": "股吧", "text": "内幕消息，必涨翻倍！"},
    ]}))

    assert out.agent_id == "A05_verifier"
    assert out.result["stats"]["verified"] == 1
    assert out.result["stats"]["rejected"] == 1
    items = out.result["items"]
    assert items[0]["verified"] is True and items[0]["verdict"] == "可信"
    assert items[1]["verified"] is False  # 股吧+夸张词+LLM拒绝
    assert "内幕消息无法证实" in items[1]["red_flags"]
    call = gw.calls[0]
    assert call["task_tier"] == "medium" and call["json_mode"] is True


async def test_verifier_empty_skips_llm():
    gw = FakeGateway(VERIFIER_REPLY)
    out = await VerifierAgent(gw).execute(_make_input({"info_items": []}))
    assert out.confidence.value == "low"
    assert gw.calls == []


async def test_verifier_low_rule_score_rejected_even_if_llm_says_ok():
    """规则分过低时即使LLM说可信也拒绝（分数权威性在本地规则）。"""
    reply = {"reviews": [{"item_id": "info_1", "verdict": "可信",
                          "red_flags": [], "reasoning": ""}]}
    out = await VerifierAgent(FakeGateway(reply)).execute(_make_input({"info_items": [
        {"source_name": "微博", "text": "梭哈！梭哈！梭哈！梭哈！梭哈！梭哈！"},
    ]}))
    assert out.result["items"][0]["verified"] is False


async def test_verifier_invalid_payload_raises():
    with pytest.raises(AgentExecutionError, match="不合法"):
        await VerifierAgent(FakeGateway(VERIFIER_REPLY)).execute(_make_input(
            {"info_items": "not-a-list"}
        ))


# ---------- A06 Agent ----------

EXTRACTOR_REPLY = {"events": [
    {"item_id": "info_1", "event_type": "policy", "subject": "中国宏观",
     "direction": "positive", "magnitude": "通胀温和", "event_date": "2026-09-09",
     "evidence_quote": "CPI同比上涨0.4%", "confidence": 0.9},
    {"item_id": "info_1", "event_type": "earnings", "subject": "某公司",
     "direction": "利好", "magnitude": "", "event_date": "",
     "evidence_quote": "业绩超预期", "confidence": "0.7"},  # 方向越界+字符串置信度
    {"item_id": "info_999", "event_type": "other", "subject": "幽灵",
     "direction": "neutral", "magnitude": "", "event_date": "",
     "evidence_quote": "编造", "confidence": 0.9},  # 不可溯源
]}


async def test_extractor_normalizes_and_filters():
    gw = FakeGateway(EXTRACTOR_REPLY)
    out = await ExtractorAgent(gw).execute(_make_input({"info_items": [
        {"item_id": "info_1", "source_name": "新浪财经", "text": "CPI同比上涨0.4%"},
    ]}))

    events = out.result["events"]
    assert len(events) == 2  # info_999不可溯源被丢弃
    assert events[0]["direction"] == "positive"
    assert events[1]["direction"] == "positive"    # 中文"利好"归一化
    assert events[1]["confidence"] == 0.7          # 字符串置信度转float
    assert out.result["stats"]["total"] == 2
    call = gw.calls[0]
    assert call["task_tier"] == "medium"
    assert "逐字来自输入" in call["prompt"]


async def test_extractor_direction_chinese_normalized():
    """中文方向'利好'应归一化为positive。"""
    from src.domain.agents.info.models import normalize_direction

    assert normalize_direction("利好") == "positive"
    assert normalize_direction("利空/负面") == "negative"
    assert normalize_direction("neutral") == "neutral"
    assert normalize_direction("") == "neutral"


async def test_extractor_unknown_event_type_maps_to_other():
    from src.domain.agents.info.models import normalize_event_type

    assert normalize_event_type("merger") == "merger"
    assert normalize_event_type("随便什么") == "other"


async def test_extractor_empty_skips_llm():
    gw = FakeGateway(EXTRACTOR_REPLY)
    out = await ExtractorAgent(gw).execute(_make_input({"info_items": []}))
    assert out.confidence.value == "low"
    assert gw.calls == []


async def test_extractor_bad_json_raises():
    class BadGateway:
        async def complete(self, *a, **k):
            from src.infrastructure.llm.models import LLMResponse

            return LLMResponse(content="不是JSON", model_used="m", provider="p")

    with pytest.raises(AgentExecutionError, match="非合法JSON"):
        await ExtractorAgent(BadGateway()).execute(_make_input(
            {"info_items": [{"item_id": "info_1", "text": "x"}]}
        ))


# ---------- A07 本地情绪指标 ----------

def test_sentiment_metrics_weighted():
    events = [
        {"direction": "positive", "confidence": 0.8, "subject": "A", "item_id": "i1"},
        {"direction": "negative", "confidence": 0.4, "subject": "B", "item_id": "i2"},
    ]
    m = compute_sentiment_metrics(events)
    assert m["weighted_sentiment"] == round((0.8 - 0.4) / 1.2, 3)
    assert m["distribution"] == {"positive": 1, "negative": 1}
    assert m["top_subjects"][0]["subject"] == "A"


def test_sentiment_metrics_empty():
    m = compute_sentiment_metrics([])
    assert m["weighted_sentiment"] == 0.0 and m["event_count"] == 0


# ---------- A07 Agent ----------

SENTIMENT_REPLY = {"conclusion": "加权情绪分0.36偏多，事件分布以利好为主",
                   "confidence": "medium", "sentiment_phase": "乐观",
                   "narrative": "官方数据驱动情绪回暖"}


async def test_sentiment_happy_path():
    gw = FakeGateway(SENTIMENT_REPLY)
    out = await SentimentAgent(gw).execute(_make_input({"events": [
        {"item_id": "info_1", "direction": "positive", "confidence": 0.9,
         "subject": "宏观", "event_type": "policy", "evidence_quote": "CPI上涨"},
    ]}))

    assert out.agent_id == "A07_sentiment"
    assert out.result["sentiment_phase"] == "乐观"
    assert out.result["sentiment_metrics"]["weighted_sentiment"] == 1.0  # 单事件方向即情绪
    assert out.result["narrative"] == "官方数据驱动情绪回暖"
    call = gw.calls[0]
    assert call["task_tier"] == "reasoning"
    assert "0.9" in call["prompt"]  # 本地指标注入prompt


async def test_sentiment_phase_contradiction_guard():
    """强空情绪不得判乐观（LLM自相矛盾时降级为不明确）。"""
    reply = {**SENTIMENT_REPLY, "sentiment_phase": "乐观"}
    out = await SentimentAgent(FakeGateway(reply)).execute(_make_input({"events": [
        {"item_id": "info_1", "direction": "negative", "confidence": 0.9,
         "subject": "X", "event_type": "litigation", "evidence_quote": "被立案"},
    ]}))
    assert out.result["sentiment_phase"] == "不明确"
    assert "矛盾" in out.conclusion


async def test_sentiment_unknown_phase_defaults():
    reply = {**SENTIMENT_REPLY, "sentiment_phase": "狂热"}  # 越界档位
    out = await SentimentAgent(FakeGateway(reply)).execute(_make_input({"events": [
        {"item_id": "i1", "direction": "positive", "confidence": 0.6,
         "subject": "Y", "event_type": "product", "evidence_quote": "发布新品"},
    ]}))
    assert out.result["sentiment_phase"] == "不明确"


async def test_sentiment_empty_skips_llm():
    gw = FakeGateway(SENTIMENT_REPLY)
    out = await SentimentAgent(gw).execute(_make_input({"events": []}))
    assert out.confidence.value == "low"
    assert gw.calls == []


# ---------- Supervisor规划路由 ----------

def test_plan_news_type_routes_info_agents():
    plan = plan_run("news", "")
    assert plan["indicators"] == []
    assert set(INFO_AGENTS) <= set(plan["agents"])
    assert "A08_macro" not in plan["agents"]
    assert "A17_recommend" in plan["agents"] and "A18_audit" in plan["agents"]


def test_plan_info_items_appends_info_layer():
    plan = plan_run("macro", "", [{"text": "某新闻"}])
    assert set(INFO_AGENTS) <= set(plan["agents"])
    assert "A08_macro" in plan["agents"]


def test_plan_no_info_items_no_info_layer():
    plan = plan_run("macro", "")
    assert not set(INFO_AGENTS) & set(plan["agents"])
