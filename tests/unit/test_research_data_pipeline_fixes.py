"""投研数据链路修复的回归测试（2026-09-26 实测故障）。

三个真实故障，全部由一位用户的综合问题暴露：
「当前宏观环境如何？对A股有什么含义？美国说年内还会再加息一次，
  请对当前A股AI产业链各细分行业龙头的估值及10月走势走分析预测。」

1. **指标契约违规 → "估值数据缺失"**
   LLM 规划器返回裸个股指标 ``"PE(TTM)"`` 且 ``target=""``。
   个股指标必须拼 6 位代码，裸名字**没有任何连接器 supports**，
   A01 瞬间 DataFetchError → 用户看到"估值无任何输入数据 /
   valuation_calc=数据不足"。而本地库里躺着 82 只个股的 PE（含最新交易日）。

2. **主题关键词饥饿 → 信息层整体空跳过**
   问题里写的是"再加息"（没有"美联储"字样），旧的精确词匹配命中为空，
   且 analysis_type="macro" 时 industry 关键词组**根本没被评估** ——
   最终 ``topic_keywords=[]``，collect 节点一个新闻都不拉，
   A05/A06/A07 拿到零输入全部空跳过。

3. **必然失败的外网调用拖慢整条采集**
   ``fed:rate_prob:next`` 依赖 CME/FRED 外网，不可达时等满 25s 硬超时。
   A01 用 asyncio.gather 并发，整段采集墙钟被它拖到 23.5s（实测），
   而且**每次都重新等满**（无失败记忆）。
"""

from __future__ import annotations

import time

from src.infrastructure.connectors.source_cooldown import (
    DEFAULT_COOLDOWN_SEC,
    SourceCooldown,
)
from src.orchestration.supervisor import (
    extract_topic_keywords,
    plan_run,
    sanitize_indicators,
)

#: 用户原问题（三个故障都由它暴露）
USER_QUERY = (
    "当前宏观环境如何？对A股有什么含义？美国说年内还会再加息一次，"
    "请对当前A股AI产业链各细分行业龙头的估值及10月走势走分析预测。"
)


# ---------------------------------------------------------------- 故障 1

def test_bare_stock_indicator_without_target_is_dropped_and_replaced():
    """裸个股指标 + 无标的 → 丢弃并换成行业估值截面（不是静默丢空）。"""
    planner_out = ["us_fed_rate", "stock_close", "PE(TTM)", "PB",
                   "资产负债率", "流动比率"]
    usable, dropped, notes = sanitize_indicators(
        planner_out, target="", analysis_type="macro")

    # 裸个股指标一个都不能留（留了就是必然失败的采集）
    for bad in ("stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率"):
        assert bad not in usable, f"{bad} 必须被丢弃（无连接器 supports 裸名字）"
    assert set(dropped) == {"stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率"}
    # 必须补上行业估值，否则分析层仍然没有估值素材
    assert "ind:sw_third_pe_ttm:all" in usable
    assert "ind:sw_third_pb:all" in usable
    assert notes, "必须留下可审计的修正说明"
    # 非个股指标要保留
    assert "us_fed_rate" in usable


def test_stock_indicator_with_target_gets_code_suffix():
    """有 6 位标的代码时，个股指标必须补上后缀（而不是丢弃）。"""
    usable, dropped, notes = sanitize_indicators(
        ["stock_close", "PE(TTM)", "PB"], target="300308",
        analysis_type="stock")
    assert usable == ["stock_close:300308", "PE(TTM):300308", "PB:300308"]
    assert dropped == []
    assert any("300308" in n for n in notes)


def test_already_suffixed_indicators_are_untouched():
    """已带后缀的指标不应被二次改写。"""
    usable, dropped, _ = sanitize_indicators(
        ["PE(TTM):600519", "PB:600519", "CPI"],
        target="300308", analysis_type="stock")
    assert usable == ["PE(TTM):600519", "PB:600519", "CPI"]
    assert dropped == []


def test_sanitize_is_noop_for_pure_macro_plan():
    """纯宏观规划不应被改动（避免修复引入副作用）。"""
    inds = ["CPI", "PPI", "us_fed_rate", "mkt:turnover:total"]
    usable, dropped, notes = sanitize_indicators(inds, "", "macro")
    assert usable == inds
    assert dropped == [] and notes == []


# ---------------------------------------------------------------- 故障 2

def test_news_keywords_found_for_user_query():
    """用户原问题必须能抽出主题关键词（否则新闻一个都不拉）。"""
    kws = extract_topic_keywords("macro", "", USER_QUERY)
    assert kws, "关键词为空 → collect 节点不拉任何新闻 → 信息层整体空跳过"
    # "再加息" 必须能命中加息组（旧实现按精确词匹配，这里只要子串命中）
    assert "加息" in kws
    assert "美联储" in kws, "命中加息相关词后应补充核心词提高召回"


def test_full_type_also_extracts_industry_keywords():
    """"综合问题"（full/stock）不能只按 analysis_type 取词。

    实测故障：LLM 把综合问题定成 analysis_type="macro"，
    旧的 if/elif 于是只走 macro 分支，industry 关键词组根本没被评估。
    """
    kws = extract_topic_keywords("full", "", USER_QUERY)
    assert "加息" in kws, "宏观侧关键词缺失"
    assert any(k in kws for k in ("AI", "人工智能", "算力", "半导体")), (
        f"行业侧关键词缺失（AI产业链没被路由到科技组）：{kws[:15]}")


def test_industry_keywords_still_work_for_industry_type():
    """industry 类型的行为不能被修复破坏。"""
    kws = extract_topic_keywords("industry", "半导体", "半导体行业景气度如何")
    assert kws and any(k in kws for k in ("半导体", "芯片", "算力"))


def test_unrelated_query_yields_no_keywords():
    """无关问题不应抽到关键词（否则会拉一堆无关新闻浪费 token）。"""
    assert extract_topic_keywords("macro", "", "今天天气怎么样") == []


def test_plan_run_includes_news_agents_for_user_query():
    """规划应带上信息层 Agent（因为抽到了主题关键词）。"""
    plan = plan_run("full", "", query=USER_QUERY)
    assert plan.get("topic_keywords"), "规划里应带 topic_keywords"
    for aid in ("A05_verifier", "A06_extractor", "A07_sentiment"):
        assert aid in plan["agents"], f"{aid} 应参与（有主题关键词就有新闻可拉）"


# ---------------------------------------------------------------- 故障 3

def test_source_cooldown_blocks_repeat_calls_within_window():
    """冷却窗口内不再放行（这是省掉 23.5s 重复等待的机制）。"""
    cd = SourceCooldown()
    assert cd.is_cooling("fed:x") is False
    cd.record_failure("fed:x")
    assert cd.is_cooling("fed:x") is True
    assert cd.remaining("fed:x") > 0


def test_source_cooldown_backs_off_exponentially_and_caps():
    """连续失败要退避并封顶（避免长期不通的源被无限探测）。"""
    cd = SourceCooldown()
    w1 = cd.record_failure("k", cooldown=10.0)
    w2 = cd.record_failure("k", cooldown=10.0)
    w3 = cd.record_failure("k", cooldown=10.0)
    assert w1 < w2 < w3, "应指数退避"
    for _ in range(50):
        w = cd.record_failure("k", cooldown=10.0)
    assert w <= 900.0, "退避必须封顶"


def test_source_cooldown_success_clears_it():
    """源恢复后必须立刻可用（不能被冷却继续冷落）。"""
    cd = SourceCooldown()
    cd.record_failure("k")
    assert cd.is_cooling("k") is True
    assert cd.record_success("k") is True
    assert cd.is_cooling("k") is False


def test_source_cooldown_is_per_key_not_global():
    """一个指标不通不应影响同源的其他指标（如腾讯有日线但外网不通）。"""
    cd = SourceCooldown()
    cd.record_failure("fedwatch:rate_prob")
    assert cd.is_cooling("fedwatch:rate_prob") is True
    assert cd.is_cooling("tencent:daily") is False


def test_fedwatch_fetch_skips_network_when_cooling(monkeypatch):
    """冷却中时 fetch 必须**不发网络请求**直接返回空。"""
    from src.infrastructure.connectors import fedwatch_connector as F

    cd = F.get_cooldown()
    cd.clear()
    cd.record_failure(F._COOLDOWN_KEY)

    called = {"n": 0}

    def _boom(_arg: str):
        called["n"] += 1
        raise AssertionError("冷却中不该真的调用外网库")

    monkeypatch.setattr(F.FedWatchConnector, "_call_lib", staticmethod(_boom))

    import asyncio

    t0 = time.perf_counter()
    pts = asyncio.run(F.FedWatchConnector().fetch("fed:rate_prob:next"))
    dt = time.perf_counter() - t0
    assert pts == []
    assert called["n"] == 0, "冷却中仍然调了外网"
    assert dt < 0.5, f"冷却中应瞬时返回，实际 {dt:.2f}s"
    cd.clear()


def test_cooldown_default_window_is_reasonable():
    """冷却窗口要有意义：太短省不下重复等待，太长会冷落刚恢复的源。"""
    assert 30.0 <= DEFAULT_COOLDOWN_SEC <= 600.0
