"""估值空间模块：判断个股处于「上涨空间」还是「估值透支」。

三个对标口径（有哪个用哪个，全部缺失即报缺口）：

  1. 个股自身历史分位（主）：PE(TTM)/PB 近三年日频序列 → 当前值的三年分位。
     数据经项目既有采集链（ConnectorRouter: QMT→CSV→AkShare 百度估值）取数，
     **复用**了既有连接器与 TTL/DB 缓存，不重复造轮子。

  2. 同业中位数（配置口径）：configs/intraday.yaml 的 peers 股票池，
     经腾讯批量快照一次取回 PE(TTM)/PB，算中位数与个股相对溢价率。

  3. 行业中位数（权威口径）：巨潮「行业市盈率」接口的中位数PE，
     作为不依赖自选池、可自动获取的行业对标线。

结论口径：估值空间分 score∈[-1,1]，正=有上涨空间，负=估值透支；
headroom 五档：ample(空间充足) / moderate(中性偏多) / stretched(合理偏贵) /
expensive(估值透支) / unknown(数据不足)。
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.intraday.config import IntradayConfig, WatchConfig
from src.intraday.features import safe_float
from src.intraday.indicators import clip, percentile_rank
from src.intraday.models import ValuationPeer, ValuationSpace
from src.intraday.sources import IntradayDataProvider

logger = logging.getLogger(__name__)

# 个股跑输/跑赢同业中位数的饱和度：溢价 50% 记满分（-1），折价 50% 记满分（+1）
_PEER_PREMIUM_SCALE = 50.0
# 分位分量的中枢：50% 分位为中性零分，0%/100% 分位各记 ±1
_PERCENTILE_CENTER = 50.0

_HEADROOM_LABELS = {
    "ample": "上涨空间充足",
    "moderate": "估值中性偏多",
    "stretched": "估值合理偏贵",
    "expensive": "估值透支",
    "unknown": "数据不足",
}


def valuation_from_series(
    pe_points: list[Any], pb_points: list[Any],
) -> dict[str, Any]:
    """PE/PB 日频序列 → 当前值与三年分位（纯函数，便于单测）。

    pe_points/pb_points 为项目 DataPoint（含 value 与 period_date）。
    """
    result: dict[str, Any] = {}
    for name, points in (("pe", pe_points), ("pb", pb_points)):
        series = pd.Series(
            [safe_float(getattr(p, "value", None)) for p in points or []],
            dtype="float64",
        ).dropna()
        result[f"{name}_current"] = None if series.empty else float(series.iloc[-1])
        result[f"{name}_percentile"] = (
            None if series.empty else percentile_rank(series, float(series.iloc[-1]))
        )
        result[f"{name}_days"] = int(len(series))
        result[f"{name}_min"] = None if series.empty else float(series.min())
        result[f"{name}_max"] = None if series.empty else float(series.max())
        result[f"{name}_median"] = None if series.empty else float(series.median())
    return result


def compute_headroom_score(
    *, pe_percentile: float | None, pb_percentile: float | None,
    pe: float | None, pb: float | None,
    peer_pe_median: float | None, peer_pb_median: float | None,
    industry_pe_median: float | None,
) -> tuple[float | None, list[str]]:
    """合成估值空间分（∈[-1,1]）与口径说明。

    分量（各占等权，仅使用可得分量）：
      分位分量  = (50 - 分位) / 50          → 分位越低（越便宜）越正
      同业溢价  = clip(-(PE/同业中位-1)×100 / 50)
      行业溢价  = clip(-(PE/行业中位-1)×100 / 50)
      同业PB溢价= clip(-(PB/同业中位-1)×100 / 50)
    """
    components: list[float] = []
    notes: list[str] = []
    percentiles = [p for p in (pe_percentile, pb_percentile) if p is not None]
    if percentiles:
        average = sum(percentiles) / len(percentiles)
        components.append(clip((_PERCENTILE_CENTER - average) / 50.0))
        notes.append(f"自身历史分位均值 {average:.0f}%")
    if pe is not None and peer_pe_median:
        premium = (pe / peer_pe_median - 1.0) * 100.0
        components.append(clip(-premium / _PEER_PREMIUM_SCALE))
        notes.append(f"PE相对同业中位数 {premium:+.0f}%")
    if pb is not None and peer_pb_median:
        premium = (pb / peer_pb_median - 1.0) * 100.0
        components.append(clip(-premium / _PEER_PREMIUM_SCALE))
        notes.append(f"PB相对同业中位数 {premium:+.0f}%")
    if pe is not None and industry_pe_median:
        premium = (pe / industry_pe_median - 1.0) * 100.0
        components.append(clip(-premium / _PEER_PREMIUM_SCALE))
        notes.append(f"PE相对行业中位数 {premium:+.0f}%")
    if not components:
        return None, notes
    return clip(sum(components) / len(components)), notes


def headroom_bucket(score: float | None) -> str:
    """估值空间分 → 五档结论。"""
    if score is None:
        return "unknown"
    if score >= 0.30:
        return "ample"
    if score >= 0.05:
        return "moderate"
    if score >= -0.20:
        return "stretched"
    return "expensive"


class ValuationProvider:
    """估值空间取数（个股分位 + 同业/行业中位数）。"""

    def __init__(self, config: IntradayConfig, data: IntradayDataProvider,
                 backend: Any = None) -> None:
        self._config = config
        self._data = data
        self._backend = backend  # ConnectorRouter（复用项目既有采集链取PE/PB序列）

    async def fetch(self, code: str, name: str = "",
                    watch: WatchConfig | None = None) -> ValuationSpace:
        """组装估值空间面板数据。"""
        gaps: list[str] = []
        source_names: list[str] = []

        pe_points: list[Any] = []
        pb_points: list[Any] = []
        if self._backend is not None:
            pe_points, pb_points, fetch_gaps = await self._fetch_series(code)
            gaps.extend(fetch_gaps)
            if pe_points or pb_points:
                proxy_source = self._proxy_source(pe_points, pb_points)
                source_names.append(
                    proxy_source or "项目采集链(百度估值)")
                gaps.extend(self._proxy_disclosure(pe_points, pb_points))
        else:
            gaps.append("估值序列不可用：未注入数据采集链")

        stats = valuation_from_series(pe_points, pb_points)
        pe = stats.get("pe_current")
        pb = stats.get("pb_current")

        # ---- 同业（配置口径） ----
        peers: list[ValuationPeer] = []
        peer_source = "unavailable"
        peer_label = ""
        peer_codes = list(watch.peers) if watch and watch.peers else []
        if peer_codes:
            peers, peer_gap, peer_ok = await self._fetch_peers(peer_codes)
            if peer_ok:
                peer_source = "configured"
                peer_label = f"自选同业（{len(peers)}只）"
                source_names.append("腾讯批量快照")
            elif peer_gap:
                gaps.append(peer_gap)

        peer_pe_median = _median([p.pe_ttm for p in peers])
        peer_pb_median = _median([p.pb for p in peers])

        # ---- 行业（巨潮权威口径） ----
        industry_name = watch.industry if watch else ""
        industry_pe_median, industry_count, industry_gap = (
            await self._fetch_industry_median(industry_name) if industry_name
            else (None, None, "")
        )
        if industry_pe_median is not None:
            source_names.append("巨潮行业市盈率")
        elif industry_gap:
            gaps.append(industry_gap)

        score, notes = compute_headroom_score(
            pe_percentile=stats.get("pe_percentile"),
            pb_percentile=stats.get("pb_percentile"),
            pe=pe, pb=pb, peer_pe_median=peer_pe_median,
            peer_pb_median=peer_pb_median,
            industry_pe_median=industry_pe_median,
        )
        bucket = headroom_bucket(score)

        pe_vs_peer = (
            (pe / peer_pe_median - 1.0) * 100.0
            if pe is not None and peer_pe_median else None
        )
        pb_vs_peer = (
            (pb / peer_pb_median - 1.0) * 100.0
            if pb is not None and peer_pb_median else None
        )
        verdict = self._verdict(bucket, stats, pe_vs_peer, notes)
        available = pe is not None or pb is not None or bool(peers)

        return ValuationSpace(
            available=available,
            code=code, name=name,
            pe_ttm=pe, pb=pb,
            pe_percentile=stats.get("pe_percentile"),
            pb_percentile=stats.get("pb_percentile"),
            pe_series_days=stats.get("pe_days", 0),
            pb_series_days=stats.get("pb_days", 0),
            pe_min=stats.get("pe_min"), pe_max=stats.get("pe_max"),
            pe_median=stats.get("pe_median"),
            pb_min=stats.get("pb_min"), pb_max=stats.get("pb_max"),
            pb_median=stats.get("pb_median"),
            peer_source=peer_source, peer_label=peer_label, peers=peers,
            peer_pe_median=peer_pe_median, peer_pb_median=peer_pb_median,
            peer_count=len(peers),
            pe_vs_peer_pct=None if pe_vs_peer is None else round(pe_vs_peer, 1),
            pb_vs_peer_pct=None if pb_vs_peer is None else round(pb_vs_peer, 1),
            industry_pe_median=industry_pe_median,
            industry_name=industry_name,
            industry_company_count=industry_count,
            verdict=verdict, headroom=bucket, score=score,
            gap="；".join(gaps) if gaps else None,
            source_name="、".join(dict.fromkeys(source_names)),
        )

    async def _fetch_series(
        self, code: str,
    ) -> tuple[list[Any], list[Any], list[str]]:
        """PE(TTM)/PB 日频序列（经项目采集链，失败仅记缺口）。"""
        gaps: list[str] = []
        points: dict[str, list[Any]] = {}
        for prefix in ("PE(TTM)", "PB"):
            indicator = f"{prefix}:{code}"
            try:
                result = await self._backend.fetch(indicator)
                points[prefix] = result or []
            except DataFetchError as exc:
                gaps.append(f"{indicator} 取数失败：{brief(exc, BRIEF_TIGHT)}")
            except Exception as exc:  # noqa: BLE001 数据层任何异常都不应打断面板
                gaps.append(f"{indicator} 取数异常：{brief(exc, BRIEF_TIGHT)}")
        return points.get("PE(TTM)", []), points.get("PB", []), gaps

    @staticmethod
    def _proxy_source(pe_points: list[Any], pb_points: list[Any]) -> str:
        """代理估值点的来源标注（非代理返回空串，走默认百度口径标注）。"""
        for points in (pe_points, pb_points):
            if points:
                extra = getattr(points[-1], "extra", None) or {}
                if extra.get("proxy"):
                    return f"{extra.get('source', '关联指数')}(ETF代理估值)"
        return ""

    @staticmethod
    def _proxy_disclosure(
        pe_points: list[Any], pb_points: list[Any],
    ) -> list[str]:
        """ETF代理估值强制披露：代理关系、序列长度（分位口径）、存储降级。"""
        notes: list[str] = []
        seen: set[str] = set()
        for metric, points in (("PE", pe_points), ("PB", pb_points)):
            if not points:
                continue
            extra = getattr(points[-1], "extra", None) or {}
            if not extra.get("proxy"):
                continue
            idx_name = extra.get("proxy_index_name", "关联指数")
            kind = "行业" if extra.get("proxy_kind") == "industry_index" else "宽基"
            key = f"{metric}:{idx_name}:{extra.get('source', '')}"
            if key in seen:
                continue
            seen.add(key)
            note = f"{metric}为ETF代理估值（非自身估值）：采用{kind}指数「{idx_name}」"
            note += f"，来源{extra.get('source', '未知')}，序列{len(points)}期"
            if len(points) < 60:
                note += "（不足3个月，历史分位仅供参考）"
            if extra.get("storage_fallback"):
                note += "，本次为本地最近快照"
            notes.append(note)
        return notes

    async def _fetch_peers(
        self, codes: list[str],
    ) -> tuple[list[ValuationPeer], str, bool]:
        """同业 PE/PB（腾讯批量快照）。"""
        try:
            quotes, _, _ = await self._data.fetch_peer_quotes(codes)
        except DataFetchError as exc:
            return [], f"同业快照失败：{brief(exc, BRIEF_TIGHT)}", False
        peers = [
            ValuationPeer(
                code=quote.code, name=quote.name, pe_ttm=quote.pe_ttm,
                pb=quote.pb, price=quote.price, change_pct=quote.change_pct,
            )
            for quote in quotes.values()
        ]
        peers.sort(key=lambda p: (p.pe_ttm is None, p.pe_ttm or 0.0))
        return peers, "", bool(peers)

    async def _fetch_industry_median(
        self, industry_name: str,
    ) -> tuple[float | None, int | None, str]:
        """巨潮行业市盈率中位数（当月无数据自动回退上月）。"""
        frame = await asyncio.to_thread(self._load_industry_frame)
        if frame is None or len(frame) == 0:
            return None, None, "巨潮行业市盈率接口不可用"
        hit = frame[frame["行业名称"].astype(str).str.strip() == industry_name.strip()]
        if hit.empty:
            hit = frame[frame["行业名称"].astype(str).str.contains(
                industry_name.strip(), na=False, regex=False)]
        if hit.empty:
            return None, None, f"巨潮行业分类未匹配到「{industry_name}」"
        row = hit.iloc[0]
        median = safe_float(row.get("静态市盈率-中位数"))
        count = safe_float(row.get("纳入计算公司数量"))
        return median, None if count is None else int(count), ""

    @staticmethod
    def _load_industry_frame() -> Any:
        try:
            import akshare as ak
        except ImportError:
            return None
        today = pd.Timestamp.now().normalize()
        for offset in (0, 1, 2):
            month = (today - pd.DateOffset(months=offset)).replace(day=1)
            try:
                frame = ak.stock_industry_pe_ratio_cninfo(
                    symbol="证监会行业分类",
                    date=month.strftime("%Y%m%d"))
            except Exception as exc:  # noqa: BLE001 巨潮接口偶发为空
                logger.warning("巨潮行业市盈率取数失败(%s): %s",
                               month.strftime("%Y%m"), brief(exc, BRIEF_TIGHT))
                continue
            if frame is not None and len(frame):
                return frame
        return None

    @staticmethod
    def _verdict(bucket: str, stats: dict[str, Any],
                 pe_vs_peer: float | None, notes: list[str]) -> str:
        """一句人话结论（前端直接展示）。"""
        label = _HEADROOM_LABELS.get(bucket, "数据不足")
        parts = [f"估值结论：{label}"]
        pe, pctl = stats.get("pe_current"), stats.get("pe_percentile")
        if pe is not None and pctl is not None:
            pe_days = int(stats.get("pe_days") or 0)
            # ETF行业代理仅近20期，不得套用"近三年"口径，按实际样本长度表述
            window = "近三年" if pe_days >= 600 else (
                f"近{pe_days}个交易日" if pe_days else "可得样本")
            parts.append(f"PE(TTM) {pe:.1f} 处于{window} {pctl:.0f}% 分位")
        if pe_vs_peer is not None:
            parts.append(
                f"相对同业中位数{'溢价' if pe_vs_peer >= 0 else '折价'}"
                f" {abs(pe_vs_peer):.0f}%")
        if stats.get("pb_percentile") is not None:
            pb_days = int(stats.get("pb_days") or 0)
            pb_window = "三年" if pb_days >= 600 else f"{pb_days}日"
            parts.append(f"PB {stats['pb_current']:.2f}"
                         f"（{pb_window}{stats['pb_percentile']:.0f}%分位）")
        return "；".join(parts)


def _median(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return None
    return round(float(pd.Series(clean).median()), 3)
