"""M3 因子筛选流水线：中性化 → IC/ICIR → 相关性聚类去重 → 分层回测 → walk-forward 样本外。

设计文档 §4.2 的四阶段在这里落地，但方法和文档里的伪代码有三处**刻意不同**：

1. **ICIR 定义**：文档代码写的是 `ic / factor.std()`（因子截面标准差）—— 那是错的，
   与它自己正文写的"IC 均值 / IC 标准差"也不一致。这里用正确的
   **IC 时间序列的均值/标准差**（`factor_analyzer.evaluate_factor` 早已实现）。
2. **样本内/样本外必须分开**：文档全文没提 walk-forward。因子筛选如果在同一段数据上
   "筛完就回测"，IC 会明显虚高。这里强制切分：前 70% 训练（挑因子与权重）、
   后 30% 检验，并**只报样本外结果**。
3. **去重靠相关性聚类，不靠 Gram-Schmidt**：文档说"GS 正交化确保相关系数 < 0.3"
   —— GS 的结果依赖因子顺序、残差两两相关也不保证阈值，还会把代表性因子的经济含义改掉。
   这里改成：相关性矩阵 → 按 |ρ| 阈值的贪心聚类 → 每簇保留 |ICIR| 最高者。

行业分类来自 QMT 的申万行业成分（`get_stock_list_in_sector("SW1银行")` 一类），
没有行业数据时只对市值中性化，并在结果里如实标注。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.quant.factor_analyzer import compute_ic_series, evaluate_factor, quantile_backtest
from src.quant.factor_base import mad_winsorize, neutralize, zscore
from src.quant.factor_library_v2 import FACTORS
from src.quant.panels import FactorPanels

logger = logging.getLogger(__name__)


@dataclass
class ScreenConfig:
    """筛选参数。"""

    ic_horizon: int = 20            # 前瞻交易日数（≈1 个月）
    min_ic: float = 0.02            # |IC| 下限
    min_icir: float = 0.3           # |ICIR| 下限
    corr_threshold: float = 0.7     # |相关系数| 超过此值视为同一簇
    train_ratio: float = 0.7        # walk-forward 训练集比例
    neutralize_mv: bool = True
    neutralize_industry: bool = False
    winsorize: bool = True
    n_groups: int = 5
    target_count: int = 20          # 去重后期望因子数上限


@dataclass
class ScreenResult:
    """筛选结果（全部可直接序列化成 JSON 给前端）。"""

    ic_table: pd.DataFrame
    oos_table: pd.DataFrame
    clusters: list[dict[str, Any]]
    selected: list[str]
    dropped: list[dict[str, str]]
    corr_matrix: pd.DataFrame
    quantile: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ic_table": _records(self.ic_table),
            "oos_table": _records(self.oos_table),
            "clusters": self.clusters,
            "selected": self.selected,
            "dropped": self.dropped,
            "quantile": self.quantile,
            "notes": self.notes,
        }


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    clean = frame.replace({np.nan: None})
    return [
        {str(key): (None if value is None or value != value else value)
         for key, value in row.items()}
        for row in clean.to_dict("records")
    ]


# ==================================================================
# 中性化
# ==================================================================


def neutralize_factors(
    factors: dict[str, pd.DataFrame], panels: FactorPanels, *,
    config: ScreenConfig | None = None,
    industry_map: dict[str, str] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """逐日截面：MAD 去极值 → 市值/行业中性化 → z-score。

    返回 (中性化后的因子面板, 说明列表)。缺市值列时跳过中性化并如实记说明。
    """
    cfg = config or ScreenConfig()
    notes: list[str] = []
    total_mv = panels.basic("total_mv")
    if cfg.neutralize_mv and total_mv.notna().to_numpy().any():
        mv_available = True
    else:
        mv_available = False
        if cfg.neutralize_mv:
            notes.append("缺少市值数据（daily_basic.total_mv），已跳过市值中性化")
    if cfg.neutralize_industry and not industry_map:
        notes.append("未提供行业分类（QMT 申万成分），已跳过行业中性化")

    output: dict[str, pd.DataFrame] = {}
    for key, panel in factors.items():
        rows: dict[str, pd.Series] = {}
        for date in panel.index:
            section = panel.loc[date].dropna()
            if len(section) < 20:
                continue
            values = mad_winsorize(section) if cfg.winsorize else section
            if mv_available and date in total_mv.index:
                mv = total_mv.loc[date]
                values = neutralize(values, mv=mv,
                                    industry_map=industry_map if cfg.neutralize_industry
                                    else None)
            rows[date] = zscore(values)
        output[key] = (pd.DataFrame(rows).T.reindex(index=panel.index,
                                                    columns=panel.columns)
                       if rows else pd.DataFrame(index=panel.index,
                                                 columns=panel.columns,
                                                 dtype="float64"))
    return output, notes


# ==================================================================
# IC / ICIR
# ==================================================================


def ic_table(factors: dict[str, pd.DataFrame],
             forward_returns: pd.DataFrame) -> pd.DataFrame:
    """逐因子 IC/ICIR/t 值/IC>0 比例。"""
    rows = []
    for key, panel in factors.items():
        spec = FACTORS.get(key)
        series = compute_ic_series(panel, forward_returns)
        base = {"factor": key,
                "label": spec.label if spec else key,
                "category": spec.category if spec else ""}
        if len(series) < 5:
            rows.append({**base, "IC": None, "ICIR": None, "t": None,
                         "IC>0": None, "periods": len(series)})
            continue
        metrics = evaluate_factor(series)
        rows.append({**base, "IC": round(metrics.ic_mean, 4),
                     "ICIR": round(metrics.ir, 4),
                     "t": round(metrics.t_stat, 2),
                     "IC>0": round(metrics.ic_positive_rate, 3),
                     "periods": metrics.n_periods})
    table = pd.DataFrame(rows)
    if not table.empty:
        table["abs_icir"] = table["ICIR"].abs()
        table = table.sort_values("abs_icir", ascending=False).drop(columns="abs_icir")
    return table.reset_index(drop=True)


# ==================================================================
# 相关性聚类去重
# ==================================================================


def correlation_matrix(factors: dict[str, pd.DataFrame],
                       dates: Sequence[str] | None = None, *,
                       min_per_date: int = 20, min_dates: int = 5) -> pd.DataFrame:
    """因子间的**逐日截面平均秩相关**（Spearman = 排名的 Pearson）。

    两个必须踩过的坑：

    1. **不能用"当天所有因子都得有值"来筛日期**：`momentum_120`/`peg` 这类低覆盖因子
       在早期交易日整列为空，一旦要求"全因子齐备"，所有训练日都会被跳过，
       相关矩阵变成全 NaN，聚类静默失效（实测：报告"35 个因子 → 35 个代表因子"，
       看起来像"没有相关因子"，实际是根本没算）。
       因此这里改成**逐对可用（pairwise complete）**：每对因子只在"两边都有值"的
       交易日上算相关，再对有效天数取均值。
    2. **不依赖 scipy**：`DataFrame.corr(method="spearman")` 会去 import scipy，
       而本项目没装 scipy（`ModuleNotFoundError`）。这里先把截面排名算出来，
       再自己对排名求 Pearson —— 数学上就是 Spearman。
    """
    keys = list(factors)
    if not keys:
        return pd.DataFrame()
    window = ([str(date) for date in dates] if dates is not None
              else list(factors[keys[0]].index))
    # 逐日截面排名（每行内排名），NaN 保持 NaN
    rank_arrays: list[np.ndarray] = []
    for key in keys:
        panel = factors[key].reindex(index=window)
        rank_arrays.append(panel.rank(axis=1).to_numpy(dtype=float))

    days = len(window)
    size = len(keys)
    totals = np.zeros((size, size), dtype=float)
    counts = np.zeros((size, size), dtype=float)
    for day in range(days):
        row = np.vstack([rank_arrays[index][day] for index in range(size)])
        valid = ~np.isnan(row)
        for left in range(size):
            for right in range(left + 1, size):
                mask = valid[left] & valid[right]
                if int(mask.sum()) < min_per_date:
                    continue
                x, y = row[left][mask], row[right][mask]
                if x.std() == 0 or y.std() == 0:
                    continue
                value = float(np.corrcoef(x, y)[0, 1])
                if value != value:
                    continue
                totals[left, right] += value
                totals[right, left] += value
                counts[left, right] += 1
                counts[right, left] += 1

    with np.errstate(invalid="ignore", divide="ignore"):
        matrix = totals / counts
    matrix[counts < min_dates] = np.nan       # 有效交易日太少的对子不参与判断
    np.fill_diagonal(matrix, 1.0)
    return pd.DataFrame(matrix, index=keys, columns=keys)


def cluster_by_correlation(corr: pd.DataFrame, threshold: float) -> list[list[str]]:
    """按 |ρ| ≥ threshold 做贪心聚类（单链接：与簇内任一成员高相关即入簇）。

    不用层次聚类是为了**可解释**：每一步"为什么这两个因子被合并"都能指着相关系数说清楚。
    """
    keys = list(corr.columns)
    parent = {key: key for key in keys}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for i, left in enumerate(keys):
        for right in keys[i + 1:]:
            value = corr.loc[left, right]
            if value is None or value != value:
                continue
            if abs(float(value)) >= threshold:
                root_left, root_right = find(left), find(right)
                if root_left != root_right:
                    parent[root_right] = root_left

    groups: dict[str, list[str]] = {}
    for key in keys:
        groups.setdefault(find(key), []).append(key)
    return sorted(groups.values(), key=lambda group: (-len(group), group[0]))


def select_representatives(corr: pd.DataFrame, table: pd.DataFrame,
                           threshold: float) -> tuple[list[str], list[dict[str, str]],
                                                     list[dict[str, Any]]]:
    """每簇保留 |ICIR| 最高者；其余记入 dropped 并注明"与谁高相关"。"""
    icir = {row["factor"]: (row["ICIR"] if row["ICIR"] is not None else 0.0)
            for _, row in table.iterrows()}
    clusters = cluster_by_correlation(corr, threshold)
    selected: list[str] = []
    dropped: list[dict[str, str]] = []
    detail: list[dict[str, Any]] = []
    for group in clusters:
        ranked = sorted(group, key=lambda key: -abs(icir.get(key, 0.0)))
        keep = ranked[0]
        selected.append(keep)
        members = []
        for key in ranked:
            members.append({"factor": key, "icir": round(icir.get(key, 0.0), 4),
                            "kept": key == keep})
            if key != keep:
                dropped.append({
                    "factor": key, "kept_by": keep,
                    "reason": f"与 {keep} 平均秩相关 |ρ|≥{threshold}"})
        detail.append({"representative": keep, "members": members,
                       "size": len(group)})
    return sorted(selected), dropped, detail


# ==================================================================
# walk-forward 样本外
# ==================================================================


def split_dates(dates: Sequence[str], train_ratio: float) -> tuple[list[str], list[str]]:
    ordered = sorted(str(date) for date in dates)
    cut = max(1, min(len(ordered) - 1, int(round(len(ordered) * train_ratio))))
    return ordered[:cut], ordered[cut:]


def forward_returns(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """前瞻收益（IC 检验的目标变量；只在评估里用，不参与任何信号生成）。"""
    return (close.shift(-horizon) / close - 1.0).replace([np.inf, -np.inf], np.nan)


def walk_forward(
    factors: dict[str, pd.DataFrame], close: pd.DataFrame, *,
    config: ScreenConfig, candidates: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (样本内 IC 表, 样本外 IC 表)。

    样本外只评估**在样本内被选中的因子**，避免"用样本外挑因子"的二次污染。
    """
    horizon = config.ic_horizon
    fwd = forward_returns(close, horizon)
    train_dates, test_dates = split_dates(list(close.index), config.train_ratio)
    train = {key: panel.reindex(index=train_dates) for key, panel in factors.items()}
    in_sample = ic_table(train, fwd.reindex(index=train_dates))
    if candidates:
        in_sample = in_sample[in_sample["factor"].isin(candidates)].reset_index(drop=True)
    test = {key: panel.reindex(index=test_dates) for key, panel in factors.items()}
    out_sample = ic_table(test, fwd.reindex(index=test_dates))
    if candidates:
        out_sample = out_sample[out_sample["factor"].isin(candidates)].reset_index(drop=True)
    return in_sample, out_sample


# ==================================================================
# 一键筛选
# ==================================================================


def screen(
    panels: FactorPanels, factors: dict[str, pd.DataFrame], *,
    config: ScreenConfig | None = None,
    industry_map: dict[str, str] | None = None,
    progress: Any = None,
) -> ScreenResult:
    """完整筛选：中性化 → 训练集 IC → 相关性去重 → **样本外复核** → 分层回测。

    **筛选过程只看训练集**（这是与"先在全样本上挑因子再报样本外指标"的关键区别）：
    门槛过滤、聚类代表选择、合成权重全部来自前 `train_ratio` 的交易日；
    后 30% 只用来评估，不参与任何选择。若用全样本 IC 挑因子，样本外指标会虚高，
    这正是设计文档没做、而我在第一版里也写错的地方（实测暴露："样本外 ICIR 反而更高"）。
    """
    cfg = config or ScreenConfig()
    notes: list[str] = []

    def step(text: str) -> None:
        if progress:
            progress(text)

    step("中性化（去极值 → 市值/行业中性化 → 标准化）")
    neutral, neutralize_notes = neutralize_factors(
        factors, panels, config=cfg, industry_map=industry_map)
    notes.extend(neutralize_notes)
    if cfg.neutralize_mv:
        notes.append("已做市值中性化：规模类因子（total_mv/circ_mv/log_mv/"
                     "free_float_mv）与中性化变量共线，其 IC 仅供参考，"
                     "不宜据此选股")

    close = panels.price("close")
    fwd = forward_returns(close, cfg.ic_horizon)
    train_dates, test_dates = split_dates(list(close.index), cfg.train_ratio)
    notes.append(f"walk-forward：训练集 {len(train_dates)} 个交易日"
                 f"（{train_dates[0]}~{train_dates[-1]}），"
                 f"样本外 {len(test_dates)} 个（{test_dates[0]}~{test_dates[-1]}）")

    step("计算训练集 IC/ICIR（筛选仅用训练集）")
    train_factors = {key: panel.reindex(index=train_dates)
                     for key, panel in neutral.items()}
    train_table = ic_table(train_factors, fwd.reindex(index=train_dates))

    step("训练集相关性聚类去重")
    corr = correlation_matrix(train_factors, train_dates)
    selected, dropped, clusters = select_representatives(
        corr, train_table, cfg.corr_threshold)
    notes.append(
        f"相关性去重：{len(train_table)} 个因子 → {len(selected)} 个代表因子"
        f"（训练集 |ρ|≥{cfg.corr_threshold} 视为同簇）")

    qualified = train_table[
        (train_table["IC"].abs() >= cfg.min_ic)
        & (train_table["ICIR"].abs() >= cfg.min_icir)
    ]["factor"].tolist() if not train_table.empty else []
    for key in train_table["factor"].tolist():
        if key not in qualified and key in selected:
            dropped.append({
                "factor": key, "kept_by": "",
                "reason": f"训练集未过门槛（|IC|≥{cfg.min_ic} 且 "
                          f"|ICIR|≥{cfg.min_icir}）"})
    selected = [key for key in selected if key in qualified]
    if len(selected) > cfg.target_count:
        selected = selected[:cfg.target_count]
        notes.append(f"代表因子超过上限 {cfg.target_count}，按训练集 |ICIR| 截断")
    notes.append(f"训练集通过门槛（|IC|≥{cfg.min_ic}, |ICIR|≥{cfg.min_icir}）"
                 f"并去重后保留 {len(selected)} 个因子")

    step("样本外复核")
    test_factors = {key: panel.reindex(index=test_dates)
                    for key, panel in neutral.items()}
    out_sample = ic_table(test_factors, fwd.reindex(index=test_dates))
    if selected:
        out_sample = out_sample[out_sample["factor"].isin(selected)].reset_index(drop=True)
        in_icir = train_table[train_table["factor"].isin(selected)]["ICIR"].abs().mean()
        oos_icir = out_sample["ICIR"].abs().mean()
        if in_icir and not np.isnan(in_icir) and oos_icir == oos_icir:
            if oos_icir >= in_icir:
                notes.append(
                    f"样本外平均 |ICIR| {oos_icir:.3f} ≥ 训练集 {in_icir:.3f}："
                    f"未见衰减，但样本外只有 {len(test_dates)} 个交易日，"
                    f"ICIR 的不确定性很大，不能当作「因子仍然有效」的证据")
            else:
                notes.append(f"训练集平均 |ICIR| {in_icir:.3f} → 样本外 "
                             f"{oos_icir:.3f}（衰减 {(1 - oos_icir / in_icir) * 100:.0f}%）")

    step("分层回测（训练集 / 样本外各跑一次，非重叠持有期）")
    quantile: dict[str, Any] = {}
    if selected:
        composite = composite_score(neutral, selected, train_table)
        # **非重叠抽样**：IC 前瞻 N 日时，每 N 个交易日取一个截面作为一期，
        # 否则相邻截面的持有期互相重叠（期数虚增、序列自相关），
        # 年化系数也必须用 252/N（实测：用默认 252 配上 20 日前瞻收益，
        # "第5组年化"会算出 46962% 这种离谱值）。
        stride = max(1, int(cfg.ic_horizon))
        periods_per_year = max(1, round(252 / stride))
        for label, window in (("oos", test_dates), ("in_sample", train_dates)):
            sampled = list(window)[::stride]
            try:
                result = quantile_backtest(
                    composite.reindex(index=sampled),
                    fwd.reindex(index=sampled), n_groups=cfg.n_groups,
                    periods_per_year=periods_per_year)
                quantile[label] = {
                    "n_groups": result.n_groups,
                    "hl_return": result.hl_return,
                    "hl_sharpe": result.hl_sharpe,
                    "hl_max_dd": result.hl_max_dd,
                    "turnover": {str(k): v for k, v in result.turnover.items()},
                    "group_stats": _records(result.group_stats.reset_index()),
                    "periods": len(sampled),
                    "holding_days": stride,
                    "periods_per_year": periods_per_year,
                }
            except Exception as exc:  # noqa: BLE001 分层回测失败不该丢掉筛选结果
                notes.append(f"{label} 分层回测失败：{str(exc)[:120]}")
        oos_periods = int(quantile.get("oos", {}).get("periods", 0))
        notes.append(f"分层回测按 {stride} 日持有期、非重叠抽样"
                     f"（样本外 {len(quantile.get('oos', {}).get('group_stats', []))} 组 × "
                     f"{oos_periods} 期）")
        if 0 < oos_periods < 8:
            # 期数太少时年化数字的噪声极大（3 期就能算出 +115% 这种数字），
            # 必须显式提醒，否则会被当成"策略很强"。
            notes.append(
                f"⚠️ 样本外仅 {oos_periods} 个非重叠持有期，年化/夏普的统计噪声极大，"
                f"只能当作方向性参考。要得到可信结论，请回补更多历史数据"
                f"（`python scripts/quant_sync.py download --start 2015-01-01 "
                f"--end <今天>`）或缩短 IC 前瞻期数")
        # 前端默认看样本外结果（更诚实），同时保留训练集供对照
        quantile["composite_factors"] = selected
        if "oos" in quantile:
            quantile.update({key: value for key, value in quantile["oos"].items()
                             if key not in quantile})
    else:
        notes.append("没有因子通过门槛，未做分层回测")

    return ScreenResult(ic_table=train_table, oos_table=out_sample,
                        clusters=clusters, selected=selected, dropped=dropped,
                        corr_matrix=corr, quantile=quantile, notes=notes)


def composite_score(factors: dict[str, pd.DataFrame], keys: Sequence[str],
                    ic_table_frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """ICIR 加权合成（权重取样本内 |ICIR|，方向由因子注册表给出）。"""
    weights: dict[str, float] = {}
    if ic_table_frame is not None and not ic_table_frame.empty:
        lookup = dict(zip(ic_table_frame["factor"], ic_table_frame["ICIR"],
                          strict=False))
        for key in keys:
            value = lookup.get(key)
            weights[key] = float(value) if value is not None and value == value else 0.0
    total_abs = sum(abs(value) for value in weights.values())
    if total_abs <= 0:
        weights = {key: 1.0 for key in keys}
        total_abs = float(len(keys))
    composite: pd.DataFrame | None = None
    for key in keys:
        # ICIR 的符号已经包含了"因子方向"，直接按符号加权即可（不再重复取反）
        weight = weights.get(key, 0.0) / total_abs
        contribution = factors[key] * weight
        composite = contribution if composite is None else composite.add(
            contribution, fill_value=np.nan)
    if composite is None:
        return pd.DataFrame()
    return composite
