"""事件采集器基类。

采集器返回 (原始条目列表, 错误列表)：单源失败不抛出，由上层记入data_gaps。
原始条目的标准键（canonical raw item）：
    title / content / source_name / source_url / publish_time /
    type_hint(policy|sector|stock|calendar|"") / extra(dict)
"""

from __future__ import annotations

from typing import Any, Protocol


def iter_rows(df: Any):
    """空DataFrame安全迭代（禁止 `df or []`：DataFrame真值歧义）。"""
    if df is None or len(df) == 0:
        return ()
    return df.itertuples(index=False)


class BaseEventCollector(Protocol):
    """事件采集器端口（结构化鸭子类型，便于测试替身）。"""

    source_name: str

    async def collect(self) -> tuple[list[dict], list[str]]: ...


def make_raw_item(
    *,
    title: str,
    content: str,
    source_name: str,
    source_url: str = "",
    publish_time: str = "",
    type_hint: str = "",
    extra: dict | None = None,
) -> dict:
    """构造采集器间统一的原始事件条目（入库前由normalize模块标准化）。"""
    return {
        "title": str(title or "")[:200],
        "content": str(content or "")[:2000],
        "source_name": source_name,
        "source_url": str(source_url or ""),
        "publish_time": str(publish_time or ""),
        "type_hint": type_hint,
        "extra": extra or {},
    }
