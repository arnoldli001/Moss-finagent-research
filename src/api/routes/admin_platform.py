"""管理员：套餐资源管控 / 功能权限 / 资源监控。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.10（管理员控制台）、
§4.6（tiers 表）、§9（可观测性）。

## 三个关注点的分工

| 端点组 | 回答的问题 |
|---|---|
| `/platform/tiers` | **每个等级给多少资源、开哪些功能、各卖多少钱** |
| `/platform/monitor` | **实际用掉了多少**（按租户聚合调用频次/延迟/异常） |
| `/me/features`（在 `my_pools` 之外单独给） | **我当前这个等级能看到哪些页面** |

## 资源监控的数据源：**已有的访问审计**，不新建埋点

`src/api/tenancy_middleware.py` 的 `TenantAuditLog` 已经把每次请求的
`tenant_id / user_id / path / status / latency_ms` 写进
`data/audit/access_audit.jsonl`（含哈希链）。所以资源监控只需要**读并聚合**它 ——
新建一套埋点会造成"审计一套、监控另一套"的口径分歧，
而两者不一致时没人知道该信哪个。

## 为什么"异常次数"按 status 而不是看日志里有没有 traceback

`status >= 500` 才是**服务端异常**（4xx 是客户端问题，不是异常）。
把它们混在一起会让"异常次数"失去意义：一次恶意扫描就能刷出几千个 4xx。
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.routes.admin import require_admin
from src.core.errors import brief
from src.domain.platform.config import (
    FEATURES,
    RESOURCE_FIELDS,
    TIER_ORDER,
    TierConfigError,
    describe_features,
    describe_resources,
    get_platform_config,
    plan_to_json,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/platform", tags=["admin-platform"])

#: 审计日志默认位置（与 `tenancy_middleware._default_audit_dir` 同一约定）
DEFAULT_AUDIT_DIR = "data/audit"
AUDIT_FILE = "access_audit.jsonl"
#: 最多读多少行（防止日志涨到几百 MB 时把内存吃光）
MAX_AUDIT_LINES = 200_000


class TierPatchBody(BaseModel):
    """改一个等级的套餐。**所有字段可选**，只改传了的。

    ⚠️ `monthly_price`（套餐月费）已下线（用户口径 2026-09-23：定价不由本系统
    维护）。这里**刻意保留一个被忽略的字段**而不是删掉签名：
    浏览器里可能还缓存着旧版前端包，它会继续发这个键 ——
    如果模型不认识它，FastAPI 只会忽略多余键（不报错），但显式接住
    才能在 `save_plan` 里打出一条"还有旧前端在发它"的日志。
    真正要做的清理是让用户硬刷新。
    """

    label: str | None = None
    sellable: bool | None = None
    #: 已下线：接收但忽略，仅用于记录"旧前端还在发"。见类文档。
    monthly_price: float | None = None
    note: str | None = None
    #: `{资源键: 数值}`
    resources: dict[str, int] = Field(default_factory=dict)
    #: `{功能键: 是否开启}`
    features: dict[str, bool] = Field(default_factory=dict)
    #: `{功能键: 单价}`
    pricing: dict[str, float] = Field(default_factory=dict)


# ======================================================================
# 套餐配置
# ======================================================================

@router.get("/tiers")
async def list_tiers(request: Request) -> dict:
    """全部等级的套餐（资源上限 + 功能权限 + 定价）。

    同时返回**可配置项的清单与中文名** —— 前端据此渲染表单，
    不再自己硬编码一份字段名（那样后端加字段时前端会静默漏掉）。
    """
    await require_admin(request)
    store = get_platform_config()
    plans = store.plans()
    return {
        "tiers": [plan_to_json(plans[k]) for k in TIER_ORDER if k in plans],
        "features": describe_features(),
        "resources": describe_resources(),
        "config_path": str(store.path),
    }


@router.put("/tiers/{tier}")
async def update_tier(tier: str, body: TierPatchBody,
                      request: Request) -> dict:
    """改一个等级的资源上限 / 功能权限 / 定价。

    ## 校验在写入前**整体完成**

    套餐是全局生效的：写坏一个字段，该等级所有用户立刻受影响。
    所以 `save_plan` 先构造完整配置、逐项校验，全通过才落盘
    （文件用"临时文件 + 原子替换"，避免写一半被杀导致全员降级）。
    """
    admin_id = await require_admin(request)
    store = get_platform_config()
    try:
        plan = store.save_plan(tier, body.model_dump(exclude_none=True))
    except TierConfigError as exc:
        # 400 而不是 500：这是**用户输入问题**，前端应把原因显示给管理员
        raise HTTPException(status_code=400, detail=brief(exc)) from exc
    logger.warning("管理员改套餐：admin=%s tier=%s", admin_id, tier)
    return {"ok": True, "message": f"已更新「{plan.label}」套餐",
            "tier": plan_to_json(plan)}


@router.get("/permissions-matrix")
async def permissions_matrix(request: Request) -> dict:
    """**权限总览矩阵**（功能 × 等级），供管理员一眼看清差异。

    单独给一个端点是刻意的：这个矩阵是"权限方案"的评审视图 ——
    改价/改权限之后要能立刻核对"哪个等级多了什么"，
    让前端自己把 `/tiers` 拼成矩阵等于把口径分散到前端。
    """
    await require_admin(request)
    plans = get_platform_config().plans()
    rows = []
    for fkey, flabel in FEATURES.items():
        rows.append({
            "feature": fkey,
            "label": flabel,
            "tiers": {
                t: {
                    "enabled": bool(plans[t].features.get(fkey, False)),
                    "price": float(plans[t].pricing.get(fkey, 0.0)),
                }
                for t in TIER_ORDER if t in plans
            },
        })
    return {"rows": rows, "tiers": [
        {"key": t, "label": plans[t].label, "sellable": plans[t].sellable}
        for t in TIER_ORDER if t in plans]}


# ======================================================================
# 资源监控
# ======================================================================

def _audit_dir() -> Path:
    import os

    raw = os.environ.get("MOSS_AUDIT_DIR") or DEFAULT_AUDIT_DIR
    p = Path(raw)
    if not p.is_absolute():
        # 相对路径按项目根（服务可能不在仓库根启动）
        p = Path(__file__).resolve().parents[3] / p
    return p


def _read_audit(limit: int = MAX_AUDIT_LINES) -> tuple[list[dict[str, Any]], int]:
    """读访问审计（**只读尾部** limit 行）→ `(rows, 非空行总数)`。

    日志会随运行时间增长；一次性 `read_text()` 在几百万行时会把内存打满。
    这里用 `deque(maxlen=...)` 流式读取，天然只保留最后 N 行。

    为什么还要把"总行数"一并返回：配额累计（今日调用数）就是从这份尾部
    数据算的。文件超过 `limit` 行时，更早的行不在内存里，累计值会**偏低** ——
    调用方必须能把这件事如实标注出来，而不是把偏小的用量当成准确账单
    展示给管理员（本项目对"显示假绿/假准"的一贯态度是拒绝）。
    """
    from collections import deque

    path = _audit_dir() / AUDIT_FILE
    if not path.exists():
        return [], 0
    tail: deque[str] = deque(maxlen=max(1, limit))
    total = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    total += 1
                    tail.append(line)
    except OSError as exc:
        logger.warning("审计日志读取失败（%s）：%s", path, exc)
        return [], 0
    out: list[dict[str, Any]] = []
    for line in tail:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue          # 单行损坏只跳过那一行，不能让整页监控失败
    return out, total


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return float(ordered[idx])


def _period_starts() -> tuple[str, str, str]:
    """返回 `(今日零点[UTC ISO], 本月零点[UTC ISO], 本月零点[本地无时区])`。

    为什么要三个：两份审计的时间口径**本来就不同**，硬凑成一个会算错。

    | 文件 | `at`/`ts` 口径 | 用途 |
    |---|---|---|
    | `access_audit.jsonl` | **UTC 带时区**（`datetime.now(timezone.utc)`） | 今日调用数 |
    | `llm_audit.jsonl` | **本地无时区**（`datetime.now()`） | 本月 token |

    "今日/本月"是**业务日**，按本机时区划（一台机器一套时区，且与用户
    看到的"今天"一致）。所以：
      · 访问审计要把本地零点换算成 UTC 再比字符串；
      · LLM 审计直接用本地零点，且只比到秒（`[:19]`）——
        带时区与不带时区的 ISO 串直接比字典序是没有意义的。
    """
    from datetime import datetime, timezone

    local_now = datetime.now().astimezone()
    day_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_local = day_local.replace(day=1)
    return (day_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            month_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            month_local.replace(tzinfo=None).isoformat(timespec="seconds"))


def _usage_block(*, calls_today: int, day_limit: int,
                 tokens_month: int, month_limit: int,
                 tokens_measured: bool) -> dict[str, Any]:
    """把"用掉多少 / 上限多少 / 还剩多少"算成一个可直接渲染的块。

    `remaining` 用 `max(0, ...)` 夹住：**额度用完是 0，不是负数**。
    负数会让界面显示"-3200"，看起来像账算错了。

    `tokens_measured=False` 时（本月还没有带租户归属的 LLM 调用记录，
    或审计文件不存在）**不能显示成"用掉 0、剩余 100%"** ——
    那是在说"你一点没用"，而事实是"我们还没量到"。两者对管理员的
    决策含义完全相反，所以这里如实标注。
    """
    def pct(used: int, limit: int) -> float | None:
        if limit <= 0:
            return None
        return round(min(100.0, used / limit * 100), 1)

    return {
        "calls_today": calls_today,
        "calls_today_limit": day_limit,
        "calls_today_remaining": max(0, day_limit - calls_today) if day_limit else 0,
        "calls_today_used_pct": pct(calls_today, day_limit),
        "tokens_month": tokens_month,
        "tokens_month_limit": month_limit,
        "tokens_month_remaining": (max(0, month_limit - tokens_month)
                                   if month_limit else 0),
        # ★ 未量到时**不给使用率**（哪怕上限存在）。给 0% 会被读成
        #   "这个月一点没用"，而事实是"我们还没量到" —— 两者对
        #   "要不要加额度"的结论正好相反。
        "tokens_month_used_pct": (pct(tokens_month, month_limit)
                                  if tokens_measured else None),
        "tokens_measured": tokens_measured,
    }


def _llm_usage(*, since: str) -> tuple[dict[str, int], dict[str, Any], str]:
    """读一次 LLM 审计 → `(按租户 token, 费用块, 说明)`。

    为什么是**一次**读取：运维页同时要"本月 token 用量"与"本月花了多少钱"，
    两者都在同一份审计里。分两次读会把"打开监控页"变成两次几十 MB 的
    文件扫描（而这一页还是 10 秒自动刷新），并且可能出现"token 是一个时间点的、
    金额是另一个时间点的"这种自相矛盾的画面。

    token 的**唯一**逐次来源就是这份审计（`tokens_in`/`tokens_out`），
    访问审计里根本没有 token 字段（早期版本曾按访问审计的 `llm_tokens`
    字段取值，那个字段从来没人写入，结果是"永远显示用掉 0"，属于典型的假数据）。
    """
    from src.core.config import get_settings
    from src.domain.platform.llm_cost import aggregate_llm_cost
    from src.infrastructure.llm.audit import LLMAuditLog

    log = LLMAuditLog(get_settings().llm_audit_dir)
    path = log.path
    if not path.exists():
        note = f"尚未产生 LLM 调用审计（{path}），本月 token 与费用均不可读"
        return {}, _empty_cost_block(), note
    rows, truncated = log.window_rows(since=since)
    usage: dict[str, int] = {}
    for row in rows:
        tenant = str(row.get("tenant_id") or "") or "(未归属)"
        usage[tenant] = (usage.get(tenant, 0)
                         + int(row.get("tokens_in") or 0)
                         + int(row.get("tokens_out") or 0))
    cost = aggregate_llm_cost(rows)
    cost["readable"] = True
    note = ""
    if truncated:
        note = ("LLM 审计超过统计窗口（只统计尾部若干行），本月 token 与费用累计"
                "**偏低**；它不是计费账单")
    return usage, cost, note


def _empty_cost_block() -> dict[str, Any]:
    """审计不可读时的空费用块。

    ⚠️ **不能省掉这个块**：前端拿不到 `llm_cost` 会把它显示成"0 元"，
    而"这个月一分钱没花"和"我们读不到审计"对管理员的含义完全相反
    （前者可以放心，后者说明监控没接上）。
    """
    return {
        "total_cny": 0.0, "calls": 0, "tokens_in": 0, "tokens_out": 0,
        "paid_calls": 0, "free_calls": 0, "unknown_provider_calls": 0,
        "unpriced_calls": 0, "unpriced_models": [], "prices_loaded": [],
        "fallback_price": {"input_cache_miss": 0.0, "output": 0.0},
        "by_feature": [], "by_tenant": [], "by_day": [],
        "readable": False,
    }


@router.get("/monitor")
async def monitor(request: Request, minutes: int = 60,
                  top_paths: int = 8) -> dict:
    """**资源监控**：按租户聚合调用频次 / 量级 / 延迟 / 异常次数。

    `minutes` 只看最近这段时间（默认 60 分钟）；传 0 表示全部。

    返回里 `by_tenant` 是主视图，`by_path` 是"哪个接口最慢/最容易错"，
    两者都是管理员定位问题的入口：
      - 慢/错集中在某个租户 → 该租户的用量或行为问题；
      - 慢/错集中在某个接口 → 是平台侧的 bug 或数据源问题。

    另外每个租户带一块 `usage`：**今日调用已用/上限/剩余**与
    **本月 token 已用/上限/剩余**（套餐里的 `api_calls_per_day`、
    `llm_tokens_per_month`）。口径与局限写在 `quota_basis` 里，
    不靠界面猜。

    还有一块 `llm_cost`：**本月花了多少钱、分别来自哪些前端功能、
    哪个租户花的**（汇总 + 按功能 + 按租户 + 按天趋势）。它与
    `usage.tokens_month` 同源（同一份 LLM 审计、同一次读取），
    金额口径与在线预算账本共用 `src/core/budget.py::call_cost_cny`。
    """
    await require_admin(request)
    from datetime import datetime, timedelta, timezone

    runtime = getattr(getattr(request.app.state, "runtime", None),
                      "intraday", None)
    data_sources_note = ("" if runtime is not None else
                         "做T模块尚未装配，数据源健康不可读（不显示假绿）")
    rows, audit_lines = _read_audit()
    audit_truncated = audit_lines > MAX_AUDIT_LINES
    day_start_utc, month_start_utc, month_start_local = _period_starts()
    token_usage, cost_block, tokens_note = _llm_usage(
        since=month_start_local)
    cost_measured = bool(cost_block.get("calls"))
    # ⚠️ `readable` **只能**由 `_llm_usage` 决定（读不到审计时它是 False）。
    #   在这里无条件置 True 会把"读不到审计"显示成"花了 0 元" ——
    #   那正是这一页最不该犯的错（假绿比不显示更糟）。
    cost_block["window_label"] = "本月（自然月，本机时区）"
    cost_block["since"] = month_start_local
    # 今日费用：从同一批行聚合出来的 `by_day` 里取今天那条
    # （**不**再读一遍文件）。
    today_local = datetime.now().strftime("%Y-%m-%d")
    cost_block["today_cny"] = round(sum(
        float(d.get("cny") or 0.0) for d in cost_block.get("by_day", [])
        if str(d.get("day")) == today_local), 4)
    cost_block["calls_today"] = int(sum(
        int(d.get("calls") or 0) for d in cost_block.get("by_day", [])
        if str(d.get("day")) == today_local))
    from src.domain.platform.llm_cost import cost_basis_notes

    cost_block["basis_notes"] = cost_basis_notes(cost_block)
    # ★ "量到了"的判据是**文件里有本月记录**，不是"用量>0"：
    #   本月确实调用了但 tokens 恰好为 0（全命中缓存）与"根本没量到"
    #   是两件事，前者剩余额度是真的、后者是未知。
    tokens_measured = bool(token_usage)
    # 费用按租户的索引（并进 `by_tenant` 的每一行）
    cost_by_tenant = {str(t["tenant_id"]): t
                      for t in cost_block.get("by_tenant", [])}
    cutoff = None
    if minutes > 0:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=minutes)).isoformat(timespec="seconds")

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "latencies": [], "paths": {},
                 "users": set(), "llm_tokens": 0})
    path_buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "latencies": []})
    total = 0

    for row in rows:
        at = str(row.get("at") or "")
        if cutoff and at and at < cutoff:
            continue
        total += 1
        tenant = str(row.get("tenant_id") or "") or "(未登录)"
        status = int(row.get("status") or 0)
        latency = float(row.get("latency_ms") or 0)
        path = str(row.get("path") or "")

        b = buckets[tenant]
        b["calls"] += 1
        b["latencies"].append(latency)
        if status >= 500:
            b["errors"] += 1
        b["paths"][path] = b["paths"].get(path, 0) + 1
        uid = str(row.get("user_id") or "")
        if uid:
            b["users"].add(uid)
        b["llm_tokens"] += int(row.get("llm_tokens") or 0)

        pb = path_buckets[path]
        pb["calls"] += 1
        pb["latencies"].append(latency)
        if status >= 500:
            pb["errors"] += 1

    # ★ 今日调用必须在**窗口之外单独统计一遍**。
    #
    # 第一版把它写在上面那个循环里，于是"3 小时前但仍是今天"的调用
    # 在 `minutes=60` 时被 `continue` 跳过了 —— 日额度因此变成"窗口内调用数"，
    # 把"今天已用掉 30%"显示成"用掉 0%"。**这是本文件自己的回归测试抓到的**
    # （`test_monitor_quota_is_daily_not_window_scoped`）：日额度与查看窗口
    # 是两个独立的口径，额度不能随"你选了几分钟"而变。
    calls_today_by_tenant: dict[str, int] = defaultdict(int)
    for row in rows:
        at = str(row.get("at") or "")
        if not at or at < day_start_utc:
            continue
        calls_today_by_tenant[
            str(row.get("tenant_id") or "") or "(未登录)"] += 1

    store = get_platform_config()
    plans = store.plans()

    # 租户集合取**窗口内**、**今日**、以及**本月有 LLM 费用**的并集：
    #   · 只看窗口 → "今天用了一整天、最近一小时却没人用"会整行消失，
    #     而那恰恰是最该看额度的时候（客户今天已经把额度烧掉大半）；
    #   · 不看费用来源 → 某个租户可能只有 LLM 花销、没有访问审计行
    #     （例如账号被停用后旧会话仍在跑后台任务），它的钱就**凭空消失**了。
    empty_bucket: dict[str, Any] = {
        "calls": 0, "errors": 0, "latencies": [], "paths": {},
        "users": set(), "llm_tokens": 0}
    all_tenants = set(buckets) | set(calls_today_by_tenant) | set(cost_by_tenant)

    by_tenant = []
    for tenant in sorted(all_tenants,
                         key=lambda t: -(buckets.get(t) or empty_bucket)["calls"]):
        b = buckets.get(tenant) or empty_bucket
        lat = b["latencies"]
        plan = plans.get(tenant)
        day_limit = (plan.resources.get("api_calls_per_day", 0) if plan else 0)
        month_limit = (plan.resources.get("llm_tokens_per_month", 0)
                       if plan else 0)
        # 没有套餐的租户要**说清是什么**，不能让管理员对着一个叫
        # "public" 或 "(未登录)" 的行猜它是哪个等级。
        if plan is not None:
            label = plan.label
        elif tenant == "(未登录)":
            label = "(未登录/匿名)"
        else:
            label = f"{tenant}（非套餐租户）"
        by_tenant.append({
            "tenant_id": tenant,
            "label": label,
            "known_tier": plan is not None,
            "calls": b["calls"],
            "errors": b["errors"],
            "error_rate": round(b["errors"] / b["calls"], 4) if b["calls"] else 0.0,
            "users": len(b["users"]),
            "latency_ms": {
                "avg": round(statistics.fmean(lat), 1) if lat else 0.0,
                "p50": round(_percentile(lat, 50), 1),
                "p95": round(_percentile(lat, 95), 1),
                "max": round(max(lat), 1) if lat else 0.0,
            },
            "top_paths": sorted(b["paths"].items(),
                                key=lambda kv: -kv[1])[:5],
            # 与套餐上限并列显示 —— 监控页的价值在于"用掉了几成"
            "limits": {
                "api_calls_per_minute": (
                    plan.resources.get("api_calls_per_minute", 0) if plan else 0),
                "api_calls_per_day": day_limit,
                "llm_tokens_per_month": month_limit,
            },
            "llm_tokens": b["llm_tokens"],
            # 本月 LLM 费用（元）与它的明细来源。与 `usage.tokens_month`
            # **同一份审计、同一次读取**，所以"token 说用了 100 万、
            # 费用却是 0"这种自相矛盾的画面不会出现。
            "llm_cost": {
                "cny": float((cost_by_tenant.get(tenant) or {}).get("cny", 0.0)),
                "calls": int((cost_by_tenant.get(tenant) or {}).get("calls", 0)),
                "tokens": int((cost_by_tenant.get(tenant) or {}).get("tokens", 0)),
                "share_pct": float((cost_by_tenant.get(tenant) or {}).get(
                    "share_pct", 0.0)),
                "measured": cost_measured,
            },
            # "还剩多少额度"（目标口径：展示剩余配额）
            "usage": _usage_block(
                calls_today=calls_today_by_tenant.get(tenant, 0),
                day_limit=day_limit,
                tokens_month=token_usage.get(tenant, 0),
                month_limit=month_limit,
                tokens_measured=tokens_measured),
        })

    by_path = []
    for path, b in sorted(path_buckets.items(), key=lambda kv: -kv[1]["calls"])[:max(1, top_paths)]:
        lat = b["latencies"]
        by_path.append({
            "path": path,
            "calls": b["calls"],
            "errors": b["errors"],
            "avg_ms": round(statistics.fmean(lat), 1) if lat else 0.0,
            "p95_ms": round(_percentile(lat, 95), 1),
        })

    all_lat = [float(r.get("latency_ms") or 0) for r in rows
               if (not cutoff or str(r.get("at") or "") >= cutoff)]
    return {
        "window_minutes": minutes,
        "sampled_calls": total,
        "audit_file": str(_audit_dir() / AUDIT_FILE),
        "overall": {
            "calls": total,
            "errors": sum(b["errors"] for b in buckets.values()),
            "avg_ms": round(statistics.fmean(all_lat), 1) if all_lat else 0.0,
            "p95_ms": round(_percentile(all_lat, 95), 1),
        },
        "by_tenant": by_tenant,
        "by_path": by_path,
        # ── LLM 费用：汇总 + 按功能 + 按租户 + 按天（管理员"钱花在哪"的主视图）──
        "llm_cost": cost_block,
        # ── 配额口径的三条如实说明（界面直接渲染，不让人猜数字怎么来的）──
        "quota_basis": {
            "day_start": day_start_utc,
            "month_start": month_start_utc,
            "audit_file": str(_audit_dir() / AUDIT_FILE),
            "audit_lines": audit_lines,
            "audit_truncated": audit_truncated,
            "tokens_truncated": bool(tokens_note),
            "notes": [
                "今日调用数由**访问审计**累计（按本机时区的自然日切分，"
                "与套餐里的 api_calls_per_day 同日口径）。",
                "本月 token 与**本月费用**都由**LLM 调用审计**累计"
                "（它是唯一逐次记录 tokens_in/tokens_out/model 的地方；"
                "租户归属优先取**会话身份**（`AccountingMiddleware`），"
                "无身份的调用（后台任务/脚本）计入 (未归属)）。",
                "两者都只统计审计文件的**尾部**，文件超出窗口时累计偏低，"
                "**不是计费账单** —— 用于回答「还剩多少额度」与「钱花在哪」。",
            ] + ([tokens_note] if tokens_note else []) + ([
                "访问审计超过统计窗口，今日调用数偏低。"] if audit_truncated else []),
        },
        "data_sources": _data_source_health(getattr(
            getattr(request.app.state, "runtime", None), "intraday", None)),
        "data_sources_note": data_sources_note,
    }


def _data_source_health(runtime: Any = None) -> list[dict[str, Any]]:
    """数据源健康 + 可用性（"各端口/数据源的剩余资源量"）。

    ## 为什么复用 `data_health` 而不是自己再探一遍

    两处各探一次必然出现"监控页说健康、健康页说不可用"的矛盾，
    而管理员没法判断该信哪个。所以这里读同一份结论。

    ## 为什么必须传 runtime

    `build_data_health(runtime)` 的签名要求它（它要拿数据库连接与
    连接器路由）。拿不到时**如实返回空**并附说明，而不是伪造"全部正常" ——
    监控页显示假绿比不显示更危险。
    """
    if runtime is None:
        return []
    try:
        from src.api.data_health import build_data_health

        health = build_data_health(runtime)
    except Exception as exc:  # noqa: BLE001 监控页不能因为探测失败整页报错
        logger.info("数据源健康读取失败：%s", exc)
        return []
    out: list[dict[str, Any]] = []
    if isinstance(health, dict):
        for key, value in health.items():
            out.append({"name": str(key), "kind": "datasource",
                        "available": bool(value) if isinstance(value, bool)
                        else bool(getattr(value, "available", False)),
                        "detail": brief(value, 160)
                        if not isinstance(value, bool) else ""})
        return out
    for attr, kind in (("databases", "database"), ("sources", "datasource")):
        for item in getattr(health, attr, []) or []:
            out.append({
                "name": str(getattr(item, "name", "")),
                "kind": kind,
                "available": bool(getattr(item, "available", False)),
                "detail": brief(getattr(item, "description", "")
                                or getattr(item, "detail", "") or "", 160),
            })
    return out


__all__ = ["RESOURCE_FIELDS", "router"]
