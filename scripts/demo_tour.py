"""面试演示导览：按固定脚本依次演示系统全部核心能力（面向运行中的API服务）。

运行：
  1. 启动服务：$env:PYTHONPATH="."; uv run uvicorn src.api.main:app --port 8100
  2. uv run python scripts/demo_tour.py                # 不含回测（回测取数较慢）
     uv run python scripts/demo_tour.py --with-backtest
说明：纯HTTP客户端（stdlib urllib），无新增依赖；真实LLM经Ollama降级链，
     宏观任务可能耗时1-3分钟，属正常现象。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DISCLAIMER = (
    "⚠️ 演示内容来自公开数据与本地模型推理，仅供参考，不构成投资建议。"
    "投资有风险，入市需谨慎，盈亏自负。"
)


def _request(base: str, path: str, body: dict | None = None,
             timeout: int = 30) -> tuple[dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}", data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
        return payload, dict(resp.headers)


def _banner(step: str, title: str) -> None:
    print("\n" + "=" * 64)
    print(f"[{step}] {title}")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="Moss-FinAgent-Research 演示导览")
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--poll-timeout", type=int, default=300)
    parser.add_argument("--with-backtest", action="store_true")
    args = parser.parse_args()
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    # 1. 健康检查
    _banner("1/7", "服务健康与审计哈希链")
    health, _ = _request(args.base, "/api/v1/health")
    print(json.dumps(health, ensure_ascii=False, indent=2)[:800])
    check("健康检查", bool(health))
    check("LLM审计哈希链完整", health.get("audit_chain", {}).get("valid") is True,
          f"records={health.get('audit_chain', {}).get('records')}")

    # 2. Supervisor 规划（不执行，秒回）
    _banner("2/7", "Supervisor DAG 规划（宏观任务）")
    plan, _ = _request(args.base, "/api/v1/debug/plan?analysis_type=macro")
    print(f"analysis_type={plan['analysis_type']} agents={plan['agents']}")
    check("规划器返回Agent序列", len(plan.get("agents", [])) >= 2)

    # 3. 提交真实分析任务并轮询
    _banner("3/7", "异步投研任务（采集→清洗→校验→入库→分析→推荐）")
    submitted, _ = _request(args.base, "/api/v1/research/analyze", {
        "query": "当前宏观经济处于什么阶段？对A股有什么含义？",
        "analysis_type": "macro",
    }, timeout=30)
    task_id = submitted["task_id"]
    print(f"task_id={task_id} plan={submitted['plan']}")
    deadline = time.time() + args.poll_timeout
    task: dict = {}
    while time.time() < deadline:
        task, _ = _request(args.base, f"/api/v1/research/{task_id}")
        if task["status"] in ("completed", "failed"):
            break
        print(f"  ...{task['status']}，等待10s")
        time.sleep(10)
    ok_task = task.get("status") == "completed" and bool(task.get("report"))
    check("任务完成且产出报告", ok_task, task.get("status", "超时"))
    if task.get("errors"):
        print(f"  非致命错误：{task['errors']}")
    print(f"  A17结论={task.get('conclusion')} 置信度={task.get('confidence')}")

    # 4. 推理过程透明化
    _banner("4/7", "Agent输出全览（推理路径可溯源）")
    if ok_task:
        agents, _ = _request(args.base, f"/api/v1/research/{task_id}/agents")
        for o in agents["agent_outputs"]:
            print(f"  - {o['agent_id']} [{o['confidence']}] {o['conclusion'][:70]}")
        check("Agent输出可枚举", len(agents["agent_outputs"]) >= 2)
        trace, _ = _request(args.base, f"/api/v1/trace/{task_id}")
        check("Trace可查询", bool(trace))
        check("Trace含LLM调用审计", len(trace.get("llm_calls", [])) >= 1,
              f"{len(trace.get('llm_calls', []))} 条LLM记录")
    else:
        check("Agent输出可枚举", False, "任务未完成，跳过")
        check("Trace可查询", False, "任务未完成，跳过")

    # 5. 定时调度
    _banner("5/7", "定时调度器（注册表/运行记录）")
    jobs, _ = _request(args.base, "/api/v1/scheduler/jobs")
    runs, _ = _request(args.base, "/api/v1/scheduler/runs?limit=10")
    job_items = jobs.get("jobs", jobs if isinstance(jobs, list) else [])
    run_items = runs.get("runs", runs if isinstance(runs, list) else [])
    print(f"  注册任务 {len(job_items)} 个；最近运行记录 {len(run_items)} 条")
    check("调度任务注册表", len(job_items) >= 1)

    # 6. LLM运行指标
    _banner("6/7", "LLM运行指标（P50/P95/P99、缓存、降级、Provider分解）")
    metrics, _ = _request(args.base, "/api/v1/metrics?limit=1000")
    m = metrics["metrics"]
    print(f"  窗口调用={m['window_calls']} P95={m['latency_ms']['p95']}ms "
          f"缓存命中率={m['cache_hit_rate']} 降级率={m['fallback_rate']}")
    check("指标端点", m["window_calls"] >= 0)

    # 7. 回测（可选）
    if args.with_backtest:
        _banner("7/7", "纯本地规则回测（真实行情实时取数，无LLM/无未来函数）")
        try:
            bt, _ = _request(args.base, "/api/v1/backtest/run", {
                "indicator": "PPI", "code": "601088", "eps_pct": 1.0,
            }, timeout=180)
            s = bt["strategy"]
            print(f"  {bt['range']['start']}~{bt['range']['end']} "
                  f"共{bt['periods']}月 信号={bt['signals']}")
            print(f"  策略累计={s['cumulative_return']:.2%} "
                  f"买入持有={s['buy_and_hold']['cumulative_return']:.2%} "
                  f"超额={s['excess_cumulative_return']:.2%}")
            check("回测端点", bt["periods"] >= 8)
        except (urllib.error.URLError, TimeoutError) as exc:
            check("回测端点", False, str(exc))
    else:
        print("\n[7/7] 回测演示已跳过（加 --with-backtest 启用）")

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok in checks if ok)
    print(f"演示导览结果：{passed}/{len(checks)} PASS")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print(DISCLAIMER)
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
