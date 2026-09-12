"""FastAPI应用入口（启动：uvicorn src.api.main:app）。

lifespan：启动时组装Runtime（Agent注册表+StateGraph），关停时取消在飞任务。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from src.api.routes import api_router
from src.api.runtime import build_runtime
from src.api.tasks import TaskStore
from src.core.config import get_settings
from src.scheduler.run_log import RunLog

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runtime = build_runtime()
    app.state.store = TaskStore()
    app.state.run_log = RunLog(f"{settings.scheduler_dir}/runs.jsonl")
    yield
    cancelled = await app.state.store.cancel_all()
    if cancelled:
        import logging

        logging.getLogger(__name__).warning("lifespan关停取消在飞任务 %d 个", cancelled)
    await app.state.runtime.repo.close()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(api_router)

# 前端构建产物（web/dist）存在时由同一服务托管，单服务演示
_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
if _dist.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
