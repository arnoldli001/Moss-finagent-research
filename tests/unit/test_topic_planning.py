"""主题关键词提取与plan_run对宏观/行业问句的规划（问答非所问修复核心路由）。"""

from src.orchestration.supervisor import (
    INFO_AGENTS,
    _verified_texts,
    extract_topic_keywords,
    plan_run,
)

MACRO_QUESTION = "美股未来9月和10月的加息概率分别有多大？"
AI_QUESTION = "当前AI产业链，哪些细分环节还有预期差，相对有投资价值洼地？"


def test_macro_question_keywords():
    kws = extract_topic_keywords("macro", "", MACRO_QUESTION)
    # 命中问法中的词，并补充美联储核心词以提高召回
    assert "美股" in kws and "加息" in kws and "美联储" in kws


def test_macro_plan_includes_info_layer():
    plan = plan_run("macro", "", query=MACRO_QUESTION)
    assert all(a in plan["agents"] for a in INFO_AGENTS)
    assert plan["topic_keywords"]


def test_macro_unrelated_question_no_topic_news():
    # 与海外/流动性无关的宏观问句不拉主题新闻，信息层仍不规划（保持空跳过）
    plan = plan_run("macro", "", query="今天天气怎么样")
    assert plan["topic_keywords"] == []
    assert not any(a in plan["agents"] for a in INFO_AGENTS)


def test_industry_question_routed_by_query_without_target():
    # target为空、问题写在query里（前端常见）：仍路由A13并放开细分环节关键词
    kws = extract_topic_keywords("industry", "", AI_QUESTION)
    assert {"AI", "光模块", "CPO", "液冷"} <= set(kws)

    plan = plan_run("industry", "", query=AI_QUESTION)
    assert "A13_tech" in plan["agents"]
    assert all(a in plan["agents"] for a in INFO_AGENTS)
    # 科技专业产业指标随路由下发
    assert any(i.startswith("ind:") for i in plan["indicators"])


def test_industry_unrelated_question_no_topic_news():
    plan = plan_run("industry", "", query="今天吃什么")
    assert plan["topic_keywords"] == []
    assert "A13_tech" not in plan["agents"]


def test_verified_texts_fallback_mapping():
    """A05输出items中verified=True的原文才能进入分析层兜底，被拒条目不进入。"""
    state = {
        "verified_items": {"items": [
            {"item_id": "info_1", "verified": True, "title": "美联储加息预期升温",
             "text": "短端利率上行", "source_name": "东方财富-全球财经",
             "publish_time": "2026-09-10"},
            {"item_id": "info_2", "verified": False, "title": "无关广告", "text": "xx"},
        ]},
        "info_items": [],
    }
    texts = _verified_texts(state)
    assert len(texts) == 1
    assert "美联储加息预期升温" in texts[0] and "东方财富-全球财经" in texts[0]
    assert _verified_texts({"verified_items": {}, "info_items": []}) == []
