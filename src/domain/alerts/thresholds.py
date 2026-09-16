"""阈值告警引擎（FR-6纯决策，无IO）。

规则（阈值全部来自Settings，可调参）：
1. confidence < alert_confidence_min 直接不告警（宁漏勿滥）；
2. 风险优先：risk_score 命中任一风险档位即判风险（即使机会分也高）；
3. 其次机会：opportunity_score 命中机会档位判机会；
4. 均未命中→不告警。
生成的Alert带alert_key（event_key:type，供24h冷却）与7天有效期。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from src.core.config import Settings, get_settings
from src.domain.alerts.dedup import alert_content_key, alert_dedup_key
from src.domain.alerts.models import (
    Alert,
    AlertLevel,
    AlertType,
    Event,
    EventAssessment,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


_LEVEL_RANK = {
    AlertLevel.LOW.value: 0,
    AlertLevel.MEDIUM.value: 1,
    AlertLevel.HIGH.value: 2,
}


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class AlertEngine:
    """评估单事件 → 0或1条告警。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    def evaluate(
        self, event: Event, assessment: EventAssessment,
        now: datetime | None = None,
    ) -> Alert | None:
        if assessment.confidence < self._s.alert_confidence_min:
            return None
        alert_type, level = self._decide(
            assessment.risk_score, assessment.opportunity_score)
        if alert_type is None or level is None:
            return None
        current = now or _now()
        industries = assessment.affected_industries or event.entities.industries
        dedup_key = alert_dedup_key(event.event_key, alert_type.value)
        return Alert(
            alert_id=f"al_{hashlib.sha1(dedup_key.encode()).hexdigest()[:12]}",
            alert_key=dedup_key,
            content_key=alert_content_key(
                event.content_key, alert_type.value),
            event_id=event.event_id,
            alert_type=alert_type,
            alert_level=level,
            title=event.title,
            description=self._describe(event, assessment, alert_type),
            risk_score=round(float(assessment.risk_score), 1),
            opportunity_score=round(float(assessment.opportunity_score), 1),
            confidence=assessment.confidence,
            affected_stocks=assessment.affected_stocks,
            affected_industries=industries,
            impact_path=assessment.impact_path,
            source_name=event.source_name,
            source_url=event.source_url,
            event_publish_time=event.publish_time,
            trigger_time=_iso(current),
            expire_time=_iso(current + timedelta(days=self._s.alert_expire_days)),
            tenant_id=event.tenant_id,
        )

    def _decide(
        self, risk_score: float, opportunity_score: float,
    ) -> tuple[AlertType | None, AlertLevel | None]:
        risk_level = self._risk_level(risk_score)
        opp_level = self._opp_level(opportunity_score)
        # 风险优先：同分时以风险定级
        if risk_level is not None:
            alert_type, level = AlertType.RISK, risk_level
        elif opp_level is not None:
            alert_type, level = AlertType.OPPORTUNITY, opp_level
        else:
            return None, None
        # 站内最低级别门槛（默认medium；low档仅入库不告警）
        min_rank = _LEVEL_RANK.get(self._s.alert_min_level, 1)
        if _LEVEL_RANK[level] < min_rank:
            return None, None
        return alert_type, level

    def _risk_level(self, score: float) -> AlertLevel | None:
        s = self._s
        if score >= s.alert_risk_high:
            return AlertLevel.HIGH
        if score >= s.alert_risk_medium:
            return AlertLevel.MEDIUM
        if score >= s.alert_risk_low:
            return AlertLevel.LOW
        return None

    def _opp_level(self, score: float) -> AlertLevel | None:
        s = self._s
        if score >= s.alert_opp_high:
            return AlertLevel.HIGH
        if score >= s.alert_opp_medium:
            return AlertLevel.MEDIUM
        if score >= s.alert_opp_low:
            return AlertLevel.LOW
        return None

    @staticmethod
    def _describe(
        event: Event, a: EventAssessment, alert_type: AlertType,
    ) -> str:
        if a.summary:
            return a.summary[:200]
        direction = "风险" if alert_type == AlertType.RISK else "机会"
        return f"{direction}信号（风险分{a.risk_score:.0f}/机会分" \
               f"{a.opportunity_score:.0f}）：{event.title}"[:200]
