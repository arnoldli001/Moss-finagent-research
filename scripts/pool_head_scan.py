"""告警头部内的因子扫描：**在真正会告警的那批板里**，还能不能把误报挑掉？

## 为什么必须单独看头部

`pool_bigmove_scan.py` 扫了全候选池的 145 个因子，结论是：**没有一个**
因子能同时在三个窗口拿到 `rel_big` AUC ≥ 0.52；连现有 `total` 自己都是
0.5263 / 0.4884 / 0.4951 —— **中窗口和新窗口直接反向**。

但告警从来不是在全池发的。真正要回答的是用户的问题：
「误报影响大，精度能不能再提？」—— 这个决策只发生在**头部**
（分数最高的那一小撮）里。全池 AUC 描述的是"粗排序"，头部 AUC 描述的
才是"发不发这条告警"。

两者可以完全不一致：一个因子在全池里毫无单调性，却能在头部把
"会涨的"和"不会涨的"分开。本项目已经见过这种形态（头部超额为正、
整体 IC 翻负）。

## 口径

- 头部定义：当日候选池内 `total` 分位 ≥ `--head-quantile`（默认 0.8）。
- 头部内再按**当日分位**给每个特征排序，算
  - `AUC_big`：未来 20 日收盘涨超 10%（用户口径）；
  - `AUC_rel`：未来 20 日收益高于当日全市场中位数。
- 复现判据与主扫描一致：三窗口点估计同向 + 按日 block bootstrap 5% 分位。
- 同时报头部基准率，用来判断"还有多少空间"：如果头部基准率已经很高，
  天花板就在那儿，任何因子的提升幅度都不会大。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/pool_head_scan.py --out docs/MAINLINE_HEAD_SCAN.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.pool_bigmove_scan import (  # noqa: E402
    AUC_FLOOR,
    BIG,
    BOOT,
    BOOT_FLOOR,
    HORIZON,
    WINDOWS,
    auc_rank,
    bootstrap_auc,
    load_bars,
    load_labels,
    load_scores,
    pooled,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="告警头部内的因子扫描")
    parser.add_argument("--table", default="mainline_score")
    parser.add_argument("--head-quantile", type=float, default=0.8)
    parser.add_argument("--out", default="")
    parser.add_argument("--json", default="docs/mainline_iterations/head_quality.json")
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--boot-top", type=int, default=12,
                        help="对点估计最好的前 N 个因子做 bootstrap")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 告警头部内的因子扫描")
    emit()
    emit(f"> 由 `scripts/pool_head_scan.py` 生成，评分表 `{args.table}`。")
    emit(f"> 头部定义：当日候选池内 `total` 分位 ≥ {args.head_quantile:g}。")
    emit("> 复现判据同主扫描：三窗口同向 + 按日 block bootstrap 5% 分位 > 0.5。")
    emit()

    windows_data: dict[str, dict] = {}
    for start, end, label in WINDOWS:
        frames, mask = load_scores(args.table, start, end)
        if not frames:
            emit(f"【{label}】没有评分数据，跳过")
            continue
        total = frames["total"]
        # 当日候选池内 total 的分位 → 头部掩码
        rank_pct = total.where(mask.fillna(0.0).astype(bool)).rank(axis=1, pct=True)
        head = mask.fillna(0.0).astype(bool) & (rank_pct >= args.head_quantile)
        labels = load_labels(start, end)
        for name, frame in load_bars(start, end).items():
            frames[name] = frame
        windows_data[label] = {"frames": frames, "mask": mask, "head": head,
                               **labels}
        pool_rows = int(np.nansum(mask.to_numpy()))
        head_rows = int(head.to_numpy().sum())
        emit(f"- 【{label} {start}~{end}】候选 {pool_rows} 行 → 头部 {head_rows} 行"
              f"（{head_rows / max(pool_rows, 1):.0%}）")

    if len(windows_data) < 3:
        emit(f"❌ 只有 {len(windows_data)} 个窗口有数据")
        return 2
    labels_all = [label for _, _, label in WINDOWS if label in windows_data]

    # ---------- 头部基准率：还剩多少空间 ----------
    emit()
    emit("## 一、头部基准率（天花板在哪里）")
    emit()
    emit("| 窗口 | 池内 `big` 基准 | 头部 `big` 基准 | 池内 `rel_big` | 头部 `rel_big` |")
    emit("|---|---:|---:|---:|---:|")

    def rate(frame: pd.DataFrame, mask: pd.DataFrame) -> float:
        values = frame.where(mask.fillna(0.0).astype(bool)).to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        return float(values.mean()) if values.size else float("nan")

    for label in labels_all:
        shelf = windows_data[label]
        big = (shelf["fwd"] > BIG).astype(float)
        rel = (shelf["rel"] > 0).astype(float)
        emit(f"| {label} | {rate(big, shelf['mask']):.1%} "
             f"| **{rate(big, shelf['head']):.1%}** "
             f"| {rate(rel, shelf['mask']):.1%} "
             f"| {rate(rel, shelf['head']):.1%} |")

    # ---------- 头部内逐因子 ----------
    names = sorted({name for item in windows_data.values() for name in item["frames"]})
    stats: dict[str, dict[str, dict]] = {}
    for name in names:
        stats[name] = {}
        for label in labels_all:
            shelf = windows_data[label]
            feat = shelf["frames"].get(name)
            if feat is None:
                continue
            entry: dict = {}
            big = (shelf["fwd"] > BIG).astype(float)
            rel = (shelf["rel"] > 0).astype(float)
            for tag, lab in (("big", big), ("rel", rel)):
                xs, ys, day_index, days = pooled(feat, shelf["head"], lab)
                if xs.size == 0:
                    continue
                entry[tag] = {"auc": auc_rank(xs, ys), "rows": int(xs.size),
                              "days": days, "base": float(ys.mean()),
                              "x": xs, "y": ys, "day_index": day_index}
            if entry:
                stats[name][label] = entry

    def worst(tag: str, name: str) -> float:
        values = [stats.get(name, {}).get(label, {}).get(tag, {}).get(
            "auc", float("nan")) for label in labels_all]
        values = [v for v in values if np.isfinite(v)]
        return min(values) if len(values) == len(labels_all) else float("nan")

    emit()
    emit("## 二、头部内单因子 AUC（按三窗口最差 `AUC_big` 排序）")
    emit()
    emit("| 因子 | " + " | ".join(f"{lb} big" for lb in labels_all)
         + " | 最差big | " + " | ".join(f"{lb} rel" for lb in labels_all)
         + " | 最差rel | 行数 |")
    emit("|---|" + "---:|" * (2 * len(labels_all) + 2))
    ranked = sorted((n for n in names if np.isfinite(worst("big", n))),
                    key=lambda n: -worst("big", n))
    for name in ranked[:30]:
        cells = []
        rows = 0
        for tag in ("big", "rel"):
            for lb in labels_all:
                got = stats[name][lb].get(tag)
                cells.append(f"{got['auc']:.4f}" if got else "—")
                if got:
                    rows = max(rows, got["rows"])
        emit(f"| `{name}` | " + " | ".join(cells[:len(labels_all)])
             + f" | **{worst('big', name):.4f}** | "
             + " | ".join(cells[len(labels_all):])
             + f" | **{worst('rel', name):.4f}** | {rows} |")

    emit()
    emit("### 基准对照")
    emit()
    emit("| 因子 | " + " | ".join(f"{lb} big" for lb in labels_all) + " | 最差big |")
    emit("|---|" + "---:|" * (len(labels_all) + 1))
    for name in ("total", "base_total", "six_dim_score", "accumulation_score",
                 "rank_neg", "breakout", "bonus_potential"):
        if name not in stats:
            continue
        cells = []
        for lb in labels_all:
            got = stats[name][lb].get("big")
            cells.append(f"{got['auc']:.4f}" if got else "—")
        emit(f"| `{name}` | " + " | ".join(cells)
             + f" | **{worst('big', name):.4f}** |")

    # ---------- bootstrap 前 N ----------
    emit()
    emit(f"## 三、点估计最好的前 {args.boot_top} 个：按日 bootstrap")
    emit()
    emit(f"门槛：三窗口 `AUC_big` 点估计均 ≥ {AUC_FLOOR}，"
         f"bootstrap 5% 分位均 > {BOOT_FLOOR}。")
    emit()
    emit("| 因子 | " + " | ".join(f"{lb} big [5%,95%]" for lb in labels_all)
         + " | 判定 |")
    emit("|---|" + "---:|" * (len(labels_all) + 1))
    survivors: list[str] = []
    for name in ranked[:args.boot_top]:
        cells = []
        passed = True
        for label in labels_all:
            got = stats[name][label].get("big")
            if got is None:
                passed = False
                cells.append("—")
                continue
            low, high = bootstrap_auc(got["x"], got["y"], got["day_index"],
                                      draws=BOOT, seed=args.seed)
            cells.append(f"{got['auc']:.4f} [{low:.4f},{high:.4f}]")
            if not np.isfinite(low) or low <= BOOT_FLOOR:
                passed = False
        if passed:
            survivors.append(name)
        emit(f"| `{name}` | " + " | ".join(cells)
             + f" | {'✅ 入选' if passed else '✗'} |")

    emit()
    if survivors:
        emit(f"**入选 {len(survivors)} 个**：" + "、".join(f"`{n}`" for n in survivors))
    else:
        emit("**头部内也没有因子能同时通过三窗口 + bootstrap。** "
             "也就是说：告警精度不是被某个没被用上的因子卡住的，"
             "而是这套数据在 20 日尺度上对板块横截面本来就只有很弱的信号。")

    if args.json:
        target = ROOT / args.json
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "head_quantile": args.head_quantile, "survivors": survivors,
            "windows": labels_all, "auc_floor": AUC_FLOOR,
            "boot_floor": BOOT_FLOOR, "horizon": HORIZON, "big": BIG,
            "top": [{"factor": n, "worst_big": worst("big", n),
                     "worst_rel": worst("rel", n)} for n in ranked[:30]],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"头部扫描 → {target}")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
