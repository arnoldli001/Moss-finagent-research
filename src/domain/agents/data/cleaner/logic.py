"""A02 清洗逻辑（纯函数）：期间格式ISO标准化、数值float化、按业务键去重。"""

from __future__ import annotations

from datetime import datetime

from src.core.schemas import DataPoint

_PERIOD_FORMATS = ("%Y-%m-%d", "%Y-%m", "%Y%m%d", "%Y年%m月", "%Y/%m/%d", "%Y年%m月%d日")


def normalize_period(raw: str | None) -> str | None:
    """将常见中文/紧凑日期格式转为ISO日期；无法解析时原样返回。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in _PERIOD_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def normalize_points(points: list[DataPoint]) -> list[DataPoint]:
    """标准化：period_date转ISO、value强转float（失败置None）。"""
    normalized: list[DataPoint] = []
    for point in points:
        value: float | None = None
        if point.value is not None:
            try:
                value = float(point.value)
            except (TypeError, ValueError):
                value = None
        normalized.append(
            point.model_copy(
                update={"period_date": normalize_period(point.period_date), "value": value}
            )
        )
    return normalized


def deduplicate_points(
    points: list[DataPoint],
) -> tuple[list[DataPoint], list[str]]:
    """按业务键(indicator, period_date, value)去重，返回(保留结果, 剔除data_id列表)。"""
    seen: set[tuple[str, str | None, float | None]] = set()
    kept: list[DataPoint] = []
    removed: list[str] = []
    for point in points:
        key = (point.indicator, point.period_date, point.value)
        if key in seen:
            removed.append(point.data_id)
            continue
        seen.add(key)
        kept.append(point)
    return kept, removed
