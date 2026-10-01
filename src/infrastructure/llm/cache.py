"""LLM语义缓存（L1内存 + L2本地文件，两级查找 + 内存语义索引）。

精确命中：规范化prompt的SHA256键；
语义命中：字符3-gram余弦相似度≥阈值（无依赖轻量向量，Demo级）。

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
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from src.infrastructure.llm.models import LLMResponse

_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

#: 3-gram 向量在索引里缓存的条目上限（防长跑进程索引无限增长）。
#: 超限后语义查找退化为"只比较最近 N 条"，精确命中不受影响。
_INDEX_MAX_ENTRIES = 20000


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

    不传 `scope` 时退化为"只看 system+prompt"，这条路径给**审计用的
    prompt_hash** 使用（它要标识 prompt 本身，与调用条件无关）。
    """
    parts = [normalize_text(system), normalize_text(prompt)]
    if scope:
        parts.insert(0, scope)
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _ngram_vector(text: str, n: int = 3) -> Counter[str]:
    if len(text) < n:
        return Counter([text]) if text else Counter()
    return Counter(text[i : i + n] for i in range(len(text) - n + 1))


def cosine_similarity(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(cnt * b.get(g, 0) for g, cnt in a.items())
    norm_a = math.sqrt(sum(c * c for c in a.values()))
    norm_b = math.sqrt(sum(c * c for c in b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class _SemanticIndex:
    """语义索引：``(agent_id, scope)`` → ``{key: vector}`` 分桶。

    为什么分桶而不是一条平铺的 list：语义复用**必须同时满足**
    `agent_id` 与 `scope` 两个作用域（见 ``_get_semantic`` 的说明）。
    分桶让"过滤"变成"直接取桶"，不必遍历全部条目再丢弃。
    """

    __slots__ = ("_buckets", "_order")

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], dict[str, Counter[str]]] = {}
        #: 插入顺序（用于超限时淘汰最旧条目）
        self._order: list[tuple[tuple[str, str], str]] = []

    def __len__(self) -> int:
        return sum(len(b) for b in self._buckets.values())

    def add(self, key: str, agent_id: str, scope: str, vector_text: str) -> None:
        if not agent_id:
            return  # 无 agent_id 标记的条目不参与语义复用（防跨 Agent 串台）
        bucket_key = (agent_id, scope)
        bucket = self._buckets.setdefault(bucket_key, {})
        if key in bucket:
            return
        bucket[key] = _ngram_vector(vector_text)
        self._order.append((bucket_key, key))
        if len(self._order) > _INDEX_MAX_ENTRIES:
            old_bucket, old_key = self._order.pop(0)
            self._buckets.get(old_bucket, {}).pop(old_key, None)

    def remove(self, key: str, agent_id: str, scope: str) -> None:
        self._buckets.get((agent_id, scope), {}).pop(key, None)

    def best(
        self, query_vec: Counter[str], agent_id: str, scope: str, threshold: float,
    ) -> str | None:
        """在同桶内取相似度最高且 ≥ 阈值的 key（无命中返回 None）。"""
        bucket = self._buckets.get((agent_id, scope))
        if not bucket:
            return None
        best_key, best_score = None, threshold
        for key, vec in bucket.items():
            score = cosine_similarity(query_vec, vec)
            if score >= best_score:
                best_key, best_score = key, score
        return best_key


class LLMCache:
    """两级LLM响应缓存（进程内存 → 本地JSON文件）+ 内存语义索引。"""

    def __init__(
        self,
        cache_dir: str | None = None,
        ttl_hours: float = 24.0,
        semantic_threshold: float = 0.85,
    ) -> None:
        if cache_dir is None:      # 默认从 registry 取（CHG-0071）
            from src.infrastructure.catalog.data_stores import store_rel

            cache_dir = store_rel("llm_cache")
        self._dir = Path(cache_dir)
        self._ttl_seconds = ttl_hours * 3600
        self._threshold = semantic_threshold
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}
        self._index = _SemanticIndex()
        self._index_built = False

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
            self._index.add(
                path.stem,
                str(raw.get("agent_id") or ""),
                str(raw.get("scope") or ""),
                str(raw.get("vector_text") or ""),
            )

    def warm_up(self) -> int:
        """预热索引并返回索引条目数（供启动期在后台线程调用）。"""
        self._build_index()
        return len(self._index)

    # ---------- 读 ----------

    def get(self, system: str, prompt: str, agent_id: str = "",
            scope: str = "") -> LLMResponse | None:
        """先精确后语义。返回的response已带cache_hit/cache_kind标记。

        ⚠️ 这是**同步**入口：语义未命中时会建索引（可能数百毫秒）。
        async 调用路径请用 ``aget()``，否则会阻塞事件循环。
        保留同步签名兼容既有同步调用方与测试。
        """
        key = cache_key(system, prompt, scope)
        hit = self._get_exact(key)
        if hit is not None:
            return self._mark(hit, "exact")
        return self._get_semantic(system, prompt, exclude=key, agent_id=agent_id,
                                  scope=scope)

    async def aget(self, system: str, prompt: str, agent_id: str = "",
                   scope: str = "") -> LLMResponse | None:
        """异步入口：语义索引构建/查找走线程池，不阻塞事件循环。

        这是 LLM 网关应当使用的入口。精确命中是"内存查表 + 单文件读"，
        代价极小，留在事件循环上反而省一次线程切换。
        """
        import asyncio

        key = cache_key(system, prompt, scope)
        hit = self._get_exact(key)
        if hit is not None:
            return self._mark(hit, "exact")
        return await asyncio.to_thread(
            self._get_semantic, system, prompt, key, agent_id, scope)

    def _get_exact(self, key: str) -> dict[str, Any] | None:
        return self._load(key)

    def _get_semantic(
        self, system: str, prompt: str, exclude: str, agent_id: str = "",
        scope: str = "",
    ) -> LLMResponse | None:
        """内存索引内取相似度最高且 ≥ 阈值的条目。

        复用必须同时满足两个作用域，缺一不可：

        - **同 `scope`**（模型层级 / json_mode）—— 否则 1.5B 的粗糙结论会被
          决策层当结论用；
        - **同 `agent_id`** —— 不同 Agent 的输入文本大量重叠时相似度会越界，
          跨 Agent 复用会造成结论串台。无 agent_id 标记的历史条目不参与复用。
        """
        if not agent_id or not self._dir.exists():
            return None
        if not self._index_built:
            self._build_index()
        # 命中项的 vector_text 可能来自索引，但响应体仍需从 L2 读（或 L1 内存）
        query_vec = _ngram_vector(normalize_text(system + prompt))
        best_key = self._index.best(query_vec, agent_id, scope, self._threshold)
        if best_key is None or best_key == exclude:
            return None
        entry = self._load(best_key)
        if entry is None:
            # 索引里的条目已过期/被删：顺手摘掉，避免下次再白找一趟
            self._index.remove(best_key, agent_id, scope)
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
            ttl_seconds: float | None = None) -> None:
        """写入缓存 + 增量更新语义索引。

        `ttl_seconds` 是**按调用覆盖**的存活时间（None = 用实例默认值）。
        为什么需要它：同一份缓存目录里装着寿命差两个数量级的东西 ——
        新闻情绪的有效期是分钟级，而"某公司主营与某题材是否相关"是季度级。
        用一个全局 TTL 只能迁就其中一边：迁就新闻就会天天重算打分，
        迁就打分就会拿昨天的新闻情绪判今天的盘（更糟）。
        所以过期时间**按条目**存（`expires_at` 一直在条目里），TTL 由调用方定。
        """
        ttl = self._ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        expires_at = time.time() + ttl
        entry = {
            **response.model_dump(mode="json"),
            "vector_text": normalize_text(system + prompt),
            "agent_id": agent_id,
            "scope": scope,
        }
        key = cache_key(system, prompt, scope)
        self._memory[key] = (expires_at, entry)
        # 索引增量更新：避免"新写入的条目要等下次全量重建才能被语义命中"
        self._index.add(key, agent_id, scope, entry["vector_text"])
        self._file_path(key).write_text(
            json.dumps({"expires_at": expires_at, **entry}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _file_path(self, key: str) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir / f"{key}.json"

    @staticmethod
    def _mark(entry: dict[str, Any], kind: str) -> LLMResponse:
        data = {k: v for k, v in entry.items() if k in LLMResponse.model_fields}
        resp = LLMResponse.model_validate(data)
        resp.cache_hit = True
        resp.cache_kind = kind  # type: ignore[assignment]
        return resp

    # ---------- 可观测性 ----------

    def stats(self) -> dict[str, Any]:
        """缓存与索引规模（供 /health 与容量评估使用）。"""
        return {
            "memory_entries": len(self._memory),
            "index_entries": len(self._index),
            "index_built": self._index_built,
            "files": (len(list(self._dir.glob("*.json")))
                      if self._dir.exists() else 0),
        }
