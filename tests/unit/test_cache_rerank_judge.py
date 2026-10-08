"""`CHG-0203`：**判定权从 bi-encoder 换到 cross-encoder** —— 行为判据（不联网）。

## 这一层为什么必须有判据

`CHG-0199` 实测（同一批 40 对）：

    判定 AUC：`BAAI/bge-m3` **0.573**（≈抛硬币） → `BAAI/bge-reranker-v2-m3` **0.775**
    操作点  ：现行 @0.80 = 召回 50% / 假阳 65%
              reranker @0.95 = 召回 60% / 假阳 **15%**

但"接线了"和"生效了"是两件事（本项目**反复**栽在这里）：
判定器注入了、代码也调了，**判定分却还是 embedding 的** —— 这种错
**不会报错、不会变慢**，只会让质量停在地板上。

## 判据怎么认出真问题（不 grep 源码）

从**替身记下的 kwargs** 与 **`stats()` 的计数**看行为：
`_FakeRerank.calls` / `_FakeRerank.last_docs` 记的是"它到底被调了没有、拿的是什么文本"。
"""
from __future__ import annotations

import math

import pytest

from src.infrastructure.llm.cache import LLMCache, normalize_text
from src.infrastructure.llm.models import LLMResponse


def _resp(content: str = "答案") -> LLMResponse:
    return LLMResponse(content=content, model_used="qwen3.5:4b", provider="ollama",
                       prompt_hash="ph", response_hash="rh")


class _FakeEmbed:
    """替身 bi-encoder：按（规范化后的）文本查表。"""

    def __init__(self, table: dict[str, list[float]] | None = None,
                 *, fail: bool = False) -> None:
        self.configured = True
        self.calls = 0
        self._table = table or {}
        self._fail = fail

    async def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        return None if self._fail else self._table.get(normalize_text(text))

    def stats(self) -> dict[str, object]:
        return {"configured": True, "calls": self.calls, "failures": 0}


class _FakeRerank:
    """替身 cross-encoder：按 doc → 分数的表返回（缺省用 `default`）。

    ⚠️ **契约与真客户端一致**：返回**与 `documents` 同序同长**的分数列表，
    不可用时 `None`。替身若返回别的形状，测的就不是真实契约。
    """

    def __init__(self, table: dict[str, float] | None = None, *,
                 default: float = 0.0, fail: bool = False) -> None:
        self.configured = True
        self.calls = 0
        self.last_query = ""
        self.last_docs: list[str] = []
        self._table = table or {}
        self._default = default
        self._fail = fail

    async def rerank(self, query: str, documents: list[str]) -> list[float] | None:
        self.calls += 1
        self.last_query = query
        self.last_docs = list(documents)
        if self._fail:
            return None
        return [self._table.get(d, self._default) for d in documents]

    def stats(self) -> dict[str, object]:
        return {"configured": True, "calls": self.calls, "failures": 0}


def _vec_cos(c: float) -> list[float]:
    """构造与 `[1.0, 0.0]` 余弦恰为 `c` 的二维向量。"""
    return [c, math.sqrt(max(0.0, 1.0 - c * c))]


ANCHOR_A = "贵州茅台的投资价值如何"
ANCHOR_B = "帮我分析下茅台值不值得买"


# ======================================================================
# 一、判定权真的换层了吗
# ======================================================================

@pytest.mark.asyncio
async def test_cross_encoder_takes_over_the_decision(tmp_dir):
    """★ 给了 rerank 客户端 ⇒ **判定分来自它**，并且它收到的是**候选的原始文本**。

    "收到原始文本"这一半同样重要：cross-encoder 的全部优势就来自
    "两段文本拼在一起过模型"，若传的是向量或空串，它会退化成噪声 ——
    而**不报错**。
    """
    fake_embed = _FakeEmbed({normalize_text(ANCHOR_A): [1.0, 0.0],
                             normalize_text(ANCHOR_B): _vec_cos(0.10)})
    # embedding 余弦只有 0.10（远低于 0.80）—— 若判定还走 embedding，**必不命中**
    fake_rr = _FakeRerank({ANCHOR_A: 0.99})
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.0,
                     embed_client=fake_embed, embed_threshold=0.80,
                     rerank_client=fake_rr, rerank_threshold=0.95)
    cache.put("SYS", "P1", _resp("茅台结论"), agent_id="A08_macro",
              anchor=ANCHOR_A, embedding=[1.0, 0.0])

    hit = await cache.aget("SYS", "P2", agent_id="A08_macro", anchor=ANCHOR_B)
    assert hit is not None and hit.content == "茅台结论", (
        "cross-encoder 给了 0.99 却没命中 —— 判定还是走的 embedding 余弦"
        "（那条路只有 0.10）")
    assert fake_rr.calls == 1, "判定器一次都没被调用"
    assert fake_rr.last_docs and all(d for d in fake_rr.last_docs), (
        f"判定器收到的文档是空的/空的文本：{fake_rr.last_docs!r} —— "
        "cross-encoder 不吃向量，传空串等于把它的能力丢掉")
    assert ANCHOR_A in fake_rr.last_docs, (
        f"候选的原始文本没传进去：{fake_rr.last_docs!r}")
    s = cache.stats()
    assert s["judge"] == "cross-encoder"
    assert s["rerank_attempts"] == 1 and s["l3_judged"] == 1


@pytest.mark.asyncio
async def test_the_ruler_changes_with_the_judge(tmp_dir):
    """★★ **判定分的尺子随判定器变** —— 用错尺子不会报错，只会整体偏移。

    构造：embedding 余弦恰好 **0.90**、rerank 分恰好 **0.90**。

        · 只有 embedding ⇒ 0.90 ≥ 0.80 ⇒ **命中**
        · 换了 reranker  ⇒ 0.90 < 0.95 ⇒ **不命中**

    同一对数、同一个分数、结论相反 —— 这正是"尺子换了"的可观测形态。
    """
    table = {normalize_text(ANCHOR_A): [1.0, 0.0],
             normalize_text(ANCHOR_B): _vec_cos(0.90)}

    # ① 只有 embedding：0.90 ≥ 0.80 ⇒ 命中
    only_embed = LLMCache(cache_dir=f"{tmp_dir}/a", ttl_hours=1,
                          semantic_threshold=0.0, embed_client=_FakeEmbed(dict(table)),
                          embed_threshold=0.80)
    only_embed.put("SYS", "P1", _resp("A"), agent_id="A08_macro",
                   anchor=ANCHOR_A, embedding=[1.0, 0.0])
    assert await only_embed.aget("SYS", "P2", agent_id="A08_macro",
                                 anchor=ANCHOR_B) is not None, (
        "前提不成立：embedding 余弦 0.90 本该 ≥ 0.80 命中")

    # ② 换了 reranker，它给 0.90 < 0.95 ⇒ 不命中
    with_rr = LLMCache(cache_dir=f"{tmp_dir}/b", ttl_hours=1,
                       semantic_threshold=0.0, embed_client=_FakeEmbed(dict(table)),
                       embed_threshold=0.80,
                       rerank_client=_FakeRerank({ANCHOR_A: 0.90}),
                       rerank_threshold=0.95)
    with_rr.put("SYS", "P1", _resp("A"), agent_id="A08_macro",
                anchor=ANCHOR_A, embedding=[1.0, 0.0])
    assert await with_rr.aget("SYS", "P2", agent_id="A08_macro",
                              anchor=ANCHOR_B) is None, (
        "reranker 给 0.90 却按 embedding 的 0.80 判成了命中 —— "
        "**两把尺子不是一回事**（实测 reranker 正样本 p50 0.9844、"
        "embedding 正样本 p50 0.8604）")


@pytest.mark.asyncio
async def test_prompt_mode_never_calls_the_cross_encoder(tmp_dir):
    """★★★ **护栏必须同时挡住判定层** —— 这条守的是我自己写出来的一个洞。

    `CHG-0180` 的护栏理由是「整 prompt 的相似度普遍偏高（两条无关资讯
    0.96~0.98）」——**那条理由对 cross-encoder 同样成立**：两条无关的长 prompt
    共享同一套骨架与 schema，在 reranker 眼里一样"高度相关"。

    ⇒ 若只挡 embedding 不挡 reranker，换判定器就把护栏**静默绕过**了：
    `cache_hit=True`、耗时更短、不报错。

    ⚠️ **这个洞是我在 `CHG-0203` 里写出来的**，被本文件同批的
    `test_l3_never_runs_in_prompt_mode`（`l3_attempts` 实测 2）当场抓住。
    """
    fake_embed = _FakeEmbed({})
    fake_rr = _FakeRerank(default=0.99)
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.0,
                     embed_client=fake_embed, embed_threshold=0.80,
                     rerank_client=fake_rr, rerank_threshold=0.95)
    cache.put("SYS", "prompt甲", _resp("答案"), agent_id="A08_macro")

    await cache.aget("SYS", "prompt乙", agent_id="A08_macro")   # 不传 anchor
    assert fake_rr.calls == 0, (
        f"prompt 模式调了 cross-encoder {fake_rr.calls} 次 —— "
        "护栏没盖住判定层，整 prompt 的通用高相似会让**任何**候选被接受")
    s = cache.stats()
    assert s["l3_attempts"] == 0, "prompt 模式被记成了 L3 判定尝试"
    assert s["l3_judged"] == 0
    assert s["l3_skipped_prompt_mode"] == 1


# ======================================================================
# 二、失败与降级必须"看得见"
# ======================================================================

@pytest.mark.asyncio
async def test_cross_encoder_failure_falls_back_and_is_counted(tmp_dir):
    """★ 判定器失败 ⇒ **退回 embedding 判定**，并且**记账**。

    退回是必须的（"升级不倒退"）；记账也是必须的 ——
    否则"判定器挂了"会表现成"判定质量下降"，而处置完全不同
    （一个查端点、一个调阈值）。
    """
    fake_embed = _FakeEmbed({normalize_text(ANCHOR_A): [1.0, 0.0],
                             normalize_text(ANCHOR_B): _vec_cos(0.90)})
    fake_rr = _FakeRerank(fail=True)
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.0,
                     embed_client=fake_embed, embed_threshold=0.80,
                     rerank_client=fake_rr, rerank_threshold=0.95)
    cache.put("SYS", "P1", _resp("A"), agent_id="A08_macro",
              anchor=ANCHOR_A, embedding=[1.0, 0.0])

    hit = await cache.aget("SYS", "P2", agent_id="A08_macro", anchor=ANCHOR_B)
    assert hit is not None, (
        "判定器挂了就没命中了 —— 那是**升级即退化**："
        "同一个 0.90 在换层前本来命得中")
    s = cache.stats()
    assert s["rerank_fallbacks"] == 1, (
        "退回了却没记账 ⇒ 「判定器挂了」会长得和「判定质量下降」一样")
    assert s["rerank_attempts"] == 0, "没拿到分数却记成了判定成功"
    assert s["l3_judged"] == 1, "退回后仍算一次真实判定（用的是 embedding 的分）"


@pytest.mark.asyncio
async def test_four_states_are_distinguishable(tmp_dir):
    """★ 四个计数器状态**两两可分** —— 合起来才回答"三级缓存到底跑起来没有"。

        attempts>0 & judged>0            ⇒ L3 真的在判
        attempts>0 & judged=0            ⇒ **每次都在降级**（端点挂了 / 全桶无向量）
        attempts=0 & skipped_prompt>0    ⇒ 调用点没传 anchor
        attempts=0 & skipped_prompt=0    ⇒ 语义层没被走到（或没候选）

    `CHG-0202` 里 **0/5007 带向量却被读成"L3 没跑"** 正是缺了第二行那个状态。
    """
    # 状态 A：真的在判
    ok_embed = _FakeEmbed({normalize_text(ANCHOR_A): [1.0, 0.0],
                           normalize_text(ANCHOR_B): _vec_cos(0.99)})
    a = LLMCache(cache_dir=f"{tmp_dir}/A", ttl_hours=1, semantic_threshold=0.0,
                 embed_client=ok_embed, embed_threshold=0.80,
                 rerank_client=_FakeRerank({ANCHOR_A: 0.99}), rerank_threshold=0.95)
    a.put("SYS", "P1", _resp("A"), agent_id="A08_macro",
          anchor=ANCHOR_A, embedding=[1.0, 0.0])
    await a.aget("SYS", "P2", agent_id="A08_macro", anchor=ANCHOR_B)
    sa = a.stats()
    assert (sa["l3_attempts"], sa["l3_judged"]) == (1, 1), sa

    # 状态 B：进了判定阶段但**没有判定信号**（全桶无向量 + 判定器也挂了）
    b = LLMCache(cache_dir=f"{tmp_dir}/B", ttl_hours=1, semantic_threshold=0.0,
                 embed_client=_FakeEmbed({}), embed_threshold=0.80)
    b.put("SYS", "P1", _resp("A"), agent_id="A08_macro", anchor=ANCHOR_A)
    await b.aget("SYS", "P2", agent_id="A08_macro", anchor=ANCHOR_B)
    sb = b.stats()
    assert (sb["l3_attempts"], sb["l3_judged"]) == (1, 0), (
        f"「每次都在降级」没被记出来：{sb}")

    # 状态 C：调用点没传 anchor
    c = LLMCache(cache_dir=f"{tmp_dir}/C", ttl_hours=1, semantic_threshold=0.0,
                 embed_client=_FakeEmbed({}), embed_threshold=0.80)
    await c.aget("SYS", "P2", agent_id="A08_macro")
    sc = c.stats()
    assert (sc["l3_attempts"], sc["l3_skipped_prompt_mode"]) == (0, 1), sc

    assert sa["judge"] == "cross-encoder"
    assert "rerank_client" in sa and sa["rerank_client"] is not None
