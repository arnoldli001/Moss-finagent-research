"""科创板/创业板板块数据连接器：成交额、估值分位、个股截面（多源主备冗余）。

数据源（全部免费、免API Key），每类数据均设计主源+备源自动降级：
1. 实时成交额：腾讯财经 qt.gtimg.cn（parts[37]=成交额万元，GBK）
   → 备源：东财push2（f48=成交额元）
2. 历史成交额序列：东财push2his日K（f57=成交额元）
   → 备源：AKShare index_zh_a_hist（东财同口径日线封装）
3. 板块整体估值：AKShare乐咕 stock_market_pe_lg（创业板/科创板全历史板块PE，含分位；
   乐咕创业板列为"平均市盈率"、科创板为"市盈率"，接口不提供板块PB，诚实降级为PE口径）
   → 备源：创业板走东财板块BK0475现值PE动态；科创板走中证官网科创50现值PE（代理口径）
4. 个股行情截面：东财clist（创业板+科创板全个股分页拉取，fltt=2真值口径；
   分页失败时部分数据仅在接受线≥80%total时采用，否则整轮降级）
   → 备源：AKShare stock_cy_a_spot_em + stock_zh_kcb_spot（东财同上游）
   → 第三源：新浪行情列表 Market_Center.getHQNodeData（独立于东财，
     node=cyb/kcb，涨跌幅为真值%，amount为元）

覆盖指数：创业板指(sz399006)、科创50(sh000688)、科创综指(sh000680)。
金额统一亿元；估值含1/3/5年/全历史分位；非交易时段返回最近交易日快照。

指标约定（mkt:cybkcb:前缀）：
- "mkt:cybkcb:turnover:all"     → 实时成交额3条（cyb/kcb/kcb_all，亿元）
- "mkt:cybkcb:turnover:cyb|kcb|kcb_all" → 单指数实时成交额
- "mkt:cybkcb:turnover_hist"    → 近60交易日两板合计成交额序列
  （value=创业板全板+科创板全板口径；extra含创业板指/科创50/科创综指三分项）
- "mkt:cybkcb:val:all"          → 板块估值2条（cyb_pe/kcb_pe，extra含历史分位）
- "mkt:cybkcb:val:cyb_pe|kcb_pe" → 单项板块PE
- "mkt:cybkcb:spot_summary"     → 两板个股截面统计（涨跌家数/成交合计/涨幅前5）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import httpx

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.index_valuation_connector import percentile_rank

logger = logging.getLogger(__name__)

_HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# 板块指数：key → (腾讯代码, 东财secid, 展示名)
_BOARD_INDICES: dict[str, tuple[str, str, str]] = {
    "cyb": ("sz399006", "0.399006", "创业板指"),
    "kcb": ("sh000688", "1.000688", "科创50"),
    "kcb_all": ("sh000680", "1.000680", "科创综指"),
}
# 腾讯报文split("~")后成交额索引（万元）
_TENCENT_AMOUNT_IDX = 37

_INDICATOR_RE_PREFIX = "mkt:cybkcb:"
_VALID_KEYS = {
    "turnover:all", "turnover:cyb", "turnover:kcb", "turnover:kcb_all",
    "turnover_hist", "val:all", "val:cyb_pe", "val:kcb_pe",
    "spot_summary",
}
# 估值分位窗口（交易日约250/年）
_PCTL_WINDOWS: dict[str, int] = {"1y": 250, "3y": 750, "5y": 1250, "all": 10**9}
_HIST_DAYS = 60
# 东财板块兜底：创业板板块PE动态/PB（现值，无分位）
_EM_BOARD_SECID = "90.BK0475"


def _safe_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _percentiles(
    history: list[float], current: float
) -> dict[str, float | None]:
    """按1/3/5年/全历史窗口计算分位（0-100）；history按期别升序。"""
    out: dict[str, float | None] = {}
    for label, window in _PCTL_WINDOWS.items():
        out[f"pct_{label}"] = percentile_rank(history[-window:], current)
    return out


class StarChinextConnector(BaseConnector):
    """科创板/创业板板块数据：成交额+估值分位+个股截面，四类数据全主备。"""

    source_name = "腾讯/东财/乐咕(科创板创业板)"
    source_url = "http://qt.gtimg.cn"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": sorted(_VALID_KEYS),
            "notes": (
                "覆盖创业板指/科创50/科创综指；成交额亿元（腾讯→东财，历史东财→"
                "AKShare）；板块估值PE含分位（乐咕→东财板块/中证官网兜底）；"
                "个股截面涨跌家数/成交合计/涨幅前5（东财clist→AKShare→新浪列表）"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        if not indicator.startswith(_INDICATOR_RE_PREFIX):
            return False
        return indicator[len(_INDICATOR_RE_PREFIX):] in _VALID_KEYS

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if not self.supports(indicator):
            raise DataFetchError(f"科创板创业板连接器不支持的指标: {indicator}")
        key = indicator[len(_INDICATOR_RE_PREFIX):]
        if key == "turnover_hist":
            return await self._fetch_turnover_hist()
        if key.startswith("val:"):
            return await self._fetch_valuation(key)
        if key == "spot_summary":
            return await self._fetch_spot_summary()
        return await self._fetch_turnover_realtime(key)

    # ---------- 1. 实时成交额：腾讯 → 东财push2 ----------

    async def _fetch_turnover_realtime(self, key: str) -> list[DataPoint]:
        wanted = list(_BOARD_INDICES) if key == "turnover:all" else [key[9:]]
        quotes, source = await self._fetch_realtime_quotes(wanted)
        today = date.today().isoformat()
        points = [
            DataPoint(
                indicator=f"{_INDICATOR_RE_PREFIX}turnover:{k}",
                value=round(quotes[k], 2), unit="亿元", period_date=today,
                extra={"name": _BOARD_INDICES[k][2], "source": source,
                       "is_intraday": True},
                source_name=self.source_name,
                source_url=self.source_url,
                fetch_method=FetchMethod.API_CALL, confidence=0.95,
            )
            for k in wanted
        ]
        if key == "turnover:all":
            return points
        return [points[0]]

    async def _fetch_realtime_quotes(
        self, wanted: list[str]
    ) -> tuple[dict[str, float], str]:
        """实时成交额（亿元），腾讯失败自动降级东财push2。"""
        try:
            quotes = await self._fetch_tencent_realtime(wanted)
            return quotes, "腾讯财经"
        except Exception as exc:  # noqa: BLE001 主源失败自动降级
            logger.warning("腾讯实时行情失败，回退东财push2: %s", exc)
        quotes = await self._fetch_eastmoney_realtime(wanted)
        return quotes, "东方财富push2"

    async def _fetch_tencent_realtime(
        self, wanted: list[str]
    ) -> dict[str, float]:
        codes = ",".join(_BOARD_INDICES[k][0] for k in wanted)
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"http://qt.gtimg.cn/q={codes}")
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="ignore")
        result: dict[str, float] = {}
        for line in text.strip().split(";"):
            if "~" not in line:
                continue
            parts = line.split("~")
            if len(parts) <= _TENCENT_AMOUNT_IDX:
                continue
            for k, (tcode, _, _) in _BOARD_INDICES.items():
                if tcode in line and k in wanted:
                    amount = _safe_float(parts[_TENCENT_AMOUNT_IDX])
                    if amount is not None:
                        result[k] = amount / 10000.0  # 万元→亿元
        missing = set(wanted) - result.keys()
        if missing:
            raise DataFetchError(f"腾讯行情缺少板块指数: {missing}")
        return result

    async def _fetch_eastmoney_realtime(
        self, wanted: list[str]
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        async with httpx.AsyncClient(timeout=8, headers=_HEADERS) as client:
            for k in wanted:
                secid = _BOARD_INDICES[k][1]
                url = (
                    "https://push2.eastmoney.com/api/qt/stock/get"
                    f"?secid={secid}&fields=f48"
                )
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = (resp.json().get("data") or {})
                except Exception as exc:  # noqa: BLE001
                    raise DataFetchError(
                        f"东财push2 {secid} 请求失败: {exc}") from exc
                amount = _safe_float(data.get("f48"))
                if amount is None:
                    raise DataFetchError(f"东财push2 {secid} 无f48")
                result[k] = amount / 1e8  # 元→亿元
        return result

    # ---------- 2. 历史成交额：东财push2his → AKShare ----------

    async def _fetch_turnover_hist(self) -> list[DataPoint]:
        """三指数近60交易日成交额序列（亿元），东财主源失败降级AKShare。

        合计value=cyb+kcb_all（创业板全板+科创板全板口径，与新浪截面
        total_turnover_yi交叉验证一致）；科创50(610亿量级)仅50只成分股，
        不得混入两板合计，单独存于extra.kcb。
        """
        try:
            rows = await self._fetch_hist_eastmoney()
            source = "东方财富push2his"
        except Exception as exc:  # noqa: BLE001 主源失败自动降级
            logger.warning("东财日K历史失败，回退AKShare: %s", exc)
            rows = await self._fetch_hist_akshare()
            source = "AKShare index_zh_a_hist"
        return [
            DataPoint(
                indicator=f"{_INDICATOR_RE_PREFIX}turnover_hist",
                value=round(r["cyb"] + r["kcb_all"], 2), unit="亿元",
                period_date=r["date"],
                extra={"cyb": round(r["cyb"], 2), "kcb": round(r["kcb"], 2),
                       "kcb_all": round(r["kcb_all"], 2),
                       "total_caliber": "创业板全板+科创板全板",
                       "source": source},
                source_name=self.source_name,
                source_url=self.source_url,
                fetch_method=FetchMethod.API_CALL, confidence=0.9,
            )
            for r in rows
        ]

    async def _fetch_hist_eastmoney(self) -> list[dict[str, Any]]:
        series: dict[str, dict[str, float]] = {}
        async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
            for k, (_, secid, _) in _BOARD_INDICES.items():
                url = (
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
                    f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
                    "&fields2=f51,f52,f53,f54,f55,f56,f57"
                    f"&klt=101&fqt=0&end=20500101&lmt={_HIST_DAYS}"
                )
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    klines = ((resp.json().get("data") or {}).get("klines")) or []
                except Exception as exc:  # noqa: BLE001
                    raise DataFetchError(
                        f"东财日K {secid} 失败: {exc}") from exc
                for line in klines:
                    cols = line.split(",")
                    if len(cols) < 7:
                        continue
                    amount = _safe_float(cols[6])
                    if amount is None:
                        continue
                    series.setdefault(cols[0], {})[k] = amount / 1e8  # 元→亿元
        rows = [
            {"date": d, **{k: s.get(k, 0.0) for k in _BOARD_INDICES}}
            for d, s in sorted(series.items())
            if len(s) == len(_BOARD_INDICES)
        ]
        if len(rows) < 5:
            raise DataFetchError(f"东财日K序列过短: {len(rows)}行")
        return rows

    async def _fetch_hist_akshare(self) -> list[dict[str, Any]]:
        """AKShare备源：index_zh_a_hist拉三指数日线（东财同口径封装）。"""
        import akshare as ak

        def _pull(symbol: str) -> dict[str, float]:
            try:
                df = ak.index_zh_a_hist(
                    symbol=symbol, period="daily",
                    start_date=_hist_start_date(), end_date="20500101",
                )
            except Exception as exc:  # noqa: BLE001
                raise DataFetchError(f"AKShare {symbol} 请求失败: {exc}") from exc
            if df is None or df.empty:
                raise DataFetchError(f"AKShare {symbol} 空数据")
            df = df.tail(_HIST_DAYS)
            return {
                str(row["日期"]): float(row["成交额"]) / 1e8
                for _, row in df.iterrows()
            }

        try:
            pulls = await asyncio.gather(*[
                asyncio.to_thread(_pull, _BOARD_INDICES[k][1].split(".")[1])
                for k in _BOARD_INDICES
            ])
        except Exception as exc:  # noqa: BLE001 备源失败统一转DataFetchError
            if isinstance(exc, DataFetchError):
                raise
            raise DataFetchError(f"AKShare历史成交额获取失败: {exc}") from exc
        maps = dict(zip(_BOARD_INDICES.keys(), pulls, strict=True))
        common = sorted(
            set.intersection(*(set(m.keys()) for m in maps.values()))
        )[-_HIST_DAYS:]
        if len(common) < 5:
            raise DataFetchError(f"AKShare历史序列过短: {len(common)}行")
        return [
            {"date": d,
             "cyb": maps["cyb"][d], "kcb": maps["kcb"][d],
             "kcb_all": maps["kcb_all"][d]}
            for d in common
        ]

    # ---------- 3. 板块PE估值：乐咕AKShare → 东财板块/中证官网兜底 ----------

    async def _fetch_valuation(self, key: str) -> list[DataPoint]:
        if key == "val:all":
            wanted = ["cyb_pe", "kcb_pe"]
        else:
            wanted = [key[4:]]
        points: list[DataPoint] = []
        failed: list[str] = []
        for item in wanted:
            board = item.split("_")[0]
            try:
                points.append(await self._valuation_legu(board))
            except Exception as exc:  # noqa: BLE001 主源失败逐项降级
                logger.warning("乐咕估值%s失败，回退备用源: %s", item, exc)
                try:
                    points.append(await self._valuation_fallback(board))
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("备用源%s兜底失败: %s", item, exc2)
                    failed.append(item)
        if failed and not points:
            raise DataFetchError(f"板块估值双源均失败: {', '.join(failed)}")
        return points

    async def _valuation_legu(self, board: str) -> DataPoint:
        """主源：乐咕乐股全历史板块PE（AKShare stock_market_pe_lg）。

        乐咕创业板列为"平均市盈率"、科创板为"市盈率"，模糊匹配；
        接口不提供板块PB，估值口径诚实标注为PE。
        """
        import akshare as ak

        symbol = {"cyb": "创业板", "kcb": "科创板"}[board]
        df = await asyncio.to_thread(ak.stock_market_pe_lg, symbol=symbol)
        if df is None or df.empty:
            raise DataFetchError(f"乐咕{symbol}估值数据为空")
        pe_col = next(
            (c for c in df.columns if "市盈率" in str(c)), None)
        if pe_col is None:
            raise DataFetchError(f"乐咕{symbol}无市盈率列: {list(df.columns)}")
        series = df.dropna(subset=[pe_col])
        current = float(series.iloc[-1][pe_col])
        if current <= 0:
            raise DataFetchError(f"乐咕{symbol}PE现值异常: {current}")
        today = str(series.iloc[-1]["日期"])[:10]
        history = [float(v) for v in series[pe_col].tolist()]
        return DataPoint(
            indicator=f"{_INDICATOR_RE_PREFIX}val:{board}_pe",
            value=round(current, 2), unit="倍", period_date=today,
            extra={
                "name": symbol, "metric": "PE",
                "pe_col": str(pe_col),
                **_percentiles(history[:-1], current),
                "history_size": len(history),
                "source": "乐咕乐股",
            },
            source_name=self.source_name, source_url="https://legulegu.com",
            fetch_method=FetchMethod.API_CALL, confidence=0.9,
        )

    async def _valuation_fallback(self, board: str) -> DataPoint:
        """备源：创业板走东财板块现值PE动态；科创板走中证官网科创50现值PE。"""
        if board == "cyb":
            return await self._valuation_em_board()
        return await self._valuation_csindex_proxy()

    async def _valuation_em_board(self) -> DataPoint:
        """东财创业板板块现值PE动态（无分位，extra披露口径）。"""
        async with httpx.AsyncClient(timeout=8, headers=_HEADERS) as client:
            url = (
                "https://push2.eastmoney.com/api/qt/stock/get"
                f"?secid={_EM_BOARD_SECID}&fields=f9,f58"
            )
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json().get("data") or {}
        value = _safe_float(data.get("f9"))
        if value is None or value <= 0:
            raise DataFetchError("东财板块无f9")
        return DataPoint(
            indicator=f"{_INDICATOR_RE_PREFIX}val:cyb_pe",
            value=round(value, 2), unit="倍", period_date=date.today().isoformat(),
            extra={
                "name": data.get("f58") or "创业板",
                "metric": "PE动态",
                "note": "东财板块现值口径，无历史分位",
                "source": "东方财富板块",
            },
            source_name=self.source_name,
            source_url="https://push2.eastmoney.com",
            fetch_method=FetchMethod.API_CALL, confidence=0.75,
        )

    async def _valuation_csindex_proxy(self) -> DataPoint:
        """中证官网科创50现值PE（科创板全板代理，extra披露口径差异）。"""
        from src.infrastructure.connectors.index_valuation_connector import (
            IndexValuationConnector,
        )

        proxy = IndexValuationConnector()
        pts = await proxy.fetch("idx_val:pe_ttm:科创50")
        if not pts:
            raise DataFetchError("中证官网科创50现值PE不可用")
        base = pts[0]
        return DataPoint(
            indicator=f"{_INDICATOR_RE_PREFIX}val:kcb_pe",
            value=base.value, unit=base.unit, period_date=base.period_date,
            extra={
                "name": "科创板", "metric": "PE",
                "note": "中证官网科创50指数现值PE（科创板全板代理口径，非全板均值）",
                "source": "中证指数官网",
            },
            source_name=self.source_name,
            source_url="https://www.csindex.com.cn",
            fetch_method=FetchMethod.API_CALL, confidence=0.7,
        )

    # ---------- 4. 个股截面：东财clist → AKShare → 新浪列表 ----------

    async def _fetch_spot_summary(self) -> list[DataPoint]:
        try:
            summary = await self._spot_eastmoney()
            source = "东方财富clist"
        except Exception as exc:  # noqa: BLE001 主源失败自动降级
            logger.warning("东财clist截面失败，回退AKShare: %s", exc)
            try:
                summary = await asyncio.to_thread(self._spot_akshare)
                source = "AKShare stock_cy_a_spot_em/stock_zh_kcb_spot"
            except Exception as exc2:  # noqa: BLE001 备源失败降级第三源
                logger.warning("AKShare截面失败，回退新浪列表: %s", exc2)
                summary = await self._spot_sina()
                source = "新浪行情列表"
        today = date.today().isoformat()
        return [DataPoint(
            indicator=f"{_INDICATOR_RE_PREFIX}spot_summary",
            value=summary["up_ratio"], unit="%",
            period_date=today,
            extra={**summary, "source": source},
            source_name=self.source_name,
            source_url="https://push2.eastmoney.com/api/qt/clist/get",
            fetch_method=FetchMethod.API_CALL, confidence=0.9,
        )]

    async def _spot_eastmoney(self) -> dict[str, Any]:
        """东财clist分页拉创业板+科创板个股（单页上限100），聚合截面统计。

        fltt=2使涨跌幅/换手率返回真值浮点（缺省为×100整数，须防口径错位）；
        单页失败重试1次，仍失败时仅当已拉取≥80%total才接受部分数据，
        否则整轮抛错触发备源降级，避免存入严重偏差的截面。
        """
        all_rows: list[dict[str, Any]] = []
        page, total = 1, 0
        async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
            while True:
                url = (
                    "https://push2.eastmoney.com/api/qt/clist/get"
                    f"?pn={page}&pz=100&po=1&np=1&fid=f6&fltt=2"
                    # 创业板(m:0+t:80/t:81) + 科创板(m:1+t:23)
                    "&fs=m:0+t:80,m:0+t:81+s:2048,m:1+t:23"
                    "&fields=f3,f6,f8,f12,f14"
                )
                data: dict[str, Any] = {}
                for attempt in range(2):  # 单页重试1次
                    try:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        data = (resp.json().get("data") or {})
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 0:
                            await asyncio.sleep(0.5)
                        elif not all_rows:
                            raise DataFetchError(
                                f"东财clist个股截面失败: {exc}") from exc
                        else:
                            last_exc = exc
                if not data:
                    # 部分数据接受线：≥80%total才可用，否则整轮降级
                    if total and len(all_rows) >= 0.8 * total:
                        logger.warning(
                            "clist第%d页失败，用已拉取%d/%d条数据",
                            page, len(all_rows), total)
                        break
                    raise DataFetchError(
                        f"东财clist个股截面失败(第{page}页): {last_exc}"
                    ) from last_exc
                rows = data.get("diff") or []
                all_rows.extend(rows)
                total = int(data.get("total") or 0)
                if not rows or (total and len(all_rows) >= total) or page > 80:
                    break
                page += 1
                await asyncio.sleep(0.15)  # 分页限速，避免触发风控
        if not all_rows:
            raise DataFetchError("东财clist个股截面为空")
        return _summarize_rows(all_rows)

    def _spot_akshare(self) -> dict[str, Any]:
        """AKShare备源：创业板+科创板实时行情聚合（同口径统计）。"""
        import akshare as ak

        def _to_rows(df) -> list[dict[str, Any]]:
            if df is None or df.empty:
                return []
            rename = {
                "涨跌幅": "f3", "成交额": "f6", "换手率": "f8",
                "代码": "f12", "名称": "f14",
            }
            return [{v: row[k] for k, v in rename.items() if k in df.columns}
                    for _, row in df.iterrows()]

        df_cyb = ak.stock_cy_a_spot_em()
        df_kcb = ak.stock_zh_kcb_spot()
        rows = _to_rows(df_cyb) + _to_rows(df_kcb)
        if not rows:
            raise DataFetchError("AKShare个股截面为空")
        return _summarize_rows(rows)

    async def _spot_sina(self) -> dict[str, Any]:
        """新浪行情列表第三源（独立于东财上游）：node=cyb/kcb分页拉取。

        changepercent为真值%（新浪原生口径），amount为元，统一映射为
        东财字段名后复用_summarize_rows聚合。
        """
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": _HEADERS["User-Agent"]},
        ) as client:
            for node in ("cyb", "kcb"):
                page = 1
                while True:
                    url = (
                        "https://vip.stock.finance.sina.com.cn/quotes_service"
                        "/api/json_v2.php/Market_Center.getHQNodeData"
                        f"?page={page}&num=100&sort=amount&asc=0&node={node}"
                        "&symbol=&_s_r_a=page"
                    )
                    try:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        batch = resp.json() or []
                    except Exception as exc:  # noqa: BLE001
                        raise DataFetchError(
                            f"新浪{node}第{page}页失败: {exc}") from exc
                    if not batch:
                        break
                    rows.extend({
                        "f12": str(r.get("code") or ""),
                        "f14": str(r.get("name") or ""),
                        "f3": _safe_float(r.get("changepercent")),
                        "f6": _safe_float(r.get("amount")),
                        "f8": _safe_float(r.get("turnoverratio")),
                    } for r in batch if isinstance(r, dict))
                    page += 1
                    await asyncio.sleep(0.15)  # 分页限速
        if not rows:
            raise DataFetchError("新浪个股截面为空")
        return _summarize_rows(rows)


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """个股行情截面 → 统一统计结构（涨跌家数/成交合计/涨幅前5等）。"""
    up = down = flat = 0
    total_amount = 0.0
    cyb_amount = kcb_amount = 0.0
    changes: list[float] = []
    top: list[dict[str, Any]] = []
    for r in rows:
        chg = _safe_float(r.get("f3"))
        amount = _safe_float(r.get("f6")) or 0.0
        code = str(r.get("f12") or "")
        if chg is not None:
            changes.append(chg)
            if chg > 0:
                up += 1
            elif chg < 0:
                down += 1
            else:
                flat += 1
        if code.startswith(("300", "301")):
            cyb_amount += amount
        elif code.startswith(("688", "689")):  # 689=科创板CDR（如九号公司）
            kcb_amount += amount
        total_amount += amount
        if chg is not None and len(top) < 200:
            top.append({"code": code, "name": str(r.get("f14") or ""),
                        "chg_pct": chg})
    top5 = sorted(top, key=lambda x: x["chg_pct"], reverse=True)[:5]
    bottom5 = sorted(top, key=lambda x: x["chg_pct"])[:5]
    median = sorted(changes)[len(changes) // 2] if changes else 0.0
    return {
        "stock_count": len(rows),
        "up_count": up, "down_count": down, "flat_count": flat,
        "up_ratio": round(up / max(1, up + down + flat) * 100, 2),
        "median_chg_pct": median,
        "total_turnover_yi": round(total_amount / 1e8, 2),
        "cyb_turnover_yi": round(cyb_amount / 1e8, 2),
        "kcb_turnover_yi": round(kcb_amount / 1e8, 2),
        "top5_gainers": top5,
        "bottom5_losers": bottom5,
        "calc": "创业板+科创板全个股行情聚合",
    }


def _hist_start_date() -> str:
    """历史起点：约120自然日≈60交易日，留足缓冲。"""
    from datetime import timedelta
    return (date.today() - timedelta(days=140)).strftime("%Y%m%d")
