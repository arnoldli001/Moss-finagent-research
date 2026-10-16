"""LLM网关数据模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TaskTier = Literal["light", "medium", "planning", "reasoning", "decision"]


class ModelSpec(BaseModel):
    """单个模型的完整规格（来自configs/models.yaml）。"""

    name: str
    provider: str
    model_name: str
    base_url: str
    max_tokens: int = 2048
    temperature: float = 0.1
    #: 该模型**跑起来需要多少显存**（MB，仅本地模型有意义）。
    #: 为什么要写进配置（2026-09-28）：本机只有 8GB 显存，而选模型时
    #: 没考虑上限 —— 大模型驻留时小模型换不进来，请求直接挂死 120s。
    #: 有了这个数字，网关才能在拉不起来时**改走云端**而不是干等。
    #: 未声明时按 vram.DEFAULT_MODEL_VRAM_MB 估算。
    vram_mb: int = 0
    # DeepSeek 原生思维链强度（none/low/high/max）；空串=不下发，用服务端默认。
    reasoning_effort: str = ""
    #: 本地（Ollama）思维链开关。`None` = 按 `MOSS_LOCAL_THINK` 解析，
    #: **默认关**（`OllamaProvider._resolve_think` 有实测依据：
    #: 思考 token 计入 `num_predict`，开思考会让 4B 100% 空返回、
    #: 让 8B 白花一倍延迟而能力不变）。
    #: 云端模型忽略此字段（DeepSeek 用 `reasoning_effort`，OpenAI 兼容端点
    #: 不支持该参数）。
    think: bool | None = None


class LLMResponse(BaseModel):
    """网关统一响应。

    provider_chain记录实际尝试序列（含降级），cache_hit标记缓存命中，
    prompt_hash/response_hash供审计链比对。

    ★ 2026-09-27 第八轮：新增 `cost_yuan` 字段，让每条响应自带真实花费（元）。
    来源：`src.core.budget.call_cost_cny`（**唯一的计价实现**，避免账本与监控分叉）。
    本地模型（provider != "deepseek"）= 0.0；未登记价用兜底价（**宁可高估不漏记**）。
    """

    content: str
    model_used: str
    provider: str
    tokens_in: int = 0
    #: 输出总 token（含思维链 —— DeepSeek 把 reasoning 计入 completion_tokens）
    tokens_out: int = 0
    #: ★ 2026-09-28 第十二轮：输出中**属于思维链**的那部分（DeepSeek
    #: `usage.completion_tokens_details.reasoning_tokens`）。
    #:
    #: 为什么必须单独记（实测归因）：A17 一次调用 3168 output / 32.7s，
    #: 但**无法判断这 3168 里多少是"想"、多少是"写"** ——
    #:   · 若大头是思维链 → 降 `reasoning_effort` 直接见效
    #:   · 若大头是正文   → 降 effort 无用，只能压缩输出长度
    #: 没有这个字段，两种优化方向无法区分（研究结论：官方 reasoning_effort
    #: 默认 high，且关思考后 3168 token 正文仍需 ~32s）。
    #: 0 = 未提供（本地 Ollama / 非推理模型 / 老版本 API）。
    reasoning_tokens: int = 0
    #: ★ 2026-10-16（`CHG-0250`）：本次调用**实际生效的输出上限**（token）。
    #:
    #: 为什么要它：`tokens_out` 单独看不出"是不是被截断"——
    #: `out=900` 与 `out=2048（上限 2048）` 的含义完全相反，而两者原先在错误里
    #: 长得一样（都只是"非合法JSON"）。有了这个字段，调用点才能区分
    #: 「模型写坏了 JSON」与「**要得太多、装不下**」。
    #: `0` = 未知（老路径/未设置）⇒ 调用方**不许**据此断言截断。
    max_tokens: int = 0
    latency_ms: int = 0
    #: ★ 2026-10-05：这一跳**排队**了多久（毫秒）—— 等 `local_gate` 槽位的时间。
    #:
    #: ## 为什么必须与 `latency_ms` 拆开（实测，`CHG-0177`）
    #:
    #: 实测 `provider=ollama` 共 12,651 次调用：p50 4,542ms 而
    #: **max 210,136ms**。那 210 秒 = `local_gate` 的 **90s** 排队上限
    #: + HTTP 的 **120s** 超时（两个常量由两个模块各自定义）。
    #: 合成一个 `latency_ms` 时，"这 210 秒里排队占多少、生成占多少"
    #: **无法回答** —— 而这两个方向的处置**完全相反**：
    #:
    #:   · 排队占大头 ⇒ 该治的是**准入/争抢**（谁在抢那个唯一槽位）
    #:   · 生成占大头 ⇒ 该治的是**模型/输出预算**（换了 4B、或砍 max_tokens）
    #:
    #: 没有这个字段，任何"本地为什么慢"的结论都只能是猜。
    #:
    #: ## 口径
    #:
    #: 只在**走闸**的 provider（Ollama）上有值。云端 provider **不适用**
    #: ⇒ `None`。⚠️ `None` = **未量到 / 不适用**，**不是 0**
    #: （AGENTS.md：「没量到」与「量到 0」必须分开）。
    wait_ms: int | None = None
    #: **模型侧**耗时（毫秒）= `latency_ms - wait_ms`。
    #:
    #: ## ⚠️ 为什么叫 `model_ms` 而不是 `gen_ms`（实测后改名，`CHG-0177`）
    #:
    #: 它**不只**是生成，而是"从拿到槽位到响应返回"的全部模型侧时间：
    #: **模型加载/换入 + prompt 预填充 + 生成**。
    #:
    #: 实测（6 路并发打真实 Ollama，`qwen2.5:1.5b`，短 prompt，`_probe_wait_gen_split`）：
    #:
    #:     #0  wait=0ms     model_ms=2383ms  out=3 tok  ← 2.38s 是**加载**，不是生成
    #:     #1  wait=2359ms  model_ms=31ms    out=3 tok  ← 模型已驻留，真的只花 31ms
    #:
    #: 若叫 `gen_ms`，就会有人算"平均生成速度 = tokens / gen_ms" ——
    #: 在冷调用上得到 1.3 tok/s 这种荒谬值。那正是本项目已登记过的
    #: 「**同名不同义**」缺陷（两个 `cache_hit` 曾差出 65.5% 的金额）。
    #: **名字必须和口径一样准。**
    #:
    #: ⚠️ 口径同 `wait_ms`：云端 provider 为 `None`（不是 0）——
    #: 没有可减的基准时，填 `latency_ms` 会把"不适用"伪装成"零排队"。
    model_ms: int | None = None
    cache_hit: bool = False
    cache_kind: Literal["exact", "semantic", "none"] = "none"
    fallback_used: bool = False
    provider_chain: list[str] = Field(default_factory=list)
    prompt_hash: str = ""
    response_hash: str = ""
    trace_id: str = ""
    #: 单次调用真实费用（元）。由 gateway 在调用成功后填入，
    #: 经 budget.py → audit.py 落审计 JSONL，让 token 优化可被钱验证。
    cost_yuan: float = 0.0
