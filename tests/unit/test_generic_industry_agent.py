"""★★★ 兜底行业 Agent（A20）护栏：**银行这类"没人管"的行业必须有人给结论**。

## 报障现场（用户 2026-09-29 原话）

> 「600036（招商银行属银行，**非本行业消费**）在数据缺口下无法给出明确持有结论：
>   **缺少银行 agent 吗？那就生成一个负责其他产业的 agent。做个兜底行业的 agent，
>   支持根据输入内容，动态注册对应行业 agent**」

修前实测（真实 LLM 端到端）：一条问"高股息招商银行"的复合问落到 **A14（消费）**
手里，它的结论第一句就是「600036（招商银行）**非本框架（消费）覆盖标的**」——
数据全在，但**没有任何 Agent 该管银行**。

## 本文件守的四件事

1. **行业解析是确定性的**：`600036 → 银行`（本地名录），
   而不是靠"公司名里有没有行业字样"（`贵州茅台` 里没有"白酒"）；
2. **路由判据正确**：无专属 Agent 覆盖的行业才挂 A20；
   有覆盖的（白酒→A14、半导体→A13）**不许**挂 —— 否则每个行业问都会多跑一次推理；
3. **拿不到行业就不猜**：返回空串，宁可"没有行业结论"也不给一个错的行业框架；
4. **不许静默跳过 LLM**：银行这类"只有申万截面、没有专属产业时序指标"的行业，
   必须**算作有输入**（`watched_indicator_count > 0`）——
   否则会走 `_skip_reason()` 输出「无本行业关注指标…跳过 LLM」，
   **那正是本次报障的形态**。
"""

from __future__ import annotations

import pytest

from src.domain.agents.analysis.base import AnalysisPayload
from src.domain.agents.industry.base import IndustryAgentBase
from src.domain.agents.industry.generic.agent import GenericIndustryAgent
from src.orchestration.supervisor import (
    _AGENT_DATA_WHITELIST,
    INDUSTRY_AGENTS,
    INDUSTRY_INDICATORS,
    augment_plan_by_query_signals,
    needs_generic_industry,
    route_industry,
)

#: 用户那条原始复合问（验收语料，**不得改写**）
_ACCEPTANCE_QUERY = (
    "当前宏观环境如何，预测下未来一年美国的加息节奏，对A股的影响，"
    "以及AI应用加速失业率增加对消费的影响节奏时间节点分析，"
    "未来半年能否持有高股息的招商银行？"
)


def _agent() -> GenericIndustryAgent:
    """不需要 gateway 的实例（本文件只测行业解析与信号，不调 LLM）。"""
    return GenericIndustryAgent.__new__(GenericIndustryAgent)


def _payload(**kw) -> AnalysisPayload:
    return AnalysisPayload(**kw)


# ============================================================
# ① 行业解析：确定性优先，且**不猜**
# ============================================================


def test_bank_stock_resolves_to_bank_industry():
    """★ 600036 → 银行（本地名录）。这是整条兜底链路的**起点**。"""
    industry, how = _agent().resolve_industry(
        _payload(focus="600036", user_query="未来半年能否持有招商银行？"))
    assert industry == "银行", f"600036 应解析为「银行」，实际 {industry!r}"
    assert how, "必须给出**依据说明**（用户要能判这个行业是怎么来的）"


def test_stock_short_name_resolves_without_industry_word_in_name():
    """★ `贵州茅台` 里**没有**"白酒"二字 —— 必须靠"简称→代码→名录"这一路。

    只做"文本里找行业名"的实现会在这里失败（或误判），
    而它是本项目最常见的标的之一。
    """
    industry, _how = _agent().resolve_industry(
        _payload(focus="", user_query="贵州茅台未来走势如何？"))
    assert industry == "白酒", f"贵州茅台 应解析为「白酒」，实际 {industry!r}"


def test_unresolvable_text_returns_empty_never_guesses():
    """★★ 解析不出就返回空 —— **绝不猜行业**。

    猜一个行业的代价是"用一个错误的框架分析"（如把银行当消费），
    比"没有行业结论"更糟，且**不报错**。
    """
    for text in ("今天天气怎么样", "帮我写一首诗", ""):
        industry, how = _agent().resolve_industry(
            _payload(focus="", user_query=text))
        assert industry == "", f"{text!r} 不该解析出行业，实际 {industry!r}"
        assert how == ""


# ============================================================
# ② 路由判据：该挂才挂，不该挂不许挂
# ============================================================


@pytest.mark.parametrize(("text", "code", "expect"), [
    # 银行：无专属 Agent → 必须兜底
    ("未来半年能否持有高股息的招商银行？", "600036", "银行"),
    ("平安银行(000001)怎么样", "000001", "银行"),
    # 白酒 → A14（消费）已覆盖，**不许**再挂兜底
    ("贵州茅台未来走势", "600519", ""),
    # 半导体 → A13（科技）已覆盖
    ("半导体行业景气度如何", "", ""),
    # 解析不出行业 → 不挂
    ("今天天气怎么样", "", ""),
])
def test_routing_decides_when_fallback_is_needed(text, code, expect):
    """★★ 兜底路由的判据（**不是"总有兜底"**）。

    多挂的代价不是错误，而是**每个行业问都白跑一次 reasoning 调用**；
    少挂的代价是用户看到「非本行业覆盖，无法给出结论」。
    两个方向都必须钉住。
    """
    assert needs_generic_industry(text, code) == expect


def test_generic_agent_is_declared_but_not_keyword_routed():
    """★ A20 必须在 `INDUSTRY_AGENTS` 里，但**不在** `INDUSTRY_KEYWORDS` 里。

    为什么必须进 `INDUSTRY_AGENTS`：宏观问法要能把它一起剥掉
    （否则白跑一次推理）；行业问法要能按"兜底解析"保留它。
    为什么不能进 `INDUSTRY_KEYWORDS`：兜底 Agent 没有关键词 ——
    放进去会让任何文本都命中它（关键词匹配是"包含"语义，空串也命中）。
    """
    assert "A20_generic_industry" in INDUSTRY_AGENTS
    assert route_industry("银行") == [], "「银行」不该被关键词路由命中"
    assert "A20_generic_industry" not in route_industry("银行")


def test_acceptance_query_attaches_the_fallback_agent():
    """★★★ 用户那条原始问句必须挂上兜底 Agent（端到端判据的最上游一环）。"""
    agents, _ind, notes = augment_plan_by_query_signals(
        _ACCEPTANCE_QUERY, "", ["A08_macro", "A09_meso"], ["CPI"],
        resolved_code="600036")
    assert "A20_generic_industry" in agents, (
        f"问句含「招商银行」（银行）却没挂兜底行业 Agent：agents={agents}"
    )
    assert any("兜底" in n for n in notes), (
        f"补挂必须留痕（用户要能看出为什么多了一个 Agent）：notes={notes}"
    )


# ============================================================
# ③ ★ 不许静默跳过 LLM（本次报障的形态）
# ============================================================


def test_bank_industry_is_not_skipped_for_lack_of_watch_keywords():
    """★★★ 银行：**只有申万截面**、没有专属产业时序指标 —— 也不许跳过 LLM。

    基类 `_skip_reason()` 在"关注指标 0 命中 + 无事件 + 无申万 + 无渗透率"时
    返回跳过理由，`execute()` 直接输出
    「采集数据中无{行业}行业关注指标（关注：…）且无相关可信事件，跳过LLM定性」
    —— **这正是用户报障的那句话**。

    银行的本地行业级数据就是申万截面里那一行
    （`ind:sw_first_pe_ttm:all` 且 `extra.industry_name == '银行'`）。
    本判据要求：**它必须被算作关注指标**，于是 `watched_indicator_count > 0`，
    于是 LLM 真的被调用、银行有专门的行业结论。
    """
    bank_row = {
        "indicator": "ind:sw_first_pe_ttm:all", "value": 7.27,
        "period_date": "2026-09-24", "source_name": "AKShare申万行业估值",
        "confidence": 0.9,
        "extra": {"industry_name": "银行", "industry_level": "first",
                  "metric": "pe_ttm"},
    }
    other_row = {
        "indicator": "ind:sw_first_pe_ttm:all", "value": 114.31,
        "period_date": "2026-09-22", "source_name": "AKShare申万行业估值",
        "confidence": 0.9,
        "extra": {"industry_name": "半导体", "industry_level": "first",
                  "metric": "pe_ttm"},
    }
    agent = _agent()
    payload = _payload(focus="600036", user_query="未来半年能否持有招商银行？",
                       data_points=[bank_row, other_row])
    watched = agent._watched_points(payload)  # noqa: SLF001
    assert bank_row in watched, (
        "申万截面里 `extra.industry_name == '银行'` 的那一行**必须**算作关注指标 —— "
        "否则 `_skip_reason()` 会判「无本行业关注指标」并静默跳过 LLM"
    )
    agent._prepare(payload)  # noqa: SLF001
    assert payload.hint["industry_signal"]["watched_indicator_count"] > 0
    assert agent._skip_reason(payload) is None, (  # noqa: SLF001
        "银行有申万截面数据却仍被判「该跳过 LLM」—— 那就是本次报障的形态"
    )


def test_industry_name_and_keywords_are_per_request_not_on_self():
    """★★ 行业名与关注词必须**按请求解析**，不许写在 `self` 上。

    为什么（实测风险）：`runtime.agents` 里的 Agent 是**单例**，
    一次进程服务任意行业。把行业名写到 `self` 上 → 并发请求互相覆盖，
    症状是"结论里写的是另一个行业"，**比报错难查得多**。
    """
    agent = _agent()
    bank = _payload(focus="600036", user_query="招商银行")
    moutai = _payload(focus="600519", user_query="贵州茅台")
    assert agent._industry_name_for(bank) == "银行"       # noqa: SLF001
    assert agent._industry_name_for(moutai) == "白酒"     # noqa: SLF001
    # 同一实例连续两次不同行业，互不影响（若写成 self.xxx 这里必红）
    assert agent._industry_name_for(bank) == "银行"       # noqa: SLF001
    assert not hasattr(agent, "industry_name_instance"), (
        "不许把按请求解析的行业名挂到实例上"
    )


def test_dynamic_industry_flag_is_declared():
    """★ A20 必须声明 `dynamic_industry` —— 护栏据此走动态分支而不是判它缺声明。"""
    assert GenericIndustryAgent.dynamic_industry is True
    assert IndustryAgentBase.dynamic_industry is False, (
        "基类默认必须是 False（A13-A16 写死行业名与关注词，那是正确设计）"
    )


# ============================================================
# ④ 白名单与产业指标：兜底 Agent 的可见性与"不写死"
# ============================================================


def test_fallback_agent_whitelist_is_family_prefixes_only():
    """★ A20 的白名单只给族前缀，**不给个股域**。

    为什么刻意不给 `PE(TTM)`/`stock_close`：那是 A10（个股）的域。
    行业 Agent 看行业截面；把单只票的行情混进行业结论，
    会让用户分不清哪句是行业、哪句是个股。
    """
    kws = _AGENT_DATA_WHITELIST["A20_generic_industry"]
    assert "ind:" in kws, "申万截面 + 产业指标 + 渗透率都在 ind: 下，必须放行"
    assert not any(k.startswith(("PE(", "stock_close")) for k in kws), (
        f"兜底行业 Agent 不该拿个股行情/估值域：{kws}"
    )


def test_fallback_agent_has_no_hardcoded_industry_indicators():
    """★ `INDUSTRY_INDICATORS["A20"]` 必须为空 —— 写死就等于只覆盖一个行业。

    它的指标在 Agent 内部按解析出的行业名从 payload 里挑。
    """
    assert INDUSTRY_INDICATORS["A20_generic_industry"] == ()
