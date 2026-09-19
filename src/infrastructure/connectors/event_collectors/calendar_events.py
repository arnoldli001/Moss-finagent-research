"""投资日历采集器：宏观数据公布(百度财经日历)+停复牌/分红/财报预约(个股日历)。

- 宏观日历：当日+次日；中国事件全保留，海外事件仅保留重要性≥3。
- 个股日程：未来7天窗口内的停复牌/除权除息/财报发布。
任一接口失败降级为空+错误条目，不影响其余日程源。
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.infrastructure.connectors.event_collectors.base import (
    iter_rows,
    make_raw_item,
)

logger = logging.getLogger(__name__)

_STOCK_WINDOW_DAYS = 7
_OVERSEAS_MIN_IMPORTANCE = 3


def _clean(value: Any) -> str:
    """pandas NaN/None → 空串。"""
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except TypeError:
        pass
    text = str(value).strip()
    return "" if text in ("nan", "NaT", "--", "None") else text


def _within_window(value: str, days: int) -> bool:
    try:
        target = datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return date.today() <= target <= date.today() + timedelta(days=days)


def _map_macro_df(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        m = row._asdict()
        region = _clean(m.get("地区"))
        event_name = _clean(m.get("事件"))
        day = _clean(m.get("日期"))
        if not event_name or not day:
            continue
        try:
            importance = int(float(_clean(m.get("重要性")) or "0"))
        except ValueError:
            importance = 0
        if region != "中国" and importance < _OVERSEAS_MIN_IMPORTANCE:
            continue
        actual, expect, prev_ = (_clean(m.get("公布")), _clean(m.get("预期")),
                                 _clean(m.get("前值")))
        title = f"{region}将公布：{event_name}"
        content = (f"重要性{importance}星；公布值={actual or '待公布'}，"
                   f"预期={expect or '无'}，前值={prev_ or '无'}")
        items.append(make_raw_item(
            title=title, content=content,
            source_name="百度财经-经济数据日历",
            publish_time=f"{day} {_clean(m.get('时间'))}".strip(),
            type_hint="calendar",
            extra={"source_tag": "baidu_calendar", "importance": importance,
                   "regions": [region] if region else []},
        ))
    return items


def _map_suspend_df(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        m = row._asdict()
        code, name = _clean(m.get("股票代码")), _clean(m.get("股票简称"))
        day = _clean(m.get("停牌时间"))
        if not code or not _within_window(day, _STOCK_WINDOW_DAYS):
            continue
        reason = _clean(m.get("停牌事项说明"))
        resume = _clean(m.get("复牌时间"))
        title = f"{name}({code})停牌：{reason or '事项待披露'}"
        content = f"停牌起始日{day}；预计复牌日={resume or '未公布'}"
        items.append(make_raw_item(
            title=title, content=content, source_name="百度财经-停复牌日程",
            publish_time=f"{day} 09:30", type_hint="stock",
            extra={"source_tag": "trade_suspend", "companies": [name]},
        ))
    return items


def _map_dividend_df(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        m = row._asdict()
        code, name = _clean(m.get("股票代码")), _clean(m.get("股票简称"))
        day = _clean(m.get("除权日"))
        if not code or not _within_window(day, _STOCK_WINDOW_DAYS):
            continue
        plan = "；".join(p for p in (
            f"每10股分红{_clean(m.get('分红'))}" if _clean(m.get("分红")) not in ("", "-") else "",
            f"送股{_clean(m.get('送股'))}" if _clean(m.get("送股")) not in ("", "-") else "",
            f"转增{_clean(m.get('转增'))}" if _clean(m.get("转增")) not in ("", "-") else "",
        ) if p)
        title = f"{name}({code})即将除权除息"
        content = f"除权日{day}；分配方案：{plan or '详见公告'}（报告期{_clean(m.get('报告期'))}）"
        items.append(make_raw_item(
            title=title, content=content, source_name="百度财经-分红派息日程",
            publish_time=f"{day} 09:30", type_hint="stock",
            extra={"source_tag": "dividend", "companies": [name]},
        ))
    return items


def _map_report_df(df: Any) -> list[dict]:
    items: list[dict] = []
    for row in iter_rows(df):
        m = row._asdict()
        code, name = _clean(m.get("股票代码")), _clean(m.get("股票简称"))
        day = _clean(m.get("发布日期"))
        if not name or not _within_window(day, _STOCK_WINDOW_DAYS):
            continue
        kind = _clean(m.get("财报类型"))
        title = f"{name}({code or '指数/海外'})将于{day}发布财报"
        items.append(make_raw_item(
            title=title, content=f"财报安排：{kind or '定期报告'}",
            source_name="百度财经-财报披露日程", publish_time=f"{day} 16:00",
            type_hint="stock",
            extra={"source_tag": "report_time", "companies": [name]},
        ))
    return items


class CalendarCollector:
    """投资日历（宏观+个股日程），逐日/逐接口失败降级。"""

    source_name = "investment_calendar"

    def __init__(self, akshare_module: Any | None = None) -> None:
        self._ak = akshare_module

    def _load_ak(self) -> Any:
        if self._ak is not None:
            return self._ak
        import akshare as ak

        self._ak = ak
        return ak

    async def _collect_macro(self) -> tuple[list[dict], list[str]]:
        ak = self._load_ak()
        items, errors = [], []
        for offset in (0, 1):
            day = (date.today() + timedelta(days=offset)).strftime("%Y%m%d")
            try:
                df = await asyncio.to_thread(ak.news_economic_baidu, date=day)
                items.extend(_map_macro_df(df))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"宏观日历 {day} 拉取失败: {brief(exc, BRIEF_TIGHT)}")
        return items, errors

    async def _collect_stock_calendar(self) -> tuple[list[dict], list[str]]:
        ak = self._load_ak()
        specs = (
            ("news_trade_notify_suspend_baidu", _map_suspend_df, {}),
            ("news_trade_notify_dividend_baidu", _map_dividend_df, {}),
        )
        items, errors = [], []
        for fn_name, mapper, kwargs in specs:
            try:
                df = await asyncio.to_thread(getattr(ak, fn_name), **kwargs)
                items.extend(mapper(df))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"个股日程 {fn_name} 拉取失败: {brief(exc, BRIEF_TIGHT)}")
        # 财报披露按当日查询，扫今天+7天内安排（逐天失败静默计一次）
        try:
            offsets = range(0, _STOCK_WINDOW_DAYS + 1)
            for offset in offsets:
                day = (date.today() + timedelta(days=offset)).strftime("%Y%m%d")
                try:
                    df = await asyncio.to_thread(ak.news_report_time_baidu, date=day)
                    items.extend(_map_report_df(df))
                except Exception:  # noqa: BLE001 逐天接口波动不记噪声
                    continue
        except Exception as exc:  # noqa: BLE001 防御 ak 模块本身异常
            errors.append(f"财报日程拉取失败: {brief(exc, BRIEF_TIGHT)}")
        return items, errors

    async def collect(self) -> tuple[list[dict], list[str]]:
        items: list[dict] = []
        errors: list[str] = []
        for coro in (self._collect_macro(), self._collect_stock_calendar()):
            try:
                part, errs = await coro
            except Exception as exc:  # noqa: BLE001
                logger.warning("投资日历分组采集异常: %s", brief(exc, BRIEF_TIGHT))
                errors.append(f"投资日历分组异常: {brief(exc, BRIEF_TIGHT)}")
                continue
            items.extend(part)
            errors.extend(errs)
        return items, errors
