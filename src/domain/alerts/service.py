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
from collections.abc import Callable
from datetime import datetime

from src.core.config import Settings, get_settings
from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.domain.alerts.analyzer import EventAnalyzer
from src.domain.alerts.dedup import (
    dedupe_events,
    event_recency_key,
    is_in_cooldown,
)
from src.domain.alerts.models import DEFAULT_TENANT, Alert, ScanResult
from src.domain.alerts.normalize import normalize_events, now_iso
from src.domain.alerts.repository import EventRepository
from src.domain.alerts.thresholds import AlertEngine

logger = logging.getLogger(__name__)


class ScanInProgressError(RuntimeError):
    """已有扫描在执行（定时/手动三入口共享同一把锁，NFR-7）。"""


def _event_time_key(event) -> tuple[datetime, int]:
    """候选截断排序键；规则与理由见 `dedup.event_recency_key`（单一实现）。

    ⚠️ 曾经的教训（2026-09-24）：这里只按 `publish_time` 倒序，于是"未来日程"
    （财报披露日程把 publish_time 写成 09-28/09-30）永远压过当天快讯，
    候选窗口被占满 → `fact_alerts` 恒为 0。修法见 `event_recency_key`。
    """
    return event_recency_key(event)


def in_trading_window(now: datetime | None = None) -> bool:
    """现在是不是"该弹窗"的时段。

    判据用 `trading_session` 的常量（与全站同一套，改一处全站一致），
    但**口径比连续竞价更宽**：集合竞价与盘后 5 分钟也算。

    为什么放宽：
      · **集合竞价（09:15~09:25）**是隔夜消息集中定价的窗口，
        这段时间的弹窗价值最高（用户还有时间准备）；
      · **盘后 15:00~15:05** 是收盘复盘窗口，刚出的公告还来得及看。

    而午休、盘前深夜、周末一律不弹 —— 那时弹了也无法动作，
    只会变成半夜的打扰。`lunch_break`（11:30~13:00）不弹是刻意的：
    只有 1.5 小时，用户多半在吃饭，开盘后会再看列表。

    ⚠️ 注意 `session_state()` **没有** `post_close` 这个状态
    （15:00 之后直接回 `closed`），所以盘后窗口必须自己按分钟数判，
    不能指望那个函数 —— 第一版就是这么写的，结果是"盘后 5 分钟永远不弹"。
    """
    from src.core.trading_session import (
        CALL_AUCTION_START, MORNING_OPEN, POST_CLOSE, minutes_of,
        session_state,
    )

    moment = now or datetime.now()
    state, _label = session_state(moment)
    if state == "trading":
        return True
    if state not in ("call_auction", "closed"):
        return False                      # 周末 / 盘前 / 午休
    if moment.weekday() >= 5:
        return False
    minutes = minutes_of(moment)
    if CALL_AUCTION_START <= minutes < MORNING_OPEN:
        return True                       # 集合竞价
    return MORNING_OPEN <= minutes <= POST_CLOSE  # 盘后复盘窗口


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
        popup_gate: Callable[[datetime | None], bool] | None = None,
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
        #: 弹窗时段闸门。**可注入**是为了可测 —— 直接调 `in_trading_window()`
        #: 会让"是否推送"取决于跑测试时的墙上时间：白天全绿、晚上全红，
        #: 而失败信息是 `assert 0 == 1`（看不出与时间有关）。
        #: 生产用默认值；测试注入恒定返回值。
        self._popup_gate = popup_gate or in_trading_window

    @property
    def is_running(self) -> bool:
        """供API入口在派发前快速判重（权威判重仍由run内的锁保证）。"""
        return self._lock.locked()

    async def run(self, trigger: str = "manual", *,
                  force: bool = False) -> ScanResult:
        """跑一轮扫描。

        `force=True`（手动"强制重扫"）：**绕过 LLM 缓存重判**，不再复用上次
        对同一批事件的评估结论。定时作业不传（默认复用缓存，省钱）。
        """
        if self._lock.locked():
            raise ScanInProgressError("上一次事件扫描仍在执行中")
        async with self._lock:
            return await self._run(trigger, force=force)

    async def _run(self, trigger: str, *, force: bool = False) -> ScanResult:
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
            await self._analyze_and_alert(candidates, result, force=force)

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
        """新采集事件 + 库里未评估的存量事件（手工导入/上轮超上限积压）。

        **无论是否超上限都排序**：候选顺序决定"谁抢到这一轮的 LLM 预算"，
        不能只在超限时才用 `_event_time_key`（未来日程挤掉当天快讯的事故里，
        "没超限所以不排序"正是漏网的那一段 —— 见 `_event_time_key` 的说明）。
        """
        limit = self._s.alert_scan_candidate_limit
        pending = list(new_events)
        seen = {e.event_id for e in pending}
        for event in await self._repo.list_unanalyzed_events(
                limit=limit, tenant_id=self._tenant):
            if event.event_id not in seen:
                seen.add(event.event_id)
                pending.append(event)
        ordered = sorted(pending, key=_event_time_key, reverse=True)
        if len(ordered) <= limit:
            return ordered
        result.data_gaps.append(
            f"待分析事件{len(ordered)}条超过LLM候选上限{limit}，"
            f"仅分析最新{limit}条，其余已入库，下轮继续")
        return ordered[:limit]

    async def _analyze_and_alert(
        self, new_events: list, result: ScanResult, *, force: bool = False,
    ) -> None:
        assessments = await self._analyzer.analyze(new_events, force=force)
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

    async def ingest_assessed(
        self, pairs: list[tuple[object, object]], *,
        trigger: str = "intel_signal", now: datetime | None = None,
    ) -> ScanResult:
        """把**已经评估好**的事件变成告警 —— 不再调 LLM。

        ## 为什么需要这条入口

        情报流里的条目在采集阶段就已经拿到了倾向（`tone.py`：规则层 +
        本地模型交叉验证），并带着可信度分数。再让告警的分析器**重新**
        用云端模型判一次，会出现两个问题：

          · 同一件事有两套方向判断（情报流说"偏多"、告警说"风险"），
            用户看到互相矛盾的结论 —— 这是最伤信任的一种错；
          · 白花一次云端 token。

        所以这条入口直接吃调用方给的 `EventAssessment`，
        只复用告警系统真正不可替代的那部分：**去重、冷却、等级判定、
        每用户已读、WebSocket 推送、邮件**。

        `pairs` 是 `(Event, EventAssessment)` 列表。
        """
        result = ScanResult(trigger=trigger, started_at=now_iso())
        result.scanned = len(pairs)
        result.data_gaps = list(self._channel_gaps())
        if not pairs:
            result.status = "success"
            result.finished_at = now_iso()
            return result

        async with self._lock:
            events = [e for e, _ in pairs]
            new_events = await self._persist_new(events, result)
            # 只对**本轮新入库**的事件出告警：已经在库里的事件说明上一轮
            # 处理过，重复出告警要靠冷却拦，而冷却窗口过期后又会重新弹 ——
            # 那不是"新消息"，是噪音。
            fresh_ids = {getattr(e, "event_id", "") for e in new_events}
            by_id = {getattr(e, "event_id", ""): (e, a) for e, a in pairs}
            for eid in fresh_ids:
                event, assessment = by_id.get(eid, (None, None))
                if event is None or assessment is None:
                    continue
                alert = self._engine.evaluate(event, assessment, now=now)
                if alert is None:
                    continue
                if not await self._passes_cooldown(alert):
                    continue
                saved = await self._repo.upsert_alerts([alert])
                if saved["inserted"] == 0:
                    continue
                await self._dispatch(alert, result, now=now)
            result.status = self._final_status(result)
            result.finished_at = now_iso()
        return result

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

    async def _dispatch(self, alert: Alert, result: ScanResult, *,
                        now: datetime | None = None) -> None:
        result.alerts_created += 1
        result.by_level[alert.alert_level.value] = (
            result.by_level.get(alert.alert_level.value, 0) + 1)
        result.by_type[alert.alert_type.value] = (
            result.by_type.get(alert.alert_type.value, 0) + 1)
        # ── 弹窗闸门：非交易时段**只入库不弹**（用户口径 2026-09-25）──
        #
        # 用户选择的是："明确方向 + 信度达标 + 仅 high 级；跨源同文合并成一条；
        # **非交易时段只进列表不弹**。"
        #
        # 为什么闸门放在**推送**而不是"不入库"：告警本身是事实记录，
        # 用户第二天早上应该能在列表里看到昨晚发生的事。
        # 半夜弹窗则是纯打扰 —— 而且 A 股在非交易时段本来就无法动作，
        # 弹了也只能干看着，第二天开盘前那点信息优势早就被隔夜消化了。
        #
        # ⚠️ 邮件**跟着一起静音**：它与弹窗是同一个"现在提醒你"的语义。
        # 不进列表才是丢信息，不弹只是调整时机。
        quiet = not self._popup_gate(now)
        if quiet:
            result.data_gaps.append(
                "非交易时段：本次告警只入库不弹窗（次个交易日开盘前可在告警列表查看）")
        else:
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
