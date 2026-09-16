"""本地量化行情CSV连接器（D:\\quantTrader\\data 等QMT导出目录）。

indicator约定与AkShare/QMT连接器一致：
- "stock_close:{code}" 个股；"index_close:{code}" 指数；"etf_close:{code}" ETF
目录结构（迅投QMT数据导出格式）：
    {base_dir}/SH/price_{code}.csv
    {base_dir}/SZ/price_{code}.csv
CSV列：timetag(YYYYMMDD),open,high,low,close,volumn,amount
作为QMT服务未启动时的二级本地兜底；目录/文件缺失抛DataFetchError继续回退在线源。
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.xtquant_connector import (
    QUOTE_PREFIXES,
    quote_qmt_code,
)


def _compact(value: str | None) -> str:
    return (value or "").replace("-", "").replace("/", "")


def csv_rows_to_points(
    rows: list[dict[str, str]], indicator: str, source_url: str
) -> list[DataPoint]:
    """CSV dict行 → DataPoint（纯函数，便于单测）。"""
    points: list[DataPoint] = []
    for row in rows:
        tag = (row.get("timetag") or "").strip()
        if len(tag) != 8 or not tag.isdigit():
            continue
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        points.append(
            DataPoint(
                indicator=indicator,
                value=close,
                period_date=f"{tag[:4]}-{tag[4:6]}-{tag[6:]}",
                extra={
                    "open": _safe_float(row.get("open")),
                    "high": _safe_float(row.get("high")),
                    "low": _safe_float(row.get("low")),
                    "volume": _safe_float(row.get("volumn") or row.get("volume")),
                    "amount": _safe_float(row.get("amount")),
                    "adjust": "unknown",
                },
                source_name=LocalCsvConnector.source_name,
                source_url=source_url,
                source_type=DataSourceType.FILE,
                fetch_method=FetchMethod.FILE_READ,
                confidence=0.85,
                verified=False,
            )
        )
    return points


def _safe_float(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


class LocalCsvConnector(BaseConnector):
    """读本地QMT导出CSV日线（不复权信息，口径unknown）。"""

    source_name = "本地行情CSV"
    source_url = "file://local-quanttrader"

    def __init__(self, base_dir: str = "") -> None:
        self._base_dir = Path(base_dir) if base_dir else None

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.FILE.value,
            "local_file": True,
            "base_dir": str(self._base_dir) if self._base_dir else "",
            "indicators": [
                "stock_close:{code}", "index_close:{code}", "etf_close:{code}"],
            "notes": "迅投QMT导出CSV，个股复权口径未知；QMT服务不可用时的本地兜底",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(QUOTE_PREFIXES)

    def _csv_path(self, indicator: str) -> Path:
        if self._base_dir is None:
            raise DataFetchError("本地行情目录未配置（LOCAL_QUOTE_DIR）")
        qmt_code = quote_qmt_code(indicator)
        code, market = qmt_code.split(".")
        path = self._base_dir / market / f"price_{code}.csv"
        if not path.exists():
            raise DataFetchError(f"本地行情文件不存在: {path}")
        return path

    def _load_rows(
        self, indicator: str, start_date: str | None, end_date: str | None
    ) -> list[dict[str, str]]:
        path = self._csv_path(indicator)
        start, end = _compact(start_date), _compact(end_date)
        try:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as exc:
            raise DataFetchError(f"本地行情读取失败({path}): {exc}") from exc
        return [
            r for r in rows
            if (not start or (r.get("timetag") or "") >= start)
            and (not end or (r.get("timetag") or "") <= end)
        ]

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        rows = await asyncio.to_thread(
            self._load_rows, indicator, start_date, end_date)
        if not rows:
            code = indicator.split(":", 1)[1].strip()
            raise DataFetchError(f"本地CSV无行情数据: {code}")
        path = self._csv_path(indicator)
        return csv_rows_to_points(rows, indicator, path.as_uri())
