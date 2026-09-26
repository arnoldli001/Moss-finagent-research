"""策略回测：分批低吸 + 止损 + 阶梯止盈，算加权平均年化。

## 用户给的规则

    提醒主线后 → 每周低吸 1 成，分 3 周买入 3 成
    从建仓起持有 60 个交易日
    三成仓建满后，亏 7% → 清仓
    收益 >10% 减半；>20% 再减半；依次类推

## 实现口径（每一处假设都写出来，否则结果无法复现）

- **买入时点**：第 0 / 5 / 10 个交易日**收盘价**各买 1 成。
  "低吸"没有给定触发条件（跌多少买？），所以只能按固定间隔建仓。
  想改成"回踩某均线/跌 X% 才买"需要另给规则。
- **仓位口径**：1 成 = **总资金的 10%**，三成 = 总资金 30%。
  所以收益有两种算法，两个都报：
    ① **资金收益率** = 盈亏 / 总资金   ← 组合层面的真实收益（本金只用出 30%）
    ② **持仓收益率** = 盈亏 / 峰值投入  ← 常说的"这笔赚了几个点"
- **止损**：从**第三笔买满**（第 10 个交易日）起生效，触发价 = 均价 × (1−7%)，
  按当日收盘成交。用均价而不是首笔价 —— 分批建仓后成本是加权平均。
- **阶梯止盈**：每上一个 10% 台阶，卖出**当时剩余的一半**。
  同一天跨多个台阶就连卖多次（跳空时会出现）。
- **收尾**：到第 60 个交易日，剩余仓位按当日收盘清掉。
- **年化**：`(1 + 资金收益率) ** (252/60) - 1`。
  ⚠️ 这一步**假设每次到期后立刻能找到下一笔同样条件的交易**，
  忽略了资金闲置期；而且告警高度重叠（日均 11.6 条），
  **现实中不可能同时吃下所有信号**。所以"平均年化"是**单笔口径**，
  不是组合可实现收益 —— 这一点必须记住。

用法：
    .venv\\Scripts\\python.exe scripts\\alert_strategy_backtest.py
    .venv\\Scripts\\python.exe scripts\\alert_strategy_backtest.py --tp-step 10 --stop 7
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
MAIN_DB = ROOT / "data" / "moss_finagent.db"


def load(start: str, end: str, min_coverage: float) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    pool = {str(r["code"]) for r in conn.execute(
        "SELECT code FROM ml_board WHERE source = 'sector_crowding:list'")}
    rows = conn.execute(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    wide = pd.DataFrame(
        [{"b": str(r["board_code"]), "d": str(r["trade_date"]),
          "c": float(r["close"] or 0)} for r in rows]
    ).pivot(index="d", columns="b", values="c").sort_index()
    coverage = wide.notna().mean()
    keep = [c for c in wide.columns
            if c in pool and coverage.get(c, 0.0) >= min_coverage]
    return wide[keep]


def simulate(values: np.ndarray, i: int, j: int, total: int, *,
             tranches: int, gap: int, hold: int, stop: float,
             tp_step: float, tp_frac: float, deploy: bool = True
             ) -> dict | None:
    """一笔信号的完整模拟。`values[t, j]` 是板块收盘价。"""
    last_buy = i + (tranches - 1) * gap
    if last_buy + hold >= total:
        return None                       # 未来数据不够
    units = 0.0
    invested = 0.0                        # 仍占用的本金（占总资金的比例）
    realized = 0.0                        # 已实现盈亏（占总资金的比例）
    peak = 0.0
    if deploy:
        for k in range(tranches):
            index = i + k * gap
            price = values[index, j]
            if not np.isfinite(price) or price <= 0:
                return None
            units += 0.10 / price
            invested += 0.10
    else:
        # 基准模式：一次性在首日买入同样金额（不设止损止盈，见调用处）
        price = values[i, j]
        if not np.isfinite(price) or price <= 0:
            return None
        units = 0.30 / price
        invested = 0.30
        last_buy = i
    peak = invested
    if units <= 0:
        return None
    cost = invested / units               # 加权平均成本

    next_tp = 1.0 + tp_step / 100.0
    exit_days = hold
    stopped = False
    for step in range(last_buy, last_buy + hold + 1):
        price = values[step, j]
        if not np.isfinite(price) or price <= 0:
            continue
        last_price = price
        # 止损（建满三成之后才生效）
        if step > last_buy and price <= cost * (1.0 - stop / 100.0):
            realized += units * price - invested
            units = 0.0
            invested = 0.0
            exit_days = step - i
            stopped = True
            break
        # 阶梯止盈：同一天可跨多个台阶
        while units > 0 and price >= cost * next_tp:
            sold = units * tp_frac
            cash = sold * price
            realized += cash - invested * tp_frac
            units -= sold
            invested *= (1.0 - tp_frac)
            next_tp += tp_step / 100.0
        if units <= 0:
            exit_days = step - i
            break
    if units > 0:
        price = values[last_buy + hold, j]
        if not np.isfinite(price) or price <= 0:
            return None
        realized += units * price - invested
        units = 0.0
    return {"资金收益率": realized * 100,
            "持仓收益率": realized / peak * 100 if peak else np.nan,
            "持有天数": exit_days, "止损": stopped,
            "峰值投入": peak * 100}


def evaluate(pairs: list[tuple[int, int]], values: np.ndarray, total: int,
             **cfg) -> dict | None:
    """对一批 `(i, j)` 跑同一个配置，返回聚合统计。"""
    out: list[dict] = []
    for i, j in pairs:
        result = simulate(values, i, j, total, **cfg)
        if result is not None:
            out.append(result)
    if not out:
        return None
    frame = pd.DataFrame(out)
    factor = 252.0 / cfg["hold"]
    capital = frame["资金收益率"]
    return {
        "笔数": len(frame),
        "资金收益率均值": capital.mean(),
        "年化(资金)": ((1 + capital / 100) ** factor - 1).mean() * 100,
        "中位数": capital.median(),
        "胜率": (capital > 0).mean() * 100,
        "止损比例": frame["止损"].mean() * 100,
        "平均持有天数": frame["持有天数"].mean(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="分批低吸策略回测")
    parser.add_argument("--start", default="20231009")
    parser.add_argument("--end", default="20260918")
    parser.add_argument("--tranches", type=int, default=3, help="分几笔建仓")
    parser.add_argument("--gap", type=int, default=5, help="每笔间隔交易日（5≈一周）")
    parser.add_argument("--hold", type=int, default=60, help="建仓起持有交易日")
    parser.add_argument("--stop", type=float, default=7.0, help="止损幅度 %")
    parser.add_argument("--tp-step", type=float, default=10.0, help="止盈台阶 %")
    parser.add_argument("--tp-frac", type=float, default=0.5, help="每台阶卖出比例")
    parser.add_argument("--min-coverage", type=float, default=0.9)
    parser.add_argument("--out", default="docs/MAINLINE_STRATEGY_BACKTEST.xlsx")
    parser.add_argument("--sweep", action="store_true",
                        help="扫描止损/止盈参数，看是策略不行还是参数不对")
    args = parser.parse_args()

    wide = load(args.start, args.end, args.min_coverage)
    values = wide.to_numpy(dtype=float)
    total = values.shape[0]
    pos_of = {d: i for i, d in enumerate(wide.index)}
    col_of = {c: j for j, c in enumerate(wide.columns)}
    print(f"universe：池内且覆盖 ≥{args.min_coverage:.0%} 的板块 "
          f"{wide.shape[1]} 个 / {total} 个交易日")

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = conn.execute(
        "SELECT trade_date, board_code, board_name, level, score"
        " FROM mainline_alert WHERE trade_date BETWEEN ? AND ?"
        " ORDER BY trade_date", (args.start, args.end)).fetchall()
    conn.close()

    # 先解析出全部 `(i, j)`，扫描与正式跑复用同一批
    pairs: list[tuple[int, int]] = []
    for alert in alerts:
        i = pos_of.get(str(alert["trade_date"]))
        j = col_of.get(str(alert["board_code"]))
        if i is not None and j is not None:
            pairs.append((i, j))

    if args.sweep:
        print()
        print("=" * 100)
        print("参数扫描：止损 × 止盈台阶（其余不变：3 笔 ×1 成、间隔 5 日、"
              "持有 60 日、每档卖 50%）")
        print("-" * 100)
        print(f"{'止损%':>7}{'止盈台阶%':>11}{'笔数':>7}{'资金收益率均值':>15}"
              f"{'年化(资金)':>12}{'胜率':>8}{'止损比例':>10}{'平均持有天数':>13}")
        grid: list[dict] = []
        for stop in (7.0, 10.0, 12.0, 15.0, 20.0, 1e9):
            for tp in (10.0, 15.0, 20.0, 1e9):
                stat = evaluate(pairs, values, total,
                                tranches=args.tranches, gap=args.gap,
                                hold=args.hold, stop=stop, tp_step=tp,
                                tp_frac=args.tp_frac, deploy=True)
                if stat is None:
                    continue
                grid.append({"止损": stop, "止盈台阶": tp, **stat})
                label = "不止损" if stop > 1e8 else f"{stop:g}"
                tp_label = "不止盈" if tp > 1e8 else f"{tp:g}"
                print(f"{label:>7}{tp_label:>11}{stat['笔数']:>7}"
                      f"{stat['资金收益率均值']:>15.2f}"
                      f"{stat['年化(资金)']:>12.2f}{stat['胜率']:>7.1f}%"
                      f"{stat['止损比例']:>9.1f}%"
                      f"{stat['平均持有天数']:>13.1f}")
        grid_frame = pd.DataFrame(grid)
        target = ROOT / "docs" / "MAINLINE_STRATEGY_SWEEP.xlsx"
        grid_frame.to_excel(target, index=False)
        print(f"\n扫描结果 → {target}")
        best = grid_frame.sort_values("年化(资金)", ascending=False).head(3)
        print("\n按年化(资金) 排序前 3：")
        print(best[["止损", "止盈台阶", "资金收益率均值", "年化(资金)",
                    "胜率", "止损比例"]].to_string(index=False))
        return 0

    rows: list[dict] = []
    for alert in alerts:
        i = pos_of.get(str(alert["trade_date"]))
        j = col_of.get(str(alert["board_code"]))
        if i is None or j is None:
            continue
        result = simulate(values, i, j, total, tranches=args.tranches,
                          gap=args.gap, hold=args.hold, stop=args.stop,
                          tp_step=args.tp_step, tp_frac=args.tp_frac)
        if result is None:
            continue
        rows.append({"触发日": str(alert["trade_date"]),
                     "板块代码": str(alert["board_code"]),
                     "板块名称": str(alert["board_name"]),
                     "等级": str(alert["level"]), **result})
    frame = pd.DataFrame(rows)
    print(f"可模拟 {len(frame)} 笔（告警 {len(alerts)} 条，"
          f"{len(alerts) - len(frame)} 条未来数据不足或不在 universe）")

    # ---------- 基准：同样规则但一次性买入、且不设止损止盈 ----------
    bench_rows: list[dict] = []
    for alert in alerts[::20]:            # 抽样，基准只需量级
        i = pos_of.get(str(alert["trade_date"]))
        j = col_of.get(str(alert["board_code"]))
        if i is None or j is None:
            continue
        result = simulate(values, i, j, total, tranches=1, gap=args.gap,
                          hold=args.hold, stop=1e9, tp_step=1e9,
                          tp_frac=0.0, deploy=False)
        if result is not None:
            bench_rows.append(result)
    bench = pd.DataFrame(bench_rows)

    period = args.hold
    factor = 252.0 / period

    def annualize(series: pd.Series) -> pd.Series:
        return (1.0 + series / 100.0) ** factor - 1.0

    ann = annualize(frame["资金收益率"]) * 100
    frame["年化(资金)"] = ann
    hold_ann = ((1.0 + frame["持仓收益率"] / 100.0) ** factor - 1.0) * 100

    print()
    print("=" * 90)
    print(f"策略：{args.tranches} 笔 × 1 成，间隔 {args.gap} 个交易日，"
          f"持有 {args.hold} 日，止损 {args.stop:g}%，"
          f"每 +{args.tp_step:g}% 卖 {args.tp_frac:.0%}")
    print("-" * 90)
    print(f"  {'':<18}{'均值':>10}{'中位数':>10}{'胜率':>8}{'10分位':>9}{'90分位':>9}")
    for label, series in (("资金收益率 %", frame["资金收益率"]),
                          ("持仓收益率 %", frame["持仓收益率"]),
                          ("年化(资金) %", frame["年化(资金)"]),
                          ("年化(持仓) %", hold_ann)):
        print(f"  {label:<18}{series.mean():>10.2f}{series.median():>10.2f}"
              f"{(series > 0).mean() * 100:>7.1f}%"
              f"{series.quantile(0.1):>9.2f}{series.quantile(0.9):>9.2f}")
    print(f"\n  止损触发比例      {frame['止损'].mean() * 100:.1f}%")
    print(f"  平均实际持有天数  {frame['持有天数'].mean():.1f}"
          f"（上限 {args.hold}）")
    print(f"  峰值投入          {frame['峰值投入'].iloc[0]:.0f}% 总资金")
    if not bench.empty:
        bench_ann = annualize(bench["资金收益率"]).mean() * 100
        print(f"\n  基准（同规则、一次性买入、无止损止盈）"
              f"资金收益率均值 {bench['资金收益率'].mean():.2f}%"
              f" / 年化 {bench_ann:.2f}%")

    # 分档 / 分年
    by_level = frame.groupby("等级").agg(
        笔数=("资金收益率", "size"),
        资金收益率均值=("资金收益率", "mean"),
        年化资金=("年化(资金)", "mean"),
        胜率=("资金收益率", lambda s: (s > 0).mean() * 100),
        止损比例=("止损", lambda s: s.mean() * 100)).round(2).reset_index()
    frame["年"] = frame["触发日"].str[:4]
    by_year = frame.groupby("年").agg(
        笔数=("资金收益率", "size"),
        资金收益率均值=("资金收益率", "mean"),
        年化资金=("年化(资金)", "mean"),
        胜率=("资金收益率", lambda s: (s > 0).mean() * 100)).round(2).reset_index()

    notes = pd.DataFrame({"口径": [
        "建仓", "仓位", "止损", "止盈", "收尾", "年化",
        "资金收益率", "持仓收益率", "基准", "重要局限",
    ], "说明": [
        f"第 0/{args.gap}/{2 * args.gap} 个交易日收盘各买 1 成"
        "（「低吸」无给定触发条件，只能按固定间隔）",
        f"{args.tranches} 成 = 总资金 {args.tranches * 10}%",
        f"建满三成后，收盘价 ≤ 均价×(1−{args.stop:g}%) 清仓",
        f"每上一个 {args.tp_step:g}% 台阶卖出现有的一半；同日可跨多台阶",
        f"第 {args.hold} 个交易日收盘清掉剩余",
        f"(1+资金收益率)^(252/{args.hold})−1，**忽略资金闲置与信号重叠**",
        "盈亏 / 总资金（本金只用出 30%）",
        "盈亏 / 峰值投入（常说的「这笔赚几个点」）",
        "同规则但一次性买入、不设止损止盈；抽样告警",
        "日均 11.6 条告警、持有 60 日 → 信号高度重叠，"
        "**平均年化是单笔口径，不是组合可实现收益**",
    ]})

    target = ROOT / args.out
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="逐笔明细", index=False)
        by_level.to_excel(writer, sheet_name="分档", index=False)
        by_year.to_excel(writer, sheet_name="分年", index=False)
        notes.to_excel(writer, sheet_name="口径说明", index=False)
        for sheet in writer.book.worksheets:
            for column in sheet.columns:
                width = max((len(str(c.value)) for c in column
                             if c.value is not None), default=8)
                sheet.column_dimensions[column[0].column_letter].width = \
                    min(max(width + 2, 10), 46)
    print(f"\nExcel → {target}")
    print("\n分档（按资金收益率）：")
    print(by_level.to_string(index=False))
    print("\n分年（按资金收益率）：")
    print(by_year.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
