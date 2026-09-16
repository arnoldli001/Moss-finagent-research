"""投研任务路由：POST analyze（异步）+ GET 状态轮询。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.runtime import Runtime, agent_health
from src.api.tasks import TaskStore, new_task_id
from src.core.cancel import CancellationToken, TaskCancelledError
from src.core.config import get_settings
from src.infrastructure.connectors.security_resolver import resolve_stock
from src.orchestration.supervisor import plan_run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["research"])

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


@router.post("/research/analyze", status_code=202)
async def submit_analyze(body: AnalyzeRequest, request: Request) -> dict:
    """提交投研分析任务（异步执行，返回task_id供轮询）。

    结果缓存：相同 query+type+target 在 10 分钟内直接返回缓存结果，
    不重跑 LangGraph 管线。force_refresh=true 可绕过缓存。
    """
    runtime = _runtime(request)
    store = _store(request)

    # ====== 结果缓存检查 ======
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

    task_id = new_task_id()

    # 个股标的解析：中文简称/问句中的名称 → 6位代码（如"中际旭创"→300308）。
    # 解析失败保留原值，由下游按"数据不足"诚实处理，不阻断任务。
    target = (body.target or "").strip()
    target_display = target
    if body.analysis_type == "stock":
        try:
            candidate = target or body.query
            resolved = await resolve_stock(candidate)
        except Exception as exc:  # noqa: BLE001 名称解析为增强能力，失败不阻断
            logger.warning("证券名称解析失败(%s): %s", candidate[:40], exc)
            resolved = None
        if resolved:
            target = resolved[0]
            target_display = resolved[1] or target_display or target

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
        "plan": [], "raw_points": [], "cleaned_points": [],
        "validated_points": [], "validation_report": {}, "storage_stats": {},
        "info_items": body.info_items, "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None, "progress": [],
        "cancellation_token": cancel_token,
    }
    # 暴露state引用，running时前端可轮询agent_messages
    store.set_live_state(task_id, state)
    # 节点内可通过cancel_token.push_progress/push_messages实时更新live_state
    cancel_token.live_state = state

    async def _run() -> None:
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
                _RESULT_CACHE[qhash] = (
                    time.time() + _RESULT_CACHE_TTL, cache_result)
                logger.info("结果缓存写入(qhash=%s, TTL=%ds)",
                            qhash, _RESULT_CACHE_TTL)
        except TaskCancelledError:
            store.update(task_id, status="cancelled", error="用户主动停止任务")
        except asyncio.CancelledError:
            store.update(task_id, status="cancelled", error="用户主动停止任务")
            raise
        except Exception as exc:  # noqa: BLE001 任务级兜底
            logger.exception("research task failed: %s", task_id)
            store.update(task_id, status="failed", error=str(exc))
        finally:
            store.clear_live_state(task_id)

    handle = asyncio.create_task(_run())
    store.register_handle(task_id, handle)
    return {"task_id": task_id, "trace_id": task_id, "status": "queued",
            "plan": plan["agents"]}


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


@router.get("/health")
async def health(request: Request) -> dict:
    """聚合健康检查：Agent + 模型网关 + 数据库 + 审计链 + 数据源健康度。"""
    import httpx

    from src.api.data_health import build_data_health
    from src.infrastructure.repositories.audit_chain import ChainVerifier

    def _data_health(runtime_obj):
        """数据源健康度（失败只降级为空，绝不让 /health 500）。"""
        try:
            return build_data_health(runtime_obj)
        except Exception as exc:  # noqa: BLE001 健康检查本身不能崩
            return {"available": False, "error": str(exc)[:200]}
    runtime = _runtime(request)
    settings = get_settings()
    agents = agent_health(runtime.agents)

    ollama_status = "unknown"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(settings.ollama_base_url)
            ollama_status = "connected" if resp.status_code == 200 else "degraded"
    except (httpx.HTTPError, OSError):
        ollama_status = "unreachable"

    chain = ChainVerifier(f"{settings.llm_audit_dir}/audit_chain.jsonl").verify()

    # 数据源真实状态：连接器能力来自注册表（非网络谎报），存储做功能探测
    import importlib.util

    from src.infrastructure.repositories.cached_repo import CachedRepository

    connectors = []
    capabilities = runtime.backend.get_capabilities()
    for cap in capabilities.get("routes", []):
        name = cap.get("name", "unknown")
        if cap.get("simulated"):
            status = "simulated"
        elif name.lower().startswith("akshare"):
            status = "ready" if importlib.util.find_spec("akshare") else "not_installed"
        else:
            status = "configured"
        connectors.append({
            "name": name,
            "status": status,
            "simulated": bool(cap.get("simulated", False)),
            "indicators": cap.get("indicators", []),
        })

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
                   "error": str(exc)}

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
            "scheduled_job": "event_alert_daily",
            "ws_endpoint": "/api/v1/ws/alerts",
        },
        "data_sources": {
            "connectors": connectors,
            "storage": storage,
            "redis_cache": redis_cache,
            # 数据源健康度：能力矩阵 + 实测延迟（做T模块的真实调用记录）+ Tushare 覆盖。
            # **必须放到线程里**：它要遍历 3.5 万个分区清单 + 查仓库九表（首次约 5 秒），
            # 而这是 CPU/IO 密集的同步代码 —— 直接在事件循环里跑会阻塞**所有**并发请求
            # （实测旧实现对 14GB 库做 COUNT(*) 时 /health 卡到 300 秒超时，
            #  同一时间做T面板也跟着卡）。
            "health": await asyncio.to_thread(_data_health, runtime),
        },
        "model_gateway": {
            "ollama": ollama_status,
            "deepseek": "configured" if settings.deepseek_api_key else "not_configured",
        },
        "audit_chain": {"valid": chain["valid"], "records": chain["count"]},
    }


@router.get("/agents/meta")
async def agents_meta() -> dict:
    """Agent展示元数据：id→中文名/层级 + 置信度枚举中文映射（供前端本地化展示）。"""
    from src.core.agent_meta import CONFIDENCE_ZH, agent_meta_table

    return {"agents": agent_meta_table(), "confidence_zh": CONFIDENCE_ZH}
