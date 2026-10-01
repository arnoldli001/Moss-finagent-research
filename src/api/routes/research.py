"""投研任务路由：POST analyze（异步）+ GET 状态轮询。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.runtime import Runtime, agent_health
from src.api.tasks import TaskStore, new_task_id
from src.core.budget import get_budget
from src.core.cancel import CancellationToken, TaskCancelledError
from src.core.config import get_settings
from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_LOG,
    brief,
)
from src.core.executors import run_infra
from src.infrastructure.connectors.security_resolver import resolve_stock
from src.orchestration.supervisor import plan_run, query_needs_stock_resolution
from src.scheduler.registry import alert_scan_schedule

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["research"])

#: Ollama 探针的进程内缓存：(取数时刻, 状态)。
#:
#: 为什么要缓存（2026-09-24 冷启动实测）：这一探是**2 秒超时**的 HTTP 调用，
#: 而本机 Ollama 没起时正好要等满 2 秒。前端**默认落地页底部**就挂着
#: 「数据源健康度」面板（20 秒轮询一次 `/health`），于是首屏那 7 个并发请求
#: 每次都陪它排队 —— 实测首屏全部就绪 3.1s 里有 2s 是这一探。
#:
#: 而"本地模型在不在"是**分钟级**信息（模型要么常驻要么没起），缓存完全够用：
#: 与 `data_health` 里 Tushare 覆盖 / 仓库统计同一口径 —— 请求读缓存秒回，
#: 过期就顺手起一个后台线程重探。首次没有缓存时如实返回 `"unknown"`
#: （"还不知道"），不编一个值。
_OLLAMA_CACHE: dict[str, object] = {"at": 0.0, "status": "unknown", "busy": False}
_OLLAMA_TTL = 60.0


def _probe_ollama(url: str) -> str:
    """同步探一次 Ollama（在线程里跑）。"""
    import httpx

    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(url)
        return "connected" if resp.status_code == 200 else "degraded"
    except (httpx.HTTPError, OSError):
        return "unreachable"


def _ollama_status(url: str) -> str:
    """读 Ollama 状态缓存；过期/缺失就起后台线程重探（**绝不阻塞请求**）。"""
    import threading

    now = time.monotonic()
    fresh = now - float(_OLLAMA_CACHE["at"] or 0.0) < _OLLAMA_TTL
    if fresh or _OLLAMA_CACHE["busy"]:
        return str(_OLLAMA_CACHE["status"])

    _OLLAMA_CACHE["busy"] = True

    def _job() -> None:
        try:
            status = _probe_ollama(url)
            _OLLAMA_CACHE["status"] = status
            _OLLAMA_CACHE["at"] = time.monotonic()
        except Exception:  # noqa: BLE001 探针失败不该影响任何东西
            logger.debug("Ollama 探针异常（忽略）", exc_info=True)
        finally:
            _OLLAMA_CACHE["busy"] = False

    threading.Thread(target=_job, name="ollama-probe", daemon=True).start()
    return str(_OLLAMA_CACHE["status"])


# 流式chunk聚合时需按LangGraph reducer语义累加的列表通道
_ACCUMULATE_LIST_KEYS = frozenset({
    "agent_outputs", "data_refs", "trace_ids", "raw_points",
    "cleaned_points", "validated_points", "errors", "progress",
})

# ====== 投研分析结果缓存 ======
# key = hash(query + analysis_type + target) → (expiry, result_dict)
# TTL 10 分钟：相同查询直接返回缓存，不重跑 LangGraph 管线
_RESULT_CACHE: dict[str, tuple[float, dict]] = {}
_RESULT_CACHE_TTL = 600  # 10 分钟
#: 结果缓存条目上限。原实现无上限：条目虽小，但长跑进程会单调增长
#: （每个条目含完整 final_report 字符串）。
_RESULT_CACHE_MAX = max(32, int(os.environ.get("MOSS_RESULT_CACHE_MAX", "500")))


# ====== 准入控制（并发上限）======
# 为什么要它（2026-09-25）：原实现 `asyncio.create_task(_run())` 直接起，
# **没有任何并发闸门** —— N 个用户 = N 条完整管线同时跑。而单条 full 管线是
# 7~15 次 LLM 调用 + 最多 20 个指标并发采集 + 20 万 token 预算，后果不是
# "变慢"而是三重放大：
#   ① LLM 配额：超限调用在 provider 层失败 → 触发**全局三态熔断器**
#      （deepseek 60s 内 3 次失败即 OPEN）→ **一个用户的突发流量让所有用户
#      一起降级到备模型**；
#   ② 成本：token 预算是**单任务**口径，无全局上限；
#   ③ 默认线程池被打满（`asyncio.to_thread` 用 `min(32, cpu+4)`），
#      连健康检查都排队（`core/executors.py` 记录了 /health 要 144.5s 的历史故障）。
#
# 上限按**后端资源**定，不按用户数定：从 LLM 配额反推
# （可支撑的并发 LLM 调用数 ÷ 单任务平均调用数）。压测后再调。
_MAX_INFLIGHT = max(1, int(os.environ.get("MOSS_RESEARCH_MAX_INFLIGHT", "4")))
#: 排队等待名额的上限：等不到就明确拒绝，而不是让请求无限挂起
_QUEUE_TIMEOUT_S = max(1.0, float(os.environ.get("MOSS_RESEARCH_QUEUE_TIMEOUT", "30")))

#: 当前在飞任务数（准入闸门占用数）
_inflight = 0
#: 准入信号量（懒建：必须绑定到运行中的事件循环）
_admission: asyncio.Semaphore | None = None

# ====== 同问合流（in-flight dedup）======
# 原实现的击穿窗口：从"检查结果缓存未命中"到"管线跑完才写缓存"之间隔着
# **整个管线执行时间**（数十秒），这期间任何相同 qhash 的请求都会重新起
# 一条完整管线。10 个用户同时点同一个热门问题 → 10 条完整管线。
#
# 做法（最小且无状态）：在**开始执行时**就用 qhash 占位登记 leader 的 task_id，
# 后到的相同请求直接拿到**同一个 task_id** 去轮询 —— 不新起管线、不额外
# 等待、也不需要唤醒机制。leader 跑完后结果进 `_RESULT_CACHE`，
# 之后（含占位释放后）的请求走第1层缓存命中。
#
# 为什么不用 "future 等结果" 来合流：那会让 follower 的连接一直挂着，
# 而本项目的前端本来就是"提交拿 task_id → 轮询"模型，复用 task_id
# 天然契合既有交互，也避免了长连接在代理/网关处被掐断的风险。
#: qhash → 正在计算的 leader task_id
_inflight_by_hash: dict[str, str] = {}


def _admission_gate() -> asyncio.Semaphore:
    """准入信号量（懒建，绑定当前事件循环）。"""
    global _admission
    if _admission is None:
        _admission = asyncio.Semaphore(_MAX_INFLIGHT)
    return _admission


def _release_admission() -> None:
    """释放准入名额。"""
    _admission_gate().release()


def _cache_put(qhash: str, payload: dict) -> None:
    """写结果缓存（带容量淘汰：最旧的先出）。"""
    _RESULT_CACHE[qhash] = (time.time() + _RESULT_CACHE_TTL, payload)
    while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
        oldest = min(_RESULT_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _RESULT_CACHE.pop(oldest, None)


def _release_hash(qhash: str) -> None:
    """计算结束后释放 qhash 占位（后续相同请求转为走结果缓存）。"""
    _inflight_by_hash.pop(qhash, None)


def capacity_snapshot() -> dict:
    """并发容量快照（供 /health 与容量评估观测）。"""
    return {
        "max_inflight": _MAX_INFLIGHT,
        "inflight": _inflight,
        "queue_timeout_s": _QUEUE_TIMEOUT_S,
        "dedup_merging": len(_inflight_by_hash),
        "result_cache_entries": len(_RESULT_CACHE),
        "result_cache_max": _RESULT_CACHE_MAX,
    }


def _query_hash(query: str, analysis_type: str, target: str) -> str:
    """归一化 query 生成缓存 key。"""
    normalized = f"{query.strip().lower()}|{analysis_type}|{target.strip()}"
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


def clear_result_cache() -> None:
    """清空结果缓存（调试/手动刷新用）。"""
    _RESULT_CACHE.clear()


class AnalyzeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    analysis_type: str = "full"
    target: str = ""
    tenant_id: str = "tenant_001"
    info_items: list[dict] = Field(default_factory=list)
    """信息层输入（新闻/公告/研报）：[{text, source_name, publish_time, title?}]；
    非空时自动追加A05→A06→A07信息层管线。"""
    options: dict = Field(default_factory=dict)


def _store(request: Request) -> TaskStore:
    return request.app.state.store


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


@dataclass(frozen=True)
class AnalysisSubject:
    """一次请求的「分析谁」：`target` 进规划 prompt，`focus_*` 是顺带看的标的。"""

    target: str = ""
    target_display: str = ""
    focus_stock_code: str = ""
    focus_stock_name: str = ""
    #: 人话说明：解析结果或**冲突改判**（空 = 无需说明）。
    note: str = ""


async def resolve_analysis_subject(
    target: str, query: str, analysis_type: str,
) -> AnalysisSubject:
    """把「输入标的 + 问句」解析成这一轮真正要分析的标的。

    ## ★★★ 2026-09-28 第二十三轮：不再只在 `analysis_type == "stock"` 时解析

    【报障现场】用户问：「…以及AI应用加速失业率增加对消费的影响节奏…
      未来半年能否持有高股息的**招商银行**？」规划器把它判成 `macro`，
      于是原先那句 `if analysis_type == "stock"` 直接跳过 →
      **"招商银行"永远没被解析成 600036** → A10 无从取数 →
      A17 只能在 `data_gaps` 里写「招商银行个股财务明细…未提供」。

    【判据】由 `query_needs_stock_resolution()` 回答"问句里有没有一只具体个股"
      （与 supervisor 的信号判据**同源**、同一份关键词表）。
      有 → 解析；解析出的代码**单独放 `focus_stock_code`**，
      **不写进 `target`** —— 否则宏观问的分析焦点会被这只股票顶掉
      （A08 的 prompt 会变成"分析焦点：招商银行"）。

    ## ★★ 2026-09-29：问句点名的股票优先于输入框里的代码

    【报障现场】同一条问句，请求里的 `target` 是 **300068**（输入框残留/误填）：
      `candidate = target or query` ⇒ 解析出 300068(ST南都)，
      于是**整条链路分析的是另一只票** —— A01 去取
      `股息率:300068`/`商誉占净资产比:300068`/`fed:effr:300068`…，
      A10/A11/A12 也全按它算；而用户读到的是"招商银行的数据没取到"。
      **不报错、答错标的**，比缺数据更危险。

    【判据】只在**两个条件同时成立**时才改（否则原样返回，保持既有行为）：
      ① 输入标的是**裸 6 位代码**（说明是"顺手填的代码"，不是主题词）；
      ② 问句能解析出一只**不同的**股票。
      这时以问句为准，**且 `target` 一起改** —— `target` 是规划 prompt 的
      「用户指定标的」与 `sanitize_indicators` 的补后缀依据，只改 `focus`
      会让 LLM 继续按旧代码生成指标（实测就是这个形态）。

    解析失败/名称表不可用 → 保持原值，下游按缺口如实处理（不阻断任务）。
    """
    target = (target or "").strip()
    subject = AnalysisSubject(target=target, target_display=target)
    if not (analysis_type == "stock"
            or query_needs_stock_resolution(query, target)):
        return subject
    try:
        resolved = await resolve_stock(target or query)
    except Exception as exc:  # noqa: BLE001 名称解析为增强能力，失败不阻断
        logger.warning("证券名称解析失败(%s): %s", (target or query)[:40], exc)
        return subject
    if not resolved:
        return subject

    note = ""
    if target and re.fullmatch(r"\d{6}", target) and resolved[0] == target:
        try:
            named = await resolve_stock(query)
        except Exception as exc:  # noqa: BLE001 解析失败就沿用输入标的
            logger.warning("问句标的解析失败(%s): %s", query[:40], exc)
            named = None
        if named and named[0] and named[0] != resolved[0]:
            logger.warning(
                "输入标的 %s(%s) 与问句点名的 %s(%s) 冲突 → 以问句为准",
                target, resolved[1], named[0], named[1])
            note = (f"输入标的 {target}({resolved[1]}) 与问句点名的 "
                    f"{named[1]}({named[0]}) 不一致 → 已按问句分析 {named[1]}")
            resolved = named
            target = named[0]

    if analysis_type == "stock" or note:
        # 个股任务：`target` 必须是这只股票的**代码**（既有行为不变）；
        # 冲突改判时同理（此时 target 已等于 resolved[0]，赋值是幂等的）。
        # ⚠️ 这两行是**行为契约**：`target` 会进规划 prompt，
        #    留着中文名会让 LLM 按名字拼指标（`PE(TTM):招商银行`）。
        target = resolved[0]
        target_display = resolved[1] or subject.target_display or target
    else:
        target_display = subject.target_display
    return AnalysisSubject(
        target=target,
        target_display=target_display,
        focus_stock_code=resolved[0],
        focus_stock_name=resolved[1],
        note=note,
    )


@router.post("/research/analyze", status_code=202)
async def submit_analyze(body: AnalyzeRequest, request: Request) -> dict:
    """提交投研分析任务（异步执行，返回task_id供轮询）。

    三层流量治理，按代价从低到高：
    1. **结果缓存**：相同 query+type+target 在 10 分钟内直接返回，不重跑管线；
    2. **同问合流**：已有相同查询在算 → 不重跑，返回那次计算的 task_id
       （堵住"检查缓存未命中"到"跑完写缓存"之间的整个管线时长击穿窗口）；
    3. **准入控制**：在飞任务数受 `MOSS_RESEARCH_MAX_INFLIGHT` 限制，
       超出则排队，等不到名额（`MOSS_RESEARCH_QUEUE_TIMEOUT`）明确拒绝。

    `force_refresh=true` 绕过前两层（但仍受准入控制约束）。
    """
    global _inflight

    runtime = _runtime(request)
    store = _store(request)

    # ====== 第1层：结果缓存 ======
    force_refresh = body.options.get("force_refresh", False)
    qhash = _query_hash(body.query, body.analysis_type,
                        body.target or body.query)
    if not force_refresh:
        cached = _RESULT_CACHE.get(qhash)
        if cached and cached[0] > time.time():
            logger.info("结果缓存命中(qhash=%s, 剩%.0fs)，直接返回",
                        qhash, cached[0] - time.time())
            return cached[1]
        elif cached:
            _RESULT_CACHE.pop(qhash, None)  # 过期清除

        # ====== 第2层：同问合流 ======
        # 已有相同查询正在计算：复用它的 task_id，不新起管线。
        # 前端拿到 task_id 后照常轮询，最终会从结果缓存读到同一份报告。
        merging_id = _inflight_by_hash.get(qhash)
        if merging_id is not None and store.get(merging_id) is not None:
            logger.info("同问合流(qhash=%s → 复用 task=%s)，不重跑管线",
                        qhash, merging_id)
            return {
                "task_id": merging_id, "trace_id": merging_id,
                "status": "queued", "deduplicated": True,
                "cache_hit": False,
                "note": "相同查询已在计算中，已复用该任务；请轮询同一 task_id",
            }

    # ====== 第3层：日成本预算 ======
    # 实测单次分析中位 0.0527 元、p90 0.2237 元，日预算由 MOSS_LLM_DAILY_BUDGET_CNY
    # 控制（默认 20 元）。**必须放在并发闸门之前**：预算不足时不该去排队，
    # 排到了也会因为没钱而失败，白占一个名额、白让用户等 30 秒。
    #
    # 用预扣而非事后记账：突发时所有任务都还没产生 token 消耗，只看已花费
    # 会集体放行、集体超额（TOCTOU）。预扣把这个窗口关掉。
    budget = get_budget()
    if not budget.reserve_task():
        snap = budget.snapshot()
        logger.warning(
            "预算拒绝：今日已用 %.4f/%.2f 元，剩余 %.4f 元不足以预扣 %.2f 元",
            snap["spent_cny"], snap["budget_cny"],
            snap["remaining_cny"] or 0.0, snap["task_reserve_cny"])
        raise HTTPException(
            status_code=503,
            detail=(f"今日 LLM 预算不足：已用 {snap['spent_cny']:.2f}/"
                    f"{snap['budget_cny']:.2f} 元（{snap['used_pct']}%），"
                    f"剩余 {snap['remaining_cny']:.2f} 元不够启动一次分析"
                    f"（单次需预留 {snap['task_reserve_cny']:.2f} 元）。"
                    f"请明日再试，或调高 MOSS_LLM_DAILY_BUDGET_CNY"),
        )
    budget_reserved = True

    # ====== 第4层：准入控制 ======
    # 名额在请求协程里等待（不阻塞事件循环），等不到就明确拒绝而非无限挂起。
    if _inflight >= _MAX_INFLIGHT:
        logger.info("准入排队：在飞=%d/%d，等待名额（超时 %.0fs）",
                    _inflight, _MAX_INFLIGHT, _QUEUE_TIMEOUT_S)
    try:
        await asyncio.wait_for(_admission_gate().acquire(), timeout=_QUEUE_TIMEOUT_S)
    except TimeoutError:
        budget.release_task()  # 名额没拿到，预扣的钱要退回去
        logger.warning("准入拒绝：队列等待超时（在飞=%d/%d）",
                       _inflight, _MAX_INFLIGHT)
        raise HTTPException(
            status_code=503,
            detail=(f"分析队列已满（当前 {_inflight}/{_MAX_INFLIGHT} 个任务进行中），"
                    f"排队 {_QUEUE_TIMEOUT_S:.0f}s 未获得名额，请稍后重试"),
        ) from None
    _inflight += 1

    task_id = new_task_id()
    try:
        # 个股标的解析：中文简称/问句中的名称 → 6位代码（如"中际旭创"→300308）。
        # 解析失败保留原值，由下游按"数据不足"诚实处理，不阻断任务。
        #
        # ★★★ 2026-09-28 第二十三轮：**不再只在 `analysis_type == "stock"` 时解析**
        #
        # 【报障现场】用户问：「…以及AI应用加速失业率增加对消费的影响节奏…
        #   未来半年能否持有高股息的**招商银行**？」规划器把它判成 `macro`，
        #   于是原先这行 `if body.analysis_type == "stock"` 直接跳过 →
        #   **"招商银行"永远没被解析成 600036** → A10 无从取数 →
        #   A17 只能在 `data_gaps` 里写「招商银行个股财务明细…未提供」。
        #
        # 【新判据】由 `query_needs_stock_resolution()` 回答"问句里有没有一只
        #   具体个股"（与 supervisor 的信号判据**同源**、同一份关键词表）。
        #   有 → 解析；解析出的代码**单独放 `focus_stock_code`**，
        #   **不写进 `target`** —— 否则宏观问的分析焦点会被这只股票顶掉
        #   （A08 的 prompt 会变成"分析焦点：招商银行"）。
        #
        # 解析失败/名称表不可用 → 保持空串，下游按缺口如实处理（不阻断任务）。
        target = (body.target or "").strip()
        target_display = target
        focus_stock_code = ""
        focus_stock_name = ""
        subject = await resolve_analysis_subject(
            target, body.query, body.analysis_type)
        target, target_display = subject.target, subject.target_display
        focus_stock_code, focus_stock_name = (
            subject.focus_stock_code, subject.focus_stock_name)

        plan = plan_run(body.analysis_type, target, body.info_items, query=body.query)

        store.create(
            task_id,
            trace_id=task_id,
            tenant_id=body.tenant_id,
            query=body.query,
            analysis_type=plan["analysis_type"],
            target=target_display,
        )
        # 取消令牌：注入state，各节点+LLM Gateway在检查点读取
        # live_state指向state本身，节点内可通过push_progress/push_messages实时更新
        cancel_token = CancellationToken(task_id)
        store.register_token(task_id, cancel_token)

        state = {
            "task_id": task_id, "tenant_id": body.tenant_id,
            "user_query": body.query, "analysis_type": plan["analysis_type"],
            "target": target, "target_display": target_display,
            # ★ 第二十三轮：复合问里夹带的个股代码（宏观/行业问也可能有）。
            #   与 `target` **分开**：target 表达"这次分析的主题"，
            #   focus_stock_code 表达"顺带要看的标的"，两者语义不同。
            "focus_stock_code": focus_stock_code,
            "focus_stock_name": focus_stock_name,
            "plan": [], "raw_points": [], "cleaned_points": [],
            # ★ 2026-09-29：必须在这里也给出初值。
            #   `_planned_indicators` 是 supervisor 写入、采集节点读取的 channel；
            #   它**必须声明在 `ResearchState`**（否则 LangGraph 会静默丢弃，
            #   见 `tests/unit/test_planned_indicators_channel.py` 的现场说明）。
            #   这里给空列表是让"键一定存在"成为契约的一部分 ——
            #   采集节点读 `None` 会回退到 `plan_run()`（**不做问句增补**），
            #   那正是"招商银行无任何个股数据"那个缺陷的形态。
            "_planned_indicators": [],
            "validated_points": [], "validation_report": {}, "storage_stats": {},
            "info_items": body.info_items, "verified_items": {}, "extracted_events": {},
            "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
            "final_report": None,
            # ★ 2026-09-29：标的**改判说明**作为第一条进度下发（`progress` 是
            #   `operator.add` 聚合通道 ⇒ 预置项会保留在最前面）。
            #   为什么必须让用户看见：`target=300068` + 问句问"招商银行"时，
            #   系统按问句分析 600036 —— 若只写日志，用户会以为系统答错了票。
            "progress": ([subject.note] if subject.note else []),
            "cancellation_token": cancel_token,
        }
        # 暴露state引用，running时前端可轮询agent_messages
        store.set_live_state(task_id, state)
        # 节点内可通过cancel_token.push_progress/push_messages实时更新live_state
        cancel_token.live_state = state

        # 登记同问合流占位：从此刻起，相同 qhash 的请求不再新起管线
        if not force_refresh:
            _inflight_by_hash[qhash] = task_id

        handle = asyncio.create_task(_run(task_id, qhash, body, plan, state, store, runtime))
        store.register_handle(task_id, handle)
    except BaseException:
        # 建任务阶段失败：必须归还名额与预扣预算，否则闸门和预算会被永久占掉
        _release_admission()
        _inflight -= 1
        _inflight_by_hash.pop(qhash, None)
        if budget_reserved:
            budget.release_task()
        raise

    return {"task_id": task_id, "trace_id": task_id, "status": "queued",
            "plan": plan["agents"],
            # ★ 改判说明（空串 = 输入标的与问句一致）：前端可直接显示，
            #   避免"我填了 300068，为什么分析的是招商银行"的困惑。
            "target": target, "target_display": target_display,
            "focus_stock_code": focus_stock_code,
            "subject_note": subject.note}


async def _run(task_id: str, qhash: str, body: AnalyzeRequest, plan: dict,
               state: dict, store: TaskStore, runtime: Runtime) -> None:
    """执行投研管线并把结果写回 TaskStore（含名额/占位/预算归还）。"""
    global _inflight

    budget = get_budget()

    async def _finish() -> None:
        """统一收尾：释放同问占位、准入名额与预算预扣。绝不抛异常。

        ⚠️ 这三件事**必须**在 finally 里做：名额只借不还 = 闸门被渐进占满，
        最终服务完全不再接受新任务（比无闸门更糟）；占位不释放 = 相同查询
        此后永远被"合流"到一个已结束的任务上；预算不结算 = 预扣持续累积，
        最终所有请求都因"预算不足"被拒。
        """
        global _inflight
        try:
            _release_hash(qhash)
        except Exception:  # noqa: BLE001 收尾不能影响任务结果
            logger.debug("释放同问占位失败", exc_info=True)
        try:
            # 结算预算：网关已把真实消耗记进 spent，这里只退预扣、补记分账。
            # actual_cost=0 —— 真实成本已由 gateway 逐次 record 计入，
            # 再传一次会重复计算；预扣退还即可（release_task 的语义是
            # reserved -= need，并可选补一笔 actual）。
            budget.release_task(actual_cost=0.0)
        except Exception:  # noqa: BLE001
            logger.debug("结算预算预扣失败", exc_info=True)
        try:
            _release_admission()
        except Exception:  # noqa: BLE001
            logger.debug("释放准入名额失败", exc_info=True)
        _inflight = max(0, _inflight - 1)

    try:
        store.update(task_id, status="running")
        try:
            final = {}
            # 用astream而非ainvoke：每个node的return值实时更新live state
            # LangGraph astream chunk 格式为 {node_name: node_return_dict}，
            # 需展开内层dict聚合到final与live state
            async for chunk in runtime.graph.astream(state):
                for node_name, node_update in chunk.items():
                    if not isinstance(node_update, dict):
                        final[node_name] = node_update
                        continue
                    # 列表通道按图reducer语义累加（agent_outputs/errors等不能被
                    # 后一个节点的update整体覆盖，否则最终只保留末尾节点产出）
                    for key, value in node_update.items():
                        if (key in _ACCUMULATE_LIST_KEYS
                                and isinstance(value, list)
                                and isinstance(final.get(key), list)):
                            final[key] = final[key] + value
                        else:
                            final[key] = value
                    # 实时更新progress和agent_messages到live state（前端轮询可见）
                    if "progress" in node_update:
                        state["progress"] = (
                            state.get("progress") or []) + node_update["progress"]
                    if "agent_messages" in node_update:
                        state["agent_messages"] = (
                            state.get("agent_messages") or []) + (
                                node_update.get("agent_messages") or [])
            store.update(
                task_id,
                status="completed",
                agent_outputs=final.get("agent_outputs", []),
                agent_messages=final.get("agent_messages", []),
                errors=final.get("errors", []),
                final_report=final.get("final_report"),
                progress=final.get("progress", []),
            )
            # ====== 结果缓存写入 ======
            # 只缓存成功的、有 final_report 的任务
            if final.get("final_report"):
                cache_result = {
                    "task_id": task_id, "trace_id": task_id,
                    "status": "completed",
                    "cache_hit": True,
                    "cache_ttl_seconds": _RESULT_CACHE_TTL,
                    "plan": plan["agents"],
                    "final_report": final.get("final_report"),
                    "agent_outputs": final.get("agent_outputs", []),
                    "progress": final.get("progress", []),
                    "errors": final.get("errors", []),
                }
                _cache_put(qhash, cache_result)
                logger.info("结果缓存写入(qhash=%s, TTL=%ds)",
                            qhash, _RESULT_CACHE_TTL)
        except TaskCancelledError:
            store.update(task_id, status="cancelled", error="用户主动停止任务")
        except asyncio.CancelledError:
            store.update(task_id, status="cancelled", error="用户主动停止任务")
            raise
        except Exception as exc:  # noqa: BLE001 任务级兜底
            logger.exception("research task failed: %s", task_id)
            store.update(task_id, status="failed", error=brief(exc, BRIEF_LOG))
    finally:
        store.clear_live_state(task_id)
        # 定期淘汰已完成任务（TTL + 容量），避免长跑内存单调增长
        store.evict()
        await _finish()


@router.get("/research/capacity")
async def research_capacity(request: Request) -> dict:
    """并发容量与背压观测（运维用）。

    ⚠️ **必须注册在 `/research/{task_id}` 之前**：Starlette 按注册顺序取
    第一个匹配的路由，`{task_id}` 会把字面量 `capacity` 当成任务 ID 吃掉
    （实测确认过），那样这个接口永远 404 "task not found"。

    回答"现在还能接多少并发、是否在排队、内存有没有涨"：

    - `inflight` / `max_inflight`：准入闸门占用率 —— 相等即满负荷，
      新请求进入排队（等 `queue_timeout_s` 拿不到名额就返回 503）。
    - `dedup_merging`：当前被同问合流合并的查询数（>0 表示热点问题被复用）。
    - `task_store`：任务表规模。这些数应**趋于稳定**而非单调增长 ——
      持续增长说明淘汰阈值要调小（见 `api/tasks.py` 的 TTL/容量双阈值）。
    - `llm_cache`：语义索引是否已建（`index_built`）。未建时首次语义查找
      要付一次建索引代价（本机 3108 文件约 0.7s，走线程池不阻塞事件循环）。
    """
    runtime = _runtime(request)
    store = _store(request)
    snap = capacity_snapshot()
    cache_stats: dict = {}
    gateway = getattr(runtime, "gateway", None)
    cache = getattr(gateway, "_cache", None) if gateway is not None else None
    if cache is not None and hasattr(cache, "stats"):
        try:
            cache_stats = cache.stats()
        except Exception as exc:  # noqa: BLE001 观测量失败不影响接口
            cache_stats = {"error": brief(exc, BRIEF_DEFAULT)}
    snap["task_store"] = store.stats()
    snap["llm_cache"] = cache_stats
    snap["budget"] = get_budget().snapshot()
    return snap


@router.get("/research/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    """任务状态与结果（completed时含conclusion/report，契约见API_REFERENCE）。

    running时从live state中读取实时agent_messages和progress，前端可展示协作过程。
    """
    store = _store(request)
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    conclusion, confidence = None, None
    for o in record.agent_outputs:
        if o.get("agent_id") == "A17_recommend":
            conclusion = o.get("conclusion")
            confidence = o.get("confidence")
    # running时从live state读取实时进度
    agent_messages = record.agent_messages
    progress = ""
    if record.status == "running":
        live = store.get_live_state(task_id)
        if live is not None:
            agent_messages = live.get("agent_messages") or []
            prog_list = live.get("progress") or []
            if isinstance(prog_list, list) and prog_list:
                progress = prog_list[-1] if prog_list else ""
    return {
        "task_id": record.task_id, "trace_id": record.trace_id,
        "status": record.status, "conclusion": conclusion,
        "confidence": confidence, "report": record.final_report,
        "agent_messages": agent_messages,
        "progress": progress,
        "errors": record.errors, "error": record.error,
        "created_at": record.created_at,
    }


@router.post("/research/{task_id}/cancel", status_code=200)
async def cancel_task(task_id: str, request: Request) -> dict:
    """用户主动取消任务：触发CancellationToken + 取消asyncio Task。"""
    store = _store(request)
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    if record.status in ("completed", "failed", "cancelled"):
        return {"task_id": task_id, "status": record.status,
                "message": "任务已结束，无需取消"}
    await store.cancel_task(task_id)
    return {"task_id": task_id, "status": "cancelled",
            "message": "任务已取消，LLM调用和Agent链路已终止"}


@router.get("/research/{task_id}/agents")
async def get_task_agents(task_id: str, request: Request) -> dict:
    """任务内全部Agent输出摘要（分析过程透明化）。"""
    record = _store(request).get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task_id": record.task_id, "status": record.status,
            "agent_outputs": record.agent_outputs, "errors": record.errors}


@router.get("/debug/plan")
async def debug_plan(analysis_type: str = "full", target: str = "") -> dict:
    """查看Supervisor对某类任务的规划（不执行）。"""
    return plan_run(analysis_type, target) | {"agents_health_note": "see /api/v1/health"}


def liveness_payload() -> dict:
    """存活探针的**唯一**返回体实现。

    `/api/v1/health/live`（带版本，前端用）与根级 `/healthz`（不带版本，
    Dockerfile / Cloudflare / k8s 探针用）**共用这一份**，`CHG-0128`。
    两份拷贝迟早会漂移 —— 而漂移的后果是"两个探针一个说活一个说死"。
    """
    import time

    return {"ok": True, "ts": time.time()}


@router.get("/health/live")
async def health_live() -> dict:
    """**极轻**存活探针：不做任何 I/O，只回答"进程还在服务请求吗"。

    为什么不能拿 `/health` 当存活探针用 —— 它会
      ① 用 2 秒超时去连 Ollama；② 校验 LLM 审计链；③ 聚合数据源健康度。
    在这个项目上实测一次 0.3~2.5 秒，且在数据源熔断、审计链很长、
    或磁盘被 akshare 占满时膨胀到几十上百秒（见
    `tests/unit/test_health_performance.py` 记录的 142.8s / 300s）。
    用它判断"后端还活着吗"，服务稍慢就会**误报为宕机** ——
    前端会对着一个正常工作的后端弹"无法连接服务器"，比不提示更糟。

    所以按标准做法拆成两个探针：

    | 路径 | 语义 | 成本 | 鉴权 | 谁用 |
    |---|---|---|---|---|
    | `/health/live` | 进程活着吗 | **0 I/O** | **免鉴权** | 前端连接状态条、网络错误归因 |
    | `/health` | 依赖都好吗 | 秒级 | 需登录 | 运维/诊断页 |

    ⚠️ **两个"不做"**：

    1. **不查数据库、不读配置、不 ping 任何外部服务** ——
       探针本身不能成为故障源。一个连数据库都打不开的进程，
       仍应如实回答"我在"，然后由 `/health` 报告依赖异常；
       两者混淆会让排障时无法区分"进程死了"与"进程活着但依赖坏了"。
    2. **不返回 pid / 环境名 / 版本号** —— 它免鉴权，
       匿名调用者拿不到任何可用于踩点的部署信息（`dev` 环境名本身
       就是一种提示）。真有运维需求请查 `/health` 或日志。

    免鉴权是**必须**的：存活探针在登录页就要能用（那时还没有会话），
    而且 k8s/负载均衡的 liveness probe 本来就不该带凭证。

    ⚠️ 根级别名 `/healthz` **曾经只写在白名单和设计文档里、没有路由**
    （实测 404），见 `CHG-0128`；现在由 `src/api/main.py` 提供，
    并返回**同一个** `liveness_payload()`。

    `CHG-0137`：顺带做一次计数打点（内存累加；它测不到"循环被阻塞"，
    那件事的仪器是 `src/core/loop_lag.py` 的 1 Hz 采样）。
    """
    import time as _time

    t0 = _time.perf_counter()
    try:
        from src.core import loop_lag

        loop_lag.note_probe((_time.perf_counter() - t0) * 1000.0)
    except Exception:  # noqa: BLE001 打点失败绝不影响探针
        pass
    return liveness_payload()


@router.get("/health")
async def health(request: Request) -> dict:
    """聚合健康检查：Agent + 模型网关 + 数据库 + 审计链 + 数据源健康度。"""
    from src.api.data_health import build_data_health

    def _data_health(runtime_obj):
        """数据源健康度（失败只降级为空，绝不让 /health 500）。"""
        try:
            return build_data_health(runtime_obj)
        except Exception as exc:  # noqa: BLE001 健康检查本身不能崩
            return {"available": False, "error": brief(exc, BRIEF_DEFAULT)}

    def _search_sources() -> dict:
        """外部**搜索源**（博查=主 / 百度=备）的闸门状态（`CHG-0122`）。

        ## 两条纪律

        1. **失败只降级为空，绝不让 /health 500** —— 与 `_data_health` 同一条。
           搜索源是"锦上添花"的兜底能力，它坏了不该把整个健康检查打挂。
        2. ★ **这里只读本地额度账本，绝不发网络请求。** `/health` 是前端
           「数据源健康度」面板 **20 秒轮询**的热路径，本项目实测过"在请求路径上
           做真探测"的代价（对 14GB 库做 COUNT(*) ⇒ `/health` 卡到 **300 秒**超时，
           同一时间做T面板一起卡死）。
           **"现在到底能不能用"的真探测在 `scripts/check_external_sources.py`**，
           不在请求路径上 —— 那个是要花钱的（每次真搜一次）。

        所以这里给的判据是"**额度还剩多少、凭据配没配**"这类**本地就能答**的问题；
        它足以支撑"要不要去开通/充值"的维护决策，而不会把面板拖慢。
        """
        try:
            from src.infrastructure.search import router as _search_router

            return {"available": True,
                    "order": list(_search_router.PROVIDER_ORDER),
                    "providers": _search_router.providers_status()}
        except Exception as exc:  # noqa: BLE001 同 _data_health
            return {"available": False, "error": brief(exc, BRIEF_DEFAULT)}

    def _sync_probes() -> dict:
        """把 /health 里**同步阻塞**的几段一次性挪进线程。

        为什么必须挪（2026-09-24 冷启动实测）：原来是"建 SSL 上下文连 Ollama
        （0.4s）+ 校验审计链 + 探测连接器能力"三段同步代码跑在事件循环上，
        而前端**默认落地页底部就挂着「数据源健康度」面板**（20 秒轮询一次）——
        于是每次打开页面，首屏那 7 个并发请求都要陪它排队：实测所有接口
        （连 `/agents/meta` 这种纯字典）都被拖到 ~2.0 秒才回。
        挪进线程后互相不再遮挡（首屏全部就绪 4.7s → 2.7s）。

        Ollama 那一探**已经不在这里**了（见 `_ollama_status`）：它 2 秒超时，
        放请求路径上永远要等满，改成读缓存 + 后台重探。
        """
        import importlib.util

        from src.infrastructure.repositories.audit_chain import ChainVerifier

        chain = ChainVerifier(
            f"{settings.llm_audit_dir}/audit_chain.jsonl").verify()

        # 数据源真实状态：连接器能力来自注册表（非网络谎报），存储做功能探测
        connectors = []
        capabilities = runtime.backend.get_capabilities()
        for cap in capabilities.get("routes", []):
            name = cap.get("name", "unknown")
            if cap.get("simulated"):
                status = "simulated"
            elif name.lower().startswith("akshare"):
                status = ("ready" if importlib.util.find_spec("akshare")
                          else "not_installed")
            else:
                status = "configured"
            connectors.append({
                "name": name,
                "status": status,
                "simulated": bool(cap.get("simulated", False)),
                "indicators": cap.get("indicators", []),
            })
        # 本地存储家底 + 调度作用域：与上面两段**同一趟线程**（不多一次往返）
        stores = _local_stores()
        schedule = _schedule_scope()
        return {"chain": chain, "connectors": connectors, "stores": stores,
                "schedule": schedule}

    def _schedule_scope() -> dict:
        """本实例**实际会触发**哪些作业（`CHG-0087`）—— 唯一能看见"被裁了什么"的面。

        ## 为什么不放在日志里（这是实测结论，不是偏好）

        我最初把"哪些作业因写权限归属被裁"写在 `SchedulerService.start()` 的
        `logger.info` 里。**实测那行字谁也看不到**：全仓库没有
        `logging.basicConfig()` / `dictConfig()`，root logger 没有 handler
        ⇒ INFO 被直接丢弃（Python 的 last-resort handler 只兜 WARNING 及以上）。
        逐字节搜 `data/run/*.log` 里的「调度器已启动」= **0 处**
        （对照：同文件里 WARNING 级的「作业…上一轮未结束，跳过本轮」在）。

        这与本项目记过的那道门是同一道：**"我改了" → "有没有人看到"**。
        所以改挂在这里 —— `/health` 是前端 20 秒轮询的既有响应，
        不新增往返、不新增磁盘 IO（`schedulable_jobs()` 只读 registry 与 YAML）。

        ⚠️ 语义边界：`pruned` 里的每一项都**不是故障**，而是"这台机器本来就不负责
        这件事"（写权限归属决定）。真故障看 `scheduler/runs.jsonl` 的 status。
        """
        try:
            from src.scheduler.registry import (
                SCHEDULER_DENY_ENV,
                scheduler_scope_report,
            )

            scope = scheduler_scope_report()
        except Exception as exc:  # noqa: BLE001 健康检查不能因此崩
            return {"available": False, "error": brief(exc, BRIEF_DEFAULT)}
        return {
            "available": True,
            "env": scope["env"],
            "total": scope["total"],
            "active": scope["active"],
            # 逐条给人话理由：只说"部分作业已跳过"会让运维以为作业丢了
            "pruned": scope["pruned"],
            "unknown_denied": scope["unknown_denied"],
            "deny_env": SCHEDULER_DENY_ENV,
            # ★ `CHG-0139`：角色 + **重作业有没有人在跑**。
            #   把 4 个重作业移出 API 进程之后，"它们此刻跑不跑"就不再由本进程
            #   的状态决定了 —— 本进程一切正常、health 全绿，而重作业可能
            #   因为 worker 没起/挂了**一直不执行**。这个字段就是那个答案，
            #   且它只有**十几个字节**的增量（`worker` 段是固定几项）。
            "role": scope.get("role"),
            "out_of_role": scope.get("out_of_role", []),
            "worker": scope.get("worker", {}),
        }

    def _local_stores() -> dict:
        """本地存储家底 + **写权限归属**（`CHG-0087`，用户要求"现在就接"）。

        接的是 `data_stores.describe(want_sizes=False)` —— registry 是
        "库在哪、谁能写、能不能删"的**单一事实源**（`configs/data_stores.yaml`），
        此前它零个生产调用方，于是"dev 与 pilot 同时写同一个 14 GiB 行情仓"
        只能靠翻日志发现。

        ## 为什么只接摘要（用字节数决定的，不是感觉）

        实测三种接法对 `/health` 体积的增量（基线 15,650 字节，前端 20 秒轮询）：

        | 接法 | 增量 | /health 变成 |
        |---|---|---|
        | 全量（含每条 `note`） | +11.46 KB | 26.7 KB（+75%） |
        | 去掉 `note` | +6.36 KB | 21.6 KB（+42%） |
        | **摘要（本节）** | **+1.58 KB** | **16.9 KB（+10%）** |

        `note` 合计 2,532 字符，是给人读 registry 的，不是给轮询接口的；
        全量家底另有出口（`data_asset_catalog` / 离线审计脚本调
        `describe(want_sizes=True)`）。

        ## 为什么放这里而不是新起一趟请求

        `_sync_probes()` 已经跑在**关键路径专用线程池**里，且
        `describe(want_sizes=False)` 实测 **11.0 ms**（对照 `want_sizes=True`
        要 walk 3.5 万个文件 = 3,288.9 ms，306 倍）—— 不新增串行往返、
        不递归磁盘，符合「首屏串行往返上限 = 1」。

        ⚠️ `want_sizes=False` 时 `size_mb` 一律是 `None`（**未量到**），
        不是 0 —— 后者会被读成"这个库是空的"。
        """
        try:
            from src.infrastructure.catalog.data_stores import describe

            full = describe(want_sizes=False)
        except Exception as exc:  # noqa: BLE001 健康检查不能因此崩
            return {"available": False, "error": brief(exc, BRIEF_DEFAULT)}
        return {
            "available": True,
            "env": full["env"],
            "env_root": full["env_root"],
            "registry": full["registry"],
            "count": full["count"],
            "protected": full["protected"],
            "sizes_measured": full["sizes_measured"],
            # 写权限裁决逐条给（**含待裁定项**）：`decided=False` 表示口径还没
            # 拍板、当前只报告不阻断 —— 两者混在一起看会把"还没定"读成"已经管住"。
            "sqlite": [
                {"name": s["name"], "path": s["path"], "exists": s["exists"],
                 "role": s["role"], "size_mb": s["size_mb"], "write": s.get("write")}
                for s in full["stores"] if s["kind"] == "sqlite"
            ],
            # 文件/目录类存储只给名字与存在性，体积要递归目录（留给离线审计）。
            "dirs": [
                {"name": s["name"], "path": s["path"], "exists": s["exists"],
                 "role": s["role"]}
                for s in full["stores"] if s["kind"] != "sqlite"
            ],
        }

    def _rate_limit_guard() -> dict:
        """免费档限流熔断状态（**唯一能看见"siliconflow 被限流"的面**）。

        为什么必须挂在这里：`light` 层（占 82% 调用量）首位是免费档，
        它被限流时链会自动前进到下一跳 —— 而这个过程**原先没有任何可见面**，
        只能从"延迟上升 + 下一跳配额被多吃"上后知后觉。
        见 `src/infrastructure/llm/rate_limit_guard.py`。

        ⚠️ 判据区分「未量到」与「量到 0」：读不到时给 `available: False`，
        绝不用 `total_429: 0` 假装"没限流"（AGENTS.md 硬约束）。
        """
        try:
            from src.infrastructure.llm.rate_limit_guard import get_guard

            return {"available": True, **get_guard().snapshot()}
        except Exception as exc:  # noqa: BLE001 健康检查不能因此崩
            return {"available": False, "error": brief(exc, BRIEF_DEFAULT)}

    def _query_data_hops() -> dict:
        """四跳取数的**跳级命中计数**（`src/core/hop_stats.py` 是唯一事实源）。

        ## 为什么运维需要它

        `query_data_for_agent` 是一条四跳优先级链（已采集点 → 本地库 → 连接器 →
        联网兜底），而"每一跳到底命中多少"此前**一个数都没有** ——
        于是「本地优先」这条策略改对了还是改坏了、联网兜底该不该扩白名单，
        都只能靠感觉。`counters` 给逐跳命中数，`total` 是分母（一次四跳路径算一次），
        `misses` 是缺口（**不是任何一跳的命中**：四跳全空）。

        ## 两条纪律

        1. **失败只降级为空，绝不让 /health 500** —— 与 `_search_sources` 同一条。
        2. **纯内存读**：只读进程内几个整数，0 I/O、不发网络请求，符合
           `/health` 是 20 秒轮询热路径这条硬约束。

        ⚠️ 判据区分「还没量到」与「量到 0」（AGENTS.md 硬约束）：
        冷启动时各项都是**有意义的 0**，而 `latest_hop` 会给「未量到」——
        用 0 假装"最近一跳是第 0 跳"会让冷启动看起来像故障。
        """
        try:
            from src.core.hop_stats import snapshot as _hop_snapshot

            return {"available": True, **_hop_snapshot()}
        except Exception as exc:  # noqa: BLE001 健康检查不能因此崩
            return {"available": False, "error": brief(exc, BRIEF_DEFAULT)}

    def _llm_circuit_breakers() -> dict:
        """LLM 熔断器的**逐桶状态**（provider × 租户，`circuit_breaker.py` 是唯一事实源）。

        ## 为什么必须暴露它（2026-10-01）

        熔断从"按 provider 一桶"改成"provider × 租户"之后，**谁能看到分桶**就成了
        这条修复能否运维的前提：一个租户的突发把**它自己**的桶打到 OPEN，
        运维必须能一眼看出"是哪个桶、被拒了多少次"，否则
        「一个用户把所有人打挂」这个老症状会以"某租户无声变慢"的新形态回来。

        契约：`{"available": bool, "counters": {桶 key: 快照}, "open": [开着的桶],
        "unmeasured": "未量到"}`。空态用 **`unmeasured`** 而不是 0 ——
        「一个桶都还没建」与「建了但都是 CLOSED」含义不同（AGENTS.md 硬约束）。

        两条纪律同 `_query_data_hops`：**失败只降级为空**、**纯内存读**
        （`snapshot_all()` 只读进程内字典，0 I/O）。
        """
        try:
            from src.infrastructure.llm.circuit_breaker import (
                get_circuit_registry,
            )

            snap = get_circuit_registry().snapshot_all()
            open_keys = sorted(k for k, v in snap.items()
                               if str(v.get("state")) == "OPEN")
            return {"available": True, "counters": snap, "open": open_keys,
                    "unmeasured": "未量到"}
        except Exception as exc:  # noqa: BLE001 健康检查不能因此崩
            return {"available": False, "error": brief(exc, BRIEF_DEFAULT)}

    runtime = _runtime(request)
    settings = get_settings()
    agents = agent_health(runtime.agents)

    # Ollama 状态读缓存（过期才后台重探）—— 见 `_ollama_status` 的实测说明。
    ollama_status = _ollama_status(settings.ollama_base_url)

    probes = await run_infra(_sync_probes)
    chain = probes["chain"]
    connectors = probes["connectors"]
    local_stores = probes["stores"]
    schedule_scope = probes["schedule"]

    try:
        counts = await runtime.repo.count_by_indicator()
        storage = {
            "backend": settings.data_backend,
            "status": "ok",
            "indicators": len(counts),
            "points": sum(counts.values()),
        }
    except Exception as exc:  # noqa: BLE001 健康检查需要把异常变成状态而非500
        storage = {"backend": settings.data_backend, "status": "error",
                   "error": brief(exc)}

    from src.infrastructure.repositories.cached_repo import CachedRepository

    if isinstance(runtime.repo, CachedRepository):
        redis_cache = await runtime.repo.probe()
    else:
        redis_cache = "disabled"

    return {
        "service": "moss-finagent-research",
        "status": "healthy" if all(v == "healthy" for v in agents.values()) else "degraded",
        "agents": agents,
        "event_alerts": {
            "available": getattr(runtime, "event_repo", None) is not None,
            "email_configured": bool(
                getattr(runtime, "email_notifier", None)
                and runtime.email_notifier.is_configured()),
            "scheduled_job": "event_alert_daily",  # 收盘后全量（另有盘中/午盘两班）
            "scheduled_jobs": [j["job"] for j in alert_scan_schedule()["jobs"]],
            "ws_endpoint": "/api/v1/ws/alerts",
        },
        "data_sources": {
            "connectors": connectors,
            "storage": storage,
            "redis_cache": redis_cache,
            # ★ 本地存储家底 + **谁能写**（`configs/data_stores.yaml` 是单一事实源）。
            # 有了它，"这台实例能不能写行情仓"不用再去翻调度日志比对时间戳：
            # `sqlite[].write.decided=True` 且 `write_allowed=False` = 口径已定且拒绝；
            # `decided=False` = 口径待裁定、当前只报告不阻断（两者不可混看）。
            "local_stores": local_stores,
            # ★ 外部搜索源（博查主 / 百度备）的闸门状态（`CHG-0122`）。
            # 加它的理由：这两个源的**额度/凭据**属于"要维护的东西"，而运维看的是
            # 面板。放在这里 ⇒ 复用既有的 20 秒轮询面，**不新增任何请求**。
            # ⚠️ 只读本地账本、不发网络请求（见 `_search_sources` 的说明）。
            "search_sources": _search_sources(),
            # ★ 本实例**实际会触发**哪些定时作业（`pruned` = 因写权限归属被裁的）。
            # 这是"哪些作业被裁"唯一可见的面：进程内 INFO 日志没有 handler，
            # 写在 `logger.info` 里的裁剪理由**谁也看不到**（实测逐字节 0 处）。
            "schedule": schedule_scope,
            # 数据源健康度：能力矩阵 + 实测延迟（做T模块的真实调用记录）+ Tushare 覆盖。
            # **必须放到线程里**：它要遍历 3.5 万个分区清单 + 查仓库九表（首次约 5 秒），
            # 而这是 CPU/IO 密集的同步代码 —— 直接在事件循环里跑会阻塞**所有**并发请求
            # （实测旧实现对 14GB 库做 COUNT(*) 时 /health 卡到 300 秒超时，
            #  同一时间做T面板也跟着卡）。
            #
            # 用**关键路径专用线程池**而不是 `asyncio.to_thread`：后者与自选池首屏
            # （26 只票并发取数）共用默认执行器，实测被占满时 /health 要排队
            # **144.5 秒**才返回，而它自己只要 3.74 秒。详见 src/core/executors.py。
            "health": await run_infra(_data_health, runtime),
        },
        "model_gateway": {
            "ollama": ollama_status,
            "deepseek": "configured" if settings.deepseek_api_key else "not_configured",
            # 免费档限流熔断（siliconflow/dashscope）：锁定期内该跳被**跳过**。
            # 判据区分「未量到」与「量到 0」——读不到时 available=False。
            "rate_limit_guard": _rate_limit_guard(),
        },
        "audit_chain": {"valid": chain["valid"], "records": chain["count"]},
        # ★ 四跳取数的**跳级命中计数**（哪一跳答出来的 / 缺口多少）。
        # 为什么挂在这里而不是新开端点：`/health` 是前端 20 秒轮询的**既有**响应，
        # 计数器是纯内存读（0 I/O、几个整数），既不新增往返也不拖慢热路径；
        # 而"我们有一条四跳优先级链"这句话，此前**没有任何数据**能证明每跳命中多少
        # （见 `src/core/hop_stats.py` 的说明）。
        "query_data_hops": _query_data_hops(),
        # ★ LLM 熔断器逐桶状态（provider × 租户）。为什么挂这里：
        # 熔断改成按租户分桶之后，"哪个桶开着、被拒了多少次"必须**看得见** ——
        # 否则「一个用户把所有人打挂」会变成「某个租户无声变慢」。
        "llm_circuit_breakers": _llm_circuit_breakers(),
        # 并发容量与背压（详见 /research/capacity 的说明）：
        # 放这里是为了让运维页一眼看到"闸门是否打满、任务表有没有涨"。
        "capacity": {
            **capacity_snapshot(),
            "task_store": _store(request).stats(),
        },
        # LLM 日成本预算（MOSS_LLM_DAILY_BUDGET_CNY，默认 20 元）：
        # by_source 直接回答"钱花在哪了" —— 实测投研 Agent 合计不到 8.62 元，
        # 而主线相关性打分一天能吃掉 487 元。
        "llm_budget": get_budget().snapshot(),
    }


@router.get("/agents/meta")
async def agents_meta() -> dict:
    """Agent展示元数据：id→中文名/层级 + 置信度枚举中文映射（供前端本地化展示）。"""
    from src.core.agent_meta import CONFIDENCE_ZH, agent_meta_table

    return {"agents": agent_meta_table(), "confidence_zh": CONFIDENCE_ZH}
