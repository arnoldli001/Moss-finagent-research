"""因子基类、注册器、预处理（去极值 / 标准化 / 中性化）。

因子计算输入约定：
- prices: pd.DataFrame(index=date, columns=code) — 日频收盘价
- fundamentals: dict[code, dict] 或 pd.DataFrame — 每个标的的 PE/PB/市值等估值指标

预处理三步（业界标准）：
1. 去极值：MAD（中位数绝对偏差，比 3σ 对肥尾更鲁棒）
2. 标准化：z-score → 截面均值 0、标准差 1
3. 中性化：对 ln(市值) + 行业哑变量做截面回归取残差

使用：
    from src.quant.factor_base import register_factor, Factor
    @register_factor("pe_ttm")
    class PEFactor(Factor):
        def calculate(self, prices, fundamentals, **kw):
            ...
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# ============== 因子注册器 ==============

_FACTORY: dict[str, type[Factor]] = {}


def register_factor(name: str):
    """装饰器：注册一个因子实现。"""
    def deco(cls: type[Factor]):
        cls.factor_name = name
        _FACTORY[name] = cls
        return cls
    return deco


def get_factor(name: str) -> type[Factor] | None:
    return _FACTORY.get(name)


def list_factors() -> list[str]:
    return sorted(_FACTORY.keys())


# ============== 因子基类 ==============

@dataclass
class FactorResult:
    """因子计算结果。"""
    name: str
    date: Any                  # 横截面日期（月末）
    values: pd.Series          # index=code, value=factor_score
    metadata: dict[str, Any] = field(default_factory=dict)


class Factor(ABC):
    """因子抽象基类。"""

    factor_name: str = ""
    description: str = ""

    @abstractmethod
    def calculate(
        self,
        prices: pd.DataFrame,
        fundamentals: pd.DataFrame | dict[str, dict] | None = None,
        industry_map: dict[str, str] | None = None,
        **kwargs,
    ) -> FactorResult:
        """计算一个横截面的因子得分。

        prices: index=date, columns=code 的日频收盘价
        fundamentals: index=code, columns=pe_ttm/pb/mv 等的 DataFrame
        industry_map: code → 行业一级分类
        """


# ============== 预处理 ==============

def mad_winsorize(x: pd.Series, n: float = 5.0) -> pd.Series:
    """MAD 去极值：超出 median ± n * 1.4826 * MAD 的值截断到边界。

    比 3σ 更鲁棒（股票因子分布通常有厚尾）。
    1.4826 是正态分布 MAD→σ 的换算系数。
    """
    if len(x) < 3:
        return x
    med = x.median()
    mad = np.abs(x - med).median()
    if mad == 0 or np.isnan(mad):
        return x
    sigma = 1.4826 * mad
    return x.clip(med - n * sigma, med + n * sigma)


def zscore(x: pd.Series) -> pd.Series:
    """截面标准化为 N(0,1)。"""
    std = x.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=x.index)
    return (x - x.mean()) / std


def neutralize(
    factor: pd.Series,
    mv: pd.Series | None = None,
    industry_map: dict[str, str] | None = None,
) -> pd.Series:
    """截面回归取残差，消除市值 + 行业暴露。

    行业用 one-hot 哑变量（k-1 列避免共线性）。
    """
    codes = factor.index
    X_parts: list[np.ndarray] = []
    col_names: list[str] = []
    if mv is not None:
        mv_aligned = mv.reindex(codes)
        mv_aligned = np.log(mv_aligned.where(mv_aligned > 0, np.nan))
        if mv_aligned.notna().mean() > 0.3:
            X_parts.append(mv_aligned.values.reshape(-1, 1))
            col_names.append("ln_mv")
    if industry_map:
        industries = pd.Series(
            [industry_map.get(c, "未知") for c in codes], index=codes)
        dummies = pd.get_dummies(industries, drop_first=True, dtype=float)
        X_parts.append(dummies.values)
        col_names.extend(dummies.columns.tolist())

    if not X_parts:
        return factor  # 没有中性化维度，原样返回

    X = np.hstack(X_parts)
    # 加截距列（OLS 必须有，否则过原点拟合会把常数项吃掉，
    # 导致 beta 被污染 → residual 和原自变量高度相关）
    X = np.column_stack([np.ones(X.shape[0]), X])
    col_names = ["_intercept"] + col_names
    y = factor.values.astype(float)
    mask = ~np.isnan(y) & ~np.isnan(X).any(axis=1)
    if mask.sum() < len(col_names) + 5:
        return factor  # 样本太少，不做中性化
    X_valid, y_valid = X[mask], y[mask]
    # 最小二乘（正常情况下截面不会多重共线性爆炸）
    beta, *_ = np.linalg.lstsq(X_valid, y_valid, rcond=None)
    residual = np.full_like(y, np.nan)
    residual[mask] = y_valid - X_valid @ beta
    return pd.Series(residual, index=codes, name=factor.name)


def preprocess(
    factor: pd.Series,
    *,
    do_winsorize: bool = True,
    do_standardize: bool = True,
    do_neutralize: bool = True,
    mv: pd.Series | None = None,
    industry_map: dict[str, str] | None = None,
) -> pd.Series:
    """一键三步预处理。"""
    out = factor
    if do_winsorize:
        out = mad_winsorize(out)
    if do_neutralize:
        out = neutralize(out, mv=mv, industry_map=industry_map)
    if do_standardize:
        out = zscore(out)
    return out
