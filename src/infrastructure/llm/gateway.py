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

from src.core.cancel import CancellationToken
from src.core.config import Settings, get_settings
from src.core.exceptions import ConfigError, LLMGatewayError
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.cache import LLMCache, cache_key
from src.infrastructure.llm.circuit_breaker import get_circuit_registry
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
LOCAL_PROVIDERS: Final[frozenset[str]] = frozenset({"ollama"})

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
             if s.provider in LOCAL_PROVIDERS and s.base_url), "")
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
    ) -> LLMResponse:
        """执行一次补全：缓存→主模型→备模型，全程审计。

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
                           if self._specs[m].provider in LOCAL_PROVIDERS]
            if not local_chain:
                raise LLMGatewayError(
                    f"{task_tier} 层没有本地模型可选（local_only）："
                    f"链={chain}，计费提供商={sorted(PAID_PROVIDERS)}")
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
                               if self._specs[m].provider not in LOCAL_PROVIDERS]
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

        if self._cache and use_cache:
            # 用 `aget` 而非 `get`：语义查找需要扫描/建索引，走线程池卸载，
            # 否则 9ms 级同步 IO 会在事件循环上冻结**所有**并发请求
            # （实测事件循环最大停顿 22.3ms；见 cache.py 模块注释）。
            hit = await self._cache.aget(system, prompt, agent_id, scope=scope)
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
        uses_paid = any(self._specs[m].provider in PAID_PROVIDERS for m in chain)
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
            # 熔断器准入检查：熔断中直接跳过该provider，降级到备模型
            cb = get_circuit_registry().get_or_create(spec.provider)
            if not cb.allow_request():
                last_error = LLMGatewayError(
                    f"提供商{spec.provider}熔断中({cb.snapshot()['state']})，"
                    "自动降级到备模型"
                )
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=LLMResponse(
                        content="", model_used=spec.model_name, provider=spec.provider,
                        prompt_hash=cache_key(system, prompt),
                        latency_ms=0, provider_chain=tried,
                    ),
                    cached=False, error=f"circuit_open: {spec.provider}",
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
            spec = spec.model_copy(update={
                "max_tokens": min(wanted_budget, hard_cap),
                "reasoning_effort": effort,
            })
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
                explicit = attempt_budget_sec is not None
                if budget and budget > 0 and (has_next or explicit):
                    resp = await asyncio.wait_for(
                        provider.chat(spec, system, prompt, **chat_kwargs),
                        timeout=budget)
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
                        prompt_hash=cache_key(system, prompt),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        provider_chain=tried,
                    ),
                    cached=False,
                    error=f"attempt_budget_exceeded({budget:.0f}s): {spec.provider}",
                )
                continue
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
                        prompt_hash=cache_key(system, prompt),
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
                    cache_hit=False,
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
                    cache_hit=False, source=agent_id or task_tier,
                )
            except Exception:  # noqa: BLE001 记账失败绝不能影响调用结果
                logger.debug("LLM 成本记账失败（忽略）", exc_info=True)
            # ⚠️ 空响应**不写缓存**（理由同前面的命中判断）：否则一次失败会被
            # 缓存固化，此后所有重试都被这条空记录挡住，故障变成"永久性"的。
            if self._cache and use_cache and (resp.content or "").strip():
                self._cache.put(
                    system, prompt, resp, agent_id, scope=scope,
                    ttl_seconds=(None if cache_ttl_hours is None
                                 else cache_ttl_hours * 3600.0))
            self._audit.record(
                trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                response=resp, cached=False,
            )
            return resp

        raise LLMGatewayError(
            f"全部模型调用失败（链: {'→'.join(tried)}）: {last_error}"
        )
