"""投研链路成本核算与日预算容量（价格取自 configs/models.yaml）。

回答：在给定的日预算（如 DeepSeek 每天 20 元）下，
     最多能做多少次投研分析？闸门该设多少？

价格口径（元/百万 token，与 configs/models.yaml 的 cost 字段一致）：
  deepseek-flash : 输入命中 0.02 / 输入未命中 1.0 / 输出 4.0
  deepseek-v4-pro: 输入命中 0.5  / 输入未命中 4.5 / 输出 13.5
  ollama 本地     : 0（只耗电）

用法：
    python scripts/cost_model.py                 # 用审计日志实算
    python scripts/cost_model.py --budget 20     # 指定日预算（元）
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "data" / "audit" / "llm_audit.jsonl"

#: 元/百万 token：(输入命中, 输入未命中, 输出)
PRICES: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": (0.02, 1.0, 4.0),
    "deepseek-v4-pro": (0.5, 4.5, 13.5),
}


def call_cost(row: dict) -> float:
    """单次 LLM 调用的成本（元）。本地模型为 0。"""
    if str(row.get("provider") or "") != "deepseek":
        return 0.0
    model = str(row.get("model") or "")
    hit_rate, miss_rate, out_rate = PRICES.get(model, PRICES["deepseek-flash"])
    tin = int(row.get("tokens_in") or 0)
    tout = int(row.get("tokens_out") or 0)
    # 命中与否决定输入单价；审计里的 cache_hit 即"本次响应来自缓存"。
    # 注意：缓存命中时 tokens_in 仍会被记录，但服务端未真正计费 ——
    # gateway 对命中调用不累计 token 预算，这里同样按命中价计（保守抬高）。
    in_rate = hit_rate if row.get("cache_hit") else miss_rate
    return (tin * in_rate + tout * out_rate) / 1_000_000.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=20.0, help="日预算（元）")
    ap.add_argument("--audit", default=str(AUDIT))
    args = ap.parse_args()

    path = Path(args.audit)
    if not path.exists():
        print(f"审计日志不存在：{path}")
        return

    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # 按 trace 聚合投研任务（trace_id 以 task_ 开头）
    tasks: dict[str, list[dict]] = collections.defaultdict(list)
    other_cost = 0.0
    for r in rows:
        tid = str(r.get("trace_id") or "")
        if tid.startswith("task_"):
            tasks[tid].append(r)
        else:
            other_cost += call_cost(r)

    costs = [sum(call_cost(r) for r in v) for v in tasks.values() if v]
    toks = [sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
                for r in v) for v in tasks.values() if v]

    print("=" * 76)
    print(f"投研链路成本核算（价格取自 configs/models.yaml）")
    print("=" * 76)
    print("  单价（元/百万token）:")
    for m, (h, mi, o) in PRICES.items():
        print(f"    {m:<18} 输入命中 {h:<6} 未命中 {mi:<6} 输出 {o}")
    print()

    if not costs:
        print("  审计日志里没有 task_ 前缀的投研任务记录")
        return

    cs = sorted(costs)
    ts = sorted(toks)
    print(f"  可测投研任务数      : {len(cs)}")
    print(f"  单次分析成本（元）  : 中位 {statistics.median(cs):.4f}  "
          f"p75 {cs[int(len(cs)*0.75)]:.4f}  "
          f"p90 {cs[int(len(cs)*0.90)]:.4f}  max {max(cs):.4f}")
    print(f"  单次分析 token      : 中位 {statistics.median(ts):,.0f}  "
          f"p90 {ts[int(len(ts)*0.90)]:,.0f}  max {max(ts):,.0f}")
    total = sum(cs)
    print(f"  样本累计成本        : {total:.2f} 元（{len(cs)} 次分析）")
    print(f"  非投研调用成本      : {other_cost:.2f} 元")

    med = statistics.median(cs)
    p90 = cs[int(len(cs) * 0.90)]

    print()
    print("=" * 76)
    print(f"日预算 {args.budget:.0f} 元下的容量")
    print("=" * 76)
    print(f"{'单次成本口径':<16} {'单次(元)':>10} {'每日可做次数':>14} "
          f"{'折算任务/分钟':>16} {'等效闸门':>10}")
    print("-" * 76)
    for label, c in (("中位", med), ("p90（保守）", p90)):
        n = args.budget / c if c > 0 else float("inf")
        # 若一天有效服务 10 小时（36000s），均匀摊开
        per_min = n / (10 * 60)
        # 闸门：使吞吐恰好等于日预算允许的速率 => c_gate = per_min * T
        T = 36.2  # 实测单任务中位时长（秒）
        gate = per_min / 60 * T
        print(f"{label:<16} {c:>10.4f} {n:>14.0f} {per_min:>16.2f} "
              f"{gate:>10.1f}")
    print()
    print("  解读：")
    print(f"    · 按中位成本，20 元/天 约支持 {args.budget/med:.0f} 次分析")
    print(f"    · 这是**成本天花板**，比并发天花板（6.6 任务/分钟 = 3960/天）")
    print(f"      低约 {3960/(args.budget/med):.0f} 倍 —— 成本才是真正的约束")
    print()
    print("  ⚠️ 前提：reasoning 层主要走 deepseek-flash。")
    print("     若 decision 层大量落到 deepseek-v4-pro（输出 13.5 元/百万，")
    print("     是 flash 的 3.4 倍），单次成本会显著上升。")
    print("     可用 --budget 试算不同预算。")


if __name__ == "__main__":
    main()
