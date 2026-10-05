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
import threading
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
from src.core.exceptions import DataFetchError, NoApplicableData
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.null_policy import null_is_legitimate
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

    ⚠️ 返回值**按 period_date 升序**，与统一数据层
    （`DataPointRepository.query_points`："按period_date升序"）以及其余连接器一致。
    AkShare 的宏观接口（如 `macro_china_consumer_goods_retail`）返回的是
    **新→旧**，原样透出会得到一个"降序"的连接器：同一个指标走网络是降序、
    命中本地 DB 短路却是升序 —— 任何依赖顺序的消费方都会时好时坏。
    实测（2026-09-26）：`ind:社会消费品零售总额同比` 返回首=2026-08 / 尾=2008-01。
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
    # 统一升序（见 docstring）：只按 period_date 排，None 期间排在最前不影响数值消费
    points.sort(key=lambda p: p.period_date or "")
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


# ============================================================================
# Module-level stock financial / dividend indicator contracts.
# KEEP THIS OUTSIDE THE CLASS -- on 2026-09-28 this block was inserted
# inside the class body, splitting it in two (ast: unexpected indent)
# and breaking the whole connector import. After structural edits run:
#   python -c "import ast,pathlib;ast.parse(pathlib.Path('src/infrastructure/connectors/akshare_connector.py').read_text(encoding='utf-8'))"
# ============================================================================

_FIN_RATIO_INDICATORS: dict[str, str] = {
    # —— 偿债（100% / 60% 覆盖）——
    "资产负债率": "资产负债率(%)",
    "流动比率": "流动比率",
    "速动比率": "速动比率",
    "产权比率": "产权比率(%)",
    # —— 盈利（100% / 60%）——
    "ROE": "净资产收益率(%)",
    "ROE加权": "加权净资产收益率(%)",
    "ROA": "总资产净利润率(%)",
    "销售净利率": "销售净利率(%)",
    "成本费用利润率": "成本费用利润率(%)",
    # —— 营运（60%）——
    "存货周转率": "存货周转率(次)",
    "应收账款周转率": "应收账款周转率(次)",
    "总资产周转率": "总资产周转率(次)",
    # —— 成长（100% / 60%）——
    "净利润增长率": "净利润增长率(%)",
    "总资产增长率": "总资产增长率(%)",
    "净资产增长率": "净资产增长率(%)",
    "营收增长率": "主营业务收入增长率(%)",
    # —— 每股（100%）——
    "EPS": "摊薄每股收益(元)",
    "EPS加权": "加权每股收益(元)",
    "每股净资产": "每股净资产_调整前(元)",
    "每股经营现金流": "每股经营性现金流(元)",
    "每股未分配利润": "每股未分配利润(元)",
    "每股资本公积": "每股资本公积金(元)",
}

#: ★ 由**两个源合成**的指标（不是简单取列）：指标前缀 → 说明。
#:
#: `股息率` 的算法（这是用户明确问的那个口径）：
#:     股息率(%) = 最近一次实施的每股派息 ÷ 同期收盘价 × 100
#:   · 每股派息 ← `ak.stock_history_dividend_detail`（**28 条历史，实测可达**）
#:   · 收盘价   ← `ak.stock_zh_a_hist`（同一连接器已用于日线）
#:
#: ⚠️ **原来的理由已不成立（`CHG-0059`，2026-09-28）**：这里曾写
#: 「为什么不用 `quant_daily_basic.dv_ratio`：那张表停在 2023-11-10」——
#: 那是**化石副本**的特征。共享行情仓的同名列一直更新到最近交易日
#: （实测 600036 的 `dv_ratio` 4,981 个点、最新 4.9213 @20260928）。
#: 但**本轮刻意不改算法**：自算 vs 直接取列是两种口径（前者跟随最新一次分红、
#: 后者是 Tushare 的 TTM 口径），换口径会让同一个"股息率"数字变形 ——
#: 那是独立决策，需单独评估与回归，不混在"修库来源"这一改里。
#: 若将来要改，判据是：同一 code/日期下两种口径的差值分布 + 分析层是否被告知。
_DERIVED_STOCK_INDICATORS: dict[str, str] = {
    "股息率": "最近实施每股派息 ÷ 同期收盘价（自行计算，非直接取列）",
}

#: 个股财务类指标的前缀全集（连接器 supports 用；由上面两张表派生，**不手写**）。
_FIN_PREFIXES: tuple[str, ...] = tuple(_FIN_RATIO_INDICATORS) + tuple(
    _DERIVED_STOCK_INDICATORS)


def _suggest_columns(target: str, columns: list[str], top: int = 3) -> list[str]:
    """给「列名写错」的场景给出**最相近的候选列名**（排查一步到位）。

    为什么不直接打印前 8 列：源表有 86 列，前 8 列里往往根本没有相关列 ——
    实测报错时打印的样例全是「每股收益(元)」，而要找的是「流动比率」，
    看了等于没看。相似度排序才能让人（和模型）立刻定位。
    """
    import difflib

    scored: list[tuple[float, str]] = []
    for col in columns:
        ratio = difflib.SequenceMatcher(None, target, col).ratio()
        # 共享子串加权：中文列名靠"字面包含"比靠字符序列更靠谱
        if target and (target in col or col in target):
            ratio = max(ratio, 0.9)
        scored.append((ratio, col))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [c for r, c in scored[:top] if r > 0.3]


def _parse_payout_per_share(raw: Any) -> float | None:
    """把分红表里的「派息」列解析成**每股派息（元）**。

    东财/新浪的分红表口径是"**每 10 股派息**"（如 `10.03` 表示 10 派 10.03 元），
    所以必须 ÷10。单位搞错会让股息率差 10 倍 —— 而那个数字看起来完全正常。
    """
    try:
        val = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return val / 10.0


def _dividend_yield_points(
    indicator: str, code: str, dividend_df: Any, price_df: Any,
) -> list[Any]:
    """用「每股派息 ÷ 收盘价」合成股息率序列。

    两端都取**最近可用值**（口径写在 `extra` 里，随数据一起下发 ——
    AGENTS.md："口径与局限随数据一起下发"）。
    """
    from src.core.schemas import DataPoint, FetchMethod

    if dividend_df is None or price_df is None or len(dividend_df) == 0 \
            or len(price_df) == 0:
        return []

    # 每股派息：取最近一条**已实施**的
    payout = None
    payout_date = ""
    try:
        for _, row in dividend_df.iterrows():
            progress = str(row.get("进度", "") or "")
            if "实施" not in progress:
                continue
            p = _parse_payout_per_share(row.get("派息"))
            if p is not None:
                payout = p
                payout_date = str(row.get("公告日期", "") or "")[:10]
                break
    except Exception:  # noqa: BLE001 解析失败按缺口处理
        return []
    if payout is None:
        return []

    close = None
    close_date = ""
    try:
        col = next((c for c in ("收盘", "close", "收盘价") if c in price_df.columns), None)
        date_col = next((c for c in ("日期", "date") if c in price_df.columns), None)
        if col is not None and len(price_df):
            close = float(price_df.iloc[-1][col])
            close_date = str(price_df.iloc[-1][date_col])[:10] if date_col else ""
    except Exception:  # noqa: BLE001
        return []
    if not close or close <= 0:
        return []

    yield_pct = round(payout / close * 100.0, 4)
    return [DataPoint(
        indicator=indicator, value=yield_pct, unit="%",
        period_date=close_date or payout_date,
        extra={
            "dividend_per_share": payout, "close": close,
            "payout_announce_date": payout_date, "close_date": close_date,
            "formula": "最近实施每股派息 / 同期收盘价",
            "note": ("口径：把最近一次已实施的每10股派息 ÷10 得到每股派息，"
                     "再除以最近收盘价；分红频率因公司而异，"
                     "股息率会随股价波动"),
        },
        source_name="东方财富(分红)+腾讯/东财(收盘)",
        source_url="https://data.eastmoney.com/yjfp/",
        fetch_method=FetchMethod.API_CALL, confidence=0.75,
    )]


#: 个股估值类字段：指标前缀 → `quant_daily_basic` 的列名。
#:
#: 这张表解决的是"库里有上千万行、投研链路零命中"那个洞（用户 2026-09-28 报障）：
#: 它含 `dv_ratio`（股息率）/`dv_ttm`/`total_mv`/`turnover_rate`…，
#: 而 `SmartFetcher` 与数据仓储里 `quant_daily_basic` 零命中。
#:
#: ⚠️ **旧注释曾写「只存在于 dev 库 / 停在 2023-11-10 / 生产上永远取不到」，那是错的**
#: （`CHG-0059`）：那是 `data/dev/moss_dev.db` 里一份**化石副本**的特征；
#: 权威副本在**共享行情仓** `data/quant/warehouse.db`，与 dev 副本同 19 列、
#: 且持续更新到最近交易日。**行数与最新日期一律现算，不许写进注释** ——
#: 那个"行数常量"（一万一千八百多万那种写法）曾被抄到 6 处，其中
#: `_DERIVED_STOCK_INDICATORS` 据此决定"不走本地、改走网络自算股息率"，
#: 为一个不成立的前提长期付网络成本。**一律现算**：见
#: `scripts/_probe_quant_columns.py`（一次性）或
#: `src/infrastructure/catalog/data_stores.py` 的存储视图。
_QUANT_COLUMN_INDICATORS: dict[str, str] = {
    "股息率TTM": "dv_ttm",
    "总市值": "total_mv",
    "流通市值": "circ_mv",
    "换手率": "turnover_rate",
    "量比": "volume_ratio",
    "市销率": "ps_ttm",
}

#: 表名**只写这一份**（`CHG-0059` 的那张全A股日频截面表）。
#:
#: 为什么单独立一个常量：本轮 `_quant_column_points` 的空结果分流要**多跑两条
#: 探针查询**，它们问的是同一张表。表名在这里抄成 3 份（主查询 / 探针① / 探针②）
#: 正是本仓库最贵的缺陷形状 —— "同一个 key 写在 3 处，只改一处 ⇒ 静默不一致"。
_QUANT_BASIC_TABLE = "quant_daily_basic"


def _quant_basic_db_path() -> str:
    """`quant_daily_basic` 所在的库 —— **共享行情仓**，不是应用库。

    ## 为什么必须有这个函数（而不是各处自己写路径）

    2026-09-28（`CHG-0059`）实测：同一张表在项目里有**三份**同名副本 ——

    | 位置 | 性质 |
    |---|---|
    | `data/quant/warehouse.db` | **权威**（共享行情仓，dev/pilot 都读它） |
    | `data/dev/moss_dev.db` | **化石副本**（2026-09-23 之前行情仓也认 `MOSS_SQLITE_PATH` 时留下的，停在 2023-11-10） |
    | `data/pilot/moss_pilot.db` | **没有这张表** |

    原先取数读的是 `settings.sqlite_path`（应用库）→ pilot 报 `no such table`、
    dev 读到 2023 年的化石。**"库在哪"必须只有一个答案**，所以收敛到这里，
    由 `WarehouseConfig.from_env()` 解析（与 `day_extras` / 股票名录同源）。
    """
    from src.quant.warehouse import WarehouseConfig

    cfg = WarehouseConfig.from_env()
    if cfg.dialect != "sqlite":
        # 行情仓切到 MySQL 时这条本地直读路径不适用：如实报错，
        # 不要静默回退到应用库（那正是本函数要修掉的缺陷）。
        raise DataFetchError(
            f"quant_daily_basic 本地直读仅支持 SQLite 行情仓，"
            f"当前为 {cfg.dialect}（{cfg.description}）；"
            f"请改走仓储层 load_dataset，或把该指标交给在线源")
    prefix = "sqlite:///"
    url = cfg.url
    if not url.startswith(prefix):
        raise DataFetchError(f"无法解析行情仓 URL：{cfg.description}")
    return url[len(prefix):]


class AkshareConnector(BaseConnector):
    """AkShare连接器：宏观(CPI/PPI/M2/社融)、A股行情、估值、财务比率、部分行业真实指标。

    ## ★ V8 串行闸门（`CHG-0149`，2026-09-30）

    akshare 的部分接口用 `py_mini_racer`（V8）执行反爬 JS，而 **V8 不是线程安全的**。
    本连接器是整条日线链的**公共路径**，`asyncio.to_thread` 会让"并发调用 =
    并发线程进 V8" ⇒ 进程**硬崩**（实测 3/3 复现：退出码 `0x80000003`
    STATUS_BREAKPOINT、**无 Python traceback**、原生栈在 `mini_racer.dll`；
    串行 0/3）。所以所有 `to_thread(self._fetch_sync, …)` 都必须过下面的
    **类级锁** —— 同一时刻只有一个线程进 akshare。

    与既有手段的关系（"同一判断只允许一份实现"）：本项目对 mini_racer 的既有
    隔离是**子进程**（`src/intraday/subproc.py`，用于同花顺板块快照这类**单点**
    接口）。那不适合这里：日线链每个请求都走，起子进程的 ~1 s 固定开销会直接
    压到交互时延上。闸门是同一目标在**热路径**上的实现 —— 只串行化"进 akshare"
    这一小段，网络与后续计算仍可并发。
    """

    #: V8 串行闸门。用 `threading.Lock`（不是 `asyncio.Lock`）：
    #: 它**与事件循环无关**，因此离线脚本/单测里多次 `asyncio.run()` 复用同一个
    #: 模块也安全；`asyncio.Lock` 跨循环复用会踩"绑定到另一个 loop"的坑。
    #: 在**工作线程里**持锁（见 `_fetch_sync_gated`），所以不占事件循环。
    _V8_GATE = threading.Lock()

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
    #: 个股**季度财务比率**：指标前缀 → 新浪财务分析指标表的**列名**。
#:
#: ## 为什么做成表而不是一串 if-elif（2026-09-28 第二十四轮）
#:
#: 原先只实现了两个（资产负债率/流动比率），代码里是一条三元的
#: `col_keyword = "资产负债率" if ... else "流动比率"` ——
#: **再加第三个就得改逻辑**，于是"A11 声称会读 ROE/商誉/应收账款，而采集侧
#: 一个都没实现"活了很多轮没人发现（用户报障："招商银行缺基本面数据，
#: 本地库/腾讯/东财都该有，为什么没找到"）。
#:
#: 现在把"指标名 → 列名"变成**纯数据**。加一个指标 = 加一行，
#: 不改逻辑；并且护栏测试会自动检查「连接器 supports 的每个指标都在
#: indicators.yaml 登记」与「这里写的列名在源表里真实存在」。
#:
#: ⚠️ **只收录实测有覆盖率的列**（`scripts/_probe_financial_columns.py`：
#: 5 只股票 × 多期，非空率见注释）。覆盖率 0% 的列名（如"销售毛利率(%)"、
#: "调整后的每股净资产(元)"）**故意不收** —— 接了也是永远空值，
#: 只会把"数据缺失"从"没实现"伪装成"接口没给"。

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
                # ★ 第二十四轮：财务比率族与 quant 列族（从映射表派生）
                *(f"{p}:{{code}}" for p in _FIN_PREFIXES),
                *(f"{p}:{{code}}" for p in _QUANT_COLUMN_INDICATORS),
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

    #: 本连接器支持的**个股类**指标前缀（带 `:{code}` 后缀）。
    #:
    #: ★ 2026-09-28 第二十四轮：从三张映射表**派生**，不再手写元组。
    #: 手写的那版只有 4 个（PE/PB/资产负债率/流动比率），而 A11 的 prompt
    #: 声称会读 ROE/商誉/应收账款 —— 采集侧一个都没实现，且**没有任何测试
    #: 会发现这个缺口**。改成派生后：映射表加一行，supports 自动跟上。
    _CODE_PREFIXES: tuple[str, ...] = (
        "PE(TTM):", "PB:",
        *(f"{p}:" for p in _FIN_PREFIXES),
        *(f"{p}:" for p in _QUANT_COLUMN_INDICATORS),
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

        # ETF无个股财务报表，禁止误打个股接口产生误导性数据。
        #
        # ★ 这里必须**按语义分两档**（两个闸门、两个 `kind`，不许互换）：
        #   · `_FIN_RATIO_INDICATORS`（流动比率/资产负债率/ROE…）是**个股财务报表口径**
        #     —— ETF 没有资产负债表，该口径对 ETF **根本不存在**（与"银行没有流动比率"
        #     同形）⇒ 语义不适用 `not_applicable`。判定依据是**代码本身是不是 ETF**
        #     （`_is_etf_code`，与网络/源可用性无关），所以这是结构化证据、不是猜语义；
        #   · `_DERIVED_STOCK_INDICATORS`（股息率）与 `_QUANT_COLUMN_INDICATORS`
        #     （市值/换手率/量比/市销率/股息率TTM）**不同**：这些口径对 ETF **客观存在**
        #     （ETF 也有市值、也可能分红），而我们的 `quant_daily_basic` 是**全A股**
        #     截面表、这条合成链也只吃个股分红明细 ⇒ 缺的是"**表/链不收录这个主体**"
        #     ⇒ **覆盖问题** `not_covered`（用户 2026-10-02 裁定 2；`docs/PRD.md`
        #     §33.5 第 2 条点名的正是 `总市值:588170` 这个判点）。
        #     标成 `not_applicable` 是**假的**（口径明明存在）＝多豁免；继续标普通
        #     `DataFetchError` 也不对：那是把"承认覆盖不到、别去补"说成"取数链故障、
        #     去修一条本来就不该收 ETF 的链"，排查方向被带偏。
        #
        # ⚠️ 原文案（`f"ETF({code})无个股{prefix}财务/基本面指标"`）**逐字保留**：
        #   构造函数据它拼标记（`NoApplicableData.__init__` 把对应的 `MARKER_*`
        #   追加在文案后面），既有文案核对依赖这些字。
        # ⚠️ 这两处**不是**"口径存在但我们此源缺数据"那一类（族 A/族 B 内部，
        #   以及 `_quant_column_points` 的两条 fail-closed）：那些一个字都没动。
        if _is_etf_code(code) and prefix in _FIN_RATIO_INDICATORS:
            raise NoApplicableData(
                f"ETF({code})无个股{prefix}财务/基本面指标", kind="not_applicable")
        if _is_etf_code(code) and prefix in (
                *_DERIVED_STOCK_INDICATORS, *_QUANT_COLUMN_INDICATORS):
            raise NoApplicableData(
                f"ETF({code})无个股{prefix}财务/基本面指标", kind="not_covered")

        # ---------- 族 A：由 quant_daily_basic 列直接取（★ 第二十四轮新增）----------
        if prefix in _QUANT_COLUMN_INDICATORS:
            return self._quant_column_points(
                indicator, code, _QUANT_COLUMN_INDICATORS[prefix],
                start_date, end_date)

        # ---------- 族 B：合成指标（股息率 = 每股派息 ÷ 收盘价）----------
        if prefix in _DERIVED_STOCK_INDICATORS:
            if prefix != "股息率":
                raise DataFetchError(f"暂不支持的合成指标: {prefix}")
            div_df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
            px = ak.stock_zh_a_hist(symbol=code, period="daily",
                                    adjust="qfq")
            return _dividend_yield_points(indicator, code, div_df, px)

        # ---------- 族 C：新浪财务分析指标表取列（原来的两个也走这里）----------
        col_keyword = _FIN_RATIO_INDICATORS.get(prefix)
        if col_keyword is None:
            raise DataFetchError(f"未登记的个股财务指标: {prefix}")
        start_year = int(start_date[:4]) if start_date else datetime.now().year - 3
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(start_year))
        if df is None or len(df) == 0:
            # 空帧 ⇒ **一点证据都没有**：既判不出"列名写错"，更判不出"该实体无值"
            # （拿不到任何列，就没有任何东西可核对）。必须 fail-closed 按真失败报：
            # 原实现会让空帧流进下面的"列未命中"分支，报出一句「最相近的列：（无）
            # —— 请在 `_FIN_RATIO_INDICATORS` 里改正列名」，把"源什么都没返回"
            # 说成"我们的列名写错了"，排查方向正好相反。
            # 若不先挡住，空帧还会带着"列存在、一个值都没有"的形状掉进下面的豁免
            # 分支 ⇒ **把源故障豁免成"该口径不适用"**（最危险的方向）。
            raise DataFetchError(
                f"{code} 的财务指标源表为空（{start_year} 起 0 期）——"
                "没有任何列可核对，按**取数失败**处理：先查源可用性，不要改映射表")
        # A) 列名根本不在源表列中 → **我们的契约写错了**（要改映射表）。
        #    这一判必须排在 `series_to_points` **之前**：转换器找不到数值列时会先抛
        #    它自己的「AkShare返回结构异常(缺日期/数值列)」—— 那句既不给最相近的列名、
        #    也不说"改映射表"，于是这段更可执行的诊断成了**死代码**（本轮实测：
        #    本判据第一版就是被它顶掉的，`_suggest_columns` 一次都没跑到过）。
        #    判定用 `_find_col`（与转换侧**同一套**包含匹配）：列被重命名但仍能被
        #    关键词命中的情况照旧可用，不会在这里被误判成"缺列"。
        cols = [str(c) for c in df.columns]
        column = _find_col(cols, col_keyword)
        if column is None:
            near = _suggest_columns(col_keyword, cols)
            raise DataFetchError(
                f"财务指标列未命中：{code} 的 {col_keyword!r} 不在源表列中。"
                f"最相近的列：{near or '（无）'} —— "
                "请在 `_FIN_RATIO_INDICATORS` 里改正列名")
        points = series_to_points(
            df, indicator, date_keywords=("日期",), value_keywords=(col_keyword,),
            start_date=start_date, end_date=end_date,
            extra={"frequency": "quarterly", "source_column": col_keyword},
            confidence=0.8)
        if not points:
            # 空结果必须**分成三类**，否则排查方向完全相反（A 已在上面判掉）：
            #   B) 列在、全表有值，但没有一期落在**这次请求的区间**内 → 区间/时效
            #      问题（放宽区间或更新源），**不是**"该实体无值"
            #   C) 列在、全表一个可转数值都没有 → **语义正确**（如银行资产负债不划分
            #      流动/非流动，「流动比率」对银行必然为空）。当成缺陷去修会白费力气，
            #      还会把"这个口径对银行不适用"这条真信息抹掉。
            #
            # 为什么 B/C 必须分开（而不是"空结果 ⇒ 不适用"）：豁免 = 这条缺口
            # **不再进缺陷清单、也不去补**。"区间没覆盖到"是真缺口，一刀切豁免
            # 等于把真缺口说成"这个口径本来就没有" —— 与用户报障的那类误判互为镜像，
            # 只是方向相反、更难发现。
            #
            # 判据是**证据**（读原帧那列的全表取值），不是文案：`_to_float` 认
            # `"--"`/NaN 为无值（新浪表用它们表示"没披露"）。
            label = next(c for c in df.columns if str(c) == column)
            series = df[label]
            raw = series.tolist() if hasattr(series, "tolist") else None
            numeric = ([] if raw is None
                       else [v for v in raw if _to_float(v) is not None])
            if raw is not None and raw and not numeric:
                # ★ **结构化**结论（`kind`），不只是文本标记：异常经路由器聚合后
                #   类型会丢、`kind` 也会丢，但标记跨层可读；而下游拿 `kind` 判
                #   才不必靠猜文案。文案逐字保留（既有的标记核对依赖它）。
                raise NoApplicableData(
                    f"{code} 的 {col_keyword!r} 在源表中**该实体无值**"
                    f"（已取到 {len(df)} 期）。这通常是**语义正确**而非缺陷 ——"
                    "例如银行资产负债不划分流动/非流动，「流动比率」对银行必然为空。"
                    "请换用适用于该行业的杠杆/资本类口径（资产负债率/产权比率等）。",
                    kind="not_applicable")
            # 走到这里 = 有值但不在区间内（或列内容读不出来）⇒ **没有豁免证据**。
            # 默认落到"真失败"这一档：宁可多报假阳性，也不许把真缺口豁免掉。
            raise DataFetchError(
                f"{code} 的 {col_keyword!r} 没有落在请求区间"
                f"（{start_date}~{end_date}）内的可用值：该列读到 "
                f"{'-' if raw is None else len(raw)} 个单元格 / {len(numeric)} 个可转数值"
                " —— 证据不足以判为口径问题，按**取数失败**处理：放宽区间或查数据源")
        return points

    # ---------------- 族 A：quant_daily_basic 列 ----------------

    def _probe_quant_rows(
        self, con: Any, where: str, params: list[Any], *,
        column: str | None = None,
    ) -> int:
        """跑一条**廉价探针**查询，返回匹配行数；读不出来 ⇒ 抛 `DataFetchError`。

        `column is None` ⇒ 探针①（**不加列过滤、不加区间**）：这只票在这张表里
        到底有没有行；否则 ⇒ 探针②（加 `IS NOT NULL`、**仍不加区间**）：这一列
        对它**历史上**有没有过值。

        为什么探针失败**绝不吞**：探针答的是「这只票到底在不在表里」。把读失败
        当成"不在表里"，就会把**一次数据源故障**说成「该专题未收录本主体（非缺陷）」
        —— 正是本项目最贵的那类错误（真故障被豁免掉，之后没人去修）。
        """
        # 表名**不在这里另写一份字面量**：下面主查询用的就是 `_QUANT_BASIC_TABLE`
        # （本仓库最贵的缺陷形状是"同一个名字写在 3 处，只改一处"）。
        sql = f'SELECT COUNT(*) FROM "{_QUANT_BASIC_TABLE}" WHERE {where}'
        if column is not None:
            sql += f' AND "{column}" IS NOT NULL'
        try:
            out = list(con.execute(sql, params))
        except Exception as exc:  # noqa: BLE001 探针读失败 ⇒ 按取数失败报
            raise DataFetchError(
                f"quant_daily_basic 探针查询不可读（WHERE {where}）：{exc}") from exc
        if len(out) != 1 or len(out[0]) != 1:
            # 聚合查询**必须**恰好返回一个值；形状变了（例如 SQL 被改成非聚合）
            # 就说明它答的不再是"有几行"，此时按第一行硬读会**静默**得出错误的
            # 豁免结论 —— 宁可显式报错。
            raise DataFetchError(
                f"quant_daily_basic 探针查询返回了意外形状：{out[:3]!r}")
        return int(out[0][0])

    def _quant_column_points(
        self, indicator: str, code: str, column: str,
        start_date: str | None, end_date: str | None,
    ) -> list[DataPoint]:
        """从 `quant_daily_basic`（全A股日频截面）取一列。

        ## 为什么需要它（用户 2026-09-28 报障）

        用户问"招商银行缺基本面与股息率数据…本地数据库应该有"。
        该表含 `dv_ratio`(股息率) / `dv_ttm` / `total_mv` / `turnover_rate` …，
        而**投研链路完全不认识它**（`SmartFetcher` 与数据仓储里零命中）
        —— "库里有上千万行，Agent 一条看不到"。

        ## ⚠️ 库来源修正（2026-09-28 · CHG-0059）

        原先这里从 `settings.sqlite_path` 读，即**应用库**（dev / pilot 各自的库），
        而 `quant_daily_basic` 实际在**共享行情仓**里。后果实测：

        | 环境 | 修正前 | 修正后 |
        |---|---|---|
        | pilot | `no such table: quant_daily_basic` | 取到 |
        | dev   | 报"期间内没有数据"（读到的是停在 2023-11-10 的化石副本） | 取到 |

        现在库路径由 `_quant_basic_db_path()` 单点解析（行情仓）。

        ## 诚实边界（随数据一起下发）

        `latest_trade_date` 与 `staleness_days` 仍然写进 `extra`：
        行情仓是**日频定稿**数据（当日 15:00~16:00 后才入库），
        盘中取到的最后一天是上一交易日 —— 调用方要能看出来，而不是当成实时值。

        ## ★ 空结果按**证据**分流（`CHG-0157` §33.5 第 2 条的确切缺口）

        原来一条 SQL 把三种情形折叠成 `rows == []` ⇒ 只能一律报普通
        `DataFetchError`。**能分开的那部分**现在用两次廉价探针分开（**只在
        这条罕见路径上跑**：空结果本来就是要报错的那一支，不进热路径）：

        * 探针①（**不加列过滤、不加区间**）**无行** ⇒ 这张表**不收录该主体**
          ⇒ `NoApplicableData(kind="not_covered")`：承认覆盖不到、**别去补**
          （表里本来就没有它）。这是取数侧自己写下的结论，不是猜语义；
        * 探针① 有行、探针②（加 `IS NOT NULL`、仍不加区间）**该列历史全 NULL**
          ⇒ 分不分得开**只看登记表** `configs/column_null_policy.yaml`（读取器
          `null_policy.py`，用户 2026-10-02 裁定 1）：登记为 `legitimate`
          （如 `dv_ttm` 真没分红）⇒ `NoApplicableData(kind="not_applicable")`；
          **未登记**或登记为 `hole`、或登记表读不到/该条不合格 ⇒ 保持**普通
          `DataFetchError`**（fail-closed：默认豁免 = 多豁免，最危险方向）；
        * 探针① 有行、探针② 有值、只是**请求区间内没有** ⇒ 区间/新鲜度问题，
          保持**普通 `DataFetchError`**：豁免它等于把真缺口说成"本来就没有"。

        ⚠️ 上面两条 `DataFetchError` 的文案里**一个豁免标记都不许出现**：
        `supervisor.NOT_APPLICABLE_MARKERS` / `NOT_COVERED_MARKERS` 是**文本兜底**
        （异常经 `ConnectorRouter` 聚合后类型会丢），文案里混进标记就会把真失败
        豁免掉 —— 上一轮已经踩过这个形状。

        ⚠️ `code` 形状已核对：表里存的是**裸代码**（实测 `code='600036'` 命中
        4983 行、`LIKE '600036.%'` 命中 0 行），与主查询的 `code = ?` 同源，
        所以探针①问的确实是"这只票在不在表里"。
        """
        db_path = _quant_basic_db_path()
        #: 主体条件**只写这一份**：主查询与两条探针问的必须是**同一个主体**，
        #: 抄成两份就会出现"主查询问 600036、探针问别的" ⇒ 分流结论无意义。
        where, params = "code = ?", [code]
        start_key = str(start_date).replace("-", "")[:8] if start_date else None
        end_key = str(end_date).replace("-", "")[:8] if end_date else None

        sql = (f'SELECT trade_date, "{column}" FROM "{_QUANT_BASIC_TABLE}" '
               f'WHERE {where} AND "{column}" IS NOT NULL')
        query_params = list(params)
        if start_key:
            sql += " AND trade_date >= ?"
            query_params.append(start_key)
        if end_key:
            sql += " AND trade_date <= ?"
            query_params.append(end_key)
        sql += " ORDER BY trade_date"

        try:
            import sqlite3

            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = list(con.execute(sql, query_params))
                # 探针**只在这条罕见路径上**跑：主查询非空就直接跳过，
                # 热路径一次额外查询都不加（判据 4 用执行记录钉死这一点）。
                coverage: int | None = None
                ever: int | None = None
                if not rows:
                    coverage = self._probe_quant_rows(con, where, params)
                    if coverage:
                        ever = self._probe_quant_rows(
                            con, where, params, column=column)
            finally:
                con.close()
        except DataFetchError:
            raise                      # 探针自己的结论（含"读不到就不许猜"）原样上传
        except Exception as exc:  # noqa: BLE001 表缺失/库不可读 → 明确报错
            raise DataFetchError(
                f"quant_daily_basic 不可读（{code} 的 {column}）：{exc}") from exc

        if not rows:
            if coverage == 0:
                # 全表都没有这只票 ⇒ **覆盖问题**，而**不是**"口径不存在"：
                # 这两个结论对客户的说法不同（`supervisor.NOT_COVERED_MARKERS`
                # 那段注释写明了为什么必须分开）。
                raise NoApplicableData(
                    f"{_QUANT_BASIC_TABLE} 未收录 {code}"
                    f"（该表是全A股日频截面，按 code 查 0 行）——"
                    f"『表里没有这只票』≠『{indicator} 为 0』；"
                    "这是**覆盖问题**（承认取不到），不是取数链故障，也别去补。",
                    kind="not_covered")
            if coverage and not ever:
                # 有行、但这一列对它**历史上一个非 NULL 都没有**：合法无值
                # 与数据洞同形 —— 分得开分不开**只看登记表**
                # （`configs/column_null_policy.yaml`，读取器 `null_policy.py`）。
                #
                # ★ 用户 2026-10-02 裁定 1 建了这张登记表，于是这一支从"一律分不开"
                #   变成按**登记 + 证据**分流：
                #   · 该列登记为 `legitimate`（如 `dv_ttm`：没分红 ⇒ 股息率没有定义）
                #     ⇒ `NoApplicableData(kind="not_applicable")`：承认"该口径对它没有
                #     值"，别去补；
                #   · 登记为 `hole` 或**未登记** ⇒ 保持普通 `DataFetchError`（fail-closed）。
                #     **默认必须是这一档**：默认豁免 = 多豁免，而多豁免会把真缺口说成
                #     "本来就没有"（最危险方向，比多报假阳性难发现得多）。
                if null_is_legitimate(column):
                    raise NoApplicableData(
                        f"{_QUANT_BASIC_TABLE} 的 {code} 在表内共 {coverage} 行，"
                        f"{column} 列**历史上全为空**：该列已在 "
                        "`configs/column_null_policy.yaml` 登记为**合法无值**"
                        "（NULL 就是该口径的正确取值，不是缺数据）——"
                        "按**口径不适用**处理，别去补，也别当成取数链故障。",
                        kind="not_applicable")
                # 文案里**不许**出现任何豁免标记（否则文本兜底会把数据洞豁免掉）。
                raise DataFetchError(
                    f"{_QUANT_BASIC_TABLE} 的 {code} 在表内共 {coverage} 行，"
                    f"但 {column} 列**历史上全为空**："
                    "『合法无值』与『数据洞』（该补没补）**只能靠登记表分开**，"
                    f"而 {column} **没有**在 `configs/column_null_policy.yaml` 里"
                    "登记为合法无值（或登记表读不到/该条不合格，一律按未登记处理）"
                    "⇒ 故按**取数失败**处理 —— 请先核对该列的数据入库链，"
                    "确属合法无值再到登记表里补一条（附证据），"
                    "不要就地改判为口径不适用")
            if coverage:
                # 有行、该列历史上也有值 ⇒ 缺的是**这次的区间**（或新鲜度）。
                # 这是真缺口，放宽区间/更新源才对，**不是**豁免。
                raise DataFetchError(
                    f"{_QUANT_BASIC_TABLE} 的 {code} 有 {coverage} 行、"
                    f"{column} 列历史上有值，但都不在请求区间"
                    f"（{start_date}~{end_date}）内 —— 按**取数失败**处理："
                    "放宽区间或更新源，不要改判为口径不适用")
            # 探针没跑成 = 不变量被破坏（空结果必跑探针）。**不编结论**：
            # 走下面那条与改动前逐字相同的兜底文案（fail-closed）。
            raise DataFetchError(
                f"{_QUANT_BASIC_TABLE} 里没有 {code} 的 {column} 数据"
                f"（或期间不在 {start_date}~{end_date} 内）")

        latest = str(rows[-1][0])
        # 陈旧度：表停更时所有人都会拿到同一个"最新日"，必须让上游看得见
        stale_days = None
        try:
            from datetime import date as _d

            y, m, d = int(latest[:4]), int(latest[4:6]), int(latest[6:8])
            stale_days = (_d.today() - _d(y, m, d)).days
        except (ValueError, IndexError):
            pass

        return [
            DataPoint(
                indicator=indicator, value=float(v),
                period_date=f"{td[:4]}-{td[4:6]}-{td[6:8]}",
                extra={
                    "column": column, "table": _QUANT_BASIC_TABLE,
                    "latest_trade_date": latest,
                    "staleness_days": stale_days,
                    "staleness_note": (
                        f"⚠️ 最新交易日 {latest} 距今 {stale_days} 天，"
                        "**不可当作当前值**；如需当日值请改用日频在线源"
                        if (stale_days or 0) > 90 else ""),
                },
                source_name="本地 quant_daily_basic(Tushare daily_basic 导入)",
                source_url="https://tushare.pro/document/2?doc_id=32",
                fetch_method=FetchMethod.FILE_READ, confidence=0.6,
            )
            for td, v in rows
        ]

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
        # ★ V8 串行闸门（`CHG-0149`）：**同一时刻只允许一个线程进 akshare**。
        #
        # akshare 的部分接口用 `py_mini_racer`（V8）执行反爬 JS，而 V8 **不是
        # 线程安全的**。本连接器是整条链的公共路径，`asyncio.to_thread` 会让
        # **并发调用 = 并发线程**进 V8 ⇒ 进程硬崩：实测 3/3 复现，
        # 退出码 `0x80000003`(STATUS_BREAKPOINT)、**没有 Python traceback**、
        # 原生栈落在 `py_mini_racer/mini_racer.dll`；串行则 0/3。
        #
        # 与既有手段的关系（"同一判断只允许一份实现"）：本项目对 mini_racer
        # 的既有隔离是**子进程**（`src/intraday/subproc.py`，用于同花顺板块快照
        # 这类**单点**接口）。那不适合这里 —— 日线链是**每个请求都走**的热路径，
        # 每次调用起一个子进程的代价（~1 s 固定开销）会直接压到交互时延上。
        # 闸门是同一目标（**不让两个线程同时进 V8**）在热路径上的实现：
        # 只串行化"进 akshare"这一小段，网络与后续计算仍可并发。
        #
        # 待办：akshare 若把 V8 调用拆到独立进程（上游修复），本闸门可删。
        return await asyncio.to_thread(
            self._fetch_sync_gated, indicator, start_date, end_date)

    def _fetch_sync_gated(self, *args: Any, **kwargs: Any) -> Any:
        """在**工作线程里**持 `_V8_GATE` 再进 akshare（见类 docstring 的闸门说明）。

        锁必须拿在**线程侧**：`to_thread` 之后代码已经在线程池里跑，
        在那里串行化才真正保证"同一时刻只有一个线程在 V8 里"。
        """
        with self._V8_GATE:
            return self._fetch_sync(*args, **kwargs)

