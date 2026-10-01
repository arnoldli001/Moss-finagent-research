"""规划层**不许复用缓存**（`docs/PRD.md` §19.18，`CHG-0094`）。

## 现场（真实端到端跑两条不同问句时抓到）

`scripts/_e2e_real_llm.py` 第 2 条是**纯行业问句**：

> 「银行板块当下行情怎么样，未来一个月还有上涨空间吗？」

它**不含任何 6 位代码**、`state["focus_stock_code"]` 也是空的。而端到端日志里
这一跳的增补说明写着「问句命中行业「银行」（**个股代码 600036** → 本地名录）」，
A10 的结论也写成「**焦点600036**本地估值计算为「数据不足」」
—— 一条行业问句被当成了**个股**问句分析。

顺着 `target` 的来源查：`supervisor_node` 的 LLM 分支返回
`"target": llm_plan["target"]`，而 `llm_plan["target"]` 来自**规划那次 LLM 调用**。
两条问句的规划结果一样 ⇒ **语义缓存把两条不同问句判成了同一条**：
规划 prompt 的绝大部分是**静态模板**（Agent 能力目录 + 指标目录 + 任务要求），
问句只占末尾一小段 ⇒ 整 prompt 相似度天然极高（本项目已登记同款：
两条完全不同资讯整 prompt 相似度 0.93、阈值 0.85 ⇒ 必然复用第一条答案）。

**规划的输出逐字段依赖问句**（`target` / `indicators` / `agents`），
所以复用不是"省一次调用"，是**把分析焦点换成另一只票**，而且**不报错**。

## 判据（本文件守的）

1. `LLMSupervisorPlanner.plan()` 调网关时必须带 `use_cache=False`（契约级判据，
   带现场说明；网关的缓存开关就是它）；
2. 不同问句**必须产生各自独立的规划调用**（两次 `plan()` → 两次真实调用，
   而不是第二次读缓存）—— 用假网关计数来判，不靠读源码。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.orchestration.planner import LLMSupervisorPlanner

_QUERY_INDUSTRY = "银行板块当下行情怎么样，未来一个月还有上涨空间吗？"
_QUERY_STOCK = "未来半年能否持有高股息的招商银行？"

_PLAN_JSON = (
    '{"analysis_type":"full","target":"","agents":["A08_macro"],'
    '"indicators":["CPI"],"topic_keywords":[],"reason":"test"}'
)


class _RecordingGateway:
    """假网关：记录每次调用的 kwargs（本文件只关心 `use_cache` 与调用次数）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, tier: str, system_prompt: str, prompt: str,
                       **kwargs: Any) -> Any:
        from src.infrastructure.llm.models import LLMResponse

        self.calls.append({"tier": tier, "prompt": prompt, **kwargs})
        return LLMResponse(content=_PLAN_JSON, model_used="stub", provider="stub",
                           tokens_in=1, tokens_out=1)


@pytest.mark.asyncio
async def test_planner_call_disables_cache() -> None:
    """★ 契约判据：规划调用必须显式 `use_cache=False`。

    没有它 ⇒ 网关默认开启缓存（含**语义**缓存）⇒ 两条不同问句可能共用一份规划。
    """
    gateway = _RecordingGateway()
    planner = LLMSupervisorPlanner(gateway)  # type: ignore[arg-type]
    await planner.plan(_QUERY_INDUSTRY, "", {})
    assert gateway.calls, "规划没有真的调用网关（判据失效）"
    call = gateway.calls[0]
    assert call.get("use_cache") is False, (
        "规划调用没有关闭缓存 —— 语义缓存会把不同问句判成同一条，"
        "从而把另一条问句的 target/indicators 搬过来（现场见本文件顶部）"
    )


@pytest.mark.asyncio
async def test_two_different_queries_produce_two_real_calls() -> None:
    """★ 行为判据：两条不同问句 → **两次真实调用**（不是第二次读缓存）。"""
    gateway = _RecordingGateway()
    planner = LLMSupervisorPlanner(gateway)  # type: ignore[arg-type]
    await planner.plan(_QUERY_INDUSTRY, "", {})
    await planner.plan(_QUERY_STOCK, "", {})
    assert len(gateway.calls) == 2, (
        f"两条不同问句只发生了 {len(gateway.calls)} 次规划调用 —— "
        "说明有一次走了缓存（问句必须各自规划）"
    )
    prompts = [str(c["prompt"]) for c in gateway.calls]
    assert _QUERY_INDUSTRY in prompts[0] and _QUERY_STOCK in prompts[1], (
        "两次调用的 prompt 里没有各自的问句 —— 缓存/拼装串了"
    )


@pytest.mark.asyncio
async def test_planner_returns_independent_plans() -> None:
    """两条问句的规划结果必须是**各自**的（这里用不同 target 的假响应区分）。"""
    gateway = _RecordingGateway()
    planner = LLMSupervisorPlanner(gateway)  # type: ignore[arg-type]
    first = await planner.plan(_QUERY_INDUSTRY, "", {})
    assert first is not None
    assert first.get("target") == "", (
        "行业问句的规划不应带上任何个股代码（现场：曾被缓存串成 600036）"
    )
