"""迅投QMT(xtquant)本地行情连接器。

indicator约定：
- "stock_close:{code}" → A股个股日频前复权收盘价（code为6位数字，如601088）
- "index_close:{code}" → 指数日频收盘价（如000300沪深300、399006创业板指）
- "etf_close:{code}"   → ETF基金日频前复权收盘价（如510300）

数据源为本地QMT极简模式（XtMiniQmt，xtdata服务127.0.0.1:58610），
全量历史由 scripts/download_qmt_data.py 预下载；本连接器在线程池中
阻塞调用xtdata（与akshare同为阻塞库）。本地无数据时自动触发单只补下载。
xtquant为可选依赖（pyproject data extra），未安装时fetch抛DataFetchError，
由ConnectorRouter回退到本地CSV/AkShare。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# 三类日频行情指标前缀（QMT/CSV/AkShare连接器共用）
QUOTE_PREFIXES = ("stock_close:", "index_close:", "etf_close:")


def to_qmt_code(code: str) -> str:
    """6位A股个股代码 → QMT代码（6/9开头沪市，其余深市）。

    归属规则与AkshareConnector._sina_symbol保持一致：600/601/603/605/688/689/900
    为沪市(.SH)，000/001/002/003/300/301等为深市(.SZ)。
    指数请使用index_close:前缀（000xxx沪/399xxx深，避免与000001平安银行重名）。
    北交所(8/4/920开头)当前不支持。
    """
    code = code.strip()
    if not (code.isdigit() and len(code) == 6):
        raise DataFetchError(f"QMT连接器仅支持6位数字代码: {code}")
    if code.startswith(("8", "4", "920")):
        raise DataFetchError(f"北交所标的暂不支持: {code}")
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def quote_qmt_code(indicator: str) -> str:
    """行情指标（stock/index/etf_close:code）→ QMT带交易所后缀代码。"""
    prefix, code = indicator.split(":", 1)
    code = code.strip()
    if not (code.isdigit() and len(code) == 6):
        raise DataFetchError(f"行情代码应为6位数字: {indicator}")
    if prefix == "stock_close":
        return to_qmt_code(code)
    if prefix == "index_close":
        if code.startswith("000") or code.startswith("880"):
            return f"{code}.SH"
        if code.startswith("399"):
            return f"{code}.SZ"
        raise DataFetchError(f"暂支持000xxx(沪)/399xxx(深)指数: {code}")
    if prefix == "etf_close":
        if code.startswith(("51", "58")):
            return f"{code}.SH"
        if code.startswith(("15", "16")):
            return f"{code}.SZ"
        raise DataFetchError(f"ETF代码段不支持: {code}")
    raise DataFetchError(f"未知行情指标前缀: {indicator}")


def normalize_date(value: str | None, *, end: bool = False) -> str:
    """'2026-09-01'/'20260901' → QMT要求的YYYYMMDD；None→空串（全历史/至今）。"""
    if not value:
        return ""
    digits = value.replace("-", "").replace("/", "")
    if len(digits) == 6 and not end:  # '202609' 月度 → 月初
        digits = digits + "01"
    return digits


def frames_to_points(df: Any, indicator: str) -> list[DataPoint]:
    """QMT日线DataFrame(time毫秒,open/high/low/close/volume/amount) → DataPoint。"""
    if df is None or len(df) == 0:
        return []
    points: list[DataPoint] = []
    for row in df.itertuples(index=False):
        mapping = row._asdict() if hasattr(row, "_asdict") else None
        if mapping is None:  # itertuples命名元组兜底
            mapping = dict(zip(df.columns, row, strict=True))
        ts_ms = mapping.get("time")
        if ts_ms is None:
            continue
        period = datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d")
        close = mapping.get("close")
        points.append(
            DataPoint(
                indicator=indicator,
                value=float(close) if close is not None else None,
                period_date=period,
                extra={
                    "open": _num(mapping.get("open")),
                    "high": _num(mapping.get("high")),
                    "low": _num(mapping.get("low")),
                    "volume": _num(mapping.get("volume")),
                    "amount": _num(mapping.get("amount")),
                    "adjust": "none" if indicator.startswith(
                        "index_close:") else "qfq",
                },
                source_name=XtQuantConnector.source_name,
                source_url=XtQuantConnector.source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.9,
                verified=False,
            )
        )
    return points


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class XtQuantConnector(BaseConnector):
    """QMT本地终端日线（前复权），无数据时单只补下载。"""

    source_name = "迅投QMT"
    source_url = "qmt://127.0.0.1:58610"

    def __init__(self) -> None:
        self._xtdata = None

    def _client(self) -> Any:
        """懒加载xtquant（未安装给出明确错误，触发路由回退）。"""
        if self._xtdata is not None:
            return self._xtdata
        try:
            from xtquant import xtdata  # type: ignore
        except ImportError as exc:
            raise DataFetchError(
                "xtquant未安装，请执行: uv sync --extra data（或uv pip install xtquant）"
            ) from exc
        xtdata.enable_hello = False
        self._xtdata = xtdata
        return xtdata

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "local_terminal": True,
            "indicators": [
                "stock_close:{code}", "index_close:{code}", "etf_close:{code}"],
            "notes": "需QMT极简模式(XtMiniQmt)运行登录；个股/ETF前复权日线，指数不除权",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(QUOTE_PREFIXES)

    def _load_frames(self, indicator: str, start_date: str | None,
                     end_date: str | None) -> Any:
        from src.core.qmt_guard import (
            download_history_isolated,
            qmt_lock,
            subscribe_once,
        )

        xtdata = self._client()
        qmt_code = quote_qmt_code(indicator)
        start = normalize_date(start_date)
        end = normalize_date(end_date, end=True)
        dividend = "front" if indicator.startswith(
            ("stock_close:", "etf_close:")) else "none"
        # 先订阅：QMT 不会自动把**当天**写进本地库，未订阅时读到的最后一根日线
        # 可能是两天前（实测 2026-09-16 盘中：600036 最新 09-14、588170 最新 09-15），
        # 而"本地库非空"又让下面的补下载分支不触发 —— 于是整个日线链静默停在过去。
        # 订阅在 1 秒内把历史+当天一起推下来（实测末根变为 20260916，即当天形成中bar）。
        try:
            subscribe_once(qmt_code, "1d", client=xtdata)
        except Exception as exc:  # noqa: BLE001 订阅失败不影响既有读取路径
            logger.warning("QMT日线订阅失败(%s): %s", qmt_code, str(exc)[:120])
        try:
            # 所有 xtquant 调用都在进程级锁内：并发访问 QMT（多线程 + 补下载）
            # 曾让服务进程无 traceback 猝死，详见 src/core/qmt_guard.py
            with qmt_lock():
                data = xtdata.get_market_data_ex(
                    [], [qmt_code], period="1d", start_time=start, end_time=end,
                    dividend_type=dividend, fill_data=False,
                )
        except Exception as exc:  # noqa: BLE001 QMT服务未启动等
            raise DataFetchError(f"QMT行情读取失败({qmt_code}): {exc}") from exc
        df = data.get(qmt_code) if isinstance(data, dict) else None
        if df is None or len(df) == 0:
            # 本地未预下载 → 触发单只增量下载后重读。
            # 下载放在**子进程**里：xtquant 下载是原生实现，崩溃会带走整个解释器，
            # 隔离后最多这一次补下载失败（上层路由会回退到别的数据源）。
            ok, detail = download_history_isolated(
                qmt_code, "1d", start_time=start, end_time=end)
            if not ok:
                raise DataFetchError(
                    f"QMT数据补下载失败({qmt_code})，请确认XtMiniQmt已登录: {detail}")
            try:
                with qmt_lock():
                    data = xtdata.get_market_data_ex(
                        [], [qmt_code], period="1d", start_time=start,
                        end_time=end, dividend_type=dividend, fill_data=False,
                    )
                df = data.get(qmt_code) if isinstance(data, dict) else None
            except Exception as exc:  # noqa: BLE001
                raise DataFetchError(
                    f"QMT数据补下载后重读失败({qmt_code}): {exc}") from exc
        if df is None or len(df) == 0:
            raise DataFetchError(f"QMT无行情数据: {qmt_code}（已下载仍为空）")
        return df

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        df = await asyncio.to_thread(
            self._load_frames, indicator, start_date, end_date)
        return frames_to_points(df, indicator)
