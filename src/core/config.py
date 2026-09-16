"""应用配置。

敏感项（API Key、密码、Token）一律经环境变量注入，禁止硬编码；
仅本地Demo默认值（如postgres口令）允许作为兜底，生产必须覆盖。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置（.env / 环境变量自动加载）。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 应用
    app_name: str = "Moss-FinAgent-Research"
    debug: bool = False
    log_level: str = "INFO"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8100

    # 模型网关
    ollama_base_url: str = "http://localhost:11434"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = os.environ.get("DEEPSEEK_API_KEY", "")  # 仅经环境变量注入
    model_config_path: str = "configs/models.yaml"
    llm_timeout_seconds: float = 120.0
    llm_cache_enabled: bool = True
    llm_cache_dir: str = "data/llm_cache"
    llm_cache_ttl_hours: float = 24.0
    llm_semantic_threshold: float = 0.85  # n-gram余弦≥该值判语义命中
    llm_audit_dir: str = "data/audit"
    # Token预算：单任务DeepSeek调用累计token上限，超过则拒绝后续调用（防超支）
    llm_token_budget_per_task: int = 30000
    # 单次调用max_tokens上限（由ModelSpec.max_tokens控制，这里是硬截断）
    # 4096：推理模型思维链+正文输出在3072推理+1024输出下足够完成结构化JSON；
    # 过大（如8192）导致推理链冗长、延迟翻倍、成本翻倍且正文常被截断为残缺JSON
    llm_max_tokens_hard_cap: int = 4096
    # 单次调用输入prompt字符数硬上限（防数据淹没用户问题，约2000 token）
    llm_input_char_hard_cap: int = 6000

    # 数据层
    data_backend: str = "sqlite"  # sqlite | postgres（经DATA_BACKEND环境变量切换）
    sqlite_path: str = "data/moss_finagent.db"
    postgres_dsn: str = "postgresql+asyncpg://moss_finagent:moss_finagent@localhost:5432/moss_finagent"
    sqlite_dsn: str = "sqlite:///data/moss_finagent.db"
    # 本地QMT导出CSV行情目录（如 D:/quantTrader/data，含SH/SZ子目录）；空则不启用
    local_quote_dir: str = os.environ.get("LOCAL_QUOTE_DIR", "")
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_enabled: bool = False  # 开启后query_points走Redis缓存（Redis不可达自动降级）
    data_cache_ttl_seconds: int = 300
    celery_broker_url: str = "redis://localhost:6379/1"
    scheduler_dir: str = "data/scheduler"  # 定时作业运行记录（runs.jsonl）
    scheduler_run_log_ttl_days: int = 90  # 运行记录保留天数（audit_cleanup作业清理）

    # 数据源Token
    tushare_token: str = os.environ.get("TUSHARE_TOKEN", "")

    # 做T辅助模块（多因子打分 + 做T信号）
    # 策略参数（权重/阈值/箱体指数/板块/自选池）在 configs/intraday.yaml，按mtime热重载
    intraday_config_path: str = "configs/intraday.yaml"
    intraday_enabled: bool = True

    # 事件监控与自动告警（事件采集→LLM分析→阈值告警→WebSocket/邮件）
    alert_scan_candidate_limit: int = 30       # 单次扫描进入LLM的候选事件上限
    alert_keywords: str = os.environ.get(
        "ALERT_KEYWORDS", "")                  # 逗号分隔自定义预筛词（空则仅内置）
    alert_confidence_min: float = 0.70         # 置信度门槛，低于则不推送
    alert_risk_high: int = 75                  # 风险分高/中/低阈值
    alert_risk_medium: int = 60
    alert_risk_low: int = 45
    alert_opp_high: int = 80                   # 机会分高/中/低阈值
    alert_opp_medium: int = 65
    alert_opp_low: int = 50
    alert_expire_days: int = 7                 # 告警有效期（天）
    alert_cooldown_hours: int = 24             # 同事件同类型告警冷却（小时）
    alert_list_limit: int = 100                # 列表接口单次上限
    # 站内告警最低级别（解决规格AC-5：默认medium+才产生告警，low档事件仅入库；
    # 调成 low 后 [45,60)/[50,65) 分段也会生成低级告警）
    alert_min_level: str = "medium"
    # 邮件通道（QQ邮箱SMTP；授权码经环境变量注入，未配置则自动降级为站内告警）
    # 邮件门控按告警类型与分数（替代原alert_email_min_level级别门槛）：
    # 风险类 risk_score 严格 > alert_email_risk_min_score（默认>69）；
    # 机会类 opportunity_score >= alert_email_opp_min_score（默认≥85）。
    alert_email_risk_min_score: float = 69.0
    alert_email_opp_min_score: float = 85.0
    alert_email_min_level: str = "high"        # 已废弃（旧级别门槛），保留兼容旧env
    alert_smtp_host: str = "smtp.qq.com"
    alert_smtp_port: int = 465
    alert_smtp_user: str = os.environ.get("ALERT_SMTP_USER", "")
    alert_smtp_auth_code: str = os.environ.get("ALERT_SMTP_AUTH_CODE", "")
    alert_email_to: str = "1027312283@qq.com"
    alert_email_from_name: str = "Moss投研事件告警"

    @property
    def alert_email_enabled(self) -> bool:
        """邮件通道是否具备发送条件（按授权码与收件人推导，T1）。"""
        return bool(
            self.alert_smtp_user and self.alert_smtp_auth_code
            and self.alert_email_to)


@lru_cache
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()
