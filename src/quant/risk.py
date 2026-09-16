"""量化风控模块：VaR / CVaR / 压力测试。

设计原则：
- 历史模拟法 VaR（参数法假设正态太乐观）
- 压力测试用真实历史极端场景（2015股灾/2020新冠/UBS瑞信/2024美联储加息）
- 纯数值计算，输入为日频收益率 DataFrame / 组合权重
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

# ============== VaR ==============

def historical_var(
    returns: pd.Series | np.ndarray,
    *,
    confidence: float = 0.95,
    horizon_days: int = 1,
) -> float:
    """历史模拟法 VaR。

    返回单日在置信度下的最大潜在损失（正数表示 loss）。
    horizon_days > 1 时用 square-root-of-time 缩放（假设收益率独立同分布）。
    """
    r = np.asarray(returns, dtype=float).flatten()
    r = r[~np.isnan(r)]
    if len(r) < 50:
        raise ValueError(f"收益率样本太少（{len(r)}），历史 VaR 至少需要 50 天")
    # 单日 VaR 是下分位数的相反数（loss 视角）
    q = np.quantile(r, 1 - confidence)  # 左尾分位数
    daily_var = -q                       # 转为正数表示 loss
    if horizon_days > 1:
        daily_var = daily_var * np.sqrt(horizon_days)
    return float(daily_var)


def historical_cvar(
    returns: pd.Series | np.ndarray,
    *,
    confidence: float = 0.95,
    horizon_days: int = 1,
) -> float:
    """条件 VaR（Expected Shortfall）：左尾均值的相反数。

    CVaR 比 VaR 更coherent（满足次可加性），Basel III 推荐指标。
    """
    r = np.asarray(returns, dtype=float).flatten()
    r = r[~np.isnan(r)]
    if len(r) < 50:
        raise ValueError(f"样本太少（{len(r)}）")
    threshold = np.quantile(r, 1 - confidence)
    tail = r[r <= threshold]
    cvar = -tail.mean() if len(tail) > 0 else 0.0
    if horizon_days > 1:
        cvar = cvar * np.sqrt(horizon_days)
    return float(cvar)


def portfolio_var(
    weights: dict[str, float],
    returns: pd.DataFrame,          # index=date, columns=code
    *,
    confidence: float = 0.95,
    horizon_days: int = 1,
) -> dict[str, float]:
    """组合 VaR：先算组合每日收益（加权和），再算 VaR/CVaR。

    返回 dict: {var, cvar, n_observations}
    """
    w = pd.Series(weights)
    w = w / w.sum()  # 归一化
    port_ret = (returns.reindex(columns=w.index) * w).sum(axis=1).dropna()
    if len(port_ret) < 50:
        return {"var": float("nan"), "cvar": float("nan"),
                "n_observations": len(port_ret)}
    return {
        "var": historical_var(port_ret, confidence=confidence,
                              horizon_days=horizon_days),
        "cvar": historical_cvar(port_ret, confidence=confidence,
                                horizon_days=horizon_days),
        "n_observations": len(port_ret),
    }


@dataclass
class VaRReport:
    confidence: float
    var_1d: float
    var_10d: float
    cvar_1d: float
    cvar_10d: float
    # 对比：历史上有多少次单日亏损超过 VaR_1d（理论上 = (1-confidence) * n_obs）
    breaches: int
    expected_breaches: float
    n_obs: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "confidence": self.confidence,
            "VaR_1d": round(self.var_1d, 4),
            "VaR_10d": round(self.var_10d, 4),
            "CVaR_1d": round(self.cvar_1d, 4),
            "CVaR_10d": round(self.cvar_10d, 4),
            "breaches": self.breaches,
            "expected_breaches": round(self.expected_breaches, 2),
            "n_obs": self.n_obs,
        }


def full_var_report(returns: pd.Series | np.ndarray, *, confidence: float = 0.95) -> VaRReport:
    """一键 VaR 报告：1d + 10d  + 期望违反次数对比。"""
    if isinstance(returns, pd.Series):
        r = returns.dropna()
    else:
        r = pd.Series(np.asarray(returns).flatten())
    var1 = historical_var(r, confidence=confidence, horizon_days=1)
    var10 = historical_var(r, confidence=confidence, horizon_days=10)
    cvar1 = historical_cvar(r, confidence=confidence, horizon_days=1)
    cvar10 = historical_cvar(r, confidence=confidence, horizon_days=10)
    breaches = int((-r > var1).sum())
    expected = (1 - confidence) * len(r)
    return VaRReport(
        confidence=confidence, var_1d=var1, var_10d=var10,
        cvar_1d=cvar1, cvar_10d=cvar10,
        breaches=breaches, expected_breaches=expected, n_obs=len(r),
    )


# ============== 压力测试 ==============

# 真实历史极端场景（日期区间 + 近似指数跌幅）。
# 场景描述取业界共识。`apply_stress` 时会把场景内的实际收益率替换进组合。
_STRESS_SCENARIOS: dict[str, dict[str, Any]] = {
    "2015_股灾": {
        "start": "2015-06-12", "end": "2015-08-26",
        "description": "上证指数 5178→2638，-49%；千股跌停 3 次",
        "index_proxy": "沪深300 同期 -45%",
    },
    "2015_股灾2": {
        "start": "2015-08-26", "end": "2015-09-16",
        "description": "救市资金撤出后二次暴跌，3 天 3 次熔断（历史）",
        "index_proxy": "沪深300 同期 -23%",
    },
    "2020_新冠": {
        "start": "2020-01-20", "end": "2020-03-24",
        "description": "疫情爆发→全球封城，创业板指 1576→2627 先跌后涨但 V 型剧烈",
        "index_proxy": "沪深300 同期 -17%",
    },
    "2024_美联储加息": {
        "start": "2022-03-01", "end": "2022-10-14",
        "description": "美联储 7 次加息 0→4.25%，全球风险资产杀估值",
        "index_proxy": "纳斯达克同期 -33%，沪深300 -21%",
    },
    "UBS_瑞信": {
        "start": "2023-03-10", "end": "2023-03-20",
        "description": "瑞信 AT1 债券清零，全球银行股踩踏",
        "index_proxy": "欧洲银行指数 -28%",
    },
    "2024_A股调整": {
        "start": "2024-01-10", "end": "2024-02-05",
        "description": "印花税下调后反弹失败，上证指数短期跌破 2600",
        "index_proxy": "沪深300 同期 -11%",
    },
}


def apply_stress(
    returns: pd.DataFrame,             # index=date, columns=code, 日频原始收益率
    scenario: str = "2015_股灾",
    *,
    target_codes: list[str] | None = None,
) -> pd.DataFrame:
    """把指定场景区间内的日收益率替换成该区间历史收益率，返回 stress 后的面板。

    核心思想：用历史真实极端行情**直接替换**进当前组合，看如果今天再发生一次会怎样。
    这比假设正态分布靠谱得多——因为极端行情的**尾部相关性**（所有股一起跌）才是真正风险。
    """
    if scenario not in _STRESS_SCENARIOS:
        raise KeyError(f"未知压力场景: {scenario}；支持 {list(_STRESS_SCENARIOS)}")
    cfg = _STRESS_SCENARIOS[scenario]
    mask = (returns.index >= cfg["start"]) & (returns.index <= cfg["end"])
    stressed = returns.copy()
    if target_codes is None:
        stressed.loc[mask] = returns.loc[mask]  # 原样保留该区间的真实收益率
    else:
        # 只替换指定标的（模拟 "如果中际旭创遇到股灾"）
        stressed.loc[mask, target_codes] = returns.loc[mask, target_codes]
    return stressed


def stress_test_portfolio(
    weights: dict[str, float],
    returns: pd.DataFrame,
    *,
    scenarios: list[str] | None = None,
) -> pd.DataFrame:
    """对一个组合跑所有压力场景。

    返回 DataFrame: index=场景, columns=场景描述/期间组合收益。
    """
    scenarios = scenarios or list(_STRESS_SCENARIOS.keys())
    w = pd.Series(weights)
    w = w / w.sum()
    (returns.reindex(columns=w.index) * w).sum(axis=1).dropna()

    rows = []
    for s in scenarios:
        cfg = _STRESS_SCENARIOS[s]
        stressed = apply_stress(returns, s)
        stressed_port = (stressed.reindex(columns=w.index) * w).sum(axis=1).dropna()
        mask = (stressed_port.index >= cfg["start"]) & (stressed_port.index <= cfg["end"])
        scenario_ret = (1 + stressed_port.loc[mask]).prod() - 1 if mask.any() else float("nan")
        rows.append({
            "scenario": s,
            "period": f"{cfg['start']} → {cfg['end']}",
            "description": cfg["description"],
            "scenario_return": round(scenario_ret, 4),
            "index_proxy": cfg["index_proxy"],
        })
    return pd.DataFrame(rows)


def list_stress_scenarios() -> list[dict[str, str]]:
    return [{"name": k, "description": v["description"]}
            for k, v in _STRESS_SCENARIOS.items()]
