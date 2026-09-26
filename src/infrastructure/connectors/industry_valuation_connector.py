"""消费/周期/医药行业估值（PE-TTM）真实数据连接器。

替换 `MockIndustryConnector` 的另外三个模拟指标 —— 与
`RealTechIndustryConnector`（A13科技）同思路：**保持 indicator id 不变**，
只把数据源从确定性合成序列换成免费公开的真实数据。

主源：中证指数官网 indicator.xls（OSS 直连，近20个交易日日频PE-TTM）
- ind:消费行业PE(TTM) → 000932 中证主要消费（中证800消费，白酒权重高）
- ind:周期行业PE(TTM) → 399998 中证煤炭
- ind:医药行业PE(TTM) → 000933 中证医药卫生（中证800医卫）

备源：申万一级行业估值（akshare `sw_index_first_info`，仅当期截面1个点）
- 消费→食品饮料(801120.SI) / 周期→煤炭(801950.SI) / 医药→医药生物(801150.SI)

口径说明（必须如实披露，避免把"行业指数PE"误读成"行业个股PE中位数"）：
三个指标用的是**中证行业指数**的滚动市盈率，即该行业指数成分股的加权整体法
PE-TTM，与"行业全部个股PE的中位数/算术平均"不是同一口径。每个 DataPoint 的
extra 都带 `industry_proxy` / `proxy_note`，报告引用时天然披露。

可靠性（与 RealTechIndustryConnector 同构）：
- 在线成功后按源落盘 data/industry_metrics/{source}/YYYY-MM-DD.json；
- 网络失败但有历史快照 → 读最近快照并在 extra 标 `storage_fallback=True`；
- 主源失败且无快照 → 退申万一级（单点）；两源皆失才抛 DataFetchError。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

import requests

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.real_industry_connector import (
    _latest_snapshot,
    _write_snapshot,
    parse_csindex_pe,
)

logger = logging.getLogger(__name__)

# 中证指数官网OSS估值文件（与 akshare_connector._CSINDEX_OSS_URL 同一路径模板）
_CSINDEX_OSS_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
    "uploads/file/autofile/indicator/{code}indicator.xls"
)
_CSINDEX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.csindex.com.cn/",
}
_CSINDEX_TIMEOUT_SEC = 12


def _snapshot_key(index_code: str) -> str:
    """快照按**指数代码**分文件。

    三个指标共用同一份中证OSS来源，但快照必须各存各的 —— 若共用一个文件名，
    后取的指数会覆盖前一个，等到某次网络失败走快照兜底时，就会把"消费"的
    历史记录当成"医药"的值返回（数值与代码同源，肉眼很难发现）。
    """
    return f"csindex_industry_pe_{index_code}"

# 指标 → (中证指数代码, 中证指数简称, 申万一级行业名, 申万行业代码)
# 周期选"煤炭"的理由：A15_cyclical 的另两个真实指标（动力煤价格、重点电厂
# 煤炭库存）同为煤炭口径，估值锚保持同一子行业才自洽；若要换成更宽的
# 周期口径（如 000929 中证800材料），改这一张表即可。
_INDUSTRY_PE: dict[str, tuple[str, str, str, str]] = {
    "ind:消费行业PE(TTM)": ("000932", "800消费", "食品饮料", "801120.SI"),
    "ind:周期行业PE(TTM)": ("399998", "中证煤炭", "煤炭", "801950.SI"),
    "ind:医药行业PE(TTM)": ("000933", "800医卫", "医药生物", "801150.SI"),
}

_PROXY_NOTE = "中证行业指数成分股加权整体法PE-TTM，非行业个股PE中位数"


class IndustryValuationConnector(BaseConnector):
    """消费/周期/医药行业PE-TTM（中证指数官网为主，申万一级行业为备）。"""

    source_name = "中证指数官网行业估值"
    source_url = "https://www.csindex.com.cn/"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "simulated": False,
            "indicators": list(_INDUSTRY_PE),
            "notes": (
                "中证行业指数近20个交易日PE-TTM（日频）；"
                "主源失败时退申万一级行业当期截面值"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator in _INDUSTRY_PE

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if indicator not in _INDUSTRY_PE:
            available = ", ".join(_INDUSTRY_PE)
            raise DataFetchError(
                f"行业估值连接器不支持的指标: {indicator}；可用: {available}"
            )
        index_code, index_name, sw_name, sw_code = _INDUSTRY_PE[indicator]

        records, storage_fallback, source_url = await self._csindex_records(
            index_code)
        if records:
            return self._to_points(
                indicator, index_code, index_name, records,
                storage_fallback=storage_fallback, source_url=source_url,
                start_date=start_date, end_date=end_date,
            )

        # 主源失败且无快照 → 申万一级行业当期截面（单点）
        sw_point = await self._sw_fallback(indicator, sw_name, sw_code)
        if sw_point is not None:
            return [sw_point]
        raise DataFetchError(
            f"{indicator} 获取失败：中证指数官网与申万一级行业两个真实源均不可用"
        )

    # ---------------- 主源：中证指数官网 ----------------

    async def _csindex_records(
        self, index_code: str,
    ) -> tuple[list[dict[str, Any]], bool, str]:
        """取中证OSS估值记录（近20个交易日，升序）。

        返回 (records, storage_fallback, source_url)；两路都拿不到时 records 为空。
        """
        url = _CSINDEX_OSS_URL.format(code=index_code)
        # 快照按指数代码分文件，避免三个行业互相覆盖后串档（见 _snapshot_key）
        snap_key = _snapshot_key(index_code)

        def _work() -> list[dict[str, Any]]:
            resp = requests.get(url, timeout=_CSINDEX_TIMEOUT_SEC,
                                headers=_CSINDEX_HEADERS)
            resp.raise_for_status()
            return parse_csindex_pe(resp.content)

        try:
            records = await asyncio.to_thread(_work)
        except Exception as exc:  # noqa: BLE001 网络/解析失败尝试本地最近快照
            snap = _latest_snapshot(snap_key)
            snap_records = (snap or {}).get("records") or []
            # 双保险：即便文件名被写错，也不把别的指数的序列当成本指标的值
            if snap_records and snap.get("index_code") not in (None, index_code):
                logger.warning("中证行业估值(%s)快照指数不匹配(%s)，弃用",
                               index_code, snap.get("index_code"))
                snap_records = []
            if snap_records:
                logger.warning("中证行业估值(%s)在线获取失败，使用本地快照: %s",
                               index_code, exc)
                return list(snap_records), True, url
            logger.warning("中证行业估值(%s)在线获取失败且无本地快照: %s",
                           index_code, exc)
            return [], False, url

        if records:
            _write_snapshot(snap_key, {
                "source_url": url,
                "index_code": index_code,
                "fetch_time": datetime.now().isoformat(),
                "records": records,
            })
        return records, False, url

    def _to_points(
        self,
        indicator: str,
        index_code: str,
        index_name: str,
        records: list[dict[str, Any]],
        *,
        storage_fallback: bool,
        source_url: str,
        start_date: str | None,
        end_date: str | None,
    ) -> list[DataPoint]:
        """中证OSS日频PE-TTM → DataPoint（按日期边界过滤）。"""
        points: list[DataPoint] = []
        for r in records:
            period = str(r["period"])
            if start_date and period < start_date:
                continue
            if end_date and period > end_date:
                continue
            pe = r.get("pe_ttm")
            if pe is None:
                continue
            extra: dict[str, Any] = {
                "simulated": False,
                "frequency": "daily",
                "metric": "PE_TTM",
                "index_code": index_code,
                "index_name": str(r.get("index_name") or index_name),
                "industry_proxy": f"中证行业指数({index_code})",
                "proxy_note": _PROXY_NOTE,
                "raw_indicator": indicator[len("ind:"):],
            }
            if storage_fallback:
                extra["storage_fallback"] = True
            points.append(DataPoint(
                indicator=indicator,
                value=float(pe),
                unit="倍",
                period_date=period,
                extra=extra,
                source_name=self.source_name,
                source_url=source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.9,
                verified=True,
            ))
        return points

    # ---------------- 备源：申万一级行业 ----------------

    async def _sw_fallback(
        self, indicator: str, sw_name: str, sw_code: str,
    ) -> DataPoint | None:
        """申万一级行业当期PE-TTM截面值（东财WAF阻断时不可用，静默返回None）。"""
        try:
            row = await asyncio.to_thread(self._sw_row, sw_name)
        except Exception as exc:  # noqa: BLE001 备源失败不掩盖主因，返回None
            logger.warning("申万一级行业(%s)兜底失败: %s", sw_name, exc)
            return None
        if row is None:
            return None
        return DataPoint(
            indicator=indicator,
            value=float(row["pe_ttm"]),
            unit="倍",
            period_date=date.today().isoformat(),
            publish_time=datetime.now().isoformat(),
            extra={
                "simulated": False,
                "frequency": "daily_snapshot",
                "metric": "PE_TTM",
                "industry_code": sw_code,
                "industry_name": sw_name,
                "industry_proxy": f"申万一级行业({sw_name})",
                "proxy_note": "申万一级行业成分股加权整体法PE-TTM；中证OSS主源本次不可用",
                "source_note": "申万一级行业估值（AKShare sw_index_first_info）",
            },
            source_name="申万一级行业估值(AKShare)",
            source_url="https://akshare.akfamily.xyz",
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.85,
            verified=True,
        )

    @staticmethod
    def _sw_row(sw_name: str) -> dict[str, Any] | None:
        """取申万一级行业指定行业的 TTM(滚动)市盈率。"""
        import akshare as ak

        df = ak.sw_index_first_info()
        if df is None or df.empty:
            return None
        name_col, pe_col = "行业名称", "TTM(滚动)市盈率"
        if name_col not in df.columns or pe_col not in df.columns:
            return None
        hit = df[df[name_col].astype(str).str.strip() == sw_name]
        if hit.empty:
            return None
        try:
            pe = float(hit.iloc[0][pe_col])
        except (TypeError, ValueError):
            return None
        if pe != pe:  # NaN
            return None
        return {"pe_ttm": pe}
