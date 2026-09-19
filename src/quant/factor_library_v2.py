"""35 个核心因子（七大类）—— 全部基于面板、PIT 安全、无未来函数。

设计文档 §3.3 列的 35 个因子在这里做全：

| 类别 | 数量 | 因子 |
|------|-----:|------|
| 价值 | 6 | EP、BP、SP、CFP、股息率、PEG |
| 成长 | 5 | 营收增速、利润增速、ROE增速、毛利率同比变化、经营现金流增速 |
| 质量 | 6 | ROE、ROA、毛利率、净利率、资产负债率(反向)、经营现金流/净利润 |
| 动量 | 5 | 20/60/120日动量、相对强度RS、5日反转(反向) |
| 波动 | 4 | 20日波动、60日波动、ATR、下行波动 |
| 流动性 | 5 | 换手率、成交额、Amihud非流动性、量比、资金净流入率 |
| 规模 | 4 | 总市值、流通市值、对数市值、自由流通市值 |

三条不可让步的规则（每一条都有对应用例）：

1. **方向统一**：所有因子都调成"**值越大越看好**"（`direction` 记原始方向），
   否则 IC 的正负号会在因子之间串味；
2. **只用过去**：滚动窗口一律 `rolling(...)`（不含未来）；财务走 PIT（按公告日）；
3. **不做前向填充**：缺数据就是 NaN，交给后续去极值/标准化/中性化处理。

因子值单位：金额类为元，比率为百分数（18.5 = 18.5%），市值为元。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.quant.panels import FactorPanels

logger = logging.getLogger(__name__)

_EPS = 1e-12


# ==================================================================
# 注册表
# ==================================================================


@dataclass(frozen=True)
class FactorSpec:
    """因子定义：键名、中文标签、类别、方向、公式说明、计算函数。"""

    key: str
    label: str
    category: str
    direction: int          # +1 越大越好；-1 原始值越大越差（计算时已取反）
    formula: str
    func: Callable[[FactorPanels], pd.DataFrame]

    def as_dict(self) -> dict[str, object]:
        return {"key": self.key, "label": self.label, "category": self.category,
                "direction": self.direction, "formula": self.formula}


FACTORS: dict[str, FactorSpec] = {}
CATEGORY_LABELS = {
    "value": "价值", "growth": "成长", "quality": "质量", "momentum": "动量",
    "volatility": "波动率", "liquidity": "流动性", "size": "规模",
}


def _register(key: str, label: str, category: str, direction: int, formula: str):
    def deco(func: Callable[[FactorPanels], pd.DataFrame]) -> Callable:
        if key in FACTORS:
            raise ValueError(f"因子重复注册：{key}")
        FACTORS[key] = FactorSpec(key=key, label=label, category=category,
                                  direction=direction, formula=formula, func=func)
        return func
    return deco


def list_factor_specs() -> list[FactorSpec]:
    return [FACTORS[key] for key in sorted(FACTORS)]


def factors_by_category() -> dict[str, list[FactorSpec]]:
    grouped: dict[str, list[FactorSpec]] = {name: [] for name in CATEGORY_LABELS}
    for spec in list_factor_specs():
        grouped.setdefault(spec.category, []).append(spec)
    return grouped


# ==================================================================
# 通用工具
# ==================================================================


def _sub(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    return a.sub(b, fill_value=np.nan)


def _safe_div(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """除法，分母接近 0 或为负（视调用处）时返回 NaN 而不是 inf。"""
    result = a.div(b.replace(0.0, np.nan))
    return result.replace([np.inf, -np.inf], np.nan)


def _rolling_mean(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return frame.rolling(window, min_periods=max(2, window // 2)).mean()


def _returns(close: pd.DataFrame, window: int) -> pd.DataFrame:
    """过去 window 个交易日的收益率（**只用到 t 及之前**）。"""
    return close / close.shift(window) - 1.0


def _daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change().replace([np.inf, -np.inf], np.nan)


def _upside(frame: pd.DataFrame) -> pd.DataFrame:
    """截断到非负（用于指数计算）。"""
    return frame.clip(lower=0.0)


# ==================================================================
# 价值（6）
# ==================================================================


@_register("ep", "盈利收益率 EP", "value", 1, "净利润/总市值 = 1/PE_TTM")
def factor_ep(panels: FactorPanels) -> pd.DataFrame:
    pe_ttm = panels.basic("pe_ttm")
    ep = _safe_div(pd.DataFrame(1.0, index=pe_ttm.index, columns=pe_ttm.columns),
                   pe_ttm.where(pe_ttm > 0))
    return ep


@_register("bp", "账面市值比 BP", "value", 1, "净资产/总市值 = 1/PB")
def factor_bp(panels: FactorPanels) -> pd.DataFrame:
    pb = panels.basic("pb")
    return _safe_div(pd.DataFrame(1.0, index=pb.index, columns=pb.columns),
                     pb.where(pb > 0))


@_register("sp", "营收市值比 SP", "value", 1, "营业收入/总市值 = 1/PS_TTM")
def factor_sp(panels: FactorPanels) -> pd.DataFrame:
    ps_ttm = panels.basic("ps_ttm")
    return _safe_div(pd.DataFrame(1.0, index=ps_ttm.index, columns=ps_ttm.columns),
                     ps_ttm.where(ps_ttm > 0))


@_register("cfp", "现金流市值比 CFP", "value", 1, "企业自由现金流/总市值")
def factor_cfp(panels: FactorPanels) -> pd.DataFrame:
    fcff = panels.fundamental_field("fcff")
    total_mv = panels.basic("total_mv")
    return _safe_div(fcff, total_mv.where(total_mv > 0))


@_register("dividend_yield", "股息率", "value", 1, "近12个月股息率（%）")
def factor_dividend_yield(panels: FactorPanels) -> pd.DataFrame:
    return panels.basic("dv_ttm")


@_register("peg", "PEG", "value", -1, "PE_TTM / 净利润同比增速（越小越便宜）")
def factor_peg(panels: FactorPanels) -> pd.DataFrame:
    pe_ttm = panels.basic("pe_ttm")
    growth = panels.fundamental_field("netprofit_yoy")
    # 增速必须为正才有意义（负增长时 PEG 无解释力，直接置 NaN 而不是造出负值）
    peg = _safe_div(pe_ttm.where(pe_ttm > 0), growth.where(growth > 0))
    return (-peg)          # direction=-1：PEG 越小越好 → 取反统一方向


# ==================================================================
# 成长（5）
# ==================================================================


@_register("revenue_growth", "营收增速", "growth", 1, "营业总收入同比增速（%）")
def factor_revenue_growth(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("tr_yoy")


@_register("profit_growth", "利润增速", "growth", 1, "归母净利润同比增速（%）")
def factor_profit_growth(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("netprofit_yoy")


@_register("roe_growth", "ROE增速", "growth", 1, "ROE 同比增速（%）")
def factor_roe_growth(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("roe_yoy")


@_register("gross_margin_trend", "毛利率同比变化", "growth", 1,
           "本期毛利率 − 去年同期毛利率（pp）")
def factor_gross_margin_trend(panels: FactorPanels) -> pd.DataFrame:
    current = panels.fundamental_field("grossprofit_margin")
    previous = _year_ago(panels, "grossprofit_margin")
    return _sub(current, previous)


@_register("ocf_growth", "经营现金流增速", "growth", 1,
           "经营活动现金流净额同比增速（%）")
def factor_ocf_growth(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("ocf_yoy")


def _year_ago(panels: FactorPanels, metric: str) -> pd.DataFrame:
    """同一 metric 的「去年同期」面板（按报告期对齐，年报对年报）。

    实现：把该字段按 **真实报告期** 建索引；对每个交易日取出当时可见的报告期 P，
    查 `P - 1年` 那条记录。全程只用已公告数据。

    踩过的坑：早先把索引建在"P-1年"这个标签上，等于查 P-1年 时拿到的是 P 自己的值，
    于是"毛利率同比变化"恒等于 0（比 NaN 更难发现）。
    """
    panel = panels.fundamentals
    if panel is None or metric not in panel.metrics:
        return pd.DataFrame(index=panels.dates, columns=panels.codes, dtype="float64")
    records = panel.records[["code", "report_period", metric]].dropna()
    if records.empty:
        return pd.DataFrame(index=panels.dates, columns=panels.codes, dtype="float64")
    # 修正公告会让同一 (code, report_period) 有多条记录 → 建索引前必须去重，
    # 否则 reindex 直接抛 "cannot handle a non-unique multi-index!"（实测踩过，
    # 该因子整列变 NaN）。保留可用日最晚的那条，与 PitPanel 取值口径一致。
    if "usable_date" in panel.records.columns:
        records = (panel.records[["code", "report_period", metric, "usable_date"]]
                   .dropna(subset=[metric])
                   .sort_values(["code", "report_period", "usable_date"])
                   .drop_duplicates(subset=["code", "report_period"], keep="last"))
    lookup = records.set_index(["code", "report_period"])[metric]

    rows: dict[str, pd.Series] = {}
    for date in panels.dates:
        snapshot = panel.as_of_records(date)      # 需要 report_period，见该方法说明
        if not len(snapshot):
            rows[date] = pd.Series(index=panels.codes, dtype="float64")
            continue
        keys = pd.MultiIndex.from_arrays(
            [snapshot.index, snapshot["report_period"].map(_shift_year)],
            names=["code", "report_period"])
        rows[date] = pd.Series(lookup.reindex(keys).to_numpy(),
                               index=snapshot.index).reindex(panels.codes)
    return pd.DataFrame(rows).T


def _shift_year(period: str) -> str:
    try:
        return f"{int(str(period)[:4]) - 1}{str(period)[4:]}"
    except (TypeError, ValueError):
        return ""


# ==================================================================
# 质量（6）
# ==================================================================


@_register("roe", "净资产收益率 ROE", "quality", 1, "净利润/净资产（%）")
def factor_roe(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("roe")


@_register("roa", "总资产报酬率 ROA", "quality", 1, "净利润/总资产（%）")
def factor_roa(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("roa")


@_register("gross_margin", "毛利率", "quality", 1, "销售毛利率（%）")
def factor_gross_margin(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("grossprofit_margin")


@_register("net_margin", "净利率", "quality", 1, "销售净利率（%）")
def factor_net_margin(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("netprofit_margin")


@_register("low_leverage", "低资产负债率", "quality", -1,
           "资产负债率（%，越小越好 → 取反）")
def factor_low_leverage(panels: FactorPanels) -> pd.DataFrame:
    debt = panels.fundamental_field("debt_to_assets")
    return -debt


@_register("ocf_to_profit", "经营现金流/净利润", "quality", 1,
           "经营活动现金流净额 / 净利润（现金流质量）")
def factor_ocf_to_profit(panels: FactorPanels) -> pd.DataFrame:
    return panels.fundamental_field("ocf_to_profit")


# ==================================================================
# 动量（5）
# ==================================================================


@_register("momentum_20", "20日动量", "momentum", 1, "过去20个交易日收益率")
def factor_momentum_20(panels: FactorPanels) -> pd.DataFrame:
    return _returns(panels.price("close"), 20)


@_register("momentum_60", "60日动量", "momentum", 1, "过去60个交易日收益率")
def factor_momentum_60(panels: FactorPanels) -> pd.DataFrame:
    return _returns(panels.price("close"), 60)


@_register("momentum_120", "120日动量", "momentum", 1, "过去120个交易日收益率")
def factor_momentum_120(panels: FactorPanels) -> pd.DataFrame:
    return _returns(panels.price("close"), 120)


@_register("relative_strength", "相对强度 RS", "momentum", 1,
           "个股60日收益 − 沪深300同期收益")
def factor_relative_strength(panels: FactorPanels) -> pd.DataFrame:
    stock = _returns(panels.price("close"), 60)
    benchmark = panels.index_returns.get("000300.SH")
    if benchmark is None or len(benchmark) == 0:
        benchmark = panels.index_returns.get("000001.SH")
    if benchmark is None or len(benchmark) == 0:
        return pd.DataFrame(index=panels.dates, columns=panels.codes, dtype="float64")
    bench_returns = (benchmark / benchmark.shift(60) - 1.0).reindex(panels.dates)
    return stock.sub(bench_returns, axis=0)


@_register("reversal_5", "5日反转", "momentum", -1, "过去5日收益率（取反）")
def factor_reversal_5(panels: FactorPanels) -> pd.DataFrame:
    return -_returns(panels.price("close"), 5)


# ==================================================================
# 波动率（4）
# ==================================================================


@_register("volatility_20", "20日波动率", "volatility", -1,
           "过去20日日收益率标准差（年化前后不影响排序，保留日频口径）")
def factor_volatility_20(panels: FactorPanels) -> pd.DataFrame:
    returns = _daily_returns(panels.price("close"))
    return -returns.rolling(20, min_periods=10).std()


@_register("volatility_60", "60日波动率", "volatility", -1,
           "过去60日日收益率标准差")
def factor_volatility_60(panels: FactorPanels) -> pd.DataFrame:
    returns = _daily_returns(panels.price("close"))
    return -returns.rolling(60, min_periods=30).std()


@_register("atr_20", "20日ATR", "volatility", -1,
           "过去20日平均真实波幅 / 收盘价（越低越好 → 取反）")
def factor_atr_20(panels: FactorPanels) -> pd.DataFrame:
    high, low, close = panels.price("high"), panels.price("low"), panels.price("close")
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ]).groupby(level=0).max()
    atr = true_range.rolling(20, min_periods=10).mean()
    return -_safe_div(atr, close)


@_register("downside_vol_20", "20日下行波动率", "volatility", -1,
           "仅负收益的20日标准差（越低越好 → 取反）")
def factor_downside_vol_20(panels: FactorPanels) -> pd.DataFrame:
    returns = _daily_returns(panels.price("close"))
    downside = returns.where(returns < 0)
    return -downside.rolling(20, min_periods=5).std()


# ==================================================================
# 流动性（5）
# ==================================================================


@_register("turnover_rate", "换手率", "liquidity", 1, "日换手率（%）")
def factor_turnover_rate(panels: FactorPanels) -> pd.DataFrame:
    return panels.basic("turnover_rate")


@_register("amount", "成交额", "liquidity", 1, "日成交额（元）")
def factor_amount(panels: FactorPanels) -> pd.DataFrame:
    return panels.price("amount")


@_register("amihud", "Amihud非流动性", "liquidity", -1,
           "|日收益率| / 成交额（越大越不流动 → 取反，20日均值）")
def factor_amihud(panels: FactorPanels) -> pd.DataFrame:
    returns = _daily_returns(panels.price("close")).abs()
    amount = panels.price("amount").where(lambda frame: frame > 0)
    illiq = _safe_div(returns, amount) * 1e9      # 缩放仅为可读性，不影响排序
    return -illiq.rolling(20, min_periods=10).mean()


@_register("volume_ratio", "量比", "liquidity", 1, "当日成交量/过去5日均量")
def factor_volume_ratio(panels: FactorPanels) -> pd.DataFrame:
    return panels.basic("volume_ratio")


@_register("money_flow_ratio", "资金净流入率", "liquidity", 1,
           "主力净流入额 / 成交额 × 100（%）")
def factor_money_flow_ratio(panels: FactorPanels) -> pd.DataFrame:
    net = panels.flow("net_mf_amount")
    amount = panels.price("amount").where(lambda frame: frame > 0)
    return _safe_div(net, amount) * 100.0


# ==================================================================
# 规模（4）
# ==================================================================


@_register("total_mv", "总市值", "size", -1, "总市值（元，小市值为正 → 取反）")
def factor_total_mv(panels: FactorPanels) -> pd.DataFrame:
    return -panels.basic("total_mv")


@_register("circ_mv", "流通市值", "size", -1, "流通市值（元，取反）")
def factor_circ_mv(panels: FactorPanels) -> pd.DataFrame:
    return -panels.basic("circ_mv")


@_register("log_mv", "对数市值", "size", -1, "ln(总市值)，取反")
def factor_log_mv(panels: FactorPanels) -> pd.DataFrame:
    total_mv = panels.basic("total_mv").where(lambda frame: frame > 0)
    return -np.log(total_mv)


@_register("free_float_mv", "自由流通市值", "size", -1,
           "自由流通股本 × 收盘价（元），取反")
def factor_free_float_mv(panels: FactorPanels) -> pd.DataFrame:
    free_share = panels.basic("free_share")
    # 必须用**未复权**收盘价：市值 = 股本 × 真实价格，
    # 用后复权价会把市值放大到不可比（复权因子会累积到几十倍）。
    close = panels.price("close_raw")
    if not close.notna().to_numpy().any():       # 面板没提供 close_raw 时退回复权价
        close = panels.price("close")
    return -(free_share * close)


# ==================================================================
# 批量计算
# ==================================================================


def compute_factors(panels: FactorPanels, *,
                    keys: list[str] | None = None,
                    verbose: bool = False) -> dict[str, pd.DataFrame]:
    """计算全部（或指定）因子，返回 {因子键: 面板}。"""
    targets = keys or list(FACTORS)
    unknown = [key for key in targets if key not in FACTORS]
    if unknown:
        raise KeyError(f"未注册的因子：{unknown}（已注册 {len(FACTORS)} 个）")
    output: dict[str, pd.DataFrame] = {}
    for key in targets:
        spec = FACTORS[key]
        try:
            frame = spec.func(panels)
        except Exception as exc:  # noqa: BLE001 单个因子失败不该拖垮整批
            logger.warning("因子 %s 计算失败：%s", key, brief(exc, BRIEF_DEFAULT))
            frame = pd.DataFrame(index=panels.dates, columns=panels.codes,
                                 dtype="float64")
        if frame is None or frame.empty:
            frame = pd.DataFrame(index=panels.dates, columns=panels.codes,
                                 dtype="float64")
        frame = frame.reindex(index=panels.dates, columns=panels.codes)
        output[key] = frame
        if verbose:
            coverage = float(frame.notna().to_numpy().mean()) if frame.size else 0.0
            logger.info("  因子 %-20s 覆盖率 %.1f%%", key, coverage * 100)
    return output


def cross_section(factor_panel: pd.DataFrame, date: str) -> pd.Series:
    """取某个交易日的因子截面（index=code），用于条件选股。"""
    if date not in factor_panel.index:
        return pd.Series(dtype="float64")
    return factor_panel.loc[date].dropna()


def factor_coverage_report(factors: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """覆盖率报表：每个因子在最后一天的覆盖只数 + 全样本非空比例。

    覆盖率是因子可用性的第一道体检：低于 60% 的因子在筛选前就该被注意到。
    """
    rows = []
    for key, frame in factors.items():
        spec = FACTORS.get(key)
        if frame is None or frame.empty:
            rows.append({"factor": key, "label": spec.label if spec else "",
                         "category": spec.category if spec else "",
                         "overall": 0.0, "latest_date": "", "latest_codes": 0})
            continue
        latest_date = str(frame.index[-1])
        latest = frame.loc[latest_date]
        rows.append({
            "factor": key,
            "label": spec.label if spec else "",
            "category": spec.category if spec else "",
            "overall": round(float(frame.notna().to_numpy().mean()), 4),
            "latest_date": latest_date,
            "latest_codes": int(latest.notna().sum()),
        })
    return pd.DataFrame(rows).sort_values(["category", "factor"]).reset_index(drop=True)
