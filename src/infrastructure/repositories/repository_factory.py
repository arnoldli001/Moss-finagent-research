"""仓储工厂：按Settings组装 后端(SQLite/PostgreSQL) + 可选Redis缓存装饰。

装配规则：
- DATA_BACKEND=sqlite（默认）→ SQLite文件库，Demo零外部依赖；
- DATA_BACKEND=postgres → asyncpg连接池（懒连接，无服务不阻断启动，首次IO报错带排障提示）；
- REDIS_CACHE_ENABLED=true → 任意后端外再包一层CachedRepository，Redis不可达自动fail-open。
"""

from __future__ import annotations

from src.core.config import Settings
from src.core.exceptions import ConfigError
from src.infrastructure.repositories.base import DataPointRepository
from src.infrastructure.repositories.cached_repo import CachedRepository
from src.infrastructure.repositories.macro_repo import MacroRepository
from src.infrastructure.repositories.postgres_repo import PostgresRepository

_BACKENDS = ("sqlite", "postgres")


def build_repository(settings: Settings) -> DataPointRepository:
    """按配置构建数据点仓储（含可选缓存层）。"""
    backend = settings.data_backend.strip().lower()
    if backend not in _BACKENDS:
        raise ConfigError(
            f"未知DATA_BACKEND={settings.data_backend!r}，可选：{', '.join(_BACKENDS)}"
        )

    if backend == "postgres":
        repo: DataPointRepository = PostgresRepository(settings.postgres_dsn)
    else:
        repo = MacroRepository(settings.sqlite_path)

    if settings.redis_cache_enabled:
        repo = CachedRepository(
            repo, settings.redis_url, ttl_seconds=settings.data_cache_ttl_seconds
        )
    return repo
