"""API路由注册。"""

import logging

from fastapi import APIRouter

from src.api.routes.admin import router as admin_router
from src.api.routes.admin_platform import router as admin_platform_router
from src.api.routes.alerts import router as alerts_router
from src.api.routes.auth import router as auth_router
from src.api.routes.backtest import router as backtest_router
from src.api.routes.code_engineer import router as code_engineer_router
from src.api.routes.data import router as data_router
from src.api.routes.fundflow import router as fundflow_router
from src.api.routes.intraday import router as intraday_router
from src.api.routes.intraday_weights import router as intraday_weights_router
from src.api.routes.intel import router as intel_router
from src.api.routes.mainline import router as mainline_router
from src.api.routes.metrics import router as metrics_router
from src.api.routes.my_features import router as my_features_router
from src.api.routes.my_profiles import router as my_profiles_router
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
# 认证排在最前：它是所有其它模块的前置能力（登录/改密/找回不依赖 runtime 装配）
api_router.include_router(auth_router)
# 管理员控制台紧随其后：注册是**审批制**，没有它就没人能放行新用户
api_router.include_router(admin_router)
# 管理员 · 平台侧：套餐资源管控 / 功能权限 / 资源监控
api_router.include_router(admin_platform_router)
# 个股口径：每用户各自一份权重/阈值（旧表主键只有 code，结构上存不下两份）
# ⚠️ `my_pools_router`（「我的自选池」）**已删除**（用户口径 2026-09-23：
#    "很鸡肋，不需要了"）。加自选/删自选在做T面板里直接写
#    `configs/intraday.yaml`，从来不走那张个人池表 —— 所以删掉它没有
#    动到日常路径；个股口径（下面这个）是**另一张表**，继续保留。
api_router.include_router(my_profiles_router)
# 当前用户可见的功能页签（由套餐权限决定，前端照着渲染）
api_router.include_router(my_features_router)
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
# 主线挖掘：挂在「资金流监控」页签左侧面板后面（前缀 /api/v1/mainline）
api_router.include_router(mainline_router)
# 舆情情报：情报雷达 / 盘前简报 / 事件处置（前缀 /api/v1/intel）
# 数据源标识不出接口，见 docs/INTEL_PERMISSION_DESIGN.md §5
api_router.include_router(intel_router)
if auction_select_router is not None:
    api_router.include_router(auction_select_router)
