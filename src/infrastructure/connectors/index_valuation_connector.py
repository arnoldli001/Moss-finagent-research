"""主要指数估值分位连接器（AKShare乐咕乐股免费源）。

通过 ak.stock_index_pe_lg / ak.stock_index_pb_lg 获取核心宽基指数的
PE-TTM、PB历史序列，并在本地计算当前值在1年/3年/5年/全部历史中的分位数。

指标约定（idx_val:前缀）：
- "idx_val:pe_ttm:{指数名}" → 某指数最新PE-TTM（extra含PB与各周期历史分位）
- "idx_val:pb:{指数名}"      → 某指数最新PB（extra含各周期历史分位）
- "idx_val:snapshot:all"    → 核心宽基指数最新估值截面（多行DataPoint）

历史分位随每次fetch从接口全量历史计算；每日收盘后定时快照入库积累自有序列。
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import date
from typing import Any

import pandas as pd
import requests

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# 中证官网OSS下载超时（秒）：文件不存在/网络异常时快速失败，避免拖垮日频作业
_CSINDEX_TIMEOUT_SEC = 12
_CSINDEX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.csindex.com.cn/",
}

# 核心宽基指数。乐咕PE/PB仅支持其symbol_map内指数（含创业板50，不含创业板指/
# 科创50/上证综指/深证成指）；科创50走中证官网XLS补充现值PE（无历史分位）。
CORE_INDICES = (
    "沪深300", "中证500", "中证1000", "上证50", "创业板50", "科创50",
)
# 中证官网indicator.xls兜底代码：乐咕被反爬挡住/不支持时取现值PE
# （仅近20期，无PB/分位；分位靠乐咕可用日的快照积累）
# 注意：创业板50(399673)为国证指数非中证系列，OSS无此文件，仅走乐咕源
_CSINDEX_CODE = {
    "沪深300": "000300",
    "中证500": "000905",
    "中证1000": "000852",
    "上证50": "000016",
    "科创50": "000688",
}
_PE_RE = re.compile(r"^idx_val:pe_ttm:(.+)$")
_PB_RE = re.compile(r"^idx_val:pb:(.+)$")
# 分位统计窗口（年）→ 截取天数（按交易日约250/年）
_PCTL_WINDOWS: dict[str, int] = {"1y": 250, "3y": 750, "5y": 1250, "all": 10**9}
# 乐咕反爬较敏感：截面采集串行（cookie获取并发会互相干扰）+指数间错峰
_SNAPSHOT_CONCURRENCY = 1
_SNAPSHOT_STAGGER_SEC = 0.5
_RETRY_SLEEP_SEC = 1.2
_RETRY_TIMES = 3  # 首次+2次退避重试（1.2s/2.4s）


def percentile_rank(series: list[float], current: float) -> float | None:
    """当前值在历史序列中的百分位（0-100，值≤current的占比）。"""
    vals = sorted(v for v in series if v is not None)
    if not vals:
        return None
    le = sum(1 for v in vals if v <= current)
    return round(le / len(vals) * 100, 1)


def _percentiles(history: list[tuple[str, float]], current: float) -> dict[str, Any]:
    """按1/3/5年/全历史窗口计算分位；history按期别升序传入。"""
    out: dict[str, Any] = {}
    for label, window in _PCTL_WINDOWS.items():
        window_vals = [v for _, v in history[-window:]]
        out[f"pct_{label}"] = percentile_rank(window_vals, current)
    out["history_size"] = len(history)
    return out


class IndexValuationConnector(BaseConnector):
    """核心宽基指数PE/PB及历史分位（AKShare乐咕乐股，免费免Key）。"""

    source_name = "AKShare乐咕指数估值"
    source_url = "https://legulegu.com"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": [
                "idx_val:pe_ttm:{指数名}", "idx_val:pb:{指数名}",
                "idx_val:snapshot:all",
            ],
            "notes": "PE-TTM/PB及1/3/5年/全历史分位；覆盖6只核心宽基（乐咕，科创50等中证官网兜底）",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(
            indicator == "idx_val:snapshot:all"
            or _PE_RE.match(indicator)
            or _PB_RE.match(indicator)
        )

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if indicator == "idx_val:snapshot:all":
            return await self._fetch_snapshot()
        m = _PE_RE.match(indicator) or _PB_RE.match(indicator)
        if not m:
            raise DataFetchError(f"指数估值连接器不支持的指标: {indicator}")
        name = m.group(1)
        metric = "pe_ttm" if indicator.startswith("idx_val:pe") else "pb"
        snap = await self._fetch_one(name, metrics=(metric,))
        if not snap:
            return []
        val = snap[0]["value"]
        extra = snap[0]["extra"]
        return [DataPoint(
            indicator=indicator, value=val,
            unit="倍", period_date=date.today().isoformat(),
            extra=extra, source_name=self.source_name,
            source_url=self.source_url,
            fetch_method=FetchMethod.WEB_CRAWL, confidence=0.9,
        )]

    async def _lg_call(self, fn: Any, name: str) -> list[tuple[str, float]]:
        """线程池调用乐咕接口，瞬时失败退避重试一次。

        以下错误短时重试无效，立即抛出由上层转中证官网兜底：
        - KeyError：symbol_map无此指数（如科创50）；
        - AttributeError("...attrs")：乐咕反爬挑战页无csrf标签（封禁以小时计）。
        """
        last_exc: Exception | None = None
        for attempt in range(_RETRY_TIMES):
            try:
                return await asyncio.to_thread(fn, name)
            except KeyError:
                raise
            except AttributeError as exc:
                if "attrs" in str(exc):
                    raise
                last_exc = exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            if attempt < _RETRY_TIMES - 1:
                wait = _RETRY_SLEEP_SEC * (attempt + 1)
                logger.info("乐咕(%s)第%d次失败(%s)，%.1fs后重试",
                            name, attempt + 1, last_exc, wait)
                await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc

    async def _fetch_one(
        self, name: str, *, metrics: tuple[str, ...] = ("pe_ttm", "pb")
    ) -> list[dict[str, Any]]:
        """取单指数最新PE/PB+分位；PE/PB串行请求且独立容错，降低单站并发压力。"""
        # --- PE（主指标）：乐咕失败/不支持 → 中证官网XLS现值兜底 ---
        pe_hist: list[tuple[str, float]] = []
        if "pe_ttm" in metrics:
            try:
                pe_hist = await self._lg_call(self._pe_series, name)
            except Exception as exc:  # noqa: BLE001
                if name in _CSINDEX_CODE:
                    try:
                        cs = await asyncio.to_thread(self._csindex_pe, name)
                    except Exception as cs_exc:  # noqa: BLE001
                        logger.warning("中证官网估值(%s)兜底失败: %s", name, cs_exc)
                        cs = None
                    if cs:
                        date_str, pe_now, pe_static = cs
                        return [{"value": pe_now, "extra": {
                            "index_name": name, "pb": None, "as_of": date_str,
                            "source": "中证指数官网indicator.xls",
                            "pe_static": pe_static,
                            "pe_pct_1y": None, "pe_pct_3y": None,
                            "pe_pct_5y": None, "pe_pct_all": None,
                            "percentiles_unavailable": True,
                            "source_note": "中证官网仅提供近20期，历史分位需快照积累",
                        }}]
                logger.warning("指数估值PE(%s)获取失败: %s", name, exc)
                return []

        # --- PB（辅指标）：失败仅置空并标注，不影响PE与分位 ---
        pb_hist: list[tuple[str, float]] = []
        pb_failed = False
        if "pb" in metrics:
            try:
                pb_hist = await self._lg_call(self._pb_series, name)
            except Exception as exc:  # noqa: BLE001
                logger.info("指数估值PB(%s)获取失败（不影响PE）: %s", name, exc)
                pb_hist, pb_failed = [], True

        latest_date = pe_hist[-1][0] if pe_hist else (
            pb_hist[-1][0] if pb_hist else "")
        pe_now = pe_hist[-1][1] if pe_hist else None
        pb_now = pb_hist[-1][1] if pb_hist else None
        extra: dict[str, Any] = {
            "index_name": name, "pb": pb_now,
            "as_of": latest_date,
            "source": "AKShare乐咕乐股",
        }
        if pb_failed:
            extra["pb_note"] = "PB乐咕源本次不可用"
        if pe_hist and pe_now is not None:
            extra.update({f"pe_{k}": v for k, v
                          in _percentiles(pe_hist, pe_now).items()})
        if pb_hist and pb_now is not None:
            extra.update({f"pb_{k}": v for k, v
                          in _percentiles(pb_hist, pb_now).items()})
        return [{"value": pe_now if "pe_ttm" in metrics else pb_now,
                 "extra": extra}]

    async def _fetch_snapshot(self) -> list[DataPoint]:
        """核心宽基估值截面：PE为主值，PB与分位列extra。

        乐咕反爬敏感：串行+指数间错峰，失败自动退避重试，PE失败转中证官网
        现值兜底，个别指数两源皆失才跳过（上层另可用存储快照降级）。
        """
        sem = asyncio.Semaphore(_SNAPSHOT_CONCURRENCY)

        async def _bound(name: str) -> list[dict[str, Any]]:
            async with sem:
                snap = await self._fetch_one(name)
                await asyncio.sleep(_SNAPSHOT_STAGGER_SEC)
                return snap

        results = await asyncio.gather(*[
            _bound(name) for name in CORE_INDICES
        ])
        today = date.today().isoformat()
        points: list[DataPoint] = []
        for snap in results:
            if not snap or snap[0]["value"] is None:
                continue
            points.append(DataPoint(
                indicator="idx_val:snapshot:all", value=snap[0]["value"],
                unit="倍", period_date=today, extra=snap[0]["extra"],
                source_name=self.source_name, source_url=self.source_url,
                fetch_method=FetchMethod.WEB_CRAWL, confidence=0.9,
            ))
        return points

    @staticmethod
    def _pe_series(name: str) -> list[tuple[str, float]]:
        import akshare as ak

        df = ak.stock_index_pe_lg(symbol=name)
        rows: list[tuple[str, float]] = []
        for _, r in df.iterrows():
            try:
                rows.append((str(r["日期"])[:10], float(r["滚动市盈率"])))
            except (TypeError, ValueError):
                continue
        return sorted(rows)

    @staticmethod
    def _pb_series(name: str) -> list[tuple[str, float]]:
        import akshare as ak

        df = ak.stock_index_pb_lg(symbol=name)
        rows: list[tuple[str, float]] = []
        for _, r in df.iterrows():
            try:
                rows.append((str(r["日期"])[:10], float(r["市净率"])))
            except (TypeError, ValueError):
                continue
        return sorted(rows)

    @staticmethod
    def _csindex_pe(name: str) -> tuple[str, float, float | None] | None:
        """中证官网indicator.xls：返回(日期, 市盈率2(滚动口径), 市盈率1)。

        直连OSS并显式超时（akshare封装无超时，缺失文件会长时间挂起）；
        仅近20期、无PB，用于乐咕被反爬挡住或不支持时的现值兜底。
        """
        code = _CSINDEX_CODE[name]
        url = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/"
               f"uploads/file/autofile/indicator/{code}indicator.xls")
        resp = requests.get(url, timeout=_CSINDEX_TIMEOUT_SEC,
                            headers=_CSINDEX_HEADERS)
        resp.raise_for_status()
        df = pd.read_excel(io.BytesIO(resp.content))
        df.columns = [
            "日期", "指数代码", "指数中文全称", "指数中文简称",
            "指数英文全称", "指数英文简称",
            "市盈率1", "市盈率2", "股息率1", "股息率2",
        ]
        df["日期"] = pd.to_datetime(
            df["日期"], format="%Y%m%d", errors="coerce").dt.date
        df["市盈率1"] = pd.to_numeric(df["市盈率1"], errors="coerce")
        df["市盈率2"] = pd.to_numeric(df["市盈率2"], errors="coerce")
        df = df.dropna(subset=["日期", "市盈率2"]).sort_values("日期")
        if df.empty:
            return None
        r = df.iloc[-1]
        p1 = r["市盈率1"]
        return (
            str(r["日期"])[:10], float(r["市盈率2"]),
            float(p1) if p1 == p1 else None,
        )
