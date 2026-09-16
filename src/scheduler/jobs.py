"""定时作业执行器：作业函数同时服务Celery Beat（分布式）与API手动触发（进程内）。

幂等性：快照作业复用研究图，A04存储按(指标,期别,内容哈希)去重，重复执行零新增；
进程内对同作业加asyncio锁，防止Beat/手动触发重叠执行。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from src.core.config import get_settings
from src.domain.alerts.service import ScanInProgressError
from src.scheduler.registry import JobSpec, get_job
from src.scheduler.run_log import RunLog

_job_locks: dict[str, asyncio.Lock] = {}

# 申万行业估值快照指标列表（工作日16:00采集，积累历史序列用于分位计算）
_SW_VALUATION_INDICATORS = [
    "ind:sw_first_pe_ttm:all", "ind:sw_first_pb:all",
    "ind:sw_second_pe_ttm:all", "ind:sw_second_pb:all",
    "ind:sw_third_pe_ttm:all", "ind:sw_third_pb:all",
    "ind:sw_third_pe_static:all", "ind:sw_third_dividend_yield:all",
]

# 渗透率核心赛道列表
_PENETRATION_TRACKS = [
    "新能源汽车", "新能源商用车", "AI大模型应用", "人形机器人",
    "固态电池", "光伏发电", "半导体国产替代", "HBM存储",
]

# 盘中实时快照（每5分钟）：成交额与全A换手率
_INTRADAY_LIQUIDITY_INDICATORS = [
    "mkt:turnover:total", "mkt:turnover_rate:all_a",
]
# 收盘后日频快照：历史序列+两融+北向+指数估值分位
_DAILY_LIQUIDITY_INDICATORS = [
    "mkt:turnover:total", "mkt:turnover:hist",
    "mkt:turnover_rate:all_a", "mkt:turnover_rate:hist",
    "mkt:margin_balance", "mkt:margin_balance:hist",
    "mkt:north_flow", "idx_val:snapshot:all",
]
# 科创板/创业板盘中实时快照：三指数实时成交额
_BOARD_INTRADAY_INDICATORS = ["mkt:cybkcb:turnover:all"]
# 科创板/创业板收盘日频快照：成交额历史序列+板块估值分位+个股截面
_BOARD_DAILY_INDICATORS = [
    "mkt:cybkcb:turnover:all", "mkt:cybkcb:turnover_hist",
    "mkt:cybkcb:val:all", "mkt:cybkcb:spot_summary",
]
# 科技行业真实产业数据（WSTS销售额同比/统计局集成电路产量同比/中证全指半导体PE-TTM）
_TECH_INDUSTRY_INDICATORS = [
    "ind:半导体销售额同比", "ind:芯片出货量同比", "ind:科技行业PE(TTM)",
]


def _lock_for(job_name: str) -> asyncio.Lock:
    if job_name not in _job_locks:
        _job_locks[job_name] = asyncio.Lock()
    return _job_locks[job_name]


def build_initial_state(
    analysis_type: str, target: str, query: str
) -> dict[str, Any]:
    task_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    return {
        "task_id": task_id, "tenant_id": "tenant_001",
        "user_query": query, "analysis_type": analysis_type,
        "target": target, "plan": [], "raw_points": [], "cleaned_points": [],
        "validated_points": [], "validation_report": {}, "storage_stats": {},
        "info_items": [], "verified_items": {}, "extracted_events": {},
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "final_report": None, "progress": "", "cancellation_token": None,
    }


async def _graph_snapshot(runtime: Any, spec: JobSpec) -> tuple[int, list[str]]:
    """跑一次或多次研究图，返回(处理数据点数, 错误列表)。"""
    params = spec.params
    targets = params.get("targets") or [params.get("target", "")]
    total_points, errors = 0, []
    for target in targets:
        query = f"[定时]{spec.description}：{target}" if target else f"[定时]{spec.description}"
        state = build_initial_state(params["analysis_type"], target, query)
        final = await runtime.graph.ainvoke(state)
        stats = final.get("storage_stats") or {}
        total_points += int(stats.get("total", len(final.get("raw_points", []))))
        errors.extend(final.get("errors", []))
    return total_points, errors


async def execute_job(
    runtime: Any, job_name: str, run_log: RunLog, trigger: str = "schedule"
) -> dict[str, Any]:
    """执行一个注册作业并落运行记录；任何异常记为failed（不向调用方抛出）。"""
    spec = get_job(job_name)
    if trigger == "schedule" and run_log.is_paused(job_name):
        record = run_log.start(job_name, trigger)
        return run_log.finish(
            record, status="skipped",
            error_message="连续失败已达阈值，作业暂停等待人工介入",
        )

    async with _lock_for(job_name):
        record = run_log.start(job_name, trigger)
        try:
            if spec.kind == "graph_snapshot":
                processed, errors = await _graph_snapshot(runtime, spec)
                if errors:
                    return run_log.finish(
                        record, status="failed", records_processed=processed,
                        error_message="; ".join(errors)[:500])
                return run_log.finish(
                    record, status="success", records_processed=processed)
            if spec.kind == "run_log_cleanup":
                removed = run_log.prune(get_settings().scheduler_run_log_ttl_days)
                return run_log.finish(
                    record, status="success", records_processed=removed)
            if spec.kind == "industry_valuation_snapshot":
                processed, errors = await _sw_valuation_snapshot(runtime)
                if errors:
                    return run_log.finish(
                        record, status="failed", records_processed=processed,
                        error_message="; ".join(errors)[:500])
                return run_log.finish(
                    record, status="success", records_processed=processed)
            if spec.kind == "penetration_rate_update":
                processed, errors = await _penetration_rate_update(runtime)
                if errors:
                    return run_log.finish(
                        record, status="failed", records_processed=processed,
                        error_message="; ".join(errors)[:500])
                return run_log.finish(
                    record, status="success", records_processed=processed)
            if spec.kind == "strategy_cases_fetch":
                processed, detail = await _strategy_cases_fetch(spec)
                return run_log.finish(
                    record, status="success", records_processed=processed,
                    error_message=detail[:500])
            if spec.kind == "market_intraday_snapshot":
                processed, errors = await _collect_through_pipeline(
                    runtime, _INTRADAY_LIQUIDITY_INDICATORS, "liq_intra")
                return _finish_collection(record, run_log, processed, errors)
            if spec.kind == "market_daily_snapshot":
                processed, errors = await _collect_through_pipeline(
                    runtime, _DAILY_LIQUIDITY_INDICATORS, "liq_daily")
                return _finish_collection(record, run_log, processed, errors)
            if spec.kind == "board_intraday_snapshot":
                processed, errors = await _collect_through_pipeline(
                    runtime, _BOARD_INTRADAY_INDICATORS, "board_intra")
                return _finish_collection(record, run_log, processed, errors)
            if spec.kind == "board_daily_snapshot":
                processed, errors = await _collect_through_pipeline(
                    runtime, _BOARD_DAILY_INDICATORS, "board_daily")
                return _finish_collection(record, run_log, processed, errors)
            if spec.kind == "tech_industry_snapshot":
                indicators = spec.params.get("indicators") or list(
                    _TECH_INDUSTRY_INDICATORS)
                processed, errors = await _collect_through_pipeline(
                    runtime, indicators, "tech_ind")
                return _finish_collection(record, run_log, processed, errors)
            if spec.kind == "fedwatch_daily":
                processed, errors = await _collect_through_pipeline(
                    runtime, ["fed:rate_prob:next"], "fedwatch")
                # CME外网不可达时连接器降级为空结果（非错误），作业仍记成功
                if not processed and not errors:
                    return run_log.finish(
                        record, status="success", records_processed=0,
                        error_message="CME/FRED不可达，本次为数据缺口（已降级）")
                return _finish_collection(record, run_log, processed, errors)
            if spec.kind == "event_alert_scan":
                service = getattr(runtime, "event_service", None)
                if service is None:
                    return run_log.finish(
                        record, status="failed",
                        error_message="event_service未装配到Runtime")
                try:
                    scan = await service.run(trigger)
                except ScanInProgressError as exc:
                    # 三入口共享service锁：冲突时跳过本轮，不记失败
                    return run_log.finish(
                        record, status="skipped",
                        error_message=f"已有事件扫描在执行，本次跳过：{exc}")
                detail = (
                    f"扫描{scan.scanned} 新增{scan.new_events} "
                    f"告警{scan.alerts_created} 级别{scan.by_level}")
                if scan.status == "failed":
                    return run_log.finish(
                        record, status="failed",
                        records_processed=scan.new_events,
                        error_message=(detail + " " + "; ".join(scan.errors))[:500])
                if scan.status == "partial":
                    return run_log.finish(
                        record, status="partial",
                        records_processed=scan.alerts_created,
                        error_message=(detail + " 部分源失败: "
                                       + "; ".join(scan.errors))[:500])
                return run_log.finish(
                    record, status="success",
                    records_processed=scan.alerts_created,
                    error_message=detail if scan.data_gaps else "")
            if spec.kind == "intraday_t_scan":
                service = getattr(runtime, "intraday", None)
                if service is None:
                    return run_log.finish(
                        record, status="failed",
                        error_message="做T辅助服务未装配到Runtime（检查 configs/intraday.yaml）")
                try:
                    processed, detail = await _intraday_t_scan(service)
                except Exception as exc:  # noqa: BLE001 盘中扫描失败只记失败，不影响其他作业
                    return run_log.finish(
                        record, status="failed",
                        error_message=f"做T扫描失败: {str(exc)[:400]}")
                return run_log.finish(
                    record, status="success", records_processed=processed,
                    error_message=detail[:500])
            if spec.kind == "dynamic_collection":
                indicators = spec.params.get("indicators") or []
                processed, errors = await _collect_through_pipeline(
                    runtime, indicators, "dyn_col")
                return _finish_collection(record, run_log, processed, errors)
        except Exception as exc:  # noqa: BLE001 作业级兜底：失败入记录供告警/重试
            return run_log.finish(record, status="failed", error_message=str(exc)[:500])

        return run_log.finish(
            record, status="failed", error_message=f"未知作业类型 {spec.kind}")


async def _strategy_cases_fetch(spec: Any) -> tuple[int, str]:
    """抓取开源策略案例（GitHub / arXiv / CSDN）并增量入库。

    放到调度器里而不是让前端触发：抓取要访问外部站点，属于"后台慢活"，
    不该占用户请求；而且**每日/每周固定跑**才能让面板保持新鲜。

    单个源失败不影响其它源（`crawl` 内部已隔离），返回 (新增条数, 说明)。
    """
    from src.quant.strategy_cases import DISCLAIMER, crawl, load_sources
    from src.quant.warehouse import strategy_case_store

    wanted = set((spec.params or {}).get("sources") or [])
    sources = [item for item in load_sources()
               if item.enabled and (not wanted or item.name in wanted)]
    if not sources:
        return 0, "没有启用的抓取源"
    cases, results = await asyncio.to_thread(crawl, sources)
    if not cases:
        failures = "；".join(
            f"{item['source']}: {item.get('error', '')[:40]}"
            for item in results if not item.get("ok"))
        # 抓不到就如实报失败，不假装成功 —— 否则面板会一直停在旧内容上而无人察觉
        return 0, f"全部源均未返回内容（{failures[:200]}）"
    written = strategy_case_store(disclaimer=DISCLAIMER).upsert(cases)
    detail = "；".join(
        f"{item['source']} {item.get('count', 0)}条" for item in results
        if item.get("ok"))
    skipped = [item["source"] for item in results if item.get("skipped")]
    return written.get("inserted", 0), (
        f"入库{written.get('inserted', 0)}条（库内共 {written.get('total', 0)} 条），"
        f"{detail}" + (f"；跳过 {','.join(skipped)}" if skipped else ""))


async def _intraday_t_scan(service: Any) -> tuple[int, str]:
    """做T辅助盘中扫描：重算自选标的打分与信号，正式信号自动推送。

    返回 (触发信号数, 说明文本)。推送由 IntradayService.snapshot 内部完成
    （仅实心三角/止损，多渠道 + 同标的同方向冷却），本作业只负责定时触发与留痕。
    """
    items = await service.watchlist()
    triggered = [
        item for item in items
        if item.signal_strength in ("solid", "forced_exit")
    ]
    kind_labels = {"low_buy": "低吸", "high_sell": "高抛", "stop_loss": "止损"}
    lines = [
        f"{item.code}{(item.name or '')}:"
        f"{kind_labels.get(item.signal_kind, item.signal_kind)}"
        f" 总分{item.total_score:+.1f}"
        for item in triggered if item.total_score is not None
    ]
    detail = (
        f"扫描{len(items)}只，触发正式信号{len(triggered)}只"
        + (f"（{'；'.join(lines)}）" if lines else "")
    )
    return len(triggered), detail


async def _sw_valuation_snapshot(runtime: Any) -> tuple[int, list[str]]:
    from src.core.models import AgentInput

    total_points, errors = 0, []
    collector = runtime.agents.get("A01_data_collector")
    if collector is None:
        return 0, ["A01_data_collector未注册"]
    for indicator in _SW_VALUATION_INDICATORS:
        try:
            output = await collector.execute(AgentInput(
                task_id=f"sw_val_{int(time.time())}",
                tenant_id="tenant_001",
                payload={"indicator": indicator},
            ))
            total_points += len(output.result.get("data_points", []))
        except Exception as exc:  # noqa: BLE001 单指标失败不阻断其余
            errors.append(f"{indicator}: {exc}")
    return total_points, errors


async def _penetration_rate_update(runtime: Any) -> tuple[int, list[str]]:
    """更新核心赛道渗透率数据，通过A01采集后入库。"""
    from src.core.models import AgentInput

    total_points, errors = 0, []
    collector = runtime.agents.get("A01_data_collector")
    if collector is None:
        return 0, ["A01_data_collector未注册"]
    for track in _PENETRATION_TRACKS:
        indicator = f"ind:penetration:{track}"
        try:
            output = await collector.execute(AgentInput(
                task_id=f"pen_rate_{int(time.time())}",
                tenant_id="tenant_001",
                payload={"indicator": indicator},
            ))
            total_points += len(output.result.get("data_points", []))
        except Exception as exc:  # noqa: BLE001 单赛道失败不阻断
            errors.append(f"{track}: {exc}")
    return total_points, errors


def _finish_collection(
    record: dict[str, Any], run_log: RunLog, processed: int, errors: list[str]
) -> dict[str, Any]:
    """多指标采集结果落状态：全成success / 部分成功partial / 全失败failed。

    partial（有入库且有失败指标，如东财限流但腾讯成交额正常）不触发
    Celery重试与连续失败自动暂停，失败明细仍写入error_message。
    """
    if not errors:
        return run_log.finish(
            record, status="success", records_processed=processed)
    if processed > 0:
        return run_log.finish(
            record, status="partial", records_processed=processed,
            error_message="部分指标失败: " + "; ".join(errors)[:450])
    return run_log.finish(
        record, status="failed", records_processed=0,
        error_message="; ".join(errors)[:500])


async def _collect_through_pipeline(
    runtime: Any, indicators: list[str], prefix: str
) -> tuple[int, list[str]]:
    """定时采集标准链路：A01采集→A02清洗→A03校验→A04入库（幂等去重）。

    单指标失败不阻断其余指标；返回(入库数据点总数, 错误列表)。
    """
    from src.core.models import AgentInput

    agents = runtime.agents
    required = ("A01_data_collector", "A02_data_cleaner",
                "A03_data_validator", "A04_data_storage")
    missing = [a for a in required if agents.get(a) is None]
    if missing:
        return 0, [f"数据管线Agent未注册: {','.join(missing)}"]

    total_points, errors = 0, []
    ts = int(time.time())
    for indicator in indicators:
        task_id = f"{prefix}_{ts}_{uuid.uuid4().hex[:6]}"
        try:
            collected = await agents["A01_data_collector"].execute(AgentInput(
                task_id=task_id, tenant_id="tenant_001",
                payload={"indicator": indicator},
            ))
            raw = collected.result.get("data_points", [])
            if not raw:
                continue
            cleaned_out = await agents["A02_data_cleaner"].execute(AgentInput(
                task_id=task_id, tenant_id="tenant_001",
                payload={"data_points": raw},
            ))
            validated_out = await agents["A03_data_validator"].execute(AgentInput(
                task_id=task_id, tenant_id="tenant_001",
                payload={"data_points": cleaned_out.result.get("data_points", [])},
            ))
            stored = await agents["A04_data_storage"].execute(AgentInput(
                task_id=task_id, tenant_id="tenant_001",
                payload={"data_points": validated_out.result.get("data_points", [])},
            ))
            stats = stored.result.get("storage_stats") or {}
            total_points += int(stats.get("total", 0))
        except Exception as exc:  # noqa: BLE001 单指标失败不阻断
            errors.append(f"{indicator}: {exc}")
    return total_points, errors
