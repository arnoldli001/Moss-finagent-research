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

    # 数据层
    postgres_dsn: str = "postgresql+asyncpg://finagent:finagent@localhost:5432/finagent"
    sqlite_dsn: str = "sqlite:///data/finagent.db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"

    # 数据源Token
    tushare_token: str = ""


@lru_cache
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()
