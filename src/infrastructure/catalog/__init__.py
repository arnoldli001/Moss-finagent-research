"""指标索引清单子包（Indicator Catalog）。

导出：
  - IndicatorMeta / IndicatorRegistry / get_registry（registry）
  - CatalogRepository / get_catalog_repository（DB-backed 运行时索引）
  - SmartFetcher（DB-first → 网络 fallback → 持久化）

注意：本子包的某些模块互相引用，故采用字符串路径导出。
具体模块导入请直接 `from src.infrastructure.catalog.registry import ...`，
不要从本 __init__ 一次性 import 全部（避免循环）。
"""

# 显式导出 registry（无循环依赖）
from src.infrastructure.catalog.registry import (
    FREQUENCIES,
    FreshnessState,
    IndicatorMeta,
    IndicatorRegistry,
    get_registry,
    reset_registry_for_test,
)

__all__ = [
    "FREQUENCIES",
    "FreshnessState",
    "IndicatorMeta",
    "IndicatorRegistry",
    "get_registry",
    "reset_registry_for_test",
    # 以下模块在 catalog_repo.py / smart_fetch.py 中按需 import
    "CatalogRepository",
    "SmartFetcher",
    "SmartFetchResult",
    "get_catalog_repository",
]