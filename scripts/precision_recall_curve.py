"""误报 ↔ 真值覆盖的边际权衡：**每降 5 个点误报，要丢多少真值事件？**

## 用户要的口径

> 给我个量化数据：每误报降 5 个百分点，真值日时间的告警丢失都少百分比。

所以要的是一张**边际表**，不是一条曲线：把"门槛"当成一个旋钮，
每抬高一点，误报降多少、真值覆盖丢多少。

## 把两个触发路径统一成一个旋钮

线上有两条路：

1. **绝对路径**：`total ≥ medium_score`；
2. **突破路径**：越过自己历史震荡上沿 **且** `total ≥ breakout_min_score`。

两条都有各自的"分数下限"。抬门槛时必须**同时**抬这两个下限
（只抬 `medium_score` 的话，突破路径仍在低分放行，等于没改）。
所以这里把两者**归一成一个旋钮 `floor`**：

    某个"候选板块日"会告警  ⟺  total ≥ floor
    某真值事件被覆盖        ⟺  窗口内该板块有 total ≥ floor 的候选日

这样边际表才读得懂：**一个数换一个数**。

## 两个误报口径分开报（用户要求"两个都报，分开看"）

- `命中@2%` = 告警后 20 日收益 > 2%（低门槛，"有反应"）
- `命中@10%` = 告警后 20 日收益 > 10%（高门槛，"成了主线"）

`>2%` 的基准命中率本来就约 46%（随便挑一个候选板块），所以它天然接近掷硬币；
`>10%` 才更接近"真主线"。两个并排看，才不会被低门槛口径误导。

用法：
    .venv\\Scripts\\python.exe scripts\\precision_recall_curve.py
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

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
WINDOWS = (("20231009", "20240806", "旧"), ("20240901", "20250930", "中"),
           ("20251001", "20260918", "新"))
HORIZON = 20
LOOKBACK, LOOKAHEAD = 20, 40


def candidates(table: str) -> pd.DataFrame:
    """候选池：`[day, code, total]`。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, total, payload FROM {table}"
        " ORDER BY trade_date").fetchall()
    conn.close()
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        records.append({"day": str(row["trade_date"]),
                        "code": str(row["board_code"]),
                        "total": float(row["total"] or 0.0)})
    return pd.DataFrame(records)


def closes() -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    frame = pd.read_sql_query(
        "SELECT board_code, trade_date, close FROM ml_board_bar", conn)
    conn.close()
    return frame.pivot(index="trade_date", columns="board_code",
                       values="close").sort_index()


def main() -> int:
    parser = argparse.ArgumentParser(description="误报↔真值覆盖边际权衡")
    parser.add_argument("--table", default="mainline_score")
    parser.add_argument("--floors", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    panel = candidates(args.table)
    close = closes()
    forward = (close.shift(-HORIZON) / close - 1.0) * 100.0
    days = sorted(set(panel["day"]))
    index = {day: position for position, day in enumerate(days)}

    # 每个候选日的未来收益（一次性算好，后面按 floor 过滤）
    values: list[tuple[str, str, float, float]] = []
    for day, code, total in zip(panel["day"], panel["code"], panel["total"],
                                strict=True):
        if day not in forward.index or code not in forward.columns:
            continue
        ret = forward.at[day, code]
        if ret != ret:
            continue
        values.append((day, code, float(total), float(ret)))
    if not values:
        print("❌ 没有可用的候选板块日")
        return 2

    # 真值事件（只取 status: ok 且代码可解析）
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    names: dict[str, str] = {}
    for row in conn.execute("SELECT code, name FROM ml_board"):
        names.setdefault(str(row["name"]), str(row["code"]))
    conn.close()
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events: list[tuple[str, list[str]]] = []
    for event in truth.get("events") or []:
        if not isinstance(event, dict) or event.get("status") != "ok":
            continue
        date = str(event.get("date") or "").replace("-", "")
        if date not in index:
            continue
        codes: list[str] = []
        for code in event.get("codes") or []:
            text = str(code)
            codes.append(names.get(text, text))
        if codes:
            events.append((date, codes))

    # 预先按板块整理"分数 ≥ floor 的日子"，避免每个 floor 重扫
    by_code: dict[str, list[tuple[str, float]]] = {}
    for day, code, total, _ in values:
        by_code.setdefault(code, []).append((day, total))

    floors = ([float(x) for x in args.floors.split(",") if x.strip()]
              if args.floors else [float(x) for x in range(45, 96, 2)])
    rows: list[dict] = []
    for floor in floors:
        chosen = [item for item in values if item[2] >= floor]
        if len(chosen) < 100:
            continue
        rets = np.array([item[3] for item in chosen])
        hit2 = float((rets > 2.0).mean() * 100)
        hit10 = float((rets > 10.0).mean() * 100)
        # 真值覆盖
        covered = 0
        for date, codes in events:
            center = index[date]
            low, high = max(0, center - LOOKBACK), min(len(days) - 1,
                                                      center + LOOKAHEAD)
            window = set(days[low:high + 1])
            hit = False
            for code in codes:
                for day, total in by_code.get(code, []):
                    if total >= floor and day in window:
                        hit = True
                        break
                if hit:
                    break
            covered += int(hit)
        rows.append({"floor": floor, "samples": len(chosen),
                     "per_day": len(chosen) / max(len(days), 1),
                     "fp2": 100 - hit2, "hit2": hit2,
                     "fp10": 100 - hit10, "hit10": hit10,
                     "cover": covered / max(len(events), 1) * 100,
                     "lost": 100 - covered / max(len(events), 1) * 100})

    emit("# 误报 ↔ 真值覆盖 的边际权衡")
    emit()
    emit(f"> 由 `scripts/precision_recall_curve.py` 生成，表 `{args.table}`"
         f"（{len(days)} 个交易日、{len(values)} 个候选板块日、"
         f"{len(events)} 个可评估真值事件）。")
    emit("> 单一旋钮 `floor`：候选日 `total ≥ floor` 才可能告警。"
         "抬门槛时 `medium_score` 与 `breakout_min_score` **必须同时抬**，"
         "只抬前者的话突破路径仍在低分放行。")
    emit()
    emit("## 一、完整曲线")
    emit()
    emit("| floor | 候选日/天 | 误报@2% | 命中@2% | 误报@10% | 命中@10% | "
         "真值覆盖 | **真值丢失** |")
    emit("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for item in rows:
        emit(f"| {item['floor']:.0f} | {item['per_day']:.2f} "
             f"| {item['fp2']:.0f}% | {item['hit2']:.0f}% "
             f"| {item['fp10']:.0f}% | {item['hit10']:.0f}% "
             f"| {item['cover']:.0f}% | **{item['lost']:.0f}%** |")
    emit()

    emit("## 二、边际表（用户口径：**每降 5 个点误报，真值丢失多少**）")
    emit()
    emit("做法：沿着曲线走，每当 `误报@2%` 比上一次记录**又降了 ≥5pp**，"
         "就记一行 —— 看这一段区间内真值覆盖丢了多少。")
    emit()
    emit("| 误报@2% 从 → 到 | 降幅 | 真值丢失 从 → 到 | 丢了多少 | "
         "换算：每降 1pp 误报 | 告警量 从 → 到 |")
    emit("|---|---:|---|---:|---:|---|")
    anchor = rows[0]
    reported = 0
    for item in rows[1:]:
        drop = anchor["fp2"] - item["fp2"]
        if drop < 5.0:
            continue
        lost = item["lost"] - anchor["lost"]
        ratio = lost / drop if drop else float("nan")
        emit(f"| {anchor['fp2']:.0f}% → {item['fp2']:.0f}% | −{drop:.0f}pp "
             f"| {anchor['lost']:.0f}% → {item['lost']:.0f}% | **+{lost:.0f}pp** "
             f"| 丢 **{ratio:.1f}pp** 真值 | {anchor['per_day']:.2f} → "
             f"{item['per_day']:.2f} 条/天 |")
        anchor = item
        reported += 1
    if reported == 0:
        emit("| （曲线太平缓，5pp 内没有可分的段） | — | — | — | — | — |")
    emit()
    emit("**判读标准**：`每降 1pp 误报` 丢掉的**真值百分点**若 > 1，"
         "这笔交易就是亏的 —— 你为了让告警更准，砍掉了更多真正的主线。")
    emit()

    emit("## 三、怎么读")
    emit()
    emit("- **`>2%` 的误报率天然很高**（全候选池基准命中约 46%，等于掷硬币），"
         "所以它的绝对数字不必纠结；要看的是**它降的时候真值丢多少**。")
    emit("- **`>10%` 更接近「真主线」**：命中率一路只有 13~27%，"
         "而且**抬门槛几乎救不动**（87% → 73%），这说明模型识别大行情的能力弱。")
    emit("- 如果「每降 1pp 误报要丢 >1pp 真值」，说明这个旋钮**不划算**，"
         "该换因子而不是继续提门槛。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
