"""白酒价格真实数据连接器（公开源：酒排名 jiupaiming.com）。

## 先读口径：这是"零售均价代理"，不是"批价"

指标 id 仍是 `ind:白酒批价(元/瓶)`，但**免费公开源里没有可靠的"批价"序列** ——
本连接器取的是该站「酒价内参」列的**终端零售均价**（飞天茅台 53度/500ml），
只作为白酒价格的**方向/趋势代理**。

同站同一天（2026-09-26 实测）并存多个口径，差距不小：

    终端零售均价（本连接器取值） = 1811
    26年飞天原箱批价 = 1740 / 1745 / 1750   ← 同日文章内三个不同值
    26年飞天散瓶批价 = 1730 / 1740 / 1745
    i茅台申购价 = 1639 ；自营店执行价 = 1766

即"零售均价 − 批价"约 **+60~80 元/瓶**。**不要把本序列当批价水平值使用**：
它系统性偏高，且该偏差量级正好落在判断"批价上行/下行"的阈值附近。

仍然可用作因子输入的理由：行业层用它算的是**环比方向**
（`IndustryAgentBase._trend_signal` 取最近两期做差投票），恒定水平偏差在做差时
抵消。但**水平值会进入 LLM 上下文**，所以口径披露写在 `source_name` 里 ——
`_format_with_fresh` 只展示 indicator/期间/值/confidence/source_name，
**不展示 extra**，把"非批价"只放进 extra 等于没披露。

## 源与覆盖（2026-09-26 实测）

- 页面: https://www.jiupaiming.com/price/feitianmaotai-53-du
- 结构: `<table class="jpm-history-table">`，价格在 `class="jpm-ph-price"`，
  口径在 `class="jpm-ph-src"`
- **硬上限 10 行、无翻页**（`?page/?paged/?range` 与 admin-ajax 均无效），
  故**只能前向积累、无法回补历史**；深度靠每日快照累加。
- 只取 `jpm-ph-src == "酒价内参"` 的行：同日另有 `综合` 口径
  （2026-09-21 两条 1812/1803、09-18 一条 1808），混进同一序列会造出凭空的跳变。
- 每日约 09:30–11:00 北京时间更新。

## 可靠性约束（宁缺口不造假）

取值范围断言 1200 < 值 < 3000、有效行数 ≥ 5、最新一期不早于今天-5 天；
任一不满足即视为本次取数失败 → 退本地快照（标 `storage_fallback`），
无快照则抛 `DataFetchError`，**绝不生成替代值**。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any

import requests

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.real_industry_connector import (
    _latest_snapshot,
    _write_snapshot,
)

logger = logging.getLogger(__name__)

INDICATOR = "ind:白酒批价(元/瓶)"
_URL = "https://www.jiupaiming.com/price/feitianmaotai-53-du"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_TIMEOUT_SEC = 25
_SNAPSHOT_SOURCE = "liquor_feitian_retail"

#: 本站历史表只保留最近 10 行（含表头行），无翻页可用
_TABLE_RE = re.compile(r'(?is)<table class="jpm-history-table".*?</table>')
_ROW_RE = re.compile(r"(?is)<tr.*?</tr>")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_PRICE_RE = re.compile(r'jpm-ph-price[^>]*>\s*[¥￥]?([\d,]+)')
_SRC_RE = re.compile(r"jpm-ph-src[^>]*>\s*([^<]+)")

#: 只认这个口径；"综合"是另一种聚合，混用会造出凭空的跳变
_PRICE_BASIS = "酒价内参"
_MIN_VALUE, _MAX_VALUE = 1200.0, 3000.0
_MIN_ROWS = 5
_MAX_STALE_DAYS = 5

#: 口径披露落在这里 —— 见模块 docstring：extra 不进 LLM 上下文
_SOURCE_NAME = "酒价内参(终端零售均价·非酒商批价)"


def parse_feitian_rows(html: str) -> list[tuple[str, float]]:
    """从历史表解析 [(period_date, 终端零售均价)]，按期升序。

    只保留 `jpm-ph-src == "酒价内参"` 的行；同时按日期去重（同日重复取后者）。
    """
    table = _TABLE_RE.search(html)
    if not table:
        raise DataFetchError("酒排名页面结构变化：未找到 jpm-history-table")
    by_date: dict[str, float] = {}
    for row in _ROW_RE.findall(table.group(0)):
        date_m = _DATE_RE.search(row)
        price_m = _PRICE_RE.search(row)
        src_m = _SRC_RE.search(row)
        if not (date_m and price_m and src_m):
            continue
        if src_m.group(1).strip() != _PRICE_BASIS:
            continue
        try:
            value = float(price_m.group(1).replace(",", ""))
        except ValueError:
            continue
        by_date[date_m.group(1)] = value
    return sorted(by_date.items())


def _validate(rows: list[tuple[str, float]]) -> None:
    """取值范围/条数/新鲜度断言，不满足即抛（由上层转快照兜底）。"""
    if len(rows) < _MIN_ROWS:
        raise DataFetchError(
            f"酒排名有效行数不足({len(rows)} < {_MIN_ROWS})，疑似页面结构变化")
    for period, value in rows:
        if not (_MIN_VALUE < value < _MAX_VALUE):
            raise DataFetchError(
                f"酒排名价格越界({period}={value})，疑似抓错口径"
                f"（如把 i茅台申购价 1639 / 自营店价 1766 当成了零售均价）")
    newest = date.fromisoformat(rows[-1][0])
    stale_days = (date.today() - newest).days
    if stale_days > _MAX_STALE_DAYS:
        raise DataFetchError(
            f"酒排名最新一期过旧({rows[-1][0]}，距今{stale_days}天)")


class LiquorPriceConnector(BaseConnector):
    """飞天茅台上游价格（酒排名「酒价内参」终端零售均价，日频，最多10期）。"""

    source_name = _SOURCE_NAME
    source_url = _URL

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "simulated": False,
            "indicators": [INDICATOR],
            "notes": (
                "酒排名「酒价内参」终端零售均价（飞天茅台53度/500ml），日频、"
                "站点仅留最近10期；口径为零售均价而**非酒商批价**，"
                "比批价系统性高约60~80元/瓶，只建议作方向代理"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator == INDICATOR

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if indicator != INDICATOR:
            raise DataFetchError(f"白酒价格连接器不支持的指标: {indicator}")

        storage_fallback = False
        try:
            rows = await asyncio.to_thread(self._fetch_rows)
            _validate(rows)
        except Exception as exc:  # noqa: BLE001 页面变化/越界/过旧/网络失败
            snap = _latest_snapshot(_SNAPSHOT_SOURCE)
            snap_rows = (snap or {}).get("records") or []
            if not snap_rows:
                raise DataFetchError(
                    f"白酒价格获取失败且无本地快照: {exc}") from exc
            logger.warning("酒排名在线取数失败，使用本地快照: %s", exc)
            rows = [(str(r["period"]), float(r["value"])) for r in snap_rows]
            storage_fallback = True
        else:
            _write_snapshot(_SNAPSHOT_SOURCE, {
                "source_url": _URL,
                "price_basis": _PRICE_BASIS,
                "fetch_time": datetime.now().isoformat(),
                "records": [{"period": p, "value": v} for p, v in rows],
            })

        return self._to_points(rows, indicator, storage_fallback,
                               start_date, end_date)

    @staticmethod
    def _fetch_rows() -> list[tuple[str, float]]:
        resp = requests.get(_URL, timeout=_TIMEOUT_SEC,
                            headers={"User-Agent": _UA})
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return parse_feitian_rows(resp.text)

    def _to_points(
        self,
        rows: list[tuple[str, float]],
        indicator: str,
        storage_fallback: bool,
        start_date: str | None,
        end_date: str | None,
    ) -> list[DataPoint]:
        points: list[DataPoint] = []
        for period, value in rows:
            if start_date and period < start_date:
                continue
            if end_date and period > end_date:
                continue
            extra: dict[str, Any] = {
                "simulated": False,
                "frequency": "daily",
                # 口径披露（结构化字段）；面向 LLM 的披露在 source_name 上
                "price_basis": "terminal_retail_avg",
                "is_wholesale_price": False,
                "proxy": "终端零售均价，作为白酒价格方向代理",
                "proxy_note": (
                    "非酒商批价；同日同站批价约低60~80元/瓶，"
                    "水平值不可当批价使用，方向可比"
                ),
                "raw_indicator": indicator[len("ind:"):],
            }
            if storage_fallback:
                extra["storage_fallback"] = True
            points.append(DataPoint(
                indicator=indicator,
                value=value,
                unit="元/瓶",
                period_date=period,
                extra=extra,
                source_name=_SOURCE_NAME,
                source_url=_URL,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.WEB_CRAWL,
                confidence=0.8,
                verified=True,
            ))
        return points
