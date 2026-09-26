"""投研链路并发容量模型（基于实测 LLM 审计日志）。

用真实数据回答两个问题：
  1. 闸门 ``MOSS_RESEARCH_MAX_INFLIGHT`` 该设多少？
  2. 这个设置下，最多能支撑多少"同时调用"的用户？

模型：M/M/c 的简化工程版
  - 服务时间 T = 单任务从提交到完成的墙钟时长（实测任务 LLM 跨度）
  - 并发度 c = 准入闸门值
  - 满负荷吞吐 = c / T
  - 排队等待 W ≈ (利用率^c / (c·(1-利用率))) · T   （Erlang-C 近似）

用法： python scripts/capacity_model.py
"""

from __future__ import annotations

import collections
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "data" / "audit" / "llm_audit.jsonl"

#: 单任务平均 LLM 调用数（无审计数据时的兜底，取自 plan_run 的 agents 数）
DEFAULT_CALLS = 4


def load_task_spans() -> tuple[list[float], list[int], list[int]]:
    """从审计日志还原每个投研任务的墙钟跨度、调用数、token 数。"""
    by: dict[str, list[dict]] = collections.defaultdict(list)
    if not AUDIT.exists():
        return [], [], []
    with AUDIT.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            trace = str(r.get("trace_id") or "")
            if trace.startswith("task_"):
                by[trace].append(r)

    spans: list[float] = []
    calls: list[int] = []
    toks: list[int] = []
    for rows in by.values():
        times = []
        for r in rows:
            s = str(r.get("ts") or "")
            try:
                from datetime import datetime

                times.append(
                    datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
            except ValueError:
                continue
        if len(times) >= 2:
            spans.append(max(times) - min(times))
        calls.append(len(rows))
        toks.append(sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
                        for r in rows))
    return spans, calls, toks


def erlang_c_wait(arrival_per_sec: float, service_sec: float, c: int) -> float:
    """Erlang-C 近似的平均排队等待（秒）。利用率≥1 时返回无穷大。"""
    if arrival_per_sec <= 0 or service_sec <= 0 or c <= 0:
        return 0.0
    rho = arrival_per_sec * service_sec / c
    if rho >= 1.0:
        return float("inf")
    # Erlang-C 公式
    a = arrival_per_sec * service_sec
    num = (a**c / _factorial(c)) * (1.0 / (1.0 - rho))
    den = sum(a**k / _factorial(k) for k in range(c)) + num
    p_wait = num / den if den else 0.0
    return p_wait * service_sec / (c * (1.0 - rho))


def _factorial(n: int) -> float:
    out = 1.0
    for i in range(2, n + 1):
        out *= i
    return out


def main() -> None:
    spans, calls, toks = load_task_spans()

    if spans:
        s = sorted(spans)
        T_med = statistics.median(s)
        T_p90 = s[int(len(s) * 0.90)]
        print("=" * 78)
        print("实测输入（来自 data/audit/llm_audit.jsonl）")
        print("=" * 78)
        print(f"  可测投研任务数        : {len(spans)}")
        print(f"  单任务墙钟时长 T      : 中位 {T_med:.1f}s   p75 {s[int(len(s)*0.75)]:.1f}s"
              f"   p90 {T_p90:.1f}s   max {max(s):.1f}s")
        print(f"  单任务 LLM 调用数     : 中位 {statistics.median(calls):.0f}  "
              f"p90 {sorted(calls)[int(len(calls)*0.9)]}  max {max(calls)}")
        print(f"  单任务 token          : 中位 {statistics.median(toks):,.0f}  "
              f"p90 {sorted(toks)[int(len(toks)*0.9)]:,.0f}  max {max(toks):,.0f}")
    else:
        T_med = T_p90 = 60.0
        print("!! 未找到审计数据，使用兜底 T=60s")

    avg_calls = statistics.median(calls) if calls else DEFAULT_CALLS

    print()
    print("=" * 78)
    print("容量模型：闸门值 → 吞吐上限与排队")
    print("=" * 78)
    print(f"{'闸门c':>5} | {'吞吐上限':>12} | {'饱和时到达率':>14} | "
          f"{'70%负载等待':>12} | {'每分钟LLM调用':>13}")
    print(f"{'':>5} | {'任务/分钟':>12} | {'任务/分钟':>14} | "
          f"{'秒':>12} | {'次':>13}")
    print("-" * 78)

    for c in (2, 3, 4, 6, 8, 12, 16):
        thr = c / T_med * 60
        # 70% 利用率下的到达率
        lam = 0.7 * c / T_med
        w = erlang_c_wait(lam, T_med, c)
        llm_rate = lam * avg_calls * 60
        w_txt = f"{w:12.1f}" if w != float("inf") else f"{'∞':>12}"
        print(f"{c:>5} | {thr:>12.1f} | {thr:>14.1f} | {w_txt} | {llm_rate:>13.1f}")

    print()
    print("=" * 78)
    print("建议（按「用户同时点击」换算）")
    print("=" * 78)
    print("  说明：一个用户点一次分析 = 一个任务。用户不会持续点，")
    print("        所以「同时 N 个用户在线」远小于「N 个任务在飞」。")
    print()
    for c in (4, 6, 8):
        thr = c / T_med * 60
        # 假设活跃用户平均每 15 分钟提交一次
        users = thr * 15
        print(f"  闸门={c:2d}: 支撑 {thr:5.1f} 任务/分钟 "
              f"→ 约 {users:5.0f} 个「每15分钟点一次」的活跃用户；"
              f"或 {c} 个真正同时提交的用户即时响应")
    print()
    print("  ⚠️ 真正的上限不是本机资源，而是 **DeepSeek API 的 RPM/并发配额**。")
    print("     上表最后一列给出每个闸门值对应的 LLM 调用速率，")
    print("     拿它对照你账号的配额即可定闸门值（宁小勿大：超限会触发全局熔断）。")


if __name__ == "__main__":
    sys.exit(main())
