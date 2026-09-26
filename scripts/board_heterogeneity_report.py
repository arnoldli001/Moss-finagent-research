"""板块异质性报告：**能不能给每个板块一套自己的门槛/权重？**

## 用户提出的问题

> 每个概念板块的主线触发门槛可能不同，不能用一套相同打分权重去算吧。

这句话其实包含**两个不同的问题**，风险完全不同，必须分开回答：

1. **门槛（trigger threshold）**：`medium_score`/`strong_score` 是**全市场
   统一**的绝对分。但板块的分数分布差异极大 —— 有的长期 40~55、有的
   长期 70~85。统一门槛下，前者**永远不可能报**，后者**隔三差五就报**。
   这是**测量尺度**问题，可以用"每个板块自己的历史分位"解决，
   **几乎不增加自由参数**。

2. **权重（scoring weights）**：给每个板块一套自己的六维权重
   （324 个板块 × 6 维 = 1944 个参数）。直觉上合理（煤炭看煤价/高股息、
   医药看研发/政策），但统计上极危险：板块之间高度同涨同跌，
   有效样本远小于表面天数。**必须先验证"每个板块的最优维度是否稳定"** ——
   若跨窗口不稳定，那套权重就是在拟合噪声。

本脚本就做这两件事，都用**严格 PIT 的做法**测量，只读不写库。

## 判据

- 门槛：看"统一线之下，各板块分数分布的分散程度"。
  若分散度很大 → 统一绝对线确实不合理。
- 权重：对每个板块算"每个维度与它自己未来 20 日收益"的相关，
  再看**跨窗口**这个排序稳不稳。若 **最优维度的跨窗口一致率接近随机**
  （1/6 ≈ 17%），就不该做 per-board 权重。

用法：
    .venv\\Scripts\\python.exe scripts\\board_heterogeneity_report.py
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

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
WINDOWS = (("20231009", "20240806", "旧"), ("20251001", "20260918", "新"))
HORIZON = 20
DIMS = ("trading", "prosperity", "moneyflow", "chips", "macro", "technical")
#: 全局门槛（V2.4 标定值），用来看"统一线"对每个板块意味着什么
GLOBAL_MEDIUM = 77.0


def load(table: str, start: str, end: str) -> pd.DataFrame:
    """长表：`[day, code, six, total, trading, prosperity, ...]`（只取候选池）。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, total, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        item: dict = {"day": str(row["trade_date"]),
                      "code": str(row["board_code"]),
                      "total": float(row["total"] or 0.0),
                      "six": float(payload.get("six_dim_score") or 0.0)}
        for layer in (payload.get("layers") or []):
            if str(layer.get("key")) != "six_dim":
                continue
            for dim in (layer.get("dimensions") or []):
                key = str(dim.get("key"))
                if key in DIMS and dim.get("available"):
                    # 反向维度取 effective（层合成用的就是它）
                    score = float(dim.get("score") or 0.0)
                    if dim.get("reversed"):
                        score = 100.0 - score
                    item[key] = score
        records.append(item)
    return pd.DataFrame(records)


def forward_returns(start: str, end: str) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    frame = pd.read_sql_query(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", conn, params=(start, end))
    conn.close()
    wide = frame.pivot(index="trade_date", columns="board_code",
                       values="close").sort_index()
    return wide.shift(-HORIZON) / wide - 1.0


def main() -> int:
    parser = argparse.ArgumentParser(description="板块异质性报告")
    parser.add_argument("--table", default="mainline_score_bak_20260921_2015")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 板块异质性报告：每板块一套门槛/权重，可行吗？")
    emit()
    emit(f"> 由 `scripts/board_heterogeneity_report.py` 生成，表 `{args.table}`。")
    emit("> **门槛**与**权重**是两个问题：前者是测量尺度（低风险、可用"
         "板块自身分位解决），后者要 324×6 个参数（必须先验证稳定性）。")
    emit()

    per_board: dict[str, pd.DataFrame] = {}
    frames: dict[str, pd.DataFrame] = {}
    for start, end, label in WINDOWS:
        panel = load(args.table, start, end)
        if panel.empty:
            continue
        frames[label] = panel
        per_board[label] = panel

    # ---------- 一、门槛：分数分布的跨板块分散度 ----------
    emit("## 一、门槛问题：同一个绝对线对不同板块意味着什么")
    emit()
    for label, panel in frames.items():
        stats = panel.groupby("code")["six"].agg(["mean", "std", "max",
                                                 "count"])
        stats = stats[stats["count"] >= 60]
        if stats.empty:
            continue
        above = panel.assign(hit=panel["total"] >= GLOBAL_MEDIUM) \
                     .groupby("code")["hit"].mean()
        emit(f"### {label} 窗口（{len(stats)} 个板块有 ≥60 天）")
        emit()
        emit("| 统计量（跨板块） | 均值 | p10 | p50 | p90 | 极差 |")
        emit("|---|---:|---:|---:|---:|---:|")
        for column, name in (("mean", "六维分均值"), ("std", "六维分波动"),
                             ("max", "六维分最高")):
            values = stats[column].to_numpy()
            emit(f"| {name} | {values.mean():.1f} | "
                 f"{np.percentile(values, 10):.1f} | "
                 f"{np.percentile(values, 50):.1f} | "
                 f"{np.percentile(values, 90):.1f} | "
                 f"{values.max() - values.min():.1f} |")
        values = above.to_numpy()
        emit(f"| 高于全局线 {GLOBAL_MEDIUM:g} 的天数占比 | {values.mean() * 100:.1f}% "
             f"| {np.percentile(values, 10) * 100:.1f}% "
             f"| {np.percentile(values, 50) * 100:.1f}% "
             f"| {np.percentile(values, 90) * 100:.1f}% "
             f"| {values.max() - values.min():.1f} |")
        never = int((values <= 0.001).sum())
        always = int((values >= 0.30).sum())
        emit()
        emit(f"- **从不越过全局线**的板块：**{never}** 个"
             f"（{never / len(values) * 100:.0f}%）")
        emit(f"- **≥30% 的天数都在全局线之上**的板块：**{always}** 个"
             f"（{always / len(values) * 100:.0f}%）")
        emit()

    # ---------- 二、权重：每板块最优维度是否跨窗口稳定 ----------
    emit("## 二、权重问题：每个板块的「最优维度」跨窗口稳定吗？")
    emit()
    emit("做法：对每个板块，算每个维度与该板块**自己未来 "
         f"{HORIZON} 日收益**的相关（时间序列，不是横截面）；"
         "再看两个窗口里「排名第 1 的维度」是否一致。")
    emit()
    emit("若一致率接近随机的 1/6 ≈ 17%，说明 per-board 权重是在拟合噪声。")
    emit()
    rank1: dict[str, dict[str, str]] = {}
    ic_table: dict[str, dict[str, dict[str, float]]] = {}
    for label, panel in frames.items():
        returns = forward_returns(*[w for w in WINDOWS if w[2] == label][0][:2])
        per: dict[str, dict[str, float]] = {}
        for code, group in panel.groupby("code"):
            series = group.set_index("day")
            if len(series) < 60:
                continue
            target = returns.get(code)
            if target is None:
                continue
            joined = series.join(target.rename("ret"), how="inner").dropna(
                subset=["ret"])
            if len(joined) < 40:
                continue
            row: dict[str, float] = {}
            for dim in DIMS:
                if dim not in joined:
                    continue
                pair = joined[[dim, "ret"]].dropna()
                if len(pair) < 40 or pair[dim].std() == 0:
                    continue
                row[dim] = float(pair[dim].corr(pair["ret"]))
            if len(row) >= 3:
                per[code] = row
        ic_table[label] = per
        rank1[label] = {code: max(row, key=lambda k: row[k])
                        for code, row in per.items()}

    labels = [label for _, _, label in WINDOWS if label in rank1]
    if len(labels) >= 2:
        left, right = labels[0], labels[-1]
        common = sorted(set(rank1[left]) & set(rank1[right]))
        if common:
            same = sum(1 for code in common
                       if rank1[left][code] == rank1[right][code])
            emit(f"- 两窗口都有足够数据的板块：**{len(common)}** 个")
            emit(f"- 「最优维度完全相同」的：**{same}** 个 = "
                 f"**{same / len(common) * 100:.0f}%**"
                 f"（随机基线约 {100 / len(DIMS):.0f}%）")
            # 每个维度的"胜出次数"对比
            emit()
            emit("| 维度 | 旧窗口胜出次数 | 新窗口胜出次数 |")
            emit("|---|---:|---:|")
            for dim in DIMS:
                emit(f"| `{dim}` | "
                     f"{sum(1 for c in common if rank1[left][c] == dim)} | "
                     f"{sum(1 for c in common if rank1[right][c] == dim)} |")
            # 两个窗口里"最优维度"的排序相关（对整个维度集合）
            pair_corrs: list[float] = []
            for code in common:
                a = ic_table[left][code]
                b = ic_table[right][code]
                dims = sorted(set(a) & set(b))
                if len(dims) < 4:
                    continue
                va = pd.Series([a[d] for d in dims]).rank().to_numpy()
                vb = pd.Series([b[d] for d in dims]).rank().to_numpy()
                if va.std() == 0 or vb.std() == 0:
                    continue
                pair_corrs.append(float(np.corrcoef(va, vb)[0, 1]))
            if pair_corrs:
                emit()
                emit(f"- 每个板块「六个维度的 IC 排序」在两窗口之间的相关："
                     f"中位 **{np.median(pair_corrs):+.3f}**"
                     f"（{len(pair_corrs)} 个板块）")
                emit("  —— 接近 0 表示「这个板块这次谁最有用」与「下次」无关。")
    emit()

    emit("## 三、结论")
    emit()
    emit("1. **门槛必须按板块归一**：统一绝对线让一批板块永远报不出来、"
         "另一批天天报。这可以只用「该板块自己的历史分位」实现，"
         "**几乎不增加自由参数**（突破触发就是这么做的）。")
    emit("2. **per-board 权重不要做**（若上面的稳定率接近随机）："
         "板块之间高度同涨同跌，per-board 最优维度跨窗口不一致，"
         "拟合出来的是噪声。要放宽，只能按**风格分组**（周期/成长/红利，"
         "3~5 组）或按「该板块自己近期的 IC」慢速自适应。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
