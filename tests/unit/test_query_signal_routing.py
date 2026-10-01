"""★★★ 复合问的领域信号路由（2026-09-28 第二十三轮）。

## 这个文件守的那个 bug

用户问（真实报障原文）：

> 「当前宏观环境如何，预测下一年美国的加息、降息节奏，**对A股的影响**，
>   以及AI应用加速失业率增加**对消费的影响**节奏时间节点分析，
>   **未来半年能否持有高股息的招商银行**？」

**四个子问题**。规划器把它判成单一 `analysis_type = "macro"`，
而 `supervisor_node` 紧接着执行：

    if llm_plan["analysis_type"] == "macro":
        planned_agents = [a for a in planned_agents if a not in industry_agents]

于是计划里只剩 `A08_macro` + A17/A18。实测后果（审计 + 缺口队列双重证据）：

    · 审计：那轮只有 2 次 LLM 调用（planning→dashscope、A17→deepseek），
      **A09-A16 一条记录都没有**；
    · `src/data/gap_queue.jsonl` 里 A17 报了 8 条缺口，逐条对应被漏掉的子问题：
        「美国联邦基金利率及目标区间（上游显示为?%）」
        「美国失业率、非农就业等就业口径」
        「招商银行个股财务明细、股息率、净息差与资产质量数据」
        「银行及高股息行业分项估值分位」

**四个子问题共用一份 CPI/PPI 数据**，然后交给 A17 一次性写完。
A17 的诚实（把缺口写进 `data_gaps`）是对的，缺的是上游没喂它东西。

## 修法：增补，而不是重写 `analysis_type`

`analysis_type` 是**单值**契约（下游 `_PLANNING` / `plan_run` / 前端都按它分支），
改成多值会牵动一条长链。所以：**保持规划器的判断，按问句真实出现的领域信号
把对应 Agent 与指标补回来**，并且增补发生在剥离**之后**（否则会被剥掉）。

## 判据纪律：宁可不补

每个信号都配了"不该命中"的反例测试 —— 误命中的代价是把无关 Agent
拉进链路（多跑几次 reasoning 层调用 = 多花几十秒），
或者把"大盘问"变成"个股问"。
"""

from __future__ import annotations

import pytest

from src.orchestration.supervisor import (
    _query_agent_signals,
    augment_plan_by_query_signals,
    query_needs_stock_resolution,
)

#: 报障原文（**逐字**保留，含全角问号）
REPORTED_QUERY = (
    "当前宏观环境如何，预测下一年美国的加息、降息节奏，对A股的影响，"
    "以及AI应用加速失业率增加对消费的影响节奏时间节点分析，"
    "未来半年能否持有高股息的招商银行？"
)


# ============================================================
# ① 报障现场：四个子问题都要有人管
# ============================================================


def test_reported_query_signals_all_four_domains():
    """★★★ 报障原文必须同时识别出：个股 / 消费 / 行业 / 高股息。

    少任何一个，就有一个子问题没有 Agent 负责 —— 而**不会有任何报错**。
    """
    signals = _query_agent_signals(REPORTED_QUERY, "")
    assert "stock" in signals, "「能否持有…招商银行」没被识别成个股问 → A10 不会入列"
    assert "consumer" in signals, "「对消费的影响」没被识别 → A14 不会入列"
    assert "dividend" in signals, "「高股息」没被识别 → 行业估值分位不会入列"


def test_reported_query_augments_agents_and_stock_indicators():
    """★ 增补后：A10/A11/A14/A09 入列，且个股指标**带上了 6 位代码**。

    带代码这一点是硬要求：`PE(TTM)` 这种裸名**没有任何连接器 supports**，
    A01 会瞬间 DataFetchError，用户看到"估值数据缺失"
    （2026-09-26 实测过的故障链，见 `sanitize_indicators` 的 docstring）。
    """
    agents, indicators, notes = augment_plan_by_query_signals(
        REPORTED_QUERY, "", ["A08_macro", "A17_recommend", "A18_audit"],
        ["CPI", "PPI"], resolved_code="600036",
    )
    for aid in ("A10_micro", "A11_fin_risk", "A14_consumer", "A09_meso"):
        assert aid in agents, f"{aid} 没被补进计划：{agents}"
    assert "A08_macro" in agents, "增补层**不许**删掉原有 Agent（只增不减）"

    assert "PE(TTM):600036" in indicators, f"个股 PE 没带上代码：{indicators}"
    assert "stock_close:600036" in indicators
    assert any(i.startswith("ind:sw_") for i in indicators), "行业估值分位未补"
    assert notes, "增补必须留下可读的原因（否则排查时看不见它为什么多跑了）"


def test_augmentation_never_removes_anything():
    """★ 只增不减：增补层不越权回滚规划层的剥离决定。"""
    agents, indicators, _ = augment_plan_by_query_signals(
        "美联储加息对A股影响", "", ["A08_macro"], ["CPI"],
    )
    assert "A08_macro" in agents
    assert "CPI" in indicators


# ============================================================
# ② 误命中守卫（每个信号一条反例）
# ============================================================


@pytest.mark.parametrize("query", [
    "美联储年内还会加息吗",
    "美国CPI数据怎么看",
    "当前宏观环境如何",
    "美债收益率上行对全球流动性的影响",
])
def test_macro_queries_do_not_trigger_stock_signal(query: str):
    """★ 纯宏观问**不许**被判成个股问。

    误命中的代价：把 A10（微观）+ A11（财务风险）拉进一条宏观链路 ——
    两次 reasoning 层云端调用（每次十几秒），而它们**无数据可分析**
    （没有标的，取不到 PE/财务比率），只会给 A17 多两条"数据不足"。
    """
    assert not query_needs_stock_resolution(query, ""), (
        f"『{query}』被误判成需要解析个股标的"
    )
    assert "stock" not in _query_agent_signals(query, "")


def test_broad_market_holding_question_is_not_a_stock_question():
    """★ 边界：『大盘未来半年能不能持有』**不是**个股问。

    它命中了"持有"，但对象是**大盘**不是个股。
    这是本判据最容易被写坏的一格 —— 所以 `_HOLDING_INTENT_WORDS`
    刻意**不收录**"值得买"/"能不能买"这类口语动词短语
    （"大盘值得买吗"会命中，且没有任何办法区分）。
    """
    assert not query_needs_stock_resolution("大盘未来半年能不能持有", "")
    assert "stock" not in _query_agent_signals("大盘未来半年能不能持有", "")


@pytest.mark.parametrize("query", [
    "半导体产业链龙头估值如何",
    "煤炭板块怎么看",
    "医药行业有什么机会",
])
def test_industry_queries_route_to_the_named_industry(query: str):
    """★ 具体行业名要路由到对应行业 Agent（这是行业层存在的意义）。"""
    signals = _query_agent_signals(query, "")
    assert "industry" in signals, f"『{query}』没识别出行业信号"


@pytest.mark.parametrize("query", [
    "美联储加息对A股的影响",   # 含"影响"但不含具体行业名
    "当前宏观环境如何",
])
def test_generic_words_do_not_trigger_industry_signal(query: str):
    """★★ 宏观问句里出现通用词（"行业/板块/赛道"）**不许**补行业 Agent。

    为什么这条很重要：`supervisor_node` 对 macro **刻意剥离** A13-A16
    （注释原文："纯宏观/个股问题跑 4 路行业 Agent 是浪费（4 次
    reasoning-tier 调用）"）。若把"行业/板块"当判据，
    等于每次宏观问都多跑 4 个 Agent，把那次优化整个撤销。
    """
    assert "industry" not in _query_agent_signals(query, "")


# ============================================================
# ③ 无代码时的诚实降级
# ============================================================


def test_stock_signal_without_code_does_not_emit_bare_indicator():
    """★ 识别出个股信号但**拿不到代码** → 不许把裸指标留在计划里。

    裸名没有任何连接器 supports：留下它 = A01 白撞一次网络 + 必然失败 +
    用户看到"估值数据缺失"（**看起来像数据源坏了，其实是契约不满足**）。

    正确处置：摘掉指标，把"没有个股代码"如实留给 A17 写进 `data_gaps`。
    """
    agents, indicators, notes = augment_plan_by_query_signals(
        "未来半年能否持有高股息的招商银行？", "",
        ["A08_macro"], ["CPI"], resolved_code="",   # ← 没有代码
    )
    assert "A10_micro" in agents, "仍应补挂 A10（让它如实报告缺口）"
    bare = [i for i in indicators if i in ("PE(TTM)", "PB", "stock_close")]
    assert not bare, f"裸个股指标被留在计划里（必然 fetch 失败）：{bare}"


def test_has_code_in_query_is_detected_without_name_table():
    """问句里**直接写了 6 位代码**时不必查名称表（判据最硬，零成本）。"""
    assert query_needs_stock_resolution("请分析 600036 的估值", "")
    signals = _query_agent_signals("请分析 600036 的估值", "")
    assert "stock" in signals


def test_augmentation_is_idempotent():
    """★ 幂等：重复增补不产生重复项（graph 重跑 / 规则兜底也会调它）。"""
    once_agents, once_inds, _ = augment_plan_by_query_signals(
        REPORTED_QUERY, "", ["A08_macro"], ["CPI"], resolved_code="600036")
    twice_agents, twice_inds, _ = augment_plan_by_query_signals(
        REPORTED_QUERY, "", once_agents, once_inds, resolved_code="600036")
    assert twice_agents == once_agents, "Agent 列表出现重复"
    assert twice_inds == once_inds, "指标列表出现重复"
