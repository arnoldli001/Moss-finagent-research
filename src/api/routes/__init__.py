"""API路由注册。"""

from fastapi import APIRouter

from src.api.routes.alerts import router as alerts_router
from src.api.routes.backtest import router as backtest_router
from src.api.routes.code_engineer import router as code_engineer_router
from src.api.routes.data import router as data_router
from src.api.routes.intraday import router as intraday_router
from src.api.routes.intraday_weights import router as intraday_weights_router
from src.api.routes.metrics import router as metrics_router
from src.api.routes.quant import router as quant_router
from src.api.routes.research import router as research_router
from src.api.routes.scheduler import router as scheduler_router
from src.api.routes.skills import router as skills_router

api_router = APIRouter()
api_router.include_router(alerts_router)
api_router.include_router(research_router)
api_router.include_router(data_router)
api_router.include_router(scheduler_router)
api_router.include_router(metrics_router)
api_router.include_router(backtest_router)
api_router.include_router(intraday_router)
api_router.include_router(intraday_weights_router)
api_router.include_router(quant_router)
api_router.include_router(code_engineer_router)
api_router.include_router(skills_router)
