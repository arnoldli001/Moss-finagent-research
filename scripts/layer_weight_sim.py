"""层权重决策实验：直接模拟「排序取头部」的收益，而不是只看 IC。

## 为什么要做这一步（以及为什么不能只看 IC）

诊断脚本 `_diag_layer_mix.py` 发现两件事：

1. 候选池只占全部板块的 **~20%**；其余 80% 的 `base_total` **只由六维层构成**
   （`service.py:470-477`）。所以「全体板块」口径的 IC 是一个混用两个公式的
   截面，不能用来比较层权重。
2. 回到**同口径的候选池**后，`six_dim` 与 `accumulation` 的逐日排序相关只有
   +0.03 / +0.07（基本不相关），而第二层在候选池内 H=20 的 IC 是
   +0.017 / **−0.057**。等权平均一个「不相关且更弱」的信号就是纯稀释。

但 IC 只是「排序能力」，用户真正关心的是**取头部之后的收益**（告警、交易）。
IC 高一点但头部没变化，等于什么都没做。所以这里直接模拟：

    每日在候选池内按合成分排序 → 取头部 K 个 → 看未来 20/60 日板块收益

对照是**候选池等权全买**（说明「排序」本身有没有超越「这个池子整体在涨」）。
并要求**两个窗口同号**才算结论（本项目反复踩过单窗口假象）。

只读、不写库。

## 两个已经踩过的坑（都写在这里，避免下次再犯）

- **单位**：`factor_ic_report.load_returns` 返回的是**小数**（`0.02` = +2%），
  第一版这里又乘了 100，于是打印出「全市场平均收益 +62%」。
- **均值 vs 中位数**：`ml_board_bar` 里有极少数收盘价接近 0 的脏板块，
  `close[t+20]/close[t]` 会给出上万倍，把**均值**整个拉飞（新区间一度
  显示 +809%）。所以全市场基准取中位数。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.factor_ic_report import (  # noqa: E402
    MAIN_DB,
    load_returns,
)

WINDOWS = (("20231009", "20240806", "旧区间"), ("20251001", "20260918", "新区间"))
HORIZONS = (20, 60)
#: 六维层占的权重（其余给第二层）；模拟「改 `synthesis.layer_weights`」
WEIGHTS = (100.0, 80.0, 70.0, 60.0, 50.0, 30.0, 0.0)
#: 每日取头部几个（对齐 funnel 的 `selected` 规模，见 configs/mainline.yaml）
TOP_K = 10
#: 是否把 `gate_bonus` 也算进排序键（现行实现是算的）
GATE_MODES = (True, False)


def load_panel(start: str, end: str) -> pd.DataFrame:
    """读成一张长表：`[trade_date, board_code, six_dim, accumulation, gate]`。

    只保留 `candidate=True` 的板块 —— 只有它们参与精选与告警排序。
    """
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_date, board_code, payload FROM mainline_score"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end)).fetchall()
    conn.close()
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue                      # 只保留候选池（真正参与排序的那一拨）
        layers = {str(item.get("key")): float(item.get("score") or 0.0)
                  for item in (payload.get("layers") or [])}
        records.append({
            "trade_date": str(row["trade_date"]),
            "board_code": str(row["board_code"]),
            "six_dim": float(payload.get("six_dim_score") or 0.0),
            "accumulation": layers.get("accumulation", 0.0),
            "gate_bonus": float(payload.get("gate_bonus") or 0.0),
            "etf_bonus": float(payload.get("etf_bonus") or 0.0),
        })
    return pd.DataFrame(records)


def robust_market_level(returns: dict[int, pd.DataFrame], horizon: int) -> float:
    """全市场板块等权收益的**中位数**（小数口径）。

    用中位数而不是均值：脏板块的 `close` 可能接近 0，比值会给出上万倍。
    """
    return float(np.nanmedian(returns[horizon].to_numpy()))


def simulate(panel: pd.DataFrame, returns: dict[int, pd.DataFrame],
             scorer, label: str) -> dict[int, dict]:
    """按给定的打分函数排序取头部，返回每个持有期的统计。

    `scorer` 接收**当日候选池的 DataFrame**，返回同索引的分数 Series。
    """
    panel = panel.copy()
    panel["score"] = np.nan
    for _day, group in panel.groupby("trade_date", sort=True):
        panel.loc[group.index, "score"] = scorer(group).to_numpy()
    out: dict[int, dict] = {}
    for horizon in HORIZONS:
        ret = returns[horizon]
        picked: list[float] = []
        pool_mean: list[float] = []
        for day, group in panel.groupby("trade_date", sort=True):
            if day not in ret.index:
                continue
            row = ret.loc[day]
            pool = group["board_code"].tolist()
            values = row.reindex(pool).dropna()
            if len(values) < TOP_K:
                continue
            top = (group.set_index("board_code")["score"]
                   .reindex(pool).astype(float)
                   .sort_values(ascending=False).head(TOP_K).index)
            picked.append(float(row.reindex(top).dropna().mean()))
            pool_mean.append(float(values.mean()))
        if not picked:
            continue
        pick = np.array(picked)
        poolv = np.array(pool_mean)
        diff = pick - poolv
        # 日度超额**不独立**（持有期重叠），所以这个 t 只是上界参考，
        # 不能当显著性证明。真正的判据是「两个窗口同号」。
        se = (diff.std(ddof=1) / np.sqrt(len(diff))
              if len(diff) > 1 else float("nan"))
        out[horizon] = {
            "days": len(pick),
            "pick": float(pick.mean()),
            "pick_med": float(np.median(pick)),
            "pool": float(poolv.mean()),
            "excess_pool": float(diff.mean()),
            "excess_se": float(se),
            "win_pool": float((diff > 0).mean()),
        }
    return out


def raw_scorer(w_six: float, gate: bool):
    """现行口径：两层原始分直接加权（可选把 `gate_bonus` 加进排序键）。"""
    def score(group: pd.DataFrame) -> pd.Series:
        value = (group["six_dim"] * w_six
                 + group["accumulation"] * (100.0 - w_six)) / 100.0
        if gate:
            value = value + group["gate_bonus"]
        return value
    return score


def standardized_equal_scorer():
    """**日内标准化后等权** —— 修的是「名义 50/50、实际由方差大的那层主导」。

    候选池内实测（696 天重打分后）：
      `six_dim`     mean 64.4 / **sd 4.9**
      `accumulation` mean 29.9 / **sd 22.1**

    两层原始分直接 50/50 相加时，方差贡献是 `0.25×4.9² = 6.0`
    对 `0.25×22.1² = 122` —— 六维层只占 **4.7%** 的方差。
    也就是说「50/50」是**名义**权重，实际排序由第二层主导（约 95%），
    而第二层的 IC 恰好更差。标准化后再等权，才是真正的 50/50。

    ⚠️ 这个方案**不加 `gate_bonus`**：标准化后的分是 z 分（量级 ±2），
    而门控加分是 0~20 的原始分 —— 直接相加会让加分**完全主导**排序。
    那本身就是本节在讨论的"尺度不同则权重是假的"错误，
    所以这里只比两层，门控的影响由对照组单独看。
    """
    def score(group: pd.DataFrame) -> pd.Series:
        out = pd.Series(0.0, index=group.index, dtype=float)
        for column in ("six_dim", "accumulation"):
            values = group[column].astype(float)
            sd = float(values.std(ddof=0))
            out = out + ((values - values.mean()) / sd if sd > 0 else 0.0)
        return out
    return score


def main() -> int:
    for start, end, label in WINDOWS:
        panel = load_panel(start, end)
        if panel.empty:
            print(f"{label}：候选池为空，跳过")
            continue
        returns = load_returns(start, end)
        print("=" * 110)
        print(f"【{label} {start}~{end}】候选池样本 {len(panel)} 行，"
              f"{panel['trade_date'].nunique()} 天；每日取头部 {TOP_K} 个")
        print("-" * 110)
        for horizon in HORIZONS:
            level = robust_market_level(returns, horizon)
            print(f"  H={horizon}  全市场板块等权收益中位数 {level * 100:+.2f}%")
        # 层内方差的分解：名义权重 vs 实际影响
        for column in ("six_dim", "accumulation"):
            var = float(panel.groupby("trade_date")[column].var(ddof=0).mean())
            print(f"  {column:<14} 日内方差均值 {var:8.2f}（sd {var ** 0.5:5.2f}）")
        v_six = float(panel.groupby("trade_date")["six_dim"].var(ddof=0).mean())
        v_acc = float(panel.groupby("trade_date")["accumulation"]
                      .var(ddof=0).mean())
        share = 0.25 * v_six / (0.25 * v_six + 0.25 * v_acc)
        print(f"  → 名义 50/50 时六维层的**方差占比** = {share * 100:.1f}%"
              f"（其余 {100 - share * 100:.1f}% 由第二层主导）")
        print()
        print(f"  {'方案':<34}{'门控':>6}{'H':>4}{'头部均值':>10}"
              f"{'头部中位':>10}{'候选池均值':>12}{'超额(池)':>10}"
              f"{'超额t上界':>11}{'胜率(池)':>10}{'天数':>6}")
        plans: list[tuple[str, object]] = [
            (f"原始分 {w:.0f}/{100 - w:.0f}（{'含' if g else '不含'}门控）",
             raw_scorer(w, g))
            for g in GATE_MODES for w in WEIGHTS]
        plans.append(("标准化等权（两层，不含门控）", standardized_equal_scorer()))
        for name, scorer in plans:
            stats = simulate(panel, returns, scorer, name)
            for horizon in HORIZONS:
                item = stats.get(horizon)
                if not item:
                    continue
                print(f"  {name:<34}{horizon:>4}{item['pick'] * 100:>10.2f}"
                      f"{item['pick_med'] * 100:>10.2f}"
                      f"{item['pool'] * 100:>12.2f}"
                      f"{item['excess_pool'] * 100:>10.2f}"
                      f"{item['excess_se'] * 100:>11.2f}"
                      f"{item['win_pool'] * 100:>9.1f}%"
                      f"{item['days']:>6}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
