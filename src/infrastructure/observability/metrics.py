"""LLM调用指标聚合：从审计JSONL计算延迟分位/缓存命中/降级/慢调用（OBSERVABILITY.md三）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SLOW_CALL_MS = 3000  # 慢查询阈值：超过3秒计入慢调用


def percentile(sorted_values: list[int], pct: float) -> int | None:
    """最近秩百分位（nearest-rank），sorted_values须升序。"""
    if not sorted_values:
        return None
    rank = max(1, round(pct / 100 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def aggregate_llm_metrics(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """对LLMAuditLog记录聚合：总量/分位延迟/缓存/降级/token/按提供方与Agent分解。"""
    total = len(entries)
    latencies = sorted(
        e["latency_ms"] for e in entries
        if isinstance(e.get("latency_ms"), (int, float))
    )
    errors = [e for e in entries if e.get("error")]
    cache_hits = [e for e in entries if e.get("cache_hit")]
    fallbacks = [e for e in entries if e.get("fallback_used")]
    slow = [e for e in latencies if e >= _SLOW_CALL_MS]

    tokens_in = sum(e.get("tokens_in") or 0 for e in entries)
    tokens_out = sum(e.get("tokens_out") or 0 for e in entries)

    def _rate(n: int) -> float | None:
        return round(n / total, 4) if total else None

    by_provider: dict[str, dict[str, Any]] = {}
    for e in entries:
        prov = str(e.get("provider") or "unknown")
        bucket = by_provider.setdefault(
            prov, {"calls": 0, "errors": 0, "cache_hits": 0,
                   "latencies_ms": []}
        )
        bucket["calls"] += 1
        if e.get("error"):
            bucket["errors"] += 1
        if e.get("cache_hit"):
            bucket["cache_hits"] += 1
        if isinstance(e.get("latency_ms"), (int, float)):
            bucket["latencies_ms"].append(e["latency_ms"])
    provider_breakdown = []
    for prov, b in sorted(by_provider.items(), key=lambda kv: -kv[1]["calls"]):
        lats = sorted(b["latencies_ms"])
        calls = b["calls"]
        provider_breakdown.append({
            "provider": prov,
            "calls": calls,
            "error_rate": round(b["errors"] / calls, 4) if calls else None,
            "cache_hit_rate": round(b["cache_hits"] / calls, 4) if calls else None,
            "p95_latency_ms": percentile(lats, 95),
        })

    by_agent: dict[str, int] = {}
    for e in entries:
        agent = str(e.get("agent_id") or "unknown")
        by_agent[agent] = by_agent.get(agent, 0) + 1
    agent_calls = [
        {"agent_id": a, "calls": n}
        for a, n in sorted(by_agent.items(), key=lambda kv: -kv[1])
    ]

    return {
        "window_calls": total,
        "errors": len(errors),
        "error_rate": _rate(len(errors)),
        "cache_hit_rate": _rate(len(cache_hits)),
        "fallback_rate": _rate(len(fallbacks)),
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "avg": int(sum(latencies) / len(latencies)) if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
        "slow_calls_over_3s": len(slow),
        "tokens": {"in": tokens_in, "out": tokens_out,
                   "total": tokens_in + tokens_out},
        "by_provider": provider_breakdown,
        "by_agent": agent_calls,
    }


def read_and_aggregate(audit_path: str | Path, limit: int = 1000) -> dict[str, Any]:
    """读取最近limit条LLM审计记录并聚合（文件不存在返回空窗指标）。"""
    path = Path(audit_path)
    entries: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        entries = [json.loads(line) for line in lines if line.strip()]
    return aggregate_llm_metrics(entries)
