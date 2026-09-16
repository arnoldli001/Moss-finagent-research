"""CME FedWatch美联储利率概率连接器（cme-fedwatch开源库，免费免Key）。

数据来源全部官方：结算价CME Group、EFFR来自FRED、FOMC日程来自美联储。
CME免费结算数据仅保留最近约5个交易日，依赖外网连通；本机/内网不可达时
fetch返回空列表（记录warning），由分析层按"概率数据缺口"诚实声明，
并退回美联储当前目标区间（AkShare us_fed_rate指标）做方向性判断。

指标约定：
- "fed:rate_prob:next"        → 下一次FOMC各利率区间概率（每个区间一个DataPoint，
                                value=概率%，extra含区间标签/会议日/EFFR/降息-不变-加息概率）
- "fed:rate_prob:{YYYY-MM-DD}" → 指定会议日
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_FED_RE = re.compile(r"^fed:rate_prob:(next|\d{4}-\d{2}-\d{2})$")
_TIMEOUT_SEC = 25


class FedWatchConnector(BaseConnector):
    """CME FedWatch FOMC利率路径概率；网络不可达时优雅降级为空结果。"""

    source_name = "CME FedWatch(cme-fedwatch开源库)"
    source_url = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["fed:rate_prob:next", "fed:rate_prob:{YYYY-MM-DD}"],
            "notes": "概率单位%；依赖CME/FRED外网，不可达时返回空（调用方需声明缺口）",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(_FED_RE.match(indicator))

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        m = _FED_RE.match(indicator)
        if not m:
            raise DataFetchError(f"FedWatch连接器不支持的指标: {indicator}")
        meeting_arg = m.group(1)
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(self._call_lib, meeting_arg),
                timeout=_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 网络/超时统一降级为空结果
            logger.warning("CME FedWatch不可达（降级为数据缺口）: %s", exc)
            return []
        return self._to_points(indicator, data)

    @staticmethod
    def _call_lib(meeting_arg: str) -> dict[str, Any]:
        from cme_fedwatch import get_probabilities

        return get_probabilities(meeting_arg)

    @staticmethod
    def _to_points(indicator: str, data: dict[str, Any]) -> list[DataPoint]:
        meetings = data.get("meetings") or []
        if not meetings:
            return []
        meeting = meetings[0]
        probs: dict[str, float] = meeting.get("probabilities") or {}
        if not probs:
            return []
        current_target = data.get("current_target", "")
        move_probs = _classify_moves(probs, current_target)
        today = date.today().isoformat()
        common_extra = {
            "meeting_date": meeting.get("date"),
            "contract": meeting.get("contract"),
            "effr": data.get("effr"),
            "current_target": current_target,
            "cut_prob": move_probs["cut"],
            "hold_prob": move_probs["hold"],
            "hike_prob": move_probs["hike"],
            "dominant_range": max(probs, key=probs.get),
            "source": "CME Group结算价+FRED EFFR(cme-fedwatch)",
            "retrieved_date": today,
        }
        return [
            DataPoint(
                indicator=indicator, value=round(float(p), 1), unit="%",
                period_date=meeting.get("date"),
                extra={**common_extra, "rate_range": label},
                source_name="CME FedWatch", source_url=FedWatchConnector.source_url,
                fetch_method=FetchMethod.API_CALL, confidence=0.85,
            )
            for label, p in sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        ]


def _range_midpoint(label: str) -> float:
    """'3.50%-3.75%' → 3.625。"""
    lo_hi = label.replace("%", "").split("-")
    return (float(lo_hi[0]) + float(lo_hi[1])) / 2


def _classify_moves(probs: dict[str, float], current_target: str) -> dict[str, float]:
    """以当前目标区间中点为基准，汇总降息/不变/加息概率。"""
    cur = _range_midpoint(current_target) if current_target else None
    cut = hold = hike = 0.0
    for label, p in probs.items():
        if cur is None:
            continue
        mid = _range_midpoint(label)
        if abs(mid - cur) < 1e-9:
            hold += p
        elif mid < cur:
            cut += p
        else:
            hike += p
    return {"cut": round(cut, 1), "hold": round(hold, 1), "hike": round(hike, 1)}
