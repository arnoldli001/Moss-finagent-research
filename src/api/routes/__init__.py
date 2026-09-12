"""API路由注册。"""

from fastapi import APIRouter

from src.api.routes.data import router as data_router
from src.api.routes.research import router as research_router

api_router = APIRouter()
api_router.include_router(research_router)
api_router.include_router(data_router)
