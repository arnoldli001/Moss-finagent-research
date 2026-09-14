"""端到端冒烟：真实akshare采集 + 真实LLM网关（Ollama降级链）跑通全图。

运行：cd d:/code/Moss-finagent-research; $env:PYTHONPATH="."; uv run python scripts/_smoke_e2e.py
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
        "info_items": [], "verified_items": {}, "extracted_events": {},
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
    print(f"[E2E] 审计链 valid={chain['valid']} records={chain['count']} "
          f"head={str(chain['head'])[:16]}")
    print("[E2E] LLM审计条目数:",
          len([e for e in runtime.gateway.audit_log.read_all()
               if e.get("trace_id") == state["task_id"]]))
    print("=" * 60)
    print(final["final_report"])

    # ---- 信息层冒烟（A05→A06→A07，纯本地文本，不采集数据）----
    info_state = {
        "task_id": f"task_smoke_info_{int(time.time())}",
        "tenant_id": "tenant_001",
        "user_query": "市场上最近有哪些值得关注的事件？情绪如何？",
        "analysis_type": "news",
        "target": "",
        "plan": [], "raw_points": [], "cleaned_points": [], "validated_points": [],
        "validation_report": {}, "storage_stats": {},
        "info_items": [
            {"title": "国家统计局发布通胀数据", "source_name": "国家统计局",
             "publish_time": "2026-09-09T09:30:00+08:00",
             "text": "2026年8月CPI同比上涨0.4%，PPI同比下降3.6%，"
                     "工业品出厂价格延续负增长区间。"},
            {"title": "惊天利好！这只股票必涨", "source_name": "股吧",
             "publish_time": "2026-09-11T20:00:00+08:00",
             "text": "内幕消息，某科技公司即将获得百亿订单，股价必将翻倍，"
                     "满仓干就完了！"},
        ],
        "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }
    info_started = time.perf_counter()
    info_final = await runtime.graph.ainvoke(info_state)
    info_elapsed = time.perf_counter() - info_started
    print("=" * 60)
    print(f"[E2E-INFO] 耗时 {info_elapsed:.1f}s | 错误 {info_final['errors'] or '无'}")
    for o in info_final["agent_outputs"]:
        print(f"  - {o['agent_id']} ({o['confidence']}): {o['conclusion'][:80]}")
    print(info_final["final_report"])
    info_ok = any(o["agent_id"] == "A05_verifier" for o in info_final["agent_outputs"]) and \
        any(o["agent_id"] == "A07_sentiment" for o in info_final["agent_outputs"])

    # ---- 行业层冒烟（industry类型 + 关键词路由：煤炭→A09中观+A15周期，真实PPI数据）----
    ind_state = {
        "task_id": f"task_smoke_ind_{int(time.time())}",
        "tenant_id": "tenant_001",
        "user_query": "煤炭行业当前景气度如何？处于库存周期什么位置？",
        "analysis_type": "industry",
        "target": "煤炭",
        "plan": [], "raw_points": [], "cleaned_points": [], "validated_points": [],
        "validation_report": {}, "storage_stats": {},
        "info_items": [], "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }
    ind_started = time.perf_counter()
    ind_final = await runtime.graph.ainvoke(ind_state)
    ind_elapsed = time.perf_counter() - ind_started
    print("=" * 60)
    print(f"[E2E-INDUSTRY] 耗时 {ind_elapsed:.1f}s | 错误 {ind_final['errors'] or '无'}")
    for o in ind_final["agent_outputs"]:
        print(f"  - {o['agent_id']} ({o['confidence']}): {o['conclusion'][:80]}")
    print(ind_final["final_report"])
    a15 = next((o for o in ind_final["agent_outputs"]
                if o["agent_id"] == "A15_cyclical"), None)
    industry_ok = (
        a15 is not None
        and (a15["result"] or {}).get("industry_signal_calc") is not None
        and (a15["result"]["industry_signal_calc"].get("watched_indicator_count") or 0) > 0
    )
    if a15 is not None:
        print("[E2E-INDUSTRY] 本地景气信号:",
              json.dumps(a15["result"].get("industry_signal_calc"), ensure_ascii=False))

    # ---- 科技行业段（模拟产业数据打通：半导体→A09+A13，ind:模拟指标+真实CPI/PPI）----
    tech_state = {
        "task_id": f"task_smoke_tech_{int(time.time())}",
        "tenant_id": "tenant_001",
        "user_query": "半导体行业当前景气度如何？渗透率处于什么阶段？",
        "analysis_type": "industry",
        "target": "半导体",
        "plan": [], "raw_points": [], "cleaned_points": [], "validated_points": [],
        "validation_report": {}, "storage_stats": {},
        "info_items": [], "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None,
    }
    tech_started = time.perf_counter()
    tech_final = await runtime.graph.ainvoke(tech_state)
    tech_elapsed = time.perf_counter() - tech_started
    print("=" * 60)
    print(f"[E2E-TECH] 耗时 {tech_elapsed:.1f}s | 错误 {tech_final['errors'] or '无'}")
    print("[E2E-TECH] 采集指标:",
          sorted({p["indicator"] for p in tech_final["raw_points"]}))
    for o in tech_final["agent_outputs"]:
        if o["agent_id"].startswith(("A13", "A17")):
            print(f"  - {o['agent_id']} ({o['confidence']}): {o['conclusion'][:100]}")
    a13 = next((o for o in tech_final["agent_outputs"]
                if o["agent_id"] == "A13_tech"), None)
    tech_ok = (
        a13 is not None
        and not (a13["result"] or {}).get("skipped")
        and (a13["result"]["industry_signal_calc"].get("watched_indicator_count") or 0) >= 48
    )
    if a13 is not None:
        print("[E2E-TECH] 本地景气信号:",
              json.dumps(a13["result"].get("industry_signal_calc"), ensure_ascii=False))

    ok = (
        len(final["raw_points"]) > 0
        and final["final_report"]
        and chain["valid"]
        and any(o["agent_id"] == "A08_macro" for o in final["agent_outputs"])
        and info_ok
        and industry_ok
        and tech_ok
    )
    Path("data").mkdir(exist_ok=True)
    Path("data/_smoke_e2e_result.json").write_text(json.dumps({
        "ok": bool(ok), "elapsed": elapsed, "errors": final["errors"],
        "storage": final["storage_stats"],
        "info_elapsed": info_elapsed, "info_errors": info_final["errors"],
        "industry_elapsed": ind_elapsed, "industry_errors": ind_final["errors"],
        "industry_signal": (a15["result"].get("industry_signal_calc") if a15 else None),
        "tech_elapsed": tech_elapsed, "tech_errors": tech_final["errors"],
        "tech_signal": (a13["result"].get("industry_signal_calc") if a13 else None),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SMOKE:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
