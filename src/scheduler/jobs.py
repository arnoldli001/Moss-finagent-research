"""定时作业执行器：作业函数同时服务Celery Beat（分布式）与API手动触发（进程内）。

幂等性：快照作业复用研究图，A04存储按(指标,期别,内容哈希)去重，重复执行零新增；
进程内对同作业加asyncio锁，防止Beat/手动触发重叠执行。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from src.core.config import get_settings
from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_LOG,
    BRIEF_TIGHT,
    brief,
)
from src.core.trading_session import minutes_of, parse_hhmm
from src.domain.alerts.service import ScanInProgressError
from src.scheduler.registry import JobSpec, get_job
from src.scheduler.run_log import RunLog

logger = logging.getLogger(__name__)

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
            if spec.kind == "data_retention":
                from src.infrastructure.retention_service import run_retention

                outcome = await run_retention(get_settings())
                total = int(outcome["points_deleted"]) + int(outcome["news_deleted"])
                detail = (
                    f"数据点删除{outcome['points_deleted']}"
                    f"（截止{outcome['points_cutoff']}），"
                    f"新闻删除{outcome['news_deleted']}"
                    + ("；错误:" + ",".join(outcome["errors"])
                       if outcome["errors"] else "")
                )
                if outcome["errors"]:
                    status = "partial" if total else "failed"
                else:
                    status = "success"
                return run_log.finish(
                    record, status=status, records_processed=total,
                    error_message=detail[:500])
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
            if spec.kind == "mainline_daily":
                processed, detail = await _mainline_daily(spec)
                if detail.get("failed"):
                    return run_log.finish(
                        record, status="failed", records_processed=processed,
                        error_message=str(detail.get("message") or "")[:500])
                return run_log.finish(
                    record, status="success", records_processed=processed,
                    error_message=str(detail.get("message") or "")[:500])
            if spec.kind == "mainline_calibrate":
                processed, detail = await _mainline_calibrate()
                return run_log.finish(
                    record, status="success", records_processed=processed,
                    error_message=str(detail)[:500])
            if spec.kind == "mainline_etf_flow":
                processed, detail = await _mainline_etf_flow()
                return run_log.finish(
                    record,
                    status="failed" if detail.get("failed") else "success",
                    records_processed=processed,
                    error_message=str(detail.get("message") or "")[:500])
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
                        error_message=f"做T扫描失败: {brief(exc, BRIEF_LOG)}")
                return run_log.finish(
                    record, status="success", records_processed=processed,
                    error_message=detail[:500])
            if spec.kind == "quant_select":
                service = getattr(runtime, "quant_select", None)
                if service is None:
                    return run_log.finish(
                        record, status="failed",
                        error_message="量化选股服务未装配到Runtime")
                processed, detail = await _quant_select(service, spec)
                status = "success" if not detail.startswith("失败") else "failed"
                return run_log.finish(
                    record, status=status, records_processed=processed,
                    error_message="" if status == "success" else detail[:500])
            if spec.kind == "quant_data_sync":
                processed, detail = await _quant_data_sync(spec)
                status = "success" if not detail.startswith("失败") else "failed"
                return run_log.finish(
                    record, status=status, records_processed=processed,
                    error_message="" if status == "success" else detail[:500])
            if spec.kind == "model_retrain":
                processed, detail = await _model_retrain(spec)
                status = "success" if not detail.startswith("失败") else "failed"
                return run_log.finish(
                    record, status=status, records_processed=processed,
                    error_message="" if status == "success" else detail[:500])
            if spec.kind == "crowding_metrics":
                processed, detail = await _crowding_metrics(spec)
                status = "success" if not detail.startswith("失败") else "failed"
                return run_log.finish(
                    record, status=status, records_processed=processed,
                    error_message="" if status == "success" else detail[:500])
            if spec.kind == "auction_tick_capture":
                processed, detail = await _auction_tick_capture(spec)
                status = "success" if not detail.startswith("失败") else "failed"
                return run_log.finish(
                    record, status=status, records_processed=processed,
                    error_message="" if status == "success" else detail[:500])
            if spec.kind == "intel_zsxq_collect":
                processed, detail = await _intel_zsxq_collect(spec)
                status = "success" if not detail.startswith("失败") else "failed"
                return run_log.finish(
                    record, status=status, records_processed=processed,
                    error_message="" if status == "success" else detail[:500])
            if spec.kind == "intel_token_alert":
                processed, detail = await _intel_token_alert(spec)
                status = "success" if not detail.startswith("失败") else "failed"
                return run_log.finish(
                    record, status=status, records_processed=processed,
                    error_message="" if status == "success" else detail[:500])
            if spec.kind == "dynamic_collection":
                indicators = spec.params.get("indicators") or []
                processed, errors = await _collect_through_pipeline(
                    runtime, indicators, "dyn_col")
                return _finish_collection(record, run_log, processed, errors)
        except Exception as exc:  # noqa: BLE001 作业级兜底：失败入记录供告警/重试
            return run_log.finish(record, status="failed", error_message=brief(exc, BRIEF_LOG))

        return run_log.finish(
            record, status="failed", error_message=f"未知作业类型 {spec.kind}")


# ======================================================================
# 情报中心：两个任务（**在 registry 里声明过，但这里一直没有分支**）
# ======================================================================
#
# ⚠️ 实测发现的硬阻断（2026-09-25）：`JOB_REGISTRY` 里声明了
# `intel_zsxq_collect` 与 `intel_token_alert`，而本模块的分发链里**没有**
# 对应的 `if spec.kind == ...` 分支 —— 于是它们每次触发都走到最底下那句
# `未知作业类型`，记一条 failed 就结束了。
#
# 后果正好命中用户点名的两条需求：
#   · "建议 2 小时一次"的采集**从来没跑过**，情报流只有实时接口在取
#   · token 7/14 天过期**没有任何提醒**，只会在前端静默变成"暂无更新"
#
# 教训：声明与实现分离的两张表必然漂移。所以下面除了补分支，还加了
# `tests/unit/test_scheduler.py` 的覆盖性断言（registry 里每个 kind
# 都必须能在 jobs.py 里找到分支）。


async def _intel_zsxq_collect(spec: Any) -> tuple[int, str]:
    """知识星球**增量**采集（水位线，2 小时一次）。

    与 `/intel/feed` 的区别：接口那条路是"用户打开页面就现取"，
    这条是"定时把内容追平并落水位线"。两者共用同一个增量游标，
    所以谁先跑都不会重复处理（`content_hash` 去重 + 严格水位线）。

    只在成功时推进水位线 —— 失败推进会导致**永久丢内容**。
    """
    from src.domain.intel.service import watchlist_codes
    from src.infrastructure.connectors.zsxq_incremental import (
        fetch_incremental, save_watermark,
    )

    max_fetch = int(spec.params.get("max_fetch") or 30)
    max_pages = int(spec.params.get("max_pages") or 5)

    def _work() -> Any:
        return fetch_incremental(max_fetch=max_fetch, max_pages=max_pages)

    try:
        inc = await asyncio.to_thread(_work)
    except Exception as exc:  # noqa: BLE001 含 TokenExpired
        from src.core.redaction import sanitize_error
        from src.infrastructure.connectors.zsxq_source import TokenExpired

        if isinstance(exc, TokenExpired):
            # 授权过期**不是故障**，是待办（管理员侧另有邮件提醒）。
            # 记 `partial` 而不是 `failed`：没有出错，只是需要人重新授权。
            return 0, "失败：授权已失效（请重新授权，见 intel_token_alert 提醒）"
        logger.warning("情报采集失败：%s", sanitize_error(exc))
        return 0, f"失败：{sanitize_error(exc)}"

    if inc.new_count and inc.watermark:
        await asyncio.to_thread(save_watermark, inc.watermark,
                                note="scheduler")
    detail = (f"取回 {inc.new_count} 条（{inc.pages_used} 页），"
              f"水位线 {inc.watermark[:16] if inc.watermark else '-'}")
    if inc.truncated:
        detail += "；命中上限，下次继续追平"
    # 自选清单是研报采集的输入，顺手作为健康信号记一句 ——
    # 清单为空时"仅研报"筛选会永远是空的，这个日志能一眼看出原因
    wl = watchlist_codes()
    detail += f"；研报关注标的 {len(wl)} 个"
    return inc.new_count, detail


async def _intel_token_alert(spec: Any) -> tuple[int, str]:
    """数据源授权到期检查：满 5 天起邮件提醒管理员。

    用户口径（2026-09-25）："过期了不要在前端显示故障，而是在管理员界面
    通知过期，或者写个脚本定时提醒我更新。"

    所以这个任务的产出是**邮件 + 管理员提示**，用户侧无感 ——
    前端永远只显示"研究笔记暂无更新"。

    ⚠️ 阈值（5 天预警 / 7 天失效）由 `token_alerts` 模块自己的常量决定，
    **不从这个任务的 `params` 传进去** —— 第一版按 `warn_after_days=` 传，
    而那个函数的签名是 `(*, root, dry_run, refresh_cmd, force)`，
    会直接 `TypeError`。两处各写一套阈值必然漂移，所以只留一处。
    """
    from src.domain.intel.token_alerts import check_and_notify

    try:
        # 它自己是 `async def`，不要再套 `to_thread`（那会返回协程对象而不是结果）
        outcome = await check_and_notify()
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        logger.warning("授权到期检查失败：%s", sanitize_error(exc))
        return 0, f"失败：{sanitize_error(exc)}"

    if not isinstance(outcome, dict):
        return 0, f"授权检查完成（返回 {type(outcome).__name__}）"
    state = str(outcome.get("state") or "unknown")
    if outcome.get("notified"):
        return 1, f"授权状态 {state}，已发提醒（收件人见 TOKEN_ALERT_EMAIL_TO）"
    skip = str(outcome.get("skipped") or "")
    return 0, f"授权状态 {state}，未发提醒" + (f"（{skip}）" if skip else "")


async def _auction_tick_capture(spec: Any) -> tuple[int, str]:
    """涨停股竞价过程采集（防过期）—— 用户口径 2026-09-22。

    竞价过程（09:15~09:25 每 3 秒）在 QMT 服务器上**只保留约 1 个月**，
    而 9:25 那一刻的完整序列**只有当天能取**（生产自己也是靠盘中实时推、
    当天落盘成录像带 `auction_snapshot` 才留下的）。所以这是个"过期就永久丢失"
    的数据，必须每个交易日跑，并且每次都要**回补最近的缺口**——哪天机器没开、
    哪天服务没起，下次运行都要把它补回来。

    作业本身幂等：只采"当日涨停名单里还没有 tick 文件记录的"那些代码。

    Returns: (采集到的股票日数, 说明)
    """
    from scripts.daily_auction_tick import run as capture_run

    params = spec.params or {}
    lookback = int(params.get("lookback", 5) or 5)
    scope = str(params.get("scope", "candidates") or "candidates")
    frm = str(params.get("from", "") or "")
    to = str(params.get("to", "") or "")

    summary = await asyncio.to_thread(
        capture_run, frm, to, lookback=lookback, scope=scope,
        refresh=bool(params.get("refresh", False)))

    if summary.get("note"):
        # 没有交易日（例如调休/节假日）不算失败 —— 如实报 0 条
        return 0, f"无需采集：{summary['note']}"

    saved = sum(int(r.get("saved") or 0) for r in summary.get("results", []))
    gap_days = [r for r in summary.get("results", []) if r.get("saved") is not None
                and r.get("note") != "已齐"]
    failed = sum(int(r.get("failed") or 0) for r in summary.get("results", []))
    detail = (f"区间 {summary['range'][0]}~{summary['range'][1]}（{summary['days']} 个交易日）；"
              f"有缺口需采的天数 {len(gap_days)}；本次落盘 {saved} 只"
              + (f"；失败 {failed} 只" if failed else ""))
    if failed and failed > saved:
        detail = "失败：" + detail          # 大面积失败按失败上报，免得静默停更
    return saved, detail


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
    if items and all(item.total_score is None for item in items):
        # 冷启动且还没有任何缓存时，`watchlist()` 给的是**占位列表**
        # （只有代码/名称/板块，没有分数与价格，见 IntradayService._placeholder_watchlist）
        # —— 据此报"触发 0 个信号"是**假结论**：用户会以为扫过了、今天没信号。
        # 如实说"这轮没数据"并跳过，下一轮（5 分钟后）缓存通常已经补齐。
        return 0, ("自选池尚无打分数据（首轮整表重算未完成或数据源不可用），"
                   "本轮跳过，不据此报「0 触发」")
    triggered = [
        item for item in items
        if item.signal_strength in ("solid", "forced_exit")
    ]
    kind_labels = {"low_buy": "回踩", "high_sell": "冲高", "stop_loss": "止损"}
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


async def _quant_select(service: Any, spec: Any) -> tuple[int, str]:
    """量化选股作业：在时间窗口内用 3 档模型跑一轮，结果进「量化选股」模块。

    返回 `(选中只数, 说明)`；说明以 `失败` 开头时会被记成 failed。

    ## 两个必须的守卫

    1. **窗口判定**：cron 是 `*/5 9 * * 1-5`（分钟粒度粗一点更抗"服务当时没起来"），
       真正的 09:25~09:45 / 14:45~15:00 边界在这里判 —— 窗口外直接 skipped，
       不留下"跑了但没到点"的运行记录；
    2. **当日去重**：窗口有 20 分钟、cron 每 5 分钟醒一次，不去重会连跑 4 轮
       （每轮几十秒且会写 4 条结果，前端看到重复批次）。
    """
    from src.intraday.auto_select import is_trading_day
    from src.quant.quant_select_service import _today_compact

    params = spec.params or {}
    window = str(params.get("window") or "manual")
    start = parse_hhmm(params.get("start") or "")
    end = parse_hhmm(params.get("end") or "")
    now = datetime.now()

    if not is_trading_day(now):
        return 0, f"非交易日（{now:%Y-%m-%d}），跳过量化选股"

    current = minutes_of(now)
    if start is not None and end is not None and not (start <= current <= end):
        return 0, (f"不在选股窗口 {start // 60:02d}:{start % 60:02d}~"
                   f"{end // 60:02d}:{end % 60:02d}（当前 {now:%H:%M}），跳过")

    trade_date = _today_compact()
    if await service.has_run_today(trade_date, window):
        return 0, f"{trade_date} 的 {window} 窗口今天已跑过，跳过重复执行"

    run = await service.run(window=window, triggered_by="schedule")
    if run.error:
        return 0, f"失败：{run.error}"
    top = "、".join(f"{item.code}{(item.name or '')}" for item in run.items[:5])
    detail = (f"{run.trade_date} {window} 窗口选出 {run.selected} 只"
              f"（打分 {run.scored} 只，阈值 {run.threshold:.4f}，"
              f"模型 {run.model_detail or run.model_version}，{run.seconds:.0f}s）")
    if top:
        detail += f"；前几：{top}"
    return run.selected, detail


async def _quant_data_sync(spec: Any) -> tuple[int, str]:
    """行情仓库同步：把 `daily/daily_basic/stk_limit` 补齐到最近一个已收盘交易日。

    返回 `(新灌入行数, 说明)`；说明以 `失败` 开头时会被记成 failed。

    ## 为什么这是个**前置条件**而不是可选项

    量化选股、3 档模型的训练与回测全部读本地 Tushare 仓库
    （`moss_selector.adapters.RealMarketDataSource` → `QuantWarehouse`）。
    仓库**不会自己更新** —— 它只由 `scripts/quant_sync.py download` +
    `scripts/quant_warehouse.py ingest` 手工推进。

    实测事故（2026-09-18）：09:10 手动跑量化选股，选出来的日期是 20260915，
    因为仓库里最新就到 0915。选股链路没有错，它忠实地用了"手上最新的一天" ——
    但那已经是两天前的市场，而且**界面上看不出任何异常**（有分数、有阈值、
    有排名、有 20 只结果）。前一天没有同步，第二天的自动选股就全是旧数据。

    ## 三个实现要点

    1. **只补缺的那几天**：先问仓库/分区当前最新交易日，从它的下一天开始按交易日历
       取区间；没有缺口就直接返回，不做任何下载（幂等、可反复跑）。
    2. **下载完必须灌库**：`sync_daily` 只写 CSV/parquet 分区，
       不 ingest 的话仓库里还是旧数据 —— "下了但没入库"是最容易发生的一步漏。
    3. **单档失败不吞掉**：哪一档没补上都写进返回值，作业记录里能看见。

    ## ⚠️ 水位以**分区**为准，仓库不可用**不能**让整轮退出（2026-09-21 修）

    原来开头是：

        warehouse = QuantWarehouse()
        if not warehouse.available():
            return 0, "失败：本地量化仓库不可用"

    结果是一旦仓库文件出问题（实测 `warehouse.db` 报 `disk I/O error`），
    **连分区都不下载了**，整轮直接退出。

    但真正被读取的是**分区**：`warehouse.load_dataset` 是"数据库可用就用库、
    否则回退分区文件"（`prefer="db"` 只是优先级，不是硬依赖），
    竞价选股的「近 N 个交易日涨停次数」正好走这条回退路径。实际后果：

    - 分区停在 0917 → 竞价选股的 18 天窗口差一天 → 首板否决规则算不出数、静默不生效；
    - 作业记录里只有一句"仓库不可用"，看着像仓库的事，与竞价选股毫无关联
      （实测排查了很久才串起来）。

    现在：**下载永远照做**，水位取"仓库与分区里更新的那个"；
    灌库退化成**尽力而为**的一步 —— 失败只记进说明、不让作业失败，
    因为分区已经把数据交付出去了。

    ## ⚠️ 2026-09-23 又两处（用户问「收盘了为什么图上还没有今天」）

    1. **目标日被陈旧的交易日历缓存锁死**：`target` 只读本地 `trade_cal` 缓存，
       缓存停在 0922 → 每次都判「已是最新」→ 0923 的数据永远下不来。
       补 `_refresh_calendar_horizon()`：用**墙上时钟**顶一次日历（该函数里写了
       完整的循环依赖说明与实测证据）。
    2. **`index_daily` 走错入口**：它在作业清单里，却不在 `download.py` 的
       `DAILY_DATASETS` 里（指数是 5 个代码逐个拉的），`sync_daily` 直接
       `KeyError`；异常把循环打断在灌库之前 —— 5 档分区已下好、仓库一行没进。
       补 `_download_one()` 分流 + **逐档隔离**（一档失败不再拖垮整轮）。
    """
    import asyncio

    from src.quant.dataset_store import DatasetStore
    from src.quant.download import TushareDownloader
    from src.quant.tushare_source import TushareClient, resolve_token
    from src.quant.warehouse import QuantWarehouse

    datasets = list((spec.params or {}).get("datasets") or
                    ["daily", "daily_basic", "stk_limit", "moneyflow"])
    # ⚠️ 顺序不能换：先把交易日历的"视野"推到今天，`target` 才看得见今天。
    calendar_note = await _refresh_calendar_horizon()
    target = _latest_complete_trade_date()
    if not target:
        return 0, "失败：交易日历不可用，无法判断该补到哪一天（不做猜测）"

    warehouse = QuantWarehouse()
    try:
        warehouse_ok = bool(warehouse.available())
    except Exception as exc:  # noqa: BLE001 仓库异常不该挡住分区同步
        logger.warning("仓库可用性检查异常：%s", brief(exc, BRIEF_DEFAULT))
        warehouse_ok = False

    # 缺口判定固定用 `daily`（日内最先落仓的一档），**不能**用 datasets[0] 之外的
    # 滞后数据集：`moneyflow` 实测比 `daily` 晚约 2 个交易日才全，拿它判"是否最新"
    # 会让作业天天认为有缺口、反复从头补。按 daily 判缺口 + 每档各自幂等补，
    # 才既不做无用下载、也不会漏掉后面补齐的那几天资金流。
    # 水位取"仓库/分区里更新的那个"：仓库落后或不可用时分区说了算。
    warehouse_latest = ""
    if warehouse_ok:
        try:
            warehouse_latest = _warehouse_latest(warehouse, "daily")
        except Exception as exc:  # noqa: BLE001 读不到就退回分区水位
            logger.warning("读仓库最新交易日异常：%s", brief(exc, BRIEF_DEFAULT))
    partition_latest = _partition_latest("daily")
    latest = max([stamp for stamp in (warehouse_latest, partition_latest) if stamp]
                 or [""])

    notes: list[str] = []
    if calendar_note:
        notes.append(calendar_note)
    if not warehouse_ok:
        notes.append("仓库不可用（读取会自动回退到分区）")

    async def _ingest_gaps() -> tuple[int, list[str]]:
        """把「分区已经有、仓库还没有」的分区按**每档自己的水位**灌进库。

        返回 `(灌入行数, 失败说明)`；仓库不可用时返回 `(0, [])`。

        ## 为什么必须一档一算（2026-09-22 修）

        原来这里只有一句 `if warehouse_latest < partition_latest:`，
        然后用 `DatasetStore("daily").keys()` 算出 `pending`，再把这个 `pending`
        喂给**每一个**数据集。它默认了「所有数据集水位一致」，而事实正相反：
        `moneyflow` 比 `daily` 晚发布约 2 个交易日，`daily_basic` / `adj_factor`
        也各落后 1~3 天。实测当天就是：

            daily     仓库 0918 / 分区 0922  → pending = {0921, 0922}
            moneyflow 仓库 **0915**          → 只灌进 0921、0922

        0916~0918 这三天**永远进不来**，而作业每天如实记
        「已是最新（0922 ≥ 目标 0922）」—— 分区文件就躺在磁盘上，没人灌。

        ## 后果不是「少几天数据」这么轻

        `ml_board_flow` 由个股口径聚合而成（Σ 成分股 `net_mf_amount`），
        仓库 `moneyflow` 停在哪天，板块资金流就停在哪天。而**评分照跑不误**：
        实测 `ml_board_bar` 已到 0922、`ml_board_flow` 停在 0915，
        0916~0922 的分数全部把 0915 的资金流当最新值用，界面上有分数、
        有排名、有告警，看不出任何异常 —— 只有「最新日期」不对。

        代价：每档多扫一遍自己的分区目录（~2s）+ 读一次自己的水位，
        一天一次；远小于静默用旧数据的代价。灌库本身幂等，重复灌无害。
        """
        if not warehouse_ok:
            return 0, []
        total = 0
        failures: list[str] = []
        for dataset in datasets:
            try:
                floor = _warehouse_latest(warehouse, dataset)
                keys = [str(key) for key in DatasetStore(dataset).keys()]
            except Exception as exc:  # noqa: BLE001 读不到就跳过这一档
                failures.append(f"{dataset}({type(exc).__name__})")
                continue
            pending = sorted(key for key in keys if key > floor)
            if not pending:
                continue
            try:
                total += int(await asyncio.to_thread(
                    warehouse.ingest_dataset, dataset, keys=pending) or 0)
            except Exception as exc:  # noqa: BLE001 分区已交付，灌库失败不算作业失败
                failures.append(f"{dataset}({type(exc).__name__})")
                logger.warning("仓库同步：%s 灌库失败 %s",
                               dataset, brief(exc, BRIEF_DEFAULT))
        return total, failures

    if latest and latest >= target:
        # 已是最新：不做下载，但**仓库可能仍落后于分区**，按每档自己的缺口补灌。
        ingested, failures = await _ingest_gaps()
        detail = f"已是最新（{latest} ≥ 目标 {target}）"
        if ingested:
            detail += f"，补灌 {ingested} 行"
        notes.extend(f"{item} 灌库补做失败" for item in failures)
        if notes:
            detail += "；" + "、".join(notes)
        return ingested, detail

    try:
        client = TushareClient()
        # token 也走显式解析：没有 token 时 `TushareClient` 自己会报错，
        # 但那样报出来的是底层异常，这里先给出可执行的提示。
        resolve_token()
        downloader = TushareDownloader(client)
        days = await downloader.calendar(
            _next_day(latest) if latest else "20260101", target)
        if not days:
            return 0, f"交易日历里 {latest or '起点'}~{target} 没有交易日，无需同步"
    except Exception as exc:  # noqa: BLE001 网络/权限失败如实上报
        return 0, (f"失败：下载 {latest or '起点'}~{target} 失败 "
                   f"{type(exc).__name__}: {brief(exc, BRIEF_LOG)}")

    # ⚠️ **逐档隔离**：一档抛异常不能让整轮下载中断（2026-09-23 实测）——
    #    `index_daily` 走错了入口（见 `_download_one`），KeyError 把循环打断在灌库之前，
    #    结果是"5 个数据集的分区已经下好，仓库一行没进"，而作业记录只说下载失败。
    fetched: dict[str, int] = {}
    download_failed: list[str] = []
    for dataset in datasets:
        try:
            result = await _download_one(downloader, dataset, days)
            fetched[dataset] = int(getattr(result, "rows", 0) or 0)
        except Exception as exc:  # noqa: BLE001 单档失败不吞掉、也不拖垮其它档
            download_failed.append(f"{dataset}({type(exc).__name__})")
            logger.warning("行情仓库同步：%s 下载失败 %s",
                           dataset, brief(exc, BRIEF_DEFAULT))
    if download_failed and not fetched:
        return 0, (f"失败：{target} 全部数据集下载失败："
                   f"{'、'.join(download_failed)}")

    # 灌库是**阻塞**的（SQLite 批量写），放到线程里，别卡住调度循环。
    # ⚠️ 尽力而为：分区已经写好、下游读得到；灌库失败只意味着"仓库这份副本落后"。
    # ⚠️ 走 `_ingest_gaps` 而不是直接灌 `days`：`days` 是按 **daily** 的水位算出来的，
    #    直接灌它会让「比 daily 更落后」的那几档漏掉自己的缺口（见 `_ingest_gaps`）。
    total, failed = await _ingest_gaps()
    if not warehouse_ok:
        notes.append("已下载分区但未灌库")

    detail = (f"{latest or '—'} → {target}：下载 {len(days)} 个交易日"
              f"（{'、'.join(days[:3])}{'…' if len(days) > 3 else ''}），"
              f"灌库 {total} 行（原始拉取 {sum(fetched.values())} 行）")
    if download_failed:
        detail += f"；{'、'.join(download_failed)} 下载失败（其余已下载并灌库）"
    if failed:
        detail += f"；{'、'.join(failed)} 灌库异常（分区已就绪）"
    if notes:
        detail += "；" + "、".join(notes)
    return total, detail


async def _download_one(downloader: Any, dataset: str, days: list[str]) -> Any:
    """按数据集选下载入口；`index_daily` 走**指数入口**，不是 `sync_daily`。

    ## 为什么要有这个分支（2026-09-23 实测）

    作业的 `datasets` 清单按 `sync_gap.DAILY_DATASETS` 列了 8 档，其中
    `index_daily`（相对强度基准）在 `download.py::DAILY_DATASETS` 里**并不存在**
    —— 指数是 5 个代码逐个拉的，入口是 `sync_index`。于是：

        TushareDownloader.sync_daily("index_daily", days) → KeyError: 'index_daily'

    而且它抛在循环里、灌库之前，于是 `daily/daily_basic/adj_factor/stk_limit/
    moneyflow` 的分区**已经写好**，仓库却一行没进，作业只留一句"下载失败"。
    """
    if dataset == "index_daily":
        return await downloader.sync_index(days)
    return await downloader.sync_daily(dataset, days)


def _calendar_cache_window() -> tuple[str, str]:
    """本地 `trade_cal` 缓存的窗口 `(start, end)`；读不到返回 `("", "")`。"""
    from src.quant.dataset_store import DatasetStore

    try:
        entry = DatasetStore("trade_cal").manifest().get("static") or {}
    except Exception as exc:  # noqa: BLE001 读不到就退回"不介入"
        logger.info("读 trade_cal 缓存窗口失败：%s", brief(exc, BRIEF_TIGHT))
        return "", ""
    return str(entry.get("start") or ""), str(entry.get("end") or "")


async def _refresh_calendar_horizon() -> str:
    """把本地 `trade_cal` 缓存的**视野**推到今天（墙上时钟）；返回说明，正常时为空串。

    ## 为什么必须有这一步（2026-09-23 用户报告的真根因）

    `target = latest_complete_trade_date()` 只读**本地** `trade_cal` 缓存
    （`freshness.calendar_days()` 刻意不联网）。缓存停在 20260922 时：

        target = 20260922；水位也是 20260922 → 每次跑都报「已是最新」
        → 09-23 的数据永远不会被下载 → 情绪周期图/量化选股停在 09-22

    实测证据：09-23 收盘后 17:30 / 18:00 / 18:30 三次作业全部 `success`、
    `records_processed=0`、各耗时约 54 秒（只跑了补灌扫描），而 Tushare
    当天 18:49 已能取到 5556 行日线。

    而它**自己解不开**：`download.py::calendar(start, end)` 只在
    `end > 缓存末日` 时才重新拉取，偏偏这个 `end` 正是从缓存算出来的 —— 循环依赖。
    所以这里用墙上时钟要一次日历，显式把 `end` 顶到今天，打破这个环。

    失败/空表都**只记一句说明**、不改判定：联网失败时按旧日历走（不猜、也不让作业失败）。
    """
    from datetime import date

    today = date.today().strftime("%Y%m%d")
    cache_start, cache_end = _calendar_cache_window()
    if not cache_end:
        # 缓存读不到 → 交给后面的日历判定如实报"判不了"，这里不猜也不联网
        return ""
    if cache_end >= today:
        return ""
    try:
        from src.quant.download import TushareDownloader
        from src.quant.tushare_source import TushareClient, resolve_token

        resolve_token()
        downloader = TushareDownloader(TushareClient())
        days = await downloader.calendar(cache_start or "19901219", today)
    except Exception as exc:  # noqa: BLE001 刷新失败不该让作业失败
        logger.warning("交易日历刷新失败：%s", brief(exc, BRIEF_DEFAULT))
        return (f"交易日历刷新失败（按旧日历继续，缓存末日 {cache_end}）："
                f"{type(exc).__name__}")
    if not days:
        return f"交易日历刷新返回空表（按旧日历继续，缓存末日 {cache_end}）"
    logger.info("交易日历缓存已推进到 %s（原缓存末日 %s）", today, cache_end)
    return ""


def _partition_latest(dataset: str) -> str:
    """本地分区文件里最新的分区键（空 = 一个分区都没有）。"""
    from src.quant.dataset_store import DatasetStore

    try:
        keys = [str(key) for key in DatasetStore(dataset).keys()]
    except Exception as exc:  # noqa: BLE001 分区读不到就当作"没有"
        logger.warning("读分区最新交易日异常：%s", brief(exc, BRIEF_DEFAULT))
        return ""
    return max(keys) if keys else ""


def _latest_complete_trade_date() -> str:
    from src.quant.freshness import latest_complete_trade_date

    return latest_complete_trade_date()


def _warehouse_latest(warehouse: Any, dataset: str) -> str:
    """仓库里某个数据集的最新交易日（空表返回空串）。"""
    frame = warehouse.load(dataset, columns=["trade_date"], order=False, limit=0)
    if frame is None or not len(frame) or "trade_date" not in frame.columns:
        return ""
    return str(frame["trade_date"].astype(str).max())


def _next_day(stamp: str) -> str:
    from datetime import date, timedelta

    try:
        base = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
    except (ValueError, IndexError):
        return stamp
    return (base + timedelta(days=1)).strftime("%Y%m%d")


async def _model_retrain(spec: Any) -> tuple[int, str]:
    """模型重训作业：非交易时段重训 3 档 + 回归测试 + 过闸门才替换。

    返回 `(产出模型档数, 说明)`；说明以 `失败` 开头时会被记成 failed。

    ## 为什么在作业里再判一次交易日

    cron 已经定在周六 20:00，但**调休**会让周六变成交易日（A 股有先例）。
    重训要跑几分钟到几十分钟，期间模型文件可能处于"替换到一半"的状态；
    如果正好撞上自动选股窗口，选股会读到半套模型 —— 这种错很难现场复现。
    所以重训前明确拒绝交易日，宁可晚一天重训。
    """
    from src.intraday.auto_select import is_trading_day

    now = datetime.now()
    if is_trading_day(now):
        return 0, f"{now:%Y-%m-%d} 是交易日，按口径跳过重训（等非交易时段）"

    from src.quant.model_retrain_service import ModelRetrainService

    params = spec.params or {}
    dry_run = bool(params.get("dry_run", False))
    report = await asyncio.to_thread(
        ModelRetrainService().run, dry_run=dry_run, triggered_by="schedule")
    if report.error:
        return 0, f"失败：{report.error}"
    produced = len(report.buckets)
    detail = report.summary_line()
    if report.notes:
        detail += "；" + "；".join(report.notes)[:200]
    return produced, detail


async def _crowding_metrics(spec: Any) -> tuple[int, str]:
    """板块拥挤度周频异动指标作业（前端告警面板那 4 列）。

    返回 `(写入板块数, 说明)`；说明以 `失败` 开头时会被记成 failed。

    ## 为什么在作业里再判一次交易日

    cron 定在周三 08:30，正常是非交易时段的盘前。但节假日调休会让它撞上
    交易日 —— 那时跑没有错（只是多算一次），**真正的风险是抢数据源**：
    指标要抓 900+ 板块的成分股，与 16:40 的仓库同步、盘中采集共用限流额度。
    所以交易日盘前仍然执行但降为"非强制"（同一周算过就跳过），避免无谓重算。
    """
    params = spec.params or {}
    force = bool(params.get("force", False))

    from src.sector_crowding import metrics as crowding_metrics

    task = await asyncio.to_thread(
        crowding_metrics.compute_all_metrics, force=force)
    if task.status == "failed":
        return 0, f"失败：{task.error}"
    if task.skipped:
        return 0, f"本周（{task.week}）已算过，跳过（共 {task.rows_written} 个板块）"
    return task.rows_written, (
        f"{task.week} 完成：{task.rows_written} 个板块，"
        f"拥挤度截至 {task.as_of_date or '—'}，"
        f"资金流截至 {task.flow_last_date or '—'}"
        + (f"；{len(task.failed_sectors)} 个板块失败" if task.failed_sectors else ""))


#: 主线同步里**允许失败**的数据集：它们缺失不会改变打分口径，只影响辅助信息。
#:
#: ⚠️ 为什么必须把它们摘出来（2026-09-23 发现）：`sync_all` 的
#: `board_crowding` / `member_crowding` 要读**应用库**里的 `sector_crowding_list`
#: / `sector_member`，而这两张表只在生产库建，`--env dev` 的隔离库
#: （`data/dev/moss_dev.db`）里没有 → dev 下**天天 failed**。
#: 而 `_mainline_daily` 原来是"任一数据集 failed 就判作业 failed"，
#: 于是 dev 里连续 3 次失败后 `mainline_daily` 会被**自动暂停** ——
#: 停的是整个每日同步，连带 `board_bar` / `board_flow` 也一起停了，
#: 表现就是"面板日期再也不动"（正是用户这次报障的那类症状）。
#: 记名但不判失败：台账里仍然看得到 failed，只是不牵连整轮。
_MAINLINE_OPTIONAL_DATASETS: tuple[str, ...] = ("board_crowding", "member_crowding")


async def _mainline_daily(spec: Any) -> tuple[int, dict[str, Any]]:
    """主线挖掘·每日盘后：同步数据 → 三层漏斗打分 → 告警 → 推送 → 落库。

    返回 `(处理条数, 说明字典)`；`说明["failed"]` 为真时作业记 failed。

    ## 为什么盘后链路要"先同步再打分"

    打分完全读本地仓（`datastore`）。不先同步就会拿昨天的数据算今天的信号 ——
    而这件事**不会报错**：分数照常产出、告警照常入库，只是全部基于过期数据。
    因此这里的顺序是硬性的：同步失败时**仍然打分**（用已有一致的数据），
    但把失败原因写进 `message` 并记 failed，让人看得见。
    """
    from datetime import datetime, timedelta

    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore, sync_all
    from src.mainline.notify import push_alerts
    from src.mainline.service import MainlineService
    from src.mainline.storage import build_mainline_repository

    params = spec.params or {}
    config = load_config()
    store = MainlineDataStore(config=config)
    repo = build_mainline_repository(get_settings())
    service = MainlineService(config=config, store=store, repo=repo)

    end = datetime.now().strftime("%Y%m%d")
    days = max(int(params.get("sync_days", 5) or 5), 1)
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=days * 2)
             ).strftime("%Y%m%d")
    results = await asyncio.to_thread(
        sync_all, store, start=start, end=end, datasets=None, progress=None)
    notes = [item.note for item in results]
    # ⚠️ 只有**非可选**数据集失败才判作业 failed：可选档（见
    # `_MAINLINE_OPTIONAL_DATASETS`）在 dev 隔离库里必然失败，
    # 天天判 failed 会让作业 3 次后被自动暂停、连带停掉整轮同步。
    failed = [item for item in results
              if item.status == "failed"
              and item.dataset not in _MAINLINE_OPTIONAL_DATASETS]
    skipped_optional = [item.dataset for item in results
                        if item.status == "failed"
                        and item.dataset in _MAINLINE_OPTIONAL_DATASETS]

    snapshot = await service.score_date("", save=True)
    pushed = []
    if params.get("notify", True) and snapshot.alerts:
        pushed = await asyncio.to_thread(push_alerts, snapshot.alerts, config)
        if repo is not None:
            try:
                await repo.save_alerts(snapshot.alerts)
            except Exception as exc:  # noqa: BLE001 推送状态回写失败不影响主流程
                logger.warning("主线告警推送状态回写失败：%s", exc)

    detail = {
        "failed": bool(failed),
        "message": (
            f"交易日 {snapshot.trade_date}：板块 {snapshot.board_count_total}，"
            f"候选 {snapshot.candidate_count}，精选 {snapshot.selected_count}，"
            f"告警 {len(snapshot.alerts)}；"
            + ("；".join(notes)[:200] if notes else "无需同步")
            + ("；推送：" + "，".join(item.note for item in pushed) if pushed else "")
            + (f"；同步失败 {len(failed)} 项" if failed else "")
            + (f"；可选数据集未同步 {'、'.join(skipped_optional)}（不影响打分）"
               if skipped_optional else "")),
    }
    return len(snapshot.alerts), detail


async def _mainline_etf_flow() -> tuple[int, dict[str, Any]]:
    """ETF 份额监控·每日盘后：组装快照 → 信号落库。

    返回 `(写入信号数, 说明字典)`；`说明["failed"]` 为真时作业记 failed。

    ## 为什么用 `_repo().load_...` 之外还要单独落一次信号

    `build_snapshot` 只**算**不落库（它是纯函数式组装，API 每次请求都会调）。
    落库放在这里，是为了让"面板看实时快照"和"信号历史可回溯"分开：
    快照可以随时重算，历史必须由调度器按日固化一次 —— 否则某天没打开面板，
    那天的信号就永久缺失了。

    ## 数据缺口不算失败

    份额没同步上来时快照仍会产出（`gaps` 里说明），此时写 0 行是**正确**结果
    而不是错误：把"今天没数据"记成 failed 会让作业看板天天飘红，
    真正的失败（库不可写）反而被淹没。
    """
    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore
    from src.mainline.etf_flow import build_snapshot
    from src.mainline.etf_flow import load_config as load_flow_config
    from src.mainline.etf_share_guard import ensure_etf_shares
    from src.mainline.storage import build_mainline_repository

    store = MainlineDataStore(config=load_config())
    repo = build_mainline_repository(get_settings())
    if repo is None:
        return 0, {"failed": True, "message": "存储不可用，ETF 份额信号无法落库"}

    # 17:30 的 `mainline_daily` 刚做过一次全市场 ETF 同步，但**份额要到次日 8:30
    # 才发布** —— 那次同步必然给最新交易日写下一批 `shares = NULL` 的行，而面板
    # 只读最后一行（2026-09-22 实测：9 只监控里 7 只因此变空）。所以开工前先自检
    # 补齐：份额已在则只做两次 SQL、不联网；没发布就照实记进说明里。
    guard = await asyncio.to_thread(ensure_etf_shares, store, wait_seconds=0.0)

    snapshot = await asyncio.to_thread(build_snapshot, store,
                                      config=load_flow_config())
    written = await repo.save_etf_signals(snapshot.signals)
    regime = snapshot.regime
    alerts = sum(1 for item in snapshot.signals
                 if item.level in ("strong", "medium") and not item.gated)
    message = (f"{snapshot.trade_date} {regime.label}（参考 {regime.index_name} "
               f"近{regime.window}日 {(regime.change or 0) * 100:+.2f}%），"
               f"信号 {len(snapshot.signals)} 条，其中会告警 {alerts} 条，"
               f"写入 {written} 行")
    if guard.action or guard.state != "fresh":
        message += "；份额自检：" + guard.summary()
    if snapshot.gaps:
        message += "；数据缺口：" + "；".join(snapshot.gaps[:3])
    return written, {"message": message, "failed": False}


async def _mainline_calibrate() -> tuple[int, str]:
    """期货映射表季度校准（非交易日 09:00 跑，避免抢盘后链路的 Tushare 频次）。"""
    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore
    from src.mainline.futures import FuturesService

    config = load_config()
    service = FuturesService(config=config,
                             store=MainlineDataStore(config=config))
    outcome = await asyncio.to_thread(service.calibrate)
    if outcome.get("gap"):
        return 0, f"未校准：{outcome['gap']}"
    return int(outcome.get("updated", 0)), (
        f"校准 {outcome.get('updated')} 条映射（窗口 "
        f"{outcome.get('window_days')} 个交易日，截至 {outcome.get('as_of')}）")


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
