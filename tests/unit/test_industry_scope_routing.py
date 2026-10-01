"""★★ 行业 Agent 的**归属判定**：不归它管的标的，不许写成「非本框架」。

## 现场（`docs/PRD.md` §19.16.5 第 1 条，真实 LLM 端到端跑出来的）

兜底 Agent（A20）接管银行之后，A13（科技）/A14（消费）**仍然会**输出：

> 「600036 属银行、**不在本次科技行业数据覆盖内**…无法给出可验证的持有结论」

A20 已经给出银行结论 ⇒ 这句不再是"没人管"，但**读起来仍然是"系统缺能力"**。
更贵的是：它每次都要花一次 `reasoning` 档 LLM 调用去说这句废话。

## 修法（三层，判据只有一份实现）

1. **规划期裁剪**（`prune_industry_agents_by_focus`）：个股/综合问法只留
   "管这个标的行业"的行业 Agent（+ 兜底 A20）。
   → "高股息招商银行"这条问句从"跑 4 路行业 Agent"变成"只跑 A20"。
2. **确定性结论**（`IndustryAgentBase._foreign_focus_reason`）：万一还是被挂上了
   （用户点名了那个行业），直接给一句**准确**的话，**不调 LLM** ——
   并说清"行业结论由谁负责"。
3. **prompt 禁令**：用户**主动点名**该行业时（`self_named`）不禁用这一节，
   但**禁止**免责话术（`_requirements` 里那段 ⚠️）。

## 本文件守的四件事

- 判据**同源**：全部走 `route_industry`（`INDUSTRY_KEYWORDS` 是路由的单一事实源）
  + `resolve_focus_industry`（本地名录）—— 领域层不重写一份；
- **判不出来就不动**：行业解析不出时**原样返回**，不许凭猜测砍 Agent；
- **点名优先**：用户说了"消费板块"，A14 就得留下（那是他要的视角）；
- **不许调 LLM**：确定性分支必须真的不花钱（用假 gateway 计调用次数来判）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.domain.agents.analysis.base import AnalysisPayload
from src.domain.agents.industry.consumer.agent import ConsumerIndustryAgent
from src.domain.agents.industry.generic.agent import GenericIndustryAgent
from src.orchestration.supervisor import (
    INDUSTRY_AGENTS,
    industry_scope_for,
    prune_industry_agents_by_focus,
    resolve_focus_industry,
)

#: 一条问句里可能被规划器"保守起见全选"的行业 Agent 全集
_ALL_INDUSTRY = list(INDUSTRY_AGENTS)

_LLM_JSON = (
    '{"conclusion":"（这条来自 LLM）","confidence":"high","outlook":"平稳",'
    '"cycle_position":"成熟期","drivers":[],"risks":[]}'
)


class _StubGateway:
    """假 LLM 网关：**记录调用次数**（确定性分支必须 0 次）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, tier: str, system_prompt: str, prompt: str,
                       **kwargs: Any) -> Any:
        self.calls.append({"tier": tier, "system": system_prompt,
                           "prompt": prompt, **kwargs})
        # 用**真实的** LLMResponse（审计链会读 model_used/tokens/cache_kind，
        # 自己搭一个假对象只会一个个属性去补，直到某天又缺一个）
        from src.infrastructure.llm.models import LLMResponse

        return LLMResponse(content=_LLM_JSON, model_used="stub", provider="stub",
                           tokens_in=1, tokens_out=1)


def _consumer(gateway: _StubGateway) -> ConsumerIndustryAgent:
    return ConsumerIndustryAgent(gateway)  # type: ignore[arg-type]


def _payload(**kw: Any) -> AnalysisPayload:
    return AnalysisPayload(**kw)


# ============================================================
# ① 标的行业解析（判据的起点，复用 §19.16 的唯一实现）
# ============================================================


class TestResolveFocusIndustry:
    def test_bank_code(self) -> None:
        assert resolve_focus_industry("600036")[0] == "银行"

    def test_liquor_code(self) -> None:
        assert resolve_focus_industry("600519")[0] == "白酒"

    def test_stock_name_without_industry_word(self) -> None:
        """`贵州茅台` 里没有"白酒"二字，靠"简称→代码→名录"这一路。"""
        assert resolve_focus_industry("贵州茅台未来走势")[0] == "白酒"

    @pytest.mark.parametrize("text", ["今天天气怎么样", "帮我写一首诗", ""])
    def test_unresolvable_never_guesses(self, text: str) -> None:
        assert resolve_focus_industry(text) == ("", "")


# ============================================================
# ② 规划期裁剪：只留"管这个标的行业"的（+ 兜底）
# ============================================================


class TestPruneIndustryAgentsByFocus:
    def test_bank_focus_keeps_only_generic(self) -> None:
        """★ 现场问句：银行没有任何专属 Agent ⇒ 只留 A20。"""
        planned = _ALL_INDUSTRY + ["A10_stock", "A17_recommend"]
        out = prune_industry_agents_by_focus(planned, "600036 招商银行高股息")
        assert "A20_generic_industry" in out
        assert [a for a in out if a in INDUSTRY_AGENTS] == ["A20_generic_industry"]
        # 非行业 Agent 一个都不许动
        assert "A10_stock" in out and "A17_recommend" in out

    def test_liquor_focus_keeps_its_owner_not_generic(self) -> None:
        """★ 600519 → 白酒 → 归 A14 管：留 A14，**不留** A20（它有主）。"""
        out = prune_industry_agents_by_focus(_ALL_INDUSTRY, "600519 贵州茅台")
        assert [a for a in out if a in INDUSTRY_AGENTS] == ["A14_consumer"]

    def test_semiconductor_name_keeps_tech(self) -> None:
        out = prune_industry_agents_by_focus(_ALL_INDUSTRY, "半导体板块怎么看")
        assert "A13_tech" in out
        assert "A14_consumer" not in out

    def test_named_industry_is_kept_even_if_focus_is_other(self) -> None:
        """★ 点名优先：用户明确问"消费板块里的银行股" ⇒ A14 必须留下。"""
        out = prune_industry_agents_by_focus(
            _ALL_INDUSTRY, "招商银行在消费板块里的表现")
        assert "A14_consumer" in out
        assert "A20_generic_industry" in out
        assert "A13_tech" not in out and "A15_cyclical" not in out

    def test_unresolvable_focus_changes_nothing(self) -> None:
        """判不出行业且没点名 ⇒ **原样返回**（宁可多跑，不许凭猜测砍）。"""
        planned = _ALL_INDUSTRY
        assert prune_industry_agents_by_focus(planned, "今天天气怎么样") == planned

    def test_returns_same_object_order_preserved(self) -> None:
        planned = ["A13_tech", "A10_stock", "A14_consumer"]
        out = prune_industry_agents_by_focus(planned, "600519 贵州茅台")
        assert out == ["A10_stock", "A14_consumer"]


# ============================================================
# ③ 归属判定 `industry_scope_for`
# ============================================================


class TestIndustryScopeFor:
    def test_consumer_agent_on_bank_focus(self) -> None:
        scope = industry_scope_for("A14_consumer", "600036 招商银行高股息")
        assert scope is not None
        assert scope["agent_industry"] == "消费"
        assert scope["focus_industry"] == "银行"
        assert scope["self_named"] is False
        assert "A20" in scope["served_by"], scope["served_by"]

    def test_owner_gets_no_scope(self) -> None:
        """归它管 → None（A14 对白酒标的的行为必须不变）。"""
        assert industry_scope_for("A14_consumer", "600519 贵州茅台") is None

    def test_named_industry_marks_self_named(self) -> None:
        scope = industry_scope_for("A14_consumer", "招商银行在消费板块里的表现")
        assert scope is not None and scope["self_named"] is True

    def test_generic_agent_never_gets_scope(self) -> None:
        """A20 是兜底，不参与"归属"判定（它本来就该管没人管的行业）。"""
        assert industry_scope_for("A20_generic_industry", "600036 招商银行") is None

    def test_unresolvable_gets_no_scope(self) -> None:
        assert industry_scope_for("A14_consumer", "今天天气怎么样") is None

    def test_served_by_names_the_owner_when_one_exists(self) -> None:
        """600519 归 A14 —— A13 被挂上时要能说清"由谁负责"。"""
        scope = industry_scope_for("A13_tech", "600519 贵州茅台")
        assert scope is not None
        assert "A14_consumer" in scope["served_by"]


# ============================================================
# ④ 确定性结论：**不调 LLM**，且说清谁负责
# ============================================================


class TestForeignFocusConclusion:
    @pytest.mark.asyncio
    async def test_scope_short_circuits_before_llm(self) -> None:
        gateway = _StubGateway()
        agent = _consumer(gateway)
        out = await agent.execute(_input(_payload(
            focus="600036",
            user_query="未来半年能否持有高股息的招商银行？",
            hint={"industry_scope": {
                "agent_industry": "消费", "focus_industry": "银行",
                "served_by": "兜底行业 Agent（A20_generic_industry）",
                "self_named": False}},
        )))
        assert gateway.calls == [], "确定性分支**不许**调 LLM（那是一次 reasoning 调用）"
        assert out.result["skipped"] is True
        assert out.result["skip_kind"] == "out_of_scope"
        assert "银行" in out.conclusion and "消费" in out.conclusion
        assert "A20" in out.conclusion
        # ★ 用户读到的这句里**不许**出现原来的免责话术
        assert "非本框架" not in out.conclusion

    @pytest.mark.asyncio
    async def test_self_named_runs_llm_and_bans_the_disclaimer(self) -> None:
        gateway = _StubGateway()
        agent = _consumer(gateway)
        out = await agent.execute(_input(_payload(
            focus="600036",
            user_query="招商银行在消费板块里的表现",
            # 有一个申万截面点 ⇒ `_skip_reason()` 不跳过（否则测的是跳过路径，
            # 而不是"点名时真的分析"）
            data_points=[{
                "indicator": "ind:sw_first_pe_ttm:all", "value": 7.27,
                "period_date": "2026-09-28", "confidence": 0.9,
                "source_name": "申万", "extra": {"industry_name": "银行"},
            }],
            hint={"industry_scope": {
                "agent_industry": "消费", "focus_industry": "银行",
                "served_by": "兜底行业 Agent（A20_generic_industry）",
                "self_named": True}},
        )))
        assert len(gateway.calls) == 1, "用户点名的行业必须真的分析（不能被短路）"
        prompt = gateway.calls[0]["prompt"]
        assert "禁止" in prompt and "非本框架" in prompt
        assert out.conclusion.startswith("（这条来自 LLM）") or out.conclusion

    @pytest.mark.asyncio
    async def test_no_scope_behaves_as_before(self) -> None:
        """没有归属判定时行为**一点不变**（A13-A16 的既有路径）。"""
        gateway = _StubGateway()
        agent = _consumer(gateway)
        out = await agent.execute(_input(_payload(
            focus="白酒", user_query="白酒板块景气度", hint={},
        )))
        assert out.result.get("skip_kind") != "out_of_scope"

    @pytest.mark.asyncio
    async def test_generic_agent_ignores_scope_key(self) -> None:
        """A20 收到 `industry_scope`（编排层理论上不会给它）也不得短路。"""
        gateway = _StubGateway()
        agent = GenericIndustryAgent(gateway)  # type: ignore[arg-type]
        payload = _payload(
            focus="600036", user_query="招商银行",
            hint={"industry_scope": {
                "agent_industry": "消费", "focus_industry": "银行",
                "served_by": "x", "self_named": False}},
        )
        assert agent._foreign_focus_reason(payload) is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_skip_kind_distinguishes_the_two_reasons(self) -> None:
        """★「标的不归我管」与「我这边没数据」**必须可区分**（都不许静默）。"""
        gateway = _StubGateway()
        agent = _consumer(gateway)
        out = await agent.execute(_input(_payload(
            focus="无行业信息的一段话", user_query="今天天气怎么样",
            hint={"industry_scope": {
                "agent_industry": "消费", "focus_industry": "银行",
                "served_by": "x", "self_named": False}},
            data_points=[], events=[], verified_texts=[],
        )))
        assert out.result["skip_kind"] == "out_of_scope"


# ============================================================
# ⑦ 免责话术的**确定性**清理（prompt 禁令实测挡不住，见 §19.16.5）
# ============================================================

#: 真实端到端跑出来的 A13/A14 原话（**不得改写**）
_REAL_DISCLAIMER_A13 = (
    "600036属银行、不在本次科技行业数据覆盖内，本地无其PE/PB/股息与宏观利率数据，"
    "无法给出可验证的持有结论，只能判为「数据不足、不加仓、底仓可留」。"
    "间接参照：科技行业PE(TTM)2026-09-22=115.14处近5年99.8%分位（追高风险大）。"
)


class TestDisclaimerStrip:
    """`_strip_disclaimers`：只在 `industry_scope` 存在时生效，**按句**处理。"""

    def _agent(self) -> ConsumerIndustryAgent:
        return _consumer(_StubGateway())

    def _scope(self, **kw: Any) -> dict[str, Any]:
        base = {"agent_industry": "消费", "focus_industry": "银行",
                "served_by": "兜底行业 Agent（A20_generic_industry）",
                "self_named": True}
        base.update(kw)
        return {"industry_scope": base}

    def test_drops_only_the_disclaimer_sentence(self) -> None:
        from src.core.models import AgentOutput
        from src.core.schemas import Confidence

        agent = self._agent()
        payload = _payload(focus="消费行业", hint=self._scope())
        out = AgentOutput(task_id="t", agent_id="A14_consumer",
                          conclusion=_REAL_DISCLAIMER_A13,
                          confidence=Confidence.LOW, trace_id="t", result={})
        cleaned = agent._strip_disclaimers(out, payload)  # noqa: SLF001
        assert "非本框架" not in cleaned.conclusion
        assert "不在本次" not in cleaned.conclusion
        assert "无法给出可验证的持有结论" not in cleaned.conclusion
        # ★ 同段里的**行业分析必须保住**（不能整段丢）
        assert "科技行业PE(TTM)2026-09-22=115.14" in cleaned.conclusion
        # ★ 丢了什么要留痕（静默改写用户看到的话是不可接受的）
        assert cleaned.result["disclaimers_dropped"]
        assert "§19.16.5" in cleaned.result["disclaimers_dropped_reason"]

    def test_no_scope_means_no_rewrite(self) -> None:
        """没有归属判定时**一个字都不许改**（防止误伤正常结论）。"""
        from src.core.models import AgentOutput
        from src.core.schemas import Confidence

        agent = self._agent()
        out = AgentOutput(task_id="t", agent_id="A14_consumer",
                          conclusion=_REAL_DISCLAIMER_A13,
                          confidence=Confidence.LOW, trace_id="t", result={})
        cleaned = agent._strip_disclaimers(out, _payload(focus="白酒", hint={}))  # noqa: SLF001
        assert cleaned.conclusion == out.conclusion

    def test_all_sentences_dropped_falls_back_to_deterministic_line(self) -> None:
        from src.core.models import AgentOutput
        from src.core.schemas import Confidence

        agent = self._agent()
        payload = _payload(focus="消费行业", hint=self._scope(self_named=False))
        out = AgentOutput(task_id="t", agent_id="A14_consumer",
                          conclusion="600036 非本框架覆盖标的，无法给出可验证的持有结论。",
                          confidence=Confidence.LOW, trace_id="t", result={})
        cleaned = agent._strip_disclaimers(out, payload)  # noqa: SLF001
        assert "消费" in cleaned.conclusion and "银行" in cleaned.conclusion
        assert "非本框架" not in cleaned.conclusion

    def test_generic_agent_is_never_rewritten(self) -> None:
        """A20 是兜底，它的结论不许被这套话术过滤器改写。"""
        from src.core.models import AgentOutput
        from src.core.schemas import Confidence

        agent = GenericIndustryAgent(_StubGateway())  # type: ignore[arg-type]
        out = AgentOutput(task_id="t", agent_id="A20_generic_industry",
                          conclusion=_REAL_DISCLAIMER_A13,
                          confidence=Confidence.LOW, trace_id="t", result={})
        cleaned = agent._strip_disclaimers(out, _payload(hint=self._scope()))  # noqa: SLF001
        assert cleaned.conclusion == out.conclusion

    def test_sentence_split_is_lossless(self) -> None:
        from src.domain.agents.industry.base import _split_sentences

        text = "第一句。第二句！第三句？\n第四句；第五句"
        assert "".join(_split_sentences(text)) == text


class TestFocusOverride:
    """用户点名行业时，焦点换成**该行业**（否则模型只会写"这票不归我管"）。"""

    def test_named_industry_switches_focus_to_the_industry(self) -> None:
        from src.orchestration.supervisor import _build_analyze_payload_fn

        state = _state()          # 验收问句里含"消费" ⇒ A14 被点名
        payload = _build_analyze_payload_fn("A14_consumer")(state)
        assert payload["focus"] == "消费行业", payload["focus"]
        assert payload["hint"]["industry_scope"]["self_named"] is True

    def test_unnamed_industry_keeps_the_stock_focus(self) -> None:
        """没点名时不换焦点 —— 那时走的是确定性短路（根本不调 LLM）。"""
        from src.orchestration.supervisor import _build_analyze_payload_fn

        state = _state(user_query="未来半年能否持有高股息的招商银行？")
        payload = _build_analyze_payload_fn("A14_consumer")(state)
        assert payload["focus"] == "招商银行"

    def test_owner_keeps_the_stock_focus(self) -> None:
        """归它管的（A14 对白酒）焦点不变 —— 那是它该分析的标的。"""
        from src.orchestration.supervisor import _build_analyze_payload_fn

        state = _state(target="600519", target_display="贵州茅台",
                       user_query="贵州茅台未来走势如何？", focus_stock_code="600519")
        payload = _build_analyze_payload_fn("A14_consumer")(state)
        assert payload["focus"] == "贵州茅台"


def _input(payload: AnalysisPayload) -> Any:
    from src.core.models import AgentInput

    return AgentInput(task_id="t-scope", tenant_id="tenant_001",
                      payload=payload.model_dump())


# ============================================================
# ⑤ 接线（"我改了" ≠ "它生效了"：判定必须真的进到 Agent 的 payload 里）
# ============================================================

#: 用户那条原始复合问（验收语料，**不得改写**）
_ACCEPTANCE_QUERY = (
    "当前宏观环境如何，预测下未来一年美国的加息节奏，对A股的影响，"
    "以及AI应用加速失业率增加对消费的影响节奏时间节点分析，"
    "未来半年能否持有高股息的招商银行？"
)


def _state(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "t-wire", "tenant_id": "tenant_001",
        "target": "600036", "target_display": "招商银行",
        "user_query": _ACCEPTANCE_QUERY, "validated_points": [],
        "analysis_hint": {}, "focus_stock_code": "600036",
    }
    base.update(kw)
    return base


class TestPayloadFnWiring:
    """`_build_analyze_payload_fn` 是判定进入 Agent 的**唯一通道**。"""

    def test_scope_reaches_the_consumer_agent_hint(self) -> None:
        """★ 用户那条验收问句本身就点了"消费" ⇒ `self_named=True`（要真的分析）。

        这不是缺陷：问句里确实有「对**消费**的影响节奏时间节点分析」这个子问题，
        所以 A14 该留下 —— 但它要回答的是**消费行业本身**，不是"600036 属不属于消费"。
        对应的机器判据：`self_named=True` + prompt 里带禁令（见 ④ 的用例）。
        """
        from src.orchestration.supervisor import _build_analyze_payload_fn

        payload = _build_analyze_payload_fn("A14_consumer")(_state())
        scope = payload["hint"]["industry_scope"]
        assert scope["focus_industry"] == "银行"
        assert scope["agent_industry"] == "消费"
        assert scope["self_named"] is True

    def test_scope_without_naming_the_industry(self) -> None:
        """问句没点"消费"时 `self_named=False` ⇒ 走确定性结论（不调 LLM）。"""
        from src.orchestration.supervisor import _build_analyze_payload_fn

        state = _state(user_query="未来半年能否持有高股息的招商银行？")
        scope = _build_analyze_payload_fn("A14_consumer")(state)["hint"]["industry_scope"]
        assert scope["self_named"] is False

    def test_scope_does_not_leak_into_shared_state_hint(self) -> None:
        """`analysis_hint` 是**共享**的 state 字段 —— 只许浅拷贝后加键。"""
        from src.orchestration.supervisor import _build_analyze_payload_fn

        state = _state()
        _build_analyze_payload_fn("A14_consumer")(state)
        assert "industry_scope" not in state["analysis_hint"], (
            "判定被写进了共享 state，下一个 Agent 会读到别人的归属结论")

    def test_owner_and_generic_agent_get_no_scope(self) -> None:
        """归它管的（A14 对白酒）与兜底的（A20）都不该有归属判定。"""
        from src.orchestration.supervisor import _build_analyze_payload_fn

        liquor = _state(target="600519", target_display="贵州茅台",
                        user_query="贵州茅台未来走势如何？", focus_stock_code="600519")
        assert "industry_scope" not in _build_analyze_payload_fn("A14_consumer")(liquor)["hint"]
        assert "industry_scope" not in _build_analyze_payload_fn("A20_generic_industry")(_state())["hint"]

    def test_non_industry_agents_never_get_scope(self) -> None:
        from src.orchestration.supervisor import _build_analyze_payload_fn

        for aid in ("A08_macro", "A10_stock", "A17_recommend"):
            assert "industry_scope" not in _build_analyze_payload_fn(aid)(_state())["hint"]


# ============================================================
# ⑥ 裸名陷阱（§19.17）：行业类指标必须补**行业名**，不许留裸名
# ============================================================


class TestIndustrySuffixSanitize:
    """`行业拥挤度` 这类族的后缀是**行业名**（不是 6 位代码）。

    与 2026-09-26 那条 `PE(TTM)` 故障链**同一形状**：LLM planner 从
    `INDICATOR_CATALOG` 选出来的是裸名，而连接器只认 `X:{行业名}`
    ⇒ 裸名没有任何连接器 `supports()` ⇒ A01 白撞一次网络后必然失败
    ⇒ 用户看到"数据缺失"（**看起来像平台没有这个数据**）。
    """

    def test_bare_industry_indicator_gets_the_industry_name(self) -> None:
        from src.orchestration.supervisor import sanitize_indicators

        usable, dropped, notes = sanitize_indicators(
            ["行业拥挤度", "行业轮动"], "", "industry", industry="银行")
        assert usable == ["行业拥挤度:银行", "行业轮动:银行"]
        assert dropped == [], dropped
        assert any("补行业名" in n for n in notes)

    def test_bare_industry_indicator_is_dropped_without_industry(self) -> None:
        """解析不出行业 → **摘掉**，且**不许**换成申万截面（那是另一回事）。"""
        from src.orchestration.supervisor import sanitize_indicators

        usable, dropped, notes = sanitize_indicators(
            ["行业拥挤度"], "", "industry")
        assert usable == [], usable
        assert dropped == ["行业拥挤度"]
        assert not any("ind:sw_" in n for n in notes), notes

    def test_already_suffixed_industry_indicator_is_kept(self) -> None:
        """已带行业名 → 原样保留（不许被 target 覆盖成别的行业）。"""
        from src.orchestration.supervisor import sanitize_indicators

        usable, _, _ = sanitize_indicators(
            ["板块资金流:白酒", "行业轮动:白酒"], "600519", "stock", industry="银行")
        assert usable == ["板块资金流:白酒", "行业轮动:白酒"]

    def test_code_suffix_behaviour_unchanged(self) -> None:
        """回归：个股类裸名的既有行为（补代码 / 无代码则丢弃并换行业截面）。"""
        from src.orchestration.supervisor import sanitize_indicators

        usable, _, _ = sanitize_indicators(["PE(TTM)"], "600036", "stock")
        assert usable == ["PE(TTM):600036"]
        usable2, dropped2, notes2 = sanitize_indicators(["PE(TTM)"], "", "macro")
        assert "PE(TTM)" in dropped2
        assert "ind:sw_third_pe_ttm:all" in usable2, notes2

    def test_two_suffix_tables_are_disjoint(self) -> None:
        """两张后缀表**不许重叠**（重叠 = 同一指标按两种方式补后缀，必错一种）。"""
        from src.orchestration.supervisor import (
            _CODE_SUFFIX_INDICATORS,
            _INDUSTRY_SUFFIX_INDICATORS,
        )

        assert not (_CODE_SUFFIX_INDICATORS & _INDUSTRY_SUFFIX_INDICATORS)

    def test_industry_families_are_declared_by_the_connector(self) -> None:
        """★ 派生断言：行业类族的连接器必须真的认 `X:{行业名}`。

        判据不写死连接器类名以外的任何东西 —— 有 `supports()` 才算"这条路能取到"，
        与 `test_contract_consistency.py` 的 ③→① 同一个理由。
        """
        from src.infrastructure.connectors.platform_data_connector import (
            PlatformDataConnector,
        )
        from src.orchestration.supervisor import _INDUSTRY_SUFFIX_INDICATORS

        for family in sorted(_INDUSTRY_SUFFIX_INDICATORS):
            assert PlatformDataConnector.supports(f"{family}:银行"), (
                f"连接器不认 {family}:{{行业名}} —— 计划里放下它必然失败")
            assert not PlatformDataConnector.supports(family), (
                f"连接器不该认裸名 {family}（那会让裸名悄悄变成'能取'）")
