"""**复用率**的守护测试（`CHG-0208`）。

## 这个文件为一次"从来没量过"而写

三级缓存做了很多轮（`CHG-0178` 建三级、`CHG-0199` 量模型、`CHG-0201` 定量召回、
`CHG-0203` 换判定器、`CHG-0205` 回填 5,007 条向量），但**"缓存到底有没有在用"
一个数都没有**：

    `l3_attempts` 是"走到 L3 的查找数"，**不是查找总数** ⇒ 做不了分母
    L1 精确命中的那些**根本不进 L3** ⇒ 分母缺一大块

于是"复用率是多少"答不出来。而 `scripts/cache_health.py` 一上来就抓到
**5,007 条里 1,928 条静默过期、42 个桶只剩 2 个** —— 正因为没计数器看得见。

## 这个文件守的三件事

1. ★★ **不变量**：`hits_exact + hits_semantic + misses == lookups`，且 `misses ≥ 0`。
   计数只写在**两处入口**（`get` / `aget` 各一处 `+= 1`）+ **一处分类**（`_mark`）。
   漏掉任何一条早退路径，这条不变量就会破 —— 所以它是最有效的那个判据。
2. **"没量到" ≠ "量到 0"**：一次查找都没发生时 `reuse_rate` 必须是 `None`，
   不能是 `0.0`（`0.0` 会被读成"缓存完全没用"）。
3. **早退也要计数**：`semantic_cache=False`、没有候选、判定未命中 ——
   这些都**是一次查找**，不记就少算（而少算的症状是"复用率虚高"）。

跑法：
    uv run python -m pytest tests/unit/test_cache_reuse_rate.py -q
"""
from __future__ import annotations

import pytest

from src.infrastructure.llm.cache import LLMCache
from src.infrastructure.llm.models import LLMResponse

SYS = "你是资深分析师。请严格按以下JSON schema输出。" * 6
P = "今天A股市场怎么样？"
ANCHOR = "今天A股市场怎么样？"
_VEC = [0.1, 0.2, 0.3, 0.4]


def _resp() -> LLMResponse:
    return LLMResponse(content="答案", model_used="m", provider="p",
                       prompt_hash="ph", response_hash="rh")


class _ConstantEmbed:
    """替身精排器：**对任何文本都返回同一个向量**。

    这样"查询 anchor"与"已存 anchor"的余弦恒为 1.0 ⇒ 语义命中是确定的，
    不依赖任何模型行为。⚠️ 必须实现 `configured` + `await embed(text)`。
    """

    configured = True

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, _text: str) -> list[float]:
        self.calls += 1
        return list(_VEC)

    def stats(self) -> dict[str, object]:
        return {"calls": self.calls}


class _DeadEmbed(_ConstantEmbed):
    """永远拿不到向量 ⇒ 逼所有查找走"未命中"。"""

    async def embed(self, _text: str) -> None:  # type: ignore[override]
        self.calls += 1
        return None


def _cache(tmp_path, embed=None) -> LLMCache:
    return LLMCache(cache_dir=str(tmp_path), ttl_hours=1.0,
                    semantic_threshold=0.85,
                    embed_client=embed, embed_threshold=0.80, recall_k=12)


def _seed(c: LLMCache) -> None:
    c.put(SYS, P, _resp(), agent_id="A08_macro", scope="s",
          anchor=ANCHOR, embedding=list(_VEC))


def _invariant(c: LLMCache) -> dict:
    st = c.stats()
    assert st["hits_exact"] + st["hits_semantic"] + st["misses"] == st["lookups"], (
        f"不变量破了：{st['hits_exact']} + {st['hits_semantic']} + "
        f"{st['misses']} != {st['lookups']} —— 说明有条路径没计数")
    assert st["misses"] >= 0, (
        f"misses 是负的（{st['misses']}）⇒ 有 `_mark` 被算在了 `lookups` 之外")
    return st


def test_no_lookup_reports_none_not_zero(tmp_path) -> None:
    """★ **没量到 ≠ 量到 0**：一次都没查时复用率是 `None`，不是 `0.0`。

    `0.0` 会被读成"缓存完全没用"（一个**很强的**结论），而真相是"还没跑过"。
    这条纪律在本仓库反复出现（`calls=0 & failures>0` 与两者皆 0 必须分开读）。
    """
    st = _cache(tmp_path).stats()
    assert st["lookups"] == 0
    assert st["reuse_rate"] is None, "没查过却报了 0% 复用率 —— 那是编的"
    assert st["exact_rate"] is None
    assert st["misses"] == 0


def test_exact_hit_is_counted_as_exact(tmp_path) -> None:
    """L1 精确命中 ⇒ 记在 `hits_exact`，**不是** `hits_semantic`。

    两者混在一个数里，就看不出"缓存主要靠字面命中还是靠语义命中"——
    而这两者的运维动作完全不同（前者调 TTL、后者调阈值/判定器）。
    """
    c = _cache(tmp_path)
    _seed(c)
    hit = c.get(SYS, P, agent_id="A08_macro", scope="s", anchor=ANCHOR)
    assert hit is not None and hit.cache_hit is True
    st = _invariant(c)
    assert (st["lookups"], st["hits_exact"], st["hits_semantic"], st["misses"]) \
        == (1, 1, 0, 0)
    assert st["reuse_rate"] == 1.0 and st["exact_rate"] == 1.0


@pytest.mark.asyncio
async def test_semantic_hit_is_counted_as_semantic(tmp_path) -> None:
    """L1 未中 ⇒ L2/L3 语义命中 ⇒ 记在 `hits_semantic`。"""
    c = _cache(tmp_path, embed=_ConstantEmbed())
    _seed(c)
    # 换个说法 ⇒ L1 不中；但替身对任何文本给同一向量 ⇒ 语义一定命中
    other = "麻烦看下今日大盘行情"
    hit = await c.aget(SYS, other, agent_id="A08_macro", scope="s", anchor=other)
    assert hit is not None and hit.cache_hit is True
    st = _invariant(c)
    assert (st["lookups"], st["hits_exact"], st["hits_semantic"]) == (1, 0, 1)
    assert st["reuse_rate"] == 1.0 and st["exact_rate"] == 0.0


@pytest.mark.asyncio
async def test_early_exit_still_counts_a_lookup(tmp_path) -> None:
    """★★ **早退路径也是一次查找** —— 这是最容易漏的一处。

    `semantic_cache=False` 时 `aget` **在 L1 之后就返回**。
    若那一行不计数，分母就少一块 ⇒ **复用率虚高**，而且不报错。
    """
    c = _cache(tmp_path, embed=_DeadEmbed())
    assert await c.aget(SYS, P, agent_id="A08_macro", scope="s",
                        semantic_cache=False) is None
    st = _invariant(c)
    assert (st["lookups"], st["misses"]) == (1, 1), (
        "关掉语义层的那次查找没被计入分母")
    assert st["reuse_rate"] == 0.0
    assert st["semantic_disabled"] == 1


@pytest.mark.asyncio
async def test_both_entry_points_count(tmp_path) -> None:
    """**同步 `get` 与异步 `aget` 都要计数** —— 只有两个入口。

    漏掉一个的症状是"复用率只有实际的一半"，而两边的调用方都存在
    （`gateway` 走 `aget`，同步调用方与测试走 `get`）。
    """
    c = _cache(tmp_path, embed=_DeadEmbed())
    c.get(SYS, P, agent_id="A08_macro", scope="s")                   # 同步
    await c.aget(SYS, P, agent_id="A08_macro", scope="s")            # 异步
    st = _invariant(c)
    assert st["lookups"] == 2, "有一个入口没计数"
    assert st["misses"] == 2


@pytest.mark.asyncio
async def test_invariant_holds_across_a_mixed_sequence(tmp_path) -> None:
    """★ 混合序列下不变量恒成立（这是本文件的核心判据）。

    造出四种结局各若干：精确命中 / 语义命中 / 未命中 / 关掉语义层。
    """
    c = _cache(tmp_path, embed=_ConstantEmbed())
    _seed(c)
    for _ in range(3):                                               # 精确 ×3
        c.get(SYS, P, agent_id="A08_macro", scope="s", anchor=ANCHOR)
    for i in range(2):                                               # 语义 ×2
        q = f"麻烦看下今日大盘行情（{i}）"
        assert await c.aget(SYS, q, agent_id="A08_macro", scope="s",
                            anchor=q) is not None
    assert await c.aget(SYS, "另一件事", agent_id="A08_macro", scope="s",
                        semantic_cache=False) is None                 # 早退 ×1
    assert c.get("别的系统", "别的问句", agent_id="A08_macro",
                 scope="s") is None                                   # 未命中 ×1

    st = _invariant(c)
    assert st["lookups"] == 7, st
    assert st["hits_exact"] == 3, st
    assert st["hits_semantic"] == 2, st
    assert st["misses"] == 2, st
    assert st["reuse_rate"] == round(5 / 7, 4), st
