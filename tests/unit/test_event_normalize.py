"""事件标准化纯函数测试（T2/T3）。"""

from __future__ import annotations

from src.domain.alerts.dedup import dedupe_events
from src.domain.alerts.models import EventType
from src.domain.alerts.normalize import (
    classify_event,
    extract_date,
    make_content_key,
    make_event_key,
    normalize_event,
    normalize_events,
    safe_source_url,
)


def test_extract_date_tolerant_formats():
    assert extract_date("2026-09-14 17:30:00") == "2026-09-14"
    assert extract_date("2026/9/1") == "2026-09-01"
    assert extract_date("2026年9月1日") == "2026-09-01"
    assert extract_date("无日期") == ""


def test_event_key_deterministic_and_normalized():
    k1 = make_event_key("东方财富-全球快讯", "【利好】固态电池政策出台", "2026-09-14")
    k2 = make_event_key("东方财富-全球快讯", "利好固态电池政策出台！", "2026-09-14")
    assert k1 == k2 and len(k1) == 16
    k3 = make_event_key("财联社-电报", "利好固态电池政策出台", "2026-09-14")
    assert k3 != k1  # 来源不同→不同事件（保留各自溯源）


def test_classify_priority_and_hints():
    assert classify_event("工信部发布固态电池政策", "", "") == EventType.POLICY
    assert classify_event("半导体板块异动", "芯片股拉升", "") == EventType.SECTOR
    assert classify_event("中际旭创(300308)业绩快报", "", "") == EventType.STOCK
    assert classify_event("中国将公布CPI", "", "calendar") == EventType.CALENDAR


def test_normalize_basic_fields_and_sina_title():
    event = normalize_event({
        "title": "国务院发布新能源产业规划",
        "content": "推动固态电池等方向发展",
        "source_name": "东方财富-全球快讯", "source_url": "https://x/1",
        "publish_time": "2026-09-14 08:00:00",
    }, fetch_time="2026-09-14T17:30:00+08:00")
    assert event is not None
    assert event.event_id == f"evt_{event.event_key}"
    assert event.publish_time == "2026-09-14 08:00:00"
    assert event.source_url == "https://x/1"
    assert event.tenant_id == "tenant_001"
    assert event.event_type == EventType.POLICY

    # 新浪无标题：从正文【】提要提取
    sina = normalize_event({
        "title": "", "content": "【ETF盛宴中的机构】中国ETF市场火热。",
        "source_name": "新浪-7x24快讯", "publish_time": "2026-09-14 06:27:31",
    })
    assert sina is not None and sina.title == "ETF盛宴中的机构"


def test_normalize_bad_time_does_not_raise():
    event = normalize_event({
        "title": "光模块板块持续走强", "content": "CPO概念活跃",
        "source_name": "test", "publish_time": "刚刚",
    })
    assert event is not None and event.event_key  # 日期缺失仍可生成键


def test_normalize_drops_empty_item():
    assert normalize_event({"title": "", "content": "", "source_name": "x"}) is None


def test_normalize_events_batch_and_dedupe():
    items = [
        {"title": "固态电池指导意见发布", "content": "锂电材料受益",
         "source_name": "s1", "publish_time": "2026-09-14 08:00:00"},
        {"title": "固态电池指导意见发布！", "content": "锂电材料受益",
         "source_name": "s1", "publish_time": "2026-09-14 09:00:00"},
        {"title": "", "content": "", "source_name": "s1"},
    ]
    events = dedupe_events(normalize_events(items))
    assert len(events) == 1


def test_content_key_cross_source_stable():
    """D2/G3：异源转载同稿，event_key不同（各自溯源），content_key一致。"""
    e1 = normalize_event({
        "title": "固态电池指导意见发布", "content": "x",
        "source_name": "财联社-电报", "publish_time": "2026-09-14 08:00:00"})
    e2 = normalize_event({
        "title": "【固态电池指导意见发布！】", "content": "y",
        "source_name": "新浪-7x24快讯", "publish_time": "2026-09-14 09:00:00"})
    assert e1 is not None and e2 is not None
    assert e1.event_key != e2.event_key
    assert e1.content_key == e2.content_key
    assert e1.content_key == make_content_key(
        "固态电池指导意见发布", "2026-09-14")


def test_safe_source_url_scheme_whitelist():
    """D12：仅放行http/https，拦截javascript:/data:等注入面。"""
    assert safe_source_url("https://x.com/a") == "https://x.com/a"
    assert safe_source_url("http://x.com") == "http://x.com"
    assert safe_source_url("javascript:alert(1)") == ""
    assert safe_source_url("data:text/html,x") == ""
    assert safe_source_url("") == ""
    event = normalize_event({
        "title": "t", "content": "c", "source_name": "s",
        "source_url": "javascript:alert(document.cookie)"})
    assert event is not None and event.source_url == ""
