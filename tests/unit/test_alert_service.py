"""AlertScanService 编排测试（T8/AC-3/AC-4/AC-8/AC-12/AC-13）。"""

from __future__ import annotations

import pytest

from src.core.config import get_settings
from src.domain.alerts.models import (
    Alert,
    AlertLevel,
    EmailSendResult,
    EventAssessment,
)
from src.domain.alerts.normalize import now_iso
from src.domain.alerts.service import AlertScanService
from src.domain.alerts.thresholds import AlertEngine


def _raw(title: str, content: str = "相关内容", hint: str = "",
         publish_time: str = "2026-09-14 08:00:00") -> dict:
    return {
        "title": title, "content": content,
        "source_name": "测试快讯", "source_url": "https://x/1",
        "publish_time": publish_time, "type_hint": hint,
    }


class FakeCollector:
    source_name = "fake_collector"

    def __init__(self, items=None, errors=None, raises: Exception | None = None):
        self.items = items or []
        self.errors = errors or []
        self.raises = raises

    async def collect(self):
        if self.raises:
            raise self.raises
        return self.items, self.errors


class FakeRepo:
    def __init__(self, seeded_alert_keys=None):
        self.event_keys: set[str] = set()
        self.events: list = []
        self.unanalyzed: list = []
        self.analyzed_ids: list[str] = []
        self.alerts: dict[str, Alert] = {}
        for key in seeded_alert_keys or []:
            self.alerts[key] = object()
        self._last_times = dict.fromkeys(seeded_alert_keys or [], now_iso())

    async def ensure_schema(self):
        return None

    async def existing_event_keys(self, keys, tenant_id="tenant_001"):
        return {k for k in keys if k in self.event_keys}

    async def list_unanalyzed_events(self, limit=100, tenant_id="tenant_001"):
        return list(self.unanalyzed[:limit])

    async def mark_events_analyzed(self, event_ids, tenant_id="tenant_001"):
        self.analyzed_ids.extend(event_ids)
        marked = set(event_ids)
        self.unanalyzed = [e for e in self.unanalyzed
                           if e.event_id not in marked]
        return len(event_ids)

    async def upsert_events(self, events):
        inserted = 0
        for e in events:
            if e.event_key not in self.event_keys:
                self.event_keys.add(e.event_key)
                self.events.append(e)
                inserted += 1
        return {"inserted": inserted, "skipped": len(events) - inserted}

    async def upsert_alerts(self, alerts):
        inserted = 0
        for a in alerts:
            if a.alert_key not in self.alerts:
                self.alerts[a.alert_key] = a
                self._last_times[a.alert_key] = a.trigger_time
                inserted += 1
        return {"inserted": inserted, "skipped": len(alerts) - inserted}

    async def last_alert_time(self, alert_key, tenant_id="tenant_001"):
        return self._last_times.get(alert_key)

    async def last_content_alert_time(
        self, content_key, tenant_id="tenant_001",
    ):
        times = [a.trigger_time for a in self.alerts.values()
                 if isinstance(a, Alert) and a.content_key == content_key]
        return max(times) if times else None


class FakeAnalyzer:
    def __init__(self, high_titles=()):
        self.high_titles = set(high_titles)
        self.calls: list[int] = []

    async def analyze(self, events, force: bool = False):
        self.calls.append(len(events))
        out = []
        for e in events:
            if e.title in self.high_titles:
                out.append(EventAssessment(
                    event_id=e.event_id, event_type=e.event_type,
                    sentiment="positive", risk_score=10,
                    opportunity_score=92, confidence=0.92,
                    affected_industries=["固态电池"],
                    impact_path="政策→需求→业绩", summary="重大利好"))
            else:
                out.append(EventAssessment(
                    event_id=e.event_id, event_type=e.event_type,
                    risk_score=20, opportunity_score=30, confidence=0.8))
        return out


class FakeHub:
    def __init__(self):
        self.sent: list[Alert] = []

    async def broadcast(self, alert, tenant_id="tenant_001"):
        self.sent.append(alert)
        return 1


class FakeEmailer:
    def __init__(self, configured: bool = True):
        self.configured = configured
        self.sent: list[Alert] = []

    def is_configured(self) -> bool:
        return self.configured

    async def send(self, alert):
        if not self.configured:
            return EmailSendResult(
                alert_id=alert.alert_id, status="unconfigured",
                detail="未配置SMTP账号或授权码")
        self.sent.append(alert)
        return EmailSendResult(
            alert_id=alert.alert_id, status="sent", detail="已发送")


def _service(collectors, repo, analyzer, emailer=None, settings=None,
             popup_gate=None):
    """构造被测服务。

    `popup_gate` 默认注入"永远可以弹" —— 生产默认是
    `in_trading_window()`（非交易时段只入库不弹窗），直接用它会让
    **测试结果取决于跑测试时的墙上时间**：白天通过、晚上失败，
    而失败信息是 `assert 0 == 1`，完全看不出与时间有关。
    时段闸门本身由 `tests/unit/test_intel_alert_bridge.py` 的
    参数化用例专门验证，这里只关心"该推的时候推了没有"。
    """
    return AlertScanService(
        collectors=collectors, repo=repo, analyzer=analyzer,
        engine=AlertEngine(settings or get_settings()),
        hub=FakeHub(), emailer=emailer or FakeEmailer(),
        settings=settings or get_settings(),
        popup_gate=popup_gate or (lambda _now=None: True))


@pytest.mark.asyncio
async def test_full_run_one_high_one_low():
    high_title = "国务院发布固态电池产业重大扶持政策"
    low_title = "某行业日常动态：市场成交平稳"
    analyzer = FakeAnalyzer(high_titles={high_title})
    repo = FakeRepo()
    emailer = FakeEmailer(configured=False)
    svc = _service(
        [FakeCollector(items=[_raw(high_title), _raw(low_title, hint="sector")])],
        repo, analyzer, emailer)

    result = await svc.run("manual")

    assert result.status == "success"
    assert result.scanned == 2 and result.new_events == 2
    assert result.alerts_created == 1
    assert result.by_level == {"high": 1} and result.by_type == {"opportunity": 1}
    assert len(svc._hub.sent) == 1  # type: ignore[attr-defined]
    assert svc._hub.sent[0].alert_level == AlertLevel.HIGH  # type: ignore[attr-defined]
    assert result.email_results[0].status == "unconfigured"
    assert any("邮件通道未配置" in g for g in result.data_gaps)
    assert len(repo.alerts) == 1


@pytest.mark.asyncio
async def test_quiet_hours_store_but_do_not_popup():
    """非交易时段：告警**照常入库**，只是不弹窗（用户口径 2026-09-25）。

    为什么两者都要断言：
      · 只断言"没弹" → 实现可能连库都没写，用户第二天在列表里也看不到，
        那才是真丢信息；
      · 只断言"入库了" → 可能连弹窗一起做了闸门，等于功能没生效。
    """
    title = "国务院发布固态电池产业重大扶持政策"
    analyzer = FakeAnalyzer(high_titles={title})
    repo = FakeRepo()
    svc = _service([FakeCollector(items=[_raw(title)])], repo, analyzer,
                   popup_gate=lambda _now=None: False)

    result = await svc.run("schedule")

    assert result.alerts_created == 1, "非交易时段也必须产出告警记录"
    assert len(repo.alerts) == 1, "告警必须落库（第二天在列表里看得到）"
    assert len(svc._hub.sent) == 0, "非交易时段不该弹窗"  # type: ignore[attr-defined]
    assert any("非交易时段" in g for g in result.data_gaps), result.data_gaps


@pytest.mark.asyncio
async def test_second_run_zero_new_zero_alerts():
    title = "国务院发布固态电池产业重大扶持政策"
    analyzer = FakeAnalyzer(high_titles={title})
    svc = _service([FakeCollector(items=[_raw(title)])], FakeRepo(), analyzer)
    first = await svc.run()
    second = await svc.run("schedule")
    assert first.alerts_created == 1
    assert second.new_events == 0 and second.alerts_created == 0
    assert analyzer.calls == [1]  # 第二次无候选，零LLM调用


@pytest.mark.asyncio
async def test_partial_when_one_source_errors():
    svc = _service(
        [FakeCollector(items=[_raw("国务院发布固态电池产业重大扶持政策")],
                       errors=["备源cls连接超时"])],
        FakeRepo(), FakeAnalyzer(high_titles={"国务院发布固态电池产业重大扶持政策"}))
    result = await svc.run()
    assert result.status == "partial"
    assert result.alerts_created == 1 and result.errors


@pytest.mark.asyncio
async def test_failed_when_all_sources_down():
    svc = _service([FakeCollector(raises=ConnectionError("blocked"))],
                   FakeRepo(), FakeAnalyzer())
    result = await svc.run()
    assert result.status == "failed"
    assert result.scanned == 0 and result.errors


@pytest.mark.asyncio
async def test_cooldown_blocks_repeat_alert():
    title = "国务院发布固态电池产业重大扶持政策"
    # 先用正常跑一次拿到alert_key，再用预置该key的新repo验证冷却
    analyzer = FakeAnalyzer(high_titles={title})
    seeded = FakeRepo()
    seed_svc = _service([FakeCollector(items=[_raw(title)])], seeded, analyzer)
    await seed_svc.run()
    alert_key = next(iter(seeded.alerts))
    # 新事件表为空但alert已存在（冷却窗内）
    cool_repo = FakeRepo(seeded_alert_keys=[alert_key])
    cool_svc = _service([FakeCollector(items=[_raw(title)])], cool_repo,
                        FakeAnalyzer(high_titles={title}))
    result = await cool_svc.run()
    assert result.alerts_created == 0
    assert len(cool_svc._hub.sent) == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_future_dated_schedule_does_not_starve_fresh_news():
    """2026-09-24 事故回归：未来日程不得挤掉当天快讯（否则告警恒 0）。

    实测：库里 87 条已分析事件 **100%** 是「百度财经-财报披露日程」，
    `publish_time` 全在未来（09-28~10-01），而 184 条当天 policy/sector 快讯
    一条都没轮到 —— 候选窗口被"未来时间"占满，扫描天天 success 但 0 告警。
    """
    settings = get_settings().model_copy(update={"alert_scan_candidate_limit": 1})
    fresh = "国务院发布固态电池产业重大扶持政策"
    analyzer = FakeAnalyzer(high_titles={fresh})
    svc = _service(
        [FakeCollector(items=[
            _raw("示例公司财报披露日程", publish_time="2099-01-01 09:00:00"),
            _raw(fresh, publish_time=now_iso()),
        ])],
        FakeRepo(), analyzer, settings=settings)

    result = await svc.run()

    assert analyzer.calls == [1]        # 候选上限 1 条
    assert result.alerts_created == 1   # 放行的是当天快讯，不是 2099 年的日程


@pytest.mark.asyncio
async def test_candidate_limit_truncates_and_records_gap():
    settings = get_settings().model_copy(update={"alert_scan_candidate_limit": 1})
    t1, t2 = "国务院发布固态电池产业重大扶持政策", "工信部出台半导体设备支持方案"
    analyzer = FakeAnalyzer(high_titles={t1, t2})
    svc = _service([FakeCollector(items=[_raw(t1), _raw(t2)])], FakeRepo(),
                   analyzer, settings=settings)
    result = await svc.run()
    assert result.new_events == 2  # 全部入库
    assert analyzer.calls == [1]   # 仅1条进LLM
    assert result.alerts_created == 1
    assert any("候选上限" in g for g in result.data_gaps)


@pytest.mark.asyncio
async def test_imported_backlog_analyzed_even_when_collectors_down():
    """AC-12闭环：采集器全挂时，手工导入的未分析事件仍能分析→告警→标记。"""
    from src.domain.alerts.normalize import normalize_event

    title = "国务院发布固态电池产业重大扶持政策"
    imported = normalize_event(_raw(title), fetch_time=now_iso())
    assert imported is not None
    repo = FakeRepo()
    repo.event_keys.add(imported.event_key)  # 模拟导入接口已写库
    repo.events.append(imported)
    repo.unanalyzed.append(imported)
    analyzer = FakeAnalyzer(high_titles={title})
    svc = _service([FakeCollector(raises=ConnectionError("offline"))], repo,
                   analyzer, FakeEmailer(configured=False))

    result = await svc.run()

    assert result.scanned == 0 and result.new_events == 0
    assert result.alerts_created == 1
    assert imported.event_id in repo.analyzed_ids
    assert result.status == "partial"  # 采集失败但闭环走通，不判failed
    # 再跑一次：已标记analyzed，零LLM零告警
    result2 = await svc.run()
    assert result2.alerts_created == 0 and analyzer.calls == [1]


def _raw_from(source: str, title: str) -> dict:
    return {
        "title": title, "content": "相关内容",
        "source_name": source, "source_url": f"https://{source}/1",
        "publish_time": "2026-09-14 08:00:00",
    }


@pytest.mark.asyncio
async def test_cross_source_same_story_alerts_only_once():
    """D2/G3：财联社与新浪转载同一稿件（同标题异来源），只产生一次告警/推送。"""
    import asyncio

    title = "国务院发布固态电池产业重大扶持政策"

    class SlowCollector:
        source_name = "slow"

        async def collect(self):
            await asyncio.sleep(0)
            return [
                _raw_from("财联社-电报", title),
                _raw_from("新浪-7x24快讯", title),
            ], []

    analyzer = FakeAnalyzer(high_titles={title})
    svc = _service([SlowCollector()], FakeRepo(), analyzer,
                   FakeEmailer(configured=False))
    result = await svc.run()
    assert result.scanned == 2 and result.new_events == 2  # 两源事件各自留溯源
    assert result.alerts_created == 1  # 跨源同文只告一次
    assert len(svc._hub.sent) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_concurrent_scan_rejected_by_shared_lock():
    """D6/NFR-7：两个入口并发run，后到者抛ScanInProgressError（共享锁）。"""
    import asyncio

    from src.domain.alerts.service import ScanInProgressError

    title = "国务院发布固态电池产业重大扶持政策"

    class SlowCollector:
        source_name = "slow"

        async def collect(self):
            await asyncio.sleep(0.15)
            return [_raw(title)], []

    svc = _service([SlowCollector()], FakeRepo(),
                   FakeAnalyzer(high_titles={title}))
    assert svc.is_running is False
    first = asyncio.ensure_future(svc.run("schedule"))
    await asyncio.sleep(0.02)
    assert svc.is_running is True
    with pytest.raises(ScanInProgressError):
        await svc.run("manual")
    assert (await first).alerts_created == 1
    assert svc.is_running is False
