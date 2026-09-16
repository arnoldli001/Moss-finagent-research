"""科技行业真实产业数据连接器（免费公开数据源，替代模拟产业数据）。

覆盖指标（indicator id 与 MockIndustryConnector 保持一致，替换不改 id）：
- ind:半导体销售额同比  → WSTS Historical Billings Report（全球月度销售额，自行计算同比%）
  数据源页：https://www.wsts.org/67/Historical-Billings-Report
  Excel 含 1986 年至今按区域（美洲/欧洲/日本/亚太/全球）月度销售额（千美元）。
- ind:芯片出货量同比    → 国家统计局"集成电路产量_同比增长(%)"（akshare 新站封装，
  路径 工业 > 工业主要产品产量 > 集成电路）；NBS WAF 拦截时重试，仍失败则抛错由上层降级。
- ind:科技行业PE(TTM)   → 中证指数官网 H30184 中证全指半导体产品与设备指数 PE-TTM
  OSS indicator.xls（近20个交易日）；日频快照由16:00作业持续积累历史序列。

可靠性：
- 原始快照按日期落盘 data/industry_metrics/{wsts,nbs,csindex}/YYYY-MM-DD.json；
- 网络失败但有历史快照时读取最近快照并在 extra 标记 storage_fallback=True；
- 任何数据均带真实 source_name/source_url，confidence=0.9，verified=True，
  与"模拟产业数据(Demo)"的 0.5/unverified 形成明确区分。
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.config import get_settings
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

WSTS_LISTING_URL = "https://www.wsts.org/67/Historical-Billings-Report"
WSTS_XLS_RE = re.compile(
    r"(https://www\.wsts\.org/[^\s\"']+WSTS-Historical-Billings-Report[^\s\"']+\.xlsx)",
    re.IGNORECASE,
)
CSINDEX_SEMI_CODE = "H30184"  # 中证全指半导体产品与设备
CSINDEX_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
    f"uploads/file/autofile/indicator/{CSINDEX_SEMI_CODE}indicator.xls"
)
NBS_IC_PATH = "工业 > 工业主要产品产量 > 集成电路"
NBS_IC_YOY_ROW = "集成电路产量_同比增长(%)"

_MONTHS_CN = {m: i + 1 for i, m in enumerate(
    ["1月", "2月", "3月", "4月", "5月", "6月",
     "7月", "8月", "9月", "10月", "11月", "12月"])}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 本连接器覆盖的三个指标 → 采集频率
_REAL_INDICATORS = (
    "ind:半导体销售额同比",
    "ind:芯片出货量同比",
    "ind:科技行业PE(TTM)",
)


def _cache_dir(source: str) -> Path:
    base = Path(get_settings().sqlite_path).parent / "industry_metrics" / source
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_snapshot(source: str, payload: dict[str, Any]) -> Path:
    path = _cache_dir(source) / f"{date.today().isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def _latest_snapshot(source: str) -> dict[str, Any] | None:
    folder = Path(get_settings().sqlite_path).parent / "industry_metrics" / source
    files = sorted(folder.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def parse_wsts_workbook(content: bytes, months: int = 24) -> list[dict[str, Any]]:
    """解析 WSTS Monthly Data 表，输出最近 months 个月全球销售额同比序列。

    返回 [{"period":"YYYY-MM","yoy":float,"sales_usd_thousand":float}] 升序。
    销售额单位与 WSTS 原表一致（千美元）；同比为与上年同月比（%）。
    """
    wb = pd.read_excel(io.BytesIO(content), sheet_name="Monthly Data",
                       header=None, engine="openpyxl")
    series: dict[tuple[int, int], float] = {}
    current_year: int | None = None
    for row in wb.itertuples(index=False):
        label = str(row[0]).strip() if pd.notna(row[0]) else ""
        if re.fullmatch(r"\d{4}", label):
            current_year = int(label)
            continue
        if label == "Worldwide" and current_year is not None:
            for m in range(1, 13):
                raw = row[m] if m < len(row) else None
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if val == val:  # 过滤 NaN
                    series[(current_year, m)] = val

    records: list[dict[str, Any]] = []
    for (year, m), value in sorted(series.items()):
        prev = series.get((year - 1, m))
        if not prev:
            continue
        records.append({
            "period": f"{year:04d}-{m:02d}",
            "yoy": round((value / prev - 1.0) * 100, 1),
            "sales_usd_thousand": round(value),
        })
    return records[-months:]


def parse_nbs_ic_yoy(df: pd.DataFrame) -> list[dict[str, Any]]:
    """NBS 集成电路产量同比表 → 升序月度 [{"period","yoy"}]。

    列形如 "2026年7月"（降序），行为各指标；仅取同比增长行。
    """
    row = df[df.index.astype(str).str.strip() == NBS_IC_YOY_ROW]
    if row.empty:
        raise DataFetchError(
            f"NBS返回缺少指标行 {NBS_IC_YOY_ROW}；实际行: {list(df.index)[:8]}"
        )
    records: list[dict[str, Any]] = []
    for col in df.columns:
        m = re.fullmatch(r"(\d{4})年(\d{1,2})月", str(col).strip())
        if not m:
            continue
        raw = row.iloc[0][col]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value == value:
            records.append({
                "period": f"{int(m.group(1)):04d}-{int(m.group(2)):02d}",
                "yoy": round(value, 1),
            })
    records.sort(key=lambda r: r["period"])
    return records


def parse_csindex_pe(content: bytes) -> list[dict[str, Any]]:
    """中证 indicator.xls → 升序日频 [{"period":"YYYY-MM-DD","pe_ttm":float}]。

    市盈率2=滚动(TTM)口径。
    """
    df = pd.read_excel(io.BytesIO(content))
    df.columns = [
        "日期", "指数代码", "指数全称", "指数简称",
        "指数英文全称", "指数英文简称",
        "市盈率1", "市盈率2", "股息率1", "股息率2",
    ]
    df["日期"] = pd.to_datetime(df["日期"], format="%Y%m%d", errors="coerce").dt.date
    df["市盈率2"] = pd.to_numeric(df["市盈率2"], errors="coerce")
    df = df.dropna(subset=["日期", "市盈率2"]).sort_values("日期")
    return [
        {"period": str(r["日期"]), "pe_ttm": round(float(r["市盈率2"]), 2),
         "index_name": str(r["指数简称"])}
        for _, r in df.iterrows()
    ]


class RealTechIndustryConnector(BaseConnector):
    """科技行业真实产业数据连接器（WSTS/NBS/中证官网）。"""

    source_name = "真实产业数据(WSTS/国家统计局/中证)"
    source_url = WSTS_LISTING_URL

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": "科技行业真实产业数据",
            "source_type": DataSourceType.API.value,
            "simulated": False,
            "indicators": list(_REAL_INDICATORS),
            "notes": (
                "半导体销售额同比=WSTS全球月度账单自行计算同比；"
                "芯片出货量同比=国家统计局集成电路产量同比；"
                "科技行业PE(TTM)=中证全指半导体(H30184)滚动市盈率"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator in _REAL_INDICATORS

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if indicator == "ind:半导体销售额同比":
            records = await self._fetch_wsts()
            return self._to_points(
                indicator, records, "yoy",
                source_name="WSTS全球半导体贸易统计",
                source_url=WSTS_LISTING_URL, frequency="monthly",
                start_date=start_date, end_date=end_date,
                value_extra={"sales_unit": "USD thousand (Worldwide)"},
            )
        if indicator == "ind:芯片出货量同比":
            records = await self._fetch_nbs()
            return self._to_points(
                indicator, records, "yoy",
                source_name="国家统计局(集成电路产量同比)",
                source_url="https://data.stats.gov.cn", frequency="monthly",
                start_date=start_date, end_date=end_date,
                value_extra={"proxy": "中国集成电路产量当月同比，作为芯片出货量代理指标"},
            )
        if indicator == "ind:科技行业PE(TTM)":
            records = await self._fetch_csindex()
            return self._to_points(
                indicator, records, "pe_ttm",
                source_name="中证指数官网(全指半导体H30184)",
                source_url=CSINDEX_URL, frequency="daily",
                start_date=start_date, end_date=end_date,
                value_extra={"index_code": CSINDEX_SEMI_CODE,
                             "metric": "PE_TTM"},
            )
        raise DataFetchError(f"真实产业连接器不支持的指标: {indicator}")

    # ---------------- 三个数据源 ----------------

    async def _fetch_wsts(self) -> list[dict[str, Any]]:
        def _work() -> tuple[list[dict[str, Any]], str]:
            import requests

            page = requests.get(WSTS_LISTING_URL, timeout=20,
                                headers={"User-Agent": _UA})
            page.raise_for_status()
            match = WSTS_XLS_RE.search(page.text)
            if not match:
                raise DataFetchError("WSTS页面未找到Historical Billings Excel链接")
            xls_url = match.group(1)
            resp = requests.get(xls_url, timeout=60,
                                headers={"User-Agent": _UA})
            resp.raise_for_status()
            return parse_wsts_workbook(resp.content), xls_url

        try:
            records, xls_url = await self._to_thread(_work)
        except Exception as exc:  # noqa: BLE001 网络失败尝试本地最近快照
            snap = _latest_snapshot("wsts")
            if snap:
                logger.warning("WSTS在线获取失败，使用本地快照: %s", exc)
                return [{**r, "storage_fallback": True} for r in snap["records"]]
            raise DataFetchError(f"WSTS半导体销售额获取失败且无本地快照: {exc}") from exc
        _write_snapshot("wsts", {"source_url": xls_url,
                                 "fetch_time": datetime.now().isoformat(),
                                 "records": records})
        return records

    async def _fetch_nbs(self) -> list[dict[str, Any]]:
        def _work() -> list[dict[str, Any]]:
            import akshare as ak

            last_exc: Exception | None = None
            for attempt in range(3):  # NBS新站偶发WAF挑战，重试
                try:
                    df = ak.macro_china_nbs_nation(
                        kind="月度数据", path=NBS_IC_PATH, period="2022-2026")
                    return parse_nbs_ic_yoy(df)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    time.sleep(2.5 * (attempt + 1))
            raise DataFetchError(f"国家统计局集成电路产量3次重试均失败: {last_exc}")

        try:
            records = await self._to_thread(_work)
        except DataFetchError:
            snap = _latest_snapshot("nbs")
            if snap:
                logger.warning("NBS在线获取失败，使用本地快照")
                return [{**r, "storage_fallback": True} for r in snap["records"]]
            raise
        if records:
            _write_snapshot("nbs", {"source": "国家统计局",
                                    "path": NBS_IC_PATH,
                                    "fetch_time": datetime.now().isoformat(),
                                    "records": records})
        return records

    async def _fetch_csindex(self) -> list[dict[str, Any]]:
        def _work() -> list[dict[str, Any]]:
            import requests

            resp = requests.get(CSINDEX_URL, timeout=15, headers={
                "User-Agent": _UA, "Referer": "https://www.csindex.com.cn/"})
            resp.raise_for_status()
            return parse_csindex_pe(resp.content)

        try:
            records = await self._to_thread(_work)
        except Exception as exc:  # noqa: BLE001
            snap = _latest_snapshot("csindex")
            if snap:
                logger.warning("中证官网PE获取失败，使用本地快照: %s", exc)
                return [{**r, "storage_fallback": True} for r in snap["records"]]
            raise DataFetchError(f"中证指数官网PE获取失败且无本地快照: {exc}") from exc
        if records:
            _write_snapshot("csindex", {"source_url": CSINDEX_URL,
                                        "fetch_time": datetime.now().isoformat(),
                                        "records": records})
        return records

    # ---------------- 组装 DataPoint ----------------

    @staticmethod
    async def _to_thread(func: Any) -> Any:
        import asyncio

        return await asyncio.to_thread(func)

    @staticmethod
    def _to_points(
        indicator: str,
        records: list[dict[str, Any]],
        value_key: str,
        *,
        source_name: str,
        source_url: str,
        frequency: str,
        start_date: str | None,
        end_date: str | None,
        value_extra: dict[str, Any],
    ) -> list[DataPoint]:
        points: list[DataPoint] = []
        for r in records:
            period = r["period"]
            # 月频 period=YYYY-MM，按 YYYY-MM-01 与日期边界比较
            comparable = period if frequency == "daily" else f"{period}-01"
            if start_date and comparable < start_date:
                continue
            if end_date and comparable > end_date:
                continue
            extra = {
                "simulated": False,
                "frequency": frequency,
                "raw_indicator": indicator[len("ind:"):],
                **value_extra,
            }
            if r.get("storage_fallback"):
                extra["storage_fallback"] = True
            if "index_name" in r:
                extra["index_name"] = r["index_name"]
            points.append(DataPoint(
                indicator=indicator,
                value=float(r[value_key]),
                period_date=period,
                extra=extra,
                source_name=source_name,
                source_url=source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.9,
                verified=True,
            ))
        return points
