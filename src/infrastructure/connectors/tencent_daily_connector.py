"""腾讯财经日K连接器（个股前复权），日线链的又一道**独立在线兜底**。

## 为什么需要它（实测，2026-09-17）

同一天实测四个源对 300308 的日线可用性：

```
迅投QMT          可用（但一掉线就整条链见底）
本地行情CSV      DataFetchError: 本地CSV无行情数据: 300308  ← 该票根本没导出过
AkShare          东财行情接口失败（300308），回退新浪源: Connection aborted → 空
Tushare Pro      可用（0.14s）
腾讯             可用 —— 做T面板的实时链路（`intraday/sources.py`）一直走的它
```

关键点：**AkShare 的两个子源（东财 + 新浪）会同时不可用**，
而腾讯是**另一条完全独立的 HTTP 通道**。做T面板的分钟线/快照当时是正常的，
说明腾讯这条通道没被阻断 —— 它值得成为日线链上的一跳。

## 口径

接口：`https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz300308,day,,,640,qfq`

- **前复权**：`qfq` 参数，与 QMT/AkShare东财/Tushare 一致；响应里对应 `qfqday` 节点
  （未请求复权时是 `day` 节点，本连接器两个都认，优先 `qfqday`）。
- **列序坑**：腾讯的行是 `[日期, 开, 收, 高, 低, 成交量(手)]` —— **收在高之前**。
  按"开高低收"的直觉去解会把 high/low 读反，K线形态与量柱判断会整体错位。
- **单位**：成交量单位是**手**（与东财/新浪一致）；腾讯日K不返回成交额，
  `amount` 置 None（下游 `daily_bars_from_points` 会补 0），
  **不编造**成交额。
- **升序输出**：腾讯返回升序，这里仍显式排序，避免依赖上游行为。

只声明 `stock_close:{code}`：指数走 `index_close`（腾讯的指数日K是点数口径，
且项目已有 `intraday` 的指数链路），ETF 走 `etf_close`，都不在本连接器职责内。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.core import symbols
from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {
    "Referer": "https://gu.qq.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
# 一次取多少根日K：500 自然日 ≈ 340 根，做T的日线上下文最多回看 250(规则)+120(画图)
_DEFAULT_COUNT = 640
_TIMEOUT = 12.0


def to_tencent_symbol(code: str) -> str:
    """6位代码 → 腾讯符号（sz300308 / sh600036）。

    与 `intraday/sources.symbol_for` 的归属规则保持一致：
    6/9 开头沪市，其余深市；北交所腾讯日K不覆盖，明确拒绝而不是拼一个错符号。
    """
    target = str(code or "").strip()
    if not (target.isdigit() and len(target) == 6):
        raise DataFetchError(f"腾讯日K连接器需要6位数字代码：{code!r}")
    if target.startswith(("4", "8", "920")):
        raise DataFetchError(f"腾讯日K不覆盖北交所标的：{target}")
    if target.startswith(("5", "1")):
        raise DataFetchError(
            f"腾讯日K连接器只覆盖A股个股，不支持该代码（疑似ETF/基金）：{target}")
    return symbols.exchange_symbol(target)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def rows_to_points(rows: list[Any], indicator: str, *, symbol: str,
                   start_date: str | None = None,
                   end_date: str | None = None) -> list[DataPoint]:
    """腾讯日K行 → DataPoint（纯函数，便于离线单测）。

    行序：`[日期, 开, 收, 高, 低, 成交量(手), ...]` —— 收在**高之前**。
    """
    lower = (start_date or "").replace("-", "")
    upper = (end_date or "").replace("-", "")
    points: list[DataPoint] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        raw_date = str(row[0]).strip().replace("-", "")
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        if lower and raw_date < lower:
            continue
        if upper and raw_date > upper:
            continue
        close = _number(row[2])
        if close is None:
            continue
        points.append(DataPoint(
            indicator=indicator,
            value=close,
            period_date=f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            extra={
                "open": _number(row[1]),
                "close": close,
                "high": _number(row[3]),
                "low": _number(row[4]),
                "volume": _number(row[5]),   # 手
                # 腾讯日K不返回成交额：置 None，绝不编造
                "amount": None,
                "adjust": "qfq",
                "symbol": symbol,
            },
            source_name="腾讯财经日K",
            source_url=_FQKLINE_URL,
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.8,
            verified=False,
        ))
    points.sort(key=lambda item: item.period_date or "")
    return points


class TencentDailyConnector(BaseConnector):
    """腾讯财经个股日K（前复权）。"""

    source_name = "腾讯财经日K"
    source_url = _FQKLINE_URL

    def __init__(self, client: httpx.AsyncClient | None = None,
                 *, count: int = _DEFAULT_COUNT) -> None:
        self._client = client
        self._count = max(2, int(count))

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith("stock_close:")

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["stock_close:{code}"],
            "priority": "P1",
            "notes": ("个股日K前复权（腾讯 fqkline，独立于东财/新浪通道）；"
                      "只返回成交量（手），不返回成交额；北交所不覆盖"),
        }

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if not self.supports(indicator):
            raise DataFetchError(f"腾讯日K连接器不支持的指标: {indicator}")
        code = indicator.split(":", 1)[1].strip()
        symbol = to_tencent_symbol(code)
        url = f"{_FQKLINE_URL}?param={symbol},day,,,{self._count},qfq"
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 网络/解析异常统一转 DataFetchError
            raise DataFetchError(f"腾讯日K请求失败({symbol}): {brief(exc, BRIEF_DEFAULT)}") from exc
        finally:
            if own_client:
                await client.aclose()

        node = (payload.get("data") or {}).get(symbol) or {}
        rows = node.get("qfqday") or node.get("day") or []
        if not rows and node:
            # 有些标的（上市不足/停牌）只在 `data` 下给个空 dict；如实报缺口
            logger.info("腾讯日K返回空节点(%s)：%s", symbol, list(node)[:6])
        points = rows_to_points(rows, indicator, symbol=symbol,
                                start_date=start_date, end_date=end_date)
        if not points:
            raise DataFetchError(
                f"腾讯日K在 {start_date or '不限'}~{end_date or '不限'} 无 {symbol} 数据")
        return points
