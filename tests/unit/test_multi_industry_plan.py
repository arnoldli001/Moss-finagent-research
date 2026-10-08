"""★★ 多行业问句的**两半**：并集判据把 A20 挂上 + A20/A09 **逐行业各出一节**。

## 报障原话（用户 2026-10-08，与 `CHG-0216`/`CHG-0217` 同一条）

> 「当前宏观环境如何，预测下未来一年美国的加息预期下，基于当前板块拥挤度和
>   能源重点项目与新业态投资20万亿的政策，未来半年能否持有高股息的
>   **宁波银行**和**中国神华**？标的 **601088**」

## 修的是什么（`docs/PRD.md` §41.36 的 A20 行）

`resolve_focus_industries`（`CHG-0217` 的**并集**解析器）明明解出**两个**行业
（`601088 → 煤炭开采`、`宁波银行 → 银行`），但**挂 A20 的判据是单值的**
（`needs_generic_industry` 只解析**一个**行业，"确定性优先"从 601088 解出
`煤炭开采` 就返回）⇒ `煤炭开采` 已被 A15（周期）覆盖 ⇒ 判据说"有人管" ⇒
**A20 不挂**；而问句里第二个行业「银行」没有任何 A13–A16 接管
⇒ 用户读到"宁波银行没有行业结论"。同一条单值判据还出现在
`prune_industry_agents_by_focus` 的 `owners` 里 ⇒ **A20 即使挂上也会被裁掉**。

## 本文件守的四件事（对应任务书的 ①②③④）

1. **并集挂载**：每一个"没有专属 Agent 接管"的行业都要补挂 A20；
2. **并集裁剪**：`prune` 不再把刚挂上的 A20 裁掉（判据同源）；
3. **单行业逐字不变**：0/1 个行业时，挂载文案、裁剪结果、A20 的执行路径
   与**修复前的原文路径**逐字一致（用冻结字符串 + 与基类路径逐字段比对钉住）；
4. **逐行业各出一节**：A20 在 ≥2 个"没人管"的行业时逐行业各跑一次既有路径、
   合并成 N 节；A09 的 prompt 在多行业时**明令逐行业**（单行业时 prompt 冻结）。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from src.core.models import AgentInput
from src.domain.agents.analysis.base import AnalysisPayload
from src.domain.agents.analysis.meso.agent import MesoAnalysisAgent
from src.domain.agents.industry.base import IndustryAgentBase
from src.domain.agents.industry.generic.agent import (
    _MAX_SECTIONS,
    GenericIndustryAgent,
)
from src.orchestration.supervisor import (
    INDUSTRY_AGENTS,
    _build_analyze_payload_fn,
    augment_plan_by_query_signals,
    needs_generic_industries,
    needs_generic_industry,
    prune_industry_agents_by_focus,
    resolve_focus_industries,
)

#: 用户那条原始复合问（验收语料，**不得改写**）
_ACCEPTANCE_QUERY = (
    "当前宏观环境如何，预测下未来一年美国的加息预期下，基于当前板块拥挤度和"
    "能源重点项目与新业态投资20万亿的政策，未来半年能否持有高股息的"
    "宁波银行和中国神华？"
)
_ACCEPTANCE_CODE = "601088"
#: `标的 + 问句` —— 编排层的判据吃的就是这个形状（与 `_apply_query_signal_augmentation` 同）
_ACCEPTANCE_TEXT = f"{_ACCEPTANCE_CODE} {_ACCEPTANCE_QUERY}"

_ALL_INDUSTRY = list(INDUSTRY_AGENTS)

#: 单行业问句下 A20 的补挂文案 —— **逐字冻结**（`'、'.join([x]) == x`，
#: 所以并集改造不许改变这一条）。
_FROZEN_SINGLE_NOTE = (
    "标的属「银行」行业且无专属行业 Agent → 补挂 A20_generic_industry（兜底）")

#: A09 单行业（无清单）时的任务要求 —— **逐字冻结**（修复前原文）
_FROZEN_SINGLE_REQUIREMENTS = (
    "输出JSON：\n"
    '- "conclusion": 首句直接答问并点出具体细分环节/方向，200字内\n'
    '- "confidence": high|medium|low\n'
    '- "industry_cycle": 导入期|成长期|成熟期|衰退期|不明确\n'
    '- "prosperity": 高|中|低\n'
    '- "chain_position": 产业链位置一句话\n'
    '- "key_points": 3-5条\n'
    '- "risks": 1-3条'
)

_INDUSTRY_MARKER = re.compile(r"本行业=([^（\n]+)")


class _StubGateway:
    """假 LLM 网关：记录每次调用（并集判据/逐节输出都靠调用次数与 prompt 判）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, tier: str, system_prompt: str, prompt: str,
                       **kwargs: Any) -> Any:
        from src.infrastructure.llm.models import LLMResponse

        self.calls.append({"tier": tier, "system": system_prompt,
                           "prompt": prompt, **kwargs})
        match = _INDUSTRY_MARKER.search(prompt)
        industry = match.group(1).strip() if match else "未知行业"
        return LLMResponse(
            content=(
                '{"conclusion":"本节只谈' + industry + '的景气与估值",'
                '"confidence":"high","outlook":"平稳","cycle_position":"成熟期",'
                '"drivers":[],"risks":[]}'
            ),
            model_used="stub", provider="stub", tokens_in=11, tokens_out=7)


def _sw_row(industry: str, value: float, metric: str = "pe_ttm") -> dict[str, Any]:
    """申万截面里**某个行业**的一行（`extra.industry_name` 是归属字段）。"""
    return {
        "indicator": "ind:sw_first_pe_ttm:all", "value": value,
        "period_date": "2026-09-29", "source_name": "AKShare申万行业估值",
        "confidence": 0.9,
        "extra": {"industry_name": industry, "industry_level": "first",
                  "metric": metric, "constituent_count": 42},
    }


def _a20(gateway: _StubGateway) -> GenericIndustryAgent:
    return GenericIndustryAgent(gateway)  # type: ignore[arg-type]


def _payload(**kw: Any) -> AnalysisPayload:
    return AnalysisPayload(**kw)


def _input(payload: AnalysisPayload) -> AgentInput:
    return AgentInput(task_id="t-multi-industry", tenant_id="tenant_001",
                      payload=payload.model_dump())


def _state(**kw: Any) -> dict[str, Any]:
    """`_build_analyze_payload_fn` 需要的最小 state（只读字段）。"""
    base: dict[str, Any] = {
        "task_id": "t-multi-industry", "tenant_id": "tenant_001",
        "target": _ACCEPTANCE_CODE, "target_display": "中国神华",
        "user_query": _ACCEPTANCE_QUERY, "validated_points": [],
        "analysis_hint": {}, "focus_stock_code": _ACCEPTANCE_CODE,
    }
    base.update(kw)
    return base


# ============================================================
# ① 并集挂载：**无专属 Agent 的行业**都要补一次 A20
# ============================================================


class TestUnionMount:
    def test_acceptance_query_resolves_two_industries(self) -> None:
        """先证明"两个行业"这件事是真的（判据的起点，`CHG-0217` 的解析器）。"""
        names = [n for n, _h in resolve_focus_industries(_ACCEPTANCE_TEXT)]
        assert names == ["煤炭开采", "银行"], names

    def test_only_the_unowned_industry_needs_the_fallback(self) -> None:
        """★ 逐行业判：`煤炭开采` 有主（A15）→ 不需要兜底；`银行` 没人管 → 需要。

        修前这里返回 `''`（单值判据看到两个字里的第一个有主就收工）。
        """
        assert needs_generic_industries(_ACCEPTANCE_TEXT, _ACCEPTANCE_CODE) == ["银行"]
        assert needs_generic_industry(_ACCEPTANCE_TEXT, _ACCEPTANCE_CODE) == "银行"

    def test_fallback_agent_is_attached_by_the_augment_layer(self) -> None:
        """★★ 端到端判据的最上游：`augment_plan_by_query_signals` 必须挂上 A20。"""
        agents, indicators, notes = augment_plan_by_query_signals(
            _ACCEPTANCE_TEXT, "", ["A08_macro", "A09_meso"], ["CPI"],
            resolved_code=_ACCEPTANCE_CODE, resolved_codes=("601088", "002142"))
        assert "A20_generic_industry" in agents, f"没挂兜底行业 Agent：{agents}"
        assert any("银行" in n and "兜底" in n for n in notes), notes
        # 两个行业各自的板块三族都要进计划（`CHG-0217` 的行为，不许被本改动打回）
        for industry in ("煤炭开采", "银行"):
            assert f"行业拥挤度:{industry}" in indicators, indicators
            assert f"板块资金流:{industry}" in indicators, indicators

    def test_fallback_note_names_every_unowned_industry(self) -> None:
        """多行业时留痕要点名**每个**要兜底的行业（用户要能看出为什么多一个 Agent）。"""
        _agents, _ind, notes = augment_plan_by_query_signals(
            _ACCEPTANCE_TEXT, "", [], [], resolved_code=_ACCEPTANCE_CODE)
        note = next(n for n in notes if "兜底" in n)
        assert "「银行」" in note, note


# ============================================================
# ② 并集裁剪：A20 刚挂上**不许**被裁掉
# ============================================================


class TestPruneKeepsTheFallback:
    def test_prune_no_longer_drops_a20(self) -> None:
        """★ 实测的缺陷形状：`prune(['A20'], txt, '601088') == []`。"""
        planned = ["A15_cyclical", "A20_generic_industry"]
        assert prune_industry_agents_by_focus(
            planned, _ACCEPTANCE_TEXT, _ACCEPTANCE_CODE) == planned

    def test_prune_keeps_both_owners_of_the_two_industries(self) -> None:
        """`煤炭开采` 归 A15、`银行` 归 A20 —— 两个都要留下，无关的仍要裁掉。"""
        planned = _ALL_INDUSTRY + ["A10_stock", "A17_recommend"]
        out = prune_industry_agents_by_focus(
            planned, _ACCEPTANCE_TEXT, _ACCEPTANCE_CODE)
        assert [a for a in out if a in INDUSTRY_AGENTS] == [
            "A15_cyclical", "A20_generic_industry"], out
        assert "A10_stock" in out and "A17_recommend" in out


# ============================================================
# ③ ★★ 单行业（0/1 个行业）：挂载 / 裁剪 / 输出**逐字不变**
# ============================================================


class TestSingleIndustryUnchanged:
    @pytest.mark.parametrize(("text", "code", "expect"), [
        ("未来半年能否持有高股息的招商银行？", "600036", "银行"),
        ("平安银行(000001)怎么样", "000001", "银行"),
        ("贵州茅台未来走势", "600519", ""),
        ("半导体行业景气度如何", "", ""),
        ("今天天气怎么样", "", ""),
    ])
    def test_single_value_entry_point_is_unchanged(
            self, text: str, code: str, expect: str) -> None:
        """单值入口的既有 5 条判据（`test_generic_industry_agent.py` 同款）不许变。"""
        assert needs_generic_industry(text, code) == expect

    @pytest.mark.parametrize(("text", "code", "expect"), [
        ("未来半年能否持有高股息的招商银行？", "600036", ["银行"]),
        ("贵州茅台未来走势", "600519", []),
        ("半导体行业景气度如何", "", []),
        ("今天天气怎么样", "", []),
    ])
    def test_union_criterion_equals_the_single_one_for_one_industry(
            self, text: str, code: str, expect: list[str]) -> None:
        """★ 单行业问句下并集判据 == `[单值判据]`（挂载/裁剪因此逐字不变）。"""
        assert needs_generic_industries(text, code) == expect

    def test_union_parser_may_add_an_industry_via_name_collision(self) -> None:
        """⚠️ **已知边界**（`CHG-0217` 的并集语义，**不是本改动引入的**）。

        「平安银行(000001)」里同时命中简称「平安」→ `601318`（中国平安）
        ⇒ 并集 = `[银行, 保险]`（采集覆盖取并集是**刻意的**：宁多一个板块，
        不漏用户点到的那个）。后果：A20 会为 `保险` 也多出一节。

        这里把它**显式钉住**（而不是假装不存在）——判据是机器可读的，
        将来若要收紧，收紧点在 `resolve_industries_from_text`（不在本轮半径内）。
        """
        assert needs_generic_industry("平安银行(000001)怎么样", "000001") == "银行"
        assert needs_generic_industries(
            "平安银行(000001)怎么样", "000001") == ["银行", "保险"]

    def test_multi_agent_note_is_byte_identical_for_one_industry(self) -> None:
        """★ 单行业时补挂文案与修复前**逐字相同**（并集只影响 ≥2 个行业时）。"""
        agents, _ind, notes = augment_plan_by_query_signals(
            "600036 未来半年能否持有高股息的招商银行？", "", [], [],
            resolved_code="600036")
        assert "A20_generic_industry" in agents
        assert _FROZEN_SINGLE_NOTE in notes, notes

    def test_prune_is_unchanged_for_one_industry(self) -> None:
        planned = _ALL_INDUSTRY + ["A10_stock"]
        assert prune_industry_agents_by_focus(
            planned, "600036 招商银行高股息") == [
            "A20_generic_industry", "A10_stock"]
        # 有主的行业：留 A14、不留 A20（既有判据）
        assert [a for a in prune_industry_agents_by_focus(
            planned, "600519 贵州茅台") if a in INDUSTRY_AGENTS] == ["A14_consumer"]
        # 判不出行业且没点名：**原样返回**
        assert prune_industry_agents_by_focus(
            planned, "今天天气怎么样") == planned

    @pytest.mark.asyncio
    async def test_execute_is_the_legacy_path_for_one_industry(self) -> None:
        """★★ "单行业逐字不变"的**结构性**判据：与基类路径逐字段比对。

        `GenericIndustryAgent.execute` 在 0/1 个行业时必须**直接转发**给
        `IndustryAgentBase.execute`（= 修复前那条原文路径）——
        所以这里拿同一份 `AgentInput` 分别跑两条路，比较
        `(conclusion, confidence, result, data_refs)` 与**prompt 字节**。
        （`reasoning_steps` 带时间戳，不参与比较。）
        """
        payload = _payload(
            focus="600036 招商银行", user_query="未来半年能否持有高股息的招商银行？",
            data_points=[_sw_row("银行", 7.27), _sw_row("煤炭开采", 12.5)])

        gw_new = _StubGateway()
        out_new = await _a20(gw_new).execute(_input(payload))

        gw_legacy = _StubGateway()
        out_legacy = await IndustryAgentBase.execute(
            _a20(gw_legacy), _input(payload))

        assert len(gw_new.calls) == 1 and len(gw_legacy.calls) == 1
        assert gw_new.calls[0]["prompt"] == gw_legacy.calls[0]["prompt"], (
            "单行业路径的 prompt 被改动了")
        assert out_new.conclusion == out_legacy.conclusion
        assert out_new.confidence == out_legacy.confidence
        assert out_new.result == out_legacy.result
        assert out_new.data_refs == out_legacy.data_refs
        # 多行业专属的键在单行业时**不许出现**
        assert "multi_industry" not in out_new.result
        assert "generic_industry_sections" not in out_new.result

    @pytest.mark.asyncio
    async def test_industry_identity_follows_the_mount_criterion(self) -> None:
        """★ A20 分析的必须是**没人管的那个行业**（不许拿 A15 的行业去兜底）。

        问句的第一个行业是 `煤炭开采`（有主），第二个是 `银行`（没人管）——
        单值三路解析会返回 `煤炭开采`，于是"编排层挂 A20 管银行、A20 自己
        分析煤炭开采"，**挂的是 A、写的是 B 且不报错**。
        """
        agent = _a20(_StubGateway())
        payload = _payload(
            focus="601088 中国神华", user_query=_ACCEPTANCE_QUERY,
            data_points=[_sw_row("银行", 7.27), _sw_row("煤炭开采", 12.5)])
        assert agent.requested_industries(payload) == [
            ("银行", "简称解析 002142 → 本地名录")]
        assert agent.resolve_industry(payload)[0] == "银行"
        agent._prepare(payload)  # noqa: SLF001
        assert payload.hint["industry_signal"]["industry"] == "银行"


# ============================================================
# ④ A20 逐行业各出一节（≥2 个"没人管"的行业）
# ============================================================


class TestGenericAgentPerIndustrySections:
    _TWO_UNOWNED = "600030 中信证券和 601601 中国太保 谁更值得持有？"

    def _payload(self) -> AnalysisPayload:
        return _payload(
            focus="600030 中信证券", user_query=self._TWO_UNOWNED,
            data_points=[_sw_row("证券", 18.0), _sw_row("保险", 9.0),
                         _sw_row("煤炭开采", 12.5), _sw_row("银行", 7.27)])

    @pytest.mark.asyncio
    async def test_two_unowned_industries_produce_two_sections(self) -> None:
        gw = _StubGateway()
        agent = _a20(gw)
        payload = self._payload()
        assert agent.requested_industries(payload) == [
            ("证券", "个股代码 600030 → 本地名录（Tushare stock_basic）"),
            ("保险", "个股代码 601601 → 本地名录（Tushare stock_basic）"),
        ]
        out = await agent.execute(_input(payload))

        assert len(gw.calls) == 2, "逐行业各一节 = 每个行业各跑一次既有路径"
        assert out.result["industries"] == ["证券", "保险"]
        assert out.result["multi_industry"] is True
        # 每节都有标题（结构 = 既有单行业输出 + 一节标题）
        assert "### [1/2] 证券" in out.conclusion
        assert "### [2/2] 保险" in out.conclusion
        # 每节的结论**真的**进了交付（不许只写一个行业）
        assert "本节只谈证券的景气与估值" in out.conclusion
        assert "本节只谈保险的景气与估值" in out.conclusion
        # 逐节的行业归属留痕（哪个数属于哪个行业，用户要能判）
        sections = out.result["generic_industry_sections"]
        assert [s["industry"] for s in sections] == ["证券", "保险"]
        assert all(s["confidence"] == "high" for s in sections)
        # 形状兼容：既有的单值键仍在（取主行业那份）
        assert out.result["generic_industry_resolution"]["industry"] == "证券"
        # 逐行业的数据归属：每个行业各命中自己那一行申万截面
        assert [r["watched_from_sw_section"]
                for r in out.result["generic_industry_resolutions"]] == [1, 1]
        # token 是**求和**的（成本不许因为拆成 N 节而少算）
        assert (out.result["tokens_in"], out.result["tokens_out"]) == (22, 14)

    @pytest.mark.asyncio
    async def test_each_section_prompt_is_scoped_to_its_own_industry(self) -> None:
        """每一节的 prompt / 本地信号都必须**只谈它自己那个行业**。"""
        gw = _StubGateway()
        agent = _a20(gw)
        payload = self._payload()
        await agent.execute(_input(payload))
        assert [_INDUSTRY_MARKER.search(c["prompt"]).group(1).strip()
                for c in gw.calls] == ["证券", "保险"]

    @pytest.mark.asyncio
    async def test_confidence_is_the_weakest_section(self) -> None:
        """逐行业结论里只要有一节 low，整份交付就不许声称 high。"""
        gw = _StubGateway()
        agent = _a20(gw)
        calls = {"n": 0}
        original = gw.complete

        async def _mixed(tier, system, prompt, **kw):
            calls["n"] += 1
            out = await original(tier, system, prompt, **kw)
            if calls["n"] == 2:
                out.content = out.content.replace('"high"', '"low"')
            return out

        gw.complete = _mixed  # type: ignore[method-assign]
        out = await agent.execute(_input(self._payload()))
        assert out.confidence.value == "low"

    @pytest.mark.asyncio
    async def test_truncation_is_explicit(self) -> None:
        """★ 超上限时只分析前 N 个，但**必须显式登记**被截断的行业（不静默少写）。"""
        text = "银行、证券、保险、火力发电、水力发电、机场、港口"
        gw = _StubGateway()
        agent = _a20(gw)
        payload = _payload(focus="多行业", user_query=text,
                           data_points=[_sw_row("银行", 7.27)])
        out = await agent.execute(_input(payload))
        assert len(gw.calls) == _MAX_SECTIONS
        assert len(out.result["industries"]) == _MAX_SECTIONS
        assert out.result["industries_truncated"] == ["港口"]
        assert "截断" in out.result["industries_truncated_reason"]


# ============================================================
# ⑤ A09_meso：多行业时 prompt **明令逐行业**，单行业时 prompt 冻结
# ============================================================


class TestMesoPerIndustryRequirements:
    def _agent(self) -> MesoAnalysisAgent:
        return MesoAnalysisAgent(_StubGateway())  # type: ignore[arg-type]

    def test_single_industry_requirements_are_frozen(self) -> None:
        """★★ 单行业（无清单）时任务要求**逐字等于修复前原文**。"""
        agent = self._agent()
        assert agent._requirements(_payload(hint={})) == _FROZEN_SINGLE_REQUIREMENTS  # noqa: SLF001
        # 只有一个行业的清单同样是单行业口径（不许追加逐行业段落）
        assert agent._requirements(_payload(  # noqa: SLF001
            hint={"focus_industries": ["银行"]})) == _FROZEN_SINGLE_REQUIREMENTS

    def test_multi_industry_requirements_ask_per_industry(self) -> None:
        """多行业时每个字段都要逐行业（否则模型只会挑一个写）。"""
        text = self._agent()._requirements(_payload(  # noqa: SLF001
            hint={"focus_industries": ["煤炭开采", "银行"]}))
        assert text.startswith(_FROZEN_SINGLE_REQUIREMENTS), (
            "逐行业要求必须是**追加**（既有字段契约一个字都不许改）")
        assert "逐行业" in text
        assert "煤炭开采" in text and "银行" in text
        for field in ("conclusion", "industry_cycle", "prosperity",
                      "chain_position", "key_points"):
            assert field in text

    def test_payload_fn_injects_the_list_only_for_multi(self) -> None:
        """★ 接线：清单由**编排层**下发（唯一解析实现），且只在 ≥2 个行业时下发。

        为什么必须"只在多行业时"：`hint` 会进 prompt（`json.dumps(hint)`），
        单行业时多一个键 ⇒ prompt 变了 ⇒ "单行业逐字不变"就不成立了。
        """
        multi = _build_analyze_payload_fn("A09_meso")(_state())
        assert multi["hint"]["focus_industries"] == ["煤炭开采", "银行"]

        single_state = _state(target="600036", target_display="招商银行",
                              user_query="未来半年能否持有高股息的招商银行？",
                              focus_stock_code="600036")
        single = _build_analyze_payload_fn("A09_meso")(single_state)
        assert "focus_industries" not in single["hint"]

        # 其它 Agent 的 payload 不受影响（本键只给 A09）
        other = _build_analyze_payload_fn("A10_micro")(_state())
        assert "focus_industries" not in other["hint"]

    def test_multi_list_never_leaks_into_shared_state(self) -> None:
        """`analysis_hint` 是**共享** state 字段 —— 只许浅拷贝后加键。"""
        state = _state()
        _build_analyze_payload_fn("A09_meso")(state)
        assert "focus_industries" not in state["analysis_hint"]
