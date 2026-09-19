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
            if spec.kind == "dynamic_collection":
                indicators = spec.params.get("indicators") or []
                processed, errors = await _collect_through_pipeline(
                    runtime, indicators, "dyn_col")
                return _finish_collection(record, run_log, processed, errors)
        except Exception as exc:  # noqa: BLE001 作业级兜底：失败入记录供告警/重试
            return run_log.finish(record, status="failed", error_message=brief(exc, BRIEF_LOG))

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

    1. **只补缺的那几天**：先问仓库当前最新交易日，从它的下一天开始按交易日历
       取区间；没有缺口就直接返回，不做任何下载（幂等、可反复跑）。
    2. **下载完必须灌库**：`sync_daily` 只写 CSV/parquet 分区，
       不 ingest 的话仓库里还是旧数据 —— "下了但没入库"是最容易发生的一步漏。
    3. **单档失败不吞掉**：哪一档没补上都写进返回值，作业记录里能看见。
    """
    import asyncio

    from src.quant.download import TushareDownloader
    from src.quant.tushare_source import TushareClient, resolve_token
    from src.quant.warehouse import QuantWarehouse

    datasets = list((spec.params or {}).get("datasets") or
                    ["daily", "daily_basic", "stk_limit"])
    target = _latest_complete_trade_date()
    if not target:
        return 0, "失败：交易日历不可用，无法判断该补到哪一天（不做猜测）"

    warehouse = QuantWarehouse()
    if not warehouse.available():
        return 0, "失败：本地量化仓库不可用"

    try:
        latest = _warehouse_latest(warehouse, datasets[0])
    except Exception as exc:  # noqa: BLE001
        return 0, f"失败：读仓库最新交易日异常 {type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"
    if latest and latest >= target:
        return 0, f"已是最新（仓库 {latest} ≥ 目标 {target}），无需同步"

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
        fetched: dict[str, int] = {}
        for dataset in datasets:
            result = await downloader.sync_daily(dataset, days)
            fetched[dataset] = int(getattr(result, "rows", 0) or 0)
    except Exception as exc:  # noqa: BLE001 网络/权限失败如实上报
        return 0, (f"失败：下载 {latest or '起点'}~{target} 失败 "
                   f"{type(exc).__name__}: {brief(exc, BRIEF_LOG)}")

    # 灌库是**阻塞**的（SQLite 批量写），放到线程里，别卡住调度循环
    written: dict[str, int] = {}
    failed: list[str] = []
    for dataset in datasets:
        try:
            written[dataset] = await asyncio.to_thread(
                warehouse.ingest_dataset, dataset, keys=days)
        except Exception as exc:  # noqa: BLE001 单档失败不影响其余
            failed.append(f"{dataset}({type(exc).__name__})")
            logger.warning("仓库同步：%s 灌库失败 %s", dataset, brief(exc, BRIEF_DEFAULT))
    total = sum(written.values())
    detail = (f"{latest or '—'} → {target}：下载 {len(days)} 个交易日"
              f"（{'、'.join(days[:3])}{'…' if len(days) > 3 else ''}），"
              f"灌库 {total} 行（原始拉取 {sum(fetched.values())} 行）")
    if failed:
        detail = f"失败：{'、'.join(failed)} 灌库异常；其余成功。{detail}"
    return total, detail


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
