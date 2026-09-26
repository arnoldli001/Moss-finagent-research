"""组合回测：入池 TOP3 中"昨日连板 ≥3 板"的隔夜接力策略。

## 策略规则（用户口径 2026-09-22，已按更正实现）

**选股顺序很关键**：先从**入池池**按分数取 **TOP3**，**再从中筛"昨日连板 ≥3 板"**。
（反过来"先筛 ≥3 板、再取前 3"会选出完全不同的票，实测一年 ~880 笔 vs 本口径 ~40 笔。）

买入：买入价 = 当日 **9:25 竞价价**（= 日线 `open`；已与录像带逐位核对）。
仓位：每只 **3.33 成**（起始资金 100 万）。
成本：**一笔买入+卖出合计 0.2%** 的磨损。

卖出：

    ① 买入当日**收盘未涨停** → **次日 9:25 竞价价**卖出（= 次日 `open`）
    ② 买入当日**收盘涨停** → 次日按下述优先级：
         a. 次日开盘涨幅 < 1%          → **开盘价**止盈卖出
         b. 次日收盘**涨停**            → **继续持有**，再重复这套判断
         c. 次日盘中触及"上一交易日收盘价" → 按**该价**卖出
         d. 以上都不满足                → **次日收盘价**卖出

## 数据口径

只用日线 OHLC + 官方涨停价（仓库 + tushare 分区），所以"有竞价过程"与"无竞价过程"
两组能在**同一口径**下比。不需要 9:45 或分时数据。

⚠️ 拿 `low` 与阈值比较有一个**顺序不可知**的保守近似：日线不知道低点出现在涨停
之前还是之后。本实现按"先触止损即成交"处理（保守，不高估收益）。
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data" / "auction_hist" / "auction_dataset.parquet"
DB = ROOT / "data" / "quant" / "warehouse.db"
OUT_DIR = ROOT / "data" / "backtest"

MIN_STREAK = 3           # 昨日连板下限
TOP_N = 3                # 入池池里按分数取前几名
WEIGHT = 0.0333          # 每只 3.33 成
START_CASH = 1_000_000.0
OPEN_GAP_TAKE = 1.0      # 次日开盘涨幅 < 1% 就止盈
MAX_HOLD_DAYS = 10       # 安全上限，避免数据缺口导致死循环
ROUND_TRIP_FEE = 0.002   # 一笔买入+卖出合计 0.2%


def _reason_bucket(text: str) -> str:
    """卖出原因归类（原始文案带价格，直接统计会一个价一个类）。"""
    if "次日竞价卖" in text:
        return "① 买入日未涨停→次日竞价卖"
    if "开盘止盈" in text:
        return "② 次日开盘<1%→开盘止盈"
    if "按该价卖" in text:
        return "③ 未涨停且触上日收盘→按该价卖"
    if "收盘价卖" in text:
        return "④ 未涨停未触上日收盘→收盘价卖"
    return "⑤ 其他"


def load_bars(start: str = "20250601") -> pd.DataFrame:
    """日线 OHLC + 官方涨停价（仓库 + tushare 分区合并）。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        d = pd.read_sql(
            "SELECT trade_date, code, open, high, low, close, pre_close "
            f"FROM quant_daily WHERE trade_date >= '{start}'", conn)
        lim = pd.read_sql(
            "SELECT trade_date, code, up_limit "
            f"FROM quant_stk_limit WHERE trade_date >= '{start}'", conn)
    finally:
        conn.close()
    for f in (d, lim):
        f["trade_date"] = f["trade_date"].astype(str)
        f["code"] = f["code"].astype(str).str.zfill(6)
    out = d.merge(lim, on=["trade_date", "code"], how="left")

    # 补 tushare 分区里仓库**不完整**的交易日。
    #
    # ⚠️ 判据不能只看"仓库有没有这一天的行"。实测 20260918：仓库 `quant_daily`
    #    有 5209 行，但 `quant_stk_limit`（up_limit）**一行都没入库** ——
    #    只看行数会把分区跳过，于是那天的 `up_limit` 为空、
    #    后面的 `drop_duplicates` 又把它压成一行，连带 0921/0922 的 `close`
    #    也变成 NaN（实测：0921/0922 前置筛选后 0 只，因为 ma20 全空）。
    #    正确判据：**这一天在仓库里是否同时具备 close 与 up_limit**。
    base = ROOT / "data" / "quant" / "tushare" / "a_share"
    usable = (out.assign(_ok=out["close"].notna() & out["up_limit"].notna())
                 .groupby("trade_date")["_ok"].sum())
    # ⚠️ 索引是**整数**（sqlite 里的 trade_date 是 INTEGER），必须转成字符串再比，
    #    否则 `p.stem in thin` 永远为假、`out[...].isin(thin)` 也永远为空 ——
    #    仓库那几天的**残缺行**就留在表里，把分区补进来的好行挤掉
    #    （实测：0918/0921 的 close 全空、而 up_limit 有 5221 行，日期错位）。
    thin = {str(d) for d, n in usable.items() if n < 100}
    have_days = set(out["trade_date"].astype(str))
    need = {p.stem for p in (base / "daily").glob("*.parquet")
            if p.stem >= start and (p.stem not in have_days or p.stem in thin)}
    # ⚠️ **逐日独立处理**，绝不跨天合并。
    #    跨天 outer merge + concat 这套写法在这里已经栽了三次（0918/0921 的 close
    #    变成 NaN、而 up_limit 有 5221 行 = 日期错位）。逐日左连接简单可靠：
    #    以 daily 分区为骨架，左连 stk_limit，然后整段替换仓库那几天的行。
    parts: list[pd.DataFrame] = []
    for day in sorted(need):
        dp = base / "daily" / f"{day}.parquet"
        if not dp.exists():
            continue
        q = pd.read_parquet(dp)
        q["trade_date"] = q["trade_date"].astype(str)
        q["code"] = q["code"].astype(str).str.zfill(6)
        keep = [c for c in ("trade_date", "code", "open", "high", "low", "close",
                            "pre_close") if c in q.columns]
        q = q[keep].copy()
        lp = base / "stk_limit" / f"{day}.parquet"
        if lp.exists():
            l2 = pd.read_parquet(lp)
            l2["trade_date"] = l2["trade_date"].astype(str)
            l2["code"] = l2["code"].astype(str).str.zfill(6)
            q = q.merge(l2[[c for c in ("trade_date", "code", "up_limit")
                            if c in l2.columns]], on=["trade_date", "code"], how="left")
        parts.append(q)
    if parts:
        filled = pd.concat(parts, ignore_index=True)
        out = out[~out["trade_date"].astype(str).isin(need)]
        out = pd.concat([out, filled], ignore_index=True)
        logger.warning("分区补入 %s：%d 行", sorted(need), len(filled))

    for c in ("open", "high", "low", "close", "pre_close", "up_limit"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.drop_duplicates(["trade_date", "code"]).sort_values(["code", "trade_date"])
    out["涨停"] = (out["close"] - out["up_limit"]).abs() < 0.005
    return out.reset_index(drop=True)


@dataclass
class Trade:
    code: str
    entry_date: str
    entry_price: float
    shares_cash: float
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    exit_bucket: str = ""
    hold_days: int = 0
    ret_pct: float = 0.0
    pnl: float = 0.0


def simulate_one(bars_by_code: dict[str, pd.DataFrame], code: str, entry_date: str,
                 cash: float) -> Trade | None:
    """按用户口径模拟一笔交易（含 0.2% 往返磨损）。"""
    df = bars_by_code.get(code)
    if df is None or not len(df):
        return None
    hit = df.index[df["trade_date"] == entry_date]
    if not len(hit):
        return None
    i = int(hit[0])
    cur = df.iloc[i]
    entry = float(cur["open"])
    if not (entry > 0):
        return None
    tr = Trade(code=code, entry_date=entry_date, entry_price=entry, shares_cash=cash)

    if not bool(cur["涨停"]):
        # ① 买入当日未涨停 → 次日竞价卖
        if i + 1 >= len(df):
            return None
        nxt = df.iloc[i + 1]
        tr.exit_date, tr.exit_price = str(nxt["trade_date"]), float(nxt["open"])
        tr.exit_reason, tr.hold_days = "买入日未涨停→次日竞价卖", 1
    else:
        # ② 买入日涨停 → 逐日判是否卖出
        j = i + 1
        prev_close = entry
        sold = False
        while j < len(df) and (j - i) <= MAX_HOLD_DAYS:
            row = df.iloc[j]
            day = str(row["trade_date"])
            o, l, c = float(row["open"]), float(row["low"]), float(row["close"])
            gap = (o / prev_close - 1) * 100 if prev_close else 0.0
            if gap < OPEN_GAP_TAKE:
                tr.exit_date, tr.exit_price = day, o
                tr.exit_reason = f"开盘涨幅 {gap:.2f}% < 1% → 开盘止盈"
                sold = True
            elif bool(row["涨停"]):
                prev_close = c
                j += 1
                continue
            elif l <= prev_close:
                tr.exit_date, tr.exit_price = day, prev_close
                tr.exit_reason = "未涨停且触及上日收盘价 → 按该价卖"
                sold = True
            else:
                tr.exit_date, tr.exit_price = day, c
                tr.exit_reason = "未涨停未触上日收盘 → 收盘价卖"
                sold = True
            if sold:
                tr.hold_days = j - i
                break
            j += 1
        if not sold:
            return None

    tr.ret_pct = (tr.exit_price / tr.entry_price - 1) * 100 - ROUND_TRIP_FEE * 100
    tr.pnl = tr.shares_cash * tr.ret_pct / 100
    tr.exit_bucket = _reason_bucket(tr.exit_reason)
    return tr


def perf_metrics(curve: pd.DataFrame) -> dict[str, Any]:
    """收益率 / 最大回撤 / 夏普率 / 索提诺（日频）。"""
    if curve is None or len(curve) < 2:
        return {}
    eq = curve["equity"].to_numpy(dtype=float)
    rets = np.diff(eq) / eq[:-1]
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    ann = math.sqrt(244)
    sd = rets.std(ddof=1)
    downside = rets[rets < 0]
    dsd = downside.std(ddof=1) if len(downside) > 1 else 0.0
    return {
        "起始资金": round(eq[0], 2),
        "期末资金": round(eq[-1], 2),
        "总收益率%": round((eq[-1] / eq[0] - 1) * 100, 2),
        "最大回撤%": round(float(dd.min()) * 100, 2),
        "夏普率": round(float(rets.mean() / sd * ann), 3) if sd > 0 else 0.0,
        "索提诺": round(float(rets.mean() / dsd * ann), 3) if dsd > 0 else 0.0,
        "交易日数": len(rets),
        "日胜率%": round(float((rets > 0).mean()) * 100, 2),
    }


def run(group: pd.DataFrame, bars: pd.DataFrame, label: str, out_prefix: str,
        streak: int = MIN_STREAK, top_n: int = TOP_N) -> dict[str, Any]:
    """跑一组：每天从入池池取 TOP n，再筛昨日连板 ≥ streak 的。"""
    by_code = {c: g.reset_index(drop=True) for c, g in bars.groupby("code", sort=False)}
    all_days = sorted(set(bars["trade_date"]))
    trades: list[Trade] = []
    cash = START_CASH
    rows: list[dict[str, Any]] = []

    for day, sub in group.groupby("trade_date"):
        ranking = sub.sort_values("open_gap_pct", ascending=False).head(top_n)
        streak_col = pd.to_numeric(ranking["prev_streak"], errors="coerce").fillna(0)
        picks = ranking[streak_col >= streak]
        day_pnl = 0.0
        for rec in picks.to_dict("records"):
            tr = simulate_one(by_code, str(rec["code"]).zfill(6), str(day),
                              cash * WEIGHT)
            if tr is not None:
                trades.append(tr)
                day_pnl += tr.pnl
        cash += day_pnl
        rows.append({"trade_date": day, "equity": cash, "pnl": day_pnl,
                     "positions": len(picks)})

    curve = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    if len(curve):
        span = [d for d in all_days
                if curve["trade_date"].min() <= d <= curve["trade_date"].max()]
        curve = pd.DataFrame({"trade_date": span}).merge(curve, on="trade_date",
                                                          how="left")
        curve["equity"] = curve["equity"].ffill().fillna(START_CASH)
        curve["pnl"] = curve["pnl"].fillna(0.0)
        curve["positions"] = curve["positions"].fillna(0).astype(int)
    return {"label": label, "trades": trades, "curve": curve,
            "metrics": perf_metrics(curve), "out_prefix": out_prefix}


def main() -> int:
    ap = argparse.ArgumentParser(description="入池TOP3 中昨日≥N板 的隔夜接力组合回测")
    ap.add_argument("--streak", type=int, default=MIN_STREAK)
    ap.add_argument("--top", type=int, default=TOP_N)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    ds = pd.read_parquet(DATA)
    ds["trade_date"] = ds["trade_date"].astype(str)
    ds["code"] = ds["code"].astype(str).str.zfill(6)
    ds["prev_streak"] = pd.to_numeric(ds["prev_streak"], errors="coerce").fillna(0)
    ds["open_gap_pct"] = pd.to_numeric(ds["open_gap_pct"], errors="coerce")

    print("=" * 104)
    print(f"入池池 TOP{args.top} → 筛「昨日连板 ≥{args.streak} 板」→ 竞价买入｜"
          f"每只 {WEIGHT * 100:.2f}% 仓｜起始 {START_CASH:,.0f} 元｜"
          f"单笔磨损 {ROUND_TRIP_FEE * 100:.1f}%")
    print("=" * 104)
    print(f"入池池 {len(ds):,} 股日 / {ds['trade_date'].nunique()} 个交易日 "
          f"（{ds['trade_date'].min()} ~ {ds['trade_date'].max()}）")

    bars = load_bars()
    print(f"日线 {len(bars):,} 行 / {bars['code'].nunique():,} 只 / "
          f"{bars['trade_date'].min()} ~ {bars['trade_date'].max()}")

    groups = [("A 有竞价过程", ds[ds["pattern"].notna()]),
              ("B 无竞价过程", ds[ds["pattern"].isna()]),
              ("全部（A+B）", ds)]
    results = []
    for label, g in groups:
        if len(g):
            results.append(run(g, bars, label, label[0], streak=args.streak,
                               top_n=args.top))

    summary = []
    for r in results:
        m: dict[str, Any] = {"口径": r["label"], "开仓笔数": len(r["trades"])}
        m.update(r["metrics"])
        if r["trades"]:
            rets = np.array([t.ret_pct for t in r["trades"]])
            m["单笔均收益%"] = round(float(rets.mean()), 2)
            m["单笔胜率%"] = round(float((rets > 0).mean()) * 100, 2)
            m["单笔最好%"] = round(float(rets.max()), 2)
            m["单笔最差%"] = round(float(rets.min()), 2)
            m["均持仓日"] = round(float(np.mean([t.hold_days for t in r["trades"]])), 2)
        summary.append(m)

    order = ["口径", "开仓笔数", "起始资金", "期末资金", "总收益率%", "最大回撤%",
             "夏普率", "索提诺", "日胜率%", "交易日数", "单笔均收益%", "单笔胜率%",
             "单笔最好%", "单笔最差%", "均持仓日"]
    st = pd.DataFrame(summary)
    print()
    print(st[[c for c in order if c in st.columns]].to_string(index=False))

    all_summary = st
    for r in results:
        if not r["trades"]:
            continue
        t = pd.DataFrame([x.__dict__ for x in r["trades"]])
        print()
        print(f"── {r['label']} 卖出原因分布（{len(t)} 笔）──")
        print(t["exit_bucket"].value_counts().to_string())
        out = OUT_DIR / f"overnight_top{args.top}_streak{args.streak}_{r['out_prefix']}.xlsx"
        try:
            with pd.ExcelWriter(out, engine="openpyxl") as xw:
                all_summary.to_excel(xw, sheet_name="总览", index=False)
                t.to_excel(xw, sheet_name="逐笔明细", index=False)
                r["curve"].to_excel(xw, sheet_name="资金曲线", index=False)
            print(f"   已写出 {out}")
        except Exception as exc:                               # noqa: BLE001
            print(f"   （Excel 写出失败：{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
