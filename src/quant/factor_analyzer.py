"""因子分析：IC/IR、分层回测、换手率。

标准流程（来自中信建投/国泰君安因子研报）：
1. 每月末计算截面因子得分 → 预处理（去极值/标准化/中性化）
2. 因子得分 vs 未来 N 日收益率 → Spearman 秩相关（Spearman 比 Pearson 对异常值鲁棒）
3. IC 序列的均值 = IC_hat，IC 序列的均值 / IC 序列的标准差 = IR（夏普比等价物）
4. 按得分分 5 组（或 10 组），看 H-L 多空组合的年化收益/夏普/最大回撤

所有函数返回 pd.DataFrame / dict，方便直接塞图表。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .factor_base import Factor, preprocess


def _rankdata(a: np.ndarray) -> np.ndarray:
    """平均并列排名（与 scipy.stats.rankdata 一致）。

    实现说明（这里优化过一次，实测 8.9s → 约 1s）：
    原版用 Python 双层 while 循环找并列区间，一列 4000 个值就要走几千次
    解释器循环；一次筛选有 171 个截面 × 十几个因子 = 2500+ 次调用，
    单这一处就占了整轮 69 秒里的 8.9 秒。

    现在用"排序 + 分段求和"的向量化写法：先 argsort 拿到名次，
    再用 `np.flatnonzero(np.diff(sorted))` 找出并列边界，
    用 `np.add.reduceat` 一次算出每段的平均名次。
    最后用 `np.empty` + 花式赋值把名次摊回原顺序 —— 全程无 Python 级循环。
    """
    values = np.asarray(a, dtype=float)
    n = len(values)
    if n == 0:
        return np.empty(0, dtype=float)
    order = values.argsort(kind="stable")
    sorted_values = values[order]
    # 并列段的起点：值发生变化的位置 + 首个位置
    boundaries = np.flatnonzero(sorted_values[1:] != sorted_values[:-1]) + 1
    starts = np.concatenate(([0], boundaries))
    counts = np.diff(np.concatenate((starts, [n])))
    # 名次从 1 开始，段内取平均
    sums = np.add.reduceat(np.arange(1, n + 1, dtype=float), starts)
    average_ranks = sums / counts
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.repeat(average_ranks, counts)
    return ranks


def _spearmanr(x: np.ndarray, y: np.ndarray) -> float:
    """纯 numpy 版 spearmanr（排名 Pearson 相关），避免 scipy 依赖。"""
    rx = _rankdata(np.asarray(x, dtype=float))
    ry = _rankdata(np.asarray(y, dtype=float))
    # Pearson on ranks
    mx, my = rx.mean(), ry.mean()
    dx, dy = rx - mx, ry - my
    denom = np.sqrt((dx ** 2).sum() * (dy ** 2).sum())
    if denom == 0:
        return float("nan")
    return float((dx * dy).sum() / denom)


# ============== IC / IR ==============

def compute_ic_series(
    factor_scores: pd.DataFrame,      # index=date, columns=code（已预处理）
    forward_returns: pd.DataFrame,    # 未来 N 日收益率，同上
    *,
    method: str = "spearman",
) -> pd.Series:
    """逐截面计算因子得分 vs 未来收益的秩相关系数。

    返回 Series: index=date, value=IC_t
    """
    dates = factor_scores.index.intersection(forward_returns.index)
    ics: dict[Any, float] = {}
    for d in dates:
        f_t = factor_scores.loc[d].dropna()
        r_t = forward_returns.loc[d].dropna()
        common = f_t.index.intersection(r_t.index)
        if len(common) < 10:
            continue
        if method == "spearman":
            rho = _spearmanr(f_t[common].values, r_t[common].values)
        else:
            rho = np.corrcoef(f_t[common].values, r_t[common].values)[0, 1]
        if not np.isnan(rho):
            ics[d] = rho
    return pd.Series(ics, name="IC")


@dataclass
class FactorMetrics:
    """因子评价指标。"""
    ic_mean: float          # IC 均值（衡量因子方向）
    ic_std: float           # IC 标准差
    ir: float               # IC 均值 / IC 标准差（衡量稳定性，>0.5 算好因子）
    ic_positive_rate: float # IC>0 的占比（方向一致性）
    t_stat: float           # IC_mean / IC_std * sqrt(n)（t 检验显著性）
    n_periods: int          # 有效截面数

    def as_dict(self) -> dict[str, float]:
        return {
            "IC_mean": round(self.ic_mean, 4),
            "IC_std": round(self.ic_std, 4),
            "IR": round(self.ir, 4),
            "IC_positive_rate": round(self.ic_positive_rate, 4),
            "t_stat": round(self.t_stat, 3),
            "n_periods": self.n_periods,
        }


def evaluate_factor(ic_series: pd.Series) -> FactorMetrics:
    """从 IC 序列算 IR 等指标。"""
    n = len(ic_series)
    if n < 5:
        raise ValueError(f"IC 序列太短（{n}），至少 5 个截面才有 IR 意义")
    mean = ic_series.mean()
    std = ic_series.std(ddof=1)
    ir = mean / std if std > 0 else float("nan")
    pos_rate = (ic_series > 0).mean()
    t_stat = mean / std * np.sqrt(n) if std > 0 else float("nan")
    return FactorMetrics(
        ic_mean=mean, ic_std=std, ir=ir,
        ic_positive_rate=pos_rate, t_stat=t_stat, n_periods=n,
    )


# ============== 分层回测 ==============

@dataclass
class QuantileResult:
    """分层回测结果。"""
    n_groups: int
    group_stats: pd.DataFrame      # index=分组编号, columns={年化收益/波动/夏普/最大回撤/平均换手}
    hl_return: float               # 多空(H-L)年化收益
    hl_sharpe: float               # 多空夏普
    hl_max_dd: float               # 多空最大回撤
    turnover: dict[int, float]     # 各组平均换手率


def quantile_backtest(
    factor_scores: pd.DataFrame,      # index=date, columns=code
    forward_returns: pd.DataFrame,    # 持有期收益率，未来看 N 天
    *,
    n_groups: int = 5,
    weight: str = "equal",            # "equal" | "value"（市值加权需传 mv）
    market_cap: pd.DataFrame | None = None,  # index=date, columns=code
    periods_per_year: int = 252,
) -> QuantileResult:
    """按因子得分分组，计算每组持有期收益 + 多空组合绩效。

    **`periods_per_year` 必须与传入收益率的口径一致**：传的是"未来 N 日收益"时，
    每个截面代表一个 N 日持有期，年化系数应是 252/N 而不是 252。
    实测踩坑：用 20 日前瞻收益配默认的 252 年化，算出的"第 5 组年化"高达 46962%
    —— 数字离谱到一眼能看出错，但如果只是轻微偏差就会被当成"因子很强"。
    另外：相邻交易日的 N 日前瞻收益彼此**重叠**，调用方应做非重叠抽样
    （每 N 个交易日取一个截面），否则期数被高估、序列自相关也会污染统计。
    """
    periods = max(1, int(periods_per_year))
    dates = factor_scores.index.intersection(forward_returns.index)
    if weight == "value" and market_cap is not None:
        dates = dates.intersection(market_cap.index)

    group_returns: dict[int, list[float]] = {g: [] for g in range(n_groups)}
    hl_returns: list[float] = []
    turnover_list: dict[int, list[float]] = {g: [] for g in range(n_groups)}
    prev_members: dict[int, set] = {}

    for d in dates:
        f_t = factor_scores.loc[d].dropna()
        r_t = forward_returns.loc[d].reindex(f_t.index)
        valid = r_t.notna()
        if valid.sum() < 10:
            continue
        f_t, r_t = f_t[valid], r_t[valid]

        if weight == "value" and market_cap is not None:
            mv_t = market_cap.loc[d].reindex(f_t.index)
            mv_t = mv_t[mv_t > 0].dropna()
            f_t, r_t = f_t.reindex(mv_t.index), r_t.reindex(mv_t.index)
            if len(f_t) < 10:
                continue
        else:
            mv_t = None

        # 按因子分位数分组
        quantiles = pd.qcut(f_t, n_groups, labels=False, duplicates="drop")
        hl, hr = [], []
        for g in range(n_groups):
            members = set(quantiles[quantiles == g].index)
            if mv_t is not None:
                w = mv_t.reindex(list(members))
                w = w / w.sum()
                g_ret = (r_t.reindex(list(members)) * w).sum()
            else:
                g_ret = r_t.reindex(list(members)).mean()
            group_returns[g].append(g_ret)
            if g == 0:
                hl.append(g_ret)
            if g == n_groups - 1:
                hr.append(g_ret)
            # 换手率：分组成分与上期的交集 / 并集
            if g in prev_members:
                if prev_members[g]:
                    union = members | prev_members[g]
                    inter = members & prev_members[g]
                    turnover_list[g].append(
                        1 - len(inter) / len(union))
            prev_members[g] = members

        if hl and hr:
            hl_returns.append(hr[0] - hl[0])

    # 汇总统计
    def _ann_stats(rets: list[float]) -> dict[str, float]:
        arr = np.array(rets)
        n = len(arr)
        if n == 0:
            return {"annual_return": float("nan"), "vol": float("nan"),
                    "sharpe": float("nan"), "max_dd": float("nan")}
        # 年化系数与收益率口径一致：每个观测代表一个持有期（periods_per_year 个/年）。
        # 夏普要去掉无风险利率的假定，这里用"年化收益/年化波动"的简化口径（与旧行为一致）。
        ann_ret = (1 + arr).prod() ** (periods / n) - 1
        ann_vol = arr.std(ddof=1) * np.sqrt(periods)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else float("nan")
        cum = np.cumprod(1 + arr)
        peak = np.maximum.accumulate(cum)
        max_dd = ((cum - peak) / peak).min()
        return {"annual_return": ann_ret, "vol": ann_vol,
                "sharpe": sharpe, "max_dd": max_dd}

    stats = {}
    for g in range(n_groups):
        s = _ann_stats(group_returns[g])
        s["avg_turnover"] = np.mean(turnover_list[g]) if turnover_list[g] else float("nan")
        stats[g] = s
    group_df = pd.DataFrame(stats).T.round(4)

    hl_stats = _ann_stats(hl_returns) if hl_returns else {
        "annual_return": float("nan"), "vol": float("nan"),
        "sharpe": float("nan"), "max_dd": float("nan"),
    }

    return QuantileResult(
        n_groups=n_groups,
        group_stats=group_df,
        hl_return=round(hl_stats["annual_return"], 4),
        hl_sharpe=round(hl_stats["sharpe"], 4),
        hl_max_dd=round(hl_stats["max_dd"], 4),
        turnover={g: round(np.mean(v), 4) if v else float("nan")
                  for g, v in turnover_list.items()},
    )


# ============== 一键分析入口 ==============

@dataclass
class FactorAnalysisResult:
    """完整因子分析报告：IC/IR + 分层回测 + 数据截断摘要。"""
    name: str
    preprocessing: dict[str, Any]
    ic_metrics: FactorMetrics
    quantile: QuantileResult
    ic_series: pd.Series = field(default_factory=pd.Series)

    def summary(self) -> str:
        ic = self.ic_metrics.as_dict()
        q = self.quantile
        return (
            f"[{self.name}] IR={ic['IR']:.2f} IC均值={ic['IC_mean']:.4f} "
            f"IC>0率={ic['IC_positive_rate']:.2f} t={ic['t_stat']:.1f}\n"
            f"  多空年化={q.hl_return:.2%} 夏普={q.hl_sharpe:.2f} 最大回撤={q.hl_max_dd:.2%}"
        )


def analyze_factor(
    factor_scores: pd.DataFrame,
    forward_returns: pd.DataFrame,
    *,
    name: str = "factor",
    mv: pd.DataFrame | None = None,
    industry_map: dict[str, str] | None = None,
    preprocess_cfg: dict | None = None,
    n_groups: int = 5,
) -> FactorAnalysisResult:
    """完整因子分析（IC + IR + 分层回测）。

    传入的 factor_scores 是已在每个截面调用过 preprocess() 的矩阵；
    本函数只负责统计和分层回测。
    """
    preprocess_cfg = preprocess_cfg or {}
    ic = compute_ic_series(factor_scores, forward_returns)
    metrics = evaluate_factor(ic)
    q = quantile_backtest(
        factor_scores, forward_returns, n_groups=n_groups,
        weight="value" if mv is not None else "equal", market_cap=mv,
    )
    return FactorAnalysisResult(
        name=name, preprocessing=preprocess_cfg,
        ic_metrics=metrics, quantile=q, ic_series=ic,
    )


# ============== 辅助：批量计算横截面因子 ==============

def compute_factor_panel(
    factor_cls: type[Factor],
    prices: pd.DataFrame,                 # index=date, columns=code
    fundamentals: pd.DataFrame | None = None,
    *,
    frequency: str = "M",                 # "M"月末 / "Q"季末
    mv: pd.DataFrame | None = None,
    industry_map: dict[str, str] | None = None,
    preprocess_cfg: dict | None = None,
    **kwargs,
) -> pd.DataFrame:
    """在指定频率的每个截面上跑因子 → 预处理 → 输出面板 DataFrame。

    返回 DataFrame: index=date, columns=code，值=预处理后的因子得分。
    """
    preprocess_cfg = preprocess_cfg or {}
    factor = factor_cls()
    dates = prices.resample(frequency).last().index  # 月末/季末
    panels: list[pd.Series] = []
    for _i, d in enumerate(dates):
        if d not in prices.index:
            continue
        # fundamentals 按最近可用截面取值
        fund_t = (
            fundamentals.loc[d]
            if fundamentals is not None and d in fundamentals.index
            else None
        )
        res = factor.calculate(prices[:d + pd.Timedelta(days=1)], fund_t,
                               industry_map=industry_map, date=d, **kwargs)
        scores = preprocess(res.values, mv=mv.loc[d] if mv is not None and d in mv.index else None,
                            industry_map=industry_map, **preprocess_cfg)
        panels.append(scores.rename(d))
    return pd.concat(panels, axis=1).T  # index=date, columns=code


def forward_return_panel(
    prices: pd.DataFrame,
    horizon: int = 21,                   # 默认 21 日 ≈ 1 月
    frequency: str = "M",
) -> pd.DataFrame:
    """在指定频率截面上，计算每个标的未来 N 日收益率。"""
    rets = prices.pct_change(horizon).shift(-horizon)  # t 截面 → 看 horizon 天后的收益
    dates = prices.resample(frequency).last().index
    return rets.loc[dates]
