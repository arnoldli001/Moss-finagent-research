"""板块概念拥挤度（sector crowding）。

口径：板块成交额 / 全市场成交额 → MA5 平滑 → 相对**近 6 年**最大值的水位。
见 `docs/SECTOR_CROWDING.md`。
"""

from __future__ import annotations

from src.sector_crowding.config import (
    SectorCrowdingConfig,
    clear_config_cache,
    is_concept_board,
    load_config,
)
from src.sector_crowding.db import (
    get_db_connection,
    init_tables,
    query_alerts,
    query_all_latest_water_level,
    query_sector_crowding,
    upsert_sector_crowding,
)
from src.sector_crowding.refresh import (
    calculate_and_store,
    compute_series,
    get_refresh_progress,
    refresh_all_incremental,
    refresh_single_sector,
    start_refresh_all,
)

__all__ = [
    "SectorCrowdingConfig",
    "calculate_and_store",
    "clear_config_cache",
    "compute_series",
    "get_db_connection",
    "get_refresh_progress",
    "init_tables",
    "is_concept_board",
    "load_config",
    "query_alerts",
    "query_all_latest_water_level",
    "query_sector_crowding",
    "refresh_all_incremental",
    "refresh_single_sector",
    "start_refresh_all",
    "upsert_sector_crowding",
]
