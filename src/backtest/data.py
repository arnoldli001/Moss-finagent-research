"""回测数据对齐：DataPoint序列 → 月度Bar（发布月对齐，无未来函数）。"""

from __future__ import annotations

import re

from src.backtest.signals import Bar

_MONTH_RE = re.compile(r"(\d{4})\D{0,2}(\d{1,2})")


def month_key(text: str | None) -> str | None:
    """从'2025-07-09'/'2025年07月'等文本提取YYYY-MM；非法返回None。"""
    if not text:
        return None
    m = _MONTH_RE.search(str(text))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def align_monthly(
    indicator_points: list, price_points: list, indicator_name: str
) -> list[Bar]:
    """月度指标 + 日频收盘价 → 内连接对齐的月末Bar序列。

    - 指标按period_date所在月归并（同月多条以后到者为准，视作修订）；
    - 价格同月取日期最大的一条（月末收盘）；
    - 指标period_date语义为发布日时，按发布月对齐即保证信号不偷看。
    """
    indicator_by_month: dict[str, float] = {}
    for p in indicator_points:
        key = month_key(getattr(p, "period_date", None))
        value = getattr(p, "value", None)
        if key and isinstance(value, (int, float)):
            indicator_by_month[key] = float(value)

    last_by_month: dict[str, tuple[str, float]] = {}
    for p in price_points:
        key = month_key(getattr(p, "period_date", None))
        value = getattr(p, "value", None)
        if key and isinstance(value, (int, float)):
            day = str(getattr(p, "period_date", ""))
            if key not in last_by_month or day > last_by_month[key][0]:
                last_by_month[key] = (day, float(value))

    months = sorted(set(indicator_by_month) & set(last_by_month))
    return [
        Bar(period=m, price=last_by_month[m][1],
            indicators={indicator_name: indicator_by_month[m]})
        for m in months
    ]
