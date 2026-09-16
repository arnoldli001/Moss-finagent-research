"""初始因子库：PE(TTM)、动量、短期反转、Growth（营收增长率）。

全部为 @register_factor 装饰，未来加因子只需写一个类。
"""
from __future__ import annotations

import pandas as pd

from .factor_base import Factor, FactorResult, register_factor


@register_factor("pe_ttm")
class PEFactor(Factor):
    """PE(TTM) 估值因子：截面越小越便宜。"""

    description = "PE(TTM) 截面因子，低 PE 为价值股"

    def calculate(self, prices, fundamentals=None, industry_map=None, **kwargs):
        if fundamentals is None:
            raise ValueError("PE 因子需要 fundamentals 传入 PE(TTM) 序列")
        factor_col = _resolve_col(fundamentals, ["pe_ttm", "PE_TTM", "pe"])
        values = _extract_series(fundamentals, factor_col).dropna()
        # PE 越大越贵，取负让小 PE 为高分组
        return FactorResult(
            name=self.factor_name, date=kwargs.get("date"), values=-values.abs().clip(lower=1),
        )


@register_factor("momentum_12m")
class Momentum12mFactor(Factor):
    """12 个月动量（不含最近 1 个月，跳空处理短期反转噪声）。"""

    description = "过去 12 个月收益率（跳过最近 1 个月），强趋势因子"

    def calculate(self, prices, fundamentals=None, industry_map=None, **kwargs):
        horizon = kwargs.get("horizon", 252)     # 12m ≈ 252 交易日
        skip = kwargs.get("skip", 21)             # 跳过最近 1 个月
        if len(prices) < horizon + skip + 1:
            raise ValueError(f"价格序列太短，需要至少 {horizon + skip + 1} 天")
        # 取最后一天作为截面
        start_idx = len(prices) - horizon - skip
        end_idx = len(prices) - skip - 1
        start_prices = prices.iloc[start_idx]
        end_prices = prices.iloc[end_idx]
        ret = (end_prices / start_prices - 1).dropna()
        return FactorResult(
            name=self.factor_name, date=prices.index[-1], values=ret,
        )


@register_factor("reversal_1m")
class Reversal1mFactor(Factor):
    """短期反转：上个月跌得多的本月容易反弹（A股显著存在）。"""

    description = "最近 1 个月收益率取负，捕捉反转"

    def calculate(self, prices, fundamentals=None, industry_map=None, **kwargs):
        horizon = kwargs.get("horizon", 21)
        if len(prices) < horizon + 1:
            raise ValueError(f"价格序列太短，需要至少 {horizon + 1} 天")
        start_prices = prices.iloc[-horizon - 1]
        end_prices = prices.iloc[-1]
        ret = (end_prices / start_prices - 1).dropna()
        return FactorResult(
            name=self.factor_name, date=prices.index[-1], values=-ret,  # 反转：跌得多的得分高
        )


@register_factor("growth_rev_yoy")
class RevGrowthFactor(Factor):
    """营收同比增速（基本面 growth）。"""

    description = "营业收入 YoY 增速截面因子，高成长高分"

    def calculate(self, prices, fundamentals=None, industry_map=None, **kwargs):
        if fundamentals is None:
            raise ValueError("Growth 因子需要 fundamentals 传入 revenue_yoy")
        factor_col = _resolve_col(
            fundamentals, ["revenue_yoy", "rev_yoy", "营收增长"])
        values = _extract_series(fundamentals, factor_col).dropna()
        return FactorResult(
            name=self.factor_name, date=kwargs.get("date"), values=values,
        )


# ============== 小工具 ==============

def _resolve_col(df_or_series, candidates):
    if isinstance(df_or_series, pd.DataFrame):
        for c in candidates:
            for col in df_or_series.columns:
                if str(col).lower() == c.lower():
                    return col
        raise KeyError(f"找不到列，候选: {candidates}；实际: {list(df_or_series.columns)}")
    return candidates[0]


def _extract_series(source, col) -> pd.Series:
    if isinstance(source, pd.DataFrame):
        return source[col]
    if isinstance(source, dict):
        return pd.Series(source)
    return source
