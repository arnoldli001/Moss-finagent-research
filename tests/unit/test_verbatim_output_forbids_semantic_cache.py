"""**输出逐字引用输入 ⇒ 必须禁语义缓存**（PRD §41.13.5，`CHG-0182`）。

## 这条规则（比任何单个调用点的修复都重要）

LLM 缓存的语义层（L2 召回 + L3 Embedding 精排）复用的是**"看起来像同一条"的答案**。
这对"结论/情绪/方向"这类输出是**收益**，但对**逐字引文**类输出是**缺陷**：

    抽取的输出 = 输入里逐字摘出来的片段（evidence_quote / phrases / codes …）
    ⇒ 把 A 输入的答案复用给 B 输入 ⇒ **引文在 B 里根本不存在**
    ⇒ 下游按"逐字可核对"消费时拿到的是**假证据**

本项目实测过它的代价：`CHG-0069` —— 抽取任务 **4/5 条 summary 为空**，
真因就是语义缓存把别的文章的答案当成了这篇的（一度被误归因成"模型惜字"）。

## 为什么**换比较文本（anchor）治不了它**

"正文进相似度"只让相似度算得更准。**同一条新闻被转载改写后，引文照样不在
新正文里** ⇒ 瓶颈是**输出契约**，不是文本表征。

## 本文件守什么

逐个**已识别的**"逐字引文类"调用点，断言它显式传了 `semantic_cache=False`。
新增同类调用点时，**在这里加一行**（参数化清单，见 `MUST_DISABLE`）。

跑法：
    uv run python -m pytest tests/unit/test_verbatim_output_forbids_semantic_cache.py -q
"""
from __future__ import annotations

import json

import pytest

from src.core.models import AgentInput


class _RecordingGateway:
    """记下 `complete()` 的全部 kwargs（判据只看 kwargs，不看源码）。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        from src.infrastructure.llm.models import LLMResponse

        self.calls.append({"task_tier": task_tier, "system": system,
                           "prompt": prompt, **kwargs})
        return LLMResponse(content=self._content, model_used="fake",
                           provider="fake", prompt_hash="ph", response_hash="rh")


# ======================================================================
# 已识别的"逐字引文类"调用点清单（新增同类请加在这里）
# ======================================================================

#: `(名称, 为什么它的输出是逐字引文)`
MUST_DISABLE: tuple[tuple[str, str], ...] = (
    ("A06_extractor",
     "输出 schema 含 evidence_quote「支撑该事件的原文片段（**必须逐字来自输入**，"
     "30字内）」+「硬性要求：evidence_quote 禁止改写或编造」"),
    ("intel_extract",
     "输出含 phrases（逐字子串校验）/ codes（必须原文里真有）/ industries / "
     "stocks[].name（逐字出现）/ summary（最长公共子串 ≥ 阈值）"),
)


@pytest.mark.asyncio
async def test_a06_extractor_disables_semantic_cache() -> None:
    """★ `A06_extractor`（信息层事件抽取）必须带 `semantic_cache=False`。

    它的输出里有 `evidence_quote`，契约写在自己的 schema 里：
    「支撑该事件的原文片段（**必须逐字来自输入**，30字内）」+
    「**硬性要求：evidence_quote 禁止改写或编造**」。

    ⇒ 复用别的资讯的抽取结果 = 给出**原文里不存在的"证据"**。
    """
    from src.domain.agents.info.extractor.agent import ExtractorAgent

    gw = _RecordingGateway(json.dumps({"events": []}, ensure_ascii=False))
    await ExtractorAgent(gw).execute(AgentInput(
        task_id="t06", tenant_id="tenant_001",
        payload={"info_items": [
            {"item_id": "i1", "title": "某公司回购公告",
             "text": "公司拟以自有资金回购股份，金额不超过5亿元。"},
        ]}))

    assert gw.calls, "没调到网关，看不到 kwargs"
    kw = gw.calls[0]
    assert kw.get("semantic_cache") is False, (
        f"`A06_extractor` 没禁语义层（semantic_cache={kw.get('semantic_cache')!r}）"
        " —— 会把别的资讯的 evidence_quote 当成这条的原文证据返回")


def test_the_rule_is_documented_and_the_list_is_not_empty() -> None:
    """规则本身也要有判据：清单非空 + 每条都写清"为什么"。

    为什么值得单独一条：这份清单是**给下一个人的入口**——
    `PRD §41.13.5` 说"新增同类调用点要逐条判断"，而"逐条判断"如果没有
    一个**可枚举的落点**，就只是一句口号。清单为空或没有理由，
    下一个人就不知道该加什么、为什么加。
    """
    assert MUST_DISABLE, "清单空了 —— 规则失去落点"
    for name, why in MUST_DISABLE:
        assert name and why, (name, why)
        assert len(why) > 20, f"{name} 的'为什么'太短，等于没写：{why!r}"
