r"""LLM响应缓存（L1内存 + L2本地文件）+ **三级匹配**（`CHG-0178`）。

## 三级结构（现行口径）

```
L1  精确哈希        normalize(scope|system|prompt) 的 SHA256     —— 不变
L2  3-gram 粗筛     **只召回不判定**：按 (agent_id, scope) 桶取 top-K
L3  Embedding 精排  anchor 上的余弦 ≥ 阈值 ⇒ 命中（可插拔、fail-open）
L4  LLM
```

## ★ 为什么要从"两级"改成"三级"（三条实测，都不是推理）

### 一、单一全局阈值在这份语料上**两边都不成立**

同 `(agent_id, scope)` 桶内两两 3-gram 余弦实测：

| 桶 | 中位 | ≥0.60 占比 |
|---|---:|---:|
| `intel_extract \| medium…` | **0.775** | **100.0%**（等于没筛） |
| `mainline_member_pure \| reasoning…` | **0.255** | **0.0%**（等于永久短路） |

⇒ 任何"一个阈值走天下"的写法，在其中一个桶里必然失效。
**所以 L2 只做 top-K 截断（`llm_embed_recall_k`），判定权交给 L3。**

### 二、阈值 0.85 坐在分布**上方**，语义层因此几乎不干活

全量审计 44,719 行里语义命中仅 **506 次 = 命中数的 1.89%**
（精确命中 26,287 次）。这不是模型不行，是**阈值相对桶内分布高得离谱**
（每桶 ≥0.85 的 pair 占比 ≤0.4%）。

### 三、★ 比较的文本必须是 **anchor**（变量部分），不是整 prompt

抽取 prompt 的固定骨架（`_SYSTEM` + schema）实测占 **79.6%**
（骨架 977 字符 / `vector_text` 中位 1227 字符），跨桶从 **34.7% 到 419%**。
拿整 prompt 算相似度 ⇒ 两条**完全不同**的资讯能到 **0.93~0.97**（远超阈值）
⇒ 必然复用第一条答案（本项目登记过的事故：`CHG-0069`、`CHG-0094`）。

    只比 anchor：不同事件 **0.0000**（3-gram）/ **0.40~0.50**（embedding）
                 同事件改写稿 **0.9639**（embedding）

`anchor` 缺省时回落到 `system + prompt`（**行为与改动前逐字一致**），
所以旧调用点不改也能继续跑。

## 语义索引为什么必须在内存里（2026-09-25 并发改造）

原实现在**每次未命中**时遍历 ``data/llm_cache/*.json`` 全部文件，
逐个 ``read_text`` + ``json.loads`` 才能读到 ``agent_id``/``scope`` 做过滤
（过滤发生在解析**之后**）。实测本机 3108 个缓存文件：

    cold 未命中扫描:  682.2 ms   (进程重启后第一次)
    warm 未命中扫描:    8.9 ms   (此后每次未命中)

两个问题都致命：

1. **同步阻塞事件循环**。这是 async 服务，9ms 的同步 IO 直接冻结**所有**
   并发请求（实测事件循环最大停顿 22.3 ms）。而且它是**串行累加**的 ——
   一次投研分析有 7~15 次 LLM 调用、大部分会未命中，N 个用户并发时
   总阻塞 ≈ N × 9ms，一个用户的分析会给其他所有人累计上百毫秒延迟。
2. **随缓存增长而退化**。缓存只增不减（过期文件仅在再次被访问时才
   ``unlink``），3108 文件 = 10MB，而这个数字会自己变大。

现在改为：**进程内维护 ``key → (agent_id, scope, vector)`` 索引**，
语义查找变成纯内存计算（微秒级）。索引按 ``agent_id``/``scope`` 两级分桶，
先分桶再算余弦 —— 单次语义查找只需比较同桶内的少量条目，
而不是"解析全部文件再丢掉 99%"。

索引是**懒加载**的：首次语义查找时建一次（一次性付 O(文件数) 的解析代价），
之后 ``put()`` 增量更新。没有额外文件格式变更，旧缓存目录可直接沿用。

## L3 精排的资源纪律（`CHG-0178`）

- **走云端，不进 GPU 生成域**：`providers.py` 的闸只包住 `/api/chat`，
  新开 `/api/embed` 出站点会绕过 `get_local_gate()`，成为**第三类无闸的
  Ollama 消费者**，去抢那个已实测"排队占墙钟 82.6%"的唯一槽位。
- **不进 `configs/models.yaml` 的 `models:` 段**：那是降级链的候选池，
  写进去等于把 embedding 模型当 LLM 用。
- **硬超时 + fail-open**：任何失败都退化成"语义未命中"，**绝不抛**。
- **查询向量按 anchor 记忆化**：一次分析 4~15 次调用 ⇒ 把 N×RTT 压成
  **O(1) / 用户问句**。
- **没有精排器的旧条目仍可被精确命中与 L2 召回**（只是不参与 L3 判定）——
  所以现存 8,463 条缓存不会失效，语义层对它们退化为旧的阈值规则。

## 同步 `get()` 与异步 `aget()` 的差别（**只有一处，且必须写在明面上**）

L3 精排要做网络调用，**只能在事件循环里做**。所以：

    aget()  →  完整三级（L1 → L2 召回 → L3 精排）
    get()   →  L1 → L2，判定退回**旧阈值规则**（无精排输入）

判定逻辑**只有一份实现**（`_pick`），两条入口的区别仅是"精排输入可不可得"。
`gateway` 生产路径走的始终是 `aget()`；`get()` 保留给同步调用方与测试。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from src.infrastructure.llm.embedding import cosine, decode_vector, encode_vector
from src.infrastructure.llm.models import LLMResponse

logger = logging.getLogger(__name__)

_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

#: 3-gram 向量在索引里缓存的条目上限（防长跑进程索引无限增长）。
#: 超限后语义查找退化为"只比较最近 N 条"，精确命中不受影响。
_INDEX_MAX_ENTRIES = 20000

#: anchor 查询向量的记忆化上限（防长跑进程无限增长）。
#: 一条 1024 维 float32 向量约占 4 KB；4096 条 ≈ 16 MB，可以接受。
_QUERY_VEC_MAX = 4096

#: ★★ 复用**年龄**的分档上界（秒）—— `CHG-0209`。
#:
#: ## 为什么需要它（这修正了我自己上一轮的一个提议）
#:
#: 上一轮我说"往审计里加 anchor 就能给 L2 定 TTL"。**那是错的**：
#: 审计**只记录出网的调用**，而 L2 的语义复用发生在**命中路径**上
#: （命中不调 LLM ⇒ **不落审计**）⇒ 审计里根本没有"复用"这件事，
#: 只有在"两次都漏掉"之间的距离。而且 `anchor_hash` 只认**完全相同**的
#: anchor，"换个说法"仍然认不出 —— 那还是 L1 口径。
#:
#: ⇒ **语义复用距离只能在缓存里量**，而且量的应该是**被复用那条的年龄**：
#:     命中时  `age = now − entry.created_at`
#: 这个数**就是** TTL 该取多少的直接依据：
#:     若某个命中来自 5 小时前的条目 ⇒ TTL 必须 ≥ 5h 才吃得到它。
#:
#: ⇒ ★ **这些分档上界就是 TTL 的候选刻度**（与 `CHG-0209` 实测的
#: 60s / 1h / 6h / 24h / 7d 一一对齐）⇒ 直方图直接读出"TTL 设成 X 能吃到多少"。
REUSE_AGE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("<1min", 60.0),
    ("1-10min", 600.0),
    ("10min-1h", 3600.0),
    ("1-6h", 21600.0),
    ("6-24h", 86400.0),
    ("1-3d", 259200.0),
    ("3-7d", 604800.0),
    (">7d", float("inf")),
)


def reuse_age_label(age_sec: float) -> str:
    """年龄 → 分档标签。**唯一实现**（直方图与读取端都用它）。"""
    for label, upper in REUSE_AGE_BUCKETS:
        if age_sec <= upper:
            return label
    return REUSE_AGE_BUCKETS[-1][0]


def normalize_text(text: str) -> str:
    """小写化并剔除空白/标点，保留字母数字与中日韩字符。"""
    return _NORMALIZE_RE.sub("", text.lower())


def cache_key(system: str, prompt: str, scope: str = "") -> str:
    """缓存键（SHA256）。

    `scope` 用于区分**同一 prompt 在不同调用条件下不应互相复用**的场景。
    最典型的是模型层级：`light` 层用本地 1.5B 跑出来的回答，
    不能拿去当 `decision` 层的结论复用 —— 两者对精度要求差一个量级，
    但 system/prompt 可能一字不差。`json_mode` 同理（是否要求结构化输出）。

    `scope` 由调用方生成（形如 `"reasoning|json=1"`），不走 `normalize_text`
    —— 它是标记而不是自然语言，不该被去标点。

    ⚠️ **本函数只用于"缓存查找"，不要拿它的返回值当审计的 `prompt_hash`。**
    它先过 `normalize_text`（小写 + 去标点），于是 `"PE(TTM)"` 与 `"pettm"`
    会得到**同一个键** —— 这对缓存是优点（换个写法也该命中），
    对审计是缺陷（两个不同的 prompt 被记成同一个）。
    审计要的是"这是不是同一个 prompt"，用 `prompt_fingerprint()`。
    """
    parts = [normalize_text(system), normalize_text(prompt)]
    if scope:
        parts.insert(0, scope)
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def prompt_fingerprint(system: str, prompt: str) -> str:
    """审计用 `prompt_hash` 的**唯一实现**（原始字节，不做任何规范化）。

    ## 为什么必须固定成"原始字节"

    命中语义是**"这是不是同一个 prompt"**，所以要满足两条：
      1. **同一 (system, prompt) 恒等** —— 换进程、换调用路径都一样；
      2. **不同 prompt 必须不同** —— `"PE(TTM)"` 与 `"pettm"` 必须区分开。

    `cache_key()` 两条都不满足第 2 条（它做 normalize，见其 docstring）。

    ## 为什么以前会不一致（`CHG-0176` 同类缺陷）

    审计字段 `prompt_hash` 有**两条生产写入路径**：
      · `LLMGateway.complete()` —— 走 `cache_key(system, prompt)`（**规范化后**哈希）；
      · `providers._wrap_response()` —— 走 `_sha256(system + "\\x00" + prompt)`（**原始**哈希）。
    同一个 prompt 走两条路径得到**两个不同的 hash** ⇒
    "这两次调用是不是同一个 prompt"在审计里**无法回答**，而没有任何报错。
    现在两条路径都调用本函数，判据见
    `tests/unit/test_prompt_fingerprint.py::test_provider_and_gateway_agree_on_prompt_hash`。
    """
    return hashlib.sha256(f"{system}\x00{prompt}".encode()).hexdigest()


def _ngram_vector(text: str, n: int = 3) -> Counter[str]:
    if len(text) < n:
        return Counter([text]) if text else Counter()
    return Counter(text[i : i + n] for i in range(len(text) - n + 1))


def cache_anchor(*parts: str) -> str:
    """拼出**语义比较**用的文本：只放"换个说法也该复用"的东西（`CHG-0180`）。

    ## 该放什么

    **用户问句 / 分析焦点**。这一类东西的价值就在于"同一件事换个说法"
    应该命中 —— 那正是 embedding 精排能买到、而字符相似度买不到的东西。

        cache_anchor(query_block)          # ✅
        cache_anchor(question, focus)      # ✅

    ## ★ 该放什么（这一条是我第一版搞错、被端到端实测推翻的）

    **数据不要放这里。** 第一版把数据段也放了进来，并在注释里断言
    "换数据就不会命中"。实测直接推翻：同一问句、只把 CPI 从 0.5 改成 9.9，
    anchor 文本变了，但 **embedding 余弦仍 ≥0.80** ⇒ L3 照命中 ⇒
    返回**上一批数据算出来的结论**。

    ⇒ 规律：**"内容变了" ≠ "语义向量变了"**。
      凡是"必须完全一致才能复用"的东西（数据、期间、参数、上游结论），
      走 **`scope` 上的精确指纹**（见 `cache_data_key`）——数据一变，
      L1/L2/L3 **整条复用路径一起失效**，而不是靠相似度碰巧没命中。

    ## 也不放骨架

    固定骨架实测占整 prompt **79.6%**（跨桶 34.7%~419%）⇒ 带上它两条完全
    不同的内容相似度能到 **0.93~0.97**，必然串答案（`CHG-0069` / `CHG-0094`）。

    ## 归一化

    这里**不做** `normalize_text` —— `LLMCache._anchor_text()` 会统一做，
    免得"拼的时候归一次、比的时候又归一次"两条路径分叉。
    """
    return "\n".join(p for p in parts if p)


def cache_data_key(*parts: str) -> str:
    """把"必须完全一致才能复用"的内容压成指纹，交给 **`scope`**（`CHG-0180`）。

    ## 为什么必须是精确指纹，不能靠相似度

    见 `cache_anchor` 里那段实测反证：一个数字的变化**几乎不移动**
    1024 维语义向量，所以"换了一批数据"在 embedding 眼里仍然"很像"。
    而结论必须随数据变 —— 复用旧数据算出的结论是**静默的错误**。

    指纹进 `scope` 之后，它同时是：
      · L1 的 SHA256 输入 ⇒ 精确层直接分开；
      · L2/L3 的分桶键   ⇒ 连候选都不会有。

    所以这是**结构保证**，不是"阈值调得好"。

    ## 该放什么

        cache_data_key(context_block, event_lines, verified_texts, hint)   # A08
        cache_data_key(upstream_context, hint)                             # A17

    ## 不该放什么

    用户问句、分析焦点 —— 那些要留给 `cache_anchor`，否则"换个说法"
    就永远命不中了，精排也白装。

    ## 截断

    只取 16 位十六进制（64 bit）。**它只需要"区分"不需要"抗碰撞攻击"**
    —— 撞了只会多一次误复用，不值得为它拉长 scope（scope 进 SHA256、
    又做分桶键，长了会拖慢且难读）。
    """
    blob = "\x1f".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _entry_embedding(raw: dict[str, Any]) -> list[float] | None:
    """从一条落盘条目里取 embedding（**任何异常都返回 `None`，不抛**）。

    为什么不让它抛：一条向量损坏只该让**这一条**不参与精排，
    不该让整个索引构建失败（那会把"一条坏数据"放大成"语义层全挂"）。
    """
    blob = raw.get("embedding")
    if not isinstance(blob, str) or not blob:
        return None
    return decode_vector(blob)


def cosine_similarity(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(cnt * b.get(g, 0) for g, cnt in a.items())
    norm_a = math.sqrt(sum(c * c for c in a.values()))
    norm_b = math.sqrt(sum(c * c for c in b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class _SemanticIndex:
    """语义索引：``(agent_id, scope, 比较模式)`` → ``{key: (3-gram 向量, embedding)}``。

    ## ★ 为什么桶键里多了一维"比较模式"（`CHG-0178` 实测补上）

    比较文本有两种来源，它们**不可比**：

        anchor 模式   比 `normalize(anchor)`（变量正文）
        prompt 模式   比 `normalize(system + prompt)`（整 prompt，旧口径）

    把两种模式混在同一个桶里，跨模式的余弦会趋近 0（一边是 32 字的正文、
    另一边是 666 字的整 prompt）—— **不会造成错命中**，但会造成
    "**写进去的条目查不出来**"，而且**不报错**。

    这个坑是我自己踩出来的：写的时候传了 anchor、读的时候没传，
    语义层静默失效。所以把它变成**结构上的分区**，而不是"靠长度差异碰巧安全"。

    为什么不混也有好处的第二个理由：分区后不必对不可比的条目白算余弦。

    为什么分桶而不是一条平铺的 list：语义复用**必须同时满足**
    `agent_id` 与 `scope` 两个作用域（见 ``_recall`` 的说明）。
    分桶让"过滤"变成"直接取桶"，不必遍历全部条目再丢弃。
    """

    __slots__ = ("_buckets", "_order", "_embeddings", "_texts")

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, str], dict[str, Counter[str]]] = {}
        #: `(bucket_key, key)` → embedding（可能有，也可能没有）
        self._embeddings: dict[tuple[tuple[str, str, str], str], list[float]] = {}
        #: ★ `(bucket_key, key)` → **比较用的原始文本**（`CHG-0203`）。
        #:
        #: ## 为什么索引里要存文本（而不是命中后再去读文件）
        #:
        #: cross-encoder 吃的是**两段原始文本**，不是向量。判定发生
        #: **在 `_pick` 之前**、对 K 条候选**逐条**打分 ⇒ 若那时才去读盘，
        #: 一次查找要 K 次文件读（K=12 ⇒ 12 次 `read_text + json.loads`）。
        #: 索引本来就在建索引时**已经解析过每条**（`_entry_embedding(raw)`），
        #: 顺手把文本留下是零额外 IO。
        #: 代价：每条约 30 字符 × 5,007 条 ≈ 150 KB 常驻内存 —— 可忽略。
        self._texts: dict[tuple[tuple[str, str, str], str], str] = {}
        #: 插入顺序（用于超限时淘汰最旧条目）
        self._order: list[tuple[tuple[str, str, str], str]] = []

    @staticmethod
    def _bucket_key(agent_id: str, scope: str, anchor_used: bool,
                    ) -> tuple[str, str, str]:
        return (agent_id, scope, "anchor" if anchor_used else "prompt")

    def __len__(self) -> int:
        return sum(len(b) for b in self._buckets.values())

    def add(self, key: str, agent_id: str, scope: str, vector_text: str,
            embedding: list[float] | None = None, *,
            anchor_used: bool = False) -> None:
        if not agent_id:
            return  # 无 agent_id 标记的条目不参与语义复用（防跨 Agent 串台）
        bucket_key = self._bucket_key(agent_id, scope, anchor_used)
        bucket = self._buckets.setdefault(bucket_key, {})
        if key in bucket:
            # 已存在：只在**新来的带 embedding 而旧的没有**时补上（升级路径：
            # 进程内先精确命中过、后来才接上精排器）。
            if embedding and (bucket_key, key) not in self._embeddings:
                self._embeddings[(bucket_key, key)] = embedding
            return
        bucket[key] = _ngram_vector(vector_text)
        self._texts[(bucket_key, key)] = vector_text
        if embedding:
            self._embeddings[(bucket_key, key)] = embedding
        self._order.append((bucket_key, key))
        if len(self._order) > _INDEX_MAX_ENTRIES:
            old_bucket, old_key = self._order.pop(0)
            self._buckets.get(old_bucket, {}).pop(old_key, None)
            self._embeddings.pop((old_bucket, old_key), None)
            self._texts.pop((old_bucket, old_key), None)

    def remove(self, key: str, agent_id: str, scope: str, *,
               anchor_used: bool = False) -> None:
        bucket_key = self._bucket_key(agent_id, scope, anchor_used)
        self._buckets.get(bucket_key, {}).pop(key, None)
        self._embeddings.pop((bucket_key, key), None)
        self._texts.pop((bucket_key, key), None)

    def top_k(
        self, query_vec: Counter[str], agent_id: str, scope: str, k: int, *,
        anchor_used: bool = False,
    ) -> list[tuple[str, float]]:
        """同桶内 3-gram 余弦 **top-K**（**不设阈值**），按分数降序。

        ## ★ 为什么不设阈值（这是本次改造的核心判断之一）

        实测同一个全局阈值在两个桶里表现完全相反
        （`intel_extract` ≥0.60 占 100%、`mainline_member_pure` 占 0%）——
        阈值既能"等于没筛"也能"等于永久短路"。**召回层的职责是别漏，
        不是判准**；判准交给 L3。所以这里只截断条数、不看分数。

        ## ⚠️ 但 3-gram **召回本身也不合格**（`CHG-0201` 实测）

        真改写的 3-gram 分数接近 0、而无关问句在 0.6 上下
        ⇒ 真匹配的中位排名**第 20 名**（池子 60）、**recall@12 只有 40%**。
        ⇒ 本方法现在是**降级路径**：只要桶里有 embedding 就用
        `top_k_by_embedding`（recall@12 **100%**），只有"一条向量都没有"时才走这里。
        """
        bucket = self._buckets.get(self._bucket_key(agent_id, scope, anchor_used))
        if not bucket or k <= 0:
            return []
        scored = [(key, cosine_similarity(query_vec, vec))
                  for key, vec in bucket.items()]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:k]

    def top_k_by_embedding(
        self, query_emb: list[float], agent_id: str, scope: str, k: int, *,
        anchor_used: bool = False,
    ) -> list[tuple[str, float]] | None:
        """同桶内 **embedding 余弦 top-K**（召回层）。

        Returns: 打分列表；**桶里一条向量都没有**时返回 `None`
        （调用方据此退回 3-gram 召回 —— 而不是返回空列表，
        那会让"没有向量"和"桶是空的"两件事长得一样）。

        ## 为什么召回层必须用 embedding 而不是 3-gram（`CHG-0201` 实测）

        同一批 20 个查询、池子 60，**真匹配在不在 top-K 里**：

            3-gram（现行召回）     @12 **40%**   中位第 **20** 名  最差第 **58** 名
            `BAAI/bge-m3`         @12 **100%**  中位第 2 名       最差第 5 名
            `Qwen3-Embedding-4B`  @12 **100%**  中位第 2 名       最差第 **2** 名

        ⇒ 3-gram 的 40% 会把**总召回上限锁死在 40%**，后面接多好的判定器都没用
        （候选里根本没有正确答案）。这推翻了 `CHG-0199` §41.20.4 那条
        "L2 3-gram 只召回"的链路。
        """
        bk = self._bucket_key(agent_id, scope, anchor_used)
        bucket = self._buckets.get(bk)
        if not bucket or k <= 0:
            return None
        scored = [
            (key, cosine(query_emb, emb))
            for key in bucket
            if (emb := self._embeddings.get((bk, key)))
        ]
        if not scored:
            return None      # 全桶无向量 ⇒ 让调用方退回 3-gram，而不是假装"没有候选"
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:k]

    def embedding_of(self, key: str, agent_id: str, scope: str, *,
                     anchor_used: bool = False) -> list[float] | None:
        bk = self._bucket_key(agent_id, scope, anchor_used)
        return self._embeddings.get((bk, key))

    def text_of(self, key: str, agent_id: str, scope: str, *,
                anchor_used: bool = False) -> str:
        """比较用的原始文本（cross-encoder 的输入）。取不到就返回空串。"""
        return self._texts.get((self._bucket_key(agent_id, scope, anchor_used),
                                key), "")

    def vector_less(self, agent_id: str, scope: str, *,
                    anchor_used: bool = False) -> int:
        """同桶里**没有 embedding** 的条目数（决定 L3 是否只能降级）。"""
        bk = self._bucket_key(agent_id, scope, anchor_used)
        bucket = self._buckets.get(bk) or {}
        return sum(1 for key in bucket if (bk, key) not in self._embeddings)


class LLMCache:
    """三级 LLM 响应缓存（精确哈希 → 3-gram 召回 → Embedding 精排）+ L1/L2 存储。"""

    def __init__(
        self,
        cache_dir: str | None = None,
        ttl_hours: float = 24.0,
        semantic_threshold: float = 0.85,
        *,
        embed_client: Any = None,
        embed_threshold: float = 0.80,
        recall_k: int = 12,
        rerank_client: Any = None,
        rerank_threshold: float = 0.95,
        rerank_top_k: int = 12,
    ) -> None:
        """
        `embed_client` 是 `embedding.EmbeddingClient`（或任何有 `await embed(text)`
        与 `configured` 属性的对象）。**`None` = 没有精排器** ⇒ L3 退化为旧的
        阈值规则（行为与两级时代一致）。

        `rerank_client` 是 `rerank.RerankClient`（鸭子类型：`configured` +
        `await rerank(query, documents)` + `stats()`）。给了它，**判定权就从
        bi-encoder 余弦换成 cross-encoder**（`CHG-0203`，实测 AUC 0.573 → 0.775）；
        `None` = 与改动前逐字一致。

        ⚠️ 两个客户端都由**调用方注入**而不是在这里 new：它们需要 Settings 里的
        base_url/key，而那些只有组装层（`gateway`）拿得到；缓存模块不该
        自己去读环境（本项目踩过"pydantic-settings 不写回 os.environ"的坑）。
        """
        if cache_dir is None:      # 默认从 registry 取（CHG-0071）
            from src.infrastructure.catalog.data_stores import store_rel

            cache_dir = store_rel("llm_cache")
        self._dir = Path(cache_dir)
        self._ttl_seconds = ttl_hours * 3600
        self._threshold = semantic_threshold
        self._embed = embed_client
        self._embed_threshold = float(embed_threshold)
        self._recall_k = max(1, int(recall_k))
        self._rerank = rerank_client
        self._rerank_threshold = float(rerank_threshold)
        self._rerank_top_k = max(1, int(rerank_top_k))
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}
        self._index = _SemanticIndex()
        self._index_built = False
        #: ★ anchor 文本哈希 → 查询向量。**把 N×RTT 压成 O(1)/用户问句 的就是这一步。**
        #:
        #: ## 为什么放在**这里**而不是 `EmbeddingClient` 里（`CHG-0178` 实测纠正）
        #:
        #: 第一版把它写在客户端里。后果：**换任何别的客户端（含测试替身）
        #: 就静默失去这个保证**，而我的单测正是用替身 ⇒ 那条断言
        #: **什么都没验**（假绿，而且是我自己交叉核对探针计数时才发现）。
        #: 记忆化是**缓存层**的诉求（"同一个 anchor 只算一次"），不是传输层的。
        self._query_vecs: dict[str, list[float]] = {}
        #: 观测计数（**没有它就无法回答"三级缓存到底跑起来没有"**）
        self._l3_attempts = 0
        self._l3_hits = 0
        self._l3_misses = 0
        #: ★ **判定层真的返回了分数的次数**（`CHG-0203`）。
        #:
        #: ## 为什么必须与 `l3_attempts` 分开
        #:
        #: `CHG-0202` 实测存量 **0/5007 带向量**，而生产里 `l3_attempts = 0`
        #: 曾被读成"L3 没跑"——**真因是"每一次都在降级"**。
        #: 换判定器之后这件事更容易混：**cross-encoder 不吃向量**，
        #: 所以"没有向量"不等于"没判定"，反之"进了判定阶段"也不等于"判成了"。
        #: ⇒ 两个数一起读才能分辨四种状态：
        #:     attempts>0 & judged>0  ⇒ L3 真的在判
        #:     attempts>0 & judged=0  ⇒ **每次都在降级**（端点挂了 / 全桶无向量）
        #:     attempts=0 & skipped>0 ⇒ 调用点没传 anchor
        #:     attempts=0 & skipped=0 ⇒ 语义层没被走到（或没候选）
        self._l3_judged = 0
        #: L3 因**调用点没传 anchor** 而跳过的次数（`CHG-0180`）。
        #: 没有它，"精排装了但没效果"会被误判成"精排不好使"，
        #: 而真因是调用点还在 prompt 模式。
        self._l3_skipped_prompt_mode = 0
        #: L3/L2 因**该调用点显式关掉了语义层**而跳过的次数（`CHG-0181`）。
        self._semantic_disabled = 0
        self._query_vec_hits = 0
        #: ★ 阈值可标定化（`CHG-0184`）：**每次 L3 判定**落一个相似度直方图。
        #:
        #: ## 为什么必须有它
        #:
        #: `embed_threshold` 起手是 **0.80**（`CHG-0178`，n=4 的实测顺手取的整数）。
        #: 只有 `l3_misses` 这个计数时，"阈值定高了"与"根本没有相似候选"
        #: **在面板上长得一模一样** ⇒ 0.80 是偏高还是偏低**永远无法回答**，
        #: 于是它只能一直挂着"未标定"的标签。
        #: 有了 `best_sim` 的分布，标定就退化成读一个直方图：
        #:   · 大量 miss 堆在 0.75~0.80 ⇒ 阈值偏高，降一点就能吃到真复用；
        #:   · miss 全堆在 0.4~0.6 而 hit 在 0.95+ ⇒ 中间是空的，0.80 位置无关紧要；
        #:   · hit 贴着 0.80 ⇒ 阈值正在切开一团连续分布，**这才是危险信号**
        #:     （意味着 0.79 的"不同事件"与 0.81 的"同一事件"混在一起）。
        #: 记的是**每次判定的 best_sim**（不是被选中的那个），所以拒绝也留痕。
        self._l3_sim_hist: dict[str, int] = {}
        #: 最近一次 L3 判定的 best_sim（`None` = 还没跑过 L3）。
        #: 直方图要看分布，这个用于"跑一次就知道大概"。
        self._l3_last_sim: float | None = None
        #: 命中但只比阈值高**一个桶**的次数 ⇒ 阈值正切在连续分布上的告警位。
        self._l3_hits_near_threshold = 0
        #: ★ cross-encoder 判定的计数（`CHG-0203`）。
        #:
        #: ## 为什么必须**分开**记，不能并进 `l3_*`
        #:
        #: `l3_attempts/hits/misses` 记的是"语义层判了几次、中了几次"，
        #: 而这一组记的是"**判定器是不是 cross-encoder**"。
        #: 合成一个数会让下面两种状态长得一样：
        #:     · cross-encoder 在跑，命中率 20%
        #:     · cross-encoder 挂了，退回 bi-encoder 判定，命中率 20%
        #: ⇒ 而它们的处置完全不同（一个调阈值、一个查端点）。
        #: `rerank_fallbacks` 是那个"退回了几次"的分母。
        self._rerank_attempts = 0
        self._rerank_hits = 0
        self._rerank_fallbacks = 0
        #: ★★ 复用率（`CHG-0208`）。
        #:
        #: ## 为什么这一组必须有（原来**一个都没有**）
        #:
        #: 改造三级缓存做了很多轮，但**"缓存到底有没有在用"从来没被量过**。
        #: 已有的 `l3_attempts` 是"**走到 L3 的**查找数"，不是查找总数 ——
        #: 它做不了分母（L1 命中的那些根本不进 L3）。于是：
        #:     · "复用率是多少" 答不出来；
        #:     · "缓存是不是在变聪明" 只能靠感觉。
        #: 而 `scripts/cache_health.py` 一上来就抓到：**5,007 条里 1,928 条
        #: 静默过期、42 个桶只剩 2 个** —— 正因为没有任何计数器看得见这件事。
        #:
        #: ## 只记**两个**计数，miss 由减法推出
        #:
        #: 三个独立计数器（hits/misses/lookups）**一定会漂移**：漏加一处、
        #: 早退路径忘记加，就得到一个永远不会报错的错数。所以：
        #:     `misses ≡ lookups - hits_exact - hits_semantic`
        #: 命中分类**只有一个地方**（`_mark`），查找数在**两个入口**各加一。
        #: ★ 由此得到一条可断言的不变量（判据里钉住它）：
        #:     `hits_exact + hits_semantic + misses == lookups`，且 miss ≥ 0。
        self._lookups = 0
        self._hits_exact = 0
        self._hits_semantic = 0
        #: ★★ 复用**年龄**直方图（`CHG-0209`）—— 决定 L1/L2 的 TTL。
        #:
        #: 与上面 `reuse_rate` 的区别：`reuse_rate` 回答"**有没有**复用"，
        #: 这一组回答"复用的是**多久以前写的**那条"。
        #: 只有后者能定 TTL：命中率高但全来自 10 秒前的条目 ⇒
        #: TTL 取 1 小时和取 7 天**没有区别**（`CHG-0209` 实测正是如此）。
        #:
        #: ⚠️ **精确命中与语义命中分开记**，因为两者该有不同的 TTL：
        #: 前者是"同一句话再问一次"（秒级），后者是"换了个说法"（可能跨小时）。
        #: 合成一个数就分不出该给哪一层调 TTL。
        self._reuse_age_exact: dict[str, int] = {}
        self._reuse_age_semantic: dict[str, int] = {}
        #: ★ 拿不到 `created_at` 的命中次数（**没量到 ≠ 量到 0**）。
        #: `created_at` 是 `CHG-0209` 才写进条目的 ⇒ **所有存量条目都没有它**。
        #: 没有这个计数器，那些命中会被静默塞进 `<1min` 桶，
        #: 让"复用都发生在 1 分钟内"这个**假结论**看着很有依据。
        self._reuse_age_unknown = 0

    @property
    def rerank_enabled(self) -> bool:
        """L3 精排是否**真的可用**（配了客户端 + 客户端自认可用）。"""
        return self._embed is not None and bool(
            getattr(self._embed, "configured", False))

    @property
    def judge_enabled(self) -> bool:
        """**判定器**是不是 cross-encoder（`CHG-0203`）。

        与 `rerank_enabled` 分开是刻意的：前者问"精排这一层开没开"，
        后者问"**判定权在谁手里**"。两者可以同时为真 —— 那时召回用
        bi-encoder、判定用 cross-encoder。
        """
        return self._rerank is not None and bool(
            getattr(self._rerank, "configured", False))

    # ---------- 语义索引构建（懒加载，只付一次代价） ----------

    def _build_index(self) -> None:
        """扫描 L2 文件建索引：**只在首次语义查找时执行一次**。

        实测 3108 文件建索引约 0.5~0.7 秒（一次性、且从第二次请求起为零），
        换来此后每次语义查找从"解析全部文件"降到"纯内存比余弦"。

        调用方应经 ``aget()``（走线程池）；同步调用会阻塞事件循环，
        只有启动期预热可以接受。
        """
        if self._index_built:
            return
        self._index_built = True  # 先置位：并发下允许重复建，但不会无限重复
        if not self._dir.exists():
            return
        now = time.time()
        for path in self._dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if float(raw.get("expires_at", 0) or 0) <= now:
                continue  # 已过期：不浪费索引空间（惰性删除留给精确路径）
            # **`anchor_text` 的存在性就是"这条属于 anchor 比较空间"的判据** ——
            # 它与 `vector_text`（永远是整 prompt）必须分开读，否则旧条目会在
            # 重建索引时被误判成 anchor 模式（→ 与整 prompt 的查询不可比）。
            anchor_text = str(raw.get("anchor_text") or "")
            self._index.add(
                path.stem,
                str(raw.get("agent_id") or ""),
                str(raw.get("scope") or ""),
                anchor_text or str(raw.get("vector_text") or ""),
                _entry_embedding(raw),
                anchor_used=bool(anchor_text),
            )

    def warm_up(self) -> int:
        """预热索引并返回索引条目数（供启动期在后台线程调用）。"""
        self._build_index()
        return len(self._index)

    # ---------- 读 ----------

    def get(self, system: str, prompt: str, agent_id: str = "",
            scope: str = "", anchor: str = "") -> LLMResponse | None:
        """L1 精确 → L2 召回，**判定退回旧的 3-gram 阈值规则**。

        ⚠️ **完整三级请用 `aget()`**：L3 精排要做网络调用，同步入口做不了。
        判定逻辑只有一份（`_pick`），两条入口的差别**仅是"精排输入可不可得"**。

        ⚠️ 本方法在语义未命中时会建索引（可能数百毫秒）—— async 路径必须用
        `aget()`，否则阻塞事件循环。保留同步签名兼容既有同步调用方与测试。
        """
        key = cache_key(system, prompt, scope)
        self._lookups += 1          # ★ 复用率的分母（`CHG-0208`）
        hit = self._get_exact(key)
        if hit is not None:
            return self._mark(hit, "exact")
        anchor_used = bool((anchor or "").strip())
        candidates = self._recall(
            self._anchor_text(system, prompt, anchor), agent_id, scope,
            anchor_used)
        return self._finish(self._pick(candidates, None), key, agent_id, scope,
                            anchor_used)

    async def aget(self, system: str, prompt: str, agent_id: str = "",
                   scope: str = "", anchor: str = "",
                   semantic_cache: bool = True) -> LLMResponse | None:
        """**完整三级**入口（`gateway` 走这条）。

        顺序与代价：

            L1 精确      内存查表 +（可能）一次文件读          —— 留在事件循环上
            L2 召回      纯内存 top-K；**冷启动要建索引**（数百 ms）⇒ 走线程池
            L3 精排      一次网络调用（硬超时 1.5s、按 anchor 记忆化、fail-open）

        L2 只在**真有候选**时才触发 L3 —— 没有候选就没有可比的东西，
        省掉一次网络往返（这是"不是每次查找都付一次 RTT"的关键）。

        ## `semantic_cache=False`：只留 L1（`CHG-0181`）

        **什么调用点必须关掉语义层**：**输出里含"原文逐字引文"的那一类**。

        典型是情报抽取（`intel_extract`）：它的 `phrases` 要逐字子串校验、
        `codes` 必须原文里真有、`summary` 要最长公共子串 ≥ 阈值
        （见 `tone.py` 的字段约束），而且 `tone_job` 明确"每段用**自己那段
        原文**校验"。

        ⇒ 把 A 文章的答案复用给 B 文章，**引文在 B 里根本不存在**
          ⇒ 逐字校验把它们**全部丢掉** ⇒ 抽取返回**空**。
          这正是 `CHG-0069` 的症状（"4/5 条 summary 为空"）。

        ⚠️ 注意这不只是"相似度算错了"：**即使把比较文本换成纯正文，
          语义复用对这类输出依然不安全** —— 同一条新闻被转载改写后，
          引文照样不在新正文里。**瓶颈是输出契约，不是文本表征。**
          （`CHG-0069` 自己列的三个候选里，第三条"抽取类显式禁缓存"就是它。）
        """
        key = cache_key(system, prompt, scope)
        self._lookups += 1          # ★ 复用率的分母（`CHG-0208`）
        hit = self._get_exact(key)
        if hit is not None:
            return self._mark(hit, "exact")
        if not semantic_cache:
            self._semantic_disabled += 1
            return None

        anchor_text = self._anchor_text(system, prompt, anchor)
        anchor_used = bool((anchor or "").strip())

        # ★★ 结构性护栏：**L3 只在 anchor 模式生效**（`CHG-0180`）。
        #
        # ## 为什么这不是"保守"，而是必须（实测反证）
        #
        # 不传 anchor 时比较的是**整 prompt**，而整 prompt 的 embedding 余弦
        # 在两条**完全不同**的资讯之间实测是 **0.9604 / 0.9768 / 0.9657**
        # —— 全部远高于精排阈值 `0.80`。
        #
        # ⇒ 若允许 prompt 模式走 L3，那么**同 agent 同层的任何候选都会被接受**，
        #   语义缓存退化成「返回这个 agent+层级的第一条缓存答案」，
        #   而且 `cache_hit=True`、耗时尚且变短、**不报错**。
        #
        # 换句话说：**「只把开关打开」会比不开更糟**，而且是静默变糟。
        # 这道护栏把它变成结构上不可能，而不是靠"记得别单独开开关"。
        #
        # ⚠️ 护栏移到召回之前（`CHG-0203`）：判定器换层之后，"要不要出网"这件事
        #    必须在**算查询向量之前**就决定，否则护栏只挡住了判定、没挡住出网。
        vec: list[float] | None = None
        if not anchor_used:
            if self.rerank_enabled or self.judge_enabled:
                # 可观测：让"开关开了但这批调用没进 anchor 模式"看得见，
                # 否则它会表现成"精排装了但没效果"（而原因在调用点没传 anchor）。
                self._l3_skipped_prompt_mode += 1
        elif self.rerank_enabled or self.judge_enabled:
            # 走**带记忆化**的 `_embed_text`（不是直接调客户端）：
            # 同一次分析里 4~15 次调用共享同一个 anchor ⇒ 只出网一次。
            vec = await self._embed_text(anchor_text)   # 失败/超时 ⇒ None

        candidates, judge, threshold = await self._recall_and_judge(
            anchor_text, anchor_used, agent_id, scope, vec)
        if not candidates:
            return None

        picked = self._pick(candidates, judge, threshold=threshold)
        if judge is not None:
            self._l3_judged += 1
            if picked:
                self._l3_hits += 1
            else:
                self._l3_misses += 1
        return self._finish(picked, key, agent_id, scope, anchor_used)

    async def _recall_and_judge(
        self, anchor_text: str, anchor_used: bool, agent_id: str, scope: str,
        vec: list[float] | None,
    ) -> tuple[list[tuple[str, float]], dict[str, float] | None, float | None]:
        """**召回 → 判定**两段，返回 `(候选, 判定分, 判定的那把尺子)`（`CHG-0203`）。

        ## 为什么把这两段放在一个方法里

        它们是**一对**：判定分只在"召回层给了哪些候选"上才有意义，
        而候选的取法又取决于判定器要不要文本。分开写会让"用了 A 的召回
        配 B 的判定"这种错配**编译期看不出来**。

        ## 两条路径

            召回：桶里有向量 ⇒ embedding 余弦 top-K（recall@12 **100%**）
                  全桶无向量 ⇒ 3-gram top-K（recall@12 **40%**，降级）
            判定：cross-encoder 可用 ⇒ 它对 K 条原始文本打分（AUC **0.775**）
                  不可用/失败   ⇒ 退回 embedding 余弦（AUC 0.573）
                  连向量也没有   ⇒ 退回 3-gram 阈值（`_pick` 的 `judge=None` 支路）

        `judge` 为 `None` = **判定层没有可用信号**，`_pick` 会走阈值规则 ——
        与改动前逐字一致，这就是"升级不倒退"的保证。
        """
        import asyncio

        # ---------- 召回层 ----------
        candidates: list[tuple[str, float]] | None = None
        if vec is not None:
            k = max(self._recall_k, self._rerank_top_k)
            candidates = await asyncio.to_thread(
                self._index.top_k_by_embedding, vec, agent_id, scope, k,
                anchor_used=anchor_used)
        if candidates is None:
            # 全桶无向量（今天的存量就是这种情况：`CHG-0202` 实测 0/5007）
            # ⇒ 退回 3-gram 召回。**不假装"没有候选"** —— 那会让旧条目
            # 在升级后突然一条都命中不了（"升级即退化"，而且不报错）。
            candidates = await asyncio.to_thread(
                self._recall, anchor_text, agent_id, scope, anchor_used)
            embed_scores = None
        else:
            embed_scores = dict(candidates)
        if not candidates:
            return [], None, None

        # ★★ **判定层与召回层共用同一道 anchor 护栏**（`CHG-0203` 补上）。
        #
        # 为什么判定层也必须挡：`CHG-0180` 的护栏理由是"整 prompt 的相似度
        # 普遍偏高"——**那条理由对 cross-encoder 同样成立**（两条无关的长 prompt
        # 在 reranker 眼里也是"高度相关"，因为它们共享同一套骨架与 schema）。
        # ⇒ 若只挡 embedding 不挡 reranker，换判定器就把那道护栏**绕过去了**，
        #   而且是静默绕过（`cache_hit=True`、耗时更短、不报错）。
        # ⚠️ 这个洞是我自己写出来的，被 `test_l3_never_runs_in_prompt_mode`
        #   的 `l3_attempts == 1` 断言当场抓住（实测拿到 2）。
        if not anchor_used:
            return candidates, None, None

        # `l3_attempts` 只记 **anchor 模式**的判定尝试 —— 口径与改动前一致。
        self._l3_attempts += 1

        # ---------- 判定层 ----------
        if self.judge_enabled:
            k = min(self._rerank_top_k, len(candidates))
            head = candidates[:k]
            docs = [self._index.text_of(key, agent_id, scope,
                                        anchor_used=anchor_used)
                    for key, _ in head]
            scored = await self._rerank_scores(anchor_text, docs)
            if scored is not None:
                # ★ 判定的尺子随判定器变：这里返回 **rerank 的阈值**。
                return (head,
                        dict(zip((key for key, _ in head), scored, strict=True)),
                        self._rerank_threshold)
            # cross-encoder 不可用 ⇒ **退回**，并记账。
            # 不记账的话，"判定器挂了"会表现成"判定质量下降"，而处置完全不同。
            self._rerank_fallbacks += 1

        return candidates, embed_scores, (
            self._embed_threshold if embed_scores else None)

    async def _rerank_scores(self, query: str, docs: list[str],
                             ) -> list[float] | None:
        """调 cross-encoder 打分。**绝不抛**；不可用返回 `None`。

        记 `_rerank_attempts` 的地方**只有这里**，且只在真的拿到分数时自增 ——
        否则"调了但失败"与"调成功"会被算成同一件事。
        """
        if self._rerank is None or not docs or not any(docs):
            return None
        try:
            scored = await self._rerank.rerank(query, docs)
        except Exception as exc:  # noqa: BLE001 客户端本该不抛，这里兜底
            logger.warning("cross-encoder 判定客户端抛出（按不可用处置）：%r", exc)
            return None
        # ⚠️ **同长**是硬契约：短了说明服务端少给了条目 ⇒ 不能当成
        #    "这些文档不相关"（那是**编造**分数）。
        if scored is None or len(scored) != len(docs):
            return None
        self._rerank_attempts += 1
        if max(scored) >= self._rerank_threshold:
            self._rerank_hits += 1
        return scored

    def _get_exact(self, key: str) -> dict[str, Any] | None:
        return self._load(key)

    # ---------- 三级匹配（判定只有这一份实现） ----------

    def _anchor_text(self, system: str, prompt: str, anchor: str) -> str:
        """**比较用的文本**：`anchor`（变量部分）优先，缺省回落整 prompt。

        回落是**故意的向后兼容**：不传 `anchor` 的旧调用点，其行为与
        改动前**逐字一致**（比较的仍是 `normalize(system + prompt)`）。
        """
        if (anchor or "").strip():
            return normalize_text(anchor)
        return normalize_text(system + prompt)

    async def _embed_text(self, text: str) -> list[float] | None:
        """anchor 文本 → 向量，**带记忆化**。L3 查询与写入两条路都走它。

        一次投研分析有 4~15 次 LLM 调用、**每次调用前都要查缓存**。
        不记忆化就是 N × RTT（实测约 420 ms/次 ⇒ 一次分析多付 1.7~6.3 秒）；
        记忆化后是 **O(1) / 用户问句**。

        ⚠️ 记忆化刻意放在**缓存层**而不是客户端里：放客户端的话，
        换一个实现（含测试替身）就静默失去这个保证（见 `_query_vecs` 的说明）。
        """
        if not self.rerank_enabled or not (text or "").strip():
            return None
        vec_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        hit = self._query_vecs.get(vec_key)
        if hit is not None:
            self._query_vec_hits += 1
            return hit
        vec = await self._embed.embed(text)
        if vec:
            if len(self._query_vecs) >= _QUERY_VEC_MAX:
                # 有界：长跑进程不能靠它无限增长（与 `_INDEX_MAX_ENTRIES` 同一纪律）
                self._query_vecs.pop(next(iter(self._query_vecs)), None)
            self._query_vecs[vec_key] = vec
        return vec

    async def anchor_embedding(self, system: str, prompt: str, anchor: str,
                               ) -> list[float] | None:
        """给 `put()` 用：算好 anchor 向量再传进去（`put` 刻意保持同步）。

        ⚠️ 它必须用**同一个** `_anchor_text`，否则"写进去的文本"与"查出来时
        比的文本"可能不一致 —— 那是最难查的一类 bug（行为看着正常，只是永不命中）。
        这里刻意不复制那段逻辑，只调用同一个方法。

        与 L3 查询**共用记忆化**：同一次分析里写入与查询用的是同一个
        anchor ⇒ 只付一次网络往返。
        """
        return await self._embed_text(self._anchor_text(system, prompt, anchor))

    def _recall(self, anchor_text: str, agent_id: str, scope: str,
                anchor_used: bool) -> list[tuple[str, float]]:
        """L2：同桶 top-K 召回。**纯 CPU、无网络、无阈值**（冷启动含建索引）。"""
        if not agent_id or not self._dir.exists():
            return []
        if not self._index_built:
            self._build_index()
        return self._index.top_k(
            _ngram_vector(anchor_text), agent_id, scope, self._recall_k,
            anchor_used=anchor_used)

    def _pick(self, candidates: list[tuple[str, float]],
              rerank: dict[str, float] | None,
              *, threshold: float | None = None) -> str | None:
        """候选 → 命中键。**唯一的判定实现**（sync / async 都走它）。

        `rerank` 为 `None` = **判定层没有可用信号**（没配 / 调用失败 / 候选没有向量）
        ⇒ 退回旧的 3-gram 阈值规则，行为与两级时代一致。

        `threshold` = **判定分的尺子**。`None` 表示"`rerank` 是 embedding 余弦"，
        用 `_embed_threshold`；传了值就说明判定器换了（cross-encoder）。

        ⚠️ 复用必须同时满足两个作用域，缺一不可（由**分桶**保证，不在这里判）：
        `scope`（模型层级 / json_mode）与 `agent_id`（防跨 Agent 串台）。
        """
        if not candidates:
            return None
        if rerank is None:
            best_key, best_coarse = candidates[0]     # top_k 已按分数降序
            return best_key if best_coarse >= self._threshold else None
        # ⚠️ 判定分的**尺子随判定器变**（`CHG-0203`）：cross-encoder 的分数
        #    与 embedding 余弦不是同一把尺子（实测：reranker 正样本 p50 0.9844 /
        #    负样本 0.6865；embedding 正 0.8604 / 负 0.8221）。
        #    用错尺子不会报错，只会让判定整体偏移 —— 所以阈值**显式传进来**。
        thr = self._embed_threshold if threshold is None else float(threshold)
        scored = [(k, rerank[k]) for k, _coarse in candidates if k in rerank]
        if not scored:
            return None
        scored.sort(key=lambda kv: kv[1], reverse=True)
        best_key, best_sim = scored[0]
        self._observe_l3_sim(best_sim, thr)   # ★ 观测点**就在唯一判定处**
        return best_key if best_sim >= thr else None

    #: 直方图分桶宽度（20 桶覆盖 [0,1]）。0.05 是刻意的：
    #: 比它细就要求样本量很大才不抖，比它粗就看不出"阈值是不是切在分布上"。
    _SIM_BUCKET = 0.05

    @staticmethod
    def _sim_bucket_order(label: str) -> float:
        """直方图标签 → 排序键。

        ⚠️ **不能直接对标签做字符串排序**：`<`(0x3C) 的码位**大于**数字
        (0x30-0x39)，直接 `sorted()` 会把首桶 `<0.05` 排到 `0.95-1.00` **后面**，
        读起来像"负数最高"。这类"看起来排好了、其实顺序是错的"图表
        正是会让人把分布读反的东西。
        """
        if label.startswith("<"):
            return -1.0
        return float(label.split("-", 1)[0])

    def _observe_l3_sim(self, sim: float, threshold: float | None = None) -> None:
        """记录一次 L3 判定的 best_sim（`CHG-0184`）。

        这里是**唯一**的写入点，且被 `_pick` 调用 ⇒ 直方图与判定规则
        **不可能漂移**（不会出现"判定改了口径、直方图还在按旧口径记"）。

        余弦可以是负数（本项目 `cosine()` 不做截断）⇒ 负数归入首桶，
        首桶标签写作 `<0.05` 而不是 `0.00-0.05`，避免把"负相关"说成"接近正交"。

        ⚠️ `threshold` 必须与**当次判定用的那把尺子**一致（`CHG-0203`）：
        换判定器后若还拿 `_embed_threshold` 判"贴不贴阈值"，
        告警位会按错误的边界响 —— **一个用错尺子的告警比没有告警更坏**
        （它会让人去调一个本来就没错的参数）。
        """
        if sim != sim:                       # NaN：绝不静默灌进首桶
            return
        thr = self._embed_threshold if threshold is None else float(threshold)
        self._l3_last_sim = float(sim)
        idx = int(sim / self._SIM_BUCKET)
        idx = 0 if idx < 0 else (19 if idx > 19 else idx)
        lo = idx * self._SIM_BUCKET
        label = (f"<{self._SIM_BUCKET:.2f}" if idx == 0
                 else f"{lo:.2f}-{lo + self._SIM_BUCKET:.2f}")
        self._l3_sim_hist[label] = self._l3_sim_hist.get(label, 0) + 1
        #: 「阈值正切在一团连续分布上」的危险信号：命中但只比阈值高一个桶。
        #: 这类命中**随时可能因为一句改写掉到阈值下**，反过来紧挨着下面的
        #: 拒绝样本也可能只是差一点措辞 ⇒ 说明桶里混着两类，阈值位置不可信。
        if thr <= sim < thr + self._SIM_BUCKET:
            self._l3_hits_near_threshold += 1

    def _finish(self, picked: str | None, exclude: str, agent_id: str,
                scope: str, anchor_used: bool) -> LLMResponse | None:
        """把选中的 key 读成响应；读不到（过期/被删）就顺手摘掉索引条目。"""
        if picked is None or picked == exclude:
            return None
        entry = self._load(picked)
        if entry is None:
            self._index.remove(picked, agent_id, scope, anchor_used=anchor_used)
            return None
        return self._mark(entry, "semantic")

    def _load(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        mem = self._memory.get(key)
        if mem:
            expires_at, entry = mem
            if expires_at > now:
                return entry
            self._memory.pop(key, None)
        path = self._file_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if data.get("expires_at", 0) <= now:
            path.unlink(missing_ok=True)
            return None
        self._memory[key] = (data["expires_at"], data)
        return data

    # ---------- 写 ----------

    def put(self, system: str, prompt: str, response: LLMResponse,
            agent_id: str = "", scope: str = "",
            ttl_seconds: float | None = None, *,
            anchor: str = "", embedding: list[float] | None = None) -> None:
        """写入缓存 + 增量更新语义索引。

        `ttl_seconds` 是**按调用覆盖**的存活时间（None = 用实例默认值）。
        为什么需要它：同一份缓存目录里装着寿命差两个数量级的东西 ——
        新闻情绪的有效期是分钟级，而"某公司主营与某题材是否相关"是季度级。
        用一个全局 TTL 只能迁就其中一边：迁就新闻就会天天重算打分，
        迁就打分就会拿昨天的新闻情绪判今天的盘（更糟）。
        所以过期时间**按条目**存（`expires_at` 一直在条目里），TTL 由调用方定。

        `anchor` = **比较用的变量文本**（见 `_anchor_text`）。
        `embedding` = **已经算好的** anchor 向量，由**调用方**（async 路径）
        算好传进来 —— 本方法刻意保持同步，因为 `put` 在网关里就在事件循环上，
        而改造它成 async 会波及所有既有调用方与 3 个测试文件。
        不传 = 该条目**不参与 L3 精排**（仍可被精确命中与 L2 召回）。
        """
        ttl = self._ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        expires_at = time.time() + ttl
        anchor_given = bool((anchor or "").strip())
        anchor_text = normalize_text(anchor) if anchor_given else ""
        entry: dict[str, Any] = {
            **response.model_dump(mode="json"),
            # ⚠️ `vector_text` 的**语义不变**：它一直是"整 prompt 的 normalize"。
            #    为什么不让它也变成 anchor：离线脚本（含 CI 审计）按它统计
            #    "骨架占比"，改语义会让那些数字**静默变味**。
            "vector_text": normalize_text(system + prompt),
            "agent_id": agent_id,
            "scope": scope,
            # ★★ 写入时刻（`CHG-0209`）。TTL 的**另一半**：只有 `expires_at`
            #    时能算出"还剩多久"，但算不出"**已经被复用过多长时间的那条**"
            #    —— 而后者才是 TTL 该取多少的依据。
            #    ⚠️ 存量条目没有这个字段 ⇒ `_mark` 会记进 `reuse_age_unknown`，
            #    不会猜一个 0（见那里的注释）。
            "created_at": time.time(),
        }
        if anchor_given:
            # **只在真的传了 anchor 时才写** —— 它的存在性就是"这条属于
            # anchor 比较空间"的判据（见 `_SemanticIndex._bucket_key`）。
            # 无条件写会让旧条目在重建索引时被误判成 anchor 模式。
            entry["anchor_text"] = anchor_text
        if embedding:
            entry["embedding"] = encode_vector(embedding)
        key = cache_key(system, prompt, scope)
        self._memory[key] = (expires_at, entry)
        # 索引增量更新：避免"新写入的条目要等下次全量重建才能被语义命中"
        self._index.add(key, agent_id, scope,
                        anchor_text or entry["vector_text"], embedding,
                        anchor_used=anchor_given)
        self._file_path(key).write_text(
            json.dumps({"expires_at": expires_at, **entry}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _file_path(self, key: str) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir / f"{key}.json"

    def _mark(self, entry: dict[str, Any], kind: str) -> LLMResponse:
        # ★ 命中分类**只在这里**发生（`CHG-0208`）—— 三个调用点
        #   （`get` / `aget` 的精确分支、`_finish` 的语义分支）全部经此，
        #   所以计数不可能与判定逻辑漂移。
        if kind == "exact":
            self._hits_exact += 1
        else:
            self._hits_semantic += 1
        # ★★ 复用**年龄**也在这里记（`CHG-0209`）—— 同一个理由：
        #    `_mark` 是**唯一**同时拿得到「命中类型」与「被命中的那条 entry」
        #    的地方。放到 `_finish` 里会漏掉精确命中，放到调用方会漏掉语义命中。
        created = entry.get("created_at")
        if isinstance(created, (int, float)) and created > 0:
            age = max(0.0, time.time() - float(created))
            hist = (self._reuse_age_exact if kind == "exact"
                    else self._reuse_age_semantic)
            label = reuse_age_label(age)
            hist[label] = hist.get(label, 0) + 1
        else:
            # 存量条目（`created_at` 之前写的）⇒ **记成未知，不猜**。
            self._reuse_age_unknown += 1
        data = {k: v for k, v in entry.items() if k in LLMResponse.model_fields}
        resp = LLMResponse.model_validate(data)
        resp.cache_hit = True
        resp.cache_kind = kind  # type: ignore[assignment]
        # ★ 2026-10-05（`CHG-0177`）：命中时**清掉**排队/模型侧耗时。
        #
        # 存进缓存的是**原始那次调用**的响应，它的 `wait_ms`/`model_ms` 描述的是
        # 那一次，不是这一次。这一次根本没碰模型 —— 既没排队也没让模型干活。
        # 不清的后果很具体：任何"本地平均排队时长"的统计会把命中行也算进去，
        # **命中越多、面板上的排队越显得严重**（方向反了，且没有任何报错）。
        #
        # ⚠️ 只清这两个**新**字段，`latency_ms` 保持原样（逐字不变）：
        #    它已有消费者（`metrics` 的延迟分位等）依赖"命中行回放原始耗时"，
        #    改它是另一件事，不在本次半径内。
        resp.wait_ms = None
        resp.model_ms = None
        return resp

    # ---------- 可观测性 ----------

    def stats(self) -> dict[str, Any]:
        """缓存与索引规模 + **三级匹配的可用性与命中分布**（供 /health）。

        ## 为什么必须把 L3 的计数也报出来

        三级缓存最大的风险不是"慢"，是**"以为开了其实没生效"**：
        精排器没配 / key 缺失 / 端点超时 ⇒ L3 静默退回阈值规则，
        而**表面行为完全正常**（照样有缓存命中）。没有这几个计数，
        这种"静默降级"只能靠人去读日志。
        """
        return {
            "memory_entries": len(self._memory),
            "index_entries": len(self._index),
            "index_built": self._index_built,
            "files": (len(list(self._dir.glob("*.json")))
                      if self._dir.exists() else 0),
            # ---- 三级匹配（`CHG-0178`）----
            "rerank_enabled": self.rerank_enabled,
            "embed_threshold": self._embed_threshold,
            "recall_k": self._recall_k,
            # `None` = **没有精排器**（未启用），不是"0 次调用" —— 两者必须分开
            "embed_client": (None if self._embed is None
                             else getattr(self._embed, "stats", lambda: {})()),
            "l3_attempts": self._l3_attempts,
            # ★ 「进了判定阶段」与「判定层真的给了分」是**两件事**（`CHG-0203`）：
            #   attempts>0 & judged=0 ⇒ **每次都在降级**（端点挂了 / 全桶无向量）——
            #   这正是 `CHG-0202` 里"0/5007 带向量却被读成 L3 没跑"那个盲点。
            "l3_judged": self._l3_judged,
            "l3_hits": self._l3_hits,
            "l3_misses": self._l3_misses,
            # ---- 阈值可标定化（`CHG-0184`）----
            # 有分布才谈得上"标定"：只有 hits/misses 两个计数时，
            # "阈值定高了" 与 "根本没有相似候选" 在面板上无法区分。
            "l3_sim_hist": {k: self._l3_sim_hist[k]
                            for k in sorted(self._l3_sim_hist,
                                            key=self._sim_bucket_order)},
            "l3_last_sim": self._l3_last_sim,
            # 命中只比阈值高一个桶 ⇒ 阈值正切在连续分布上（危险信号，不是好消息）
            "l3_hits_near_threshold": self._l3_hits_near_threshold,
            # 「开关开了但这批调用没进 anchor 模式」必须看得见（`CHG-0180`）
            "l3_skipped_prompt_mode": self._l3_skipped_prompt_mode,
            # 「该调用点显式关掉了语义层」也要看得见（`CHG-0181`）——
            # 否则"语义命中率下降"会被误判成"精排不好使"，
            # 而真因是那类调用点**本来就不该复用**。
            "semantic_disabled": self._semantic_disabled,
            # 记忆化的效果必须看得见：`hits` 高 = N×RTT 真的被压成了 O(1)/问句
            "query_vec_hits": self._query_vec_hits,
            "query_vec_entries": len(self._query_vecs),
            # ---- ★ 判定器换层（`CHG-0203`）----
            # 这一组回答的是"**判定权在谁手里**"，与上面的 `l3_*`（"判了几次"）
            # 是**两个问题**。合成一个数会让下面两种状态长得一样：
            #     · cross-encoder 在跑，命中率 20%
            #     · cross-encoder 挂了、退回 bi-encoder，命中率 20%
            # 而处置完全不同（一个调阈值、一个查端点）。
            "judge": "cross-encoder" if self.judge_enabled else (
                "embedding" if self.rerank_enabled else "3-gram"),
            "rerank_threshold": self._rerank_threshold,
            "rerank_top_k": self._rerank_top_k,
            # `None` = **没配 cross-encoder**，不是"0 次调用" —— 必须分开读
            "rerank_client": (None if self._rerank is None
                              else getattr(self._rerank, "stats", lambda: {})()),
            # 判定真的走了 cross-encoder 的次数（只在**拿到分数**时自增）
            "rerank_attempts": self._rerank_attempts,
            "rerank_hits": self._rerank_hits,
            # cross-encoder 不可用而**退回** embedding 判定的次数。
            # 没有它，"判定器挂了"会表现成"判定质量下降"。
            "rerank_fallbacks": self._rerank_fallbacks,
            # ---- ★★ 复用率（`CHG-0208`）----
            # 这一组回答的是**唯一一个业务问题**：缓存到底有没有在用、有没有在变聪明。
            # ⚠️ 与上面所有计数的关键区别：上面都是"某一层内部"的数，
            #    这一组是**整条链路的入口分母** —— 没有它，命中数再多也算不出比率。
            "lookups": self._lookups,
            "hits_exact": self._hits_exact,
            "hits_semantic": self._hits_semantic,
            # ★ 由减法推出，**不单独维护**：三个独立计数器一定会漂移
            #   （理由见 `__init__` 的 `_lookups` 注释）。
            #   刻意**不加 `max(0, …)`** —— 万一日后有人把 `_mark` 接到
            #   `get`/`aget` 之外的路径上，这里会**变成负数并当场暴露**，
            #   而 `max(0, …)` 会把它悄悄压成 0（假绿）。
            "misses": self._lookups - self._hits_exact - self._hits_semantic,
            # `None` = **一次查找都还没有**，不是"复用率 0%" ——
            # 「没量到 ≠ 量到 0」（本项目纪律）。`0.0` 会被读成"缓存完全没用"。
            "reuse_rate": (
                None if self._lookups == 0
                else round((self._hits_exact + self._hits_semantic)
                           / self._lookups, 4)),
            "exact_rate": (
                None if self._lookups == 0
                else round(self._hits_exact / self._lookups, 4)),
            # ---- ★★ 复用**年龄**（`CHG-0209`）----
            # 这一组是 **TTL 的直接依据**：每个桶的上界就是一个候选 TTL，
            # 桶里的计数就是"TTL 取到那里能吃到多少次复用"。
            # ⚠️ 与 `reuse_rate` 是两个问题：那个答"有没有复用"，
            #    这个答"复用的是多久以前写的"。**只有后者能定 TTL。**
            "reuse_age_exact": self._ordered_age(self._reuse_age_exact),
            "reuse_age_semantic": self._ordered_age(self._reuse_age_semantic),
            # ★ 拿不到 `created_at` 的命中数（存量条目）——
            #   **没量到 ≠ 量到 0**：不报这个数，那部分命中会被读成
            #   "复用都发生在 1 分钟内"。
            "reuse_age_unknown": self._reuse_age_unknown,
        }

    @staticmethod
    def _ordered_age(hist: dict[str, int]) -> dict[str, int]:
        """按 `REUSE_AGE_BUCKETS` 的**刻度顺序**输出（不是字典顺序）。

        直方图的读法是"累积到某一档"，乱序输出会让人读错累积方向；
        且空桶要**显式补 0** —— 缺一档与"那一档是 0 次"必须长得不一样。
        """
        return {label: hist.get(label, 0) for label, _upper in REUSE_AGE_BUCKETS}
