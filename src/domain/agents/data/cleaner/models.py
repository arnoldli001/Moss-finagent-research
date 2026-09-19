"""A02 输入输出模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CleanerPayload(BaseModel):
    """A02任务入参：待清洗的DataPoint字典列表（来自A01 result）。"""

    data_points: list[dict[str, Any]] = Field(default_factory=list)
