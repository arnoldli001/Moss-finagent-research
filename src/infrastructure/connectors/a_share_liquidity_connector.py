"""A股流动性连接器：两市成交额、全A换手率（多源互备）。

数据源（全部免费、免API Key），按可靠性自动fallback：
1. 实时成交额：腾讯财经 qt.gtimg.cn（GBK，parts[37]=成交额万元，毫秒级）
2. 东财push2实时行情（f48=成交额元，f168=涨跌幅）
3. 全A换手率：东财clist（Σ成交额/Σ流通市值）→ 腾讯成交额 / AkShare流通市值代理
4. 历史序列：东财push2his日K → AkShare stock_zh_index_daily（新浪源）

故障域说明：
- 2026-09-15 实测 push2.eastmoney.com 整体被风控断连（clist + push2his 均 Server disconnected）
- 修复：所有 push2/push2his 调用加**腾讯/AkShare 独立故障域** fallback
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any

import httpx

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# 腾讯代码 → (展示名, 分项key)
_TENCENT_CODES: dict[str, tuple[str, str]] = {
    "sh000001": ("上证指数", "sh"),
    "sz399001": ("深证成指", "sz"),
    "sz399006": ("创业板指", "cyb"),
    "sh000688": ("科创50", "kcb"),
}
# 东财secid
_EM_SECIDS: dict[str, str] = {
    "sh": "1.000001", "sz": "0.399001",
    "cyb": "0.399006", "kcb": "1.000688",
}
_HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


async def _get_json(client: httpx.AsyncClient, url: str, *, retries: int = 2) -> Any:
    """GET+json，东财偶发断连时短退避重试（实测高频访问会被临时限流）。"""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


_TURNOVER_INDICATOR_RE = re.compile(r"^mkt:turnover:(total|sh|sz|cyb|kcb|hist)$")
_RATE_INDICATOR_RE = re.compile(r"^mkt:turnover_rate:(all_a|hist)$")
_HIST_DAYS = 60  # MA50至少需要50个交易日


class AShareLiquidityConnector(BaseConnector):
    """A股大盘流动性：两市成交额 + 全A换手率，腾讯/东财双源互备。"""

    source_name = "腾讯财经/东方财富(A股流动性)"
    source_url = "http://qt.gtimg.cn"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": [
                "mkt:turnover:total", "mkt:turnover:sh", "mkt:turnover:sz",
                "mkt:turnover:cyb", "mkt:turnover:kcb",
                "mkt:turnover:hist",
                "mkt:turnover_rate:all_a", "mkt:turnover_rate:hist",
            ],
            "notes": "成交额单位亿元；换手率单位%；腾讯→东财实时双源，历史取东财日K",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(
            _TURNOVER_INDICATOR_RE.match(indicator)
            or _RATE_INDICATOR_RE.match(indicator)
        )

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        today = date.today().isoformat()
        if indicator == "mkt:turnover:hist":
            return await self._fetch_turnover_hist(today)
        if indicator == "mkt:turnover_rate:hist":
            return await self._fetch_turnover_rate_hist(today)
        if indicator == "mkt:turnover_rate:all_a":
            return await self._fetch_all_a_rate(today)
        m = _TURNOVER_INDICATOR_RE.match(indicator)
        if not m:
            raise DataFetchError(f"A股流动性连接器不支持的指标: {indicator}")
        key = m.group(1)
        quotes = await self._fetch_realtime_quotes()
        if key == "total":
            total = quotes["sh"] + quotes["sz"]
            return [DataPoint(
                indicator=indicator, value=round(total, 2), unit="亿元",
                period_date=today,
                extra={
                    "sh": round(quotes["sh"], 2), "sz": round(quotes["sz"], 2),
                    "cyb": round(quotes["cyb"], 2), "kcb": round(quotes["kcb"], 2),
                    "total": round(total, 2),
                    "source": quotes["_source"], "is_intraday": quotes["_intraday"],
                },
                source_name=self.source_name, source_url=self.source_url,
                fetch_method=FetchMethod.API_CALL, confidence=0.95,
            )]
        return [DataPoint(
            indicator=indicator, value=round(quotes[key], 2), unit="亿元",
            period_date=today,
            extra={"name": _TENCENT_CODES[[c for c, v in _TENCENT_CODES.items()
                                           if v[1] == key][0]][0],
                   "source": quotes["_source"], "is_intraday": quotes["_intraday"]},
            source_name=self.source_name, source_url=self.source_url,
            fetch_method=FetchMethod.API_CALL, confidence=0.95,
        )]

    # ---------- 实时（腾讯→东财 fallback） ----------

    async def _fetch_realtime_quotes(self, *, client: httpx.AsyncClient | None = None
                                     ) -> dict[str, Any]:
        """返回 {sh,sz,cyb,kcb: 亿元} 及_source标记；腾讯失败回退东财push2。"""
        try:
            quotes = await self._fetch_tencent(client)
            quotes["_source"] = "腾讯财经"
            return quotes
        except Exception as exc:  # noqa: BLE001 主源失败自动降级
            logger.warning("腾讯成交额接口失败，回退东财push2: %s", exc)
        quotes = await self._fetch_eastmoney_push2(client)
        quotes["_source"] = "东方财富push2"
        return quotes

    async def _fetch_tencent(self, client: httpx.AsyncClient | None = None
                             ) -> dict[str, Any]:
        owns = client or httpx.AsyncClient(timeout=8)
        try:
            codes = ",".join(_TENCENT_CODES.keys())
            resp = await owns.get(f"http://qt.gtimg.cn/q={codes}")
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="ignore")
        finally:
            if client is None:
                await owns.aclose()
        result: dict[str, Any] = {}
        for line in text.strip().split(";"):
            if "~" not in line:
                continue
            parts = line.split("~")
            if len(parts) <= 37:
                continue
            for tcode, (_, key) in _TENCENT_CODES.items():
                if tcode in line:
                    result[key] = float(parts[37]) / 10000.0  # 万元→亿元
        missing = {"sh", "sz", "cyb", "kcb"} - result.keys()
        if missing:
            raise DataFetchError(f"腾讯行情缺少字段: {missing}")
        result["_intraday"] = True
        return result

    async def _fetch_eastmoney_push2(self, client: httpx.AsyncClient | None = None
                                     ) -> dict[str, Any]:
        owns = client or httpx.AsyncClient(timeout=8, headers=_HEADERS)
        try:
            result: dict[str, Any] = {}
            for key, secid in _EM_SECIDS.items():
                url = (
                    "https://push2.eastmoney.com/api/qt/stock/get"
                    f"?secid={secid}&fields=f48"
                )
                data = (await _get_json(owns, url)).get("data") or {}
                amount_yuan = data.get("f48")
                if amount_yuan is None:
                    raise DataFetchError(f"东财push2 {secid} 无f48")
                result[key] = float(amount_yuan) / 1e8  # 元→亿元
            result["_intraday"] = True
            return result
        finally:
            if client is None:
                await owns.aclose()

    # ---------- 全A换手率（clist聚合 + 独立故障域代理） ----------

    async def _fetch_all_a_rate(self, today: str) -> list[DataPoint]:
        """全市场加权换手率 = Σ成交额 / Σ流通市值 ×100%。

        主源：东财 push2 clist（push2.eastmoney.com/api/qt/clist/get）
        备源：腾讯两市成交额（独立故障域 qt.gtimg.cn）× 历史全A换手率代理值
        终极降级：返回上证综指换手率代理（历史上全A换手率 ≈ 上证综指换手率 × 1.1-1.3）
        """
        # 主源尝试
        try:
            return await self._fetch_all_a_rate_clist(today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财clist全A换手率断连，降级腾讯代理: %s", exc)

        # 备源：腾讯成交额 × 固定代理（历史上全A日均成交 ~12000亿，
        # 全A流通市值 ~80万亿 → 日均换手率 ≈ 1.5%，代理系数 1.3）
        try:
            proxy = await self._fetch_all_a_rate_proxy(today)
            if proxy is not None:
                return proxy
        except Exception as exc:  # noqa: BLE001
            logger.warning("腾讯代理换手率也失败，终极降级: %s", exc)

        # 终极降级：返回历史平均（2024-2025 全A日均换手率 ~1.5%）
        return [DataPoint(
            indicator="mkt:turnover_rate:all_a", value=1.5, unit="%",
            period_date=today,
            extra={"source": "fallback_constant",
                   "note": "东财+腾讯双源均断连，返回历史均值~1.5%",
                   "proxy": "上证综指换手率×1.25经验系数"},
            source_name=self.source_name,
            source_url="https://qt.gtimg.cn",
            fetch_method=FetchMethod.FALLBACK, confidence=0.5,
        )]

    async def _fetch_all_a_rate_clist(self, today: str) -> list[DataPoint]:
        """东财 clist 全市场实时聚合（原始主源）。"""
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            "?pn=1&pz=6000&po=1&np=1&fid=f6"
            "&fs=m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80,m:0+t:81+s:2048"
            "&fields=f6,f21"
        )
        async with httpx.AsyncClient(timeout=12, headers=_HEADERS) as owns:
            payload = await _get_json(owns, url)
            rows = (payload.get("data") or {}).get("diff") or []
        total_amount = sum(float(r.get("f6") or 0) for r in rows)
        total_float_mv = sum(float(r.get("f21") or 0) for r in rows)
        if total_float_mv <= 0:
            raise DataFetchError("东财clist流通市值聚合为0")
        rate = total_amount / total_float_mv * 100
        amounts = sorted(
            (float(r.get("f6") or 0) for r in rows), reverse=True)
        top_n = max(1, int(len(amounts) * 0.05))
        concentration = round(sum(amounts[:top_n]) / total_amount * 100, 2)
        return [DataPoint(
            indicator="mkt:turnover_rate:all_a", value=round(rate, 3), unit="%",
            period_date=today,
            extra={"sample_size": len(rows),
                   "total_turnover_yi": round(total_amount / 1e8, 2),
                   "top5pct_concentration_pct": concentration,
                   "calc": "Σ个股成交额/Σ个股流通市值×100",
                   "source": "东方财富clist"},
            source_name=self.source_name,
            source_url="https://push2.eastmoney.com/api/qt/clist/get",
            fetch_method=FetchMethod.API_CALL, confidence=0.9,
        )]

    async def _fetch_all_a_rate_proxy(self, today: str) -> list[DataPoint] | None:
        """腾讯成交额 × 换手率代理系数（独立故障域）。"""
        try:
            quotes = await self._fetch_realtime_quotes()
        except Exception as exc:  # noqa: BLE001
            logger.info("腾讯成交额也失败: %s", exc)
            return None
        total_yi = quotes["sh"] + quotes["sz"]
        # 全A流通市值（固定代理：2025年初约80万亿，单位亿元）
        float_mv_yi = 800_000.0
        rate = total_yi / float_mv_yi * 100
        return [DataPoint(
            indicator="mkt:turnover_rate:all_a", value=round(rate, 3), unit="%",
            period_date=today,
            extra={"source": "腾讯成交额 + 流通市值代理",
                   "total_turnover_yi": round(total_yi, 2),
                   "proxy_float_mv_yi": float_mv_yi,
                   "note": "东财clist断连降级，confidence降至0.7"},
            source_name=self.source_name,
            source_url="https://qt.gtimg.cn",
            fetch_method=FetchMethod.FALLBACK, confidence=0.7,
        )]

    # ---------- 历史序列（东财push2his日K + 新浪独立故障域） ----------

    async def _fetch_em_kline(self, secid: str, limit: int = _HIST_DAYS
                              ) -> list[dict[str, Any]]:
        """指数日K + 成交额 + 换手率。

        主源：东财 push2his（push2his.eastmoney.com/api/qt/stock/kline/get）
        备源：AkShare stock_zh_index_daily（新浪 finance.sina.com.cn，独立故障域）
        """
        # 主源尝试
        try:
            rows = await self._fetch_em_kline_push2his(secid, limit)
            if rows:
                return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财push2his断连，降级新浪源: %s", exc)

        # 备源：AkShare 新浪（独立故障域）
        try:
            rows = await self._fetch_em_kline_sina(secid, limit)
            if rows:
                return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("新浪源也失败: %s", exc)

        raise DataFetchError(
            f"东财push2his + 新浪 双源均失败: secid={secid}")

    async def _fetch_em_kline_push2his(self, secid: str, limit: int
                                       ) -> list[dict[str, Any]]:
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt=101&fqt=0&end=20500101&lmt={limit}"
        )
        async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as owns:
            payload = await _get_json(owns, url)
            klines = ((payload.get("data") or {}).get("klines")) or []
        rows: list[dict[str, Any]] = []
        for line in klines:
            cols = line.split(",")
            if len(cols) < 11:
                continue
            rows.append({
                "date": cols[0], "close": _safe_float(cols[2]),
                "amount_yi": (_safe_float(cols[6]) or 0) / 1e8,
                "turnover_rate": _safe_float(cols[10]),
            })
        return rows

    async def _fetch_em_kline_sina(self, secid: str, limit: int
                                   ) -> list[dict[str, Any]]:
        """AkShare 新浪源指数日线（独立故障域）。

        secid 映射：1.000001 → sh000001；0.399001 → sz399001
        新浪只有 volume（手）没有 amount（元）和 turnover_rate，
        用 amount ≈ volume × close × 100 近似，turnover_rate 返回 None。
        """
        import akshare as ak
        # secid → 新浪 symbol
        secid_map = {
            "1.000001": "sh000001",   # 上证指数
            "0.399001": "sz399001",   # 深证成指
        }
        sina_symbol = secid_map.get(secid)
        if sina_symbol is None:
            raise DataFetchError(f"新浪源无映射 secid={secid}")

        loop = asyncio.get_running_loop()
        df = await loop.run_in_executor(
            None, lambda: ak.stock_zh_index_daily(symbol=sina_symbol))
        if df is None or len(df) == 0:
            raise DataFetchError(f"新浪源 {sina_symbol} 无数据")

        # 取最近 limit 条
        tail = df.tail(limit)
        rows: list[dict[str, Any]] = []
        for _, r in tail.iterrows():
            close = float(r["close"])
            vol_shou = float(r["volume"])   # 手
            amount_yuan = vol_shou * close * 100  # 手×价格×每手100股
            rows.append({
                "date": str(r["date"])[:10],
                "close": round(close, 2),
                "amount_yi": round(amount_yuan / 1e8, 2),
                "turnover_rate": None,  # 新浪源无换手率字段
            })
        return rows

    async def _fetch_turnover_hist(self, today: str) -> list[DataPoint]:
        """沪+深日成交额加总序列（亿元），供MA5/MA10/MA50与量能分位计算。"""
        try:
            sh_rows, sz_rows = await asyncio.gather(
                self._fetch_em_kline(_EM_SECIDS["sh"]),
                self._fetch_em_kline(_EM_SECIDS["sz"]),
            )
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"东财日K历史成交额获取失败: {exc}") from exc
        sz_map = {r["date"]: r["amount_yi"] for r in sz_rows}
        points: list[DataPoint] = []
        for r in sh_rows:
            total = r["amount_yi"] + sz_map.get(r["date"], 0.0)
            points.append(DataPoint(
                indicator="mkt:turnover:hist", value=round(total, 2), unit="亿元",
                period_date=r["date"],
                extra={"sh": round(r["amount_yi"], 2),
                       "sz": round(sz_map.get(r["date"], 0.0), 2),
                       "close_sh": r["close"], "source": "东方财富push2his"},
                source_name=self.source_name,
                source_url="https://push2his.eastmoney.com",
                fetch_method=FetchMethod.API_CALL, confidence=0.9,
            ))
        return points

    async def _fetch_turnover_rate_hist(self, today: str) -> list[DataPoint]:
        """上证综指换手率历史（全A换手率的免费代理，extra披露口径）。

        当 push2his 可用时直接用 f61 字段；
        降级到新浪源时，用 amount_yi / (收盘价 × 流通市值代理) 反推换手率。
        """
        rows = await self._fetch_em_kline(_EM_SECIDS["sh"])
        source_flag = "东方财富push2his"
        confidence = 0.85
        # 新浪源：turnover_rate=None，用 amount / (close × float_mv_proxy) 反推
        if rows and rows[0].get("turnover_rate") is None:
            source_flag = "新浪AkShare(换手率反推)"
            confidence = 0.6
            float_mv_proxy = 40_000.0  # 上证综指成分股流通市值代理 4万亿
            enriched = []
            for r in rows:
                amount_yi = r.get("amount_yi") or 0
                turnover = amount_yi / float_mv_proxy * 100 if float_mv_proxy > 0 else 0
                enriched.append({**r, "turnover_rate": round(turnover, 4)})
            rows = enriched
        return [DataPoint(
            indicator="mkt:turnover_rate:hist",
            value=r["turnover_rate"], unit="%",
            period_date=r["date"],
            extra={"proxy": "上证综指换手率（全A免费代理口径）",
                   "source": source_flag},
            source_name=self.source_name,
            source_url="https://push2his.eastmoney.com",
            fetch_method=FetchMethod.FALLBACK if confidence < 0.7 else FetchMethod.API_CALL,
            confidence=confidence,
        ) for r in rows]


def _safe_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
