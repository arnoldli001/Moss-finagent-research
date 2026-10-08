"""缓存 `anchor` 接线：调用点必须把**变量部分**交给缓存（`CHG-0180`）。

## 为什么这些必须是**行为**判据，而不是 `grep "anchor="`

"调用点传了 anchor"这句话最容易写成形状判据（搜参数名），而它拦不住
真正会出错的三种形态：

  · 参数名写对了、**传的是空串**（= 没传，退回整 prompt 模式）；
  · 传的是**整 prompt**（= 骨架又回来了 ⇒ 必然串答案）；
  · 传的**只含用户问句**（= 换了数据也会命中 ⇒ 返回上一批数据的结论）。

所以这里全部**真跑一遍 agent**，从 `FakeGateway` 记下的 kwargs 里看
anchor 到底长什么样。

跑法：
    uv run python -m pytest tests/unit/test_cache_anchor_wiring.py -q
"""
from __future__ import annotations

import json

import pytest

from src.core.models import AgentInput


class _FakeGateway:
    """把每次 `complete()` 的 **全部 kwargs**（含 `anchor`）记下来。"""

    def __init__(self, reply: dict | None = None) -> None:
        self._reply = reply or {}
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        from src.infrastructure.llm.models import LLMResponse

        self.calls.append({"task_tier": task_tier, "system": system,
                           "prompt": prompt, **kwargs})
        return LLMResponse(content=json.dumps(self._reply, ensure_ascii=False),
                           model_used="fake", provider="fake",
                           prompt_hash="ph", response_hash="rh")


def _dp(indicator: str, value: float, period: str = "2026-08") -> dict:
    return {"indicator": indicator, "value": value, "period_date": period,
            "source_name": "AkShare", "data_id": f"d_{indicator}"}


def _macro_payload(query: str, cpi: float = 0.5) -> dict:
    """A08 的 payload。

    `user_query` 刻意**不含模板触发词**（"美联储/加息"）—— 命中触发词时
    A08 走纯模板、**根本不调 LLM**，那样就看不到 anchor 了
    （见 `test_macro_agent.py::test_macro_template_skips_llm_...`）。
    """
    return {
        "focus": "宏观",
        "user_query": query,
        "data_points": [_dp("CPI", cpi), _dp("us_fed_rate", 5.25)],
    }


# ======================================================================
# 一、A08（analysis/base.py）：anchor 只放问句，数据走 scope
# ======================================================================

@pytest.mark.asyncio
async def test_a08_anchor_is_the_question_and_excludes_skeleton_and_data():
    """★ A08 的 anchor = **只放问句**；骨架与数据都**不许**进去。

    ## 这条判据自证非空（否则"不含骨架"可能只是因为 anchor 是空的）

    它**同时**断言骨架标记与数据值**在 prompt 里存在** —— 所以
    "anchor 里没有它们"是真的被排除了，不是"恰好两边都没有"。
    """
    from src.domain.agents.analysis.macro.agent import MacroAnalysisAgent

    gw = _FakeGateway({"conclusion": "结构性分析", "confidence": "medium",
                       "cycle_position": "不明确", "liquidity": "中性",
                       "key_points": ["x"], "risks": ["y"]})
    await MacroAnalysisAgent(gw).execute(AgentInput(
        task_id="t", tenant_id="tenant_001",
        payload=_macro_payload("为什么最近猪肉价格波动这么大")))

    assert len(gw.calls) == 1, "没调到 LLM，看不到 anchor"
    call = gw.calls[0]
    anchor = call.get("anchor", "")
    assert anchor, "A08 没传 anchor（或传了空串）—— L3 永远不会生效"

    prompt = call["prompt"]
    # ① 问句必须在 anchor 里（语义可比的那部分）
    assert "为什么最近猪肉价格波动这么大" in anchor
    # ② 骨架必须**在 prompt 里、不在 anchor 里**（自证非空）
    for marker in ("### 规则", "### 时效红线", "## 任务要求", "## 专业技能指引"):
        assert marker in prompt, f"前提不成立：prompt 里本该有骨架标记 {marker}"
        assert marker not in anchor, (
            f"骨架标记 {marker} 进了 anchor —— 骨架占整 prompt 79.6%，"
            "带上它两条完全不同的问题相似度能到 0.93~0.97 ⇒ 必然串答案")
    # ③ **数据不许进 anchor**（它必须走 scope 精确匹配）
    assert "5.25" in prompt, "前提不成立：prompt 里本该有数据"
    assert "5.25" not in anchor, (
        "数据进了 anchor —— 一个数字几乎不移动语义向量，"
        "换数据时 embedding 余弦仍会 ≥ 阈值 ⇒ 返回上一批数据的结论")
    # ④ 数据指纹必须在 scope 上（否则上面那条只是"没放进去"，没有替代机制）
    assert len(call.get("scope_extra", "")) == 16, (
        f"scope_extra 不是 16 位指纹：{call.get('scope_extra')!r}")


@pytest.mark.asyncio
async def test_a08_data_change_moves_the_scope_and_blocks_reuse(tmp_dir):
    """★★ **同问句、换数据 ⇒ 必须未命中**，而且靠的是 scope（精确）不是 anchor。

    ## 为什么这条是本次最贵的判据

    第一版把数据放进 `anchor`，并在注释里断言"换了数据就不会命中"。
    **端到端实测直接推翻了它**：同一问句、只把 CPI 从 0.5 改成 9.9，
    anchor 文本确实变了，但 **embedding 余弦仍然 ≥ 0.80** ⇒ L3 照命中 ⇒
    返回上一批数据算出的结论（而新鲜度标注还是今天的）。

    ⇒ 规律：**"内容变了" ≠ "语义向量变了"**。
      凡是"必须完全一致才能复用"的东西都要走 scope。

    这里用**真实缓存**验三层一起失效：不同 scope ⇒ 不同桶 ⇒ 连候选都没有。
    """
    from src.domain.agents.analysis.macro.agent import MacroAnalysisAgent
    from src.infrastructure.llm.cache import LLMCache

    reply = {"conclusion": "c", "confidence": "medium",
             "cycle_position": "不明确", "liquidity": "中性",
             "key_points": [], "risks": []}
    q = "为什么最近猪肉价格波动这么大"

    gw1 = _FakeGateway(reply)
    await MacroAnalysisAgent(gw1).execute(AgentInput(
        task_id="t1", tenant_id="tenant_001", payload=_macro_payload(q, cpi=0.5)))
    gw2 = _FakeGateway(reply)
    await MacroAnalysisAgent(gw2).execute(AgentInput(
        task_id="t2", tenant_id="tenant_001", payload=_macro_payload(q, cpi=9.9)))

    c1, c2 = gw1.calls[0], gw2.calls[0]
    # ① anchor **相同**（问句没变 ⇒ 语义上确实"该复用"）
    assert c1["anchor"] == c2["anchor"], (
        "问句没变，anchor 却变了 —— 那'换个说法该命中'这条价值就没了")
    # ② scope 指纹**必须不同**（数据变了 ⇒ 精确层必须挡住）
    assert c1["scope_extra"] != c2["scope_extra"], (
        "换了数据但指纹相同 —— 精确层挡不住，L3 会按语义相似度误命中")

    # ③ 真缓存验证：不同 scope ⇒ 不同桶 ⇒ 语义层也命中不了
    from src.infrastructure.llm.models import LLMResponse

    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.3)
    resp = LLMResponse(content="旧数据的结论", model_used="m", provider="ollama",
                       prompt_hash="ph", response_hash="rh")
    cache.put("SYS", "p1", resp, agent_id="A08_macro",
              scope=f"reasoning|d={c1['scope_extra']}", anchor=c1["anchor"])
    hit = await cache.aget("SYS", "p2", agent_id="A08_macro",
                           scope=f"reasoning|d={c2['scope_extra']}",
                           anchor=c2["anchor"])
    assert hit is None, (
        "换了数据仍然命中了 —— 说明复用路径没有被 scope 切断，"
        "用户会拿到上一批数据算出来的结论")


# ======================================================================
# 二、A17（decision/recommend）：anchor 含问题，上游结论走 scope
# ======================================================================

def test_a17_anchor_carries_question_and_upstream_goes_to_scope():
    """★ A17：anchor 只放焦点+问句；上游结论与量化参考走 scope 指纹。"""
    from src.domain.agents.decision.recommend.agent import (
        RecommendationAgent,
        RecommendationPayload,
    )

    payload = RecommendationPayload(
        focus="银行板块",
        user_query="未来一个月还有上涨空间吗",
        analyses=[{"agent_id": "A09_meso", "conclusion": "息差压力边际缓解",
                   "confidence": "medium", "result": {}}],
    )
    prompt, anchor, scope_extra = RecommendationAgent(
        _FakeGateway())._render_prompt(payload, react_mode=True)  # noqa: SLF001

    assert "未来一个月还有上涨空间吗" in anchor
    assert "银行板块" in anchor
    # 骨架（输出 schema）必须在 prompt 里、不在 anchor 里 —— 自证非空
    marker = "输出JSON二选一"
    assert marker in prompt, "前提不成立：prompt 里本该有输出 schema"
    assert marker not in anchor, "输出 schema 进了 anchor —— 它占篇幅极大，会稀释变量"
    # 上游结论不许进 anchor（一个数字/一句话的改动不移动语义向量）
    assert "息差压力边际缓解" in prompt, "前提不成立：prompt 里本该有上游结论"
    assert "息差压力边际缓解" not in anchor, "上游结论进了 anchor"
    assert len(scope_extra) == 16, f"scope_extra 不是 16 位指纹：{scope_extra!r}"


def test_a17_upstream_change_moves_the_scope():
    """上游结论变了 ⇒ scope 指纹必须变（否则综合的是上一批上游结论）。"""
    from src.domain.agents.decision.recommend.agent import (
        RecommendationAgent,
        RecommendationPayload,
    )

    def _scope(conclusion: str) -> str:
        p = RecommendationPayload(
            focus="银行板块", user_query="还有空间吗",
            analyses=[{"agent_id": "A09_meso", "conclusion": conclusion,
                       "confidence": "medium", "result": {}}])
        return RecommendationAgent(_FakeGateway())._render_prompt(p)[2]  # noqa: SLF001

    assert _scope("息差缓解") != _scope("息差继续恶化"), (
        "上游结论变了但 scope 指纹相同 —— 会综合上一批上游结论")


@pytest.mark.asyncio
async def test_a17_execute_passes_anchor_and_scope_through():
    """★ 端到端：A17 的 `execute()` 必须把两样都真的交给网关。

    只测 `_render_prompt` 会漏掉"算出来了但没传"——那正是 `CHG-0178`
    结束时"接口齐全、零调用点"的状态。
    """
    from src.domain.agents.decision.recommend.agent import RecommendationAgent

    gw = _FakeGateway({"conclusion": "结论", "confidence": "medium",
                       "stance": "中性", "key_logic": [], "catalysts": [],
                       "risks": [], "monitoring_points": [],
                       "conflicts_resolved": [], "data_gaps": [],
                       "position_advice": None, "expected_return_3_6m": None})
    await RecommendationAgent(gw).execute(AgentInput(
        task_id="t17", tenant_id="tenant_001",
        payload={"focus": "银行板块", "user_query": "还有空间吗",
                 "analyses": [{"agent_id": "A09_meso", "conclusion": "息差缓解",
                               "confidence": "medium", "result": {}}]}))
    assert gw.calls, "A17 没调 LLM"
    call = gw.calls[0]
    assert call.get("anchor"), "A17 的 execute() 没把 anchor 传给网关"
    assert "还有空间吗" in call["anchor"]
    assert len(call.get("scope_extra", "")) == 16, (
        "A17 的 execute() 没把 scope_extra 传给网关")


# ======================================================================
# 三、测试环境不许意外走真网络
# ======================================================================

def test_env_guard_keeps_rerank_off_for_settings_built_in_tests():
    """★ `conftest` 的 autouse 夹具必须让测试里的 `Settings()` 默认**关掉** L3。

    ## 为什么需要它（`CHG-0180`）

    `LLM_EMBED_RERANK_ENABLED` 的**代码默认值是 True**（精排已上线），
    而 `LLMGateway` 会按 `Settings` 建**真的** `EmbeddingClient` ——
    本机 `.env` 里有 `MOSS_SILICONFLOW_API_KEY`，所以任何走 `Settings()`
    构造网关的用例都可能**真的出网**。

    今天它还没出网，只因为"调用点没传 anchor"挡住了 —— 那是**隐式安全**。
    这条判据把它变成显式。
    """
    from src.core.config import Settings

    assert Settings().llm_embed_rerank_enabled is False, (
        "测试环境里 Settings() 的精排却是开着的 —— conftest 的 autouse "
        "环境变量没生效，将来某个用例一旦传 anchor 就会真出网")

    # 显式传参必须能盖过它（pydantic-settings：init 参数优先于环境变量）
    assert Settings(llm_embed_rerank_enabled=True).llm_embed_rerank_enabled is True
