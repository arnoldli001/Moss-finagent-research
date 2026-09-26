"""FastAPI应用入口（启动：uvicorn src.api.main:app）。

lifespan：启动时组装Runtime（Agent注册表+StateGraph），关停时取消在飞任务。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from src.api.accounting_middleware import AccountingMiddleware
from src.api.data_health import warm as warm_data_health
from src.api.event_wiring import build_event_stack
from src.api.exception_handlers import register_handlers
from src.api.login_gate import LoginGateMiddleware, docs_kwargs
from src.api.routes import api_router
from src.api.runtime import build_runtime
from src.api.tasks import TaskStore
from src.api.tenancy_middleware import TenancyMiddleware, describe_enforcement
from src.core.config import describe_environment, get_settings
from src.core.errors import BRIEF_DEFAULT, brief
from src.core.exceptions import ConfigError
from src.core.executors import shutdown_infra_executors
from src.core.sqlite_recovery import (
    checkpoint_and_close,
    ensure_all_usable,
    release_all_connections,
)
from src.scheduler.run_log import RunLog
from src.scheduler.service import CronScheduler

settings = get_settings()
logger = logging.getLogger(__name__)


def _sqlite_paths_to_check() -> list[str]:
    """启动时要确认可用的**可写** SQLite 库清单。

    为什么必须在 `build_runtime()` **之前**做：仓储在第一次连接时就决定了
    本次进程的命运。一旦 `moss_finagent.db` 带着陈旧的 `-wal`/`-shm` 启动，
    每个走它的请求都会拿到 `disk I/O error` → 仓储 fail-open 穿透网络链 →
    首次请求几十秒（实测重启后 `intraday/watchlist` 首次 182.9s，第二次 0.0s）。

    刻意**不含** `data/quant/warehouse.db`：那是 15GB 只读为主的行情仓库，
    没有 WAL 一致性问题的历史，不该在启动时对它做任何写动作；
    即使它出问题也应该人工介入，而不是自动隔离伴生文件。
    """
    paths = [str(settings.sqlite_path)]
    for name in ("alert_db_path", "scheduler_dir"):
        raw = getattr(settings, name, None)
        if isinstance(raw, str) and raw.endswith(".db"):
            paths.append(raw)
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique



#: 启动后多久才做"重量级后台预热"（秒）。
#: 见 lifespan 里的说明：早开跑会把首屏请求与 /health 一起拖慢。
#:
#: 取值从 60 提到 **600**（2026-09-18 实测）：60 秒时板块预热（一个 akshare
#: 子进程 + 十几个板块的网络取数）会与"重启后头一波用户请求"以及**量化选股**
#: 抢 CPU/磁盘 —— 实测同一次选股在无争抢时 27 秒，撞上预热时被拖成 828 秒且
#: 候选池退化到 0 只（数据没取全）。板块是展示项（14/100 权重），
#: 晚 10 分钟对任何信号都没有影响；而"重启后立刻要准的数据"只有选股与自选。
_BACKGROUND_WARM_DELAY = 600.0

#: 数据健康度预热的延后时长（秒）。
#:
#: 它要遍历 3.5 万个分区清单 + 扫 14GB 仓库（实测 6~28 秒 I/O），本来是**立刻**
#: 在后台线程里算的。问题在于：前端默认落地页底部就挂着「数据源健康度」面板，
#: 而那个后台预热线程与首屏请求抢同一台机器的磁盘/CPU/GIL ——
#: 2026-09-24 冷启动实测（7 个首屏请求并发）：
#:
#: | | 首屏全部就绪 |
#: |---|---|
#: | 预热立刻开跑 | 4.7s（且 `agents/meta` 这种纯字典接口也要 2.0s） |
#: | 预热延后 20 秒 | **2.7s** |
#:
#: 延后不影响正确性：`/health` 在没有统计缓存时会返回"统计生成中"占位并顺手起
#: 后台重算（见 data_health 的 `_tushare_pending`），20 秒后预热自己把缓存写热。
_WARM_DATA_HEALTH_DELAY = 20.0


async def _warm_data_health_later(runtime, *, delay: float = _WARM_DATA_HEALTH_DELAY) -> None:
    """延后做数据健康度预热（见 `_WARM_DATA_HEALTH_DELAY` 的实测表）。

    异常只记日志：预热是优化，失败不影响任何功能。
    """
    try:
        await asyncio.sleep(delay)
        # `warm` 自己会起守护线程去算（见 data_health.warm），这里只负责"错开时间"。
        warm_data_health(runtime)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 预热是优化，绝不能影响服务
        logger.warning("数据健康度预热失败（忽略）", exc_info=True)


async def _warm_board_snapshots_later(runtime) -> None:
    """延后做板块快照预热；异常只记日志（预热失败不影响任何功能）。"""
    try:
        await asyncio.sleep(_BACKGROUND_WARM_DELAY)
        await runtime.intraday.warm_board_snapshots()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 预热是优化，绝不能影响服务
        logger.warning("板块快照预热失败（忽略）", exc_info=True)


async def _ensure_etf_shares_later(delay: float = 3.0) -> None:
    """启动后自检 ETF 份额：最新交易日的份额缺了就补拉（见 etf_share_guard）。

    ## 为什么要挂在启动路径上

    份额是**次日 8:30 左右**才发布的，而 ETF 日线当晚就有。于是"份额发布前跑一次
    同步"会给最新交易日留下一批 `shares = NULL` 的行，而面板以
    `MAX(trade_date)` 为锚点只读最后一行 —— 9 只被监控的宽基里 7 只的份额、
    1/5/10/20 日变化率、对应指数分位**一起变空**（2026-09-22 实测事故）。
    这不是一次性事故，是每晚都会重演的时序问题，所以修在"进程必经路径"上。

    ## 三条边界

    * 延迟 `delay` 秒再跑：启动瞬间的磁盘/CPU 要留给首屏与 `/health`
      （与板块预热同一考虑，见 `_BACKGROUND_WARM_DELAY`）；
    * 自检**不拦启动**、异常只记日志 —— 它失败等于退回今天之前的行为；
    * 默认**不等待**份额发布（`MOSS_ETF_SHARE_WAIT` 可开）：份额在 8:30 后
      通常已经在了，凭空等待只会拖慢首屏。
    """
    try:
        await asyncio.sleep(delay)
        from src.mainline.datastore import MainlineDataStore
        from src.mainline.etf_share_guard import (
            ensure_etf_shares,
            log_report,
            startup_wait_seconds,
        )

        store = await asyncio.to_thread(MainlineDataStore)
        report = await asyncio.to_thread(
            ensure_etf_shares, store, wait_seconds=startup_wait_seconds())
        log_report(report)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 自检失败绝不能影响服务
        logger.warning("ETF 份额启动自检异常（忽略）", exc_info=True)


#: 资金流快照预热的延后时长（秒）。
#:
#: ⚠️ **不能立刻开跑** —— 这条纪律本项目已经用实测付过学费（见
#: `_BACKGROUND_WARM_DELAY` 与 `_WARM_DATA_HEALTH_DELAY` 两张实测表：
#: 预热与首屏抢磁盘，把 `/health` 从 3.7 秒拖成 54~142 秒、把一次 27 秒的
#: 选股拖成 828 秒）。重启后的头几秒正是"一波用户同时打开面板"的时刻。
#:
#: 取 60 秒（比 `_BACKGROUND_WARM_DELAY=600` 短得多）的理由：本条预热的代价
#: 只有**一次 15 GiB 行情仓的冷打开**（实测 3.87 秒，之后重算仅 231 ms），
#: 比"akshare 子进程 + 十几个板块网络取数"轻一个数量级；60 秒已足够让
#: 首屏那一波过去，而预热仍然落在同一次重启窗口内、对用户有意义。
_FUNDFLOW_WARM_DELAY = 60.0


async def _warm_fundflow(runtime: Any,
                         *, delay: float = _FUNDFLOW_WARM_DELAY) -> None:
    """后台预热资金流快照（见 lifespan 里的调用点说明）。

    **任何失败都吞掉**：这是一次机会性的预热，不是启动的前置条件 ——
    让"预热失败"变成"服务起不来"是把优化做成了故障。
    """
    service = getattr(runtime, "fundflow", None)
    if service is None:
        return
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        raise
    t0 = time.monotonic()
    try:
        await service.snapshot(force=True, window_days=10, top=20)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 预热失败不影响任何业务功能
        logger.info("资金流快照预热失败（忽略，按需自算）：%s", type(exc).__name__)
        return
    logger.info("资金流快照预热完成：%.2fs", time.monotonic() - t0)


async def _warm_external_sources(runtime: Any, *, delay: float = 8.0) -> None:
    """启动后台探测"可能不可达"的外网数据源，把故障冷却预先建立起来。

    为什么需要（2026-09-26 实测）：``fed:rate_prob:next``（CME FedWatch）依赖
    CME/FRED **外网**。不可达时 ``FedWatchConnector`` 会等满 25 秒硬超时才降级，
    而 A01 用 ``asyncio.gather`` 并发采集 —— 于是**整条数据采集阶段的墙钟时间
    被这一个指标拖到 23.5 秒**，用户看到的就是"每次都先从互联网采集，等很久"。

    现在失败会记 60 秒冷却（`connectors/source_cooldown.py`），但**第一个用户
    仍要替所有人挨这一下**。所以启动后主动探一次，让冷却在用户到来前就位。

    与 `_ollama_status` 同一思路：把"必然失败的等待"从请求路径挪到后台。
    失败只记日志 —— 探测本身绝不能影响启动。
    """
    try:
        await asyncio.sleep(delay)
        from src.infrastructure.connectors.fedwatch_connector import (
            FedWatchConnector,
        )

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        points = await FedWatchConnector().fetch("fed:rate_prob:next")
        elapsed = loop.time() - t0
        logger.info(
            "外网数据源预探：fed:rate_prob:next 返回 %d 点，耗时 %.1fs%s",
            len(points), elapsed,
            "" if points else "（不可达 → 已记冷却，后续 60s 内请求不再等待）")
    except Exception:  # noqa: BLE001 预探失败不影响任何功能
        logger.debug("外网数据源预探异常（忽略）", exc_info=True)


async def _start_auction_scheduler_later(delay: float = 60.0) -> None:
    """延迟启动竞价选股调度（见 lifespan 里的调用点说明）。

    为什么延迟 60 秒：与板块快照预热同一理由 —— 启动瞬间磁盘/CPU 要留给首屏与
    `/health`。竞价调度第一次真正干活是 09:15，晚一分钟启动不影响任何结果。
    启动失败只降级（记日志），绝不能拦住服务。
    """
    try:
        await asyncio.sleep(delay)
        from src.auction_select.scheduler import start_scheduler

        outcome = await asyncio.to_thread(start_scheduler)
        logger.info("竞价选股调度：%s", outcome)
    except ImportError:  # 公开版不含竞价选股，属预期而非故障
        logger.info("竞价选股模块未包含（商业版功能），不启动其调度")
    except Exception:  # noqa: BLE001 调度起不来只是少个功能，不该影响主链路
        logger.warning("竞价选股调度启动失败（降级：该模块仅手动触发可用）",
                       exc_info=True)


async def _warm_llm_cache_index(runtime: Any, *, delay: float = 5.0) -> None:
    """后台预热 LLM 语义缓存索引（把首次建索引代价摘出请求路径）。

    语义查找需要一个 ``key → (agent_id, scope, 3-gram向量)`` 的内存索引，
    首次建索引要扫描整个缓存目录（本机 3108 文件约 0.7 秒）。
    建索引本身走线程池、不阻塞事件循环，但冷启动后第一个用户的第一次
    未命中仍要白等这一次 —— 所以放后台先建好。

    延迟几秒再跑：与数据健康度预热同一理由（启动瞬间把磁盘留给首屏）。
    失败只记日志 —— 缓存是优化，绝不能拦住服务（未建时按需懒建，行为不变）。
    """
    try:
        await asyncio.sleep(delay)
        gateway = getattr(runtime, "gateway", None)
        cache = getattr(gateway, "_cache", None) if gateway is not None else None
        if cache is None or not hasattr(cache, "warm_up"):
            logger.debug("LLM 缓存未启用，跳过语义索引预热")
            return
        n = await asyncio.to_thread(cache.warm_up)
        logger.info("LLM 语义缓存索引预热完成：%d 条", n)
    except Exception:  # noqa: BLE001 预热失败不影响任何功能（按需懒建兜底）
        logger.warning("LLM 语义缓存索引预热失败（忽略，将按需懒建）", exc_info=True)


async def _check_quant_sync_at_startup(scheduler: Any, runtime: Any, *,
                                       delay: float = 30.0) -> None:
    """服务启动后自检行情数据是否同步；缺了就**自动补一次**。

    ## 为什么要有它（2026-09-23 用户报告）

    「量化选股用的是 20260917 的行情，最近一个已收盘交易日是 20260922」。
    根因不是选股逻辑，而是 `stk_limit` **分区已到 0922、仓库却停在 0917**
    —— 数据早就下载好了，只是没灌进库（`quant_data_sync` 的旧版本用 `daily`
    的缺口去灌所有数据集，`stk_limit` 自己的那几天永远补不上）。

    而当时**没有任何机制会自愈**：16:40 那次已经跑过且"成功"了，
    下一次机会要等到**下一个工作日**。用户在这之间打开页面，只能看到一句
    「数据滞后」，还得自己记住去手工跑两条命令。

    放在启动自检，是因为**重启是用户唯一确定会做的动作**（改了配置、更新了代码），
    这时顺手把数据补齐，比让他等到明天 16:40 更符合直觉。

    ## 三条约束（都不是可选的）

    1. **延迟 + 后台线程**：数据健康度要遍历 3.5 万个分区目录（磁盘忙时实测
       140 秒），绝不能占着首屏和 `/health`；
    2. **判定只读已有统计**：`build_data_health(force=True)` 负责在后台把两份
       统计算出来，判定本身只是内存里的字符串比较（见 `sync_gap.py`）；
    3. **任何异常只记日志**：数据没补上不该让服务起不来 —— 这是自检，不是前置条件。
    """
    try:
        await asyncio.sleep(delay)
        from src.api import data_health
        from src.quant import sync_gap as sync_gap_mod

        payload = await asyncio.to_thread(
            data_health.build_data_health, runtime, force=True)
        gap = (payload or {}).get("sync_gap") or {}
        outcome: dict[str, Any] = {
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "expected": gap.get("expected") or "",
            "synced": bool(gap.get("synced")),
            "behind": list(gap.get("behind") or []),
            "unknown": list(gap.get("unknown") or []),
            "action": "none",
        }
        if not gap.get("checked"):
            outcome["action"] = "无法判定"
            logger.info("行情同步自检：%s", gap.get("note") or "无法判定（不猜）")
        elif gap.get("synced"):
            logger.info("行情同步自检：日频数据集已同步到 %s", gap.get("expected"))
        else:
            logger.warning("行情同步自检发现缺口：%s", gap.get("note"))
            if scheduler is None:
                outcome["action"] = "调度器不可用，未补偿"
                logger.warning("行情同步自检：调度器不可用，跳过自动补偿")
            else:
                record = await scheduler.trigger("quant_data_sync",
                                                 source="startup")
                outcome["action"] = "已触发补偿同步"
                outcome["job_status"] = (record or {}).get("status")
                outcome["job_rows"] = (record or {}).get("records_processed")
                logger.info("行情同步自检补偿完成：%s（%s 条）",
                            outcome["job_status"], outcome["job_rows"])
        sync_gap_mod.record_check(outcome)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 自检失败绝不能影响服务
        logger.warning("行情同步启动自检异常（忽略）", exc_info=True)


async def _run_intel_prewarm(runtime: Any) -> None:
    """情报流后台预热循环（见 `domain.intel.prewarm` 的模块 docstring）。

    ## 为什么需要它

    `GET /feed` 的契约是**请求永不等待采集** —— 冷启动先下发空壳，后台重建完
    再经 WS 通知前端回拉。用户口径（2026-09-25）：

    > "服务启动就加载已有数据…这样就不存在首屏冷启动。"

    预热把"重建"挪到启动期与后台周期：用户打开页面直接读已建好的缓存
    （实测复用是 **~120 ms / 0 次上游**，而重建一次是 2.6~3.3 秒）。

    ## 三点说明

    1. **hub 复用既有的告警 WS 通道**（`AlertHub`），不新开第二条：App 层本来
       就有一条常驻连接，再开一条等于两套重连/心跳/鉴权各自维护。
       hub 拿不到时只落缓存 —— 前端下次请求/轮询自己会拿到，功能不降级。
    2. **异常全部吞掉**：预热是机会性优化，让"预热失败"变成"服务起不来"
       是把优化做成了故障（本文件里多个预热都遵守这条）。
    3. **是否该抓的判据在 `prewarm.should_fetch`**（工作日 + 2 小时间隔），
       本函数只负责把它挂到事件循环上。
    """
    from src.domain.intel import prewarm

    try:
        await prewarm.run_prewarm_loop(hub=getattr(runtime, "alert_hub", None))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 预热失败绝不能影响服务
        logger.warning("情报流预热循环异常退出（忽略）", exc_info=True)


async def _run_event_alert_on_startup(scheduler: Any, runtime: Any, *,
                                      delay: float = 90.0) -> None:
    """服务启动后自动补跑一次事件告警扫描。

    ## 用户口径（2026-09-24）

    告警扫描的定时班次是「工作日 09:30 / 10:30 / 12:00 / 13:30 / 14:30 / 17:30」
    （见 `src/scheduler/registry.py` 的三个 `event_alert_scan` 作业），
    **只要服务在启动，就自动触发一次** —— 否则"盘中重启一次"就要干等到下一个
    点位才有告警，运维上完全说不过去。

    ## 三条约束（都不是可选的）

    1. **延迟 + 后台**：启动那几十秒要抢首屏、抢行情预热与板块预热（实测争抢会把
       `/health` 从 3.7 秒拖到上百秒），而扫描要打多路快讯源 + 逐条过 LLM，
       所以让出 90 秒（与板块预热的 60 秒错开）后再开跑；
    2. **任何异常只记日志**：这是自检不是启动前置条件，扫描失败不该让服务起不来；
    3. **复用 `event_alert_intraday` 这个作业名**：运行记录里 `trigger="startup"`
       与定时班次的 `"schedule"` 一眼可分；与定时那班共用 service 扫描锁
       （`ScanInProgressError` → 记 skipped），撞上只会跳过，不会双跑。

    子系统没装配（`event_service is None`，如非 SQLite 后端）时直接跳过 ——
    不去写一条注定 failed 的运行记录污染调度面板。
    """
    try:
        await asyncio.sleep(delay)
        if scheduler is None:
            logger.info("事件告警启动补扫跳过：调度器不可用")
            return
        if getattr(runtime, "event_service", None) is None:
            logger.info("事件告警启动补扫跳过：事件告警子系统未装配")
            return
        record = await scheduler.trigger("event_alert_intraday",
                                         source="startup")
        logger.info("事件告警启动补扫完成：%s（告警 %s 条）",
                    (record or {}).get("status"),
                    (record or {}).get("records_processed"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 启动补扫失败绝不能影响服务
        logger.warning("事件告警启动补扫异常（忽略）：%s", exc, exc_info=True)


async def _run_retention_on_startup(*, delay: float = 120.0) -> None:
    """服务启动后延迟执行一次数据保留清理（最多保留 N 年/N 天）。

    与每日 03:30 的 data_retention_daily 作业互补：定时任务长期没跑的环境（如
    一直只在白天开机），在重启时也能收敛。延迟让出首屏与预热的磁盘/CPU；保留
    服务内部已隔离异常，这里任何失败都只记日志。

    ⚠️ 结果用 `warning` 上报而不是 `info`：本模块的 logger **没有配 handler**，
    `logger.info` 会被直接丢弃 —— 实测本文件里 `资金流快照预热完成`
    `竞价选股调度` `事件告警启动补扫完成` 等启动钩子的 INFO **一条都不落盘**
    （uvicorn 自己的输出有 201 条）。于是"启动清理跑没跑"在日志里看不出来，
    而保留恰恰是"静默失效"风险最高的一类功能（不清理不报错，只会某天发现库很大）。

    `run_retention` 内部已按"有没有真删行/出错"决定级别；这里只补一条
    **极简**的确认行，保证即使哪天它的日志级别被调回去，启动清理依然可观测。
    """
    try:
        await asyncio.sleep(delay)
        from src.infrastructure.retention_service import run_retention

        outcome = await run_retention(settings)
        logger.warning(
            "启动数据保留完成：数据点删除 %s，新闻删除 %s，流水档删除 %s%s",
            outcome["points_deleted"], outcome["news_deleted"],
            outcome.get("passes_deleted", 0),
            ("，错误:" + ",".join(outcome["errors"])) if outcome["errors"] else "")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 保留失败绝不能影响服务
        logger.warning("启动数据保留异常（忽略）", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 先确认可写 SQLite 库能正常打开，再组装 Runtime：陈旧 WAL 索引会让每个
    # 请求都 `disk I/O error` 并穿透网络（根因与证据见 src/core/sqlite_recovery.py）。
    # 健康库在这里只花一次 `sqlite_master` 计数的时间（实测 0.00s 量级）。
    for outcome in ensure_all_usable(_sqlite_paths_to_check()):
        if outcome.recovered:
            logger.warning("启动自愈：%s %s", outcome.path, outcome.reason)
        elif not outcome.healthy:
            logger.error("启动检查：%s 不可用（%s）", outcome.path, outcome.reason)
    runtime = build_runtime()
    app.state.runtime = runtime
    # 个股「关联板块 / 海外映射」绑定**启动时热加载**（用户口径 2026-09-23）。
    #
    # 为什么必须在启动时做：`IntradayService.add_watch` 是**同步**方法（路由直接调、
    # 加一只票要立刻返回），它没法 await 一次 SQLite 查询。把已配过的绑定常驻内存后，
    # 加自选时"自动加载之前的配置"就是纯内存读 —— 用户明确要求"避免加自选时
    # 无法迅速读取"。失败只降级为空（绑定是增强项），绝不拖垮启动。
    intraday = getattr(runtime, "intraday", None)
    if intraday is not None:
        try:
            count = intraday.warm_bindings()
            if count:
                logger.info("启动热加载：个股绑定 %d 只（关联板块/海外映射）", count)
        except Exception:  # noqa: BLE001 绑定热加载失败不该影响启动
            logger.warning("个股绑定热加载失败（降级为空）", exc_info=True)
    # 东财 push2* 在本机网络被按 TLS SNI 阻断（实测证据见模块 docstring）：
    # 这里装一层"只在被阻断时回退直连 IP"的 requests 壳。关掉：MOSS_EM_DIRECT=0。
    try:
        from src.core.eastmoney_direct import install as install_em_direct

        install_em_direct()
    except Exception:  # noqa: BLE001 规避机制本身绝不能影响启动
        logger.warning("东财直连回退装配失败（不影响其它数据源）", exc_info=True)
    app.state.store = TaskStore()
    app.state.run_log = RunLog(f"{settings.scheduler_dir}/runs.jsonl")
    # 事件告警子系统（SQLite；装配失败仅降级，不影响研究主链路）
    try:
        stack = build_event_stack(settings, runtime.gateway)
        await stack.repo.ensure_schema()
        runtime.event_repo = stack.repo
        runtime.event_service = stack.service
        runtime.alert_hub = stack.hub
        runtime.email_notifier = stack.emailer
        app.state.alert_hub = stack.hub
    except ConfigError as exc:
        logger.warning("事件告警子系统不可用（降级）: %s", brief(exc, BRIEF_DEFAULT))
    # 零依赖演示模式：进程内Cron调度（生产用Celery Beat，见celery_app.py）
    app.state.scheduler = CronScheduler(runtime, app.state.run_log)
    await app.state.scheduler.start()

    # ★ 主线挖掘「热快照」：启动即装回进程内缓存。
    #
    # 不做这一步的后果是**每次重启后第一个打开主线面板的人**要等一次全市场
    # 重算（实测 8.4~22.6 秒，随 IO 竞争浮动），而进程内缓存重启即清零。
    # 2026-09-25 一天为了发布重启了 5 次，就是 5 次。
    #
    # 为什么同步 `await`（而不是 `create_task`）：它只要 ~0.5 秒（装配上下文
    # + 读一份 0.1 MB 的 JSON，`data_status` 也从文件里来、不重算），
    # 而挂到后台就存在"请求先到、还是冷启动"的竞态 —— 那正是要消掉的东西。
    # 失败一律吞掉（缓存坏了不该拦住服务启动）。
    try:
        from src.api.routes.mainline import warm_load

        info = await asyncio.to_thread(warm_load)
        logger.info("主线热快照启动加载：%s", info)
    except Exception as exc:  # noqa: BLE001 预热失败不影响启动
        logger.warning("主线热快照启动加载异常（忽略）：%s", type(exc).__name__)

    # ★ 资金流监控：**后台**预热一次快照。
    #
    # 它的冷启动实测 **3.87 秒**（`FundFlowProvider` 首次打开 15 GiB 行情仓
    # + 首次取数），之后 60 秒缓存命中是 0 ms、过期重算只有 231 ms。
    # 所以"慢"只发生在重启后的第一个访问者身上 —— 和主线那个问题同源。
    #
    # 为什么**不 await**：它要将近 4 秒，await 就等于把服务启动推迟 4 秒
    # （这段时间 /health 也不响应，隧道那边会判成 502）。挂后台即可 ——
    # 预热完成前到的请求照常自己算，只是那一个慢；完成后所有人都是 0 ms。
    # 且它自己还要先睡 `_FUNDFLOW_WARM_DELAY`（见那里的实测依据）。
    asyncio.create_task(_warm_fundflow(runtime), name="fundflow-warm")

    heartbeat_task: asyncio.Task | None = None
    if runtime.alert_hub is not None:
        async def _hub_keepalive() -> None:
            while True:
                await asyncio.sleep(25)
                try:
                    await runtime.alert_hub.heartbeat()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 心跳异常不影响主进程
                    logger.debug("告警WS心跳异常", exc_info=True)
        heartbeat_task = asyncio.create_task(_hub_keepalive())
    # 做T权重档案表（`dim_intraday_profile`）：与 fact_* 三张表同库。
    # 建表失败只让档案接口降级 —— 做T主链路继续按 YAML/全局口径出分。
    if runtime.intraday_profile_repo is not None:
        try:
            await runtime.intraday_profile_repo.ensure_schema()
        except Exception:  # noqa: BLE001
            logger.warning("做T权重档案建表失败（降级：档案接口不可用）", exc_info=True)
            runtime.intraday_profile_repo = None
    # 资金流监控的"选择列表"表（`dim_fund_flow_watch`）：同样只影响该页签的持久化，
    # 建表失败时榜单/走势照常（前端仍可看，只是加入的板块/个股不会留存）。
    if runtime.fundflow_repo is not None:
        try:
            await runtime.fundflow_repo.ensure_schema()
        except Exception:  # noqa: BLE001
            logger.warning("资金流监控建表失败（降级：选择列表不可持久化）", exc_info=True)
            runtime.fundflow_repo = None
    # 量化选股的自定义板块表（dim_quant_sector 等四张）：同样只影响该模块，
    # 建表失败时接口返回 503，做T/资金流不受影响。
    if runtime.quant_select is not None and runtime.quant_select.repo is not None:
        try:
            await runtime.quant_select.repo.ensure_schema()
        except Exception:  # noqa: BLE001
            logger.warning("量化选股建表失败（降级：该模块不可用）", exc_info=True)
            runtime.quant_select = None
    # 做T辅助：盘中（09:15~11:30 / 13:00~15:00）每分钟把全部自选标的的分钟数据
    # 取到最新。**放在服务端**而不是前端定时器：这样"自选股自动刷新"不依赖浏览器
    # 是否开着，多标签页也只算一次，且信号推送在无人看盘时照常触发。
    if runtime.intraday is not None:
        # 热加载上次关停前的自选概览：冷启动首个 /watchlist 实测 59.8 秒
        # （26~39 只票全部要打数据源），热加载后首屏 0 秒出数据，
        # 新鲜度由 `watchlist()` 既有的 stale-while-revalidate 分支补 —— 详见
        # src/intraday/hot_cache.py。载入失败/过旧都不影响启动。
        try:
            state = runtime.intraday.load_cached_watchlist()
            if not state.get("loaded"):
                logger.info("自选概览热加载未生效：%s", state.get("reason"))
        except Exception:  # noqa: BLE001 热加载是优化，绝不能拦住启动
            logger.warning("自选概览热加载异常（忽略）", exc_info=True)
        await runtime.intraday.start_watchlist_refresh()
        # 报价快车道：现价/涨跌幅比打分快一个数量级（批量 tick 实测 9 只 0.54ms）
        await runtime.intraday.start_quote_refresh()
        # 自动选股：只在交易日的 09:25~09:40 与 14:45~15:00 每分钟跑一轮
        # （日K多方条件 + 分时打分>40 → 综合分前 6 自动加自选），盘后 15:05 再补一次。
        # 放服务端而不是前端：这两个窗口很短，浏览器不开着就完全错过。
        await runtime.intraday.start_auto_select()
    # 数据健康度预热：要遍历 3.5 万个分区清单 + 查 14GB 仓库（实测 6~28 秒 I/O），
    # 放后台线程算一次，用户第一次打开「运行指标」页就是热的（不再一直转圈）。
    # **延后 `_WARM_DATA_HEALTH_DELAY` 秒**：它立刻开跑会与首屏抢磁盘/CPU/GIL，
    # 实测冷启动首屏 2.7s → 4.7s（见该常量的表）。
    asyncio.create_task(_warm_data_health_later(runtime),
                        name="data-health-warm")
    # 集合竞价选股调度：开市日 09:15 预热、09:25 出池（要求 09:27 前）。
    # 放**服务进程内**的守护线程，不额外起服务、不改系统计划任务 ——
    # 竞价数据是盘中实时的，取完要立刻算分落库给前端，独立进程还要额外解决
    # SQLite 双写与结果传递。放后台任务启动，不阻塞首屏。
    asyncio.create_task(_start_auction_scheduler_later(),
                        name="auction-select-scheduler")
    # 行情同步启动自检：比对「分区 / 仓库 / 应该有」三层，缺了就自动补一次
    # （2026-09-23 事故：`stk_limit` 分区 0922、仓库 0917，选股因此停在 0917）。
    # 延迟 + 后台，且失败只记日志 —— 见 `_check_quant_sync_at_startup` 的说明。
    asyncio.create_task(
        _check_quant_sync_at_startup(app.state.scheduler, runtime),
        name="quant-sync-startup-check")
    # LLM 语义缓存索引预热：首次语义查找要扫描整个缓存目录建索引
    # （本机 3108 文件约 0.7s）。虽然是走线程池、不阻塞事件循环，
    # 但冷启动后第一个用户的第一次未命中要白等这一次。
    # 放后台建一次，把它从请求路径上彻底摘掉。
    asyncio.create_task(_warm_llm_cache_index(runtime), name="llm-cache-warm")
    # 外网数据源预探：fed:rate_prob:next 不可达时要等满 25s 硬超时，
    # 而 A01 并发采集会被它拖到 23.5s。启动先探一次，让失败冷却提前就位，
    # 第一个用户就不必替所有人挨这一下。见 _warm_external_sources。
    asyncio.create_task(
        _warm_external_sources(runtime), name="external-source-warm")
    # 事件告警启动补扫：**只要服务在启动就自动跑一次扫描**（用户口径 2026-09-24），
    # 让"盘中重启"不必干等到下一个定时点位。延迟 + 后台 + 失败只记日志，
    # 详见 `_run_event_alert_on_startup` 的说明。
    asyncio.create_task(
        _run_event_alert_on_startup(app.state.scheduler, runtime),
        name="event-alert-startup-scan")
    # 数据保留启动清理：最多保留 N 年数据点 / N 天新闻，延迟后台执行，
    # 与每日 03:30 的定时作业互补；失败只记日志。
    asyncio.create_task(_run_retention_on_startup(),
                        name="data-retention-startup")
    # 情报流后台预热（用户口径 2026-09-25）：先把**上次落盘的 payload 热加载**
    # 进内存缓存（用户此刻打开页面就是已有数据、0 IO），再起后台循环，每 15 分钟
    # 检查"工作日 + 距上次抓取 ≥2 小时"，满足就在后台抓一次并推送前端。
    # 失败只记日志（预热是优化，不是启动前置条件）。
    if settings.intel_prewarm_enabled:
        try:
            from src.domain.intel import prewarm as _intel_prewarm

            _intel_prewarm.prime_cache()
        except Exception:  # noqa: BLE001 热加载失败只意味着首屏慢一点
            logger.warning("情报流缓存热加载失败（忽略）", exc_info=True)
        # ★ 慢聚合（投资日历 / 舆情热度）的缓存状态**当场报出来**。
        #
        # 为什么只报不建：日历实测要 11 秒（宏观源按天请求财经日历，30 天
        # ≈ 30 次 HTTP 占 8.7 秒），放在启动路径里会和首屏/行情预热抢资源。
        # 重建交给下面的后台循环；这里把"首屏会不会慢"变成启动日志里
        # 一眼可判的事实（用户 2026-09-26 报障"打开要等 5~10 秒"）。
        try:
            _intel_prewarm.prime_slow()
        except Exception:  # noqa: BLE001 只是少一条状态日志
            logger.warning("情报慢聚合缓存状态检查失败（忽略）", exc_info=True)
        asyncio.create_task(
            _run_intel_prewarm(runtime), name="intel-feed-prewarm")
    # ETF 份额自检：最新交易日的份额成片为 NULL 时补拉（每晚都会重演的时序问题，
    # 见 `_ensure_etf_shares_later`）。同样延迟 + 后台，绝不阻塞首屏。
    asyncio.create_task(_ensure_etf_shares_later(),
                        name="etf-share-guard")
    # 板块快照预热：自选池涉及几十个板块，逐个取要几十次子进程（每次 ~1s 固定成本，
    # 主因是 `import akshare`），整表重算实测 110 秒。这里用**一个**子进程全取回来，
    # 之后每只票的快照都命中缓存。放后台任务，不阻塞启动也不影响首个请求。
    #
    # 但**不要立刻开跑**（2026-09-17 实测）：预热要让一个子进程去 `import akshare`
    # 并拉十几个板块，磁盘与 CPU 会把它和首屏请求、`/health` 抢在一起 ——
    # 实测 `/health` 从 3.7 秒（无争抢）退化成 54~142 秒。
    # 让出 60 秒：这段时间足够首屏与运维页各自被服务完，而板块预热晚一分钟
    # 对做T信号没有任何影响（板块是展示项 + 14/100 的权重，见 board.py）。
    if runtime.intraday is not None:
        # 不保存 task 引用：它是"发后不管"的预热，异常已在方法内部消化，
        # 存着反而会在关停时引入"要不要 await"的额外分支。
        asyncio.create_task(
            _warm_board_snapshots_later(runtime), name="intraday-board-warmup")
    yield
    if heartbeat_task is not None:
        heartbeat_task.cancel()
    await app.state.scheduler.stop()
    # 竞价选股的两个调度线程：daemon 线程本来会随进程退出，但显式停更干净
    # （避免关停瞬间正好在写库，留下半写状态）
    try:
        from src.auction_select.scheduler import stop_scheduler as _stop_auction

        _stop_auction()
    except Exception:  # noqa: BLE001 关停是尽力而为，一句失败不影响其余收尾
        logger.debug("竞价选股调度停止失败（忽略）", exc_info=True)
    if runtime.intraday is not None:
        await runtime.intraday.aclose()  # 关闭做T模块的HTTP连接池
    cancelled = await app.state.store.cancel_all()
    if cancelled:
        logger.warning("lifespan关停取消在飞任务 %d 个", cancelled)
    # ⚠️ 关停段的每一句都必须"各自兜住异常"（2026-09-17 实测事故）：
    # `fundflow_repo` 那个仓储按调用建连、**没有 `close()`**，一句
    # `await runtime.fundflow_repo.close()` 抛 AttributeError 就让整个关停段
    # 中断 —— 后面的 `repo.close()` 与**WAL checkpoint 全部没执行**，
    # 于是下次启动又读到脏 `-wal`/`-shm`，前端又要等一分钟。
    # 关停是"尽力而为"，任何一句失败都不该影响其余收尾。
    for label, closer in (
        ("事件告警仓储", getattr(runtime.event_repo, "close", None)),
        ("做T权重档案仓储", getattr(runtime.intraday_profile_repo, "close", None)),
        ("资金流选择列表仓储", getattr(runtime.fundflow_repo, "close", None)),
        ("主数据点仓储", getattr(runtime.repo, "close", None)),
    ):
        if closer is None:
            continue                      # 未装配、或该仓储不需要显式关闭
        try:
            await closer()
        except Exception:  # noqa: BLE001 关停尽力而为，绝不互相拖累
            logger.warning("%s 关闭失败（继续关停收尾）", label, exc_info=True)
    # 关停收尾：把所有登记过的 SQLite 连接 checkpoint 后关闭，并把主库的 WAL
    # 收干。硬杀（taskkill /F）时这段不会执行 —— 那正是下次启动读到陈旧
    # `-wal`/`-shm` 的原因；能走到这里的优雅关停至少要留下一个干净的库。
    try:
        released = release_all_connections()
        cleaned = sum(
            1 for db in _sqlite_paths_to_check() if checkpoint_and_close(db))
        logger.info("关停：释放常驻 SQLite 连接 %d 个，checkpoint 主库 %d 个",
                    released, cleaned)
    except Exception:  # noqa: BLE001 checkpoint 失败不能把关停搞崩
        logger.warning("关停 checkpoint 失败（不影响进程退出）", exc_info=True)
    shutdown_infra_executors()


app = FastAPI(
    title=settings.app_name, version="0.1.0", lifespan=lifespan,
    # ⚠️ **公网环境（prod / pilot）根本不注册交互式文档**。
    #
    # `/docs`、`/redoc`、`/openapi.json` 不需要登录就能打开，会一次性
    # 交出**全部接口面**：路径、参数名、请求体 schema、枚举取值。
    # 对一个面向客户的实例来说，这就是一份给攻击者准备好的踩点清单
    # （实测：dev 实例上未登录访问 `/openapi.json` 返回 200）。
    # dev/test 保留（本地联调要看文档），公网一律不注册 ——
    # 判据抽在 `login_gate.docs_kwargs` 里，与"公网暴露面"的其它规则放在一起，
    # 并且**可单测**（写在 FastAPI(...) 里就没法测了）。
    **docs_kwargs(settings),
)

# 多租户接入层：必须在业务路由**之前**注册中间件，否则路由拿不到 Principal。
# 鉴权强度由 MOSS_TENANCY_ENFORCE 控制，未强制时会在响应头标注 dev-bypass。
logger.info(describe_enforcement())
logger.info("运行环境：%s", describe_environment(settings))

# ★ 中间件顺序（Starlette：**后注册的在更外层**）。
#   目标顺序 = 审计(tenancy) → 登录门槛(gate) → 业务。
#   为什么让 tenancy 在外层：被登录门槛挡掉的请求也要进访问审计，
#   否则"公网上有人在扫接口"这件事在审计里看不到。
app.add_middleware(LoginGateMiddleware)
app.add_middleware(TenancyMiddleware)
# 记账归属（会话 → 租户/用户 + 请求路径）：**只影响审计与计费字段**，
# 不参与任何准入判断，所以放在最外层、并且在它后面不再有拦人的逻辑。
# 放在最外层的原因：它要把上下文包住下面所有中间件与路由（含它们
# `create_task` 出来的后台任务），这样"钱花在哪个功能上"才是精确的。
app.add_middleware(AccountingMiddleware)
app.include_router(api_router)


# ============ 响应压缩（2026-09-26 用户报障：事件告警打开要等 1-2 秒）============
#
# **原来整个应用没有任何压缩**：所有 JSON 响应都是明文发出去的。
#
# 为什么这对本项目是决定性的（不是"常规优化"）：
# 对外试点走的是**公网隧道**，实测带宽只有 **≈51 KB/s**、单次往返 0.4~2 秒
# （见 `web/src/alertsCache.ts` 里记录的实测账）。在这种链路上，
# **响应体积直接等于加载时间**：
#
#     事件告警列表 `/alerts?limit=100`   112 KB  →  ~2.2 s（纯传输）
#     整表 JSON（7 行实测）              9.3 KB  →  2.3 KB（gzip 后）
#
# 而告警内容是**中文事件描述**（占大头）+ 大量重复的字段名与枚举值 ——
# 这正是 gzip 压缩率最高的数据形态。实测列表 payload 压到 ~20%，
# 即隧道上的传输时间降到 1/4~1/5。
#
# 为什么用 `minimum_size` 而不是全压：
# 小响应（如 `/health/live` 那种几十字节的探针）压缩后**反而更大**
# （gzip 头 + CRC 就要 20 字节），而且白费 CPU。512B 以下是纯亏。
#
# ⚠️ 只压 `text/*` 与 `application/json`：静态资源（`web/dist` 的
# js/css）本来就由 Vite 预压缩，重复压缩没有收益；而图片/字体压缩是负收益。
app.add_middleware(
    GZipMiddleware,
    minimum_size=512,
    compresslevel=6,
)


# 全局异常处理器（实现见 src/api/exception_handlers.py）：
# 出错响应一律 {"detail","code"}，5xx 永不外发原始 detail，未捕获异常带 trace_id。
register_handlers(app)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """入口HTML禁缓存（发布新构建后刷新即生效）；hash资源仍走浏览器缓存。"""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# 前端构建产物（web/dist）存在时由同一服务托管，单服务演示
#
# ★ `MOSS_WEB_DIST` 可覆盖托管目录 —— 对外试点实例必须用**冻结的一份**。
#
# 为什么需要它：`StaticFiles` 是**每次请求都从磁盘读**，而 dev 与 pilot
# 跑的是同一个 checkout、同一个 `web/dist`。后果是"我在本地 `npm run build`
# 一跑，客户的页面上就立刻变成那份还没验过的界面"（刷新即生效）——
# 调试前端时这等于把半成品直接推给客户。
# 所以对外实例指向 `web/dist-pilot` 这样一份**经过验证才同步过去**的副本：
#   验证：dev 上看过没问题 → 复制到 dist-pilot → 客户刷新即可见。
# 默认（不设该环境变量）仍是 `web/dist`，本地开发不受影响。
_dist = Path(os.environ.get("MOSS_WEB_DIST")
             or Path(__file__).resolve().parents[2] / "web" / "dist")
if _dist.is_dir():
    from fastapi.staticfiles import StaticFiles

    logger.info("前端静态资源托管目录：%s", _dist)
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
elif os.environ.get("MOSS_WEB_DIST"):
    # 显式指定了却不存在的目录：必须吵，不能静默回落到别的目录
    # （否则"客户看到的到底是哪份前端"就说不清了）
    logger.error("MOSS_WEB_DIST=%s 不存在，前端将无法访问", _dist)
