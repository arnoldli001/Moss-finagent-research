"""三级缓存（精确哈希 → 3-gram 召回 → Embedding 精排）的守护测试。

## 这套断言要守住七件事（`CHG-0178`）

1. **★ 骨架不再决定命中**：两条**完全不同**的资讯、共用同一段长骨架时，
   **旧口径（整 prompt）会命中**（这正是 `CHG-0069` 的现场），
   **新口径（只比 anchor）必须不命中**；
2. **`anchor` 缺省 ⇒ 行为与改动前逐字一致**（旧调用点不受影响）；
3. **L2 只召回、不判定**：3-gram 分数低于旧阈值但 embedding 高的候选
   **必须能命中**（否则"三级"只是把阈值换个名字）；
4. **精排失败一律 fail-open**（退化成未命中，**绝不抛**）；
5. **候选全无向量 ⇒ 退回阈值规则**（开启精排不许把老缓存的命中全丢掉）；
6. **查询向量记忆化**（一次问句只付一次网络往返）；
7. **可观测**：`stats()` 必须能回答"精排到底跑没跑"，
   且**没配精排器时报 `None` 而不是 0**（"没量到" ≠ "量到 0"）。

跑法：
    uv run python -m pytest tests/unit/test_llm_cache_three_level.py -q
"""
from __future__ import annotations

import math

import pytest

from src.infrastructure.llm.cache import (
    LLMCache,
    _ngram_vector,
    _SemanticIndex,
    cosine_similarity,
    normalize_text,
)
from src.infrastructure.llm.embedding import (
    EmbeddingClient,
    cosine,
    decode_vector,
    encode_vector,
)
from src.infrastructure.llm.models import LLMResponse

#: 一段**长且固定**的骨架 —— 模拟真实的 `_SYSTEM` + JSON schema。
_SKELETON = "你是资深分析师。请严格按以下JSON schema输出，不要输出任何解释。" * 18

_BODY_A = "公司发布公告称，拟以自有资金回购股份，回购价格不超过每股30元。"
_BODY_B = "据央视新闻报道，国内首条8英寸碳化硅晶圆生产线正式投产，年产能10万片。"


def _resp(content: str = "答案") -> LLMResponse:
    return LLMResponse(
        content=content, model_used="qwen3.5:4b", provider="ollama",
        prompt_hash="ph", response_hash="rh",
    )


class _FakeEmbed:
    """替身精排器：按（规范化后的）文本查表。

    ⚠️ 它必须实现 `configured` 与 `await embed(text)` 两个成员 ——
    这就是 `LLMCache` 对精排器的**全部**要求（鸭子类型，不 import 具体类）。
    """

    def __init__(self, table: dict[str, list[float]] | None = None,
                 *, fail: bool = False) -> None:
        self.configured = True
        self.calls = 0
        self._table = table or {}
        self._fail = fail

    async def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        if self._fail:
            return None
        return self._table.get(normalize_text(text))

    def stats(self) -> dict[str, object]:
        return {"fake": True, "calls": self.calls}


# ======================================================================
# 一、★ 骨架不再决定命中（`CHG-0069` 的回归）
# ======================================================================

@pytest.mark.asyncio
async def test_skeleton_no_longer_decides_the_hit(tmp_dir):
    """★★ 同一个长骨架下的两条**完全不同**的资讯**不许**互相复用。

    ## 这条测试自证前提（这是它最重要的性质）

    它先断言"**旧口径下确实会命中**" —— 如果哪天 `normalize_text` / 3-gram
    的实现变了、整 prompt 相似度掉到阈值以下，这条断言会**先红**，
    告诉我们"这次跑的已经不是那个现场了"，而不是**静默地绿**。

    ## 现场（`CHG-0069` / `CHG-0094`，本项目真实事故）

    抽取 prompt 的固定骨架实测占 **79.6%**（跨桶 34.7%~419%）⇒
    两条完全不同的资讯整 prompt 相似度 **0.93~0.97**（远超阈值 0.85）
    ⇒ 第 2 条起必然复用第 1 条的答案。规划层甚至因此把**一条银行板块问句
    分析成了 600036**，最后只能给那一跳关掉缓存（`use_cache=False`）。

    修法不是"换更强的表征"，而是**换比较的文本**：
        只比 anchor：不同事件 3-gram **0.0000** / embedding **0.40~0.50**。
    """
    whole = cosine_similarity(
        _ngram_vector(normalize_text(_SKELETON + _BODY_A)),
        _ngram_vector(normalize_text(_SKELETON + _BODY_B)),
    )
    assert whole >= 0.85, (
        f"前提不成立：整 prompt 的 3-gram 相似度只有 {whole:.4f}（<0.85）—— "
        "说明这条用例已经**不再是 CHG-0069 的现场**了，需要重新构造骨架长度")

    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.85)

    # ① **旧口径的缺陷现场**（prompt 模式：不传 anchor，比较整 prompt）
    #    —— 这一段必须**能命中**，否则说明我们已经不在那个现场了
    cache.put("SYS", _SKELETON + _BODY_A, _resp("A的答案"),
              agent_id="intel_extract")
    old = cache.get("SYS", _SKELETON + _BODY_B, agent_id="intel_extract")
    assert old is not None and old.content == "A的答案", (
        "前提不成立：整 prompt 口径下本应命中 —— 若这条红了，"
        "说明旧口径已经不复现该缺陷，本用例的对照失效")

    # ② **新口径**（anchor 模式）：同一条资讯**必须**命中
    cache.put("SYS", _SKELETON + _BODY_A, _resp("A的答案2"),
              agent_id="intel_extract", anchor=_BODY_A)
    same = await cache.aget("SYS", _SKELETON + _BODY_A,
                            agent_id="intel_extract", anchor=_BODY_A)
    assert same is not None, "同一个 anchor 都命不中 —— 改造把语义层弄失效了"

    # ③ **新口径**：两条完全不同的资讯**必须不命中** ← 这就是修复本身
    diff = await cache.aget("SYS", _SKELETON + _BODY_B,
                            agent_id="intel_extract", anchor=_BODY_B)
    assert diff is None, (
        "两条完全不同的资讯仍然互相复用了 —— anchor 没有被用作比较文本")


@pytest.mark.asyncio
async def test_anchor_and_prompt_modes_never_mix(tmp_dir):
    """★ 两种**比较空间**不许混（这是我自己踩出来的坑，`CHG-0178`）。

    比较文本有两个来源，它们**不可比**：

        anchor 模式   比 `normalize(anchor)`（几十字的变量正文）
        prompt 模式   比 `normalize(system + prompt)`（几百字的整 prompt）

    混在一个桶里不会造成**错命中**（跨模式余弦趋近 0），但会造成
    "**写进去的条目查不出来**"，而且**不报错** —— 我的第一版就是
    "写传 anchor、读没传"，语义层静默失效。

    所以把它做成**结构分区**，而不是"靠长度差异碰巧安全"。
    """
    a = "公司拟回购股份，金额不超过五亿元。"
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.6)

    # 写用 anchor 模式
    cache.put("SYS", a, _resp("答案"), agent_id="A08_macro", anchor=a)
    # 读也用 anchor 模式 ⇒ 命中
    assert await cache.aget("SYS", "完全不同的prompt", agent_id="A08_macro",
                            anchor=a) is not None
    # 读用 prompt 模式 ⇒ **不命中**（不可比，不是错命中）
    assert cache.get("SYS", "完全不同的prompt", agent_id="A08_macro") is None, (
        "跨比较空间命中了 —— 两种模式的向量本不可比，命中就是错的")


# ======================================================================
# 二、缺省 anchor ⇒ 行为与改动前逐字一致
# ======================================================================

def test_missing_anchor_keeps_legacy_behaviour(tmp_dir):
    """不传 `anchor` 时，比较文本仍是 `normalize(system+prompt)`（旧行为）。

    这是**向后兼容的判据**：现存 8,463 条缓存与全部旧调用点都不传 anchor，
    它们的行为必须一个字都不变 —— 否则这次改造就是一次静默的全量失效。
    """
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.6)
    cache.put("系统", "请基于宏观流动性与行业景气度，分析贵州茅台的投资价值",
              _resp("结论A"), agent_id="A08_macro")
    hit = cache.get("系统", "请基于宏观流动性与行业景气度，分析五粮液的投资价值",
                    agent_id="A08_macro")
    assert hit is not None and hit.cache_kind == "semantic"


def test_anchor_and_prompt_share_the_same_memo_text(tmp_dir):
    """`put` 与 `aget` 必须用**同一个** `_anchor_text`（防"写读不一致"）。

    写进去的文本与查出来时比的文本若不一致，症状是"行为看着正常、
    只是**永远不命中**" —— 最难查的一类 bug。所以判据钉在"两者相等"上。
    """
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1)
    assert (cache._anchor_text("S", "P", "变量正文")            # noqa: SLF001
            == cache._anchor_text("另一个S", "另一个P", "变量正文"))  # noqa: SLF001
    # 不给 anchor 时回落整 prompt（与旧行为一致）
    assert cache._anchor_text("S", "P", "") == normalize_text("SP")  # noqa: SLF001


# ======================================================================
# 三、L2 只召回、不判定
# ======================================================================

def test_top_k_recalls_without_threshold() -> None:
    """`top_k` **不设阈值**：分数再低也要召回（判定权在 L3）。

    实测依据：同一个全局阈值在 `intel_extract` 桶放行 **100%**、
    在 `mainline_member_pure` 桶放行 **0%** —— 阈值既能"等于没筛"
    也能"等于永久短路"。所以召回层只截断条数、不看分数。
    """
    idx = _SemanticIndex()
    for i, text in enumerate(["aaa", "bbb", "ccc"]):
        idx.add(f"k{i}", "A", "s", text)
    got = idx.top_k(_ngram_vector("zzz"), "A", "s", 12)   # 与谁都无关
    assert len(got) == 3, "关联度为零也必须全部召回（召回层不做判定）"
    assert all(score == 0.0 for _, score in got)
    # K 生效
    assert len(idx.top_k(_ngram_vector("aaa"), "A", "s", 1)) == 1
    # 分桶生效：别的 agent 取不到
    assert idx.top_k(_ngram_vector("aaa"), "B", "s", 12) == []


@pytest.mark.asyncio
async def test_low_threegram_but_high_embedding_still_hits(tmp_dir):
    """★ 3-gram 分数**低于旧阈值**、但 embedding 高的候选**必须能命中**。

    这条是"三级"与"两级"的分水岭：如果 L3 命中的还是 L2 已经筛过的那些，
    那只是把阈值换了个名字。用一对**语义相同、字面完全不同**的文本
    （同义改写）来验：3-gram ≈ 0，embedding 高。
    """
    a = "公司拟回购股份，金额不超过五亿元。"
    b = "该上市公司宣布将以不超五亿元自有资金实施股份回购。"   # 同义改写
    assert cosine_similarity(_ngram_vector(normalize_text(a)),
                             _ngram_vector(normalize_text(b))) < 0.85, \
        "前提不成立：这一对改写稿的 3-gram 相似度不该高于阈值"

    fake = _FakeEmbed({normalize_text(a): [1.0, 0.0],
                       normalize_text(b): [0.98, 0.199]})   # cosine ≈ 0.98
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.99,
                     embed_client=fake, embed_threshold=0.80)
    cache.put("SYS", a, _resp("A的答案"), agent_id="A08_macro",
              anchor=a, embedding=[1.0, 0.0])

    hit = await cache.aget("SYS", b, agent_id="A08_macro", anchor=b)
    assert hit is not None and hit.content == "A的答案", (
        "L3 没有真的在判定 —— 3-gram 低于阈值但 embedding 高的候选被漏掉了")
    assert cache.stats()["l3_hits"] == 1


# ======================================================================
# 四、精排失败必须 fail-open
# ======================================================================

@pytest.mark.asyncio
async def test_rerank_failure_falls_back_and_never_raises(tmp_dir):
    """精排不可用时**退回阈值规则**，且**绝不抛**。

    精排是"锦上添花"的一层，它的失败只该退化成"语义未命中"。
    抛出去就会变成**又一条零痕迹失败路径** —— 本项目刚修掉一条
    （`LocalQueueTimeout` 穿透网关且不留审计）。
    """
    fake = _FakeEmbed(fail=True)
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.6,
                     embed_client=fake, embed_threshold=0.99)
    _q1 = "请基于宏观流动性与行业景气度，分析贵州茅台的投资价值"
    _q2 = "请基于宏观流动性与行业景气度，分析五粮液的投资价值"
    cache.put("SYS", _q1, _resp("结论A"), agent_id="A08_macro", anchor=_q1)

    # 不抛，且按**旧的阈值规则**命中（0.6 下这对是相似的）
    hit = await cache.aget("SYS", _q2, agent_id="A08_macro", anchor=_q2)
    assert hit is not None, "精排失败后没有退回阈值规则（等于精排一挂就全不命中）"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_candidates_without_vectors_fall_back_to_threshold(tmp_dir):
    """★ 候选**全都没有**向量（例如全是旧条目）⇒ 退回阈值规则。

    不这么做的话，一开启精排就会把老缓存的语义命中**全部丢掉** ——
    那是"升级即退化"，而且**不报错**（`CHG-0177` 那类静默失效的同款）。
    """
    fake = _FakeEmbed({})          # 任何文本都拿不到向量
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.6,
                     embed_client=fake, embed_threshold=0.99)
    _q1 = "请基于宏观流动性与行业景气度，分析贵州茅台的投资价值"
    _q2 = "请基于宏观流动性与行业景气度，分析五粮液的投资价值"
    # 故意**不传 embedding** ⇒ 索引里这条没有向量
    cache.put("SYS", _q1, _resp("结论A"), agent_id="A08_macro", anchor=_q1)

    hit = await cache.aget("SYS", _q2, agent_id="A08_macro", anchor=_q2)
    assert hit is not None, "候选没有向量时没有退回阈值规则 —— 老缓存会被静默作废"


# ======================================================================
# 五、查询向量记忆化 + 可观测
# ======================================================================

@pytest.mark.asyncio
async def test_query_vector_is_memoized_per_anchor(tmp_dir):
    """同一个 anchor 查两次 ⇒ 精排器**只被调一次**。

    一次投研分析有 4~15 次 LLM 调用、每次调用前都要查缓存。
    不记忆化就是 N × RTT（实测约 **420 ms/次** ⇒ 一次分析多付 1.7~6.3 秒）；
    记忆化后是 **O(1) / 用户问句**。

    ## ★ 这条测试第一版是**假绿**（我自己交叉核对探针计数时发现的）

    第一版只调用了一次 `aget` 就断言 `calls == 1`。但那次 `aget` 的
    (system, prompt) 与 `put` 的**完全相同** ⇒ 它命中了**精确层**、
    **根本没走到 L3**。于是 `calls == 1` 只证明"第一次调了"，
    与记忆化毫无关系。

    修法：两次 `aget` 用**不同的 prompt**（保证都未命中精确层）、
    **相同的 anchor**，然后看**增量**。这样才真的在量记忆化。
    """
    fake = _FakeEmbed({normalize_text("问句"): [1.0, 0.0]})
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.85,
                     embed_client=fake, embed_threshold=0.80)
    cache.put("SYS", "写入用的prompt", _resp("x"), agent_id="A08_macro",
              anchor="问句", embedding=[1.0, 0.0])
    assert fake.calls == 0, "put 不该自己调精排器（向量由调用方算好传入）"

    # 两次都要**绕过精确层**才算真的走到 L3
    r1 = await cache.aget("SYS", "查询prompt-1", agent_id="A08_macro", anchor="问句")
    n1 = fake.calls
    r2 = await cache.aget("SYS", "查询prompt-2", agent_id="A08_macro", anchor="问句")
    n2 = fake.calls

    assert r1 is not None and r2 is not None, "没走到 L3（可能命中了精确层）"
    assert n1 == 1, f"第一次就该出网一次，实际 {n1}"
    assert n2 - n1 == 0, (
        f"同一个 anchor 第二次仍然出网（{n1} → {n2}）—— 记忆化没生效，"
        "一次分析会付 N 次网络往返")
    assert cache.stats()["query_vec_hits"] >= 1


class _ConstantEmbed:
    """总是返回**同一个向量**的替身 —— 精排里最容易误命中的形态。

    用它来验"prompt 模式下 L3 一次都不许跑"：只要它被调用了，
    余弦就必然 = 1.0 ⇒ 必然命中。所以"没命中"能**反证**它没被调用。
    """

    def __init__(self) -> None:
        self.configured = True
        self.calls = 0

    async def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        return [1.0, 0.0]

    def stats(self) -> dict[str, object]:
        return {"constant": True, "calls": self.calls}


@pytest.mark.asyncio
async def test_l3_never_runs_in_prompt_mode(tmp_dir):
    """★★ 结构性护栏：**不传 anchor 时 L3 一次网络都不出**（`CHG-0180`）。

    ## 为什么这个护栏是必须的（实测反证）

    不传 anchor 时比较的是**整 prompt**，而整 prompt 的 embedding 余弦在两条
    **完全不同**的资讯之间实测是 **0.9604 / 0.9768 / 0.9657** —— 全部远高于
    精排阈值 `0.80`。

    ⇒ 若允许 prompt 模式走 L3，同 agent 同层的**任何**候选都会被接受，
      语义缓存退化成「返回该 agent+层级的第一条缓存答案」，
      而且 `cache_hit=True`、耗时更短、**不报错**。
    ⇒ 换句话说：**「只把开关打开」会比不开更糟，且静默。**

    这条判据用**恒等向量**替身来验：它一旦被调用余弦必然 = 1.0，
    所以"没命中"能反证"没被调用"。
    """
    emb = _ConstantEmbed()
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.6,
                     embed_client=emb, embed_threshold=0.80)
    assert cache.rerank_enabled is True

    # ① prompt 模式（不传 anchor）
    cache.put("SYS", "请基于宏观流动性与行业景气度，分析贵州茅台的投资价值",
              _resp("结论A"), agent_id="A08_macro")
    hit = await cache.aget("SYS", "请基于宏观流动性与行业景气度，分析五粮液的投资价值",
                           agent_id="A08_macro")
    assert hit is not None, "前提不成立：prompt 模式下本应能按阈值规则命中"
    assert emb.calls == 0, (
        f"prompt 模式竟然调了精排 {emb.calls} 次 —— 护栏没生效，"
        "整 prompt 余弦 0.96~0.98 会让所有候选都被接受（静默串答案）")
    assert cache.stats()["l3_skipped_prompt_mode"] == 1, (
        "跳过了却没记账 —— '开关开了但没进 anchor 模式'会表现成'精排没效果'")

    # ② anchor 模式 ⇒ L3 必须真的跑
    cache.put("SYS", "另一个prompt", _resp("结论B"), agent_id="A08_macro",
              anchor="变量甲", embedding=[1.0, 0.0])
    hit2 = await cache.aget("SYS", "第三个prompt", agent_id="A08_macro",
                            anchor="变量乙")
    assert hit2 is not None
    assert emb.calls == 1, "anchor 模式下 L3 必须真的出网一次"
    assert cache.stats()["l3_attempts"] == 1


@pytest.mark.asyncio
async def test_semantic_cache_false_keeps_exact_only(tmp_dir):
    """★★ `semantic_cache=False` ⇒ **只留 L1**，L2/L3 一次都不跑（`CHG-0181`）。

    ## 谁必须关掉它

    **输出里含"原文逐字引文"的那一类调用点**。典型是情报抽取
    （`intel_extract`）：`phrases` 要逐字子串校验、`codes` 必须原文里真有、
    `summary` 要最长公共子串 ≥ 阈值，而校验用的是**这一段自己的原文**
    （`tone_job` 的 `_ask_segment` 明写"送进去的 seg 与校验用的 text
    必须是同一个字符串"）。

    ⇒ 把 A 文章的答案复用给 B 文章，**引文在 B 里根本不存在**
      ⇒ 逐字校验把它们全部丢掉 ⇒ 抽取返回**空**。
      这正是 `CHG-0069`「4/5 条 summary 为空」的**机制本身**。

    ⚠️ 换比较文本（正文进相似度）**治不了**它 —— 同一条新闻被转载改写后，
    引文照样不在新正文里。**瓶颈是输出契约，不是文本表征。**

    ## 自证前提（否则"没命中"可能只是碰巧）

    末尾用**同样条件但开着语义层**再跑一次，它**必须命中** ——
    否则这条测试证明不了"是开关挡住的"。
    """
    emb = _ConstantEmbed()          # 一旦被调，余弦必为 1.0 ⇒ 必命中
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.3,
                     embed_client=emb, embed_threshold=0.5)
    cache.put("SYS", "同一段原文", _resp("抽出来的字段"),
              agent_id="intel_extract")

    # ① 精确命中**照常保留**（同段原文重跑才是本作业真正的复用来源）
    exact = await cache.aget("SYS", "同一段原文", agent_id="intel_extract",
                             semantic_cache=False)
    assert exact is not None and exact.cache_kind == "exact"

    # ② 语义层被关掉 ⇒ 换一段文本必须**不命中**，且一次向量都不算
    hit = await cache.aget("SYS", "完全不同的另一段原文",
                           agent_id="intel_extract", semantic_cache=False)
    assert hit is None, "关掉语义层后仍然语义命中了"
    assert emb.calls == 0, "关掉语义层还是算了向量（白花一次网络往返）"
    assert cache.stats()["semantic_disabled"] == 1, (
        "关掉了却没记账 —— '语义命中率下降'会被误判成'精排不好使'")

    # ③ 自证前提：同一条件、**开着**语义层 ⇒ 必须命中
    hit2 = await cache.aget("SYS", "完全不同的另一段原文",
                            agent_id="intel_extract")
    assert hit2 is not None, (
        "前提不成立：开着语义层时本该命中 —— 那这条测试证明不了是开关挡住的")


def test_stats_report_rerank_state_without_faking_zero(tmp_dir):
    """★ 没配精排器时 `embed_client` 必须是 `None`，**不是 0/空字典**。

    「没量到」与「量到 0」必须分开（AGENTS.md 硬约束）。填 0 的后果：
    面板上"精排调用 0 次"既可以读成"没启用"，也可以读成"启用了但一次没跑"，
    而这两种情况的处置完全相反。
    """
    off = LLMCache(cache_dir=tmp_dir, ttl_hours=1)
    s = off.stats()
    assert s["rerank_enabled"] is False
    assert s["embed_client"] is None, "没配精排器却报了对象/0 —— 口径不明"
    assert s["l3_attempts"] == 0

    on = LLMCache(cache_dir=tmp_dir, ttl_hours=1, embed_client=_FakeEmbed({}),
                  embed_threshold=0.8, recall_k=5)
    s2 = on.stats()
    assert s2["rerank_enabled"] is True
    assert s2["embed_threshold"] == 0.8 and s2["recall_k"] == 5
    assert s2["embed_client"] == {"fake": True, "calls": 0}
    # 记忆化的效果必须也能看见（否则"压成 O(1)/问句"这句话无法验证）
    assert s2["query_vec_hits"] == 0 and s2["query_vec_entries"] == 0


# ======================================================================
# 六、向量的打包 / 解包（落盘体积与健壮性）
# ======================================================================

def test_vector_codec_roundtrip_and_corruption() -> None:
    """float32 + base64 往返**无损**；**坏数据返回 None 而不是抛**。

    为什么必须紧凑：1024 维用 JSON 数字数组是 12~18 KB/条，
    现存 8,463 条 ⇒ 单这一项就 100+ MB。float32 打包后约 46 MB。

    ## ⚠️ 为什么钉死 float32 而不是 float16（实测踩到）

    第一版用 `array.array("e")`（float16），**在本机 Windows 的 CPython 上
    直接抛 `ValueError: bad typecode`** —— `'e'` 是平台条件编译的。
    本项目有 Windows 开发机 + Linux VPS 两套环境，一个"看平台而定"的
    落盘格式意味着**同一份缓存换个平台就读不出来**，而且不报错
    （`decode_vector` 会把坏数据当"没有向量"）。
    ⇒ 这条断言用**非 float16 可精确表示**的值来锁住"我们用的是 float32"：
    若有人换回 float16，这些值会失真、`abs(x-y) < 1e-9` 立刻红。

    为什么坏数据不能抛：一条向量损坏只该让**这一条**不参与精排，
    不该把"一条坏数据"放大成"语义层全挂"。
    """
    vec = [0.125, -0.5, 1.0, 0.0, 0.1]      # 0.1 在 float16 下会失真到 0.099976
    back = decode_vector(encode_vector(vec))
    assert back is not None and len(back) == len(vec)
    for x, y in zip(vec, back, strict=True):
        # 容差 1e-7 是**卡在两种精度之间**的：float32 的实测误差约 1.5e-9，
        # float16 约 2.4e-5。所以这条断言能真的分辨"用的是哪种精度"，
        # 而不是"只要差不多就过"（那等于没验）。
        assert abs(x - y) < 1e-7, (
            f"{x} → {y} 的误差 {abs(x - y):.2e} 超过了 float32 的量级 —— "
            "说明打包精度不是 float32")
    assert cosine(vec, back) > 0.999999

    assert decode_vector("这不是 base64!!") is None
    # 长度不一致 ⇒ 0.0（维度不符就是不可比，**不当成相似**）
    assert cosine([1.0, 0.0], [1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0


def test_embedding_client_is_not_configured_without_credentials() -> None:
    """缺 key 或缺 base_url ⇒ `configured=False`（调用方按"未启用"处置）。

    `gateway._build_embed_client` 靠这个属性决定"要不要报精排已启用" ——
    没有它就会出现"日志说启用了、实际每次都失败"（`CHG-0173` 那类
    "开关没接上"的同款）。
    """
    assert EmbeddingClient(base_url="", api_key="k").configured is False
    assert EmbeddingClient(base_url="http://x", api_key="").configured is False
    ok = EmbeddingClient(base_url="http://x", api_key="k")
    assert ok.configured is True
    assert ok.stats()["calls"] == 0 and ok.stats()["failures"] == 0


# ======================================================================
# 七、★ 阈值可标定化：让 0.80 从"猜的"变成"可读的"（`CHG-0184`）
# ======================================================================

def _vec_cos(c: float) -> list[float]:
    """构造一个与 `[1.0, 0.0]` 的余弦**恰为** `c` 的二维向量。"""
    return [c, math.sqrt(max(0.0, 1.0 - c * c))]


@pytest.mark.asyncio
async def test_l3_records_the_similarity_it_judged_on(tmp_dir):
    """★★ **被拒绝的那次判定也必须留下相似度** —— 这是阈值可标定的前提。

    ## 病灶（这是本节存在的唯一理由）

    `embed_threshold = 0.80` 是 `CHG-0178` 里 n=4 顺手取的整数。只有
    `l3_hits` / `l3_misses` 两个计数时，下面两种情况在面板上**长得一模一样**：

        (a) 阈值定高了 —— 真复用就在 0.78 那儿躺着，被 0.80 挡住；
        (b) 根本没有相似候选 —— 最近的一个才 0.35，阈值调到 0.1 也没用。

    分不清 (a)(b) ⇒ 0.80 是偏高还是偏低**永远无法回答** ⇒ 它只能一直挂着
    "未标定"的标签，而"未标定"又会被读成"先这样吧"。
    ⇒ 记下**每次判定的 best_sim**，标定就退化成读一个直方图。

    ## 这条判据验的是"拒绝也留痕"，不是"直方图能算数"

    所以主断言挂在 **miss** 上：0.79 被 0.80 挡掉之后，
    `0.75-0.80` 这一桶必须有 1 个 —— 如果只记命中，这一桶会是空的，
    而"阈值切在哪里"这个信息**恰好只存在于被拒绝的那一侧**。
    """
    stored, ask_low, ask_high = "问句甲的表述", "问句甲的表述换个说法", "问句甲的表述再换个说法"
    fake = _FakeEmbed({
        normalize_text(stored): [1.0, 0.0],
        normalize_text(ask_low): _vec_cos(0.79),      # 差一点 ⇒ 必须被拒
        normalize_text(ask_high): _vec_cos(0.82),     # 刚过线 ⇒ 必须命中
    })
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.0,
                     embed_client=fake, embed_threshold=0.80)
    cache.put("SYS", "prompt甲", _resp("答案甲"), agent_id="A08_macro",
              anchor=stored, embedding=[1.0, 0.0])

    # ① 0.79 < 0.80 ⇒ 拒绝，但**必须留痕**
    assert await cache.aget("SYS", "prompt乙", agent_id="A08_macro",
                            anchor=ask_low) is None
    s = cache.stats()
    assert s["l3_attempts"] == 1 and s["l3_misses"] == 1
    assert s["l3_sim_hist"] == {"0.75-0.80": 1}, (
        f"被拒绝的那次没留下相似度：hist={s['l3_sim_hist']} —— "
        "没有它就无法判断 0.80 是偏高还是偏低（这正是本节要修的病）")
    assert s["l3_last_sim"] == pytest.approx(0.79, abs=1e-9)

    # ② 0.82 ≥ 0.80 ⇒ 命中，并且**紧贴阈值**要被单独点出来
    hit = await cache.aget("SYS", "prompt丙", agent_id="A08_macro",
                           anchor=ask_high)
    assert hit is not None and hit.content == "答案甲"
    s = cache.stats()
    assert s["l3_attempts"] == 2 and s["l3_hits"] == 1
    assert s["l3_sim_hist"] == {"0.75-0.80": 1, "0.80-0.85": 1}
    assert s["l3_hits_near_threshold"] == 1, (
        "0.82 只比阈值高 0.02 却没被告警 —— 这类命中随时会因为一句改写"
        "掉到阈值下，反过来 0.79 那个拒绝也只是差一点措辞 ⇒ "
        "说明这一桶里混着两类，阈值位置不可信")

    # ③ 桶序：`<0.05` 必须排最前（字符串序会把它排到数字**之后**）
    cache._observe_l3_sim(-0.4)
    assert list(cache.stats()["l3_sim_hist"])[0] == "<0.05", (
        "首桶跑到了分布末尾 —— 负余弦会被读成「最高相似度」；"
        "这种「看起来排好了、其实顺序反了」的图表会让人把分布读反")

    # ④ NaN 不许静默灌进首桶（灌进去就是凭空造出一个样本）
    before = dict(cache.stats()["l3_sim_hist"])
    cache._observe_l3_sim(float("nan"))
    assert cache.stats()["l3_sim_hist"] == before, "NaN 被当成样本记进去了"


@pytest.mark.asyncio
async def test_l3_histogram_only_records_real_judgements(tmp_dir):
    """★ 直方图只记**真的做过 L3 判定**的那些次 —— 否则它会自己说谎。

    两类"没做判定"必须**不**进直方图，而且各自的计数要能区分：

        · prompt 模式   ⇒ `l3_skipped_prompt_mode`（护栏挡住的，`CHG-0180`）
        · 向量算不出来  ⇒ `rerank is None` ⇒ 退回 L2 阈值规则（fail-open）

    若把这两类也记进直方图，分布里会凭空多出一堆 0.0，
    **看起来像"相似度普遍很低"**，进而得出"阈值 0.80 太高"的**反向结论**。
    """
    fake = _FakeEmbed({normalize_text("变量甲"): [1.0, 0.0]})
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.0,
                     embed_client=fake, embed_threshold=0.80)
    cache.put("SYS", "prompt甲", _resp("答案甲"), agent_id="A08_macro",
              anchor="变量甲", embedding=[1.0, 0.0])
    # 再写一条 **prompt 模式**条目：否则 prompt 模式的桶里没有候选，
    # `aget` 会在「没候选」那一行直接返回，护栏那段代码根本不执行。
    cache.put("SYS", "prompt丙", _resp("答案丙"), agent_id="A08_macro")

    # ① ★ 计数器口径（**本轮有意改过一次，`CHG-0203`**）。
    #
    #    **旧口径**（直到 `CHG-0199`）：`l3_skipped_prompt_mode` 记的是
    #    「prompt 模式**且有候选**因而跳过 L3」—— 桶为空时一动不动。
    #
    #    ⚠️ 那个"且有候选"**不是设计，是代码顺序的副产品**：护栏当初写在
    #    `if not candidates: return None` **之后**，于是空桶提前返回、计数没走到。
    #    ⇒ 而计数器的**声明用途**是"有多少调用点没传 anchor"——
    #      按这个用途，**空桶也是一次没传 anchor 的调用**，应当计入。
    #
    #    `CHG-0203` 把护栏移到召回之前（换判定器后"要不要出网"必须在算向量之前定），
    #    顺带口径变得更忠实：**现在它 = prompt 模式的语义层查找次数**。
    #    ⇒ 判据随之更新为钉住**新**口径，并写明为什么改 ——
    #      "判据要适应代码，不要让代码迁就判据"。
    empty = LLMCache(cache_dir=f"{tmp_dir}/empty", ttl_hours=1,
                     semantic_threshold=0.0, embed_client=fake,
                     embed_threshold=0.80)
    await empty.aget("SYS", "prompt乙", agent_id="A08_macro")
    assert empty.stats()["l3_skipped_prompt_mode"] == 1, (
        "空桶的 prompt 模式没有被计入 —— 口径又回到了"
        "「且有候选」那个**副产品**语义，而下游是拿它当"
        "'有多少调用点没传 anchor'读的 ⇒ 会系统性少算")
    assert empty.stats()["l3_attempts"] == 0, (
        "prompt 模式被记成了 L3 判定 —— `l3_attempts` 的口径是"
        "「**anchor 模式**的判定次数」，prompt 模式一次都不该计入")

    # ② prompt 模式（不传 anchor）且有候选 ⇒ 护栏挡住，直方图**不许**动
    await cache.aget("SYS", "prompt乙", agent_id="A08_macro")
    assert cache.stats()["l3_skipped_prompt_mode"] == 1
    assert cache.stats()["l3_sim_hist"] == {}, (
        "prompt 模式被记进了直方图 —— 那些 0.0 会被读成"
        "'相似度普遍很低'，直接推出与事实相反的标定结论")

    # ② 向量算不出来（替身故障）⇒ **不进判定层**
    #
    #    ⚠️ 口径在 `CHG-0203` 改过一次，理由要写清：
    #    · **旧**：`l3_attempts` 只在"有查询向量"时自增 ⇒ 那时它 =
    #      「精排真的能算的次数」。当时成立，因为**判定只能靠向量**。
    #    · **新**：判定器换成了 cross-encoder，**它不吃向量** ⇒
    #      "没有向量"不再等于"没做判定"，反之亦然。
    #    ⇒ 拆成两个数：`l3_attempts` = **进了判定阶段的次数**；
    #      `l3_judged` = **判定层真的返回了分数的次数**。
    #      这样"每次都在降级"（attempts>0 / judged=0）才看得出来 ——
    #      而 `CHG-0202` 里 **0/5007 带向量却被读成 "L3 没跑"** 正是缺了它。
    broken = _FakeEmbed(fail=True)
    cache2 = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.0,
                      embed_client=broken, embed_threshold=0.80)
    cache2.put("SYS", "prompt甲", _resp("答案乙"), agent_id="A08_macro",
               anchor="变量甲", embedding=[1.0, 0.0])
    await cache2.aget("SYS", "prompt乙", agent_id="A08_macro", anchor="变量甲换个说法")
    s2 = cache2.stats()
    assert s2["l3_attempts"] == 1, (
        "进了判定阶段却没记账 ⇒ 「端点挂了、每次都在降级」在面板上"
        "会长得和「L3 根本没跑」一模一样")
    assert s2["l3_judged"] == 0, "判定层没给分却记成判定了"
    assert s2["l3_sim_hist"] == {}, "判定层没给分却写了直方图（那是凭空造样本）"

