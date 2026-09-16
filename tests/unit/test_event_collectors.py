"""事件采集器测试：合成DataFrame + 假akshare模块，不联网（T2/AC-1）。"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta

import pandas as pd
import pytest

from src.infrastructure.connectors.event_collectors import (
    CalendarCollector,
    ManualEventCollector,
    NewsFlashCollector,
)


class _FakeAk:
    """按源返回合成DataFrame，可注入异常。"""

    def __init__(self, behaviors: dict | None = None) -> None:
        self.behaviors = behaviors or {}

    def _out(self, key: str, df: pd.DataFrame):
        beh = self.behaviors.get(key)
        if isinstance(beh, Exception):
            raise beh
        return df if beh is None else beh

    def stock_info_global_em(self):
        return self._out("em", pd.DataFrame([
            {"标题": "工信部等发布固态电池产业支持政策", "摘要": "锂电材料板块受益",
             "发布时间": "2026-09-14 09:00:00", "链接": "https://em/1"},
            {"标题": "某海外娱乐资讯", "摘要": "与市场无关",
             "发布时间": "2026-09-14 09:01:00", "链接": "https://em/2"},
        ]))

    def stock_info_global_ths(self):
        return self._out("ths", pd.DataFrame([
            {"标题": "半导体设备国产替代提速", "内容": "芯片产业链景气",
             "发布时间": "2026-09-14 10:00:00", "链接": "https://ths/1"},
        ]))

    def stock_info_global_cls(self, symbol: str = "全部"):
        return self._out("cls", pd.DataFrame([
            {"标题": "", "内容": "财联社电，央行开展逆回购操作呵护流动性。",
             "发布日期": "2026-09-14", "发布时间": "09:15:00"},
        ]))

    def stock_info_global_sina(self):
        return self._out("sina", pd.DataFrame([
            {"时间": "2026-09-14 11:00:00",
             "内容": "【创新药板块走强】医药板块午后持续拉升。"},
        ]))

    def news_economic_baidu(self, date: str):
        future_day = (date_cls.today() + timedelta(days=2)).strftime("%Y-%m-%d")
        return self._out("macro", pd.DataFrame([
            {"日期": future_day, "时间": "10:00", "地区": "中国",
             "事件": "中国月度工业增加值", "公布": "", "预期": "5.0",
             "前值": "4.8", "重要性": "1"},
            {"日期": future_day, "时间": "20:30", "地区": "美国",
             "事件": "美国零售销售", "公布": "", "预期": "0.2",
             "前值": "0.1", "重要性": "2"},
        ]))

    def news_trade_notify_suspend_baidu(self):
        future = (date_cls.today() + timedelta(days=3)).strftime("%Y-%m-%d")
        past = "2020-01-01"
        return self._out("suspend", pd.DataFrame([
            {"股票代码": "688001", "股票简称": "示例科技", "停牌时间": future,
             "复牌时间": "nan", "停牌事项说明": "拟筹划重大资产重组"},
            {"股票代码": "600000", "股票简称": "旧闻股份", "停牌时间": past,
             "复牌时间": past, "停牌事项说明": "历史停牌"},
        ]))

    def news_trade_notify_dividend_baidu(self):
        future = (date_cls.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        return self._out("dividend", pd.DataFrame([
            {"股票代码": "601398", "股票简称": "示例银行", "除权日": future,
             "分红": "1.50元", "送股": "-", "转增": "2", "报告期": "2026-06-30"},
        ]))

    def news_report_time_baidu(self, date: str):
        return self._out("report", pd.DataFrame([]))


@pytest.mark.asyncio
async def test_news_flash_multi_source_and_prefilter():
    items, errors = await NewsFlashCollector(_FakeAk()).collect()
    assert errors == []
    tags = {it["extra"]["source_tag"] for it in items}
    assert {"em", "ths", "cls", "sina"} <= tags
    # 无关娱乐新闻被关键词预筛过滤
    assert all("娱乐" not in it["title"] for it in items)
    em_hit = next(i for i in items if i["extra"]["source_tag"] == "em")
    assert em_hit["source_url"] == "https://em/1"


@pytest.mark.asyncio
async def test_news_flash_fallback_when_primary_blocked():
    fake = _FakeAk({"em": ConnectionError("Server disconnected")})
    items, errors = await NewsFlashCollector(fake).collect()
    assert items, "主源失败应自动回落备源"
    assert any("em" in e for e in errors)
    assert any(i["extra"]["source_tag"] == "ths" for i in items)


@pytest.mark.asyncio
async def test_news_flash_all_sources_failed_no_raise():
    fake = _FakeAk({
        "em": ConnectionError("x"), "ths": TimeoutError(),
        "cls": RuntimeError("y"), "sina": ConnectionError("z"),
    })
    items, errors = await NewsFlashCollector(fake).collect()
    assert items == []
    assert len(errors) >= 4  # 每源一条+汇总提示


@pytest.mark.asyncio
async def test_calendar_collector_filters_window_and_importance():
    items, errors = await CalendarCollector(_FakeAk()).collect()
    assert errors == []
    tags = {it["extra"]["source_tag"] for it in items}
    assert {"baidu_calendar", "trade_suspend", "dividend"} <= tags
    # 美国重要性2星被过滤，中国1星保留
    macro = [i for i in items if i["extra"]["source_tag"] == "baidu_calendar"]
    assert any("工业增加值" in i["title"] for i in macro)
    assert not any("零售销售" in i["title"] for i in macro)
    # 窗口外旧停牌被过滤
    assert not any("旧闻" in i["title"] for i in items)
    assert any("示例科技" in i["title"] and "停牌" in i["title"] for i in items)


@pytest.mark.asyncio
async def test_calendar_source_failure_isolated():
    fake = _FakeAk({"macro": ConnectionError("blocked"),
                    "suspend": ConnectionError("blocked")})
    items, errors = await CalendarCollector(fake).collect()
    assert any(i["extra"]["source_tag"] == "dividend" for i in items)
    assert errors  # 失败被记录但不抛出


@pytest.mark.asyncio
async def test_manual_collector_validation():
    collector = ManualEventCollector([
        {"title": "手工政策事件", "content": "正文", "event_type": "policy",
         "source_url": "https://x/1", "publish_time": "2026-09-14 08:00"},
        {"title": "", "content": "无标题应丢弃"},
        {"title": "非法类型", "event_type": "bomb"},
    ])
    items, errors = await collector.collect()
    assert errors == [] and len(items) == 2
    assert items[0]["type_hint"] == "policy"
    assert items[1]["type_hint"] == ""  # 非法hint被归一
