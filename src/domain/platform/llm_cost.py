"""LLM 费用归属与聚合：**钱花在哪个功能 / 哪个租户 / 哪天**。

对应界面：管理员控制台「资源监控」（`GET /api/v1/admin/platform/monitor`
的 `llm_cost` 块）。数据来源是 **LLM 调用审计**（`data/audit/llm_audit.jsonl`）
—— 它是唯一逐次记录 `tokens_in/tokens_out/model/provider` 的地方，
因此也是唯一能算出"这一次调用花了多少钱"的地方。

## 归属是怎么定的（两段，**精确优先**）

    ① row["path"] 存在  → 按 **HTTP 路径**映射到前端功能（精确）
    ② 否则按 row["agent_id"] → 映射到功能，并标注来源（后台作业 / 命令行脚本）

`path` 字段是 2026-09-26 随 `AccountingMiddleware` 一起加的，所以：

- **新写入的行**：①命中，"钱花在前端哪个功能"是**事实**，不是推断；
- **历史行**（没有 `path`）：只能走 ②，按 agent 名字归类。这时
  "页面触发的调用"与"定时作业触发的调用"分不开 —— 同一个 agent 两边都会用。
  这一点**必须显示在界面上**（`basis` 里如实写），否则管理员会把一个
  估算当成账单。

## 为什么按"前端功能 key"而不是按 agent 名分组

功能 key（`research` / `mainline` / `intel.alerts` …）与「功能权限」「资源管控」
是同一套（`src/domain/platform/config.py` 的 `FEATURES`）。这样管理员看到的
"这个功能这个月烧了 12 元"与"这个等级开没开这个功能"能对上号；
按 agent 名分组则无法回答"该不该给这个等级开这一项"。

## 未登记价格的模型必须被看见

实测 309 次调用用的 `deepseek-v4-flash` 是 `deepseek-flash` 的**旧名**，
不在 `configs/models.yaml` 里。计价会退回兜底价（约 1 元量级），
但**"我们是在估算"这件事必须显示出来** —— 否则管理员会以为这是账单。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.core.budget import _FALLBACK_PRICE, call_cost_cny, model_prices
from src.domain.platform.config import FEATURES

#: 费用的功能分组：前端功能（与 `FEATURES` 同 key）之外的三个**非页面**桶。
#: 为什么单列：`mainline_relevance` 一个脚本就占了全站费用的 78%，
#: 把它塞进任何一个前端功能里都会让那个功能的数字变得没有意义。
EXTRA_FEATURES: dict[str, str] = {
    "ops.job": "后台定时作业（无页面入口）",
    "ops.script": "命令行/一次性脚本（无前端入口）",
    "sys.unknown": "未归属（无法判定来源）",
}

#: HTTP 路径前缀 → 功能 key。**长的在前**（前缀匹配按顺序取第一个命中）。
PATH_FEATURES: tuple[tuple[str, str], ...] = (
    ("/api/v1/research", "research"),
    ("/api/v1/code-engineer", "research"),
    ("/api/v1/mainline", "mainline"),
    ("/api/v1/intel/alerts", "intel.alerts"),
    ("/api/v1/alerts", "intel.alerts"),
    ("/api/v1/intel/brief", "intel.brief"),
    ("/api/v1/intel", "intel.radar"),
    ("/api/v1/intraday", "quant.intraday"),
    ("/api/v1/auction", "quant.auction"),
    ("/api/v1/quant-select", "quant.select"),
    ("/api/v1/quant", "quant.select"),
    ("/api/v1/backtest", "backtest"),
    ("/api/v1/fundflow", "fundflow"),
    ("/api/v1/sector-crowding", "mainline"),
    ("/api/v1/admin", "ops.admin"),
    ("/api/v1/scheduler", "ops.admin"),
)

#: `agent_id` → 功能 key（**只在没有 `path` 时使用**）。
#:
#: 命名规范上 `A**` 是研究图里的 Agent（A01–A18），其余是各模块自己的 id。
AGENT_FEATURES: dict[str, str] = {
    "A17_recommend": "research",
    "supervisor_planner": "research",
    "data_gap_resolver": "research",
    "intraday_news_sentiment": "quant.intraday",
    "intel_extract": "intel.radar",
    "intel_tone": "intel.radar",
    "intel_hot": "intel.radar",
    "tone_probe": "intel.radar",
    "alert_analyzer": "intel.alerts",
    "mainline_relevance": "ops.script",
    "mainline_member_pure": "ops.script",
    "validate_pure_prompt": "ops.script",
    "A19_code_engineer": "ops.script",
}

#: `agent_id` 前缀 → 功能 key（兜底，避免每加一个 Agent 就要改表）。
AGENT_PREFIX_FEATURES: tuple[tuple[str, str], ...] = (
    ("A0", "research"),          # A01–A09
    ("A1", "research"),          # A10–A19
    ("intel_", "intel.radar"),
    ("mainline_", "ops.script"),
    ("probe_", "ops.script"),
    ("probe", "ops.script"),
    ("diag", "ops.script"),
    ("dbg", "ops.script"),
    ("debug", "ops.script"),
    ("smoke", "ops.script"),
    ("t", "ops.script"),         # 临时脚本
    ("", "sys.unknown"),
)

#: 非页面的功能标签（`FEATURES` 里没有的）
NON_PAGE_FEATURES: dict[str, str] = {
    "ops.admin": "管理控制台（运维自身）",
    **EXTRA_FEATURES,
}


def feature_label(key: str) -> str:
    return FEATURES.get(key) or NON_PAGE_FEATURES.get(key) or key


def is_page_feature(key: str) -> bool:
    """该功能是不是**前端页面**（用于界面区分"可售卖功能"与"平台自身开销"）。"""
    return key in FEATURES


def feature_of(row: Mapping[str, Any]) -> tuple[str, str]:
    """一次调用属于哪个功能 → `(功能 key, 归属依据)`。

    依据取值：`page`（按请求路径，精确）/ `agent`（按 agent 名，推断）/
    `unknown`。
    """
    path = str(row.get("path") or "")
    if path:
        for prefix, key in PATH_FEATURES:
            if path.startswith(prefix):
                return key, "page"
        return "sys.unknown", "page"
    agent = str(row.get("agent_id") or "")
    if agent in AGENT_FEATURES:
        return AGENT_FEATURES[agent], "agent"
    for prefix, key in AGENT_PREFIX_FEATURES:
        if prefix and agent.startswith(prefix):
            return key, "agent"
    return "sys.unknown", "unknown"


def _day_of(row: Mapping[str, Any]) -> str:
    return str(row.get("ts") or "")[:10]


def aggregate_llm_cost(
    rows: Iterable[Mapping[str, Any]], *,
    days: int = 14,
    prices: dict[str, tuple[float, float, float]] | None = None,
) -> dict[str, Any]:
    """把 LLM 审计行聚合成运维页要的 `llm_cost` 块。

    `rows` 应当已经按时间窗过滤过（例如"本月"）。参数 `days` 只影响
    `by_day` 趋势保留多少天。

    返回的费用口径与 `CostBudget.record` **共用同一个 `call_cost_cny`**，
    所以"在线账本"与"事后统计"不会对不上。
    """
    table = model_prices() if prices is None else prices

    total = 0.0
    calls = 0
    paid_calls = 0
    free_calls = 0            # 本地模型（0 元）
    unknown_provider = 0      # 审计里没有 provider 字段（老行/测试造的行）
    unpriced_calls = 0
    unpriced_models: dict[str, int] = {}
    tokens_in = 0
    tokens_out = 0

    by_feature: dict[str, dict[str, Any]] = {}
    by_tenant: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}

    for row in rows:
        calls += 1
        provider = str(row.get("provider") or "")
        model = str(row.get("model") or "")
        tin = int(row.get("tokens_in") or 0)
        tout = int(row.get("tokens_out") or 0)
        tokens_in += tin
        tokens_out += tout

        if provider == "deepseek":
            cost = call_cost_cny(provider=provider, model=model,
                                 tokens_in=tin, tokens_out=tout,
                                 cache_hit=bool(row.get("cache_hit")),
                                 prices=table)
            paid_calls += 1
            if model not in table:
                unpriced_calls += 1
                unpriced_models[model or "(空)"] = \
                    unpriced_models.get(model or "(空)", 0) + 1
        elif provider:
            cost = 0.0        # ollama 等本地提供方
            free_calls += 1
        else:
            cost = 0.0        # 没有 provider 字段：不猜，如实计入 unknowns
            unknown_provider += 1

        total += cost

        key, basis = feature_of(row)
        f = by_feature.setdefault(key, {
            "key": key, "label": feature_label(key), "cny": 0.0, "calls": 0,
            "tokens": 0, "page_feature": is_page_feature(key),
            "basis": basis,
        })
        f["cny"] += cost
        f["calls"] += 1
        f["tokens"] += tin + tout
        # 依据混用时要如实说（同一功能可能既有页面调用也有作业调用）
        if f["basis"] != basis:
            f["basis"] = "mixed"

        tenant = str(row.get("tenant_id") or "") or "(未归属)"
        t = by_tenant.setdefault(tenant, {
            "tenant_id": tenant, "cny": 0.0, "calls": 0, "tokens": 0,
            "users": set(), "sources": set(),
        })
        t["cny"] += cost
        t["calls"] += 1
        t["tokens"] += tin + tout
        uid = str(row.get("user_id") or "")
        if uid:
            t["users"].add(uid)
        t["sources"].add(str(row.get("tenant_source") or "unknown"))

        day = _day_of(row)
        if day:
            d = by_day.setdefault(day, {"day": day, "cny": 0.0, "calls": 0})
            d["cny"] += cost
            d["calls"] += 1

    def rnd(value: float) -> float:
        return round(value, 4)

    features = []
    for item in sorted(by_feature.values(), key=lambda i: -i["cny"]):
        item["cny"] = rnd(item["cny"])
        item["share_pct"] = (round(item["cny"] / total * 100, 1)
                             if total > 0 else 0.0)
        features.append(item)

    tenants = []
    for item in sorted(by_tenant.values(), key=lambda i: -i["cny"]):
        tenants.append({
            "tenant_id": item["tenant_id"],
            "cny": rnd(item["cny"]),
            "calls": item["calls"],
            "tokens": item["tokens"],
            "users": len(item["users"]),
            "sources": sorted(s for s in item["sources"] if s),
            "share_pct": (round(item["cny"] / total * 100, 1)
                          if total > 0 else 0.0),
        })

    trend = []
    for item in sorted(by_day.values(), key=lambda i: i["day"])[-max(1, days):]:
        trend.append({"day": item["day"], "cny": rnd(item["cny"]),
                      "calls": item["calls"]})

    return {
        "total_cny": rnd(total),
        "calls": calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "paid_calls": paid_calls,
        "free_calls": free_calls,
        "unknown_provider_calls": unknown_provider,
        "unpriced_calls": unpriced_calls,
        "unpriced_models": sorted(unpriced_models.items(),
                                  key=lambda kv: -kv[1]),
        "prices_loaded": sorted(table),
        "fallback_price": {"input_cache_miss": _FALLBACK_PRICE[1],
                           "output": _FALLBACK_PRICE[2]},
        "by_feature": features,
        "by_tenant": tenants,
        "by_day": trend,
    }


def cost_basis_notes(cost: Mapping[str, Any]) -> list[str]:
    """把"这些钱是怎么算出来的、哪里不准"如实说清（界面直接渲染）。"""
    notes = [
        "费用由 **LLM 调用审计**逐次计价："
        "`tokens_in × 输入价 + tokens_out × 输出价`，单价取自 "
        "`configs/models.yaml`（与在线预算账本共用同一个计价函数，"
        "所以两处金额不会对不上）。",
        "**归属**：优先按调用发生时记录的**请求路径**映射到前端功能（精确）；"
        "没有路径的历史行按 Agent 名归类，那时'页面触发'与'定时作业触发'"
        "分不开 —— 该字段自 2026-09-26 起才会写入，越早的数据越依赖推断。",
        "本地模型（Ollama）计 0 元；`paid_calls` / `free_calls` 如实分开计数，"
        "不把免费调用算进金额。",
        "只统计审计文件的**尾部**（超出窗口时金额偏低），"
        "它是**用量视图**而不是财务账单；对账请以云厂商账单为准。",
    ]
    if cost.get("unpriced_calls"):
        models = "、".join(f"{m}×{n}" for m, n in cost["unpriced_models"])
        notes.append(
            f"⚠️ 有 {cost['unpriced_calls']} 次调用使用的模型名**没有登记价格**"
            f"（{models}），已按兜底价（输入 "
            f"{cost['fallback_price']['input_cache_miss']} 元/百万、输出 "
            f"{cost['fallback_price']['output']} 元/百万）**估算** —— "
            f"要么把该模型名加进 `configs/models.yaml`，要么改掉调用方的模型名"
            f"（`deepseek-v4-flash` 是 `deepseek-flash` 的旧名）。")
    if cost.get("unknown_provider_calls"):
        notes.append(
            f"有 {cost['unknown_provider_calls']} 条记录没有 `provider` 字段"
            f"（早期格式），金额按 0 计 —— 它们的费用未计入本页数字。")
    return notes


__all__ = [
    "AGENT_FEATURES",
    "AGENT_PREFIX_FEATURES",
    "EXTRA_FEATURES",
    "NON_PAGE_FEATURES",
    "PATH_FEATURES",
    "aggregate_llm_cost",
    "cost_basis_notes",
    "feature_label",
    "feature_of",
    "is_page_feature",
]
