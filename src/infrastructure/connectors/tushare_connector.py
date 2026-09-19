"""Tushare Pro 日线连接器（个股前复权），作为日线链的**最后一道在线兜底**。

## 为什么需要它（实测，2026-09-17）

日线链原本是 QMT → 本地QMT导出CSV → AkShare。当天实测三个源的状态：

```
迅投QMT         可用，最新 2026-09-16
本地行情CSV      DataFetchError: 本地CSV无行情数据: 300308   ← 该票根本没导出过
AkShare         东财行情接口失败（300308），回退新浪源: Connection aborted → 返回空
```

也就是说 **QMT 一掉线，这条链对 300308 就是空的**：本地CSV没有这只票的文件，
AkShare 的东财主源被阻断、新浪回退也断连。而 Tushare 同一时刻
**0.14 秒**返回 12 根、最新 2026-09-16、close=907.80（与 QMT 完全一致）。

所以它接在链尾：平时不参与（前面的源够新就不打网络），
QMT 不可用 + CSV/AkShare 也拿不到时才顶上。

## 口径（必须与其他源一致，否则图上会出现"换源跳空"）

- **前复权**：走 `ts.pro_bar(adj='qfq')`，与 QMT / AkShare 东财源一致；
  若账号没有 `adj_factor` 权限或 `pro_bar` 不可用，**降级为不复权**并在
  `extra["adjust"]="none"` 里如实标注（不静默换口径）。
- **单位**：`vol`（手）直接作为 `volume`；`amount`（千元）**×1000 转成元** ——
  与 AkShare 东财/新浪源的口径对齐（成交量按手、成交额按元）。
- **排序**：Tushare 返回**按日期倒序**，这里统一翻成升序 ——
  下游的 K 线形态/量柱/缠论全部假定时间升序，倒序会算出完全错误的结构。

## 与其他连接器的分工

只声明 `stock_close:{code}`：指数走 `index_close`（Tushare 是 `index_daily`、
点数口径不同）、ETF 走 `etf_close`（`fund_daily`），这两类本项目的 AkShare/QMT
已覆盖，且**口径差异需要单独验证**，不做不验证的扩展。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# 只处理个股日线；指数/ETF 不在本连接器职责内（见模块 docstring）
_SUPPORTED_PREFIXES: tuple[str, ...] = ("stock_close:",)

# A股代码 → Tushare ts_code 的市场后缀
_SH_SUFFIXES = ("6", "9")           # 沪市主板/科创板(688)/B股
_SZ_SUFFIXES = ("0", "2", "3")      # 深市主板/创业板/B股
_BJ_SUFFIXES = ("4", "8")           # 北交所（920xxx 单列判断）


def to_ts_code(code: str) -> str:
    """6位代码 → Tushare ts_code（如 300308 → 300308.SZ）。

    本连接器只声明了个股日线，但被显式调用时仍要给出**明确**的拒绝，
    而不是拼一个错误的 ts_code 去换回一个空结果（空结果与"该股停牌"无法区分）。
    """
    target = str(code or "").strip()
    if not (target.isdigit() and len(target) == 6):
        raise DataFetchError(f"Tushare连接器需要6位数字代码：{code!r}")
    if target.startswith("920"):
        return f"{target}.BJ"
    if target.startswith(_SH_SUFFIXES):
        return f"{target}.SH"
    if target.startswith(_SZ_SUFFIXES):
        return f"{target}.SZ"
    if target.startswith(_BJ_SUFFIXES):
        return f"{target}.BJ"
    # 5xxxxx/1xxxxx 是 ETF/基金，走 etf_close 而非 stock_close
    raise DataFetchError(
        f"Tushare连接器只覆盖A股个股日线，不支持该代码（疑似ETF/基金）：{target}")


def _compact(value: str | None) -> str:
    return (value or "").replace("-", "").replace("/", "")


def frame_to_points(frame: pd.DataFrame | None, indicator: str, *,
                    ts_code: str, adjust: str,
                    start_date: str | None = None,
                    end_date: str | None = None) -> list[DataPoint]:
    """Tushare 日线 DataFrame → DataPoint（纯函数，便于离线单测）。

    **升序输出**：Tushare 按 trade_date 倒序返回，下游全部假定升序。
    """
    if frame is None or len(frame) == 0:
        return []
    lower = _compact(start_date)
    upper = _compact(end_date)
    points: list[DataPoint] = []
    for _, row in frame.iterrows():
        raw_date = str(row.get("trade_date", "")).strip()
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        if lower and raw_date < lower:
            continue
        if upper and raw_date > upper:
            continue
        close = _float(row.get("close"))
        if close is None:
            continue
        amount = _float(row.get("amount"))
        points.append(DataPoint(
            indicator=indicator,
            value=close,
            period_date=f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            extra={
                "open": _float(row.get("open")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "close": close,
                "pre_close": _float(row.get("pre_close")),
                "pct_chg": _float(row.get("pct_chg")),
                # vol 单位=手（与东财/新浪一致）；amount 原始单位=千元 → 转元
                "volume": _float(row.get("vol")),
                "amount": None if amount is None else amount * 1000.0,
                "adjust": adjust,
                "ts_code": ts_code,
            },
            source_name="Tushare Pro",
            source_url="https://tushare.pro",
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.85,
            verified=False,
        ))
    points.sort(key=lambda item: item.period_date or "")
    return points


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 过滤 NaN


class TushareConnector(BaseConnector):
    """Tushare Pro 个股日线（前复权优先，降级不复权并如实标注）。"""

    source_name = "Tushare Pro"
    source_url = "https://tushare.pro"

    def __init__(self, client: Any = None) -> None:
        # 客户端惰性构造：没有 token 时**构造连接器本身不能失败**，
        # 否则整个采集链装配就断了（Tushare 只是兜底，不该拖垮主链路）。
        self._client = client
        self._client_error: str | None = None

    # ---- 能力声明 ----

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(_SUPPORTED_PREFIXES)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["stock_close:{code}"],
            "priority": "P1",
            "notes": ("个股日线前复权（ts.pro_bar adj=qfq）；"
                      "无 adj_factor 权限时降级不复权并在 extra.adjust 标注；"
                      "需 TUSHARE_TOKEN（环境变量/.env/Windows注册表）"),
        }

    # ---- 取数 ----

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_error is not None:
            raise DataFetchError(self._client_error)
        try:
            from src.quant.tushare_source import TushareClient, resolve_token

            self._client = TushareClient(resolve_token())
        except Exception as exc:  # noqa: BLE001 token 缺失/权限问题都按"该源不可用"
            self._client_error = f"Tushare 不可用：{brief(exc, BRIEF_DEFAULT)}"
            raise DataFetchError(self._client_error) from exc
        return self._client

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if not self.supports(indicator):
            raise DataFetchError(f"Tushare连接器不支持的指标: {indicator}")
        code = indicator.split(":", 1)[1].strip()
        ts_code = to_ts_code(code)
        # Tushare SDK 是同步阻塞的 → 丢线程池，别把事件循环堵住
        return await asyncio.to_thread(
            self._fetch_sync, indicator, ts_code, start_date, end_date)

    def _fetch_sync(
        self, indicator: str, ts_code: str,
        start_date: str | None, end_date: str | None,
    ) -> list[DataPoint]:
        client = self._ensure_client()
        start = _compact(start_date) or "19900101"
        end = _compact(end_date) or "20991231"

        frame, adjust = self._daily_frame(client, ts_code, start, end)
        points = frame_to_points(
            frame, indicator, ts_code=ts_code, adjust=adjust,
            start_date=start_date, end_date=end_date)
        if not points:
            raise DataFetchError(
                f"Tushare 在 {start}~{end} 无 {ts_code} 日线数据"
                "（区间外/已退市/接口无权限）")
        return points

    @staticmethod
    def _daily_frame(client: Any, ts_code: str, start: str,
                     end: str) -> tuple[pd.DataFrame | None, str]:
        """优先前复权；权限不足或 pro_bar 不可用时降级为不复权。"""
        try:
            import tushare as ts

            frame = ts.pro_bar(ts_code=ts_code, adj="qfq", start_date=start,
                               end_date=end, api=client.pro)
            if frame is not None and len(frame):
                return frame, "qfq"
            logger.info("Tushare pro_bar(qfq) 返回空(%s)，尝试不复权 daily", ts_code)
        except Exception as exc:  # noqa: BLE001 降级不复权，但要留下痕迹
            logger.warning("Tushare 前复权失败(%s)，降级不复权：%s",
                           ts_code, brief(exc, BRIEF_TIGHT))
        frame = client.call(api="daily", ts_code=ts_code,
                            start_date=start, end_date=end)
        return frame, "none"
