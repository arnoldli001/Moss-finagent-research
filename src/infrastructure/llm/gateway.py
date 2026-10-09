"""LLM统一网关：路由 → 语义缓存 → 降级链 → 哈希审计。

架构红线：所有LLM调用必须经过本网关（禁止Agent直连模型API）。
调用流程：缓存查询（exact/semantic）→ 主模型 → 失败降级备模型 →
全程审计落JSONL（prompt_hash/response_hash/token/延迟/降级链）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any, Final

import yaml

from src.core import deadline as deadline_mod
from src.core.cancel import CancellationToken
from src.core.config import Settings, get_settings
from src.core.exceptions import ConfigError, LLMGatewayError
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.cache import LLMCache, prompt_fingerprint
from src.infrastructure.llm.circuit_breaker import (
    caller_tenant_id,
    get_circuit_registry,
)
from src.infrastructure.llm.local_budget import (
    LOCAL_MAX_OUTPUT_TOKENS,
    LOCAL_TOKENS_PER_SEC,
)

# ★ 2026-10-05：本地排队超时必须在这里被**显式**接住并留痕（见下面 except 分支）。
# 模块级导入是安全的：`local_gate` 只依赖 stdlib，不反向 import 网关。
from src.infrastructure.llm.local_gate import LocalQueueTimeout
from src.infrastructure.llm.models import LLMResponse, ModelSpec, TaskTier
from src.infrastructure.llm.providers import BaseProvider, build_providers
from src.infrastructure.llm.rate_limit_guard import (
    RateLimitGuard,
    get_guard,
    looks_rate_limited,
)
from src.infrastructure.llm.vram import (
    DEFAULT_MODEL_VRAM_MB,
    LocalCapacity,
)

logger = logging.getLogger(__name__)

#: **计费**的提供商（会产生云端 token 账单的那一类）。
#:
#: 用户口径（2026-09-25）："本地模型推理不费钱，浪费就浪费……只要不用云端
#: tokens就行。" —— 所以"这次调用会不会花钱"必须能在**调用点**上关掉，
#: 见 `complete(local_only=True)`。
#:
#: 为什么用提供商名而不是"有没有 cost 字段"：`uses_paid`（token 预算检查）
#: 一直是这么判的，两处口径必须一致，否则"预算没挡住的调用反而是计费的"。
PAID_PROVIDERS: Final[frozenset[str]] = frozenset({"deepseek"})

#: ★ **"跑在本机"**（与"是否花钱"是**两个不同的概念**）。
#:
#: 2026-09-28 实测触发的缺陷：`pin_local` 与 `_filter_local_by_vram` 原先都用
#: `PAID_PROVIDERS` 当"是否本地"判据。而 `dashscope` / `siliconflow` / `zhipu`
#: 是**免费**的 → 不在 `PAID_PROVIDERS` 里 → **被误判成本地模型**：
#:
#:     light 层本地模型显存不足且无云端备源：空闲显存 338MB < 需要 2560MB，
#:     qwen-flash 拉不起来          ← qwen-flash 是**云端**模型
#:
#: 后果：**任何免费云端 provider 一接进来就被显存检查拦掉**，
#: 而 `local_only` 层会把它们当成"本地可用"从而裁掉真正的付费备源。
#:
#: 判据修正：**本地 = 真的跑在本机**（目前只有 ollama）；免费云端是云端。
#: 两个谓词各司其职 —— 成本用 `PAID_PROVIDERS`，位置用 `LOCAL_PROVIDERS`。
#:
#: ★★ `CHG-0192`：这两个集合现在是**回退值**，不再是唯一事实源 ——
#: 首选判据是提供商在 `providers.PROVIDER_DECLARATIONS` 里的**自声明**
#: （见下面的 `_provider_is_paid` / `_provider_is_local`）。
#: 保留它们有两个理由：① 测试与第三方直接实现 `BaseProvider`（没有自声明）
#: 时行为不变；② 它们是"内置提供商"这一层的可读清单。
#: ⚠️ **新增付费提供商时请改 `PROVIDER_DECLARATIONS`**，不要只改这里 ——
#: 只改这里等于新提供商的调用不进 token 预算（静默失效）。
LOCAL_PROVIDERS: Final[frozenset[str]] = frozenset({"ollama"})


def _provider_is_paid(name: str, providers: dict[str, Any] | None = None) -> bool:
    """该提供商是否**计费**（决定 token 预算累积、付费是否只做链首）。

    首选**自声明**（provider 实例的 `paid` 属性），未声明时回退到
    `PAID_PROVIDERS`。这样新增提供商只要在 `PROVIDER_DECLARATIONS` 登记一行，
    网关侧不需要再改任何集合 —— 修掉"新增付费商不进预算"那条静默失效。
    """
    if providers:
        obj = providers.get(name)
        declared = getattr(obj, "paid", None)
        if isinstance(declared, bool):
            return declared
    return name in PAID_PROVIDERS


def _provider_is_local(name: str, providers: dict[str, Any] | None = None) -> bool:
    """该提供商是否**跑在本机**（决定是否受显存检查、"本地可用"怎么算）。

    与 `_provider_is_paid` 同构：首选自声明，未声明回退 `LOCAL_PROVIDERS`。
    """
    if providers:
        obj = providers.get(name)
        declared = getattr(obj, "local", None)
        if isinstance(declared, bool):
            return declared
    return name in LOCAL_PROVIDERS

# 按层级的思维链强度（DeepSeek 官方 reasoning_effort：none/low/high/max）。
# 快任务收敛思维链省 token/降延迟；核心分析与决策保留 high 质量。
_TIER_REASONING_EFFORT: dict[str, str] = {
    "light": "low",
    "medium": "low",
    "reasoning": "high",
    "decision": "high",
}
_VALID_EFFORTS = ("none", "low", "high", "max")

# 按层级的输出预算（含思维链；只是上限，未用满不计费）。
# reasoning/decision 给 8192：DeepSeek 思维链计入输出，先吃光旧 4096 会致正文空响应。
_TIER_OUTPUT_BUDGET: dict[str, int] = {
    "light": 1024,
    "medium": 2048,
    "reasoning": 8192,
    "decision": 8192,
}

#: 单次尝试的墙钟预算（秒）。**只对有备用模型的链生效** ——
#: 链上最后一跳没有退路，给它设预算只会把"慢但正确"变成"必然失败"。
#: （例外：调用方**显式**传入 `attempt_budget_sec` 时，最后一跳也生效 ——
#:  显式传入 ≡ 声明自己有兜底。见 `complete()` 内 `explicit` 分支的实测记录。）
#:
#: 为什么需要（2026-09-26 实测）：`medium` 层主模型是本地 Ollama qwen3:8b，
#: 实测 45.7s；而云端备模型 deepseek-flash 只要 11.1s。
#: 降级链只在**失败**时前进，"成功但极慢"不算失败，于是备模型永远轮不到 ——
#: 信息层 A05/A06 各 38.1s / 52.9s，串行 91s。
#:
#: 取 20 秒：比云端 p90（18.3s）略宽，不会误伤正常的云端调用；
#: 又远小于本地模型的 45s，能及时切到备模型。设为 0 关闭该机制。
_ATTEMPT_BUDGET: dict[str, float] = {
    "light": 20.0,
    "medium": 20.0,
    "planning": 20.0,
    "reasoning": 25.0,   # 2026-09-28：45→25。备源已从"本地 12~20s"换成
    #                      "免费云端 0.4~3s"，45s 变成"等一个没必要的长超时"
    "decision": 30.0,    # 同理 60→30。decision 实测 5.2~6.9s
}

#: ★ 2026-10-05：**单跳预算与"允许输出"必须自洽**，否则"允许"是假的。
#:
#: 实测依据（`data/{pilot,dev}/audit/llm_audit.jsonl`，deepseek-flash，
#: 2026-10-05 六次采样 `tokens_out/latency_ms`）：
#:
#:     1948/10.0s · 2404/11.5s · 2532/11.6s · 2540/12.5s · 3764/16.9s · 5214/24.3s
#:     ⇒ **195~223 tok/s**，取 210 tok/s；TTFT 另留 2s。
#:
#: 而 `_TIER_OUTPUT_BUDGET["reasoning"] = 8192` 在 210 tok/s 下需要 **39s**，
#: 单跳预算却只有 25s ⇒ **任何真用满输出预算的调用必然被砍**：
#: 非流式 ⇒ 0 token 可救 ⇒ 25s 白花 + 再花 4s 降级重跑。
#: 实测现场（A09_meso，2026-10-05 12:21）：
#:
#:     12:21:23  deepseek-flash  attempt_budget_exceeded(25s)  out=0   ← 25.0s 丢弃
#:     12:21:27  qwen-flash      out=455  lat=4.1s  fallback      ← 降级，前端显示"降级=是"
#:
#: 同模型同层的历史分布（n=161，`p50=12.5s / p90=18.7s / p99=34.7s`）说明
#: 被砍的不是"异常慢"，而是**复杂度真的更高的那一档问题**（本次是五问合一的宏观题）
#: —— 正是最需要主源答案的时候。
_OUTPUT_TOKENS_PER_SEC: Final[float] = 210.0
_TTFT_ALLOWANCE_SEC: Final[float] = 2.0


def _read_budget_cap() -> float:
    """`MOSS_ATTEMPT_BUDGET_CAP_SEC` → 秒；空/非法 → `0.0`（= 不设上限）。

    非法值**出声**而不是静默当成 0：这个开关是"/紧急"口径，
    拼错了却以为生效，表现为"仍然等了 41 秒"，那比不设更难查。
    """
    raw = (os.environ.get("MOSS_ATTEMPT_BUDGET_CAP_SEC") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "MOSS_ATTEMPT_BUDGET_CAP_SEC 不是数字（%r）⇒ 按**不设上限**处理", raw)
        return 0.0


#: 单跳预算的**全局上限**（秒）。`0` = 不设上限（默认，行为与从前逐字一致）。
#:
#: 用途：试运行档 / 紧急口径下"**宁可降级也不等**"—— 设 20 后，
#: 连上面派生出来的付费链首预算（reasoning ≈41s）也会被压到 20s，
#: 不会为了等主源把拖过 30 秒。**只压上限、不抬下限**：
#: 调大它不会让任何一跳等更久。
_ATTEMPT_BUDGET_CAP_SEC: Final[float] = _read_budget_cap()

#: ★ 被单跳预算砍掉的那一跳，**要不要让它跑完并把结果写进缓存**（默认开）。
#:
#: ## 依据（实测，2026-10-05 用户报障「30 秒内出结果」）
#:
#: 被砍的那一跳是**非流式**的：请求已经发出去、钱已经计了，但我们
#: **0 token 可救**（A09 白等 25s、A17 的 deepseek 被砍 7s 都是同一形态）。
#: 既然代价已经付过，取消它就等于把钱扔了；让它跑完写进缓存，则
#: **同一个 prompt 下次直接命中主源（更强模型）的完整答案**——
#: 而当次仍然是"到点就降级"，/交互的时延不受影响。
#:
#: `MOSS_KEEP_LATE_RESULT=0` 可退回旧行为（取消、丢弃）。
def _read_keep_late() -> bool:
    raw = (os.environ.get("MOSS_KEEP_LATE_RESULT", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


_KEEP_LATE_RESULT: Final[bool] = _read_keep_late()

#: 迟到的后台结果最多再等多久（秒）：到点取消，避免留下无界后台任务
#: （本地模型"挂死"实测能到 120s，所以这里跟 HTTP 超时同量级即可）。
_LATE_RESULT_GRACE_SEC: Final[float] = 120.0

#: 在飞的迟到任务（持有强引用防 GC；同时也是"现在有几个迟到任务"的观测点）。
_LATE_TASKS: set[asyncio.Task] = set()


def _paid_primary_budget(tier_budget: float, out_budget: int, *,
                         paid_primary: bool) -> float:
    """付费**主源**那一跳的墙钟预算 = max(层级预算, 把允许输出跑完所需时间)。

    为什么只给这一跳放宽（而不是所有跳、也不是整体抬预算）：

    * **付费主源的下一跳是更弱的模型**（`configs/models.yaml` 的链按质量降序排），
      砍掉它 = 拿质量换时间，而且那 25s 已经付过钱（非流式 → 0 token 可救）；
    * **本地主源必须保持紧预算** —— 它的下一跳是更强的云端，砍掉它是**赚**的
      （这正是 `reasoning` 档 45→25 那次裁定的原意）。把派生规则无差别套上去，
      会把"本地挂死 120s"的老毛病放回来。

    所以判据是"**这一跳砍掉之后，下一跳是更强还是更弱**"，用
    `index == 0 and provider in PAID_PROVIDERS` 表达：付费只做链首，
    链首之后都是降级位。
    """
    if not paid_primary or out_budget <= 0 or tier_budget <= 0:
        return tier_budget
    need = out_budget / _OUTPUT_TOKENS_PER_SEC + _TTFT_ALLOWANCE_SEC
    return max(tier_budget, need)


def _load_model_config(path: str) -> tuple[
        dict[str, ModelSpec], dict[str, list[str]], set[str]]:
    """解析configs/models.yaml → (模型规格表, 层级降级链表, 钉死本地的层级)。"""
    try:
        with open(path, encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError(f"模型配置读取失败: {path}: {exc}") from exc

    specs: dict[str, ModelSpec] = {}
    for name, item in (raw.get("models") or {}).items():
        specs[name] = ModelSpec(name=name, **{
            k: item[k] for k in ("provider", "model_name", "base_url")
            if k in item
        } | {
            "max_tokens": item.get("max_tokens", 2048),
            "temperature": item.get("temperature", 0.1),
            # 本地模型跑起来需要多少显存：网关据此判断"拉不拉得起来"
            "vram_mb": item.get("vram_mb", 0),
        })

    routing: dict[str, list[str]] = {}
    tiers_local_only: set[str] = set()
    for tier, item in (raw.get("routing") or {}).items():
        chain = [item["primary"]]
        # 备源：`fallback`（单个，向后兼容）或 `fallbacks`（有序多跳）。
        #
        # 为什么需要多跳（2026-09-28 用户裁定）：
        #   「跨厂商不应该是两个云端模型吗 —— deepseek-flash、glm，
        #     本地 ollama 最后备用？」
        # 两跳模式表达不了「云端1 → 云端2 → 本地」这个次序：写
        # `fallback: local_medium` 会把**会挂死的本地**放在第一备源位，
        # 云端一抖动就掉到最不可靠的那一跳。
        fb = item.get("fallbacks")
        if isinstance(fb, (list, tuple)):
            chain.extend(str(x) for x in fb if x)
        elif item.get("fallback"):
            chain.append(item["fallback"])
        routing[tier] = chain
        # ★ 层级级 `local_only`（结构修补，2026-09-26）：
        #   `light` / `medium` 两层的 primary 是本地模型、**fallback 是付费的
        #   deepseek-flash** —— 本地一抖动就"悄悄花钱"，实测踩过
        #   （事件告警阶段一那 0.0196 元就是这么来的；探针脚本更隐蔽）。
        #   在配置里给这两层钉上 `local_only: true`，比在每个调用点记得传
        #   `local_only=True` 可靠：**新调用方默认就是安全的**。
        if item.get("local_only"):
            tiers_local_only.add(tier)

    if not specs or not routing:
        raise ConfigError(f"模型配置不完整: {path}")
    return specs, routing, tiers_local_only


class LLMGateway:
    """供所有Agent注入使用的统一LLM入口。
    Token预算保护：按trace_id累计DeepSeek调用token，超限拒绝后续调用。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        providers: dict[str, BaseProvider] | None = None,
        cache: LLMCache | None = None,
        audit: LLMAuditLog | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        (self._specs, self._routing,
         self._tiers_local_only) = _load_model_config(
             self._settings.model_config_path)
        self._providers = providers if providers is not None else build_providers()
        self._cache = cache if cache is not None else (
            LLMCache(
                cache_dir=self._settings.llm_cache_dir,
                ttl_hours=self._settings.llm_cache_ttl_hours,
                semantic_threshold=self._settings.llm_semantic_threshold,
                # ★ 三级缓存的 L3 精排器（`CHG-0178`）。
                #   默认关闭（`llm_embed_rerank_enabled=False`）：打开它等于
                #   给**每一次缓存查找**加一次网络调用，那是"更容易出错"的一侧
                #   —— 默认值即护栏，由使用方显式打开。
                embed_client=self._build_embed_client(),
                embed_threshold=self._settings.llm_embed_threshold,
                recall_k=self._settings.llm_embed_recall_k,
                # ★★ **判定器换层**（`CHG-0203`）：召回仍用 bi-encoder，
                #    但"是不是同一件事"的**判定权交给 cross-encoder**
                #    （实测 AUC 0.573 → 0.775，且每个操作点都严格优于现行）。
                rerank_client=self._build_rerank_client(),
                rerank_threshold=self._settings.llm_rerank_threshold,
                rerank_top_k=self._settings.llm_rerank_top_k,
            )
            if self._settings.llm_cache_enabled
            else None
        )
        self._audit = audit or LLMAuditLog(self._settings.llm_audit_dir)
        # Token预算：trace_id → 累计token数
        self._token_usage: dict[str, int] = {}
        self._token_budget = self._settings.llm_token_budget_per_task
        # 本地显存能力（带 TTL 缓存）：「钉死本地」是否可行由它回答。
        # 构造时**不探测**（探测要起子进程），第一次用到才查。
        local_base = next(
            (s.base_url for s in self._specs.values()
             if _provider_is_local(s.provider, self._providers) and s.base_url), "")
        self._local_capacity = LocalCapacity(base_url=local_base)
        # 免费档限流熔断（落盘）。默认路径在 data/run/ 下 ——
        # 与 backend.pid / incidents 同目录，运维一眼能找到；
        # **若实例设了 LLM_AUDIT_DIR（dev/pilot 隔离）则跟着它走**，
        # 避免"dev 调试出的 429 把线上的同一跳锁掉"（见 rate_limit_guard）。
        #
        # ⚠️ 用**进程级单例**而不是每次 `RateLimitGuard()`：
        # `/health` 里 `llm.rate_limit_guard` 那一段要展示的是**这一个**
        # 生效中实例的计数。各建一个对象的话两边各读一份文件，
        # "界面上显示没锁、实际网关锁着"就会长期共存（而且都不报错）。
        self._rate_guard = get_guard()

    def _paid_provider_names(self) -> list[str]:
        """当前**实际注册且计费**的提供商名（用于报错文案，不是判据来源）。

        从活注册表 + 自声明现算，而不是打印模块级的 `PAID_PROVIDERS` 常量 ——
        否则新增一个付费提供商后，报错会告诉用户"计费提供商=[deepseek]"，
        而实际链上还有别人（**文案与事实不符**，正是本仓库明令避免的一类）。
        """
        return sorted(n for n in self._providers
                      if _provider_is_paid(n, self._providers))

    def _build_embed_client(self) -> Any:
        """建 L3 精排器（`None` = 未启用）。见 `embedding` 模块的资源纪律。

        ## 三个刻意的选择

        1. **走云端，不走本地 Ollama**：`providers.py` 的闸只包住 `/api/chat`，
           新开 `/api/embed` 出站点会绕过 `get_local_gate()`，成为**第三类无闸的
           Ollama 消费者** —— 去抢那个已实测"排队占墙钟 82.6%"的唯一槽位。
        2. **端点/key 从 `Settings` 读，不从 `os.environ` 读**：本项目踩过
           "pydantic-settings 只把 `.env` 读进 Settings、不写回 `os.environ`"
           的坑（`MOSS_NETWORK_FALLBACK_ALLOWLIST` 当时表现为"护栏看起来在工作、
           其实开关压根没接上"）。
        3. **缺 key / 缺 base_url 时返回 None 而不是建一个会失败的客户端**：
           `rerank_enabled` 会如实报 False，`/health` 一眼能看出"精排没接上"，
           而不是"接上了但每次都超时"。
        """
        from src.infrastructure.llm.embedding import EmbeddingClient
        from src.infrastructure.llm.rerank import is_paid_tier

        if not getattr(self._settings, "llm_embed_rerank_enabled", False):
            return None
        base = ((self._settings.llm_embed_base_url or "").strip()
                or (self._settings.siliconflow_base_url or "").strip())
        key = (self._settings.siliconflow_api_key or "").strip()
        if is_paid_tier(self._settings.llm_embed_model):
            # ⚠️ **不拦，只报警**：`Pro/` 是同名同效果的**收费**档位
            # （`CHG-0202` 账单实测：不带前缀 ¥0.0000、带前缀 ¥0.0842/M tokens，
            # 而两者的 p50 与 AUC 逐位相同）。拦掉会挡住"确实想买更高配额"的人，
            # 但**沉默地多花钱**更不可接受 ⇒ 出声。
            logger.warning(
                "LLM_EMBED_MODEL=%s **带 `Pro/` 前缀** —— 这是同名同效果的"
                "收费档位（实测 p50/AUC 逐位相同，账单 ¥0.0842/M vs 免费）"
                "⇒ 除非你确实需要它的配额，否则去掉 `Pro/`。",
                self._settings.llm_embed_model)
        client = EmbeddingClient(
            base_url=base,
            api_key=key,
            model=self._settings.llm_embed_model,
            timeout=self._settings.llm_embed_timeout_seconds,
        )
        if not client.configured:
            logger.warning(
                "LLM_EMBED_RERANK_ENABLED=1 但精排器配不齐（base_url=%r key=%s）"
                " ⇒ L3 精排**不会生效**，语义层退回旧的阈值规则。",
                bool(base), "已配" if key else "缺失")
            return None
        logger.info("LLM 缓存 L3 精排已启用：%s @ %s（阈值 %.2f，召回 K=%d）",
                    client.model, base, self._settings.llm_embed_threshold,
                    self._settings.llm_embed_recall_k)
        return client

    def _build_rerank_client(self) -> Any:
        """建 **cross-encoder 判定器**（`None` = 未启用）。`CHG-0203`。

        与 `_build_embed_client` 同一套纪律（云端、Settings 读、缺 key 返回 None），
        外加一条 `Pro/` 前缀告警 —— 它是**同名同效果的收费档位**。

        ⚠️ **它和 embed 客户端是两个东西，可以一个有一个没有**：
        没有 embed ⇒ 召回退回 3-gram（recall@12 40%），但**判定仍可由它完成**
        （它吃原始文本、不需要向量）—— 这正是它能"立刻改善存量 5,007 条"的原因。
        """
        from src.infrastructure.llm.rerank import RerankClient, is_paid_tier

        if not getattr(self._settings, "llm_rerank_enabled", False):
            return None
        base = ((self._settings.llm_embed_base_url or "").strip()
                or (self._settings.siliconflow_base_url or "").strip())
        key = (self._settings.siliconflow_api_key or "").strip()
        model = self._settings.llm_rerank_model
        if is_paid_tier(model):
            logger.warning(
                "LLM_RERANK_MODEL=%s **带 `Pro/` 前缀** —— 同名同效果的收费档位"
                "（`CHG-0202` 账单：¥0.0842/M vs 免费）⇒ 除非确实需要配额，去掉前缀。",
                model)
        client = RerankClient(
            base_url=base,
            api_key=key,
            model=model,
            timeout=self._settings.llm_rerank_timeout_seconds,
        )
        if not client.configured:
            logger.warning(
                "LLM_RERANK_ENABLED=1 但判定器配不齐（base_url=%r key=%s）"
                " ⇒ 判定退回 embedding 阈值（行为与换层前一致）。",
                bool(base), "已配" if key else "缺失")
            return None
        logger.info(
            "LLM 缓存判定器 = **cross-encoder**：%s @ %s（阈值 %.2f，候选 K=%d）",
            client.model, base, self._settings.llm_rerank_threshold,
            self._settings.llm_rerank_top_k)
        return client

    async def _filter_local_by_vram(
            self, local_chain: list[str]) -> tuple[list[str], list[str]]:
        """筛掉**显存上拉不起来**的本地模型。

        Returns: `(可用列表, 不可用原因列表)`。
        判据见 `vram.decide_local_usable` —— 已驻留 / 空闲够 / **判不了**
        三种情况都算可用（不花钱的一侧是安全侧）。

        ⚠️ 必须是 async 并用 `acheck`：探测要起 `nvidia-smi` 子进程，
        同步跑会**冻结整条事件循环**（所有并发请求一起卡）。
        """
        usable: list[str] = []
        rejected: list[str] = []
        for name in local_chain:
            spec = self._specs[name]
            need = spec.vram_mb or DEFAULT_MODEL_VRAM_MB
            # ⚠️ 查驻留必须用 **Ollama 的模型名**（`spec.model_name`），
            # 不是配置里的键（`local_light`）—— Ollama `/api/ps` 报的是前者。
            # 实测踩过：传键会让"已驻留"永远判不中 → **每次都白走云端**。
            ok, why = await self._local_capacity.acheck(spec.model_name, need)
            if ok:
                usable.append(name)
            else:
                rejected.append(why)
                logger.warning("本地模型 %s 显存不足，跳过：%s", name, why)
        return usable, rejected

    async def _await_with_budget(
        self, provider: BaseProvider, spec: ModelSpec, system: str, prompt: str,
        chat_kwargs: dict[str, Any], *, budget: float, agent_id: str, scope: str,
        use_cache: bool, cache_ttl_hours: float | None, task_tier: str,
        trace_id: str, anchor: str = "",
    ) -> LLMResponse:
        """在 `budget` 内等这一跳；超时**不取消调用**，让它后台跑完并写缓存。

        ## 与 `asyncio.wait_for` 的唯一差别（这就是本方法存在的全部理由）

        `wait_for` 超时会**取消**内层协程 ⇒ 那一跳的算力与**已经计费**的 token
        全部丢弃（实测：A09 白等 25.0s / A17 的 deepseek 被砍 7.2s，两次都是
        `tokens_out=0`）。这里改成"等 `budget`，没回来就**放手让它继续跑**"，
        由 `_adopt_late_result` 收尾：跑完了就写进 LLM 缓存 + 记账 + 留审计痕迹，
        于是**同一个 prompt 下一次直接命中主源的完整答案**。

        语义仍然与 `wait_for` 一致：超时就抛 `TimeoutError`，调用方的
        `except TimeoutError` 分支（降级到备模型）**一个字都不用改**。
        """
        task = asyncio.ensure_future(provider.chat(spec, system, prompt, **chat_kwargs))
        done, _pending = await asyncio.wait({task}, timeout=budget)
        if task in done:
            return task.result()          # 异常照旧抛出（与 wait_for 语义一致）
        if not _KEEP_LATE_RESULT:
            task.cancel()
            raise TimeoutError(
                f"{spec.provider}({spec.model_name}) 超出单跳延迟预算 {budget:.0f}s")
        self._adopt_late_result(
            task, spec=spec, system=system, prompt=prompt, agent_id=agent_id,
            scope=scope, use_cache=use_cache, cache_ttl_hours=cache_ttl_hours,
            task_tier=task_tier, trace_id=trace_id, budget=budget, anchor=anchor)
        raise TimeoutError(
            f"{spec.provider}({spec.model_name}) 超出单跳延迟预算 {budget:.0f}s")

    def _adopt_late_result(
        self, task: asyncio.Task, *, spec: ModelSpec, system: str, prompt: str,
        agent_id: str, scope: str, use_cache: bool, cache_ttl_hours: float | None,
        task_tier: str, trace_id: str, budget: float, anchor: str = "",
    ) -> None:
        """收尾一个"超预算但仍在跑"的调用：跑完 ⇒ 写缓存 + 记账 + 留痕。

        ⚠️ 记账口径**必须**走与正常路径同一套（`call_cost_cny` / `get_budget().record`
        / `LLMCache.put`）：两条各自记账的实现必然分叉，而分叉的后果是
        "钱花了但账本上没有"——本项目已登记过同类缺陷。
        """
        gateway = self

        async def _reap() -> None:
            try:
                resp = await asyncio.wait_for(task, timeout=_LATE_RESULT_GRACE_SEC)
            except TimeoutError:
                task.cancel()
                logger.info("迟到结果放弃：%s/%s 超过 %.0fs 仍未回（未写缓存）",
                            spec.provider, spec.model_name, _LATE_RESULT_GRACE_SEC)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 迟到失败只是"没赚到"，不影响任何调用方
                logger.info("迟到结果未取到：%s/%s %s: %s", spec.provider,
                            spec.model_name, type(exc).__name__, str(exc)[:120])
                return
            written = False
            try:
                from src.core.budget import call_cost_cny, get_budget

                resp.cost_yuan = call_cost_cny(
                    provider=spec.provider, model=spec.model_name,
                    tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                    provider_cache_hit=False)
                get_budget().record(
                    provider=spec.provider, model=spec.model_name,
                    tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                    provider_cache_hit=False,
                    source=f"{agent_id or task_tier}:late")
            except Exception:  # noqa: BLE001 记账失败不影响写缓存
                logger.debug("迟到结果记账失败（忽略）", exc_info=True)
            gateway._rate_guard.record_success(spec.name)
            if (gateway._cache and use_cache
                    and (resp.content or "").strip()):
                try:
                    # 与正常路径同一套（含 anchor/向量）：两条写缓存的实现
                    # 若分叉，收编进来的迟到结果会**永远无法被精排命中**。
                    _emb = await gateway._cache.anchor_embedding(
                        system, prompt, anchor)
                    gateway._cache.put(
                        system, prompt, resp, agent_id, scope=scope,
                        ttl_seconds=(None if cache_ttl_hours is None
                                     else cache_ttl_hours * 3600.0),
                        anchor=anchor, embedding=_emb)
                    written = True
                except Exception:  # noqa: BLE001 写缓存失败只是"没赚到"
                    logger.debug("迟到结果写缓存失败（忽略）", exc_info=True)
            gateway._audit.record(
                trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                response=resp, cached=False,
                error=(f"late_result{' _cached' if written else ''}"
                       f"(超预算 {budget:.0f}s 但跑完，out={resp.tokens_out} tok)："
                       f"{spec.provider}"))
            logger.info(
                "迟到结果已收编：%s/%s 超预算 %.0fs 后跑完（%s），%s",
                spec.provider, spec.model_name, budget,
                f"{resp.tokens_in}+{resp.tokens_out} tok",
                "已写缓存，下次同问直接命中" if written else "未能写缓存（无缓存或内容为空）")

        reaper = asyncio.ensure_future(_reap())
        _LATE_TASKS.add(reaper)
        reaper.add_done_callback(_LATE_TASKS.discard)

    @property
    def audit_log(self) -> LLMAuditLog:
        return self._audit

    @property
    def rate_guard(self) -> RateLimitGuard:
        """生效中的限流熔断器（`/health` 与验收脚本读它，别读私有字段）。"""
        return self._rate_guard

    def _truncate(self, prompt: str) -> str:
        """超长 prompt 截断：**保留头尾、压缩中段**。

        为什么不能只截尾部：分析类 prompt 的结构是
        `[数据正文]` + `## 任务要求`（JSON schema）+ `## 专业技能指引`。
        指令段在**尾部**，一刀切尾部会把输出 schema 整段丢掉 ——
        模型拿不到 schema 就会输出不合规，触发 repair 重试
        （且重试走 `use_cache=False`、prompt 更长），单次成本反而翻倍。

        所以按 7:3 保留头尾：头部是数据（模型的主要输入），
        尾部是指令与 schema（必须完整）。
        """
        cap = int(self._settings.llm_input_char_hard_cap or 0)
        if cap <= 0 or len(prompt) <= cap:
            return prompt
        head = int(cap * 0.7)
        tail = cap - head
        dropped = len(prompt) - cap
        return (prompt[:head]
                + f"\n…（中段已省略 {dropped} 字符）\n"
                + prompt[-tail:])

    def _spec(self, model_name: str) -> ModelSpec:
        try:
            return self._specs[model_name]
        except KeyError as exc:
            raise ConfigError(f"模型未在configs/models.yaml定义: {model_name}") from exc

    def _resolve_effort(
        self, task_tier: TaskTier, call_override: str | None = None
    ) -> str:
        """解析本次思维链强度，优先级：调用覆盖 > 全局配置 > 按层级映射。"""
        override = (call_override or "").strip().lower()
        if override not in _VALID_EFFORTS:
            override = (self._settings.llm_reasoning_effort or "").strip().lower()
        if override in _VALID_EFFORTS:
            return override
        return _TIER_REASONING_EFFORT.get(task_tier, "")

    async def complete(
        self,
        task_tier: TaskTier,
        system: str,
        prompt: str,
        *,
        agent_id: str = "",
        trace_id: str = "",
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
        use_cache: bool = True,
        cache_ttl_hours: float | None = None,
        cancel_token: CancellationToken | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        local_only: bool | None = None,
        attempt_budget_sec: float | None = None,
        anchor: str = "",
        scope_extra: str = "",
        semantic_cache: bool = True,
    ) -> LLMResponse:
        """执行一次补全：缓存→主模型→备模型，全程审计。

        `anchor` = **缓存语义比较用的变量文本**（`CHG-0178`）。不传则回落到
        `system + prompt`，**行为与改动前逐字一致**。

        ## 为什么必须有它（而不是继续比整 prompt）

        实测抽取 prompt 的固定骨架占 **79.6%**（跨桶 34.7%~419%），
        拿整 prompt 算相似度时两条**完全不同**的资讯能到 **0.93~0.97**
        ⇒ 必然复用第一条答案（本项目登记过的事故 `CHG-0069` / `CHG-0094`）。
        只比变量文本：不同事件 3-gram **0.0000** / embedding **0.40~0.50**，
        同事件改写稿 **0.9639**。

        投研分析里最自然的 anchor 就是**用户问句**。

        `attempt_budget_sec` 是**单次尝试的墙钟预算**（None = 用 `_ATTEMPT_BUDGET`）。
        为什么需要它（2026-09-26 实测）：`medium` 层的主模型是**本地 Ollama
        qwen3:8b**，而它比云端的备模型慢得多 —— 同一个 prompt：

            本地 qwen3:8b        45.7s   （主模型，"成功"了）
            云端 deepseek-flash  11.1s   （备模型，永远轮不到）

        信息层 A05/A06 都挂 `medium`，审计实测各 38.1s / 52.9s，
        串行合计 91s —— **是整条投研链路最贵的一段**。

        根因不是"本地模型不可用"（它可用），而是**没有延迟预算**：
        降级链只在"失败"时前进，而"成功但极慢"不会被判定为失败，
        于是备模型永远不被尝试。加预算后，超时的本地尝试被中断、
        自动落到云端备模型 —— "可用的慢"不再拖住链路。

        cancel_token非None且被取消时，直接跳过API调用抛TaskCancelledError。
        输入prompt超过llm_input_char_hard_cap时尾部截断，防数据淹没致token超支。

        `json_schema` 是**输出结构的语法级约束**（None = 不约束）。
        为什么需要它：`json_mode=True` 只保证"是 JSON"，不保证"是你要的 JSON"。
        实测本地 `qwen2.5:1.5b` 在"从笔记里提取热门个股"任务上，json_mode 下仍
        返回 54 字符的坏 JSON（`{"1. 【CC电新】液冷金帝...": -1.1e6}`），解析失败 →
        下游拿到 0 条结果。传 schema 后 Ollama 走受约束解码（`format=<schema>`），
        结构由采样器保证，小模型也能用。DeepSeek 只支持 json_object，
        传 schema 时退化为 json_object（schema 仍需在 prompt 里描述）。

        `max_tokens` 是**按调用**的输出上限覆盖（None = 用模型配置里的值）。
        为什么需要它：DeepSeek 的**推理 token 计入输出上限**，所以同一个
        `max_tokens` 在"推理型"任务上会先被思维链吃光 —— 实测打分任务
        （`src/mainline/relevance.py`）有约 20% 的调用 `tokens_out` 正好撞到
        4096 上限、**正文一个字都没输出**（返回空响应），且重试同样撞墙。
        这种调用需要更大的输出预算，但不该把全局 `llm_max_tokens_hard_cap`
        一起抬高（那是成本护栏，影响所有调用）。

        `cache_ttl_hours` 是**按调用**的缓存存活时间覆盖（None = 用全局配置）。
        用途：这份缓存目录里同时装着分钟级（新闻情绪）和季度级（主营题材相关性）
        的东西，全局 TTL 只能迁就一边。见 `LLMCache.put` 的说明。

        ⚠️ **`use_cache=False` 是"强制刷新"的唯一正确写法**：调用方要重算时
        必须传它，只清自己的进程内缓存是不够的 —— 那样只会绕开 L1，
        磁盘上的旧答案照样会被命中（前端表现为"点了强制刷新，数据没变"）。

        `local_only=True` 把降级链**裁到只剩本地模型**（`PAID_PROVIDERS`
        之外的），并保持原有的主备顺序。用途是"这条链路绝不能花云端 token"
        的场景 —— 情报抽取每 2 小时跑几十条，而 `light` 层的 fallback 配的是
        `deepseek-flash`：本地 Ollama 一挂，它就会**真的去调云端**并计费。
        裁完为空（这一层根本没有本地模型）时**明确抛错**，而不是悄悄降级到
        云端 —— 调用方本来就按"模型不可用就走规则层"设计，报错比花钱好。

        `local_only` 的三种取值（2026-09-26 改）

        | 传值 | 含义 |
        |---|---|
        | `None`（默认） | **跟随层级配置**：该层写了 `local_only: true` 就钉死本地 |
        | `True` | 强制只用本地（与以前一致） |
        | `False` | 强制允许付费云端（显式退出，少数场景才用） |

        为什么要有配置级的默认值：靠"每个调用点记得传 `local_only=True`"
        是靠不住的 —— `light` / `medium` 两层的 fallback 都是**付费**的
        deepseek-flash，任何一处忘传，本地一抖动就悄悄花钱（实测：
        事件告警阶段一 0.0196 元、`_dbg_hot.py` 这类探针更隐蔽）。
        现在这两层在配置里钉死本地，**新调用方默认就是安全的**。

        ## ★ 2026-09-28 第十二轮：`MOSS_MEDIUM_FORCE_LOCAL` 环境回退

        `medium` 层（A05/A06）的 primary 已从本地改为云端 —— 实测本地
        36-40 tok/s 导致 A05+A06 串行 **91s**（Ollama 单槽），换云端
        172 tok/s 后约 16s，代价 ¥0.017/次。详见 `configs/models.yaml`。

        要**一键退回**旧行为（只用本地、不花钱）：
            MOSS_MEDIUM_FORCE_LOCAL=1
        该开关把 `medium` 层的降级链裁回本地，等价于旧配置的
        `local_only: true`。做成环境变量而不是改 yaml，是为了让
        "延迟 vs 成本"这个取舍可以在**不重新部署**的情况下切换。
        """
        if task_tier not in self._routing:
            raise ConfigError(f"未知任务层级: {task_tier}")
        # 取消检查：在调用provider前拦截，避免浪费token
        if cancel_token is not None:
            cancel_token.check()
        prompt = self._truncate(prompt)
        chain = self._routing[task_tier]
        # `None` = 跟随层级配置（见 docstring 的三值表）。
        pin_local = (task_tier in self._tiers_local_only
                     if local_only is None else bool(local_only))
        # ★ 2026-09-28 第十二轮：`MOSS_MEDIUM_FORCE_LOCAL=1` 一键退回本地。
        # 让"延迟 vs 成本"的取舍可以在不重新部署的情况下切换（见 docstring）。
        # 只对 medium 生效 —— light 层本来就是本地优先的轻量任务，无需该开关。
        if task_tier == "medium" and local_only is None:
            _force_local = (
                os.environ.get("MOSS_MEDIUM_FORCE_LOCAL", "0") or "0"
            ).strip() in ("1", "true", "True", "yes")
            if _force_local:
                pin_local = True
        if pin_local:
            # 新列表，**不改配置里那份**（`self._routing` 是共享的，
            # 就地改会把"只这一次不花云端"变成"这一层以后都不花云端"，
            # 而调用方并没有这么要求）
            local_chain = [m for m in chain
                           if _provider_is_local(self._specs[m].provider,
                                                  self._providers)]
            if not local_chain:
                raise LLMGatewayError(
                    f"{task_tier} 层没有本地模型可选（local_only）："
                    f"链={chain}，计费提供商={sorted(self._paid_provider_names())}")
            # ★ 显存可行性检查（2026-09-28 结构修补）：
            # 「钉死本地」这条护栏假设**本地模型拉得起来**。本机只有 8GB 显存，
            # 大模型驻留时小模型换不进来 → 请求挂死（实测 120294ms / in=0 out=0）。
            # 所以这里先问一句"真的拉得起来吗"：拉不起来就**放弃钉死**，
            # 让降级链保留云端备源 —— 拿一个真结果回来，而不是干等到超时。
            usable, verdicts = await self._filter_local_by_vram(local_chain)
            if usable:
                chain = usable
            else:
                # 本地全不可用 → **把本地从链里摘掉**，直接走云端备源。
                #
                # ⚠️ 这里**不能**只是"不钉死"：不钉死的话链首仍是那个跑不起来的
                # 本地模型，还是会先撞它、再等预算超时 —— 那是"晚 20s 才用云端"，
                # 不是"改用云端"。用户要的是后者。
                cloud_chain = [m for m in chain
                               if not _provider_is_local(
                                   self._specs[m].provider, self._providers)]
                if not cloud_chain:
                    raise LLMGatewayError(
                        f"{task_tier} 层本地模型显存不足且无云端备源："
                        f"{'；'.join(verdicts)}")
                chain = cloud_chain
                # 代价是花钱，所以必须出声 —— 静默改道等于偷偷花钱。
                reason = "；".join(verdicts)
                logger.warning(
                    "%s 层的本地模型拉不起来，本次改走云端备源 %s：%s",
                    task_tier, cloud_chain, reason)
                # 走既有的 error 通道登记，不新增 schema、不污染成本核算。
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=LLMResponse(content="", model_used=cloud_chain[0],
                                         provider="router"),
                    cached=False,
                    error=f"vram_reroute: {reason}"[:400])
        effort = self._resolve_effort(task_tier, reasoning_effort)
        # 缓存作用域：同一 system/prompt 在不同层级/输出格式/推理强度下不可互相复用。
        # light 层本地 1.5B 的回答不能当 decision 层结论；effort 入 scope 防改配置后串缓存。
        # schema 也入 scope：同一 prompt 换 schema（字段增删/改名）后旧答案结构不兼容，
        # 复用会直接解析失败，看起来像"模型突然不会输出了"。
        schema_fp = ""
        if json_schema is not None:
            raw_schema = json.dumps(
                json_schema, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            schema_fp = hashlib.sha256(raw_schema.encode("utf-8")).hexdigest()[:12]
        scope = f"{task_tier}|json={int(json_mode)}|eff={effort}|sch={schema_fp}"
        if scope_extra:
            # ★★ 数据指纹进 **scope**（`CHG-0180`）—— "换数据必须重算"的**结构保证**。
            #
            # ## 为什么不能靠 anchor 区分数据（实测反证，我第一版就错在这）
            #
            # 第一版把数据段也放进了 `anchor`，并在注释里断言"换了数据就不会命中"。
            # **端到端实测直接推翻了它**：同一问句、只把 CPI 从 0.5 改成 9.9，
            # anchor 文本确实变了，但 **embedding 余弦仍然 ≥0.80** ——
            # 一个数字的变化几乎不移动 1024 维语义向量 ⇒ **L3 照命中**，
            # 返回的是**上一批数据算出来的结论**（而新鲜度标注还是今天的）。
            #
            # ⇒ 教训：**"内容变了"不等于"语义向量变了"**。
            #   凡是"必须完全一致才能复用"的东西（数据、期间、参数），
            #   都要走**精确匹配**的 scope，不能交给模糊层去分辨。
            #
            # scope 同时是 L1 的哈希输入与 L2/L3 的分桶键 ⇒
            # 数据一变**整条复用路径一起失效**（连候选都不会有），
            # 而不是"靠相似度碰巧没命中"。
            scope = f"{scope}|d={scope_extra}"

        if self._cache and use_cache:
            # 用 `aget` 而非 `get`：三级匹配要建索引（同步 IO，实测 9ms 级冻结
            # 全部并发请求）并且 L3 精排要做网络调用 —— 只有 async 入口能做。
            # 见 cache.py 模块注释的"同步 get() 与异步 aget() 的差别"。
            hit = await self._cache.aget(system, prompt, agent_id, scope=scope,
                                         anchor=anchor,
                                         semantic_cache=semantic_cache)
            # ⚠️ **空响应不算缓存命中**。实测踩过的坑：某次调用因为 max_tokens
            # 被思维链吃光而返回空 content，那条空记录被写进缓存后，**后续每一次
            # 重试都命中这条空缓存并立刻返回** —— 表现为"确定性失败、重试无用"，
            # 看起来完全不像缓存问题（耗时还因为命中缓存而变快）。
            # 空内容对调用方没有任何价值，一律当作未命中重新请求。
            if hit is not None and (hit.content or "").strip():
                hit.trace_id = trace_id
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=hit, cached=True,
                )
                return hit

        # Token预算检查：单任务DeepSeek调用累计超限则拒绝
        uses_paid = any(_provider_is_paid(self._specs[m].provider, self._providers)
                        for m in chain)
        if trace_id and uses_paid:
            used = self._token_usage.get(trace_id, 0)
            if used >= self._token_budget:
                raise LLMGatewayError(
                    f"任务{trace_id}的token预算已耗尽"
                    f"（已用{used}/{self._token_budget}），"
                    "请缩小分析范围或调整配置llm_token_budget_per_task"
                )

        tried: list[str] = []
        last_error: Exception | None = None
        for index, model_name in enumerate(chain):
            # 降级链中再次检查取消（fallback时也拦截）
            if cancel_token is not None:
                cancel_token.check()
            spec = self._spec(model_name)
            tried.append(model_name)
            started = time.perf_counter()
            try:
                provider = self._providers[spec.provider]
            except KeyError:
                last_error = LLMGatewayError(f"提供商未注册: {spec.provider}")
                continue
            # ★ 免费档限流熔断（2026-09-28 第二十二轮）：处于锁定期的模型
            # **直接从链上跳过**，不再白撞一次 429。
            # 为什么必须有：`light`（占 82% 调用量）首位是免费档，
            # 它限流时若每次都先试一次，会(a)拖慢每一次调用、(b)白白多消耗
            # 下一跳的配额。而这个过程**没有任何可见面**。
            # 状态落盘（`rate_limit_guard`），重启不失效。
            if self._rate_guard.is_locked(model_name):
                snap = self._rate_guard.snapshot()["models"].get(model_name, {})
                last_error = LLMGatewayError(
                    f"{model_name} 限流锁定中（剩余 {snap.get('remaining_s')}s，"
                    f"累计 429 {snap.get('total_429', 0)} 次），跳过该跳")
                logger.info("跳过限流锁定的 %s（剩余 %ss）",
                            model_name, snap.get("remaining_s"))
                continue
            # 熔断器准入检查：熔断中直接跳过该provider，降级到备模型。
            # ★ 桶按 `provider × 租户` 隔离（2026-09-30）：修复前这里是
            #   `get_or_create(spec.provider)`，**一个租户**的突发流量把
            #   所有租户一起打到 circuit_open（见 `routes/research.py`
            #   @107-109 的实测记录）。`for_call` 是唯一建 key 的地方；
            #   取不到租户身份 ⇒ 退回 provider 级全局桶（**仍然熔断**，
            #   取舍写在 `circuit_breaker` 模块 docstring 里）。
            cb = get_circuit_registry().for_call(
                spec.provider, caller_tenant_id())
            if not cb.allow_request():
                last_error = LLMGatewayError(
                    f"提供商{spec.provider}熔断中({cb.snapshot()['state']})，"
                    "自动降级到备模型"
                )
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=LLMResponse(
                        content="", model_used=spec.model_name, provider=spec.provider,
                        prompt_hash=prompt_fingerprint(system, prompt),
                        latency_ms=0, provider_chain=tried,
                    ),
                    cached=False,
                    # 记**桶 key**而不是只记 provider：租户桶被拒与全局桶被拒
                    # 是两件事，而这条审计行是排障时唯一的逐次记录。
                    # 无租户时 `cb.name` 就是 provider，字符串与改动前一致。
                    error=f"circuit_open: {cb.name}",
                )
                continue
            # 输出预算统一解析：调用显式覆盖 > 层级默认预算；统一夹到全局硬上限。
            # 先按模型配置夹到硬上限、再设目标预算，避免覆盖大于旧上限时被截回
            # （实测"覆盖没生效、空响应照旧"）。model_copy 保留其余字段。
            hard_cap = self._settings.llm_max_tokens_hard_cap
            if spec.max_tokens > hard_cap:
                spec = spec.model_copy(update={"max_tokens": hard_cap})
            wanted_budget = (
                int(max_tokens) if (max_tokens and int(max_tokens) > 0)
                else int(_TIER_OUTPUT_BUDGET.get(task_tier, spec.max_tokens))
            )
            # ★★ 本地跳的输出必须能在那段**模型侧窗口**内跑完（`CHG-0179`）。
            #
            # 反证（实测）：`_TIER_OUTPUT_BUDGET["reasoning"] = 8192` 会**覆盖**
            # `models.yaml` 里 `local_medium` 的 `max_tokens: 4096`。而
            #   8192 ÷ 44.1 tok/s = **185.8 s** ≫ 模型侧窗口 80 s
            # ⇒ 那个调用**数学上不可能成功**：跑满 80 秒、HTTP 超时、
            #   非流式 ⇒ **一个 token 都拿不到**（不是"少给一点"，是 0）。
            #
            # 压到窗口之内不是"降低能力"，而是把**必然的 0 产出**换成
            # **能跑完的答案**。
            is_local_hop = _provider_is_local(spec.provider, self._providers)
            if is_local_hop:
                wanted_budget = min(wanted_budget, LOCAL_MAX_OUTPUT_TOKENS)
            spec = spec.model_copy(update={
                "max_tokens": min(wanted_budget, hard_cap),
                "reasoning_effort": effort,
            })
            # ★ 任务级期限（`src/core/deadline.py`）：允许输出也要按剩余时间折算，
            #   否则"允许 8192 token"照样跑不完 —— 那正是 2026-10-05 那次
            #   A09 白等 25s 的根因（两个预算互相矛盾）。
            #   未启用期限时**一行都不改**（默认行为逐字不变）。
            #
            # ★ 折算用的速率**必须按 provider 取**（`CHG-0179`）：
            #   `_OUTPUT_TOKENS_PER_SEC = 210` 是 **deepseek 的实测值**
            #   （195~223 tok/s），而本地实测 **44.1 tok/s** —— 差 4.8 倍。
            #   用 210 去折算本地输出，等于允许它生成 4.8 倍于它跑得完的量。
            _tps = LOCAL_TOKENS_PER_SEC if is_local_hop else _OUTPUT_TOKENS_PER_SEC
            _cap = deadline_mod.output_token_cap(_tps,
                                                 ttft_sec=_TTFT_ALLOWANCE_SEC)
            if _cap and spec.max_tokens > _cap:
                spec = spec.model_copy(update={"max_tokens": _cap})
            try:
                # 只在真的有 schema 时才传该 kwarg：测试/自定义 provider 的
                # chat 签名可能只有 json_mode，无条件传会 TypeError。
                chat_kwargs: dict[str, Any] = {"json_mode": json_mode}
                if json_schema is not None:
                    chat_kwargs["json_schema"] = json_schema
                # 延迟预算：**只对链上还有退路的那几跳生效**。
                # 最后一跳设预算会把"慢但正确"变成"必然失败"。
                budget = (attempt_budget_sec if attempt_budget_sec is not None
                          else _ATTEMPT_BUDGET.get(task_tier, 0.0))
                if attempt_budget_sec is None:
                    # ★ 2026-10-05：链首是**付费**模型时，预算必须够它把
                    #   `_TIER_OUTPUT_BUDGET` 允许的输出跑完（理由与实测见
                    #   `_paid_primary_budget`）。本地链首与所有降级位不受影响。
                    budget = _paid_primary_budget(
                        budget, spec.max_tokens,
                        paid_primary=(index == 0
                                      and _provider_is_paid(spec.provider,
                                                            self._providers)))
                # ★ 全局上限（/紧急口径）：压在**所有**跳之上，包括显式传入的
                #   预算 —— 它是"这次调用最多等多久"的硬承诺，不是建议。
                if _ATTEMPT_BUDGET_CAP_SEC > 0 and budget > _ATTEMPT_BUDGET_CAP_SEC:
                    budget = _ATTEMPT_BUDGET_CAP_SEC
                # ★ 任务级期限：把这一跳压进"剩余时间 − 收尾预留"。
                #   未启用期限 ⇒ `clamp` 原样返回（默认行为不变）。
                budget = deadline_mod.clamp(budget, floor=deadline_mod.MIN_HOP_SEC)
                has_next = index < len(chain) - 1
                # ⚠️ **显式传入的预算在最后一跳也生效**（2026-09-28 结构修补）。
                #
                # 上面那条启发式（"最后一跳不设预算"）本意是保护**隐式的层级默认值**：
                # 没有退路时把"慢但正确"变成"必然失败"是净损失。
                # 但它与 `local_only: true` 交叉出了一个洞：
                #
                #   local_only 钉死本地 → 链只剩 1 跳 → has_next=False
                #   → 预算被跳过 → 裸调 → 本地模型挂死时**拿满 HTTP 120s**
                #
                # 实测（2026-09-28）：规划层 `supervisor_planner` 因此耗
                # **120294ms / in=0 out=0**，端到端 123.45s 全被它吃掉，
                # 而它自己是 `except Exception: return None` 静默回退规则式规划 ——
                # **结论正确、耗时 40 倍**。
                #
                # 判据：**显式传入**预算 ≡ 调用方声明"我有兜底，超时对我是可接受的失败"
                # （规划层的兜底就是规则式规划）。此时尊重调用方，而不是替他保守。
                # ★★ 2026-10-05 **撤回**上一版在这里加的 `or deadline_mod.active()`：
                #   它让"任务级期限"也算显式声明 ⇒ 最后一跳也拿预算 ⇒ 期限耗尽时
                #   三跳各被夹到 1s、**整任务失败**（实测事故：前端报
                #   「任务全部模型调用失败」）。原启发式的理由仍然成立：
                #   **没有退路时，把"慢但正确"变成"必然失败"是净损失**。
                #   期限现在由 `deadline.clamp()` 保证"到点就不再夹"（见该函数规则 3），
                #   不需要也不允许碰"最后一跳"这条线。
                explicit = attempt_budget_sec is not None
                if budget and budget > 0 and (has_next or explicit):
                    resp = await self._await_with_budget(
                        provider, spec, system, prompt, chat_kwargs, budget=budget,
                        agent_id=agent_id, scope=scope, use_cache=use_cache,
                        cache_ttl_hours=cache_ttl_hours, task_tier=task_tier,
                        trace_id=trace_id, anchor=anchor)
                else:
                    resp = await provider.chat(spec, system, prompt, **chat_kwargs)
                # 成功 → 清零该模型的"连续 429"计数（"连续"的语义）
                self._rate_guard.record_success(model_name)
            except TimeoutError:
                # 超时视为**该跳不可用**（不是全链失败）：中断慢的本地模型，
                # 落到备模型。这正是"本地成功但极慢"拖住链路的解法。
                last_error = LLMGatewayError(
                    f"{spec.provider}({spec.model_name}) 超出单跳延迟预算 "
                    f"{budget:.0f}s，降级到备模型")
                # 不计入熔断：慢 ≠ 坏。把慢源计进熔断会把它标记为"故障"，
                # 而它其实能出正确结果，只是不该在这个预算内等。
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=LLMResponse(
                        content="", model_used=spec.model_name,
                        provider=spec.provider,
                        prompt_hash=prompt_fingerprint(system, prompt),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        provider_chain=tried,
                    ),
                    cached=False,
                    error=f"attempt_budget_exceeded({budget:.0f}s): {spec.provider}",
                )
                continue
            except LocalQueueTimeout as exc:
                # ★★ 2026-10-05：本地排队超时**必须留痕**，且**不许改变异常类型**。
                #
                # ## 为什么必须有这个分支（这是一条"零痕迹失败路径"）
                #
                # `LocalQueueTimeout` **刻意不继承** `LLMGatewayError`
                # （理由见 `local_gate` 的类文档：怕"本地在排队"被翻译成
                #  "降级到付费的 deepseek-flash"）。但本函数此前只捕
                # `TimeoutError` 与 `LLMGatewayError` 两种，`providers.py`
                # 只捕 `httpx.HTTPError`/`ValueError`/`KeyError` ⇒ 它**从三个
                # 入口全部漏过**，一路穿透 `complete()` 抛给调用方：
                #
                #   · 不降级   ← 符合设计意图
                #   · 不花钱   ← 符合设计意图
                #   · **不写审计** ← 这是缺陷
                #
                # 后果（实测）：本地调用 p50 4.5s 但 max **210.1s**
                # （= 90s 排队上限 + 120s HTTP 超时，两个常量由两个模块各自定义、
                #  从未相加），而**"排队超时"这一支在
                #  `data/**/audit/llm_audit.jsonl` 里一行都没有**。
                # 12,651 条 `provider=ollama` 里 ≥90s 的 97 条全部是
                # "抢到了槽位"的；**没抢到的那些无从计数** ——
                # 最严重的失败形态恰好是审计里查不到的那一种。
                # 一个只记录成功排队、不记录排队失败的观测面，会把"越来越挤"
                # 显示成"一切正常"。
                #
                # ## 为什么 `raise` 原样重抛，而不是 `continue` 降级
                #
                # 链尾**永远**是本地模型（`configs/models.yaml` 五层的最后一名
                # 都是 `local_light` / `local_medium`），所以 `continue` 今天等价于
                # 直接结束循环。但**将来若有人在本地之后加一跳付费模型**，
                # `continue` 就会把"本地在排队"静默翻译成"花钱" ——
                # 而那正是 `local_gate` 这一层存在的全部理由
                # （契约由 `tests/unit/test_local_llm_gate.py::
                #  test_queue_timeout_is_not_a_gateway_error` 钉住）。
                # 重抛让**契约与代码一致**，且调用方今天看到的异常类型
                # **一个字都不变**。
                #
                # 不计入熔断：**排队 ≠ 坏**。与上面 `TimeoutError` 分支同一条纪律
                # （"慢 ≠ 坏"）—— 把"忙"记成"故障"会让熔断器在高峰期打开，
                # 用 `circuit_open` 淹没真正的病因。
                #
                # ⚠️ 留痕本身**必须**被包住：`record()` 要写文件（磁盘满 / 权限 /
                #    句柄耗尽都会抛）。不包的话，"留痕失败"会把原始的
                #    `LocalQueueTimeout` **替换**成 IO 异常 —— 调用方看到的病因
                #    就从"本地在排队"变成"审计写不进去"，而 `local_gate` 那条
                #    契约（不许把排队变成别的语义）也就跟着破了。
                #    纪律同 `analysis/base.py`：「审计写不进去不能反过来把
                #    兜底也弄坏」。
                try:
                    self._audit.record(
                        trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                        response=LLMResponse(
                            content="", model_used=spec.model_name,
                            provider=spec.provider,
                            prompt_hash=prompt_fingerprint(system, prompt),
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            provider_chain=tried,
                        ),
                        cached=False,
                        error=f"local_queue_timeout: {exc}"[:400],
                    )
                except Exception:  # noqa: BLE001 留痕失败绝不改变原始异常
                    logger.warning(
                        "本地排队超时的审计写入失败（原始异常照常抛出）",
                        exc_info=True)
                raise
            except LLMGatewayError as exc:
                last_error = exc
                # ★ 限流（429）单独计数：连续 N 次就锁定该模型，后续直接从
                # 链上跳过。判据优先用**结构化状态码**（`http_status`），
                # 只在拿不到状态码时才回退到文案匹配 —— 文案是 httpx 的实现
                # 细节，改个写法判据就静默失效（见 exceptions.LLMGatewayError）。
                if looks_rate_limited(
                        exc,
                        http_status=getattr(exc, "http_status", None)):
                    self._rate_guard.record_rate_limited(
                        model_name, reason=str(exc))
                # 只有**瞬时**故障才计入熔断；配置类错误（缺 key/401/402/403）
                # 重试不会自愈，计进去只会把熔断器打开、用 circuit_open 淹没病因。
                counted = bool(getattr(exc, "count_as_failure", True))
                if counted:
                    cb.record_failure()
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=LLMResponse(
                        content="", model_used=spec.model_name, provider=spec.provider,
                        prompt_hash=prompt_fingerprint(system, prompt),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        provider_chain=tried,
                    ),
                    cached=False,
                    error=str(exc) if counted else f"config_error(不计熔断): {exc}",
                )
                continue

            resp.provider_chain = list(tried)
            resp.fallback_used = index > 0
            resp.trace_id = trace_id
            # ★ 2026-09-27 第八轮：单次调用真实费用写入响应
            #   让每条审计记录自带"花了多少钱"，让 token 优化**可被钱验证**
            try:
                from src.core.budget import call_cost_cny
                resp.cost_yuan = call_cost_cny(
                    provider=spec.provider, model=spec.model_name,
                    tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                    # 真的调用了提供商 ⇒ 输入按**未命中价**计。
                    # 参数名不叫 `cache_hit`，理由见 `call_cost_cny` 的 docstring：
                    # 审计里的 `cache_hit` 是**本地**缓存命中（那种调用根本走不到这里）。
                    provider_cache_hit=False,
                )
            except Exception:  # noqa: BLE001 计价失败不影响调用结果
                logger.debug("cost_yuan 计价失败（忽略）", exc_info=True)
            cb.record_success()  # 熔断器成功计数
            # 累计token预算（仅DeepSeek）
            if spec.provider == "deepseek" and trace_id:
                self._token_usage[trace_id] = (
                    self._token_usage.get(trace_id, 0)
                    + resp.tokens_in + resp.tokens_out
                )
            # 日成本记账（所有调用方共用同一个池子）：投研请求按预算预扣，
            # 批量作业（mainline_relevance 等）只记账不拦截 —— 实测它们才是
            # 真正烧钱的那部分（484 元 vs 投研每次 0.05 元），
            # 硬拦会把跑了一半的批处理变成脏状态，该由它们自己查 remaining()。
            try:
                from src.core.budget import get_budget

                get_budget().record(
                    provider=spec.provider, model=spec.model_name,
                    tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                    provider_cache_hit=False, source=agent_id or task_tier,
                )
            except Exception:  # noqa: BLE001 记账失败绝不能影响调用结果
                logger.debug("LLM 成本记账失败（忽略）", exc_info=True)
            # ⚠️ 空响应**不写缓存**（理由同前面的命中判断）：否则一次失败会被
            # 缓存固化，此后所有重试都被这条空记录挡住，故障变成"永久性"的。
            if self._cache and use_cache and (resp.content or "").strip():
                # ★ 先算 anchor 向量再写（`CHG-0178`）：`put` 保持同步，
                #   网络调用留在这里的 async 上下文里做。
                #   失败 ⇒ None ⇒ 该条目不参与 L3 精排（仍可被精确命中与 L2 召回），
                #   **绝不因为"存向量失败"而丢掉整条缓存**。
                #
                # ⚠️ `semantic_cache=False` 时**不白算向量**（`CHG-0181`）：
                #   那个调用点永远不会走语义层，算了也没人会读 ——
                #   白花一次网络往返，正是本模块一直在防的事。
                embedding = None
                if semantic_cache:
                    try:
                        embedding = await self._cache.anchor_embedding(
                            system, prompt, anchor)
                    except Exception:  # noqa: BLE001 存向量失败不能连累写缓存
                        logger.info("anchor 向量计算失败（该条目不参与精排）",
                                    exc_info=True)
                        embedding = None
                self._cache.put(
                    system, prompt, resp, agent_id, scope=scope,
                    ttl_seconds=(None if cache_ttl_hours is None
                                 else cache_ttl_hours * 3600.0),
                    anchor=anchor, embedding=embedding)
            self._audit.record(
                trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                response=resp, cached=False,
            )
            return resp

        raise LLMGatewayError(
            f"全部模型调用失败（链: {'→'.join(tried)}）: {last_error}"
        )
