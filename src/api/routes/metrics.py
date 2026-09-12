"""运行指标端点：LLM调用延迟分位/缓存命中/降级/慢调用。"""

from __future__ import annotations

from fastapi import APIRouter

from src.core.config import get_settings
from src.infrastructure.observability.metrics import read_and_aggregate

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


@router.get("")
async def llm_metrics(limit: int = 1000) -> dict:
    """最近N条LLM调用的聚合指标（默认最近1000条）。"""
    limit = max(1, min(limit, 100_000))
    settings = get_settings()
    metrics = read_and_aggregate(
        f"{settings.llm_audit_dir}/llm_audit.jsonl", limit=limit
    )
    return {"limit": limit, "slow_call_threshold_ms": 3000, "metrics": metrics}
