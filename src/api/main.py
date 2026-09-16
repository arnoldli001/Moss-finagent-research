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
from src.core.config import get_settings
from src.core.exceptions import ConfigError
from src.scheduler.run_log import RunLog
from src.scheduler.service import CronScheduler

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = build_runtime()
    app.state.runtime = runtime
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
        logger.warning("事件告警子系统不可用（降级）: %s", str(exc)[:200])
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
    # 做T辅助：盘中（09:15~11:30 / 13:00~15:00）每分钟把全部自选标的的分钟数据
    # 取到最新。**放在服务端**而不是前端定时器：这样"自选股自动刷新"不依赖浏览器
    # 是否开着，多标签页也只算一次，且信号推送在无人看盘时照常触发。
    if runtime.intraday is not None:
        await runtime.intraday.start_watchlist_refresh()
        # 报价快车道：现价/涨跌幅比打分快一个数量级（批量 tick 实测 9 只 0.54ms）
        await runtime.intraday.start_quote_refresh()
    # 数据健康度预热：要遍历 3.5 万个分区清单 + 查 14GB 仓库（实测 6~28 秒 I/O），
    # 放后台线程算一次，用户第一次打开「运行指标」页就是热的（不再一直转圈）。
    warm_data_health(runtime)
    yield
    if heartbeat_task is not None:
        heartbeat_task.cancel()
    await app.state.scheduler.stop()
    if runtime.intraday is not None:
        await runtime.intraday.aclose()  # 关闭做T模块的HTTP连接池
    cancelled = await app.state.store.cancel_all()
    if cancelled:
        logger.warning("lifespan关停取消在飞任务 %d 个", cancelled)
    if runtime.event_repo is not None:
        await runtime.event_repo.close()
    if runtime.intraday_profile_repo is not None:
        await runtime.intraday_profile_repo.close()
    await runtime.repo.close()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
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
