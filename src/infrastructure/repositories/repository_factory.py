"""仓储工厂：按Settings组装 后端(SQLite/PostgreSQL) + 可选Redis缓存装饰。

装配规则：
- DATA_BACKEND=sqlite（默认）→ SQLite文件库，零外部依赖；
- DATA_BACKEND=postgres → asyncpg连接池（懒连接，无服务不阻断启动，首次IO报错带排障提示）；
- REDIS_CACHE_ENABLED=true → 任意后端外再包一层CachedRepository，Redis不可达自动fail-open。
"""

from __future__ import annotations

from src.core.config import Settings
from src.core.exceptions import ConfigError
from src.domain.alerts.repository import EventRepository
from src.domain.intraday.repository import IntradayProfileRepository
from src.infrastructure.repositories.base import DataPointRepository
from src.infrastructure.repositories.cached_repo import CachedRepository
from src.infrastructure.repositories.event_sqlite_repo import EventSqliteRepository
from src.infrastructure.repositories.intraday_profile_sqlite_repo import (
    IntradayProfileSqliteRepository,
)
from src.infrastructure.repositories.macro_repo import MacroRepository
from src.infrastructure.repositories.news_cache_sqlite_repo import (
    NewsCacheRepository,
    NewsCacheSqliteRepository,
)
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


def build_event_repository(settings: Settings) -> EventRepository:
    """构建事件/告警仓储（当前仅SQLite；postgres实现未纳入本版范围）。"""
    backend = settings.data_backend.strip().lower()
    if backend == "postgres":
        raise ConfigError(
            "事件告警仓储暂仅支持 DATA_BACKEND=sqlite；"
            "请使用SQLite或在后续版本补充PostgreSQL事件表实现。"
        )
    return EventSqliteRepository(settings.sqlite_path)


def build_intraday_profile_repository(
    settings: Settings,
) -> IntradayProfileRepository:
    """构建做T权重档案仓储（当前仅SQLite；postgres实现未纳入本版范围）。

    与做T权重档案的姊妹能力「个股微调」的分工：
    `configs/intraday.yaml` 的 `overrides:` 段继续可用（手工编辑、可版本化），
    本仓储是**可被前端编辑的主档案**，优先级更高。
    两者同时存在时以档案库为准，并在面板 gaps 里明说是哪一份在生效。
    """
    backend = settings.data_backend.strip().lower()
    if backend not in _BACKENDS:
        raise ConfigError(
            f"未知DATA_BACKEND={settings.data_backend!r}，可选：{', '.join(_BACKENDS)}"
        )
    if backend == "postgres":
        raise ConfigError(
            "做T权重档案暂仅支持 DATA_BACKEND=sqlite；"
            "请使用SQLite或补充PostgreSQL实现（表结构见 intraday_profile_sqlite_repo）。"
        )
    return IntradayProfileSqliteRepository(settings.sqlite_path)


def build_news_cache_repository(settings: Settings) -> NewsCacheRepository:
    """构建新闻/快讯缓存仓储（当前仅SQLite；新闻为增强链路）。"""
    backend = settings.data_backend.strip().lower()
    if backend not in _BACKENDS:
        raise ConfigError(
            f"未知DATA_BACKEND={settings.data_backend!r}，可选：{', '.join(_BACKENDS)}"
        )
    if backend == "postgres":
        raise ConfigError(
            "新闻缓存仓储暂仅支持 DATA_BACKEND=sqlite；"
            "请使用SQLite或补充PostgreSQL实现（表结构见 news_cache_sqlite_repo）。"
        )
    return NewsCacheSqliteRepository(settings.sqlite_path)

