"""入选池（**生产真实打分**）TOP3 → 筛「昨日连板 ≥3 板」→ 竞价买入。

## 与前面几版的根本区别

| 版本 | "TOP3" 依据 |
|---|---|
| `backtest_overnight_relay.py` | 候选池 + **竞价涨幅**代理排序（口径不对：候选池不是入选池） |
| `backtest_pool_relay.py` | 入选池 + **竞价涨幅**代理排序 |
| **本脚本** | 入选池 + **`scoring.score_candidate` 的真实总分** |

即：候选 → 前置筛选 → 否决 → **按生产总分排序** → 取 TOP3 → 只买其中"昨日连板 ≥3 板"。

`score_candidate` 是生产同一个函数：12 维加权（权重和 = 100）+ 抢筹族加成，
**缺席维度按剩余权重归一化**（某维没数据不能当 0 分，否则缺数据的票被系统性压分）。

## ⚠️ 哪些维度缺席、影响多大

能重建的维度（本脚本会喂进去）：开盘位置、竞价量能、昨日质量、连板梯队、盘口适配、
封流比、换手率、价格位置等。

**重建不出来的**（我没有竞价快照）：承接强度（逐笔委托）、题材热度（历史题材榜）、
竞价情绪的部分分量、市场级字段（市场温度/炸板率/涨停家数）。它们对应的权重会**不计入
分母**，所以总分的绝对值与生产不可比 —— 但**同一天内各票的相对排序**仍然有意义
（同一天缺席的维度是同一批）。这一点是本次回测最重要的口径限制。
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


def build_features() -> tuple[pd.DataFrame, pd.DataFrame]:
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
    f["full_code"] = f["code"].map(
        lambda c: c + (".SH" if str(c).startswith(("6", "9")) else ".SZ"))
    f["limit_up_days"] = pd.to_numeric(f["limit_up_days_18"], errors="coerce")
    f["prev_close"] = pd.to_numeric(f["pre_close"], errors="coerce")
    f["circ_mv"] = pd.to_numeric(f["circ_mv"], errors="coerce")
    f["open_gap_pct"] = pd.to_numeric(f["open_gap_pct"], errors="coerce")
    f["auction_price"] = pd.to_numeric(f["auction_price"], errors="coerce").fillna(
        pd.to_numeric(f["open"], errors="coerce"))
    f["prev_streak"] = pd.to_numeric(f["prev_streak"], errors="coerce").fillna(0)
    f["prev_limit_up_streak"] = f["prev_streak"]
    f["turnover_rate"] = pd.to_numeric(f.get("turnover_rate"), errors="coerce")
    f["auction_volume_ratio"] = pd.to_numeric(f.get("auction_volume_ratio"),
                                              errors="coerce")
    f["auction_vs_yesterday"] = pd.to_numeric(f.get("auction_vs_yesterday"),
                                              errors="coerce")
    f["float_share"] = pd.to_numeric(f.get("float_share"), errors="coerce")
    # ── 把维度函数真正要读的**字段名**补齐（否则维度缺席、权重覆盖不足）──
    #    规则表 `DimSpec.inputs` 要的是这些键，而统一数据集里叫的是另一套名字：
    #      auction_strength(19)  ← auction_volume_vs_yesterday + auction_amount_ratio
    #      capital_fit(4)        ← auction_amount + circulating_market_value
    #      seal_flow_ratio(6) / previous_day(5) ← prev_seal_to_float_ratio
    #    缺一个键就整维缺席 —— 实测只补出 27% 权重覆盖，打分器把每只票都判 reject。
    f["auction_volume_vs_yesterday"] = f["auction_vs_yesterday"]
    f["auction_amount"] = pd.to_numeric(f.get("auction_amount"), errors="coerce")
    amt_prev = pd.to_numeric(f.get("amount"), errors="coerce")     # 昨日全天成交额
    f["auction_amount_ratio"] = f["auction_amount"] / amt_prev
    f["circulating_market_value"] = f["circ_mv"]
    # 「封单占流通盘」：竞价阶段拿不到封单额，用"昨日竞价量 ÷ 流通股本"作**近似**
    # （竞价量越大说明昨日承接越强），并在报告里标明是近似口径。
    f["prev_seal_to_float_ratio"] = (
        pd.to_numeric(f.get("prev_auction_volume"), errors="coerce")
        * 100.0 / f["float_share"])
    return f, bars


def score_row(feat: dict[str, Any], cfg: Any) -> tuple[float | None, str]:
    """调用**生产打分器**，返回 (total_score, decision)。

    ⚠️ `scoring.score_candidate` 第 521 行 `list(feature.get("rush_labels") or [])`
    在 `rush_labels` 是 `NaN`（从库里读回来的缺失值）时会抛
    `TypeError: 'float' object is not iterable` —— 这是**生产代码里的一个真 bug**
    （特征行从库里反序列化时就可能带 NaN）。这里先把它规整成列表绕过去。
    """
    from src.auction_select import scoring

    feat = dict(feat)
    labels = feat.get("rush_labels")
    if labels is None or not isinstance(labels, (list, tuple)):
        try:
            if labels != labels:                              # NaN
                feat["rush_labels"] = []
        except Exception:                                     # noqa: BLE001
            feat["rush_labels"] = []
        if not isinstance(feat.get("rush_labels"), (list, tuple)):
            feat["rush_labels"] = [x for x in str(labels).split("|") if x] \
                if isinstance(labels, str) and labels else []
    try:
        r = scoring.score_candidate(feature=feat, config=cfg,
                                    name=str(feat.get("name") or ""),
                                    full_code=str(feat.get("full_code") or ""),
                                    market_cap=feat.get("circ_mv"))
    except Exception as exc:                                   # noqa: BLE001
        return None, f"err:{type(exc).__name__}"
    return float(r.get("total_score") or 0.0), str(r.get("decision") or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="真实打分版：入选池 TOP3 × 连板≥N")
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
    # ── 方案 ①：放宽 bit10「维度完整度」──
    #
    # 生产 `veto.min_weight_coverage = 1.0`，而 `total_weight` 是 100 的**硬约束**
    # （有单测钉住），所以它实际要求**12 个维度全有数据**。回测环境里
    # 「承接强度(12) / 题材热度(12) / 竞价情绪(5) / 封流比(6) / 题材龙头(3)」
    # 这些依赖 9:15~9:25 逐笔或快照的维度**必然缺席**（= 38% 权重），
    # 于是每一只票都会被判 `reject` —— 实测：有分票 0、开仓 0 笔。
    #
    # 这里降到 `scoring.MIN_WEIGHT_COVERAGE`（0.55，代码里本来就有这个默认值），
    # 让"能重建的那部分维度"参与打分。**必须在报告里标明这一点**：
    # 总分绝对值与生产不可比，但同一交易日内各票的**相对排序**仍可比
    # （同一天缺席的维度是同一批）。
    _cov = float(getattr(cfg.veto, "min_weight_coverage", 1.0))
    cfg.veto.min_weight_coverage = 0.55
    print(f"⚠️ bit10 维度完整度阈值：{_cov} → {cfg.veto.min_weight_coverage}（方案①）")
    print(f"   缺的维度（约 38% 权重）：承接强度/题材热度/竞价情绪/封流比/题材龙头")

    f, bars = build_features()
    print("=" * 104)
    print(f"【真实打分口径】候选 → 前置筛选 → 否决 → **生产总分排序** → TOP{args.top} → "
          f"筛连板≥{args.streak} → 竞价买入｜每只 {WEIGHT*100:.2f}%｜磨损 "
          f"{ROUND_TRIP_FEE*100:.1f}%")
    print("=" * 104)
    print(f"候选池 {len(f):,} 股日 / {f['trade_date'].nunique()} 天 "
          f"（{f['trade_date'].min()} ~ {f['trade_date'].max()}）")

    rows, stats, scored_any = [], [], 0
    for day, sub in f.groupby("trade_date"):
        pre = apply_prefilter(sub, cfg)
        if not len(pre.kept):
            stats.append({"交易日": day, "候选": len(sub), "前置筛选后": 0,
                          "否决后": 0, "有分": 0, "命中": 0})
            continue
        alive, _h, _u = apply_vetoes(pre.kept, cfg)
        if not len(alive):
            stats.append({"交易日": day, "候选": len(sub),
                          "前置筛选后": len(pre.kept), "否决后": 0, "有分": 0,
                          "命中": 0})
            continue
        recs = []
        for feat in alive.to_dict("records"):
            score, decision = score_row(feat, cfg)
            if score is None or decision != "buy":
                continue
            feat["_score"], feat["_decision"] = score, decision
            recs.append(feat)
        if not recs:
            stats.append({"交易日": day, "候选": len(sub),
                          "前置筛选后": len(pre.kept), "否决后": len(alive),
                          "有分": 0, "命中": 0})
            continue
        scored_any += len(recs)
        sdf = pd.DataFrame(recs).sort_values("_score", ascending=False)
        sdf = sdf.reset_index(drop=True)
        top = sdf.head(args.top)
        idx = top.index[pd.to_numeric(top["prev_streak"], errors="coerce")
                        .fillna(0) >= args.streak]
        stats.append({"交易日": day, "候选": len(sub),
                      "前置筛选后": len(pre.kept), "否决后": len(alive),
                      "有分": len(recs), "命中": len(idx)})
        sdf["_day"] = day
        sdf["_rank"] = range(1, len(sdf) + 1)
        sdf["_picked"] = sdf.index.isin(idx)
        rows.append(sdf)

    pool = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    stats_df = pd.DataFrame(stats)
    picked = pool[pool["_picked"]] if len(pool) else pd.DataFrame()
    print(f"有分（decision=buy）的票共 {scored_any:,} 股日；"
          f"命中「TOP{args.top} 且连板≥{args.streak}」{len(picked)} 笔")
    if len(pool):
        print(f"逐日均分示例（前 5 天）：")
        for d, g in list(pool.groupby("_day"))[:5]:
            print("   %s  均分 %.2f  最高 %.2f（%s）" % (
                d, g["_score"].mean(), g["_score"].max(),
                g.loc[g["_score"].idxmax(), "code"]))

    results = []
    for label, g in (("A 有竞价过程", picked[picked["pattern"].notna()] if len(picked) else picked),
                     ("B 无竞价过程", picked[picked["pattern"].isna()] if len(picked) else picked),
                     ("全部（A+B）", picked)):
        if not len(g):
            continue
        by_code = {c: x.reset_index(drop=True) for c, x in bars.groupby("code", sort=False)}
        trades, cash, curve_rows = [], START_CASH, []
        for day, sub in g.groupby("_day"):
            day_pnl = 0.0
            for rec in sub.to_dict("records"):
                tr = simulate_one(by_code, str(rec["code"]).zfill(6), str(day), cash * WEIGHT)
                if tr is not None:
                    trades.append(tr)
                    day_pnl += tr.pnl
            cash += day_pnl
            curve_rows.append({"trade_date": day, "equity": cash, "pnl": day_pnl})
        curve = pd.DataFrame(curve_rows).sort_values("trade_date").reset_index(drop=True)
        if len(curve):
            span = [d for d in sorted(set(bars["trade_date"]))
                    if curve["trade_date"].min() <= d <= curve["trade_date"].max()]
            curve = pd.DataFrame({"trade_date": span}).merge(curve, on="trade_date", how="left")
            curve["equity"] = curve["equity"].ffill().fillna(START_CASH)
            curve["pnl"] = curve["pnl"].fillna(0.0)
        m: dict[str, Any] = {"口径": label, "开仓笔数": len(trades)}
        m.update(perf_metrics(curve))
        if trades:
            rets = np.array([t.ret_pct for t in trades], dtype=float)
            m["单笔均收益%"] = round(float(np.nanmean(rets)), 2)
            m["单笔胜率%"] = round(float(np.nanmean(rets > 0)) * 100, 2)
        results.append((pd.DataFrame([m]), trades, curve, label))

    if results:
        st = pd.concat([x[0] for x in results], ignore_index=True)
        order = ["口径", "开仓笔数", "起始资金", "期末资金", "总收益率%", "最大回撤%",
                 "夏普率", "索提诺", "日胜率%", "交易日数", "单笔均收益%", "单笔胜率%"]
        print()
        print(st[[c for c in order if c in st.columns]].to_string(index=False))
        for _m, trades, curve, label in results:
            if not trades:
                continue
            t = pd.DataFrame([x.__dict__ for x in trades])
            print()
            print(f"── {label} 卖出原因（{len(t)} 笔）──")
            print(t["exit_bucket"].value_counts().to_string())
            out = OUT_DIR / f"realscore_top{args.top}_streak{args.streak}_{label[0]}.xlsx"
            try:
                with pd.ExcelWriter(out, engine="openpyxl") as xw:
                    st.to_excel(xw, sheet_name="总览", index=False)
                    t.to_excel(xw, sheet_name="逐笔明细", index=False)
                    curve.to_excel(xw, sheet_name="资金曲线", index=False)
                    stats_df.to_excel(xw, sheet_name="逐日筛选", index=False)
                    if len(pool):
                        cols = [c for c in ("_day", "_rank", "code", "_score",
                                            "prev_streak", "open_gap_pct", "_picked")
                                if c in pool.columns]
                        pool[cols].to_excel(xw, sheet_name="打分明细", index=False)
                print(f"   已写出 {out}")
            except Exception as exc:                           # noqa: BLE001
                print(f"   （Excel 写出失败：{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
