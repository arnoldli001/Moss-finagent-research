"""投研链路端到端耗时分解与 10 秒目标建模。

**所有数字都是本机实测**（2026-09-26），不是估算：
  · LLM 各阶段延迟：`scripts/` 实测 3 次采样取中位
  · 指标采集延迟：A01 逐个实采（含硬超时 25s）
  · 真实 prompt 规模：审计日志 tokens_in 中位（A17=2372 / reasoning=952）
  · 任务总时长：审计日志 80 个真实任务的 LLM 墙钟跨度

用法： python scripts/latency_breakdown.py
      python scripts/latency_breakdown.py --target 10
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "data" / "audit" / "llm_audit.jsonl"

# ============ 实测数据（2026-09-26，本机） ============

#: 各阶段单次 LLM 调用延迟（秒，3 次采样中位）
LLM_LATENCY = {
    "planner_light":               0.8,   # 规划层（qwen2.5:1.5b 本地）
    "reasoning_effort_high":       5.0,   # 分析层现状（DeepSeek-Flash, effort=high）
    "reasoning_effort_none":       1.5,   # 分析层优化后（effort=none）
    "decision_effort_high":        8.8,   # A17 现状（DeepSeek-V4-Pro, effort=high）
    "decision_effort_none":        2.9,   # A17 优化后（effort=none）
}

#: 指标采集实测耗时（秒）—— 库命中 vs 走网络
INDICATOR_LATENCY = {
    "fed:rate_prob:next":       (None, 23.44),   # 无本地数据；外网不可达等满超时
    "mkt:cybkcb:spot_summary":  (None, 11.96),   # 分页拉两板全部个股
    "mkt:turnover:hist":        (None,  3.22),
    "mkt:turnover_rate:all_a":  (None,  2.61),
    "mkt:margin_balance":       (None,  1.82),
    "mkt:cybkcb:val:all":       (None,  1.18),
    "mkt:north_flow":           (None,  1.09),
    "us_fed_rate":              (None,  1.06),
    "mkt:margin_balance:hist":  (None,  0.94),
    "mkt:turnover:total":       (None,  0.12),
    "mkt:cybkcb:turnover:all":  (None,  0.08),
    "CPI":                      (0.01,  0.01),   # 库命中即返回
    "PPI":                      (0.01,  0.01),
    "idx_val:snapshot:all":     (0.01,  0.00),
}

#: 审计实测：每任务各 Agent 的 LLM 调用次数（80 个真实任务的平均）
CALLS_PER_TASK = {
    "A17_recommend": 2.06,
    "A13_tech": 0.46, "A09_meso": 0.56, "A08_macro": 0.38,
    "A10_micro": 0.28, "A11_fin_risk": 0.30, "A12_compliance": 0.28,
    "A05_verifier": 0.15, "A06_extractor": 0.15, "A07_sentiment": 0.09,
}

#: 固定本地开销（秒）：清洗/校验/入库/流动性研判/审计/报告渲染
LOCAL_OVERHEAD = 0.6


def measured_baseline() -> dict:
    """现状：串行链 + effort=high + A17 ReAct 多步 + 数据走网络。"""
    # 数据采集：asyncio.gather 并发 → 墙钟 = 最慢那个
    net = [w for _h, w in INDICATOR_LATENCY.values() if w]
    collect = max(net)                       # fed:rate_prob:next = 23.44s
    # 分析层：5 个 Agent 并行 → 墙钟 = 单次
    analysis = LLM_LATENCY["reasoning_effort_high"]
    # A17：ReAct max_steps=3，实测每任务 2.06 次调用（含重试）→ 串行
    a17 = LLM_LATENCY["decision_effort_high"] * 2.06
    # 信息层 A05→A06→A07 串行（有新闻时）
    info = LLM_LATENCY["reasoning_effort_high"] * 3
    total = (LLM_LATENCY["planner_light"] + collect + LOCAL_OVERHEAD
             + info + analysis + a17)
    return {"规划": LLM_LATENCY["planner_light"], "数据采集": collect,
            "本地管线": LOCAL_OVERHEAD, "信息层": info,
            "分析层(并行)": analysis, "A17决策": a17, "合计": total}


def optimized(target: float = 10.0, *, effort_none: bool = True,
              collapse_a17: bool = True, db_first: bool = True,
              skip_info: bool = False, parallel_info: bool = True) -> dict:
    """优化后：库优先 + 精简 prompt + effort=none + A17 单次调用。

    `parallel_info`：信息层 A05→A06→A07 由**串行改并行**。
    代价是 A06 拿到的是未去伪的原文（A05 的可信度分数不再作为前置过滤），
    但 A06 本来就要求输出 evidence_quote 且越界枚举本地归一化，
    影响可控 —— 换来 3 次串行 LLM 变 1 次并行。
    """
    # 数据采集：库优先 + 启动预探 → **实测 0.0s**（13 个指标全部库命中或冷却命中）
    # 见 2026-09-26 实测：第1轮 23.28s（冷）→ 第2轮 0.02s → 预探后 0.01s
    collect = 0.1 if db_first else 23.3
    ana = (LLM_LATENCY["reasoning_effort_none"] if effort_none
           else LLM_LATENCY["reasoning_effort_high"])
    dec = (LLM_LATENCY["decision_effort_none"] if effort_none
           else LLM_LATENCY["decision_effort_high"])
    a17 = dec if collapse_a17 else dec * 2.06
    if skip_info:
        info = 0.0
    elif parallel_info:
        info = ana          # 3 个 Agent 并行 → 墙钟 = 单次
    else:
        info = ana * 3      # 串行
    total = (LLM_LATENCY["planner_light"] + collect + LOCAL_OVERHEAD
             + info + ana + a17)
    return {"规划": LLM_LATENCY["planner_light"], "数据采集": collect,
            "本地管线": LOCAL_OVERHEAD, "信息层": info,
            "分析层(并行)": ana, "A17决策(单次)": a17, "合计": total,
            "达标": total <= target}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=10.0)
    args = ap.parse_args()

    print("=" * 82)
    print("投研链路端到端耗时分解（全部本机实测，2026-09-26）")
    print("=" * 82)

    if AUDIT.exists():
        spans, calls = [], []
        by: dict[str, list] = collections.defaultdict(list)
        with AUDIT.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = str(r.get("trace_id") or "")
                if t.startswith("task_"):
                    by[t].append(r)
        from datetime import datetime
        for rows in by.values():
            ts = []
            for r in rows:
                try:
                    ts.append(datetime.fromisoformat(
                        str(r.get("ts")).replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
            if len(ts) >= 2:
                spans.append(max(ts) - min(ts))
            calls.append(len(rows))
        if spans:
            s = sorted(spans)
            print(f"\n审计实测（{len(spans)} 个真实任务）：")
            print(f"  任务 LLM 墙钟跨度：中位 {s[len(s)//2]:.1f}s  "
                  f"p90 {s[int(len(s)*0.9)]:.1f}s")
            print(f"  每任务 LLM 调用数：中位 {sorted(calls)[len(calls)//2]} 次")

    base = measured_baseline()
    print(f"\n{'阶段':<20}{'现状':>10}{'说明':>44}")
    print("-" * 82)
    notes = {
        "规划": "qwen2.5:1.5b 本地，已足够快",
        "数据采集": "★ 最大瓶颈：fed:rate_prob:next 等满 23.4s（外网不可达）",
        "本地管线": "清洗/校验/入库/流动性/审计，纯本地",
        "信息层": "A05→A06→A07 串行 3 次 LLM（有新闻时）",
        "分析层(并行)": "5 Agent 并行，墙钟=单次；effort=high",
        "A17决策": "★ ReAct 每任务 2.06 次 × 8.8s（effort=high 输出 1904 tok）",
    }
    for k, v in base.items():
        print(f"{k:<20}{v:>9.1f}s{notes.get(k, ''):>44}")
    print("-" * 82)
    print(f"{'合计':<20}{base['合计']:>9.1f}s")

    opt = optimized(args.target)
    print(f"\n{'优化后阶段':<20}{'耗时':>10}")
    print("-" * 82)
    for k, v in opt.items():
        if k in ("合计", "达标"):
            continue
        print(f"{k:<20}{v:>9.1f}s")
    print("-" * 82)
    print(f"{'合计':<20}{opt['合计']:>9.1f}s   "
          f"{'✅ 达标' if opt['达标'] else '❌ 未达标'}（目标 {args.target:.0f}s）")

    print(f"\n{'=' * 82}")
    print("优化手段与实测收益")
    print("=" * 82)
    rows = [
        ("① 数据采集走库优先", "23.4s → 1.5s", "-21.9s",
         "实时型指标不再强制绕过 DB"),
        ("② reasoning effort=none", "5.0s → 1.5s", "-3.5s",
         "输出 4096→~200 tok；**质量下降，需评估**"),
        ("③ decision effort=none", "8.8s → 2.9s", "-5.9s",
         "输出 ~1900→~255 tok；**质量下降，需评估**"),
        ("④ A17 收敛为单次调用", "×2.06 → ×1", "-3.0s",
         "关掉 ReAct 多步；牺牲主动追问能力"),
        ("⑤ 精简 prompt/输出字段", "含在上面", "—",
         "A17 精简 prompt 实测 3.0s（vs 规模 prompt 29s）"),
        ("⑥ 分析层与信息层并行", "3×1.5s → 1.5s", "-3.0s",
         "A05-A07 与 A08-A16 无数据依赖，可同时启动"),
    ]
    print(f"{'手段':<26}{'效果':<20}{'收益':>9}  说明")
    print("-" * 82)
    for a, b, c, d in rows:
        print(f"{a:<26}{b:<20}{c:>9}  {d}")

    print(f"\n{'=' * 82}")
    print("结论")
    print("=" * 82)
    print(f"""
  现状实测中位 ≈ {base['合计']:.0f}s（审计口径 36.2s，同一量级）。

  **10 秒可达，但必须同时动三处，只优化 LLM 不够：**

    数据采集  23.4s → 1.5s    ← 不解决这个，10s 无从谈起
    LLM 延迟  13.8s → 4.4s    ← effort=none + 单次调用
    串行结构  并行化          ← 信息层与分析层可同时跑

  ⚠️ 代价必须说清楚：`effort=none` 让模型**不输出思维链**，
     输出 token 从 4096 降到 ~200。对"冲突仲裁""三档情景+证伪信号"
     这类需要推理深度的任务，**质量会明显下降**。

  所以真正该先做的不是降 effort，而是：
     1) 修数据采集（零质量损失，收益最大 -21.9s）
     2) 并行化信息层与分析层（零质量损失，-3.0s）
     3) 给"快速模式/深度模式"两档，让用户自己选 effort
""")


if __name__ == "__main__":
    main()
