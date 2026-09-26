"""离线实验：缺数据的子维度填 **中性 50** vs 现行**重新归一化**，
以及 🩸 **层合成不看 `available`** 造成的"没测到 = 测得极差"。

## 两个不同层面的问题

**第一层（子维度）**：`scoring.weighted_score` 把 `available=False` 的子维度
从**分母剔除**。原意是对的（别把"没取到数"当 0 分），副作用是覆盖率进入排序。

**第二层（层之间，这才是真问题）**：`service.py` 合成两层时是

    base_total = (six_dim.score * six_w + accumulation.score * acc_w) / 100

**没有检查层的 `available`**。而 `weighted_score` 在该层所有子维度都缺时返回
`(0.0, 0.0)`，也就是说"这一层没有数据"的表示就是 **score = 0**。
于是第二层没数据的板块 `base_total` **被腰斩** —— 正是 `weighted_score`
内部刻意避免的那个错误，在上一层又被犯了回来。

实测（`mainline_score_bak` 旧窗口）超过一成候选板块的第二层覆盖率为 **0**，
而它们随后反而跑赢 → `accumulation_coverage` 的 IC 是 **−0.0455 / −0.0805**
（覆盖率本不该有预测能力）。这解释了旧窗口"纯六维优于 50/50"——
V2.3 的 100/0 其实是**掩盖了这个 bug**（把第二层权重设成 0 等于绕过它），
而不是发现了更好的排序。

## 这个脚本只读、不写库

所有子维度分与层的 `available` 都在 payload 里，所以各种合成方案都能
**离线**重算 —— 先量出效果，再决定要不要花一次重打分（约 70 分钟）。

用法：
    .venv\\Scripts\\python.exe scripts\\coverage_fill_experiment.py
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
WINDOWS = (("20231009", "20240806", "旧"),
           ("20240901", "20250930", "中"),
           ("20251001", "20260918", "新"))
HORIZONS = (20, 60)
TOP_K = 10
NEUTRAL = 50.0
#: 层权重网格（六维层占比）
WEIGHT_GRID = (100.0, 80.0, 70.0, 60.0, 50.0)
#: 层内权重（取自 configs/mainline.yaml；ETF 权重 0，不参与）
SIX_WEIGHTS = {"trading": 20.0, "prosperity": 30.0, "moneyflow": 20.0,
               "chips": 10.0, "macro": 5.0, "technical": 15.0}
ACC_WEIGHTS = {"leverage": 25.0, "northbound": 50.0, "volume_price": 25.0}


def load(table: str, start: str, end: str) -> list[dict]:
    """候选池：每行带两层的子维度 `(score, available)` 与层的 `available`。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        item: dict = {"day": str(row["trade_date"]),
                      "code": str(row["board_code"])}
        for layer in (payload.get("layers") or []):
            key = str(layer.get("key") or "")
            if key not in ("six_dim", "accumulation"):
                continue
            # 层自己的 available：全部子维度都缺时是 False
            item[f"{key}_avail"] = bool(layer.get("available"))
            for dim in (layer.get("dimensions") or []):
                dim_key = str(dim.get("key"))
                score = float(dim.get("score") or 0.0)
                # 六维层合成用 `effective_score`（反向维度取 100−score），
                # 而 payload 里存的是**自然分**（见 `DimensionScore`）
                if key == "six_dim" and dim.get("reversed"):
                    score = 100.0 - score
                item[f"{key}:{dim_key}"] = (score,
                                            bool(dim.get("available")))
        out.append(item)
    return out


def layer_score(item: dict, layer: str, weights: dict[str, float],
                mode: str) -> float | None:
    """层内合成。`mode`：`renorm` 剔出缺项分母 / `fill` 缺项填中性 50。"""
    num = usable = total = 0.0
    for dim_key, weight in weights.items():
        cell = item.get(f"{layer}:{dim_key}")
        if cell is None:
            continue
        score, ok = cell
        total += weight
        if ok:
            num += score * weight
            usable += weight
        elif mode == "fill":
            num += NEUTRAL * weight
    if mode == "renorm":
        return None if usable <= 0 else num / usable
    return None if total <= 0 else num / total


def layer_mix(item: dict, w_six: float, *, aware: bool) -> float | None:
    """层间合成。

    - `aware=False`（**现行**）：`(six·w + acc·(100−w)) / 100`，不看层的
      `available` —— 层没数据时它的 score 是 0，于是"没测到"被算成"极差"；
    - `aware=True`（**修后**）：只在**有数据**的层之间按权重平均。
    """
    six = item.get("six_renorm")
    acc = item.get("acc_renorm")
    if not aware:
        return (six or 0.0) * w_six / 100.0 + (acc or 0.0) * (100 - w_six) / 100.0
    parts: list[tuple[float, float]] = []
    if item.get("six_dim_avail") and six is not None:
        parts.append((six, w_six))
    if item.get("accumulation_avail") and acc is not None:
        parts.append((acc, 100.0 - w_six))
    weight = sum(w for _, w in parts)
    if weight <= 0:
        return None
    return sum(value * w for value, w in parts) / weight


def frame_of(records: list[dict], key: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"day": [r["day"] for r in records],
         "code": [r["code"] for r in records],
         "v": [r.get(key) for r in records]}).pivot_table(
        index="day", columns="code", values="v")


def ic_and_excess(records: list[dict], key: str,
                  returns: dict[int, pd.DataFrame]) -> dict:
    """返回 `{horizon: (IC, 头部超额pp)}`。"""
    frame = frame_of(records, key)
    out: dict[int, tuple[float, float]] = {}
    for horizon in HORIZONS:
        ret = returns[horizon]
        series, _ = rank_ic(frame, ret)
        stat = summarize(series, horizon)
        ic = stat["ic"] if stat else float("nan")
        picks: list[float] = []
        pools: list[float] = []
        for day in frame.index:
            if day not in ret.index:
                continue
            row = ret.loc[day]
            scores = frame.loc[day].dropna()
            shared = scores.index.intersection(row.dropna().index)
            if len(shared) < TOP_K:
                continue
            top = scores[shared].sort_values(ascending=False).head(TOP_K).index
            picks.append(float(row[top].mean()))
            pools.append(float(row[shared].mean()))
        excess = (float(np.mean(np.array(picks) - np.array(pools))) * 100.0
                  if picks else float("nan"))
        out[horizon] = (ic, excess)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="覆盖率/层可用性离线实验")
    parser.add_argument("--table", default="mainline_score_bak_20260921_2015")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 离线实验：子维度中性填 50、以及 🩸 层合成不看 `available`")
    emit()
    emit(f"> 由 `scripts/coverage_fill_experiment.py` 生成，表 `{args.table}`。")
    emit("> **核心问题**：`service.py` 合成两层时用 "
         "`(six·w + acc·(100−w))/100`，**不看层的 `available`**；"
         "而层没数据时 `weighted_score` 返回 `score = 0`。"
         "于是「没测到」被算成「测得极差」，第二层没数据的板块分数被腰斩。")
    emit()

    collected: dict[str, dict[str, tuple[float, float]]] = {}
    window_data: dict[str, dict] = {}
    for start, end, label in WINDOWS:
        records = load(args.table, start, end)
        if not records:
            emit(f"- {label} 窗口无数据")
            continue
        returns = load_returns(start, end)
        for record in records:
            for mode in ("renorm", "fill"):
                record[f"six_{mode}"] = layer_score(record, "six_dim",
                                                    SIX_WEIGHTS, mode)
                record[f"acc_{mode}"] = layer_score(record, "accumulation",
                                                    ACC_WEIGHTS, mode)
            for weight in WEIGHT_GRID:
                record[f"naive{weight:g}"] = layer_mix(record, weight,
                                                       aware=False)
                record[f"aware{weight:g}"] = layer_mix(record, weight,
                                                       aware=True)
        window_data[label] = {"records": records, "returns": returns}
        emit(f"## {label} 窗口 {start}~{end}（候选池 {len(records)} 行）")
        emit()

        # ① 子维度两种填法（只看 50/50，隔离层内效应）
        emit("### ① 子维度：现行（剔出分母） vs 填中性 50 —— 层内效应")
        emit()
        emit("| 层 | H | 现行 IC | 填 50 IC | 变化 |")
        emit("|---|---:|---:|---:|---:|")
        for layer, name in (("six", "六维层"), ("acc", "第二层")):
            for mode in ("renorm", "fill"):
                for record in records:
                    record[f"mix_{mode}_{layer}"] = (
                        0.5 * (record.get(f"six_{mode}") or 0.0)
                        + 0.5 * (record.get(f"acc_{mode}") or 0.0))
            for horizon in HORIZONS:
                values = {}
                for mode in ("renorm", "fill"):
                    stats = ic_and_excess(records, f"{layer}_{mode}", returns)
                    values[mode] = stats[horizon][0]
                emit(f"| {name} | {horizon} | {values['renorm']:+.4f} | "
                     f"{values['fill']:+.4f} | "
                     f"{values['fill'] - values['renorm']:+.4f} |")
        emit()

        # ② 层间：现行 vs 修后（各权重）
        emit("### ② 🩸 层间：现行（不看 available） vs 修后（只看有数据的层）")
        emit()
        emit("| 六维权重 | H | 现行 IC | 修后 IC | 变化 | 现行超额(pp) | "
             "修后超额(pp) |")
        emit("|---:|---:|---:|---:|---:|---:|---:|")
        for weight in WEIGHT_GRID:
            for horizon in HORIZONS:
                naive = ic_and_excess(records, f"naive{weight:g}", returns)
                aware = ic_and_excess(records, f"aware{weight:g}", returns)
                emit(f"| {weight:g} | {horizon} | {naive[horizon][0]:+.4f} | "
                     f"{aware[horizon][0]:+.4f} | "
                     f"{aware[horizon][0] - naive[horizon][0]:+.4f} | "
                     f"{naive[horizon][1]:+.2f} | {aware[horizon][1]:+.2f} |")
        emit()
        for weight in WEIGHT_GRID:
            for mode, tag in (("naive", "现行"), ("aware", "修后")):
                stats = ic_and_excess(records, f"{mode}{weight:g}", returns)
                collected.setdefault(f"{tag}{weight:g}", {})[label] = (
                    stats[20][0], stats[60][0], stats[20][1], stats[60][1])

    # ③ 跨窗口六格：修后 vs 现行
    emit("## ③ 层权重的**跨窗口**汇总（每格 = IC，括号内为头部超额 pp）")
    emit()
    emit("⚠️ §16.38 的六格表是在**有 bug 的**合成上算的，"
         "所以那个「没有层权重全正」的结论必须在这里重算。")
    emit()
    for tag in ("现行", "修后"):
        emit(f"### {tag}合成")
        emit()
        emit("| 六维权重 | " + " | ".join(f"{label} H{h}"
                                          for _, _, label in WINDOWS
                                          for h in HORIZONS)
             + " | 最差 IC | 负号格 | 平均 IC | 最差超额 |")
        emit("|---:|" + "---:|" * (len(WINDOWS) * len(HORIZONS) + 4))
        for weight in WEIGHT_GRID:
            key = f"{tag}{weight:g}"
            cells: list[str] = []
            ics: list[float] = []
            excesses: list[float] = []
            for _, _, label in WINDOWS:
                entry = collected.get(key, {}).get(label)
                if entry is None:
                    cells.extend(["—"] * len(HORIZONS))
                    continue
                for index, _horizon in enumerate(HORIZONS):
                    ic = entry[0] if index == 0 else entry[1]
                    excess = entry[2] if index == 0 else entry[3]
                    ics.append(ic)
                    excesses.append(excess)
                    flag = "" if ic > 0 else " ⛔"
                    cells.append(f"{ic:+.4f}({excess:+.2f}){flag}")
            if not ics:
                continue
            emit(f"| {weight:g} | " + " | ".join(cells) + " | "
                 f"{min(ics):+.4f} | {sum(1 for v in ics if v <= 0)}/{len(ics)}"
                 f" | {float(np.mean(ics)):+.4f} | {min(excesses):+.2f} |")
        emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
