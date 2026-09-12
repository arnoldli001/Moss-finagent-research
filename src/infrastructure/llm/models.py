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


class LLMResponse(BaseModel):
    """网关统一响应。

    provider_chain记录实际尝试序列（含降级），cache_hit标记缓存命中，
    prompt_hash/response_hash供审计链比对。
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
