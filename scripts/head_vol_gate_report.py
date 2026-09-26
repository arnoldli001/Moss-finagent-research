"""`bar.vol20` 到底是不是信号？—— 波动率、误报、真值覆盖的三方对账。

## 起因

`pool_head_scan.py` 在候选池**头部**扫了 145 个因子（三窗口 + 按日
bootstrap），只有 `bar.vol20`（板块近 20 日日收益标准差）活下来：

    AUC_big（未来 20 日涨超 10%）  旧 0.5493 / 中 0.6040 / 新 0.5809
    三窗口 bootstrap 5% 分位      0.5062 / 0.5764 / 0.5524

但同一张表里它的 `rel`（相对当日全市场中位数的超额）AUC 是
**0.4347 / 0.5245 / 0.5171** —— 旧窗口反向。这个不一致必须解释清楚，
否则就是在给用户推一个"提高波动率"的门限，而不是提高胜率。

先验怀疑：**波动率不是 alpha，它是方差。** 高波动板块的
P(未来 20 日 > +10%) 本来就更高，但 P(< −10%) 同样更高。
如果是这样，把它当门限等于放大赌注，误报（这里定义为"告警后反而大跌"）
会跟着一起涨 —— 与用户「重点是误报影响大」的诉求正好相反。

## 三步判据

1. **分位表**：头部内按 `vol20` 分五组，同时看
   P(>+10%)、P(<−10%)、均值、中位数。只看第一列会得出错误结论。
2. **风险调整后的标签**：把标签换成 `fwd20 / vol20 > 0`（波动率归一）。
   如果 `vol20` 的 AUC 掉回 0.5 附近，就说明它只是在解释**方差**。
3. **真值覆盖对账**：在真实告警集上按 `vol20` 分位加门限，
   看告警条数、误报率、以及 47 个真值事件的合格率/漏报怎么变。
   一个只在样本统计上漂亮、却把真值事件砍掉的门限是不能上的。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/head_vol_gate_report.py \\
        --out docs/MAINLINE_VOL_GATE.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.pool_bigmove_scan import (  # noqa: E402
    BIG,
    CACHE_DB,
    HORIZON,
    MAIN_DB,
    WINDOWS,
    auc_rank,
    bootstrap_auc,
)

TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LEAD_OK, LAG_OK = 10, 5
LOOKBACK, LOOKAHEAD = 20, 40


def load_panel(table: str) -> pd.DataFrame:
    """一行 = 一个 (交易日, 板块)，带分数、等级、当日池内分位。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, total, level, candidate FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (WINDOWS[0][0], WINDOWS[-1][1])
    ).fetchall()
    conn.close()
    frame = pd.DataFrame([{"day": str(r["trade_date"]), "code": str(r["board_code"]),
                           "total": float(r["total"] or 0.0),
                           "level": str(r["level"] or "none"),
                           "candidate": bool(r["candidate"])} for r in rows])
    # 当日**候选池内**的分位：头部/门槛都在这个尺度上说
    pool = frame[frame["candidate"]].copy()
    pool["pool_pct"] = pool.groupby("day")["total"].rank(pct=True)
    return pool


def load_market() -> tuple[pd.DataFrame, pd.DataFrame]:
    """`(vol20 宽表, 未来 20 日收益宽表)`。

    ⚠️ 起读日必须**远早于**第一个回测窗口。第一版从 `WINDOWS[0][0]` 起读，
    结果 42% 的告警行 `vol20` 是 NaN（20 日滚动需要 20 个前置交易日），
    而 `τ > 0` 的筛选会把 NaN 静默丢掉 —— 于是 τ=0.2 的对比里
    「少掉的告警」其实绝大部分是**缺数据**，不是「低波动被过滤」。
    这类"看起来是结论、其实是数据缺口"的坑，本项目已经踩过好几次。
    """
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
        " JOIN ml_calendar k ON k.trade_date = b.trade_date"
        " WHERE b.trade_date BETWEEN ? AND ? ORDER BY b.trade_date",
        ("20100101", "20261231")).fetchall()
    conn.close()
    frame = pd.DataFrame([{"code": str(r["board_code"]), "day": str(r["trade_date"]),
                           "close": float(r["close"] or 0.0)} for r in rows])
    close = frame.pivot_table(index="day", columns="code", values="close").sort_index()
    vol = close.pct_change().rolling(20).std()
    fwd = close.shift(-HORIZON) / close - 1.0
    return vol, fwd


def attach(panel: pd.DataFrame, vol: pd.DataFrame, fwd: pd.DataFrame
           ) -> pd.DataFrame:
    """把行情特征按 (day, code) 贴到长表上，并算当日池内 `vol` 分位。"""
    vol_long = vol.stack().rename("vol20").reset_index()
    vol_long.columns = ["day", "code", "vol20"]
    fwd_long = fwd.stack().rename("fwd").reset_index()
    fwd_long.columns = ["day", "code", "fwd"]
    out = panel.merge(vol_long, on=["day", "code"], how="left")
    out = out.merge(fwd_long, on=["day", "code"], how="left")
    out["vol_pct"] = out.groupby("day")["vol20"].rank(pct=True)
    return out


def describe(frame: pd.DataFrame, label: str) -> list[str]:
    """一组行 → 一行 Markdown：条数、P(>10%)、P(<−10%)、均值、中位数。"""
    fwd = frame["fwd"].to_numpy(dtype=float)
    fwd = fwd[np.isfinite(fwd)]
    if fwd.size == 0:
        return [f"| {label} | 0 | — | — | — | — |"]
    return [f"| {label} | {fwd.size} | {(fwd > BIG).mean():.1%} "
            f"| {(fwd < -BIG).mean():.1%} | {fwd.mean():+.2%} "
            f"| {np.median(fwd):+.2%} |"]


def quantile_table(frame: pd.DataFrame, *, buckets: int = 5) -> list[str]:
    out = ["| 组 | 条数 | P(>+10%) | P(<−10%) | 均值 | 中位数 |",
           "|---|---:|---:|---:|---:|---:|"]
    ranked = frame.dropna(subset=["vol_pct", "fwd"])
    if ranked.empty:
        return out
    for index in range(buckets):
        low = index / buckets
        high = (index + 1) / buckets
        subset = ranked[(ranked["vol_pct"] > low) & (ranked["vol_pct"] <= high)]
        out += describe(subset, f"Q{index + 1}（vol 分位 {low:.0%}~{high:.0%}）")
    return out


def truth_coverage(alerts: pd.DataFrame, events: list[dict],
                   names: dict[str, str], days: list[str]) -> dict:
    """真值事件的合格率/漏报（与 `alert_timing_report.py` 同一口径）。"""
    index = {day: position for position, day in enumerate(days)}
    by_code: dict[str, list[str]] = {}
    for row in alerts.itertuples():
        by_code.setdefault(row.code, []).append(row.day)
    for value in by_code.values():
        value.sort()
    good = late = early = missed = 0
    deltas: list[int] = []
    for event in events:
        date = str(event.get("date") or "").replace("-", "")
        if date not in index:
            continue
        codes = [names.get(str(c), str(c)) for c in (event.get("codes") or [])]
        center = index[date]
        low, high = center - LOOKBACK, min(len(days) - 1, center + LOOKAHEAD)
        best: int | None = None
        for code in codes:
            for day in by_code.get(code, ()):
                position = index.get(day)
                if position is None or not (low <= position <= high):
                    continue
                if best is None or position < best:
                    best = position
        if best is None:
            missed += 1
            continue
        delta = best - center
        deltas.append(delta)
        if delta < -LEAD_OK:
            early += 1
        elif delta <= LAG_OK:
            good += 1
        else:
            late += 1
    total = good + late + early + missed
    return {"total": total, "good": good, "late": late, "early": early,
            "missed": missed,
            "rate": (good / total * 100) if total else float("nan"),
            "median": (float(np.median(deltas)) if deltas else float("nan"))}


def main() -> int:
    parser = argparse.ArgumentParser(description="vol20 门限对账")
    parser.add_argument("--table", default="mainline_score")
    parser.add_argument("--out", default="")
    parser.add_argument("--boot", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260921)
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    panel = load_panel(args.table)
    vol, fwd = load_market()
    data = attach(panel, vol, fwd)

    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    names: dict[str, str] = {}
    for row in conn.execute("SELECT code, name FROM ml_board"):
        names.setdefault(str(row["name"]), str(row["code"]))
    conn.close()
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]

    emit("# `bar.vol20` 是不是信号：波动率 / 误报 / 真值覆盖对账")
    emit()
    emit(f"> 由 `scripts/head_vol_gate_report.py` 生成，评分表 `{args.table}`。")
    emit(f"> 标签 `big`：未来 {HORIZON} 日收盘涨幅 > {BIG:.0%}；"
         "「误报」在这里细分为两种：涨不到 10%（空耗）与**反而跌超 10%**（亏钱）。")
    emit()

    # ---------- 一、它是不是只是方差 ----------
    emit("## 一、风险归一化：它是在解释方差，还是在选股？")
    emit()
    emit("把标签换成 `fwd20 / vol20`（波动率归一后的相对表现），"
         "再算 `vol20` 的 AUC。若掉回 0.5，说明它只是在解释**方差**。")
    emit()
    emit("| 窗口 | AUC(`vol20` → fwd>10%) | AUC(`vol20` → fwd/vol>0) | 判定 |")
    emit("|---|---:|---:|---|")
    for start, end, label in WINDOWS:
        shelf = data[(data["day"] >= start) & (data["day"] <= end)]
        head = shelf[shelf["pool_pct"] >= 0.8].dropna(subset=["vol_pct", "vol20", "fwd"])
        if head.empty:
            continue
        risk_adj = (head["fwd"] / head["vol20"].replace(0.0, np.nan))
        frame = pd.DataFrame({"x": head["vol_pct"].to_numpy(dtype=float),
                              "binary": (head["fwd"] > BIG).astype(float).to_numpy(),
                              "adjusted": (risk_adj > 0).astype(float).to_numpy()})
        frame = frame[np.isfinite(frame["x"]) & np.isfinite(frame["adjusted"])]
        binary_auc = auc_rank(frame["x"].to_numpy(), frame["binary"].to_numpy())
        adjusted_auc = auc_rank(frame["x"].to_numpy(), frame["adjusted"].to_numpy())
        verdict = ("纯方差" if not np.isfinite(adjusted_auc)
                   or abs(adjusted_auc - 0.5) < 0.015 else "含方向信息")
        emit(f"| {label} | {binary_auc:.4f} | {adjusted_auc:.4f} | {verdict} |")

    # ---------- 二、分位表 ----------
    emit()
    emit("## 二、头部内按 `vol20` 分五组（旧 / 中 / 新）")
    emit()
    emit("⚠️ 只看 P(>+10%) 会漏掉同样被抬高的 P(<−10%)。两列一起读。")
    for start, end, label in WINDOWS:
        shelf = data[(data["day"] >= start) & (data["day"] <= end)]
        head = shelf[shelf["pool_pct"] >= 0.8]
        emit()
        emit(f"### 【{label} {start}~{end}】头部 {len(head)} 行")
        emit()
        for row in quantile_table(head):
            emit(row)

    # ---------- 三、按日 bootstrap ----------
    emit()
    emit("## 三、头部内 `vol20` 的按日 bootstrap（对 `big` 标签）")
    emit()
    emit("| 窗口 | AUC | 5% | 95% | 有效天数 |")
    emit("|---|---:|---:|---:|---:|")
    for start, end, label in WINDOWS:
        shelf = data[(data["day"] >= start) & (data["day"] <= end)]
        head = shelf[shelf["pool_pct"] >= 0.8].dropna(
            subset=["vol_pct", "fwd"])
        keep = np.isfinite(head["vol_pct"].to_numpy(dtype=float))
        head = head[keep]
        day_codes, day_index = np.unique(head["day"].to_numpy(), return_inverse=True)
        low, high = bootstrap_auc(head["vol_pct"].to_numpy(dtype=float),
                                  (head["fwd"] > BIG).astype(float).to_numpy(),
                                  day_index, draws=args.boot, seed=args.seed)
        value = auc_rank(head["vol_pct"].to_numpy(dtype=float),
                         (head["fwd"] > BIG).astype(float).to_numpy())
        emit(f"| {label} | {value:.4f} | {low:.4f} | {high:.4f} "
             f"| {len(day_codes)} |")

    # ---------- 四、门限对账 ----------
    emit()
    emit("## 四、在**真实告警集**上加 `vol20` 门限的代价")
    emit()
    emit("告警集 = 候选池内 `level ∈ {strong, medium}`（冷却/确认之前的上界）。"
         "门限 = 当日池内 `vol20` 分位 ≥ τ。")
    emit()
    alerts_all = data[data["level"].isin(("strong", "medium"))].copy()
    # 所有 τ（含 τ=0）都必须在**同一批行**上比：否则 τ=0 带着缺数据行、
    # τ>0 把缺数据行丢掉，差异会被读成"门限的效果"。
    alerts = alerts_all.dropna(subset=["vol_pct"])
    emit(f"告警集 {len(alerts_all)} 行，其中 {len(alerts_all) - len(alerts)} 行"
         f"缺少 `vol20`（未上市/停牌/行情缺失），**所有 τ 都在这 {len(alerts)} 行上比较**。")
    emit()
    all_days = sorted(data["day"].unique())
    taus = [0.0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8]
    emit("| τ | 告警 | 日均 | P(>+10%) | P(<−10%) | 均值 | 合格率 | 漏报 | 太早 | 滞后 |")
    emit("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for tau in taus:
        kept = alerts[alerts["vol_pct"] >= tau] if tau > 0 else alerts
        stat = truth_coverage(kept, events, names, all_days)
        fwd_values = kept["fwd"].to_numpy(dtype=float)
        fwd_values = fwd_values[np.isfinite(fwd_values)]
        hit = float((fwd_values > BIG).mean()) if fwd_values.size else float("nan")
        drop = (float((fwd_values < -BIG).mean()) if fwd_values.size
                else float("nan"))
        emit(f"| {tau:g} | {len(kept)} | {len(kept) / len(all_days):.2f} "
             f"| {hit:.1%} | {drop:.1%} | {fwd_values.mean():+.2%} "
             f"| {stat['rate']:.0f}% | {stat['missed']} | {stat['early']} "
             f"| {stat['late']} |")

    # ---------- 五、结论 ----------
    emit()
    emit("## 五、怎么读")
    emit()
    emit("- 若第一节的「风险归一化 AUC」贴在 0.5，`vol20` 就不是 alpha，"
         "把它当门限只是把告警挪到波动更大的板块上：")
    emit("  P(>+10%) 上去的同时 P(<−10%) 也上去，用户说的「误报」并没有变少。")
    emit("- 若第四节里 τ 一抬，**漏报**就开始涨，那这个门限的代价是真值覆盖，"
         "和抬高绝对分数线是同一个失败模式。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    print(json.dumps({"table": args.table, "taus": taus}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
