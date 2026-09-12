"""应用配置。

敏感项（API Key、密码、Token）一律经环境变量注入，禁止硬编码；
仅本地Demo默认值（如postgres口令）允许作为兜底，生产必须覆盖。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置（.env / 环境变量自动加载）。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 应用
    app_name: str = "FinAgent-Research"
    debug: bool = False
    log_level: str = "INFO"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # 模型网关
    ollama_base_url: str = "http://localhost:11434"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""  # 仅经环境变量注入
    model_config_path: str = "configs/models.yaml"
    llm_timeout_seconds: float = 120.0
    llm_cache_enabled: bool = True
    llm_cache_dir: str = "data/llm_cache"
    llm_cache_ttl_hours: float = 24.0
    llm_semantic_threshold: float = 0.85  # n-gram余弦≥该值判语义命中
    llm_audit_dir: str = "data/audit"

    # 数据层
    data_backend: str = "sqlite"  # sqlite | postgres（经DATA_BACKEND环境变量切换）
    sqlite_path: str = "data/finagent.db"
    postgres_dsn: str = "postgresql+asyncpg://finagent:finagent@localhost:5432/finagent"
    sqlite_dsn: str = "sqlite:///data/finagent.db"
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_enabled: bool = False  # 开启后query_points走Redis缓存（Redis不可达自动降级）
    data_cache_ttl_seconds: int = 300
    celery_broker_url: str = "redis://localhost:6379/1"
    scheduler_dir: str = "data/scheduler"  # 定时作业运行记录（runs.jsonl）
    scheduler_run_log_ttl_days: int = 90  # 运行记录保留天数（audit_cleanup作业清理）

    # 数据源Token
    tushare_token: str = ""


@lru_cache
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()
