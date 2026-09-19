"""A03 输入输出模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValidatorPayload(BaseModel):
    """A03任务入参：待校验的DataPoint字典列表。"""

    data_points: list[dict[str, Any]] = Field(default_factory=list)
