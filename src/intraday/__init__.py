"""股票做T辅助模块（Intraday T-Assist）。

四面板一体：
  1. 估值空间      —— 个股PE/PB三年分位 + 同业/行业中位数对比（判断上涨空间 vs 估值透支）
  2. 分时图与提示点 —— 个股与关联板块分时，叠加做T三角标记与关键价位线
  3. 多指标合成打分 —— 七因子加权模型（权重合计100，得分∈[-1,1]）核心引擎
  4. 消息面与情绪   —— 板块涨跌家数 + 个股相对板块强度 + DeepSeek新闻情绪

入口：IntradayService（src/intraday/service.py）→ FastAPI /api/v1/intraday/*
"""

from src.intraday.config import IntradayConfig, load_intraday_config
from src.intraday.engine import compute_levels, decide_signal, run_engine, zone_for
from src.intraday.models import IntradaySnapshot
from src.intraday.sources import IntradayDataProvider

__all__ = [
    "IntradayConfig",
    "IntradayDataProvider",
    "IntradaySnapshot",
    "compute_levels",
    "decide_signal",
    "load_intraday_config",
    "run_engine",
    "zone_for",
]
