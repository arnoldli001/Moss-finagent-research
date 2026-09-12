"""LLM指标聚合离线测试。"""

from __future__ import annotations

import json

from src.infrastructure.observability.metrics import (
    aggregate_llm_metrics,
    percentile,
    read_and_aggregate,
)


def _entry(
    latency: int,
    *,
    cache: bool = False,
    fallback: bool = False,
    error: str | None = None,
    provider: str = "ollama",
    agent: str = "A08_macro",
    tokens_in: int = 100,
    tokens_out: int = 50,
) -> dict:
    return {
        "latency_ms": latency, "cache_hit": cache, "fallback_used": fallback,
        "error": error, "provider": provider, "agent_id": agent,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
    }


def test_percentile_nearest_rank():
    assert percentile([], 95) is None
    vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(vals, 50) == 50
    assert percentile(vals, 95) == 100
    assert percentile(vals, 99) == 100


def test_aggregate_rates_and_latency():
    entries = [
        _entry(100, cache=True),
        _entry(200, cache=True),
        _entry(3500, fallback=True),  # 慢调用+降级
        _entry(400, error="timeout"),
    ]
    m = aggregate_llm_metrics(entries)
    assert m["window_calls"] == 4
    assert m["cache_hit_rate"] == 0.5
    assert m["fallback_rate"] == 0.25
    assert m["error_rate"] == 0.25
    assert m["slow_calls_over_3s"] == 1
    assert m["latency_ms"]["p50"] == 200
    assert m["latency_ms"]["p95"] == 3500
    assert m["latency_ms"]["avg"] == 1050
    assert m["tokens"]["total"] == 600


def test_aggregate_provider_and_agent_breakdown():
    entries = [
        _entry(100, provider="ollama", agent="A08_macro"),
        _entry(200, provider="ollama", agent="A09_meso", cache=True),
        _entry(300, provider="deepseek", agent="A08_macro", error="x"),
    ]
    m = aggregate_llm_metrics(entries)
    by_prov = {p["provider"]: p for p in m["by_provider"]}
    assert by_prov["ollama"]["calls"] == 2
    assert by_prov["ollama"]["cache_hit_rate"] == 0.5
    assert by_prov["deepseek"]["error_rate"] == 1.0
    by_agent = {a["agent_id"]: a["calls"] for a in m["by_agent"]}
    assert by_agent == {"A08_macro": 2, "A09_meso": 1}


def test_aggregate_empty_window():
    m = aggregate_llm_metrics([])
    assert m["window_calls"] == 0
    assert m["cache_hit_rate"] is None
    assert m["latency_ms"] == {"p50": None, "p95": None, "p99": None,
                               "avg": None, "max": None}


def test_read_and_aggregate_respects_limit_and_missing_file(tmp_dir):
    path = f"{tmp_dir}/obs/llm_audit.jsonl"
    # 文件不存在：空窗不报错
    assert read_and_aggregate(path)["window_calls"] == 0

    rows = [_entry(100 * i) for i in range(1, 6)]
    import pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    assert read_and_aggregate(path, limit=2)["window_calls"] == 2
    assert read_and_aggregate(path)["window_calls"] == 5
