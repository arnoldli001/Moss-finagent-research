"""端到端冒烟：真实akshare采集 + 真实LLM网关（Ollama降级链）跑通全图。

运行：cd d:/code/finagent-research; $env:PYTHONPATH="."; uv run python scripts/_smoke_e2e.py
说明：无DEEPSEEK_API_KEY时DeepSeek主模型快速失败自动降级Ollama本地模型。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.runtime import build_runtime  # noqa: E402
from src.infrastructure.repositories.audit_chain import ChainVerifier  # noqa: E402


async def main() -> None:
    runtime = build_runtime()
    state = {
        "task_id": f"task_smoke_{int(time.time())}",
        "tenant_id": "tenant_001",
        "user_query": "当前宏观经济处于什么阶段？对A股有什么含义？",
        "analysis_type": "macro",
        "target": "",
        "plan": [], "raw_points": [], "cleaned_points": [], "validated_points": [],
        "validation_report": {}, "storage_stats": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }
    started = time.perf_counter()
    final = await runtime.graph.ainvoke(state)
    elapsed = time.perf_counter() - started

    print("=" * 60)
    print(f"[E2E] 耗时 {elapsed:.1f}s")
    print(f"[E2E] 采集 {len(final['raw_points'])} 条 | 入库 {final['storage_stats']}")
    print(f"[E2E] 错误 {final['errors'] or '无'}")
    for o in final["agent_outputs"]:
        print(f"  - {o['agent_id']} ({o['confidence']}): {o['conclusion'][:80]}")
    chain = ChainVerifier("data/audit/audit_chain.jsonl").verify()
    print(f"[E2E] 审计链 valid={chain['valid']} records={chain['count']} head={str(chain['head'])[:16]}")
    print("[E2E] LLM审计条目数:",
          len([e for e in runtime.gateway.audit_log.read_all()
               if e.get("trace_id") == state["task_id"]]))
    print("=" * 60)
    print(final["final_report"])

    ok = (
        len(final["raw_points"]) > 0
        and final["final_report"]
        and chain["valid"]
        and any(o["agent_id"] == "A08_macro" for o in final["agent_outputs"])
    )
    Path("data").mkdir(exist_ok=True)
    Path("data/_smoke_e2e_result.json").write_text(json.dumps({
        "ok": bool(ok), "elapsed": elapsed, "errors": final["errors"],
        "storage": final["storage_stats"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SMOKE:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
