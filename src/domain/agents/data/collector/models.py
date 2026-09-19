"""A01 输入输出模型。"""

from __future__ import annotations

from pydantic import BaseModel


class CollectorPayload(BaseModel):
    """A01任务入参：指标与可选时间窗。"""

    indicator: str
    """指标名，如 "CPI"、"PPI"、"stock_close:000001" """
    start_date: str | None = None
    end_date: str | None = None
