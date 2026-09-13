"""AkShare数据源连接器。

indicator约定（configs/data_sources.yaml akshare节）：
- "CPI"                  → 全国居民消费价格指数（月度同比）
- "PPI"                  → 工业生产者出厂价格指数（月度同比）
- "stock_close:{code}"   → A股日频收盘价（如 stock_close:000001）

个股行情主源东财（stock_zh_a_hist），连接失败自动回退新浪（stock_zh_a_daily，
前复权）——实测东财接口偶发断连。akshare为阻塞库，fetch经asyncio.to_thread线程池化。
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_DATE_COLUMN_HINTS = ("日期", "月份", "报告日", "时间")
_VALUE_COLUMN_PRIORITY = ("同比", "收盘", "今值")


def _to_float(raw: Any) -> float | None:
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


def _pick_date_column(columns: list[str]) -> str | None:
    """按提示词匹配日期列。"""
    for hint in _DATE_COLUMN_HINTS:
        for col in columns:
            if hint in col:
                return col
    return None


def _pick_value_column(df: Any, columns: list[str], date_col: str | None) -> str | None:
    """选主数值列：业务关键词优先（同比/收盘/今值），否则首个可转float的非日期列。"""
    for keyword in _VALUE_COLUMN_PRIORITY:
        for col in columns:
            if keyword in col:
                return col
    for col in columns:
        if col == date_col:
            continue
        sample = df[col].dropna()
        if len(sample) and _to_float(sample.iloc[0]) is not None:
            return col
    return None


def df_to_data_points(
    df: Any,
    indicator: str,
    source_name: str,
    source_url: str,
) -> list[DataPoint]:
    """将DataFrame逐行转换为DataPoint（纯函数，可独立测试）。

    每行一个DataPoint：value取主数值列，period_date取日期列原始值
    （ISO标准化由A02数据清洗Agent完成），完整行存入extra。
    宏观接口无官方发布时间，publish_time置空并降低confidence。
    """
    if df is None or len(df) == 0:
        return []

    columns = [str(c) for c in df.columns]
    df.columns = columns
    date_col = _pick_date_column(columns)
    value_col = _pick_value_column(df, columns, date_col)

    points: list[DataPoint] = []
    for _, row in df.iterrows():
        row_dict = {col: row[col] for col in columns}
        value = _to_float(row[value_col]) if value_col is not None else None
        points.append(
            DataPoint(
                indicator=indicator,
                value=value,
                period_date=str(row[date_col]) if date_col is not None else None,
                extra=row_dict,
                source_name=source_name,
                source_url=source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.8,
                verified=False,
            )
        )
    return points


class AkshareConnector(BaseConnector):
    """AkShare连接器：覆盖宏观CPI/PPI与A股日频行情。"""

    source_name = "AkShare"
    source_url = "https://akshare.akfamily.xyz"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["CPI", "PPI", "stock_close:{code}"],
            "notes": " akshare未安装时fetch将抛出DataFetchError，需 uv sync --extra data",
        }

    def _load_dataframe(
        self, indicator: str, start_date: str | None, end_date: str | None
    ) -> Any:
        """同步加载原始DataFrame（在线程池中执行）。延迟导入akshare。"""
        try:
            import akshare as ak
        except ImportError as exc:
            raise DataFetchError("akshare未安装，请执行: uv sync --extra data") from exc

        if indicator == "CPI":
            return ak.macro_china_cpi_monthly()
        if indicator == "PPI":
            return ak.macro_china_ppi_yearly()
        if indicator.startswith("stock_close:"):
            code = indicator.split(":", 1)[1].strip()
            return self._stock_dataframe(ak, code, start_date, end_date)
        raise DataFetchError(f"AkShare连接器不支持的指标: {indicator}")

    @staticmethod
    def _sina_symbol(code: str) -> str:
        """A股代码→新浪带市场前缀符号：6/9开头沪市，其余按深市。"""
        return f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"

    def _stock_dataframe(
        self, ak: Any, code: str, start_date: str | None, end_date: str | None
    ) -> Any:
        """东财主源失败时回退新浪前复权；返回列名统一为中文（日期/收盘…）。"""
        try:
            return ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date or "",
                end_date=end_date or "",
            )
        except Exception as exc:  # noqa: BLE001 源故障回退（实测东财偶发RemoteDisconnected）
            logger.warning("东财行情接口失败（%s），回退新浪源: %s", code, exc)
            df = ak.stock_zh_a_daily(symbol=self._sina_symbol(code), adjust="qfq")
            df = df.rename(columns={"date": "日期", "close": "收盘"})
            if start_date or end_date:
                dates = df["日期"].astype(str).str.replace("-", "")
                if start_date:
                    df = df[dates >= start_date]
                if end_date:
                    df = df[df["日期"].astype(str).str.replace("-", "") <= end_date]
            return df

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        df = await asyncio.to_thread(self._load_dataframe, indicator, start_date, end_date)
        return df_to_data_points(df, indicator, self.source_name, self.source_url)
