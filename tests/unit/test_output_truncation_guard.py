"""`CHG-0250`：**输出撞上限（截断）**必须自成一态，且 A06 要按"更小的要求"重试一次。

## 这个文件在防什么

2026-10-16 用户报障（**与昨天 `CHG-0249` 的 `char 0` 是两个不同根因**）：

    A06_extractor LLM输出非合法JSON: Expecting value: line 224 column 18 (char 5278)
    （用量：model=qwen-flash tier=medium out=2048）

`char 5278` 是**正文深处**（不是 `char 0` 的空正文）⇒ JSON 写到一半被**截断**，
而 `out=2048` **正好等于** `medium` 层预算（`_TIER_OUTPUT_BUDGET`）——
而 prompt 当时写的是「提取**全部**可确认的事件（每条 0-3 个）」：20 多条资讯
⇒ 最多要 60 条 ⇒ **必然写不完** ⇒ 一条都读不出来。

判据分四层：
  ① 判据函数：`out >= 上限` 才算截断；**上限未知时不下判断**（不许猜）；
  ② 文案：错误里必须**说出成因**（"被输出上限截断"），不再只说"非合法JSON"；
  ③ A06 行为：截断 ⇒ **换更小的要求**重试一次；非截断 ⇒ **不重试**（不制造循环）；
  ④ 不许顺手抬 `max_tokens`（层级预算与单跳墙钟配套，见 `test_empty_body_and_cot_budget.py`）。
"""

from __future__ import annotations

import json

import pytest

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput
from src.domain.agents.analysis.base import (
    _usage_suffix,
    output_hit_cap,
    parse_llm_json,
)
from src.infrastructure.llm.gateway import tier_output_budget
from src.infrastructure.llm.models import LLMResponse

TRUNCATED = '{"events": [{"item_id": "i1", "event_type": "earnings"'   # 写到一半
GOOD = json.dumps({"events": [{
    "item_id": "i1", "event_type": "earnings", "subject": "某公司",
    "direction": "positive", "magnitude": "", "event_date": "",
    "evidence_quote": "公司拟回购", "confidence": 0.8}]}, ensure_ascii=False)


class _ScriptedGateway:
    """按脚本依次返回响应；记下每次 `complete()` 的 kwargs。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        self.calls.append({"task_tier": task_tier, "system": system,
                           "prompt": prompt, **kwargs})
        return self._responses.pop(0)


def _resp(content: str, *, out: int = 0, cap: int = 0) -> LLMResponse:
    return LLMResponse(content=content, model_used="fake", provider="fake",
                       tokens_out=out, max_tokens=cap,
                       prompt_hash="ph", response_hash="rh")


def _input(items: int = 3) -> AgentInput:
    return AgentInput(
        task_id="t06", tenant_id="tenant_001",
        payload={"info_items": [
            {"item_id": f"i{n}", "title": f"资讯{n}", "text": "公司公告：拟回购股份。"}
            for n in range(1, items + 1)]},
    )


async def _run(responses: list[LLMResponse]):
    from src.domain.agents.info.extractor.agent import ExtractorAgent

    gw = _ScriptedGateway(responses)
    out = await ExtractorAgent(gw).execute(_input())
    return out, gw


# ===========================================================================
# ① 判据函数：截断只在"顶到上限"时成立
# ===========================================================================


def test_hit_cap_requires_both_numbers() -> None:
    assert output_hit_cap({"tokens_out": 2048, "max_tokens": 2048}) is True
    assert output_hit_cap({"tokens_out": 900, "max_tokens": 2048}) is False
    # ★ 上限未知 ⇒ **不下判断**（宁可不说，也不猜 —— 猜错会把人带去改错地方）
    assert output_hit_cap({"tokens_out": 2048}) is False
    assert output_hit_cap({"tokens_out": 2048, "max_tokens": 0}) is False
    assert output_hit_cap({}) is False
    assert output_hit_cap(None) is False


def test_tier_budget_is_public_and_honest() -> None:
    """层预算取法必须公开可用；取不到的层返回 0（不许返回一个像真的默认值）。"""
    assert tier_output_budget("medium") == 2048
    assert tier_output_budget("light") == 1024
    assert tier_output_budget("查无此层") == 0
    assert tier_output_budget("") == 0


# ===========================================================================
# ② 文案：必须说出"被上限截断"
# ===========================================================================


def test_truncated_json_names_the_real_cause() -> None:
    diag = {"model": "qwen-flash", "task_tier": "medium",
            "tokens_out": 2048, "max_tokens": 2048}
    with pytest.raises(AgentExecutionError) as ei:
        parse_llm_json("A06_extractor", TRUNCATED, diagnostics=diag)
    msg = str(ei.value)
    assert "被输出上限截断" in msg, msg
    assert "上限=2048" in msg, msg
    assert "原样重发" in msg, "必须写明'原样重发必然同样被截断'，否则下一个人会去重试"


def test_non_truncated_failure_keeps_the_old_wording() -> None:
    """反例：`out` 没顶到上限 ⇒ **不许**声称截断（不许把判据放大到所有失败）。"""
    diag = {"model": "qwen-flash", "task_tier": "medium",
            "tokens_out": 900, "max_tokens": 2048}
    with pytest.raises(AgentExecutionError) as ei:
        parse_llm_json("A06_extractor", TRUNCATED, diagnostics=diag)
    msg = str(ei.value)
    assert "被输出上限截断" not in msg, msg
    assert "上限=2048" in msg, "用量事实照旧要给出"


def test_unknown_cap_never_claims_truncation() -> None:
    with pytest.raises(AgentExecutionError) as ei:
        parse_llm_json("A06_extractor", TRUNCATED,
                       diagnostics={"tokens_out": 2048})
    assert "被输出上限截断" not in str(ei.value)


def test_usage_suffix_without_diagnostics_is_unchanged() -> None:
    """老调用点（不传 diagnostics）的文案**逐字不变**。"""
    assert _usage_suffix(None) == ""


# ===========================================================================
# ③ A06 行为：截断换更小的要求；非截断不重试
# ===========================================================================


@pytest.mark.asyncio
async def test_a06_retries_with_a_smaller_ask_when_truncated() -> None:
    out, gw = await _run([
        _resp(TRUNCATED, out=2048, cap=2048),      # 第一次：撞上限、JSON 没收尾
        _resp(GOOD, out=120, cap=2048),            # 第二次：更小的要求 ⇒ 写完
    ])
    assert len(gw.calls) == 2, f"应重试一次，实得 {len(gw.calls)} 次"
    first, second = gw.calls
    assert second["prompt"] != first["prompt"], "原样重发必然同样被截断"
    assert "被长度上限截断" in second["prompt"], second["prompt"][-200:]
    assert "最多只输出 6 条" in second["prompt"], second["prompt"][-200:]
    # 既有契约不许被顺手改掉
    assert second.get("reasoning_effort") == "none"
    assert second.get("semantic_cache") is False
    assert second.get("json_mode") is True
    # ★ 不许自己抬上限（层级预算与单跳墙钟配套）
    assert "max_tokens" not in second, second.keys()
    assert out.result["stats"]["total"] == 1, out.result


@pytest.mark.asyncio
async def test_a06_does_not_retry_when_not_truncated() -> None:
    """非截断的解析失败 ⇒ **只调一次**（换个小要求也治不了"写坏了"）。"""
    with pytest.raises(AgentExecutionError):
        await _run([_resp(TRUNCATED, out=77, cap=2048)])
    # 用另一个网关断言调用次数（上面的异常没带出 gw）
    from src.domain.agents.info.extractor.agent import ExtractorAgent

    gw = _ScriptedGateway([_resp(TRUNCATED, out=77, cap=2048)])
    with pytest.raises(AgentExecutionError):
        await ExtractorAgent(gw).execute(_input())
    assert len(gw.calls) == 1, f"不许重试，实得 {len(gw.calls)} 次"


@pytest.mark.asyncio
async def test_a06_prompt_asks_for_a_bounded_number_of_events() -> None:
    """★ 事前就把"要多少"写进 prompt —— 这是**消除**截断，而不是事后补救。"""
    out, gw = await _run([_resp(json.dumps({"events": []}), out=10, cap=2048)])
    prompt = gw.calls[0]["prompt"]
    assert "最多输出 16 条事件" in prompt, prompt[:400]
    assert "全部可确认的事件" not in prompt, "旧措辞（要全部）必须被换掉"
    assert out.result["stats"]["total"] == 0


@pytest.mark.asyncio
async def test_a06_diagnostics_carry_the_effective_cap() -> None:
    """截断文案要成立，诊断里必须带**上限**；上限优先取响应自带值。"""
    from src.domain.agents.info.extractor.agent import _extract_diagnostics

    assert _extract_diagnostics(_resp("", out=2048, cap=1234), "medium")["max_tokens"] == 1234
    # 响应没带 ⇒ 退回本层默认预算（不是 0，否则判据永远不成立）
    assert _extract_diagnostics(_resp("", out=2048, cap=0), "medium")["max_tokens"] == 2048
    # 连层预算都取不到 ⇒ 0 = 未知（此时 output_hit_cap 不下判断）
    assert _extract_diagnostics(_resp("", out=2048, cap=0), "查无此层")["max_tokens"] == 0
