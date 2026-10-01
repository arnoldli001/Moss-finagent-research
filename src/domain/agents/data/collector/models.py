"""A01 输入输出模型。"""

from __future__ import annotations

from pydantic import BaseModel


class CollectorPayload(BaseModel):
    """A01任务入参：指标与可选时间窗。"""

    indicator: str
    """指标名，如 "CPI"、"PPI"、"stock_close:000001" """
    start_date: str | None = None
    end_date: str | None = None
    deadline_sec: float | None = None
    """★ 防撞钟秒数（**只有交互路径传**）：超过它就终止这条指标的取数。

    `None`（缺省）= 不限时 —— 定时作业与预热路径走这支，行为与加这个字段之前
    逐字一致。交互路径传 `src/core/intel_limits.py::QUERY_DEADLINE_SEC`
    （默认 10s，实测依据见该模块）。为什么是"按调用方传"而不是"全局默认":
    把预热路径也掐在 10 秒，"质押整表/双创截面"这类重活就永远养不起缓存，
    交互侧反而永远超时 —— 那是"修一个坏一个"。
    """
