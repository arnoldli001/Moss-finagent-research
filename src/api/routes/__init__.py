"""API路由注册。"""

import logging

from fastapi import APIRouter

from src.api.routes.alerts import router as alerts_router
from src.api.routes.backtest import router as backtest_router
from src.api.routes.code_engineer import router as code_engineer_router
from src.api.routes.data import router as data_router
from src.api.routes.fundflow import router as fundflow_router
from src.api.routes.intraday import router as intraday_router
from src.api.routes.intraday_weights import router as intraday_weights_router
from src.api.routes.metrics import router as metrics_router
from src.api.routes.quant import router as quant_router
from src.api.routes.research import router as research_router
from src.api.routes.scheduler import router as scheduler_router
from src.api.routes.sector_crowding import router as sector_crowding_router
from src.api.routes.skills import router as skills_router

# 以下两个模块属私有商业版资产（竞价选股/量化选股），公开仓库不含其实现；
# 本地持有文件时正常注册，缺失时静默跳过，保证开源版可直接启动。
try:
    from src.api.routes.auction_select import router as auction_select_router
except ImportError:  # 公开版：竞价选股未包含
    auction_select_router = None
    logging.getLogger(__name__).info("竞价选股模块未包含（商业版功能），跳过路由注册")

try:
    from src.api.routes.quant_select import router as quant_select_router
except ImportError:  # 公开版：量化选股未包含
    quant_select_router = None
    logging.getLogger(__name__).info("量化选股模块未包含（商业版功能），跳过路由注册")

api_router = APIRouter()
api_router.include_router(alerts_router)
api_router.include_router(research_router)
api_router.include_router(data_router)
api_router.include_router(scheduler_router)
api_router.include_router(metrics_router)
api_router.include_router(backtest_router)
api_router.include_router(intraday_router)
api_router.include_router(intraday_weights_router)
api_router.include_router(fundflow_router)
api_router.include_router(quant_router)
if quant_select_router is not None:
    api_router.include_router(quant_select_router)
api_router.include_router(code_engineer_router)
api_router.include_router(skills_router)
api_router.include_router(sector_crowding_router)
if auction_select_router is not None:
    api_router.include_router(auction_select_router)
