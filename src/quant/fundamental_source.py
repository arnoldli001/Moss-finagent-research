"""基本面数据源（AkShare，免 token）——**只保留能给出公告日的数据源做 PIT**。

实测结论（2026-09-15 逐接口核对列名，明细见 docs/QUANT_M1_DATA_LAYER.md）：

- `ak.stock_yjbb_em(date=报告期)`：东财业绩报表，**有公告日** → PIT 骨架；
- `ak.stock_financial_analysis_indicator`：新浪财务指标 86 列，**无公告日**
  → 只能做字段补充（公告日取自业绩报表）；
- 新浪财务摘要/三大报表、同花顺摘要：均无公告日，备用。

**为什么公告日不可让步**：报告期（如 20260630）与实际公告日（如 20260828）相差 1~4 个月，
按报告期对齐等于 7 月 1 日就读到 8 月底才公布的半年报。实测该报告期在 20260701
**一页都还没公告**（最早 20260716），即：报告期口径会把 5225 只股票的"未来数据"一次性引入。

Tushare 的 `daily_basic`/`fina_indicator`/`moneyflow` 需 2000 积分（全市场财务横截面需
5000 积分的 `fina_indicator_vip`），当前 `.env` 无 `TUSHARE_TOKEN`，故 M1 不依赖它；
将来有 token 时可并列加一个 `TushareSource` 做交叉校验。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 东财业绩报表 → 规范化英文字段（因子层用英文键，避免中文列名在代码里到处传）
YJBB_FIELD_MAP: dict[str, str] = {
    "每股收益": "eps",
    "营业总收入-营业总收入": "revenue",
    "营业总收入-同比增长": "revenue_yoy",
    "净利润-净利润": "net_profit",
    "净利润-同比增长": "net_profit_yoy",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "每股经营现金流量": "ocfps",
    "销售毛利率": "gross_margin",
    "所处行业": "industry",
}

# 新浪财务分析指标 → 规范化字段（质量/成长/偿债类补充）
SINA_INDICATOR_FIELD_MAP: dict[str, str] = {
    "净资产收益率(%)": "roe_sina",
    "加权净资产收益率(%)": "roe_waa_sina",
    "销售毛利率(%)": "gross_margin_sina",
    "销售净利率(%)": "net_margin_sina",
    "总资产净利润率(%)": "roa_sina",
    "资产负债率(%)": "debt_to_assets",
    "主营业务收入增长率(%)": "revenue_yoy_sina",
    "净利润增长率(%)": "net_profit_yoy_sina",
    "总资产增长率(%)": "asset_yoy_sina",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "每股净资产_调整后(元)": "bps_sina",
    "每股经营性现金流(元)": "ocfps_sina",
    "应收账款周转率(次)": "ar_turnover",
    "存货周转率(次)": "inv_turnover",
    "总资产周转率(次)": "asset_turnover",
}


@dataclass
class FetchAttempt:
    """一次取数尝试（供 health/gaps 展示，绝不静默失败）。"""

    source: str
    ok: bool
    rows: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "ok": self.ok, "rows": self.rows,
                "detail": self.detail}


def report_periods(
    start_year: int, end_year: int, *,
    as_of: str | None = None, grace_days: int = 15,
) -> list[str]:
    """报告期序列（YYYYMMDD，按季）：0331/0630/0930/1231。

    默认**剔除尚未披露的报告期**：`grace_days=15` 表示"报告期结束后还要等 15 天"
    （A 股首份季报/半年报通常 T+10~T+20 才出）。实测（2026-09-15）请求 20260930/20261231
    会稳定报 `TypeError: 'NoneType' object is not subscriptable` —— 那是 akshare 拿到
    空响应后的内部错误，属于"数据还没出生"，不该当成取数失败反复重试。
    """
    if end_year < start_year:
        raise ValueError(f"结束年份 {end_year} 早于开始年份 {start_year}")
    periods = [f"{year}{suffix}"
               for year in range(start_year, end_year + 1)
               for suffix in ("0331", "0630", "0930", "1231")]
    today = as_of or datetime.now().strftime("%Y%m%d")
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=int(grace_days))).strftime("%Y%m%d")
    return [period for period in periods if period <= cutoff]


# ==================================================================
# 股票池（universe）：业绩报表里混着新三板，必须过滤
# ==================================================================

# 实测（2026-09-15，报告期 20260630）：接口返回 11449 行，其中
#   沪主板 1702 + 科创板 618 + 深主板 1495 + 创业板 1410 = 5225（沪深A股）
#   4xx/8xx/920 共 6186 行 = 新三板 + 北交所（其中 5798 行连"所处行业"都没有）
# 也就是说**报表里 54% 不是可交易的沪深A股**。不过滤的话，因子截面会被新三板占满，
# 而它们没有可用行情、披露规则也不同，回测结论会完全跑偏。
_CN_PREFIXES = ("sh", "sz")


def market_of(code: Any) -> str:
    """代码 → 市场归类（sh/sz/bse_or_neeq/neeq/fund/other）。

    注意：北交所（43/83/87/88/920）与新三板（400/430/83x/87x…）**前缀高度重叠**，
    仅凭代码无法区分，需要交易所标的名录（M2 用 QMT 的标的名录来锁定）。
    因此这里如实返回 `bse_or_neeq`，不假装能分清。
    """
    text = str(code).strip().zfill(6)
    if text.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh"
    if text.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return "sz"
    if text.startswith("920"):
        return "bse_or_neeq"
    if text.startswith(("43", "83", "87", "88")):
        return "bse_or_neeq"
    if text.startswith(("4", "8")):
        return "neeq"
    if text.startswith(("5", "1", "2", "9")):
        return "fund"
    return "other"


def universe_mask(codes: pd.Series, universe: str = "a_share") -> pd.Series:
    """股票池掩码。`a_share`=沪深A股（默认）；`all`=不过滤。"""
    markets = codes.map(market_of)
    if universe == "a_share":
        return markets.isin(_CN_PREFIXES)
    if universe == "all":
        return pd.Series(True, index=codes.index)
    raise ValueError(f"未知股票池 {universe!r}（可选：a_share / all）")


def filter_universe(frame: pd.DataFrame, *, code_col: str = "code",
                    universe: str = "a_share") -> tuple[pd.DataFrame, dict[str, int]]:
    """按股票池过滤，并返回**每个市场各多少行**（不静默丢数据）。"""
    if frame is None or len(frame) == 0:
        return frame, {}
    markets = frame[code_col].map(market_of)
    counts = markets.value_counts().to_dict()
    mask = universe_mask(frame[code_col], universe)
    return frame[mask].reset_index(drop=True), {str(k): int(v) for k, v in counts.items()}


async def fetch_performance_report(
    period: str, *, timeout: float = 60.0,
) -> tuple[pd.DataFrame, FetchAttempt]:
    """东财业绩报表（**含最新公告日期**）——PIT 面板的骨架数据。

    返回 (原始宽表, 尝试记录)；取数失败抛 DataFetchError 由上层记缺口。
    """

    def _call() -> pd.DataFrame:
        import akshare as ak
        return ak.stock_yjbb_em(date=period)

    try:
        frame = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 akshare 抛的异常种类很多
        detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        logger.warning("业绩报表取数失败(%s): %s", period, detail)
        return pd.DataFrame(), FetchAttempt(
            source="东财业绩报表(akshare)", ok=False, detail=detail)
    if frame is None or len(frame) == 0:
        return pd.DataFrame(), FetchAttempt(
            source="东财业绩报表(akshare)", ok=False, detail=f"{period} 返回空表")
    return frame, FetchAttempt(
        source="东财业绩报表(akshare)", ok=True, rows=len(frame),
        detail=f"报告期 {period}")


async def fetch_stock_indicators(
    code: str, *, start_year: str = "2015", timeout: float = 45.0,
) -> tuple[pd.DataFrame, FetchAttempt]:
    """新浪财务分析指标（逐股，86 列；无公告日，需与业绩报表的公告日配合使用）。"""

    def _call() -> pd.DataFrame:
        import akshare as ak
        return ak.stock_financial_analysis_indicator(
            symbol=code, start_year=start_year)

    try:
        frame = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        logger.warning("新浪财务指标取数失败(%s): %s", code, detail)
        return pd.DataFrame(), FetchAttempt(
            source="新浪财务分析指标(akshare)", ok=False, detail=detail)
    if frame is None or len(frame) == 0:
        return pd.DataFrame(), FetchAttempt(
            source="新浪财务分析指标(akshare)", ok=False, detail=f"{code} 返回空表")
    return frame, FetchAttempt(
        source="新浪财务分析指标(akshare)", ok=True, rows=len(frame), detail=code)


def normalize_yjbb(frame: pd.DataFrame, period: str,
                   *, field_map: dict[str, str] | None = None,
                   code_col: str = "股票代码",
                   ann_col: str = "最新公告日期",
                   name_col: str = "股票简称",
                   universe: str = "a_share") -> pd.DataFrame:
    """东财业绩报表 → 规范化长表（纯函数，便于单测）。

    输出列：code / name / report_period / ann_date / <指标…>；
    - **缺公告日的行直接丢弃**（不能进 PIT 面板）；
    - 默认只保留沪深A股（`universe="a_share"`），把新三板/北交所行剔掉并记日志 ——
      实测这批占报表 54%，混进因子截面会让回测结论完全跑偏。
    """
    from src.quant.pit import normalize_date, to_yyyymmdd

    mapping = field_map or YJBB_FIELD_MAP
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["code", "name", "report_period", "ann_date"])
    if code_col not in frame.columns or ann_col not in frame.columns:
        raise ValueError(
            f"业绩报表缺少必要列：需要 {code_col!r} 与 {ann_col!r}，"
            f"实际列={list(frame.columns)[:12]}")

    out = pd.DataFrame({
        "code": frame[code_col].astype(str).str.strip().str.zfill(6),
        "name": (frame[name_col].astype(str).str.strip()
                 if name_col in frame.columns else ""),
        # 整表同一个报告期 → 必须用标量归一化，直接赋值 Series 会按 index 对齐
        # 变成「第一行有值、其余行 NaN」（实测踩过）。
        "report_period": normalize_date(period),
        "ann_date": to_yyyymmdd(frame[ann_col]),
    })
    for source_col, target in mapping.items():
        if source_col in frame.columns:
            column = frame[source_col]
            if target == "industry":
                out[target] = column.astype(str).str.strip().replace(
                    {"nan": "", "None": ""})
            else:
                out[target] = pd.to_numeric(column, errors="coerce")
    before = len(out)
    out = out[out["ann_date"].notna() & (out["ann_date"] != "")]
    dropped = before - len(out)
    if dropped:
        logger.info("业绩报表 %s：%d 行缺公告日，已排除（不得进入 PIT 面板）",
                    period, dropped)
    out, market_counts = filter_universe(out, universe=universe)
    if universe != "all" and market_counts:
        logger.info("业绩报表 %s：股票池=%s，各市场行数 %s（已剔除新三板/北交所等）",
                    period, universe, market_counts)
    return out.reset_index(drop=True)


def normalize_indicators(frame: pd.DataFrame, code: str,
                         *, field_map: dict[str, str] | None = None,
                         date_col: str = "日期") -> pd.DataFrame:
    """新浪财务分析指标 → 规范化长表（**无公告日**，需上层补 ann_date 后再入面板）。

    输出列：code / report_period / <指标…>。
    """
    from src.quant.pit import to_yyyymmdd

    mapping = field_map or SINA_INDICATOR_FIELD_MAP
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["code", "report_period"])
    if date_col not in frame.columns:
        raise ValueError(f"新浪财务指标缺少 {date_col!r} 列：{list(frame.columns)[:8]}")
    out = pd.DataFrame({
        "code": str(code).zfill(6),
        "report_period": to_yyyymmdd(frame[date_col]),
    })
    for source_col, target in mapping.items():
        if source_col in frame.columns:
            out[target] = pd.to_numeric(frame[source_col], errors="coerce")
    return out[out["report_period"].notna()].reset_index(drop=True)
