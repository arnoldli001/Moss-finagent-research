"""AkShare数据源连接器。

indicator约定（configs/data_sources.yaml akshare节）：
- "CPI"                  → 全国居民消费价格指数（月度同比，国家统计局NBS源，发布月对齐）
- "PPI"                  → 工业生产者出厂价格指数（月度同比，国家统计局NBS源）
- "stock_close:{code}"   → A股日频收盘价（如 stock_close:000001）
- "index_close:{code}"   → 指数日频收盘价（新浪源，如 index_close:000300）
- "etf_close:{code}"     → ETF日频前复权收盘价（东财主/新浪备，如 etf_close:510300）
- "M2"/"社融"             → 货币供应M2同比 / 社融规模增量（月度）
- "PE(TTM):{code}"/"PB:{code}" → 百度估值日线序列（喂饱A10微观估值）；
  ETF代码（51/58/15/16开头）百度不支持，改走关联指数代理估值：行业主题
  （半导体/芯片→中证全指半导体H30184，中证官网PE-TTM近20期）优先，其次
  宽基（沪深300/中证500/创业板50/科创50等，乐咕PE+PB全序列，PE可降级中证
  官网）；代理关系与口径强制写入每个DataPoint.extra（proxy/proxy_index/…）
- "资产负债率:{code}"/"流动比率:{code}" → 季度财务比率（喂饱A11财务排雷；ETF无此数据）
- "ind:社会消费品零售总额同比" → 真实社零月度同比（替换同名模拟指标）
- "ind:动力煤价格(元/吨)" → 郑煤期货主力收盘价（现货价代理，extra披露口径）

个股行情主源东财（stock_zh_a_hist），连接失败自动回退新浪（stock_zh_a_daily，
前复权）——实测东财接口偶发断连。akshare为阻塞库，fetch经asyncio.to_thread线程池化。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import date as _date
from datetime import datetime
from typing import Any

import numpy as np
import requests

from src.core import symbols
from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.real_industry_connector import (
    _latest_snapshot,
    parse_csindex_pe,
)
from src.infrastructure.connectors.xtquant_connector import QUOTE_PREFIXES

#: 复权口径标签列/键：行情点统一带 `extra["adjust"]`
#: （与腾讯 `re"qfq"` / Tushare `extra["adjust"]` / baostock 同一口径命名）。
#: 它既是"这行是什么价"的溯源，也是链上换源时的对拍依据 ——
#: 缺了它，一次漏传 `adjust=` 的改动会静默污染整条 K 线却无人察觉。
ADJUST_COLUMN = "adjust"


def _is_missing_adjust_param(exc: BaseException) -> bool:
    """该 TypeError 是否是"这个版本的 akshare 没有 `adjust` 形参"。

    只在消息里**同时**出现 `unexpected keyword argument` 与 `adjust` 时才认。
    宽泛地按 `TypeError` 判定会把其它真实缺陷吞掉（实测踩过：调用方签名不匹配
    也被当成"不支持 adjust"，现场只剩一条误导性告警）。
    """
    text = str(exc)
    return "unexpected keyword argument" in text and "adjust" in text

logger = logging.getLogger(__name__)

_DATE_COLUMN_HINTS = ("日期", "月份", "报告日", "时间", "date")
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


#: 股 → 手。新浪各日线接口的成交量单位是**股**，本项目统一口径是**手**
_SINA_SHARES_PER_LOT = 100.0


def _sina_volume_to_lots(df: Any) -> Any:
    """新浪日线帧的成交量：股 → 手（÷100），**就地统一到项目口径**。

    实测（2026-09-22，三源同日对照）：
      600036  新浪 stock_zh_a_daily  volume=43,342,069 ↔ 腾讯 433,421 手（**差 100 倍**）
      510300  新浪 fund_etf_hist_sina volume=644,587,351 ↔ 腾讯 6,445,874 手（差 100 倍）
      000300  新浪 stock_zh_index_daily volume=17,786,387,600 ↔ Tushare index_daily
              17,886,387,600 股（同量级，同为股）
    同日成交额三个源完全一致（新浪 1,770,009,799 元 = Tushare amount 千元×1000），
    说明**只有成交量**这一个字段的单位与项目约定不同。

    这不是"风格问题"：本项目 `extra["volume"]` 的口径是手（东财/腾讯/Tushare/baostock
    都按手），新浪路径不换算就是 100 倍级静默误差，且下游 `daily_bars_from_points`
    只认列名、不做单位校验，量比/量能类因子会整体失真。
    只对有 `volume` 列的新浪帧生效；东财路径（`stock_zh_a_hist` 等）本身即为手，不经此处。

    顺带把换算后的值再写一份 `成交量` 列：东财路径的 extra 用的是中文列名，
    下游 `daily_bars_from_points` 的列名别名表里 `成交量` 排在 `volume` 之后 ——
    两个名字都给出，取数侧就不必关心这一跳走的是哪个子源。
    """
    if df is None or len(df) == 0 or "volume" not in df.columns:
        return df
    df = df.copy()
    lots = df["volume"] / _SINA_SHARES_PER_LOT
    df["volume"] = lots
    df["成交量"] = lots
    return df


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


_MONTH_CN_RE = re.compile(r"(\d{4})\D{0,2}(\d{1,2})")


def period_to_iso(raw: Any) -> str | None:
    """把'2026年07月份'/'202607'/'2026-07-01'/date对象 统一为 YYYY-MM 或 YYYY-MM-DD。"""
    if raw is None:
        return None
    if isinstance(raw, (datetime, _date)):
        return raw.isoformat()[:10]
    text = str(raw).strip()
    digits = text.replace("-", "").replace("/", "")
    if digits.isdigit():
        if len(digits) == 6:
            return f"{digits[:4]}-{digits[4:]}"
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    m = _MONTH_CN_RE.search(text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return text[:10] or None


def _find_col(columns: list[str], *keywords: str) -> str | None:
    """按关键词包含匹配列名（akshare列名随版本可能微调，容错选取）。"""
    for kw in keywords:
        for col in columns:
            if kw in col:
                return col
    return None


def _in_range(period: str | None, start: str | None, end: str | None) -> bool:
    """ISO期间(YYYY-MM或YYYY-MM-DD)与可选起止比较；月粒度边界按整月包含处理。"""
    if not period:
        return False
    key = period.replace("-", "")
    if len(key) == 6:
        key_lo, key_hi = key + "01", key + "31"
    else:
        key_lo = key_hi = key
    s = (start or "").replace("-", "")
    e = (end or "").replace("-", "")
    s = (s + "01")[:8] if s else ""
    e = (e + "31")[:8] if len(e) == 6 else e
    return (not s or key_hi >= s) and (not e or key_lo <= e)


def series_to_points(
    df: Any,
    indicator: str,
    *,
    date_keywords: tuple[str, ...],
    value_keywords: tuple[str, ...],
    start_date: str | None,
    end_date: str | None,
    extra: dict[str, Any] | None = None,
    confidence: float = 0.8,
    exact_date_col: str | None = None,
    exact_value_col: str | None = None,
) -> list[DataPoint]:
    """通用单列时间序列 → DataPoint（日期列/数值列按关键词容错选取）。

    传入 exact_*_col 时要求列名精确相等，避免子串误中
    （如乐咕"等权滚动市盈率"包含"滚动市盈率"）。
    """
    if df is None or len(df) == 0:
        return []
    columns = [str(c) for c in df.columns]
    if exact_date_col is not None or exact_value_col is not None:
        if exact_date_col not in columns or exact_value_col not in columns:
            raise DataFetchError(
                f"AkShare返回结构异常({indicator})：缺{exact_date_col}/"
                f"{exact_value_col}列，实际列={columns}"
            )
        date_col, value_col = exact_date_col, exact_value_col
    else:
        date_col = _find_col(columns, *date_keywords)
        value_col = _find_col(columns, *value_keywords)
    if date_col is None or value_col is None:
        raise DataFetchError(
            f"AkShare返回结构异常({indicator})：缺日期/数值列，实际列={columns}"
        )
    points: list[DataPoint] = []
    for _, row in df.iterrows():
        period = period_to_iso(row[date_col])
        value = _to_float(row[value_col])
        if value is None or not _in_range(period, start_date, end_date):
            continue
        points.append(
            DataPoint(
                indicator=indicator, value=value, period_date=period,
                extra=dict(extra or {}),
                source_name=AkshareConnector.source_name,
                source_url=AkshareConnector.source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=confidence, verified=False,
            )
        )
    return points


# ==================== ETF 代理估值 ====================
# 百度个股估值接口(stock_zh_valuation_baidu)对ETF与行业指数均不可用（实测 KeyError
# 'chartInfo'；对000688/000001等指数代码返回的是同名深市个股估值，绝非指数口径，
# 严禁误用）。ETF 的 PE(TTM)/PB 改按「最相关的行业/宽基指数估值」代理，代理关系在
# 每个 DataPoint.extra 中强制披露（数据溯源规范）：
#   - 行业主题：中证官网 indicator.xls（市盈率2=TTM滚动，仅近20期、无PB，日频积累）；
#   - 宽基：乐咕指数估值（PE+PB全序列），PE 在乐咕反爬/不支持时降级中证官网序列。
_ETF_PREFIXES = ("51", "56", "58", "15", "16")
# 名称关键词（首个命中为准）→ (中证指数代码, 指数名)；行业主题优先于宽基匹配
_ETF_PROXY_CSINDEX: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("半导体", "芯片", "集成电路"), "H30184", "中证全指半导体产品与设备"),
)
# 名称关键词 → 乐咕指数名（PE/PB历史序列）
_ETF_PROXY_LEGU: tuple[tuple[tuple[str, ...], str], ...] = (
    (("沪深300",), "沪深300"),
    (("中证500",), "中证500"),
    (("中证1000",), "中证1000"),
    (("上证50",), "上证50"),
    (("创业板50",), "创业板50"),
    # 创业板指乐咕/中证官网均不支持，以流动性最高的创业板50近似（extra强制披露）
    (("创业板",), "创业板50"),
    (("科创50",), "科创50"),
    (("科创板", "科创"), "科创50"),
)
# 乐咕指数名 → 中证官网指数代码（乐咕不可用时PE兜底；创业板50为国指无此文件）
_LEGU_CSINDEX_CODE = {
    "沪深300": "000300", "中证500": "000905", "中证1000": "000852",
    "上证50": "000016", "科创50": "000688",
}
_ETF_NAME_TTL_SEC = 24 * 3600.0
_CSINDEX_OSS_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
    "uploads/file/autofile/indicator/{code}indicator.xls"
)
_CSINDEX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.csindex.com.cn/",
}
_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbol}"


def _is_etf_code(code: str) -> bool:
    """6位代码是否为场内ETF（沪 51/58，深 15/16）。"""
    return len(code) == 6 and code.isdigit() and code.startswith(_ETF_PREFIXES)


class AkshareConnector(BaseConnector):
    """AkShare连接器：宏观(CPI/PPI/M2/社融)、A股行情、估值、财务比率、部分行业真实指标。"""

    source_name = "AkShare"
    source_url = "https://akshare.akfamily.xyz"

    # ETF简称进程内缓存：code → (缓存时间戳, 简称)，避免PE/PB两次取数重复请求腾讯
    _etf_name_cache: dict[str, tuple[float, str]] = {}

    # 无代码参数的月度真实序列：指标 → (ak接口, 日期列关键词, 数值列关键词, 附加extra, confidence)
    _MACRO_SERIES: dict[str, tuple[str, tuple, tuple, dict, float]] = {
        "M2": ("macro_china_money_supply", ("月份",), ("货币和准货币(M2)-同比增长",),
               {"unit": "同比%"}, 0.8),
        "社融": ("macro_china_shrzgm", ("月份",), ("社会融资规模增量",),
                {"unit": "亿元"}, 0.8),
        "ind:社会消费品零售总额同比": (
            "macro_china_consumer_goods_retail", ("月份",), ("同比增长",),
            {"unit": "同比%", "real_industry_data": True}, 0.85),
    }
    # 美国宏观指标：指标 → (ak接口, 日期列关键词, 数值列关键词, 附加extra, confidence)
    _US_MACRO_SERIES: dict[str, tuple[str, tuple, tuple, dict, float]] = {
        "us_cpi_yoy": ("macro_usa_cpi_yoy", ("时间",), ("现值",),
                       {"unit": "同比%", "country": "US"}, 0.85),
        "us_core_cpi": ("macro_usa_core_cpi_monthly", ("日期",), ("今值",),
                        {"unit": "同比%", "country": "US"}, 0.85),
        "us_nonfarm": ("macro_usa_non_farm", ("日期",), ("今值",),
                       {"unit": "万人", "country": "US",
                        "note": "非农就业新增"}, 0.8),
        "us_unemployment": ("macro_usa_unemployment_rate", ("日期",), ("今值",),
                            {"unit": "%", "country": "US"}, 0.8),
        "us_fed_rate": ("macro_bank_usa_interest_rate", ("日期",), ("今值",),
                        {"unit": "%", "country": "US",
                         "note": "美联储利率决议"}, 0.85),
        "us_pce": ("macro_usa_core_pce_price", ("日期",), ("今值",),
                   {"unit": "同比%", "country": "US",
                    "note": "核心PCE"}, 0.8),
    }
    _CODE_PREFIXES = ("PE(TTM):", "PB:", "资产负债率:", "流动比率:")

    # CPI/PPI 走国家统计局NBS（旧英为财情macro_china_*源2025-09后停更）。
    # NBS目录按年代分段，列"(上年同月=100)指数"→同比%=指数-100。
    _NBS_PRICE_SERIES: dict[str, dict[str, Any]] = {
        "CPI": {
            "row_prefix": "居民消费价格指数(",
            "nodes": [
                ("价格指数 > 居民消费价格分类指数 (上年同月=100) "
                 "> 全国居民消费价格分类指数 (上年同月=100) (2026-)", "2026-2026"),
                ("价格指数 > 居民消费价格分类指数 (上年同月=100) "
                 "> 全国居民消费价格分类指数 (上年同月=100) (2021-2025)", "2021-2025"),
                ("价格指数 > 居民消费价格分类指数 (上年同月=100) "
                 "> 全国居民消费价格分类指数 (上年同月=100) (2016-2020)", "2016-2020"),
            ],
        },
        "PPI": {
            "row_prefix": "工业生产者出厂价格指数(",
            "nodes": [
                ("价格指数 > 工业生产者出厂价格分类指数 "
                 "> 工业生产者出厂价格指数 (上年同月=100)", "2016-2026"),
            ],
        },
    }

    def _nbs_price_yoy_points(
        self, ak: Any, indicator: str,
        start_date: str | None, end_date: str | None,
    ) -> list[DataPoint] | None:
        """NBS月度价格指数同比；全部年代段失败返回None（由旧源兜底）。"""
        cfg = self._NBS_PRICE_SERIES[indicator]
        merged: dict[str, float] = {}
        for path, period in cfg["nodes"]:
            df = None
            for attempt in range(3):  # NBS新站偶发WAF挑战
                try:
                    df = ak.macro_china_nbs_nation(
                        kind="月度数据", path=path, period=period)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        df = None
                    time.sleep(2.0 * (attempt + 1))
            if df is None or len(df) == 0:
                continue
            row = df[df.index.astype(str).str.startswith(cfg["row_prefix"])]
            if row.empty:
                continue
            for col in df.columns:
                m = re.fullmatch(r"(\d{4})年(\d{1,2})月", str(col).strip())
                if not m:
                    continue
                try:
                    index_value = float(row.iloc[0][col])
                except (TypeError, ValueError):
                    continue
                if index_value != index_value:  # NaN
                    continue
                year, month = int(m.group(1)), int(m.group(2))
                # 数据月→次月发布月对齐（CPI/PPI次月9-15日发布），防止月末信号偷看
                month += 1
                if month == 13:
                    year, month = year + 1, 1
                period_date = f"{year:04d}-{month:02d}-01"
                if start_date and period_date < start_date:
                    continue
                if end_date and period_date > end_date:
                    continue
                merged[period_date] = round(index_value - 100.0, 1)
        if not merged:
            return None
        return [
            DataPoint(
                indicator=indicator, value=yoy, period_date=period_date,
                extra={"unit": "同比%", "frequency": "monthly",
                       "nbs_basis": "上年同月=100指数减100",
                       "release_month_aligned": True},
                source_name="国家统计局(AkShare封装)",
                source_url="https://data.stats.gov.cn",
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.85, verified=True,
            )
            for period_date, yoy in sorted(merged.items())
        ]

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": [
                "CPI", "PPI", "M2", "社融",
                "stock_close:{code}", "index_close:{code}", "etf_close:{code}",
                "PE(TTM):{code}", "PB:{code}",
                "资产负债率:{code}", "流动比率:{code}",
                "ind:社会消费品零售总额同比", "ind:动力煤价格(元/吨)",
                *self._US_MACRO_SERIES.keys(),
            ],
            "notes": " akshare未安装时fetch将抛出DataFetchError，需 uv sync --extra data",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return (
            indicator in AkshareConnector._MACRO_SERIES
            or indicator in AkshareConnector._US_MACRO_SERIES
            or indicator in ("CPI", "PPI", "ind:动力煤价格(元/吨)")
            or indicator.startswith(
                QUOTE_PREFIXES + AkshareConnector._CODE_PREFIXES)
        )

    def _load_dataframe(
        self, indicator: str, start_date: str | None, end_date: str | None
    ) -> Any:
        """同步加载原始DataFrame（在线程池中执行）。延迟导入akshare。"""
        try:
            import akshare as ak
        except ImportError as exc:
            raise DataFetchError("akshare未安装，请执行: uv sync --extra data") from exc

        # CPI/PPI旧英为财情源（2025-09后停更，仅作NBS全失败时的历史兜底）
        if indicator == "CPI":
            return ak.macro_china_cpi_monthly()
        if indicator == "PPI":
            return ak.macro_china_ppi_yearly()
        if indicator.startswith(QUOTE_PREFIXES):
            kind, code = indicator.split(":", 1)
            code = code.strip()
            if kind == "stock_close":
                return self._stock_dataframe(ak, code, start_date, end_date)
            if kind == "index_close":
                return self._index_dataframe(ak, code, start_date, end_date)
            if kind == "etf_close":
                return self._etf_dataframe(ak, code, start_date, end_date)
        raise DataFetchError(f"AkShare连接器不支持的指标: {indicator}")

    @staticmethod
    def _filter_frame_dates(
        df: Any, start_date: str | None, end_date: str | None
    ) -> Any:
        """对自带全历史的新浪帧按YYYYMMDD字符串过滤。

        ⚠️ 两个区间条件必须合成**一个不带索引的**掩码（实测踩坑 2026-09-22）：
        新浪返回的是全历史表（上证指数 8731 行、沪深300 5998 行）。若先
        `df = df[dates >= 起]` 再 `df = df[dates <= 止]`，第二个掩码仍带着**原表**
        的 RangeIndex，pandas 会把它 reindex 到已过滤的小表上（UserWarning:
        "Boolean Series key will be reindexed to match DataFrame index"），
        未对齐的位置一律按 False 处理 —— 实测把 2020-01-01~2026-09-22 的指数/ETF
        查询**静默**截断成 2020-01-02~2025-12-31（1631 行，丢掉 2026 全年），
        面板上少一年数据却没有任何报错。改用 numpy 数组（无索引）即不会 reindex。
        """
        dates = df["日期"].astype(str).str.replace("-", "")
        mask = np.ones(len(df), dtype=bool)
        if start_date:
            mask &= (dates >= start_date.replace("-", "")).to_numpy()
        if end_date:
            mask &= (dates <= end_date.replace("-", "")).to_numpy()
        return _sina_volume_to_lots(df[mask])

    def _index_dataframe(
        self, ak: Any, code: str,
        start_date: str | None, end_date: str | None,
    ) -> Any:
        """指数日线（新浪源，指数不除权）：000/880沪，399深。"""
        if code.startswith(("000", "880")):
            symbol = f"sh{code}"
        elif code.startswith("399"):
            symbol = f"sz{code}"
        else:
            raise DataFetchError(f"暂支持000xxx(沪)/399xxx(深)指数: {code}")
        df = ak.stock_zh_index_daily(symbol=symbol)
        df = df.rename(columns={"date": "日期", "close": "收盘"})
        return self._filter_frame_dates(df, start_date, end_date)

    def _etf_dataframe(
        self, ak: Any, code: str,
        start_date: str | None, end_date: str | None,
    ) -> Any:
        """ETF日线：东财主源(前复权)，失败回退新浪(不除权，ETF分红少)。"""
        try:
            return ak.fund_etf_hist_em(
                symbol=code, period="daily",
                start_date=start_date or "20100101",
                end_date=end_date or "20991231", adjust="qfq")
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财ETF接口失败（%s），回退新浪源: %s", code, exc)
            symbol = ("sh" if code.startswith(("51", "58")) else "sz") + code
            df = ak.fund_etf_hist_sina(symbol=symbol)
            df = df.rename(columns={"date": "日期", "close": "收盘"})
            return self._filter_frame_dates(df, start_date, end_date)

    def _load_extra_points(
        self, ak: Any, indicator: str,
        start_date: str | None, end_date: str | None,
    ) -> list[DataPoint] | None:
        """扩展指标（宏观/行业真实序列/估值/财务）；非扩展指标返回None走DataFrame路径。"""
        if indicator in ("CPI", "PPI"):
            points = self._nbs_price_yoy_points(
                ak, indicator, start_date, end_date)
            if points is not None:
                return points
            logger.warning(
                "NBS价格指数(%s)取数失败，回退英为财情历史序列（数据可能滞后）",
                indicator)

        if indicator in self._MACRO_SERIES:
            fn_name, dkw, vkw, extra, conf = self._MACRO_SERIES[indicator]
            df = getattr(ak, fn_name)()
            return series_to_points(
                df, indicator, date_keywords=dkw, value_keywords=vkw,
                start_date=start_date, end_date=end_date, extra=extra, confidence=conf)

        if indicator in self._US_MACRO_SERIES:
            fn_name, dkw, vkw, extra, conf = self._US_MACRO_SERIES[indicator]
            df = getattr(ak, fn_name)()
            return series_to_points(
                df, indicator, date_keywords=dkw, value_keywords=vkw,
                start_date=start_date, end_date=end_date, extra=extra, confidence=conf)

        if indicator == "ind:动力煤价格(元/吨)":
            # 郑煤期货主力连续（ZC0）收盘价代理现货动力煤价，口径必须在extra披露
            df = ak.futures_main_sina(symbol="ZC0", start_date="20150101")
            return series_to_points(
                df, indicator, date_keywords=("日期",), value_keywords=("收盘价",),
                start_date=start_date, end_date=end_date,
                extra={"unit": "元/吨", "real_industry_data": True,
                       "proxy": "郑煤期货主力连续收盘价，非秦皇岛港现货价"},
                confidence=0.65)

        if indicator.startswith(self._CODE_PREFIXES):
            return self._stock_fundamental(ak, indicator, start_date, end_date)
        return None

    def _stock_fundamental(
        self, ak: Any, indicator: str,
        start_date: str | None, end_date: str | None,
    ) -> list[DataPoint]:
        """个股估值（百度日线PE/PB）或季度财务比率；ETF走关联指数代理估值。"""
        prefix, code = indicator.split(":", 1)
        code = code.strip()
        if prefix in ("PE(TTM)", "PB"):
            if _is_etf_code(code):
                return self._etf_proxy_fundamental(
                    ak, indicator, code, prefix, start_date, end_date)
            baidu_ind = "市盈率(TTM)" if prefix == "PE(TTM)" else "市净率"
            df = ak.stock_zh_valuation_baidu(
                symbol=code, indicator=baidu_ind, period="近三年")
            return series_to_points(
                df, indicator, date_keywords=("date", "日期"),
                value_keywords=("value", baidu_ind),
                start_date=start_date, end_date=end_date,
                extra={"valuation": baidu_ind}, confidence=0.8)

        # ETF无个股财务报表，禁止误打个股接口产生误导性数据
        if prefix in ("资产负债率", "流动比率") and _is_etf_code(code):
            raise DataFetchError(f"ETF({code})无个股{prefix}财务指标")

        # 季度财务比率（资产负债率/流动比率），一次拉取整表后取列
        start_year = int(start_date[:4]) if start_date else datetime.now().year - 3
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(start_year))
        col_keyword = "资产负债率" if prefix == "资产负债率" else "流动比率"
        return series_to_points(
            df, indicator, date_keywords=("日期",), value_keywords=(col_keyword,),
            start_date=start_date, end_date=end_date,
            extra={"frequency": "quarterly"}, confidence=0.8)

    # ---------------- ETF 代理估值 ----------------

    def _etf_display_name(self, code: str) -> str:
        """腾讯快照取ETF简称（进程内TTL缓存24h）；失败返回空串。"""
        now = time.time()
        cached = self._etf_name_cache.get(code)
        if cached and now - cached[0] < _ETF_NAME_TTL_SEC:
            return cached[1]
        market = symbols.market_of(code)
        name = ""
        try:
            resp = requests.get(
                _TENCENT_QUOTE_URL.format(symbol=f"{market}{code}"),
                timeout=4, headers={"Referer": "https://gu.qq.com/"})
            resp.encoding = "gbk"
            if '"' in resp.text:
                payload = resp.text.split('"')[1].split("~")
                if len(payload) > 1:
                    name = payload[1].strip()
        except requests.RequestException as exc:
            logger.warning("ETF简称解析失败(%s): %s", code, brief(exc, BRIEF_TIGHT))
        self._etf_name_cache[code] = (now, name)
        return name

    @staticmethod
    def _proxy_extra(
        *, valuation: str, etf_code: str, etf_name: str, kind: str,
        index_name: str, index_code: str | None, source: str, note: str,
    ) -> dict[str, Any]:
        """代理估值 DataPoint 统一溯源字段（强制披露代理关系）。"""
        return {
            "valuation": valuation,
            "proxy": True,
            "proxy_kind": kind,  # industry_index / broad_index
            "proxy_index": index_code or index_name,
            "proxy_index_name": index_name,
            "underlying_etf": etf_code,
            "etf_name": etf_name,
            "source": source,
            "proxy_note": note,
        }

    def _etf_proxy_fundamental(
        self, ak: Any, indicator: str, code: str, prefix: str,
        start_date: str | None, end_date: str | None,
    ) -> list[DataPoint]:
        """ETF PE/PB → 最相关行业/宽基指数估值代理（extra强制披露代理关系）。"""
        etf_name = self._etf_display_name(code)
        if not etf_name:
            raise DataFetchError(
                f"ETF {code} 简称解析失败，无法匹配关联行业/宽基指数估值代理")
        valuation = "市盈率(TTM)" if prefix == "PE(TTM)" else "市净率"

        # 1) 行业主题优先：中证官网 indicator.xls，仅PE-TTM近20期，无PB
        for keywords, idx_code, idx_name in _ETF_PROXY_CSINDEX:
            if any(k in etf_name for k in keywords):
                if prefix == "PB":
                    raise DataFetchError(
                        f"ETF无个股PB；{idx_name}({idx_code})"
                        "仅披露PE-TTM，PB暂缺")
                note = (f"ETF本身无个股估值，采用最相关行业指数"
                        f"「{idx_name}」PE-TTM代理（非ETF自身估值）")
                return self._etf_csindex_points(
                    indicator, code, etf_name, idx_code, idx_name,
                    "industry_index", note, start_date, end_date,
                    snapshot_fallback=(idx_code == "H30184"))

        # 2) 宽基指数：乐咕 PE/PB 全序列；PE 失败时可降级中证官网序列
        for keywords, idx_name in _ETF_PROXY_LEGU:
            if not any(k in etf_name for k in keywords):
                continue
            note = (f"ETF本身无个股估值，采用相关宽基指数「{idx_name}」"
                    "估值代理（非ETF自身估值）")
            if idx_name == "创业板50" and "创业板50" not in etf_name:
                note = ("ETF本身无个股估值，创业板指无公开PE/PB序列，"
                        "采用「创业板50」估值近似代理（非ETF自身估值）")
            extra = self._proxy_extra(
                valuation=valuation, etf_code=code, etf_name=etf_name,
                kind="broad_index", index_name=idx_name, index_code=None,
                source="AKShare乐咕乐股", note=note)
            if prefix == "PE(TTM)":
                try:
                    df = ak.stock_index_pe_lg(symbol=idx_name)
                    points = series_to_points(
                        df, indicator, date_keywords=("日期",),
                        value_keywords=("滚动市盈率",),
                        start_date=start_date, end_date=end_date,
                        extra=extra, confidence=0.7,
                        exact_date_col="日期", exact_value_col="滚动市盈率")
                except Exception as exc:  # noqa: BLE001 反爬/不支持→中证官网兜底
                    oss_code = _LEGU_CSINDEX_CODE.get(idx_name)
                    if not oss_code:
                        raise DataFetchError(
                            f"乐咕宽基PE({idx_name})不可用且无中证官网兜底: "
                            f"{brief(exc, BRIEF_TIGHT)}") from exc
                    logger.info("乐咕PE(%s)失败，降级中证官网: %s",
                                idx_name, brief(exc, BRIEF_TIGHT))
                    return self._etf_csindex_points(
                        indicator, code, etf_name, oss_code, idx_name,
                        "broad_index", note, start_date, end_date,
                        snapshot_fallback=False,
                        source_note="乐咕源不可用，中证官网仅近20期PE、无历史分位")
                if not points:
                    raise DataFetchError(f"乐咕宽基PE({idx_name})返回空序列")
                return points
            # PB：仅乐咕有源；失败（反爬/不支持）如实记缺口，禁止杜撰
            try:
                df = ak.stock_index_pb_lg(symbol=idx_name)
                points = series_to_points(
                    df, indicator, date_keywords=("日期",),
                    value_keywords=("市净率",),
                    start_date=start_date, end_date=end_date,
                    extra=extra, confidence=0.7,
                    exact_date_col="日期", exact_value_col="市净率")
            except Exception as exc:  # noqa: BLE001
                raise DataFetchError(
                    f"ETF代理PB：乐咕{idx_name}市净率不可用"
                    f"（中证官网仅提供PE）：{brief(exc, BRIEF_TIGHT)}") from exc
            if not points:
                raise DataFetchError(f"乐咕宽基PB({idx_name})返回空序列")
            return points

        raise DataFetchError(
            f"ETF({etf_name})暂无已验证的关联行业/宽基指数估值映射，"
            "PE/PB代理取数留缺口")

    def _etf_csindex_points(
        self, indicator: str, code: str, etf_name: str, idx_code: str,
        idx_name: str, kind: str, note: str,
        start_date: str | None, end_date: str | None, *,
        snapshot_fallback: bool, source_note: str = "",
    ) -> list[DataPoint]:
        """中证官网 indicator.xls PE-TTM（市盈率2滚动口径）→ 代理 DataPoint。"""
        url = _CSINDEX_OSS_URL.format(code=idx_code)
        records: list[dict[str, Any]] = []
        storage_fallback = False
        try:
            resp = requests.get(url, timeout=12, headers=_CSINDEX_HEADERS)
            resp.raise_for_status()
            records = parse_csindex_pe(resp.content)
        except requests.RequestException as exc:
            if snapshot_fallback:
                snap = _latest_snapshot("csindex")
                if snap and snap.get("records"):
                    records = list(snap["records"])
                    storage_fallback = True
                    logger.warning("中证官网PE(%s)失败，使用本地快照: %s",
                                   idx_code, brief(exc, BRIEF_TIGHT))
            if not records:
                raise DataFetchError(
                    f"关联指数{idx_name}({idx_code})中证官网PE获取失败: "
                    f"{brief(exc, BRIEF_TIGHT)}") from exc
        extra = self._proxy_extra(
            valuation="市盈率(TTM)", etf_code=code, etf_name=etf_name,
            kind=kind, index_name=idx_name, index_code=idx_code,
            source="中证指数官网indicator.xls", note=note)
        if source_note:
            extra["source_note"] = source_note
        if storage_fallback:
            extra["storage_fallback"] = True
        points: list[DataPoint] = []
        for rec in records:
            period = str(rec["period"])
            if not _in_range(period, start_date, end_date):
                continue
            points.append(DataPoint(
                indicator=indicator, value=float(rec["pe_ttm"]),
                period_date=period, extra=dict(extra),
                source_name="中证指数官网(ETF估值代理)", source_url=url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.WEB_CRAWL,
                confidence=0.7, verified=False))
        if not points:
            raise DataFetchError(
                f"关联指数{idx_name}({idx_code})PE序列在请求区间内无数据")
        return points

    @staticmethod
    def _sina_symbol(code: str) -> str:
        """A股代码→新浪带市场前缀符号：6/9开头沪市，其余按深市。"""
        return symbols.exchange_symbol(code)

    def _stock_dataframe(
        self, ak: Any, code: str, start_date: str | None, end_date: str | None
    ) -> Any:
        """东财主源前复权，失败回退新浪前复权；返回列名统一为中文（日期/收盘…）。

        ## 复权口径（2026-09-22 修复了一处**静默口径错误**）

        原来东财分支调 `ak.stock_zh_a_hist(...)` **不传 `adjust`** ——
        akshare 的该参数默认 `""`（不复权），于是这条"主源"返回的是**不复权**价，
        而**只有回退分支**（新浪）传了 `adjust="qfq"`。后果是同一只票的日线
        会随"走主源还是走回退"而变口径，且完全没有痕迹：
        分红除权日会凭空多出一个向下跳空缺口，K线形态/缠论笔/回测收益率全被污染。
        项目其它日线源（腾讯 fqkline / Tushare pro_bar / baostock adjustflag=2）
        都是前复权，这条链必须对齐。

        现在两个分支都请求 `qfq`；另外把实际生效的复权口径回写到每一行
        （见 `_fetch_sync` 与 `_annotate_adjust`），让"链上换源"可被事后核对。
        """
        try:
            return ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date or "",
                end_date=end_date or "",
                adjust="qfq",
            )
        except TypeError as exc:
            # 老版本 akshare 的 `stock_zh_a_hist` 没有 `adjust` 形参。
            # ⚠️ 这里**必须精确判定**：TypeError 也可能来自帧/参数处理里的别的
            # 缺陷（实测：测试替身的签名不匹配就会走到这里）。宽泛地吞掉它会让
            # 真正的异常被改写成"接口不支持 adjust"的告警，然后第二个 try 里
            # 的同一个异常又冒出来 —— 现场只剩下一条误导性的日志。
            # 只认"unexpected keyword argument 'adjust'" 这一种。
            if not _is_missing_adjust_param(exc):
                raise
            # 宁可如实标注不复权，也不能静默当成前复权。
            logger.warning(
                "东财行情接口不支持 adjust 形参（%s），本次按**不复权**取数并标注: %s",
                code, brief(exc, BRIEF_TIGHT))
            try:
                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date or "",
                    end_date=end_date or "",
                )
            except Exception as retry_exc:  # noqa: BLE001 连降级调用也失败 → 走新浪
                # ⚠️ 这一层**必须**有：只把"不支持 adjust"当成一条可恢复的告警、
                # 却不兜住降级调用本身的失败，会让异常直接穿出 `_stock_dataframe`——
                # 于是"东财挂了应该回退新浪"这条容灾路径在最需要它的时候失效。
                logger.warning("东财行情接口失败（%s），回退新浪源: %s", code, retry_exc)
                return self._sina_daily_frame(
                    ak, code, start_date, end_date)
            df = df.copy()
            df[ADJUST_COLUMN] = "none"
            return df
        except Exception as exc:  # noqa: BLE001 源故障回退（实测东财偶发RemoteDisconnected）
            logger.warning("东财行情接口失败（%s），回退新浪源: %s", code, exc)
            return self._sina_daily_frame(ak, code, start_date, end_date)

    def _sina_daily_frame(
        self, ak: Any, code: str, start_date: str | None, end_date: str | None
    ) -> Any:
        """新浪前复权日线（东财不可用时的回退通道）。

        抽成独立方法是因为它现在有**两个**调用点（首次失败、降级调用再失败），
        复制两份区间过滤逻辑正是本项目"两处口径漂移"类缺陷的温床 ——
        下面那段掩码写法踩过坑，见注释。
        """
        df = ak.stock_zh_a_daily(symbol=self._sina_symbol(code), adjust="qfq")
        df = df.rename(columns={"date": "日期", "close": "收盘"})
        if start_date or end_date:
            # ⚠️ 两个区间条件必须合成**一个**布尔掩码（实测踩坑 2026-09-22）：
            # 新浪返回的是全历史表（600036 共 5867 行）。若先 `df = df[dates >= 起]`
            # 再 `df = df[dates <= 止]`，第二个掩码仍带着**原表**的 RangeIndex，
            # pandas 会把它 reindex 到已过滤的小表上（UserWarning:
            # "Boolean Series key will be reindexed to match DataFrame index"），
            # 未对齐的位置一律按 False 处理 —— 实测把长区间查询截断成
            # 2020-01-02~2025-12-31（1455 行，且**静默**丢掉 2026 全年），
            # 结果就是日K面板整整少一年数据却毫无报错。
            dates = df["日期"].astype(str).str.replace("-", "")
            # 用 numpy 数组（**不带索引**）做掩码：pandas 不会对 ndarray 触发
            # reindex，两个条件因此可以安全地逐次相与。
            mask = np.ones(len(df), dtype=bool)
            if start_date:
                mask &= (dates >= start_date.replace("-", "")).to_numpy()
            if end_date:
                mask &= (dates <= end_date.replace("-", "")).to_numpy()
            df = df[mask]
        # 新浪成交量单位是股 → 统一折成手（与东财/腾讯/Tushare 口径一致）
        return _sina_volume_to_lots(df)

    def _fetch_sync(
        self, indicator: str, start_date: str | None, end_date: str | None
    ) -> list[DataPoint]:
        """同步取数（线程池执行）：扩展指标直接出点，其余走DataFrame通用转换。

        行情三件套（`stock/index/etf_close`）出来后统一补一个 `ADJUST_COLUMN`
        口径标签（见下）。`adj_factor` 那种按日查表的口径不适合塞进每行的
        `extra`（会误导下游以为"每行复权系数不同"），所以这里只在**序列型**
        日线上标注。
        """
        try:
            import akshare as ak
        except ImportError as exc:
            raise DataFetchError("akshare未安装，请执行: uv sync --extra data") from exc
        extra = self._load_extra_points(ak, indicator, start_date, end_date)
        if extra is not None:
            return extra
        df = self._load_dataframe(indicator, start_date, end_date)
        points = df_to_data_points(df, indicator, self.source_name, self.source_url)
        if indicator.startswith(QUOTE_PREFIXES):
            self._annotate_adjust(points, indicator, df)
        return points

    @staticmethod
    def _annotate_adjust(points: list[DataPoint], indicator: str,
                         df: Any) -> None:
        """给行情点补 `ADJUST_COLUMN` 复权口径标签（与腾讯/baostock 同口径）。

        口径来源优先级：
          1. 帧里已经带了 `ADJUST_COLUMN` —— 那是**实际请求**的口径
             （例如东财分支降级成不复权时写进来的 `none`），**必须采信它**；
          2. 否则按指标前缀推断：个股/ETF 走 `qfq`，指数不除权走 `none`。

        为什么要标：本项目历史上出过"链上换源导致复权口径悄悄变了"的问题
        （东财分支漏传 `adjust` 就是其中一例）。标出来才能让"K线上多出的
        除权跳空"这类现象被定位到具体某一跳，而不是靠猜。
        """
        fallback = ("none" if indicator.startswith("index_close:") else "qfq")
        frame_has = (df is not None and hasattr(df, "columns")
                     and ADJUST_COLUMN in [str(c) for c in df.columns])
        values = ([str(v) for v in df[ADJUST_COLUMN].tolist()] if frame_has else [])
        for index, point in enumerate(points):
            if not isinstance(point.extra, dict):
                continue
            point.extra[ADJUST_COLUMN] = (
                values[index] if index < len(values) else fallback)

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        return await asyncio.to_thread(self._fetch_sync, indicator, start_date, end_date)
