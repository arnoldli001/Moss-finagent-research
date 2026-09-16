"""两融余额连接器（AKShare交易所免费源）。

- 沪市：ak.stock_margin_sse(start_date, end_date) 区间一次返回（单位：元）
- 深市：ak.stock_margin_szse(date=YYYYMMDD) 单日返回（单位：亿元）

交易所T+1披露，非交易日返回最近交易日。深市历史需逐日调用，
:hist指标通过线程池并发取最近10个交易日，单日失败降级为沪市口径并在extra披露。

指标约定（mkt:前缀）：
- "mkt:margin_balance"      → 两市融资融券余额合计（亿元），extra含沪深分项
- "mkt:margin_net_buy"     → 最新交易日两市融资买入额（亿元，杠杆活跃度）
- "mkt:margin_balance:hist" → 最近10个交易日两融余额序列（亿元，趋势判断用）
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_MARGIN_RE = re.compile(r"^mkt:margin_(balance|net_buy)(?::hist)?$")
_HIST_DAYS = 10


class MarginTradingConnector(BaseConnector):
    """两市融资融券余额：沪市SSE区间接口 + 深市SZSE单日接口。"""

    source_name = "AKShare交易所两融"
    source_url = "https://www.sse.com.cn"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.OFFICIAL.value,
            "indicators": [
                "mkt:margin_balance", "mkt:margin_net_buy",
                "mkt:margin_balance:hist",
            ],
            "notes": "融资融券余额/融资买入额，单位亿元；交易所T+1披露",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(_MARGIN_RE.match(indicator))

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        m = _MARGIN_RE.match(indicator)
        if not m:
            raise DataFetchError(f"两融连接器不支持的指标: {indicator}")
        end = date.today()
        start = end - timedelta(days=_HIST_DAYS * 3)  # 留足周末/节假日余量
        try:
            sh_rows = await asyncio.to_thread(
                self._sse_rows,
                start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
            )
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"沪市两融数据获取失败: {exc}") from exc
        if not sh_rows:
            return []
        if indicator == "mkt:margin_balance:hist":
            return await self._hist_points(sh_rows)
        # 最新值：沪市最新一行 + 同日深市
        latest = sh_rows[0]
        sz = await self._szse_one(latest["date"])
        return self._latest_points(indicator, latest, sz)

    # ---------- 组装 ----------

    def _latest_points(
        self, indicator: str, latest: dict[str, Any], sz: dict[str, Any] | None
    ) -> list[DataPoint]:
        sh_balance = latest["rzrq_yi"]
        sz_balance = sz["rzrq_yi"] if sz else None
        total = round(sh_balance + (sz_balance or 0), 2)
        sh_buy = latest["rzye_yi"]  # 融资买入额（亿元）
        sz_buy = sz["rzye_yi"] if sz else None
        net_buy = round(sh_buy + (sz_buy or 0), 2)
        common_extra = {
            "as_of": latest["date"],
            "sh_yi": round(sh_balance, 2),
            "sz_yi": round(sz_balance, 2) if sz_balance is not None else None,
            "coverage": "沪深两市" if sz else "仅沪市（深市暂缺）",
            "source": "AKShare:SSE/SZSE",
        }
        if indicator == "mkt:margin_net_buy":
            return [DataPoint(
                indicator=indicator, value=net_buy, unit="亿元",
                period_date=latest["date"],
                extra={**common_extra,
                       "sh_buy_yi": round(sh_buy, 2),
                       "sz_buy_yi": round(sz_buy, 2) if sz_buy is not None else None},
                source_name=self.source_name, source_url=self.source_url,
                fetch_method=FetchMethod.WEB_CRAWL, confidence=0.9 if sz else 0.7,
            )]
        return [DataPoint(
            indicator=indicator, value=total, unit="亿元",
            period_date=latest["date"], extra=common_extra,
            source_name=self.source_name, source_url=self.source_url,
            fetch_method=FetchMethod.WEB_CRAWL, confidence=0.9 if sz else 0.7,
        )]

    async def _hist_points(
        self, sh_rows: list[dict[str, Any]]
    ) -> list[DataPoint]:
        """最近_HIST_DAYS个交易日：并发拉深市，逐日失败降级沪市口径。"""
        recent = sh_rows[:_HIST_DAYS]
        sz_results = await asyncio.gather(*[
            self._szse_one(row["date"]) for row in recent
        ], return_exceptions=False)
        points: list[DataPoint] = []
        for row, sz in zip(recent, sz_results, strict=True):
            sh_b = row["rzrq_yi"]
            sz_b = sz["rzrq_yi"] if sz else None
            value = round(sh_b + (sz_b or 0), 2)
            points.append(DataPoint(
                indicator="mkt:margin_balance:hist", value=value, unit="亿元",
                period_date=row["date"],
                extra={"sh_yi": round(sh_b, 2),
                       "sz_yi": round(sz_b, 2) if sz_b is not None else None,
                       "coverage": "沪深两市" if sz else "仅沪市（深市暂缺）",
                       "source": "AKShare:SSE/SZSE"},
                source_name=self.source_name, source_url=self.source_url,
                fetch_method=FetchMethod.WEB_CRAWL,
                confidence=0.9 if sz else 0.7,
            ))
        return points

    # ---------- AKShare封装（线程内执行） ----------

    @staticmethod
    def _sse_rows(start: str, end: str) -> list[dict[str, Any]]:
        """沪市区间两融，按日期降序；元→亿元。"""
        import akshare as ak

        df = ak.stock_margin_sse(start_date=start, end_date=end)
        rows: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            try:
                d = str(r["信用交易日期"])
                rows.append({
                    "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                    "rzrq_yi": float(r["融资融券余额"]) / 1e8,
                    "rzye_yi": float(r["融资买入额"]) / 1e8,
                })
            except (TypeError, ValueError, KeyError):
                continue
        rows.sort(key=lambda x: x["date"], reverse=True)
        return rows

    async def _szse_one(self, iso_date: str) -> dict[str, Any] | None:
        """深市单日两融（亿元）；失败返回None由调用方降级。"""
        yyyymmdd = iso_date.replace("-", "")
        try:
            df = await asyncio.to_thread(self._szse_call, yyyymmdd)
        except Exception as exc:  # noqa: BLE001 深市单日缺失不阻断
            logger.debug("深市两融%s缺失: %s", iso_date, exc)
            return None
        if df is None or df.empty:
            return None
        row = df.iloc[0]
        try:
            return {
                "date": iso_date,
                "rzrq_yi": float(row["融资融券余额"]),
                "rzye_yi": float(row["融资买入额"]),
            }
        except (TypeError, ValueError, KeyError):
            return None

    @staticmethod
    def _szse_call(yyyymmdd: str):
        import akshare as ak

        return ak.stock_margin_szse(date=yyyymmdd)
