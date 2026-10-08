"""cross-encoder 判定客户端 —— **把「是不是同一件事」的判定权交给它**（`CHG-0203`）。

## 它和 `embedding.py` 是什么关系

两者都打同一个 SiliconFlow 端点，但**干的是两件不同的事**：

    `EmbeddingClient`  bi-encoder  —— 每段文本**各算一个向量**，再比余弦
    `RerankClient`     cross-encoder —— 把 query 与每篇文档**拼在一起**过一遍模型

⇒ 后者能看见"两段文本**一起**长什么样"，所以对
「**一词之差但含义相反**」（`加息` / `降息`、`涨` / `跌`、`A股` / `港股`）
这类判别明显更强。实测同一批 40 对（`CHG-0199`）：

    判定 AUC：`BAAI/bge-m3` **0.573**（≈抛硬币） → `BAAI/bge-reranker-v2-m3` **0.775**
    操作点  ：现行 @0.80 = 召回 50% / 假阳 65%
              reranker @0.95 = 召回 60% / 假阳 **15%**

**为什么 bi-encoder 在这个任务上天生不行**（`CHG-0187` 的机理）：
正样本是**真改写**（字面重叠低 ⇒ 向量判"远"），难负样本是**改一两个字**
（字面重叠高 ⇒ 向量判"近"）⇒ 稠密向量被**字面重叠**主导，
而字面重叠在难负样本上恰好最大。

## 三条纪律（与 `embedding.py` 逐条对应）

1. **走云端，不进 GPU 生成域**：`providers.py` 的闸只包住 `/api/chat`，
   新开一个本地出站点会成为**第三类无闸的 Ollama 消费者**，去抢那个
   已实测"排队占墙钟 82.6%"的唯一槽位。
2. **硬超时 + fail-open**：任何失败都返回 `None`，**绝不抛** ——
   判定失败只该让这一次"语义未命中"，不该让整条分析挂掉。
3. **★ 熔断（`CHG-0206` 起 `embedding.py` **也有**了，两者共用 `judge_breaker()`）**：
   连续失败到阈值后**冷却一段时间**，期间直接返回 `None` 不再出网。
   理由：进程外的网络黑洞（不是 402 那种立即返回的错误）
   会让**每一次**缓存查找都白等一整个超时 —— 而一次分析有 4~15 次查找。

   ⚠️ 这条纪律**必须两个客户端同时有**，实测（`_probe_cache_latency.py`，
   端点指向 RFC 5737 的 `192.0.2.1`，保证不可路由）：

       只有 rerank 有熔断：单次查找 **4.5 s**（= embed 1.5 + rerank 3.0），
                          第 4 次起降到 1.5 s —— **embedding 侧每次都重付**
       ⇒ 4~15 次查找 = 最坏 **18~69 s** 只花在缓存上；
         而加 rerank 之前的同一场景只有 embedding，最坏 6~23 s。
       ⇒ **只给一侧加熔断 = 把最坏情况放大 3 倍，且永不恢复。**

   所以熔断策略本身（阈值/冷却/窗口/恢复条件）**只写在
   `circuit_breaker.judge_breaker()` 一处**，两个客户端各自持有一个实例。

## ⚠️ 它不记 prompt/response，也不写缓存

它是**判据组件**，不是 LLM。`configs/models.yaml` 的 `models:` 段是**降级链**候选池，
把 reranker 写进去等于把它当 LLM 用 —— **不许**。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Final

import httpx

from src.infrastructure.llm.circuit_breaker import (
    JUDGE_COOLDOWN_SEC,
    JUDGE_FAILURES_TO_OPEN,
    judge_breaker,
)

logger = logging.getLogger(__name__)

#: 默认模型。选它的依据（`CHG-0199` 实测，同一批 40 对）：
#:   · AUC **0.775** —— 同端点下唯一显著高于抛硬币的判定器
#:     （`Qwen/Qwen3-Reranker-*` 实测 0.502 / 0.435，但**那次调用没按官方指令模板**，
#:      ⇒ 结果可疑，不能当作它们的结论）
#:   · **账单 ¥0.0000**（73.367 K tokens 未计费，`CHG-0202`）
#:   · ⚠️ **不要用 `Pro/BAAI/bge-reranker-v2-m3`**：同款收费档位
DEFAULT_MODEL: Final = "BAAI/bge-reranker-v2-m3"

#: 默认硬超时（秒）。K=12 实测 p50 **157 ms**、K=59 **576 ms** ⇒ 3 s 是有界的宽裕值。
DEFAULT_TIMEOUT_SEC: Final = 3.0

#: ★ 熔断参数**不再是本模块的私有常量** —— 判定客户端的策略统一在
#: `circuit_breaker.judge_breaker()`。这里保留同名别名只为兼容既有导入
#: （`tests/unit/test_rerank_client.py` 与 `__all__`），**值只有一个来源**。
CONSECUTIVE_FAILURES_TO_OPEN: Final = JUDGE_FAILURES_TO_OPEN

#: ★ 冷却时长（秒）。同名别名，来源同上。
COOLDOWN_SEC: Final = JUDGE_COOLDOWN_SEC

#: 计费档位前缀 —— **同名同效果的收费档位**（`CHG-0202` 账单实测）。
PAID_TIER_PREFIX: Final = "Pro/"


def is_paid_tier(model: str) -> bool:
    """模型名是不是**收费档位**（`Pro/` 前缀）。

    `Pro/BAAI/bge-m3` 与 `BAAI/bge-m3` 的 p50 与 AUC **逐位相同**
    （0.8604 / 0.8221 / 0.573）⇒ **同一个模型**；而账单显示
    **不带前缀 ¥0.0000、带前缀 ¥0.0842/M tokens**。
    ⇒ 判据只需一个前缀比较，收益是"不会有人为了更好去点那个更贵的同名模型"。
    """
    return (model or "").strip().startswith(PAID_TIER_PREFIX)


class RerankClient:
    """cross-encoder 判定器（鸭子类型：`configured` + `await rerank(...)` + `stats()`）。

    ⚠️ `LLMCache` 只依赖这三个成员 —— 测试里用替身时**必须实现全部三个**
    （`configured` 缺失会被当成"未启用"，`stats` 缺失会让 `/health` 报错）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = (api_key or "").strip()
        self._model = model
        self._timeout = max(0.1, float(timeout))
        #: 观测计数（供 `/health` 与排障）。
        self.calls = 0
        self.failures = 0
        self.total_ms = 0
        self.docs = 0
        #: 熔断状态机（**共用实现**，见模块 docstring 第 3 条）。
        #: ⚠️ 桶名与 embedding 客户端**必须不同**：合桶的话"rerank 挂了"
        #: 会顺手熔断 `_recall_and_judge` 用来兜底的 embedding 那一层。
        self._cb = judge_breaker("rerank")
        self.circuit_open_skips = 0

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def circuit_open(self) -> bool:
        """熔断是否**正在生效**（只影响出网，不影响 fail-open 语义）。

        ⚠️ 走 `snapshot()` 而不是 `allow_request()`：后者**有副作用**
        （会把 OPEN 推进到 HALF_OPEN、给 `total_rejected` 加一）
        ⇒ 一个"读状态"的属性不该改状态，否则 `/health` 每被读一次
        就消耗掉一次探测机会，熔断器的行为会依赖**被观测的次数**。
        """
        return self._cb.snapshot()["state"] == "OPEN"

    async def rerank(self, query: str, documents: list[str],
                     top_k: int | None = None) -> list[float] | None:
        """给 `documents` 逐篇打**与 query 的相关性**分。**绝不抛。**

        Returns: 与 `documents` **同序同长**的分数列表；不可用时 `None`。

        ⚠️ **同序同长**是刻意契约：调用方拿 `zip(docs, scores)` 对齐，
        若按服务端返回顺序（它按分数降序）直接返回，就会出现
        "第 0 篇文档拿到第 3 篇的分数"—— **一个不会报错的错位**。
        """
        if not self.configured or not query or not documents:
            return None
        if not self._cb.allow_request():
            self.circuit_open_skips += 1
            return None

        n = len(documents)
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/rerank",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "query": query,
                          "documents": documents,
                          "top_n": min(top_k or n, n),
                          "return_documents": False},
                )
            self.total_ms += int((time.perf_counter() - t0) * 1000)
            self.docs += n
            if resp.status_code != 200:
                self._fail(f"HTTP {resp.status_code}: {resp.text[:120]}")
                return None
            results = (resp.json() or {}).get("results") or []
        except Exception as exc:  # noqa: BLE001 端点失败只该让这一次判定不可用
            self.total_ms += int((time.perf_counter() - t0) * 1000)
            self._fail(f"{type(exc).__name__}: {exc}")
            return None

        # 按服务端给的 index 回填到**原顺序**（契约见 docstring）。
        out: list[float | None] = [None] * n
        for item in results:
            try:
                idx = int(item.get("index", -1))
                score = float(item.get("relevance_score"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n:
                out[idx] = score
        if any(v is None for v in out):
            # 服务端少给了条目 ⇒ 不当成"这些文档不相关"（那是**编造**分数）。
            self._fail(f"results 不完整：{len(results)}/{n}")
            return None
        self.calls += 1
        self._cb.record_success()
        return [float(v) for v in out]

    def _fail(self, why: str) -> None:
        self.failures += 1
        was_open = self.circuit_open
        self._cb.record_failure()
        if not was_open and self.circuit_open:
            logger.warning(
                "cross-encoder 判定连续失败 ⇒ **熔断 %.0fs**（期间不再出网，"
                "判定退回 embedding 阈值）。最后一次失败：%s",
                JUDGE_COOLDOWN_SEC, why)
        else:
            logger.debug("cross-encoder 判定失败（fail-open）：%s", why)

    def stats(self) -> dict[str, Any]:
        avg = (self.total_ms / self.calls) if self.calls else None
        return {
            "configured": self.configured,
            "model": self._model,
            # ⚠️ `calls` 只在**成功**时自增、`failures` 只在失败时自增
            #    ⇒ `calls=0 & failures>0` 读作"每次都失败"，
            #    与"从没被调用过"（两者都是 0）**必须分开读**（`CHG-0199` 的教训）。
            "calls": self.calls,
            "failures": self.failures,
            "total_ms": self.total_ms,
            "avg_ms": round(avg, 1) if avg is not None else None,
            "docs": self.docs,
            "circuit_open": self.circuit_open,
            "circuit_open_skips": self.circuit_open_skips,
            # ★ 三态快照：`circuit_open` 只是个布尔，排障时读不出
            #   "是还没恢复（OPEN）还是正在探测（HALF_OPEN）"。
            "circuit_state": self._cb.snapshot()["state"],
        }


__all__ = [
    "CONSECUTIVE_FAILURES_TO_OPEN",
    "COOLDOWN_SEC",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SEC",
    "PAID_TIER_PREFIX",
    "RerankClient",
    "is_paid_tier",
]
