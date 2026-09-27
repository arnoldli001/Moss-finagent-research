"""LLM网关数据模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TaskTier = Literal["light", "medium", "reasoning", "decision"]


class ModelSpec(BaseModel):
    """单个模型的完整规格（来自configs/models.yaml）。"""

    name: str
    provider: str
    model_name: str
    base_url: str
    max_tokens: int = 2048
    temperature: float = 0.1
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
    tokens_out: int = 0
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
