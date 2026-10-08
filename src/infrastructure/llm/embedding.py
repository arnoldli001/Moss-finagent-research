"""缓存精排用的 Embedding 客户端（**云端**，OpenAI 兼容 `/embeddings`）。

## 为什么走云端，而不是本地 Ollama（`CHG-0178`）

**这是资源域划分问题，不是显存问题。**

| | 生成域 | 匹配层（本模块） |
|---|---|---|
| 频率 | 每请求 4~15 次 | **每次 LLM 调用之前都查**（更高频） |
| 稀缺性 | GPU 单槽，全机最稀缺 | 必须廉价、可降级 |
| 失败语义 | 慢但正确 | **必须立刻退化成 miss** |
| 延迟分布 | 双峰（实测 p50 4.5s / max 210s） | 必须有界 |

三条具体理由（都可复跑）：

1. **`OllamaProvider` 的闸只包住 `/api/chat`**。新开一个 `/api/embed` 出站点
   **绕过 `get_local_gate()`**，等于引入**第三类无闸的 Ollama 消费者**，
   直接和生成抢那个唯一槽位。
2. **单槽的代价已经量过了**：6 路并发打真实 Ollama 时
   **排队占墙钟 82.6%**（`sum(wait)=12108ms` / `sum(latency)=14650ms`）。
   再往里塞一路高频调用，是在最稀缺的资源上做最频繁的事。
3. **显存余量是靠"不互相换出"省出来的**（`configs/models.yaml` 顶部实测表）：
   4B(2983MB) + 1.5B(1112MB) = 4095/8188MB。任何第三个模型的进出都可能触发
   `evict → reload`，而那正是历史 `120294ms` 挂死的成因。

## 为什么不登记进 `configs/models.yaml` 的 `models:` 段

那一段是**降级链的候选池**。把 embedding 模型写进去，它就会被当成
"某一跳失败后的备源 LLM" —— 等于**把 embedding 模型当 LLM 用**。
所以本模块自带配置，**不进路由**。

## 失败语义：一律 fail-open

任何异常（超时 / 连接失败 / 非 200 / 解析失败 / 维度不符）都返回 `None`，
**绝不抛给调用方**。理由：精排是"锦上添花"的一层，它的失败只该退化成
"语义未命中"，然后照常走 LLM。抛出去会变成**又一条零痕迹失败路径**
（本项目刚修掉一条：`LocalQueueTimeout` 穿透网关且不留审计）。

## ★ 熔断（`CHG-0206` 补上 —— 原先**只有** `rerank.py` 有）

`rerank.py` 的模块 docstring 第 3 条把这个缺口写成了"本模块没有"，
当时的判断是"先给新加的判定器加上"——**那个判断是错的，而且是可量的错**：

    端点变成网络黑洞（不是 402 那种立即返回的错误）时，单次缓存查找要等满
    embed 1.5 s + rerank 3.0 s = **4.5 s**；冷却后 rerank 侧不再出网，
    但 **embedding 侧每次都重付 1.5 s** ⇒ 4~15 次查找 = 最坏 **18~69 s**
    只花在缓存上（加 rerank **之前**同一场景只有 embedding ⇒ 6~23 s）。

⇒ **只给一侧加熔断，等于把最坏情况放大 3 倍，且永不恢复。**
熔断策略本身写在 `circuit_breaker.judge_breaker()` **一处**，
两个客户端各持一个实例（桶名分开，见下）。

⚠️ 桶名**必须**与 rerank 不同：合桶的话"rerank 挂了"会顺手把 embedding
也熔断掉，而 embedding 正是 `_recall_and_judge` 用来兜底的那一层
（3-gram 兜底的质量实测差得多 —— 召回@12 只有 40%，见 `CHG-0199`）。

## ⚠️ 查询向量的记忆化**不在本模块**（`CHG-0178` 实测纠正）

第一版把"同文本只算一次"的记忆化写在这里。**那是错的位置**：

- 记忆化是**缓存层**的诉求（"同一个 anchor 只算一次"），不是传输层的；
- 放在客户端里 ⇒ **换任何别的客户端（含测试替身）就静默失去这个保证**，
  而我的单测正是用替身，于是那条断言**什么都没验**（假绿）。

现在它在 `LLMCache._embed_text()` 里（`_query_vecs`），
本模块只做一件事：**把文本变成向量**。
"""
from __future__ import annotations

import base64
import logging
import math
import time

import httpx

from src.infrastructure.llm.circuit_breaker import (
    JUDGE_COOLDOWN_SEC,
    judge_breaker,
)

logger = logging.getLogger(__name__)

#: 默认模型。选它的依据（`CHG-0178` 实测）：
#:   · 中文/多语言（本项目语料是 A 股中文），1024 维
#:   · 用**已配好的** SiliconFlow key 实测 `HTTP 200`
#:   · 同端点还有 `BAAI/bge-reranker-v2-m3`（cross-encoder），是将来升精排的现成路子
DEFAULT_MODEL = "BAAI/bge-m3"

#: 单次 embed 的硬超时（秒）。**必须有界**：它在每次缓存查找的关键路径上。
#: 1.5s 的依据：实测短文本单次调用在数百毫秒量级（3 次调用共 1,257 ms，
#: 约 **420 ms/次**），留 3~5 倍余量即可；再长就等于"用一次网络抖动换掉一次缓存查找"。
DEFAULT_TIMEOUT_SEC = 1.5


#: 向量的落盘打包精度。**float32，不是 float16。**
#:
#: ## 为什么放弃 float16（实测踩到，`CHG-0178`）
#:
#: 第一版用 `array.array("e")`（float16）。在**本机 Windows 的 CPython 上
#: 直接抛 `ValueError: bad typecode`** —— `'e'` 是**平台条件编译**的，
#: MSVC 构建里没有。而本项目有 Windows 开发机 + Linux VPS 两套环境，
#: 一个"看平台而定"的落盘格式意味着**同一份缓存换个平台就读不出来**，
#: 而且不会报错（`decode_vector` 会把坏数据当"没有向量"）。
#:
#: float32 是**无损**的（API 返回的就是 float32 精度的浮点），
#: 且 `array('f')` 在所有平台都可用。代价是体积翻倍：
#: 1024 维 = **4,096 字节 → base64 5,464 字符**，8,463 条约 **46 MB**
#: （float16 是 23 MB，但换来一个跨平台读不出来的风险，不划算）。
_VECTOR_TYPECODE = "f"


def encode_vector(vec: list[float]) -> str:
    """`list[float]` → base64(float32) 紧凑串。

    ## 为什么不用 JSON 数字数组

    1024 维 × 每个数 12~18 字符 ≈ **12~18 KB / 条**；缓存目录现有 8,463 条
    ⇒ 单这一项就是 **100+ MB**。float32 打包后是 **4,096 字节 → base64 5,464 字符**，
    约 46 MB（约 1/3）。

    ⚠️ **本函数不依赖 numpy**（用 stdlib `array`）：`numpy` 虽然是运行时依赖，
    但缓存模块的单测不该被一个纯打包函数拖进数值栈。
    """
    import array

    arr = array.array(_VECTOR_TYPECODE, [float(x) for x in vec])
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_vector(blob: str) -> list[float] | None:
    """`encode_vector` 的逆。任何损坏都返回 `None`（**不抛**）。"""
    import array

    try:
        raw = base64.b64decode(blob.encode("ascii"))
        arr = array.array(_VECTOR_TYPECODE)
        arr.frombytes(raw)
        return [float(x) for x in arr]
    except Exception:  # noqa: BLE001 损坏的向量只该让这一条不可用
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。长度不一致 ⇒ 0.0（**维度不符就是不可比，不当成相似**）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


class EmbeddingClient:
    """OpenAI 兼容 `/embeddings` 的**极简**客户端（只做一件事：把文本变向量）。

    `api_key` 由调用方注入（**不从环境变量硬读**）：本项目已经踩过
    "pydantic-settings 只把 `.env` 读进 Settings、不写回 `os.environ`"
    的坑（`MOSS_NETWORK_FALLBACK_ALLOWLIST`），所以 key 必须由 Settings 传进来。
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
        #: 观测计数（供 `/health` 与排障）：不记的话"精排到底有没有在跑"只能靠猜。
        #: ⚠️ **这里没有"命中数"** —— 记忆化在 `LLMCache` 层（见模块 docstring）。
        self.calls = 0
        self.failures = 0
        self.total_ms = 0
        #: 熔断状态机（**与 `RerankClient` 共用一份策略**，见模块 docstring）。
        #: 桶名 `"embed"` 与 `"rerank"` 分开 —— 合桶会连带熔断兜底层。
        self._cb = judge_breaker("embed")
        self.circuit_open_skips = 0

    @property
    def configured(self) -> bool:
        """能不能用。缺 base_url 或 key ⇒ False（调用方按"精排未启用"处置）。"""
        return bool(self._base_url and self._api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def circuit_open(self) -> bool:
        """熔断是否**正在生效**（只影响出网，不影响 fail-open 语义）。

        ⚠️ 走 `snapshot()` 而非 `allow_request()`：后者有副作用（推进状态机、
        给 `total_rejected` 加一）⇒ 读状态的属性不该改状态。
        """
        return self._cb.snapshot()["state"] == "OPEN"

    async def embed(self, text: str) -> list[float] | None:
        """文本 → 向量。**任何失败都返回 `None`，绝不抛。**

        `None` 的语义是"这一次没有向量可用" ⇒ 调用方按**未命中**处置。
        ⚠️ 不要把它读成"零向量"——返回空列表才是"量到 0"，而那种情况不存在。

        ⚠️ 本方法**不做记忆化**：那是 `LLMCache` 的职责（见模块 docstring）。
        直接调它 = 每次都真的出网。
        """
        if not self.configured or not (text or "").strip():
            return None
        if not self._cb.allow_request():
            self.circuit_open_skips += 1
            return None
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    json={"model": self._model, "input": [text]},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
            vec = [float(x) for x in data["data"][0]["embedding"]]
        except Exception as exc:  # noqa: BLE001 精排失败只该退化成 miss
            self._fail(f"{type(exc).__name__}: {str(exc)[:160]}")
            return None
        finally:
            self.total_ms += int((time.perf_counter() - started) * 1000)

        if not vec:
            self._fail("服务端返回空向量")
            return None
        self.calls += 1
        self._cb.record_success()
        return vec

    def _fail(self, why: str) -> None:
        """记一次失败并驱动熔断状态机。**绝不抛。**"""
        self.failures += 1
        was_open = self.circuit_open
        self._cb.record_failure()
        if not was_open and self.circuit_open:
            logger.warning(
                "embedding 精排连续失败 ⇒ **熔断 %.0fs**（期间不再出网，"
                "L2 召回退回 3-gram/向量阈值）。最后一次失败：%s",
                JUDGE_COOLDOWN_SEC, why)
        else:
            logger.info("embedding 精排不可用（按未命中处置）：%s", why)

    def stats(self) -> dict[str, object]:
        """观测面。⚠️ `configured=False` 时**不要**报 0 次失败 —— 那是"没量到"。"""
        return {
            "configured": self.configured,
            "model": self._model,
            "calls": self.calls,
            "failures": self.failures,
            "total_ms": self.total_ms,
            "avg_ms": (round(self.total_ms / self.calls, 1) if self.calls else None),
            "circuit_open": self.circuit_open,
            "circuit_open_skips": self.circuit_open_skips,
            "circuit_state": self._cb.snapshot()["state"],
        }


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SEC",
    "EmbeddingClient",
    "cosine",
    "decode_vector",
    "encode_vector",
]
