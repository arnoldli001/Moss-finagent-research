"""告警扫描编排服务（domain应用服务，FR-1~FR-11串联，T8）。

流水线：多源采集(失败隔离) → 标准化跨源去重 → 事件幂等入库 →
仅对新事件LLM分析(候选上限30) → 阈值引擎 → 24h冷却+唯一约束双保险 →
WebSocket广播 / 分数门槛邮件（风险>69、机会≥85，可调）→
汇总ScanResult（success/partial/failed）。
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone

from src.core.config import Settings, get_settings
from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.domain.alerts.analyzer import EventAnalyzer
from src.domain.alerts.dedup import _parse_dt, dedupe_events, is_in_cooldown
from src.domain.alerts.models import DEFAULT_TENANT, Alert, ScanResult
from src.domain.alerts.normalize import normalize_events, now_iso
from src.domain.alerts.repository import EventRepository
from src.domain.alerts.thresholds import AlertEngine

logger = logging.getLogger(__name__)


class ScanInProgressError(RuntimeError):
    """已有扫描在执行（定时/手动三入口共享同一把锁，NFR-7）。"""


def _event_time_key(event) -> datetime:
    """候选截断排序键：发布时间优先，回退抓取时间；混格式/naive统一为本地时区。"""
    dt = _parse_dt(event.publish_time) or _parse_dt(event.fetch_time)
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now(timezone.utc).astimezone().tzinfo)
    return dt


class AlertScanService:
    """无状态扫描服务（状态均在仓储；进程内asyncio.Lock串行化所有入口）。"""

    def __init__(
        self,
        collectors: list,
        repo: EventRepository,
        analyzer: EventAnalyzer,
        engine: AlertEngine,
        hub,
        emailer,
        settings: Settings | None = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        self._collectors = collectors
        self._repo = repo
        self._analyzer = analyzer
        self._engine = engine
        self._hub = hub
        self._emailer = emailer
        self._s = settings or get_settings()
        self._tenant = tenant_id
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """供API入口在派发前快速判重（权威判重仍由run内的锁保证）。"""
        return self._lock.locked()

    async def run(self, trigger: str = "manual") -> ScanResult:
        if self._lock.locked():
            raise ScanInProgressError("上一次事件扫描仍在执行中")
        async with self._lock:
            return await self._run(trigger)

    async def _run(self, trigger: str) -> ScanResult:
        result = ScanResult(trigger=trigger, started_at=now_iso())
        raw_items, errors, per_source = await self._collect_all()
        events = dedupe_events(normalize_events(raw_items, fetch_time=now_iso()))
        result.scanned = len(events)
        result.errors = errors
        result.per_source = dict(per_source)
        result.data_gaps = list(self._channel_gaps())

        new_events = await self._persist_new(events, result) if events else []
        # 即使采集器全挂（scanned=0），仍处理手工导入/上轮积压的未分析事件（AC-12）
        candidates = await self._merge_backlog(new_events, result)
        if candidates:
            await self._analyze_and_alert(candidates, result)

        result.status = self._final_status(result)
        result.finished_at = now_iso()
        logger.info(
            "事件扫描(%s)完成: %s 扫描=%d 新增=%d 告警=%d",
            trigger, result.status, result.scanned,
            result.new_events, result.alerts_created)
        return result

    async def _collect_all(self) -> tuple[list[dict], list[str], Counter]:
        raw_items: list[dict] = []
        errors: list[str] = []
        per_source: Counter = Counter()
        for collector in self._collectors:
            try:
                items, source_errors = await collector.collect()
            except Exception as exc:  # noqa: BLE001 单采集器故障隔离
                logger.warning("采集器%s失败: %s",
                               getattr(collector, "source_name", "?"),
                               brief(exc, BRIEF_DEFAULT))
                errors.append(f"{getattr(collector, 'source_name', 'collector')}"
                              f"采集异常: {brief(exc, BRIEF_TIGHT)}")
                continue
            raw_items.extend(items)
            errors.extend(source_errors)
            for item in items:
                per_source[item.get("source_name", "unknown")] += 1
        return raw_items, errors, per_source

    async def _persist_new(self, events, result: ScanResult) -> list:
        known = await self._repo.existing_event_keys(
            [e.event_key for e in events], self._tenant)
        new_events = [e for e in events if e.event_key not in known]
        saved = await self._repo.upsert_events(events)
        result.new_events = saved["inserted"]
        return new_events

    async def _merge_backlog(self, new_events: list, result: ScanResult) -> list:
        """新采集事件 + 库里未评估的存量事件（手工导入/上轮超上限积压）。"""
        limit = self._s.alert_scan_candidate_limit
        pending = list(new_events)
        seen = {e.event_id for e in pending}
        for event in await self._repo.list_unanalyzed_events(
                limit=limit, tenant_id=self._tenant):
            if event.event_id not in seen:
                seen.add(event.event_id)
                pending.append(event)
        if len(pending) <= limit:
            return pending
        ordered = sorted(pending, key=_event_time_key, reverse=True)
        result.data_gaps.append(
            f"待分析事件{len(pending)}条超过LLM候选上限{limit}，"
            f"仅分析最新{limit}条，其余已入库，下轮继续")
        return ordered[:limit]

    async def _analyze_and_alert(
        self, new_events: list, result: ScanResult,
    ) -> None:
        assessments = await self._analyzer.analyze(new_events)
        if new_events and not assessments:
            result.data_gaps.append(
                "LLM评分阶段未产出有效评估（模型不可用或输出异常），本轮无告警")
        by_id = {a.event_id: a for a in assessments}
        for event in new_events:
            assessment = by_id.get(event.event_id)
            if assessment is None:
                continue
            alert = self._engine.evaluate(event, assessment)
            if alert is None:
                continue
            if not await self._passes_cooldown(alert):
                continue
            saved = await self._repo.upsert_alerts([alert])
            if saved["inserted"] == 0:
                continue  # 唯一约束双保险：已存在alert_key
            await self._dispatch(alert, result)
        # 仅标记拿到有效评估的事件；LLM整体失败时下轮重试
        assessed_ids = [a.event_id for a in assessments]
        if assessed_ids:
            await self._repo.mark_events_analyzed(
                assessed_ids, self._tenant)

    async def _passes_cooldown(self, alert: Alert) -> bool:
        """同源alert_key冷却 + 跨源content_key抑制（G3防刷屏）。"""
        last = await self._repo.last_alert_time(alert.alert_key, self._tenant)
        if is_in_cooldown(last, hours=self._s.alert_cooldown_hours):
            logger.info("告警冷却中，跳过: %s", alert.alert_key)
            return False
        if alert.content_key:
            last_same = await self._repo.last_content_alert_time(
                alert.content_key, self._tenant)
            if is_in_cooldown(last_same, hours=self._s.alert_cooldown_hours):
                logger.info(
                    "跨源同文告警冷却中，跳过: %s", alert.content_key)
                return False
        return True

    async def _dispatch(self, alert: Alert, result: ScanResult) -> None:
        result.alerts_created += 1
        result.by_level[alert.alert_level.value] = (
            result.by_level.get(alert.alert_level.value, 0) + 1)
        result.by_type[alert.alert_type.value] = (
            result.by_type.get(alert.alert_type.value, 0) + 1)
        try:
            await self._hub.broadcast(alert, alert.tenant_id)
        except Exception as exc:  # noqa: BLE001 推送失败不影响落库/邮件
            logger.warning("WebSocket广播失败: %s", brief(exc, BRIEF_TIGHT))
        email_result = await self._emailer.send(alert)
        if email_result.status != "suppressed":
            result.email_results.append(email_result)

    def _channel_gaps(self) -> list[str]:
        gaps: list[str] = []
        is_configured = getattr(self._emailer, "is_configured", None)
        if callable(is_configured) and not is_configured():
            gaps.append("邮件通道未配置授权码（ALERT_SMTP_USER/ALERT_SMTP_AUTH_CODE），"
                        "当前仅站内告警")
        return gaps

    @staticmethod
    def _final_status(result: ScanResult) -> str:
        if result.scanned == 0 and result.errors and result.alerts_created == 0:
            return "failed"
        if result.errors:
            return "partial"
        return "success"
