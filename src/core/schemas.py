"""core层共享Schema：置信度、数据引用、推理步骤、数据点契约。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    """LLM输出与分析结论的置信度分级。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def coerce_confidence(value: Any) -> Confidence:
    """LLM偶发输出'medium-low'等非枚举值时容错映射，避免整任务失败。"""
    text = str(value or "").strip().lower()
    if text in ("high", "medium", "low"):
        return Confidence(text)
    if "high" in text:
        return Confidence.HIGH
    if "low" in text:
        return Confidence.LOW
    return Confidence.MEDIUM


StepType = Literal[
    "data_retrieval",
    "indicator_calculation",
    "signal_trigger",
    "llm_inference",
    "cross_validation",
    "final_conclusion",
    "skill_injection",
]
"""推理步骤类型（OBSERVABILITY.md 二）。"""


class DataSourceRef(BaseModel):
    """数据溯源引用（最小集，完整契约见 DataPoint）。"""

    data_id: str = Field(default_factory=lambda: f"d_{uuid4().hex}")
    source_name: str
    source_url: str


class TraceStep(BaseModel):
    """推理路径单步记录（docs/OBSERVABILITY.md 一）。"""

    step: int
    step_type: StepType
    description: str
    data_refs: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int | None = None


class DataSourceType(str, Enum):
    """数据源类型（DATA_CONTRACT.md source_type）。"""

    OFFICIAL = "official"
    API = "api"
    FILE = "file"
    REPORT = "report"
    NEWS = "news"
    #: ★ 2026-09-30：**算出来的**（派生指标，如 `净息差:600036`）。
    #:
    #: 为什么要单独一个取值而不是复用 `report`：下游要能一眼分辨
    #: "这是原始读数"还是"这是用公式算的"（口径/偏置随 `extra` 下发），
    #: 也便于按来源筛选与审计。判据只认枚举，不认人话文案。
    DERIVED = "derived"


class FetchMethod(str, Enum):
    """数据获取方式（DATA_CONTRACT.md fetch_method）。"""

    WEB_CRAWL = "web_crawl"
    API_CALL = "api_call"
    FILE_READ = "file_read"
    MANUAL_INPUT = "manual_input"
    # 降级/代理值：主源不可用时用独立故障域备源或常数代理
    FALLBACK = "fallback"
    #: ★ 2026-09-30：本地按公式**计算**得出（没有网络往返、没有源）。
    COMPUTED = "computed"


def hash_content(raw: Any) -> str:
    """对原始内容计算SHA256哈希（raw_content_hash字段）。

    任意可序列化对象：dict先转稳定JSON，其余转str。
    """
    if isinstance(raw, dict):
        normalized = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
    else:
        normalized = str(raw)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class DataPoint(BaseModel):
    """统一数据点契约（业务字段 + docs/DATA_CONTRACT.md 溯源元数据12字段）。

    core层持有该契约以便domain与infrastructure共享，避免反向依赖。
    """

    # ---- 业务字段 ----
    indicator: str
    """指标名，如 "CPI"、"stock_close:000001" """
    value: float | None = None
    unit: str | None = None
    period_date: str | None = None
    """数据所属期间，ISO格式（如 2026-08-01）"""
    extra: dict[str, Any] = Field(default_factory=dict)
    """原始行兜底字段"""

    # ---- 溯源元数据（DATA_CONTRACT.md 强制12字段）----
    data_id: str = Field(default_factory=lambda: f"d_{uuid4().hex}")
    source_name: str = ""
    source_url: str = ""
    source_type: DataSourceType = DataSourceType.API
    publish_time: datetime | None = None
    fetch_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fetch_method: FetchMethod = FetchMethod.API_CALL
    raw_content_hash: str = ""
    processed_by: str = "A01_data_collector"
    process_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = 1.0
    verified: bool = False

    def model_post_init(self, __context: Any) -> None:
        """raw_content_hash缺省时按extra+indicator自动计算。"""
        if not self.raw_content_hash:
            self.raw_content_hash = hash_content(
                {"indicator": self.indicator, "extra": self.extra}
            )
