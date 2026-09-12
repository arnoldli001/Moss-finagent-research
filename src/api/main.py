"""FastAPI应用入口（启动：uvicorn src.api.main:app）。"""

from __future__ import annotations

from fastapi import FastAPI

from src.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/api/v1/health")
async def health() -> dict:
    """健康检查（API_REFERENCE.md 五）。

    agents/data_sources 状态在后续阶段接入各Agent health_check聚合。
    """
    return {
        "status": "healthy",
        "agents": {},
        "data_sources": {},
    }
