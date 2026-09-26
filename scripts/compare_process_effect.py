"""受控对比：**同样的日子**上，竞价过程规则（bit4/6/7）到底有没有用。

## 为什么需要它

`pool_top3_streak3_A` 与 `_B` 的对比**不是受控实验**：
A 组是 20260826~20260916 那 12 个开仓日、B 组是 20250710~20260918 共 293 笔，
而 A 组区间内 B 组**只有 1 笔** —— 两组时间不重叠，比的其实是"两个不同时段的市场"。

本脚本在**同一批日子**（有 tick 覆盖的那 22 天）上跑两遍：

    ① 只判 bit2/3/11/12/13（= 无过程口径，等同 B 组规则）
    ② 再叠加 bit4/6/7（有过程口径）

两遍用**同一批候选、同一个 TOP3 规则、同一个买卖模拟**，唯一变量就是那三条
过程规则。这样才回答得了"过程规则有没有用"。
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
from backtest_pool_relay import DATA, MA_WINDOWS, add_ma, run_length  # noqa: E402


def build_features() -> pd.DataFrame:
    ds = pd.read_parquet(DATA)
    ds["trade_date"] = ds["trade_date"].astype(str)
    ds["code"] = ds["code"].astype(str).str.zfill(6)
    bars = add_ma(load_bars())
    b = bars.sort_values(["code", "trade_date"]).copy()
    b["_run"] = b.groupby("code", sort=False)["涨停"].transform(run_length)
    b["prev_streak2"] = b.groupby("code", sort=False)["_run"].shift(1).fillna(0)
    ds = ds.drop(columns=["prev_streak"], errors="ignore").merge(
        b[["trade_date", "code", "prev_streak2"]].rename(
            columns={"prev_streak2": "prev_streak"}),
        on=["trade_date", "code"], how="left")
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
    f["prev_streak"] = pd.to_numeric(f["prev_streak"], errors="coerce").fillna(0)
    return f, bars


def pool_for(f: pd.DataFrame, cfg: Any, *, use_process: bool, top: int,
             streak: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构建入选池并选出「TOP n 且连板≥streak」的票。

    `use_process=False` 时把过程列**置空**，使 bit4/6/7 判不了 → 等同无过程口径。
    """
    rows, stats = [], []
    for day, sub in f.groupby("trade_date"):
        s = sub
        if not use_process:
            s = sub.copy()
            for c in ("pattern", "jump_value", "rush_labels", "auction_volume_ratio"):
                if c in s.columns:
                    s[c] = None
        pre = apply_prefilter(s, cfg)
        if not len(pre.kept):
            stats.append({"交易日": day, "入池": 0, "命中": 0})
            continue
        alive, _h, _u = apply_vetoes(pre.kept, cfg)
        ranked = alive.sort_values("open_gap_pct", ascending=False).reset_index(drop=True)
        topd = ranked.head(top)
        idx = topd.index[pd.to_numeric(topd["prev_streak"], errors="coerce")
                         .fillna(0) >= streak]
        stats.append({"交易日": day, "入池": len(ranked), "命中": len(idx)})
        if len(ranked):
            r = ranked.copy()
            r["_day"] = day
            r["_picked"] = r.index.isin(idx)
            rows.append(r)
    pool = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return pool, pd.DataFrame(stats)


def simulate(picked: pd.DataFrame, bars: pd.DataFrame) -> dict[str, Any]:
    by_code = {c: x.reset_index(drop=True) for c, x in bars.groupby("code", sort=False)}
    trades, cash = [], START_CASH
    curve_rows = []
    for day, sub in picked.groupby("trade_date"):
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
        span = [d for d in sorted(set(bars["trade_date"]))
                if curve["trade_date"].min() <= d <= curve["trade_date"].max()]
        curve = pd.DataFrame({"trade_date": span}).merge(curve, on="trade_date",
                                                          how="left")
        curve["equity"] = curve["equity"].ffill().fillna(START_CASH)
        curve["pnl"] = curve["pnl"].fillna(0.0)
    m: dict[str, Any] = {"开仓笔数": len(trades)}
    m.update(perf_metrics(curve))
    if trades:
        rets = np.array([t.ret_pct for t in trades])
        m["单笔均收益%"] = round(float(rets.mean()), 2)
        m["单笔胜率%"] = round(float((rets > 0).mean()) * 100, 2)
    return {"metrics": m, "trades": trades, "curve": curve}


def main() -> int:
    ap = argparse.ArgumentParser(description="受控对比：过程规则有没有用")
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
    f, bars = build_features()
    proc_days = sorted(f.loc[f["pattern"].notna(), "trade_date"].unique())
    print("=" * 104)
    print(f"受控对比：同一批 {len(proc_days)} 个有过程数据的交易日"
          f"（{proc_days[0]} ~ {proc_days[-1]}）")
    print("=" * 104)

    out = {}
    for name, use in (("无过程口径（只判 bit2/3/11/12/13）", False),
                      ("有过程口径（叠加 bit4/6/7）", True)):
        f2 = f[f["trade_date"].isin(proc_days)]
        pool, stats = pool_for(f2, cfg, use_process=use, top=args.top,
                               streak=args.streak)
        picked = pool[pool["_picked"]] if len(pool) else pd.DataFrame()
        r = simulate(picked, bars) if len(picked) else {"metrics": {"开仓笔数": 0},
                                                        "trades": [], "curve": None}
        out[name] = {**r, "pool": pool, "stats": stats, "picked": picked}
        print(f"\n◆ {name}：入池 {len(pool):,} 股日、命中 {len(picked)} 笔")
        print(pd.DataFrame([{**{'口径': name}, **r['metrics']}]).to_string(index=False))

    st = pd.DataFrame([{"口径": k, **v["metrics"]} for k, v in out.items()])
    order = ["口径", "开仓笔数", "起始资金", "期末资金", "总收益率%", "最大回撤%",
             "夏普率", "索提诺", "日胜率%", "单笔均收益%", "单笔胜率%"]
    print()
    print("=" * 104)
    print(st[[c for c in order if c in st.columns]].to_string(index=False))

    # 过程规则拦掉了哪些票
    print()
    for name, v in out.items():
        if v["trades"]:
            t = pd.DataFrame([x.__dict__ for x in v["trades"]])
            print(f"── {name} 卖出原因（{len(t)} 笔）──")
            print(t["exit_bucket"].value_counts().to_string())

    out_x = OUT_DIR / f"controlled_process_top{args.top}_streak{args.streak}.xlsx"
    try:
        with pd.ExcelWriter(out_x, engine="openpyxl") as xw:
            st.to_excel(xw, sheet_name="总览", index=False)
            for name, v in out.items():
                tag = "有过程" if "有过程口径" in name else "无过程"
                if v["trades"]:
                    pd.DataFrame([x.__dict__ for x in v["trades"]]).to_excel(
                        xw, sheet_name=f"逐笔_{tag}", index=False)
                if len(v["picked"]):
                    cols = [c for c in ("_day", "code", "prev_streak", "open_gap_pct",
                                        "pattern", "jump_value", "rush_labels")
                            if c in v["picked"].columns]
                    v["picked"][cols].to_excel(xw, sheet_name=f"选中_{tag}", index=False)
        print(f"\n已写出 {out_x}")
    except Exception as exc:                                   # noqa: BLE001
        print(f"（Excel 写出失败：{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
