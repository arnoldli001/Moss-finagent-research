"""候选池内的打分方案扫描：**六格判据**（3 窗口 × 2 持有期）选出最优合成分。

## 为什么需要它（以及为什么不能只看一个持有期）

`pool_factor_scan.py` 的结果是一条**干净的口径分裂**（两端都跨窗口同号）：

| 因子 | 池内 H=20（跨窗最差） | 池内 H=60（跨窗最差） |
|---|---|---|
| `six_dim_score` | **+0.0290** | +0.0388 |
| `accumulation_score` | −0.0487 | +0.0455 |
| `base_total`（V2.2 的 50/50） | −0.0487 | **+0.0516** |
| `leader.seat`（龙虎榜席位） | **+0.0484** | −0.0092 |
| `six_dim.prosperity` | −0.0036 | **+0.0857** |

也就是说：

- **H=20 偏向六维**（V2.3 把层权重改成 100/0 正确）；
- **H=60 偏向混合**（`base_total` 的 0.0516 比纯六维的 0.0388 好，
  而且两个窗口都是 0.055/0.052 —— 相当一致）；
- **`leader.seat` 是 H=20 最强的单项**，却在 H=60 变成轻微负贡献。

只优化一个持有期就会在另一个上翻车（V2.3 的 100/0 就是这么定的：
它只看 H=20 与"取头部超额"，H=60 上其实是退步的）。
所以这里用 **min over 4 格** 当目标函数，并要求"正号格数"一起看 ——
要的是**六格都不差**，不是某格特别漂亮。

## 两个指标都要看

1. **池内 IC**（六格）：衡量排序能力；
2. **取头部 10 个的超额收益**（相对候选池等权）：衡量"真去下单"的结果。
   IC 好但头部没变化等于什么都没做，所以两者都要过。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts\\pool_score_scan.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.factor_ic_report import load_returns, rank_ic, summarize  # noqa: E402

DB = ROOT / "data" / "moss_finagent.db"
#: ⚠️ **三个**窗口。原来只用两个（旧/新），而 2024-09~2025-09 这 13 个月
#: 在本项目早期是**没有打分的**（见 16.31 待办第 8 条），后来全量重打分
#: 才补上。补上之后它就是一段**独立行情**，能当第三个复现窗口 ——
#: 两个窗口"同号"很容易是巧合，三个同号才勉强算规律。
WINDOWS = (("20231009", "20240806", "旧"),
           ("20240901", "20250930", "中"),
           ("20251001", "20260918", "新"))
HORIZONS = (20, 60)
TOP_K = 10
#: 六维层占的权重（其余给第二层）
WEIGHTS = (100.0, 90.0, 80.0, 70.0, 60.0, 50.0)
#: 席位分附加系数（`score = 层混合 + λ · seat`，seat 是 0~100 的分）
SEAT_LAMBDAS = (0.0, 0.1, 0.2, 0.3)


def load_panel(table: str, start: str, end: str) -> pd.DataFrame:
    """候选池长表：`six_dim` / `accumulation` / `seat` / 加分等。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        dims: dict[str, float] = {}
        for layer in (payload.get("layers") or []):
            key = str(layer.get("key") or "")
            for dim in (layer.get("dimensions") or []):
                if dim.get("available"):
                    dims[f"{key}.{dim.get('key')}"] = \
                        float(dim.get("score") or 0.0)
        records.append({
            "day": str(row["trade_date"]),
            "code": str(row["board_code"]),
            "six_dim": float(payload.get("six_dim_score") or 0.0),
            "accumulation": float(payload.get("accumulation_score") or 0.0),
            # 席位不可用时填 0（= 不额外加分），而不是丢掉这一行 ——
            # 丢掉会让"没有席位信息"变成"这个板块不存在"
            "seat": dims.get("leader.seat", 0.0),
            "prosperity": dims.get("six_dim.prosperity", 0.0),
        })
    return pd.DataFrame(records)


def grid_ic(panel: pd.DataFrame, score: pd.Series,
                 returns: dict[int, pd.DataFrame]) -> dict:
    """在**同一天**把 score 铺成截面表，算各持有期 IC。"""
    frame = panel.assign(score=score.to_numpy()).pivot_table(
        index="day", columns="code", values="score")
    out: dict = {}
    for horizon in HORIZONS:
        series, median_n = rank_ic(frame, returns[horizon])
        stat = summarize(series, horizon)
        if stat:
            out[horizon] = {"ic": stat["ic"], "n": median_n,
                            "win": stat["win"], "t_ne": stat["t_ne"]}
    return out


def topk_excess(panel: pd.DataFrame, score: pd.Series,
                returns: dict[int, pd.DataFrame]) -> dict:
    """每日按 score 取头部 `TOP_K`，算相对候选池等权的超额（百分点）。"""
    scored = panel.assign(score=score.to_numpy())
    out: dict = {}
    for horizon in HORIZONS:
        ret = returns[horizon]
        picks: list[float] = []
        pools: list[float] = []
        for day, group in scored.groupby("day", sort=True):
            if day not in ret.index:
                continue
            row = ret.loc[day]
            codes = group["code"].tolist()
            values = row.reindex(codes).dropna()
            if len(values) < TOP_K:
                continue
            top = (group.set_index("code")["score"].reindex(codes).astype(float)
                   .sort_values(ascending=False).head(TOP_K).index)
            picks.append(float(row.reindex(top).dropna().mean()))
            pools.append(float(values.mean()))
        if not picks:
            continue
        diff = np.array(picks) - np.array(pools)
        out[horizon] = {"excess": float(diff.mean()) * 100.0,
                        "win": float((diff > 0).mean()),
                        "days": len(diff)}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="候选池内的打分方案扫描")
    parser.add_argument("--table", default="mainline_score_bak_20260921_2015",
                        help="评分表；默认 V2.2 备份（两个窗口都完整）")
    parser.add_argument("--out", default="", help="写出 Markdown 记录")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 候选池内的打分方案扫描（六格判据）")
    emit()
    emit(f"> 由 `scripts/pool_score_scan.py` 生成，数据表 `{args.table}`。")
    emit("> **六格** = 3 个窗口 × 2 个持有期（2024-09~2025-09 是第三个独立"
         "窗口，早期没有打分，全量重打分后才补上）。目标函数取**六格最小值**"
         " —— 只优化一个持有期会在另一个上翻车（V2.3 的 100/0 就只看 H=20），"
         "只用两个窗口则容易把巧合当规律。")
    emit()
    emit("## ⚠️ 读这张表之前必须知道的前提")
    emit()
    emit("1. **席位（`leader.seat`）的 IC 里含未兑现的信息**。"
         "`service.py` 取龙虎榜用的是 `store.seats(start, end=target)` —— "
         "**包含交易日当天**，而龙虎榜是当天收盘后才公布的；"
         "而收益口径是「当天收盘买入」（`close[D+H]/close[D]`）。"
         "也就是说模型用了收盘后才知道的信息，在收盘时下单。")
    emit("2. 这个约定是**全局**的：`moneyflow`、`northbound`、`chips` 等"
         "同样是「当日 EOD 数据 + 当日收盘价成交」。所以**因子之间**的相对"
         "比较大致公平，但**绝对**IC 与头部超额都偏乐观，"
         "不能当成可交易收益。")
    emit("3. 因此下表中带 `λ>0`（含席位）的方案，"
         "**必须先把席位窗口改成 `end = 上一交易日`** 再复测一次；"
         "如果 IC 塌掉，说明它主要来自这个时间差，不能采用。"
         "在那之前，本表只能用来回答「层内权重怎么分」，"
         "不能用来给席位加分背书。")
    emit()
    emit("## 🛑 后续结论（第 5~6 轮已补测，**λ>0 的方案已被否决**）")
    emit()
    emit("- **时间差不是问题**：`scripts/_diag_seat_leak.py` 把收益拆成三腿后，"
         "席位的『第一腿 D→D+1』IC 在旧窗口是**负的**（−0.0207），"
         "而『扣一腿』『滞后1期』都保留甚至更高 —— 所以不是靠隔夜跳空。")
    emit("- **真正的问题是本表的 IC 用了错误的口径**：席位每天只有约 1/5 的"
         "候选板块有值，过不了 `rank_ic` 的『每天 ≥20 个共有样本』门槛，"
         "旧窗口 204 天里只有 **14 天**能算 —— 而这 14 天正是席位最密集的"
         "2024-05/06，是**选择性样本**。")
    emit("- 正确的检验是『有信号的板块 vs 池内其余』（样本量按天算），见 "
         "`docs/MAINLINE_SEAT_SPARSE_TEST.md`：新窗口 H=20 **+0.68pp**（胜率 55%）"
         "但旧窗口只有 **+0.16pp 且胜率 44%**；H=60 两窗口**反号**"
         "（−0.75 / +1.95）。")
    emit("- **结论：席位未通过跨窗口复现判据，不给它加分。**"
         "本表 `λ>0` 的行只作为「IC 口径陷阱」的示例保留，"
         "不要据此改配置。")
    emit()

    table_data: dict[str, dict] = {}
    for start, end, label in WINDOWS:
        panel = load_panel(args.table, start, end)
        if panel.empty:
            emit(f"- ⚠️ {label} 窗口没有候选池数据，跳过")
            continue
        table_data[label] = {"panel": panel,
                             "returns": load_returns(start, end)}
        emit(f"- {label} 窗口 {start}~{end}：候选池 {len(panel)} 行，"
             f"{panel['day'].nunique()} 天")
    labels = list(table_data)
    if len(labels) < 1:
        print("❌ 没有数据")
        return 2
    emit()

    rows: list[dict] = []
    for weight in WEIGHTS:
        for lam in SEAT_LAMBDAS:
            entry: dict = {"weight": weight, "lam": lam, "cells": {}}
            for label in labels:
                panel = table_data[label]["panel"]
                returns = table_data[label]["returns"]
                score = (panel["six_dim"] * weight / 100.0
                         + panel["accumulation"] * (100.0 - weight) / 100.0
                         + panel["seat"] * lam)
                entry["cells"][label] = {
                    "ic": grid_ic(panel, score, returns),
                    "topk": topk_excess(panel, score, returns)}
            # 每一格 = 一个 (窗口, 持有期) 的 IC
            ics = [entry["cells"][label]["ic"].get(horizon, {}).get("ic")
                   for label in labels for horizon in HORIZONS]
            ics = [v for v in ics if v is not None]
            entry["worst_ic"] = min(ics) if ics else float("nan")
            entry["positive_cells"] = sum(1 for v in ics if v > 0)
            entry["mean_ic"] = float(np.mean(ics)) if ics else float("nan")
            excesses = [entry["cells"][label]["topk"].get(horizon, {})
                        .get("excess")
                        for label in labels for horizon in HORIZONS]
            excesses = [v for v in excesses if v is not None]
            entry["worst_excess"] = (min(excesses) if excesses
                                     else float("nan"))
            rows.append(entry)

    emit("## 一、按「六格最差 IC」排序")
    emit()
    emit("| 六维权重 | 席位系数 λ | 最差 IC | 正号格数 | 平均 IC | 最差头部超额(pp) |")
    emit("|---:|---:|---:|---:|---:|---:|")
    for entry in sorted(rows, key=lambda e: -e["worst_ic"])[:14]:
        emit(f"| {entry['weight']:g} | {entry['lam']:g} | "
             f"{entry['worst_ic']:+.4f} | {entry['positive_cells']}/6 | "
             f"{entry['mean_ic']:+.4f} | {entry['worst_excess']:+.2f} |")
    emit()

    emit("## 二、只看层权重（λ=0，**不含席位**，不受上面第 1 条时间差影响）")
    emit()
    emit("| 六维权重 | 最差 IC | 正号格数 | 平均 IC | 最差头部超额(pp) |")
    emit("|---:|---:|---:|---:|---:|")
    for entry in sorted((e for e in rows if e["lam"] == 0.0),
                        key=lambda e: -e["worst_ic"]):
        emit(f"| {entry['weight']:g} | {entry['worst_ic']:+.4f} | "
             f"{entry['positive_cells']}/6 | {entry['mean_ic']:+.4f} | "
             f"{entry['worst_excess']:+.2f} |")
    emit()

    emit("## 二之二、层权重（λ=0）的**逐格**明细 —— 看清是哪一格塌了")
    emit()
    emit("⚠️ 加进第三个窗口（2024-09~2025-09）之后，**没有任何层权重能做到"
         "六格全正**，而按「六格最差」排序 80/20 反超 100/0。"
         "所以必须逐格看，否则会以为「换个权重就能全正」。")
    emit()
    for weight in WEIGHTS:
        entry = next((e for e in rows
                      if e["lam"] == 0.0 and e["weight"] == weight), None)
        if entry is None:
            continue
        cells = []
        for label in labels:
            ic = entry["cells"][label]["ic"]
            top = entry["cells"][label]["topk"]
            for horizon in HORIZONS:
                stat = ic.get(horizon)
                if not stat:
                    continue
                mark = "" if stat["ic"] > 0 else " ⛔"
                excess = top.get(horizon, {}).get("excess", 0.0)
                cells.append(f"{label} H{horizon} {stat['ic']:+.4f}"
                             f"(超额 {excess:+.2f}pp){mark}")
        emit(f"- **{weight:g}/{100 - weight:g}**：" + "；".join(cells))
    emit()

    emit("## 三、逐格明细（含席位里六格最差 IC 的前 6 名）")
    emit()
    for entry in sorted(rows, key=lambda e: -e["worst_ic"])[:6]:
        emit(f"### 六维权重 {entry['weight']:g}、席位 λ={entry['lam']:g}")
        emit()
        emit("| 窗口 | H | IC | 截面 n | IC>0 | 头部超额(pp) | 超额胜率 | 天数 |")
        emit("|---|---:|---:|---:|---:|---:|---:|---:|")
        for label in labels:
            cell = entry["cells"][label]
            for horizon in HORIZONS:
                ic = cell["ic"].get(horizon)
                top = cell["topk"].get(horizon, {})
                if not ic:
                    continue
                emit(f"| {label} | {horizon} | {ic['ic']:+.4f} | {ic['n']} | "
                     f"{ic['win'] * 100:.0f}% | "
                     f"{top.get('excess', float('nan')):+.2f} | "
                     f"{top.get('win', float('nan')) * 100:.0f}% | "
                     f"{top.get('days', 0)} |")
        emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"扫描记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
