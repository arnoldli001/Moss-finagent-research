"""FastAPI应用入口（启动：uvicorn src.api.main:app）。

lifespan：启动时组装Runtime（Agent注册表+StateGraph），关停时取消在飞任务。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from src.api.data_health import warm as warm_data_health
from src.api.event_wiring import build_event_stack
from src.api.routes import api_router
from src.api.runtime import build_runtime
from src.api.tasks import TaskStore
from src.api.tenancy_middleware import TenancyMiddleware, describe_enforcement
from src.core.config import get_settings
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


async def _warm_board_snapshots_later(runtime) -> None:
    """延后做板块快照预热；异常只记日志（预热失败不影响任何功能）。"""
    try:
        await asyncio.sleep(_BACKGROUND_WARM_DELAY)
        await runtime.intraday.warm_board_snapshots()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 预热是优化，绝不能影响服务
        logger.warning("板块快照预热失败（忽略）", exc_info=True)


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
        # （日K买入信号 + 分时打分>40 → 综合分前 6 自动加自选），盘后 15:05 再补一次。
        # 放服务端而不是前端：这两个窗口很短，浏览器不开着就完全错过。
        await runtime.intraday.start_auto_select()
    # 数据健康度预热：要遍历 3.5 万个分区清单 + 查 14GB 仓库（实测 6~28 秒 I/O），
    # 放后台线程算一次，用户第一次打开「运行指标」页就是热的（不再一直转圈）。
    warm_data_health(runtime)
    # 集合竞价选股调度：开市日 09:15 预热、09:25 出池（要求 09:27 前）。
    # 放**服务进程内**的守护线程，不额外起服务、不改系统计划任务 ——
    # 竞价数据是盘中实时的，取完要立刻算分落库给前端，独立进程还要额外解决
    # SQLite 双写与结果传递。放后台任务启动，不阻塞首屏。
    asyncio.create_task(_start_auction_scheduler_later(),
                        name="auction-select-scheduler")
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


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

# 多租户接入层：必须在业务路由**之前**注册中间件，否则路由拿不到 Principal。
# 鉴权强度由 MOSS_TENANCY_ENFORCE 控制，未强制时会在响应头标注 dev-bypass。
logger.info(describe_enforcement())
app.add_middleware(TenancyMiddleware)
app.include_router(api_router)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """入口HTML禁缓存（发布新构建后刷新即生效）；hash资源仍走浏览器缓存。"""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# 前端构建产物（web/dist）存在时由同一服务托管，单服务演示
_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
if _dist.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
