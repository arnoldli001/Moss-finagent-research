"""候选池内的因子扫描：**在候选池里**，还有哪个因子能预测未来收益？

## 为什么这个问题是当前最关键的

V2.3 把 `synthesis.layer_weights` 改成 100/0 之后，第二层不再进入合成分。
后果是漏斗**塌成单因子**：

    第一层 six_dim 排名 → 取前 ~20% 进候选池
    第二层（权重 0）    → 对精选排名**毫无影响**
    精选 = 候选池里按 six_dim 再取前 10
         ≈ 全市场按 six_dim 取前 10

也就是说 V2.0 设计的"第一层粗筛、第二层在池内精挑"里，**第二层被关掉了**。
100/0 是实测更好的（`layer_weight_sim.py`：取头部超额 +0.53/+1.16 对
+0.17/+0.30，H=20 两窗口同号），但那只是在"six 独占"与"50/50 混合"之间比。

**真正该问的是**：池内有没有**别的**因子比 six_dim 更会挑？
如果有，第二层就该按那个因子重建，而不是简单地置零。

## 口径

- **只在候选池内算**（`candidate=True`）。全市场 IC 衡量的是"粗筛能力"，
  而这里要的是"精挑能力"，两者是不同的问题。
- 秩相关，逐日横截面，两个窗口分别算，**要求跨窗口同号**才算规律
  （本项目的复现判据）。
- 重叠窗口会把 t 值吹大，所以同时给非重叠 t（`summarize` 里的 `t_ne`）。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts\\pool_factor_scan.py
    .venv\\Scripts\\python.exe scripts\\pool_factor_scan.py \\
        --table mainline_score_bak_20260921_2015
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

from scripts.factor_ic_report import (  # noqa: E402
    load_returns,
    rank_ic,
    summarize,
)

DB = ROOT / "data" / "moss_finagent.db"
WINDOWS = (("20231009", "20240806", "旧区间"), ("20251001", "20260918", "新区间"))
HORIZONS = (20, 60)
#: 每层直接给的分数（不走 dimensions）
SCALARS = ("six_dim_score", "accumulation_score", "leader_score",
           "base_total", "total", "etf_bonus", "gate_bonus")


def load_table(table: str, start: str, end: str) -> pd.DataFrame:
    """读成一张长表：每行一块板一天，列是各因子值。"""
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
            continue                       # 只看候选池：问的是"精挑"能力
        item: dict = {"day": str(row["trade_date"]),
                      "code": str(row["board_code"]),
                      "resonance": 1.0 if payload.get("resonance") else 0.0}
        for name in SCALARS:
            item[name] = float(payload.get(name) or 0.0)
        for layer in (payload.get("layers") or []):
            key = str(layer.get("key") or "")
            for dim in (layer.get("dimensions") or []):
                if not dim.get("available"):
                    continue
                item[f"{key}.{dim.get('key')}"] = float(dim.get("score") or 0.0)
        records.append(item)
    return pd.DataFrame(records)


def to_frame(panel: pd.DataFrame, column: str) -> pd.DataFrame:
    """长表 → `索引=日期、列=板块` 的因子表。"""
    return panel.pivot_table(index="day", columns="code", values=column)


def ic_with_coverage(frame: pd.DataFrame, returns: dict[int, pd.DataFrame],
                     horizon: int) -> tuple[dict | None, int]:
    """算 IC，并**同时返回每日截面的中位样本数**。

    ⚠️ 这一步不能省：像 `leader.seat` 这种"未确认席位就 `available=False`"
    的维度，在很多交易日只有个位数的板块有值。秩相关在 n=2~3 时几乎是
    纯噪声（n=2 时 ±1 各半），只报一个正的均值 IC 会让人以为发现了规律。
    所以必须把 n 一起看 —— 本项目已经栽过"小样本看起来像信号"的跟头。

    ⚠️⚠️ 但**只有 n 还不够**，这是本项目第 5 轮踩到的第二次：
    `rank_ic` 要求每天 ≥20 个共有样本，否则整天跳过。所以一个因子可能
    "每天 n=23 看起来很不错"，**却只有 13 天能算** ——
    另一个窗口甚至只有 7% 的交易日够门槛。
    所以 `summarize` 的 `days`（可用天数）与 `n` **必须一起报**，
    并把 `days` 太少的因子标出来。只看 n 会把"样本天数极少"误判成"样本充足"。
    """
    series, median_n = rank_ic(frame, returns[horizon])
    stat = summarize(series, horizon)
    return stat, median_n


def main() -> int:
    parser = argparse.ArgumentParser(description="候选池内的因子扫描")
    parser.add_argument("--table", default="mainline_score_bak_20260921_2015",
                        help="评分表；默认用 V2.2 备份（两个窗口都完整）")
    parser.add_argument("--top", type=int, default=0,
                        help="只列出前 N 名（0 = 全部）")
    parser.add_argument("--out", default="",
                        help="写出 Markdown 记录（可复现产物）")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 候选池内的因子扫描（精挑能力）")
    emit()
    emit(f"> 由 `scripts/pool_factor_scan.py` 生成，数据表 `{args.table}`。")
    emit("> **只在候选池内**算：全市场 IC 衡量『粗筛』，池内 IC 衡量『精挑』，")
    emit("> 是两个不同的问题。**跨窗口同号**才算规律（本项目的复现判据）。")
    emit("> `n` = 每日截面中位样本数；`n < 10` 的 IC 基本是噪声。")
    emit()

    results: dict[str, dict[str, dict]] = {}
    for start, end, label in WINDOWS:
        panel = load_table(args.table, start, end)
        if panel.empty:
            emit(f"{label}：没有候选池数据，跳过")
            continue
        returns = load_returns(start, end)
        columns = [c for c in panel.columns
                   if c not in ("day", "code", "resonance")]
        rows: dict[str, dict] = {}
        for column in columns:
            frame = to_frame(panel, column)
            entry: dict = {"n": 0}
            for horizon in HORIZONS:
                stat, median_n = ic_with_coverage(frame, returns, horizon)
                if stat:
                    entry[horizon] = stat
                    entry["n"] = max(entry["n"], median_n)
            if len(entry) > 1:
                rows[column] = entry
        results[label] = rows
        emit(f"【{label} {start}~{end}】候选池 {len(panel)} 行，"
              f"{panel['day'].nunique()} 天，因子 {len(rows)} 个")

    labels = [label for _, _, label in WINDOWS if label in results]
    if not labels:
        emit("❌ 两个窗口都没有数据")
        return 2

    # 汇总：按 H=20 的**跨窗口最差** IC 排序（要的是两边都行，不是一边很好）
    summary: list[dict] = []
    for column in sorted({c for rows in results.values() for c in rows}):
        old = results.get(labels[0], {}).get(column, {}).get(20)
        new = (results.get(labels[-1], {}).get(column, {}).get(20)
               if len(labels) > 1 else None)
        if old is None:
            continue
        both = [old["ic"]] + ([new["ic"]] if new else [])
        summary.append({
            "factor": column,
            "old_ic": old["ic"], "old_win": old["win"], "old_t": old["t_ne"],
            "old_n": results[labels[0]].get(column, {}).get("n", 0),
            "old_days": old.get("days", 0),
            "new_ic": new["ic"] if new else float("nan"),
            "new_win": new["win"] if new else float("nan"),
            "new_t": new["t_ne"] if new else float("nan"),
            # ⚠️ 用 `.get`：有些因子只在一个窗口里有数据
            # （`six_dim.macro` 在新区间整个缺失），直接下标会 KeyError
            "new_n": (results[labels[-1]].get(column, {}).get("n", 0)
                      if len(labels) > 1 else 0),
            "new_days": new.get("days", 0) if new else 0,
            "worst": min(both), "mean": float(np.mean(both)),
            "same_sign": (len(both) > 1 and (all(v > 0 for v in both)
                                             or all(v < 0 for v in both))),
        })
    summary.sort(key=lambda item: -item["worst"])

    emit()
    emit("=" * 118)
    emit("候选池内 H=20 的秩相关 IC（按「跨窗口最差」排序 —— "
          "要的是两个窗口都能用，不是单窗口漂亮）")
    emit("-" * 118)
    emit(f"  {'因子':<30}{'旧IC':>9}{'旧n':>5}{'旧天':>5}"
          f"{'新IC':>9}{'新n':>5}{'新天':>5}{'最差':>9}{'同号':>5}  说明")
    shown = summary[:args.top] if args.top else summary
    for item in shown:
        notes: list[str] = []
        # ① 每日截面中位样本数 < 10 的 IC 基本不可信（n=2 时 ±1 各半）
        if min(item["old_n"], item["new_n"] or item["old_n"]) < 10:
            notes.append("截面样本过小")
        # ② ⚠️ 更隐蔽的一条：`rank_ic` 要求每天 ≥20 个共有样本，
        # 所以"每天 n 够"不代表"有足够多的天"。`leader.seat` 就是
        # 每天 n=23 却只有 14 天能算（另一个窗口只有 7% 的交易日够门槛）——
        # 只看 n 会把它误判成"样本充足"。
        if min(item["old_days"], item["new_days"]) < 30:
            notes.append(f"可用天数过少（旧 {item['old_days']} / "
                         f"新 {item['new_days']}）")
        emit(f"  {item['factor']:<30}{item['old_ic']:>9.4f}{item['old_n']:>5}"
              f"{item['old_days']:>5}{item['new_ic']:>9.4f}{item['new_n']:>5}"
              f"{item['new_days']:>5}{item['worst']:>9.4f}"
              f"{'是' if item['same_sign'] else '否':>5}"
              f"  {'⚠️ ' + '；'.join(notes) if notes else ''}")

    emit()
    emit("=" * 118)
    emit("候选池内 H=60（`n` = 每日截面中位样本数，`天` = 可用天数）")
    emit("-" * 118)
    emit(f"  {'因子':<30}{'旧IC':>9}{'旧n':>5}{'旧天':>5}"
          f"{'新IC':>9}{'新n':>5}{'新天':>5}{'最差':>9}")
    rows60: list[tuple[str, float, int, int, float, int, int, float]] = []
    for column in sorted({c for rows in results.values() for c in rows}):
        old = results.get(labels[0], {}).get(column, {}).get(60)
        new = (results.get(labels[-1], {}).get(column, {}).get(60)
               if len(labels) > 1 else None)
        if old is None:
            continue
        both = [old["ic"]] + ([new["ic"]] if new else [])
        old_days = old.get("days", 0)
        new_days = new.get("days", 0) if new else 0
        rows60.append((column, old["ic"],
                       results[labels[0]].get(column, {}).get("n", 0), old_days,
                       new["ic"] if new else float("nan"),
                       (results[labels[-1]].get(column, {}).get("n", 0)
                        if len(labels) > 1 else 0), new_days,
                       min(both)))
    for column, old_ic, old_n, old_days, new_ic, new_n, new_days, worst in sorted(
            rows60, key=lambda r: -r[7]):
        notes: list[str] = []
        if min(old_n, new_n or old_n) < 10:
            notes.append("样本过小")
        if min(old_days, new_days) < 30:
            notes.append(f"天数过少（旧 {old_days} / 新 {new_days}）")
        emit(f"  {column:<30}{old_ic:>9.4f}{old_n:>5}{old_days:>5}"
              f"{new_ic:>9.4f}{new_n:>5}{new_days:>5}{worst:>9.4f}"
              f"{'  ⚠️ ' + '；'.join(notes) if notes else ''}")

    emit()
    emit("提示：IC 只说明排序能力，**不能直接当结论**。挑出候选因子之后，"
          "用 `scripts/pool_score_scan.py`（同样的四格判据 + 取头部超额收益）"
          "把因子拼进打分方案再比一次 —— IC 好但头部没变化等于什么都没做。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"扫描记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
