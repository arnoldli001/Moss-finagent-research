"""股票池流动性过滤：剔除每日成交额最差的 N%（业界常规做法）。

## 为什么要有这一层

A 股有大量几乎没有成交的票（僵尸股、长期停牌边缘、退市整理）。它们：

1. **不可投资**：回测里买得进、实盘买不进（一买就是几个点的冲击成本）；
2. **污染截面**：Amihud 非流动性、换手率这类因子的极端值几乎全部来自它们，
   IC 与分层回测都会被少数极端票带偏；
3. **占内存**：面板是"日期 × 全市场"的矩形，多装 30% 的票就多 30% 的内存 ——
   这也是 `build_panels` 能把 2015 年以来的窗口跑起来的前提之一。

## 三条设计约束

1. **默认不启用**：它会改变截面构成（也就是改变 IC 结果）。按项目一贯原则
   "会改变结果的过滤必须是显式选项"，默认关闭，打开后在结果里如实标注。
2. **只用过去的数据**：排序用**过去 `window` 个交易日**的平均成交额，
   不用当日值 —— 当日成交额噪声极大（一根大阳线就能把僵尸股顶进前 50%），
   而且用当日值等于给截面排序引入了"当天才知道"的信息。
3. **无法度量 = 剔除**：窗口内没有成交数据的票（未上市 / 长期停牌），
   按"不能证实它可交易"处理，直接剔除。

## 两件事分开：掩码 vs 列子集

- **掩码**（`liquidity_exclusion`）：逐日的 `(日期 × 代码)` 布尔表，
  True = 该日剔除。它保证**每天的截面口径正确**；
- **列子集**（`select_codes`）：面板是矩形的，列一旦装进来就占内存 ——
  只在掩码里体现省不了内存。所以还要选出一个"常驻列"名单：
  窗口内合格天数占比 ≥ `keep_ratio` 的票才装进面板，其余列干脆不加载。
  两者的差异（某天某只票不合格但列还在）由掩码兜住。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LiquidityFilter:
    """股票池流动性过滤参数（显式选项，默认关闭）。"""

    enabled: bool = False
    drop_pct: float = 0.30      # 每日剔除成交额最差的这一比例
    window: int = 20            # 用过去多少个交易日的平均成交额排序
    min_days: int = 10          # 窗口内至少要有多少天数据才参与排序
    keep_ratio: float = 0.5     # 常驻列门槛：窗口内合格天数占比 ≥ 该值

    def describe(self) -> str:
        return (f"股票池过滤：按过去 {self.window} 日均成交额，"
                f"每日剔除最差的 {self.drop_pct:.0%}"
                f"（常驻列门槛：合格天数 ≥ {self.keep_ratio:.0%}）")


def liquidity_exclusion(amount: pd.DataFrame, *,
                        drop_pct: float = 0.30,
                        window: int = 20,
                        min_days: int = 10) -> pd.DataFrame:
    """逐日流动性掩码：True = 该日该票应被剔除。

    `amount`：成交额面板（index=日期，columns=代码，单位元）。
    调用方应当**多传 `window` 个前置交易日**，否则窗口头几天算不出均值
    （`min_days` 未满 → 全被剔除），那段区间会没有可用样本。

    ## 为什么按**排名分位**而不是"金额分位"

    一开始写的是 `avg < avg.quantile(1 - drop_pct)`（在成交额数值上取分位）。
    单测立刻抓到它不对：成交额是**右偏**分布（少数票占绝大部分成交额），
    数值分位对应的股票数与"最差 30%"根本不是一回事 ——
    4 只票、log 级差的三只（10 亿/1 亿/1000 万）在 25% 分位下被剔掉 2 只（50%）。

    改成 `rank(pct=True) <= drop_pct` 之后，"剔除最差的 N%"是**精确**的：
    N 只票就剔除 ⌈N × drop_pct⌉ 只，与金额分布无关。
    """
    if amount is None or amount.empty:
        return pd.DataFrame(dtype=bool)
    window = max(1, int(window))
    # min_periods 不能超过 window（pandas 会直接抛错）；同时至少 2 天，
    # 否则"均值"退化成当日值 —— 那正是本模块要避免的噪声来源。
    periods = max(1, min(window, max(2, int(min_days))))
    avg = amount.rolling(window, min_periods=periods).mean()
    # 横截面排名分位（pct=True → 0~1）；NaN 保持 NaN（na_option="keep"）
    ranks = avg.rank(axis=1, pct=True, na_option="keep")
    # NaN（窗口内没有成交数据）→ 视为剔除：不能证实它可交易
    mask = avg.isna() | ranks.le(float(drop_pct))

    # **边界保护：算不出排序的日子整天放行**，而不是交出一个空截面。
    #
    # 两种触发情形（都发生在区间头部 —— 滚动均值还没攒够历史）：
    #   a) 这一天所有票都算不出均值；
    #   b) 能算出均值的票**全部**落进"最差 N%"。
    # 任其发生的话，那几天的截面是空的 → IC 变 NaN、分层回测少几期，
    # 而现象看起来像"因子在早期失效"，排查方向完全跑偏。
    # 调用方（`build_panels`）会多取 `window` 个前置交易日来避免它，
    # 这里是兜底。
    valid = avg.notna()
    no_info = ~valid.any(axis=1)
    all_cut = valid.any(axis=1) & (mask & valid).all(axis=1)
    keep_all = no_info | all_cut
    if bool(keep_all.any()):
        mask.loc[keep_all] = False
    return mask


def select_codes(exclusion: pd.DataFrame, *, keep_ratio: float = 0.5,
                 dates: Sequence[str] | None = None) -> list[str]:
    """从逐日掩码里选出**常驻列**（要真正装进面板的代码）。

    `dates`：只按这些日期统计合格率（掩码里可能含为算滚动均值而多取的前置
    交易日；那几天不该影响"这只票值不值得装"的判断）。
    """
    if exclusion is None or exclusion.empty:
        return []
    grid = exclusion.reindex(index=list(dates)) if dates is not None else exclusion
    grid = grid.dropna(how="all")
    if grid.empty:
        return []
    passed = ~grid.fillna(True).astype(bool)     # NaN 掩码 = 未知 → 按剔除算
    ratio = passed.sum(axis=0) / float(len(grid))
    return [str(code) for code in ratio.index[ratio >= float(keep_ratio)]]


def describe_effect(exclusion: pd.DataFrame, kept: Sequence[str], *,
                    universe: int, dates: Sequence[str] | None = None) -> str:
    """一句话体检：每日剔除多少、常驻列砍掉多少。"""
    if exclusion is None or exclusion.empty:
        return "股票池过滤：无成交额数据，未生效"
    grid = exclusion.reindex(index=list(dates)) if dates is not None else exclusion
    per_day = grid.sum(axis=1)
    total_codes = int(grid.shape[1])
    share = (len(kept) / total_codes * 100) if total_codes else 0.0
    return (f"股票池过滤生效：全市场 {total_codes} 只 → 常驻 {len(kept)} 列"
            f"（{share:.1f}%）；每日平均剔除 {per_day.mean():.0f} 只"
            f"（占当日截面 {per_day.mean() / max(1, total_codes) * 100:.1f}%）"
            f"；覆盖 {len(grid)} 个交易日")
