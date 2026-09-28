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
    latency_ms: int = 0
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
