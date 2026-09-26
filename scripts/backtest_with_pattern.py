"""竞价选股回测（**含竞价过程规则**）—— 吃统一数据集，输出正确率。

## 与 `backtest_auction.py` 的区别

| | `backtest_auction.py` | 本脚本 |
|---|---|---|
| 数据来源 | 仓库日线 + QMT，回测时现拼 | **统一数据集** `auction_dataset.parquet` |
| bit4（竞价量比） | ❌ 未重建 | ✅ 1 分钟线竞价摘要，全年可用 |
| bit6（急速下坠型） | ❌ 未重建 | ✅ tick 过程覆盖的日子 |
| bit7（抢跑） | ❌ 未重建 | ✅ tick 过程覆盖的日子 |
| 判不了的规则 | 静默通过 | **单独计数**，绝不假装通过 |

## 为什么必须区分"没判"和"通过"

这是上一轮把 豪尔赛/金帝股份 放进池子的根因：回测只重建了 6 条规则，
bit6/bit7 根本没跑，但结果表上它们看起来"通过了一切检查"。
现在 `apply_vetoes` 会把判不了的**行数**单独报出来，写进「判不了」统计里。

## 用法

    .venv\\Scripts\\python.exe scripts/backtest_with_pattern.py --top 5
    .venv\\Scripts\\python.exe scripts/backtest_with_pattern.py --top 5 --top3
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
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
    format_detail,
    group_breakdown,
    summarize,
    top_n_by_day,
)

logger = logging.getLogger("backtest_with_pattern")

DATA = ROOT / "data" / "auction_hist" / "auction_dataset.parquet"
DB = ROOT / "data" / "quant" / "warehouse.db"
#: 均线窗口（规则 B 用 60/120/250，前置筛选用 20）
MA_WINDOWS = (20, 60, 120, 250)


def load_daily_for_ma(start: str = "20250101") -> pd.DataFrame:
    """取日线收盘价算均线（统一数据集里没有均线）。

    ⚠️ 仓库 `quant_daily` 只到 20260917，而候选已到 20260922 —— 不补的话
    最近几天的 `ma20/60/120/250` 全是 NaN，**前置筛选会把它们整片剔掉**
    （实测：主表已到 0922，但回测样本数一行没涨）。所以分区文件也要一起读。
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        d = pd.read_sql(
            "SELECT trade_date, code, close FROM quant_daily WHERE trade_date >= ?",
            conn, params=[start])
    finally:
        conn.close()
    d["trade_date"] = d["trade_date"].astype(str)
    d["code"] = d["code"].astype(str).str.zfill(6)
    # 补 tushare 分区里、仓库还没有的交易日（原始价）
    base = ROOT / "data" / "quant" / "tushare" / "a_share" / "daily"
    have = set(d["trade_date"])
    parts = []
    for p in sorted(base.glob("*.parquet")):
        day = p.stem
        if day < start or day in have:
            continue
        q = pd.read_parquet(p, columns=["trade_date", "code", "close"])
        q["trade_date"] = q["trade_date"].astype(str)
        q["code"] = q["code"].astype(str).str.zfill(6)
        parts.append(q)
    if parts:
        d = pd.concat([d] + parts, ignore_index=True)
        logger.warning("均线数据补入分区：%s", sorted(p.stem for p in base.glob("*.parquet")
                                                   if p.stem >= start and p.stem not in have))
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.sort_values(["code", "trade_date"])
    g = d.groupby("code", sort=False)["close"]
    for w in MA_WINDOWS:
        # 截至**当日**收盘的均线（判昨日用 shift(1) 之后的那根）
        d[f"ma{w}"] = g.transform(
            lambda s, w=w: s.rolling(w, min_periods=w).mean()).groupby(
            d["code"], sort=False).shift(1)
    return d


def build_feature_frame(ds: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """把统一数据集拼成规则引擎要的特征矩阵。"""
    f = ds.merge(daily[["trade_date", "code"] + [f"ma{w}" for w in MA_WINDOWS]],
                 on=["trade_date", "code"], how="left")
    f["is_main_board"] = f["code"].map(lambda c: 1.0 if _is_main_board(c) else 0.0)
    f["is_chinext"] = f["code"].map(lambda c: 1.0 if _is_chinext(c) else 0.0)
    f["is_star"] = f["code"].map(lambda c: 1.0 if _is_star(c) else 0.0)
    # 候选集本身 = "上一交易日涨停"，所以 prev_sealed 恒为 1
    f["prev_sealed"] = 1
    f["bar_no"] = 999                          # 上市满 5 日的代理（候选已是老票）
    f["name"] = ""
    # 统一口径：`limit_up_days` 用 18 日窗口（生产 `veto_first_board_lookback_days`）
    if "limit_up_days_18" in f.columns:
        f["limit_up_days"] = pd.to_numeric(f["limit_up_days_18"], errors="coerce")
    # 事前筛选要用的派生量
    f["circ_mv"] = pd.to_numeric(f["circ_mv"], errors="coerce")
    f["prev_close"] = pd.to_numeric(f["pre_close"], errors="coerce")
    f["auction_price"] = pd.to_numeric(f["auction_price"], errors="coerce")
    f["auction_price"] = f["auction_price"].fillna(
        pd.to_numeric(f["open"], errors="coerce"))
    f["intraday_pct"] = (pd.to_numeric(f["close"], errors="coerce")
                         / f["auction_price"] - 1) * 100
    ul = pd.to_numeric(f["up_limit"], errors="coerce")
    cl = pd.to_numeric(f["close"], errors="coerce")
    f["sealed"] = (cl - ul).abs() < 0.005
    f["up_limit_close"] = f["sealed"]
    f["limit_up_price"] = ul
    # 次日：候选集里没有"次日"，置空（本脚本只报当日口径）
    f["next_day_pct"] = np.nan
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description="含竞价过程规则的回测")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    if not DATA.exists():
        print(f"缺统一数据集 {DATA.name}，请先跑 build_auction_dataset.py")
        return 1
    ds = pd.read_parquet(DATA)
    ds["trade_date"] = ds["trade_date"].astype(str)
    ds["code"] = ds["code"].astype(str).str.zfill(6)
    if args.start:
        ds = ds[ds["trade_date"] >= args.start]
    if args.end:
        ds = ds[ds["trade_date"] <= args.end]
    print("=" * 104)
    print(f"竞价选股回测（含竞价过程规则） 候选 {len(ds):,} 股日 / "
          f"{ds['trade_date'].nunique()} 个交易日 "
          f"（{ds['trade_date'].min()} ~ {ds['trade_date'].max()}）")
    print("=" * 104)

    daily = load_daily_for_ma()
    feat = build_feature_frame(ds, daily)

    from src.auction_select import config as auction_config

    cfg = auction_config.load_config()

    ranked: list[pd.DataFrame] = []
    day_rows: list[dict[str, Any]] = []
    for day, sub in feat.groupby("trade_date"):
        pre = apply_prefilter(sub, cfg)
        if not len(pre.kept):
            day_rows.append({"交易日": day, "候选": len(sub), "前置筛选后": 0,
                             "否决后": 0, "入池": 0})
            continue
        alive, hits, unjudge = apply_vetoes(pre.kept, cfg)
        day_rows.append({
            "交易日": day, "候选": len(sub), "前置筛选后": len(pre.kept),
            "否决后": len(alive), "入池": min(len(alive), args.top),
            "有过程数据": int(pre.kept.get("pattern", pd.Series()).notna().sum())
            if "pattern" in pre.kept.columns else 0,
            "形态否决": hits.get("bit6 竞价形态「急剧下坠型」", 0),
            "抢跑硬线": hits.get("bit7 抢跑（硬线 跳空 < 0.97，无豁免）", 0),
            "抢跑常规": hits.get("bit7 抢跑（常规档 跳空 < 0.99 且 量比 > 10%）", 0),
        })
        if len(alive):
            r = alive.reset_index(drop=True).copy()
            r["_day"] = day
            r["_rank"] = range(1, len(r) + 1)
            ranked.append(r)

    if not ranked:
        print("没有选出任何票")
        return 1
    allr = pd.concat(ranked, ignore_index=True)
    day_table = pd.DataFrame(day_rows)
    picks = top_n_by_day(allr, args.top)
    picks["年"] = picks["trade_date"].str[:4]

    # 分层：过程覆盖的日子 vs 没覆盖的日子 —— **分开统计，不混在一起**
    has_proc = (picks["pattern"].notna() if "pattern" in picks.columns
                else pd.Series(False, index=picks.index))
    picks_a = picks[has_proc]      # bit4/6/7 真跑了
    picks_b = picks[~has_proc]     # bit4/6/7 未判
    print()
    print("─" * 104)
    print(f"【总览】每日前 {args.top}（竞价买入、当日收盘卖）"
          f"；主板口径（创业板已在候选阶段剔除）")
    print("─" * 104)
    rows = [
        summarize(picks, "全部入池"),
        summarize(picks_a, "　└ A 有竞价过程（bit4/6/7 生效）"),
        summarize(picks_b, "　└ B 无过程数据（bit4/6/7 未判）"),
    ]
    print(pd.DataFrame(rows).to_string(index=False))

    print()
    print("─" * 104)
    print("【A / B 两组分别分档】")
    print("─" * 104)
    groups: dict[str, pd.DataFrame] = {}
    for name, frame in (("A 有过程数据", picks_a), ("B 无过程数据", picks_b)):
        g = group_breakdown(frame)
        groups[name] = g
        print(f"\n◆ {name}（{len(frame)} 只）")
        print(g.to_string(index=False) if len(g) else "（无）")

    print()
    print("─" * 104)
    print("【竞价过程规则的拦截效果】（只在有过程数据的日子可判）")
    print("─" * 104)
    tot = day_table[["形态否决", "抢跑硬线", "抢跑常规"]].sum()
    print(f"  形态否决（bit6）{int(tot['形态否决'])} 只、"
          f"抢跑硬线（bit7）{int(tot['抢跑硬线'])} 只、"
          f"抢跑常规（bit7）{int(tot['抢跑常规'])} 只")
    proc_days = day_table[day_table["有过程数据"] > 0]
    print(f"  有过程数据的日子 {len(proc_days)} / {len(day_table)} 天；"
          f"无过程数据的日子 bit4/6/7 **未判**（不是通过）")

    print()
    print("─" * 104)
    print("【逐日】")
    print("─" * 104)
    print(day_table.to_string(index=False))

    # ⚠️ 输出文件名必须带口径。曾经固定叫 `auction_pattern_backtest_top{top}.xlsx`，
    #    但 `--top` 只在文件名里体现，跑完 top5 再跑 top3 时**前一个文件被覆盖**
    #    （用户反馈："数据重名被覆盖了"）。现在文件名与 sheet 名都带 topN。
    out = OUT_DIR / (f"auction_pattern_backtest_top{args.top}_"
                     f"{ds['trade_date'].min()}_{ds['trade_date'].max()}.xlsx")
    try:
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            pd.DataFrame(rows).to_excel(xw, sheet_name=f"top{args.top}_总览", index=False)
            for name, g in groups.items():
                if len(g):
                    tag = "A有过程" if name.startswith("A") else "B无过程"
                    g.to_excel(xw, sheet_name=f"top{args.top}_分档_{tag}", index=False)
            day_table.to_excel(xw, sheet_name=f"top{args.top}_逐日", index=False)
            for name, frame in (("全部", picks), ("A有过程", picks_a), ("B无过程", picks_b)):
                d = format_detail(frame)
                if len(d):
                    d["代码"] = d["代码"].astype(str).str.zfill(6)
                    d.to_excel(xw, sheet_name=f"top{args.top}_明细_{name}", index=False)
        print(f"\n已写出 {out}")
    except Exception as exc:                                   # noqa: BLE001
        print(f"\n（Excel 写出失败：{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
