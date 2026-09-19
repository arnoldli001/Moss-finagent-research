"""A04 输入输出模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StoragePayload(BaseModel):
    """A04任务入参：待入库的DataPoint字典列表。"""

    data_points: list[dict[str, Any]] = Field(default_factory=list)
