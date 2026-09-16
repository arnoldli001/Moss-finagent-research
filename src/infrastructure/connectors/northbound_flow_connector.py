"""北向资金连接器（AKShare东方财富免费源）。

重要数据缺口：自2024年8月19日起，沪深交易所停止披露北向资金实时/日度
成交净买额（改为按季度披露持股）。AKShare hist接口中该日期后
"当日成交净买额"列为NaN。连接器诚实处理：

- "mkt:north_flow"      → 最后可得的日度净买额（亿元），period_date为停披前
                          最后日期，extra.status="disclosure_halted"并注明日期
- "mkt:north_flow:hist" → 最近30个**有真实净买额**的交易日序列（止于停披日）

分析Agent不得将停披解读为"无外资流动"，应表述为"实时净流向不可得"。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_NORTH_RE = re.compile(r"^mkt:north_flow(?::hist)?$")
_HALT_NOTE = (
    "沪深交易所自2024-08-19起停止披露北向资金日度成交净买额（改为季度持股披露），"
    "当前无实时净流入数据；本值为停披前最后可得交易日数据"
)


def _f(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    import math
    return None if math.isnan(v) else v


class NorthboundFlowConnector(BaseConnector):
    """北向资金日度净买额：AKShare东财历史接口，停披期诚实标注数据缺口。"""

    source_name = "AKShare东方财富(沪深港通)"
    source_url = "https://data.eastmoney.com/hsgt"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["mkt:north_flow", "mkt:north_flow:hist"],
            "notes": "单位亿元；2024-08-19起日度净买额停止披露，仅返回停披前数据",
            "data_gap": "northbound_daily_net_flow_halted_since_2024-08-19",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(_NORTH_RE.match(indicator))

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        import asyncio

        try:
            rows = await asyncio.to_thread(self._load_rows)
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"北向资金历史数据获取失败: {exc}") from exc
        if not rows:
            return []
        if indicator == "mkt:north_flow:hist":
            return self._build_hist(rows)
        return self._build_latest(rows)

    @staticmethod
    def _load_rows() -> list[dict[str, Any]]:
        """按期别升序的完整历史，只保留可解析字段。"""
        import akshare as ak

        df = ak.stock_hsgt_hist_em(symbol="北向资金")
        rows: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            rows.append({
                "date": str(r["日期"])[:10],
                "net_buy": _f(r.get("当日成交净买额")),
                "buy": _f(r.get("买入成交额")),
                "sell": _f(r.get("卖出成交额")),
            })
        return sorted(rows, key=lambda x: x["date"])

    def _build_latest(self, rows: list[dict[str, Any]]) -> list[DataPoint]:
        """最新可得净买额；若已停披，取停披前最后真值并标注缺口。"""
        valid = [r for r in rows if r["net_buy"] is not None]
        if not valid:
            return []
        last_valid = valid[-1]
        halted = last_valid["date"] < rows[-1]["date"]
        extra: dict[str, Any] = {
            "buy_yi": last_valid["buy"], "sell_yi": last_valid["sell"],
            "source": "AKShare:stock_hsgt_hist_em",
        }
        confidence = 0.85
        if halted:
            first_gap = next(
                r["date"] for r in rows if r["net_buy"] is None
                and r["date"] > last_valid["date"]
            )
            extra.update({
                "status": "disclosure_halted",
                "halted_since": first_gap,
                "latest_calendar_date": rows[-1]["date"],
                "note": _HALT_NOTE,
            })
            confidence = 0.5
        else:
            extra["status"] = "normal"
        return [DataPoint(
            indicator="mkt:north_flow",
            value=round(last_valid["net_buy"], 2), unit="亿元",
            period_date=last_valid["date"], extra=extra,
            source_name=self.source_name, source_url=self.source_url,
            fetch_method=FetchMethod.WEB_CRAWL, confidence=confidence,
        )]

    def _build_hist(self, rows: list[dict[str, Any]]) -> list[DataPoint]:
        """最近30个有真实净买额的交易日（升序），供均值/趋势判断。"""
        valid = [r for r in rows if r["net_buy"] is not None][-30:]
        if not valid:
            return []
        halted = valid[-1]["date"] < rows[-1]["date"]
        return [DataPoint(
            indicator="mkt:north_flow:hist",
            value=round(r["net_buy"], 2), unit="亿元",
            period_date=r["date"],
            extra={"buy_yi": r["buy"], "sell_yi": r["sell"],
                   "source": "AKShare:stock_hsgt_hist_em",
                   **({"status": "disclosure_halted",
                       "halted_since": "2024-08-19"} if halted else {})},
            source_name=self.source_name, source_url=self.source_url,
            fetch_method=FetchMethod.WEB_CRAWL,
            confidence=0.5 if halted else 0.85,
        ) for r in valid]
