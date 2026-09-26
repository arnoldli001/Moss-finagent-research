"""在**入选池**上回测：入池 TOP3 → 筛「昨日连板 ≥3 板」→ 竞价买入。

## 与 `backtest_overnight_relay.py` 的关键区别

那一版直接用了 `auction_dataset.parquet`，而它**只是候选池**（上一交易日涨停 +
市值/价格/板块/ST 过滤），**不含前置筛选、不含否决、也没有分数** ——
也就是说它**包含被否决的股，也包含分数没达标的股**。在它上面跑出来的结果
不能叫"入选池"。

本脚本**现场构建入选池**：

    候选池 → ① 前置筛选（rulebook 的口径）
           → ② 否决（bit2/3/11/12/13 + 有过程数据时的 bit4/6/7）
           → ③ 按分数排序取 TOP3（分数用「竞价涨幅」作代理，见下）
           → ④ 筛「昨日连板 ≥ N 板」
           → ⑤ 模拟买卖（用户口径）

## ⚠️ 三个必须知道的口径限制

1. **分数是代理**：生产真实分数含「承接强度 / 封流比 / 题材热度」等只有竞价
   快照才有的维度；这里用 **竞价涨幅降序** 代替。所以"TOP3"不等于生产 TOP3。
2. **连板高度在本脚本内重算**：`auction_dataset.prev_streak` 对 0918~0922 是空的
   （那三天的日线是后补的），这里从日线 run-length 重算，覆盖全区间。
3. **A 组样本极小**：入选池 TOP3 里恰好连板 ≥3 的极少（预计 ~20 笔）。

## 用法

    .venv\\Scripts\\python.exe scripts/backtest_pool_relay.py --streak 3 --top 3
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_auction import (  # noqa: E402
    OUT_DIR,
    _is_chinext,
    _is_main_board,
    _is_star,
    apply_prefilter,
    apply_vetoes,
)
from backtest_overnight_relay import (  # noqa: E402
    ROUND_TRIP_FEE,
    START_CASH,
    WEIGHT,
    load_bars,
    perf_metrics,
    simulate_one,
)

DATA = ROOT / "data" / "auction_hist" / "auction_dataset.parquet"
MA_WINDOWS = (20, 60, 120, 250)


def run_length(s: pd.Series) -> pd.Series:
    """连续涨停天数（遇非涨停归零）。"""
    out, run = [], 0
    for v in s.to_numpy():
        run = (run + 1) if v else 0
        out.append(run)
    return pd.Series(out, index=s.index, dtype="float64")


def add_ma(bars: pd.DataFrame) -> pd.DataFrame:
    """算均线（截至当日收盘，判昨日用 shift(1) 后的那根）。"""
    b = bars.sort_values(["code", "trade_date"]).copy()
    g = b.groupby("code", sort=False)["close"]
    for w in MA_WINDOWS:
        b[f"ma{w}"] = g.transform(
            lambda s, w=w: s.rolling(w, min_periods=w).mean()).groupby(
            b["code"], sort=False).shift(1)
    return b


def main() -> int:
    ap = argparse.ArgumentParser(description="入选池 TOP3 × 昨日≥N板 隔夜接力回测")
    ap.add_argument("--streak", type=int, default=3)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    from src.auction_select import config as auction_config

    cfg = auction_config.load_config()
    ds = pd.read_parquet(DATA)
    ds["trade_date"] = ds["trade_date"].astype(str)
    ds["code"] = ds["code"].astype(str).str.zfill(6)
    bars = load_bars()
    bars = add_ma(bars)

    # ── 连板高度在本脚本内重算（覆盖 0918~0922）──
    b = bars.sort_values(["code", "trade_date"]).copy()
    b["_run"] = b.groupby("code", sort=False)["涨停"].transform(run_length)
    b["prev_streak2"] = b.groupby("code", sort=False)["_run"].shift(1).fillna(0)
    ds = ds.drop(columns=["prev_streak"], errors="ignore").merge(
        b[["trade_date", "code", "prev_streak2"]].rename(
            columns={"prev_streak2": "prev_streak"}),
        on=["trade_date", "code"], how="left")
    ds["prev_streak"] = pd.to_numeric(ds["prev_streak"], errors="coerce").fillna(0)

    # ── 拼成规则引擎要的特征矩阵 ──
    f = ds.merge(b[["trade_date", "code"] + [f"ma{w}" for w in MA_WINDOWS]],
                 on=["trade_date", "code"], how="left")
    f["is_main_board"] = f["code"].map(lambda c: 1.0 if _is_main_board(c) else 0.0)
    f["is_chinext"] = f["code"].map(lambda c: 1.0 if _is_chinext(c) else 0.0)
    f["is_star"] = f["code"].map(lambda c: 1.0 if _is_star(c) else 0.0)
    f["prev_sealed"] = 1
    f["bar_no"] = 999
    f["name"] = ""
    f["limit_up_days"] = pd.to_numeric(f["limit_up_days_18"], errors="coerce")
    f["prev_close"] = pd.to_numeric(f["pre_close"], errors="coerce")
    f["circ_mv"] = pd.to_numeric(f["circ_mv"], errors="coerce")
    f["open_gap_pct"] = pd.to_numeric(f["open_gap_pct"], errors="coerce")
    f["auction_price"] = pd.to_numeric(f["auction_price"], errors="coerce").fillna(
        pd.to_numeric(f["open"], errors="coerce"))

    print("=" * 104)
    print(f"【入选池口径】候选 → 前置筛选 → 否决 → TOP{args.top} → 连板≥{args.streak} → "
          f"竞价买入｜每只 {WEIGHT * 100:.2f}% 仓｜磨损 {ROUND_TRIP_FEE * 100:.1f}%")
    print("=" * 104)
    print(f"候选池 {len(ds):,} 股日 / {ds['trade_date'].nunique()} 天 "
          f"（{ds['trade_date'].min()} ~ {ds['trade_date'].max()}）")

    # ── 逐日构建入选池 ──
    pool_rows: list[dict[str, Any]] = []
    stat_rows: list[dict[str, Any]] = []
    for day, sub in f.groupby("trade_date"):
        pre = apply_prefilter(sub, cfg)
        if not len(pre.kept):
            stat_rows.append({"交易日": day, "候选": len(sub), "前置筛选后": 0,
                              "否决后": 0, "入池": 0, "池内连板≥N": 0})
            continue
        alive, _hits, _un = apply_vetoes(pre.kept, cfg)
        ranked = alive.sort_values("open_gap_pct", ascending=False).reset_index(drop=True)
        top = ranked.head(args.top)
        # ⚠️ 必须用 `top` 自己的索引来标记（`ranked` 已 reset_index，拿原索引比对会全 False）。
        #    实测踩过：标记 0 笔、整个回测空跑。
        picked_idx = top.index[pd.to_numeric(top["prev_streak"], errors="coerce")
                               .fillna(0) >= args.streak]
        picks = ranked.loc[picked_idx]
        stat_rows.append({"交易日": day, "候选": len(sub), "前置筛选后": len(pre.kept),
                          "否决后": len(alive), "入池": len(ranked),
                          "池内连板≥N": len(picks)})
        if len(ranked):
            r = ranked.copy()
            r["_day"] = day
            r["_rank"] = range(1, len(r) + 1)
            r["_picked"] = r.index.isin(picked_idx)
            pool_rows.append(r)

    pool = pd.concat(pool_rows, ignore_index=True) if pool_rows else pd.DataFrame()
    stats = pd.DataFrame(stat_rows)
    print(f"入选池（所有入池票）{len(pool):,} 股日")
    print(f"其中「TOP{args.top} 且连板≥{args.streak}」{int(pool['_picked'].sum())} 笔")
    print(f"逐日：入池合计 {int(stats['入池'].sum()):,}、命中 {int(stats['池内连板≥N'].sum())}")

    # ── 分 A/B 两组模拟 ──
    picked = pool[pool["_picked"]].copy()
    results = []
    for label, g in (("A 有竞价过程", picked[picked["pattern"].notna()]),
                     ("B 无竞价过程", picked[picked["pattern"].isna()]),
                     ("全部（A+B）", picked)):
        if not len(g):
            continue
        by_code = {c: x.reset_index(drop=True)
                   for c, x in bars.groupby("code", sort=False)}
        trades, cash = [], START_CASH
        curve_rows = []
        for day, sub in g.groupby("trade_date"):
            day_pnl = 0.0
            for rec in sub.to_dict("records"):
                tr = simulate_one(by_code, str(rec["code"]).zfill(6), str(day),
                                  cash * WEIGHT)
                if tr is not None:
                    trades.append(tr)
                    day_pnl += tr.pnl
            cash += day_pnl
            curve_rows.append({"trade_date": day, "equity": cash, "pnl": day_pnl})
        curve = pd.DataFrame(curve_rows).sort_values("trade_date").reset_index(drop=True)
        if len(curve):
            all_days = sorted(set(bars["trade_date"]))
            span = [d for d in all_days
                    if curve["trade_date"].min() <= d <= curve["trade_date"].max()]
            curve = pd.DataFrame({"trade_date": span}).merge(curve, on="trade_date",
                                                              how="left")
            curve["equity"] = curve["equity"].ffill().fillna(START_CASH)
            curve["pnl"] = curve["pnl"].fillna(0.0)
        m: dict[str, Any] = {"口径": label, "开仓笔数": len(trades)}
        m.update(perf_metrics(curve))
        if trades:
            rets = np.array([t.ret_pct for t in trades])
            m["单笔均收益%"] = round(float(rets.mean()), 2)
            m["单笔胜率%"] = round(float((rets > 0).mean()) * 100, 2)
            m["单笔最好%"] = round(float(rets.max()), 2)
            m["单笔最差%"] = round(float(rets.min()), 2)
        results.append((m, trades, curve, label))

    st = pd.DataFrame([m for m, _t, _c, _l in results])
    order = ["口径", "开仓笔数", "起始资金", "期末资金", "总收益率%", "最大回撤%",
             "夏普率", "索提诺", "日胜率%", "交易日数", "单笔均收益%", "单笔胜率%",
             "单笔最好%", "单笔最差%"]
    print()
    print(st[[c for c in order if c in st.columns]].to_string(index=False))

    for m, trades, curve, label in results:
        if not trades:
            continue
        t = pd.DataFrame([x.__dict__ for x in trades])
        print()
        print(f"── {label} 卖出原因分布（{len(t)} 笔）──")
        print(t["exit_bucket"].value_counts().to_string())
        out = OUT_DIR / f"pool_top{args.top}_streak{args.streak}_{label[0]}.xlsx"
        try:
            with pd.ExcelWriter(out, engine="openpyxl") as xw:
                st.to_excel(xw, sheet_name="总览", index=False)
                t.to_excel(xw, sheet_name="逐笔明细", index=False)
                curve.to_excel(xw, sheet_name="资金曲线", index=False)
                pool.to_excel(xw, sheet_name="入选池", index=False)
                stats.to_excel(xw, sheet_name="逐日筛选", index=False)
            print(f"   已写出 {out}")
        except Exception as exc:                               # noqa: BLE001
            print(f"   （Excel 写出失败：{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
