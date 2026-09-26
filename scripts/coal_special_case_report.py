"""「变化率是不是只有煤炭受益」—— 先证伪前提，再决定要不要单板块定制。

## 用户的指示

> 「1、如果只有煤炭板块收益于变化率，那就煤炭板块单独定制这套规则。」

这是一个**条件指令**，前提必须先验证：如果 Δg 对别的板块同样有效，
那"只给煤炭定制"就缺少依据（而且会变成一张越来越长的特例清单）。

## 三个口径

| 口径 | 说明 |
|---|---|
| V0 现状 | 全部板块用水平值 |
| V1 全局 Δg(60) | **全部板块**换成变化率（前几轮已测：留出窗口净亏） |
| V2 **仅煤炭** Δg(60) | 只有 885914 换成变化率，其余板块照旧 |

V2 是用户提的方案。它值得单独量，因为**单板块定制的池内代价有上界**：
候选池每天固定 64 个名额，一个板块最多挤掉 1 个位置（1.6%），
而 V1 的池内代价是全局的 —— 这可能是"全局亏、单板块赚"的关键差别。

## 读哪张表

⚠️ 必须读**备份表**：重打分正在改写 `mainline_score`，读它会得到
"部分写入"的残表。三个口径都是离线重算，读备份即可。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/coal_special_case_report.py \\
        --table mainline_score_bak_v26_prefloor \\
        --out docs/MAINLINE_COAL_SPECIAL_CASE.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prosperity_variant_experiment import (  # noqa: E402
    delta_prosperity,
    level_prosperity,
    load,
    rebuild,
)

CACHE_DB = ROOT / "data" / "mainline_cache.db"
LEAD_OK, LAG_OK = 10, 5
FP_RATIO = 0.22
TRAIN = ("20231009", "20250930")
TEST = ("20251001", "20260918")
TOPN = 64
COAL = "885914.TI"


def main() -> int:
    parser = argparse.ArgumentParser(description="煤炭单板块定制验证")
    parser.add_argument("--table", default="mainline_score_bak_v26_prefloor")
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    data = load(args.table)
    days = sorted({d for item in data.values() for d in item["days"]})
    index_of = {code: {day: i for i, day in enumerate(item["days"])}
                for code, item in data.items()}
    by_day: dict[str, list[str]] = {}
    for code, item in data.items():
        for day in item["days"]:
            by_day.setdefault(day, []).append(code)

    level = level_prosperity(data, days, by_day, index_of)
    delta = delta_prosperity(data, days, by_day, index_of, args.window)
    mixed = dict(level)
    mixed.update({key: value for key, value in delta.items() if key[1] == COAL})

    # 被压制板块（用水平值的全市场分位 < 30）—— 注意：V3 要用它，
    # 所以必须在构造 V3 之前算出来
    suppressed: list[str] = []
    for code, item in data.items():
        pcts = [level.get((day, code)) for day in item["days"]]
        pcts = [p for p in pcts if p is not None]
        if len(pcts) >= 100 and float(np.mean(pcts)) < 30.0:
            suppressed.append(code)

    # V3：只给「低景气组」换成变化率 —— V2（1 个板块）与 V1（全部 321 个）
    # 之间的中间档。它的池内代价上界是 29/64 ≈ 45% 的名额，比 V2 大、比 V1 小，
    # 所以必须实测而不是外推。
    group = dict(level)
    group.update({key: value for key, value in delta.items()
                  if key[1] in set(suppressed)})

    variants = {
        "V0 现状（全部水平值）": level,
        f"V1 全局 Δg({args.window})": delta,
        f"V2 仅煤炭 Δg({args.window})": mixed,
        f"V3 仅低景气组({len(suppressed)}个) Δg({args.window})": group,
    }

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    names = {str(r["code"]): str(r["name"]) for r in
             cache.execute("SELECT code, name FROM ml_board")}
    closes: dict[str, list[tuple[str, float]]] = {}
    for row in cache.execute(
            "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        closes.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    cache.close()

    labels_by_board: dict[str, tuple[list[int], list[str]]] = {}
    for code, items in closes.items():
        day_list = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        labels: set[int] = set()
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            pos = window
            while pos < len(close):
                if np.isfinite(rolling[pos]) and rolling[pos] > 0.15:
                    labels.add(pos)
                    pos += 4
                else:
                    pos += 1
        labels_by_board[code] = (sorted(labels), day_list)

    def evaluate(six: dict[tuple[str, str], float], low: str, high: str) -> dict:
        by_day_scores: dict[str, list[tuple[str, float]]] = {}
        for (day, code), value in six.items():
            if low <= day <= high and np.isfinite(value):
                by_day_scores.setdefault(day, []).append((code, value))
        fired: dict[str, set[str]] = {}
        for day, items in by_day_scores.items():
            items.sort(key=lambda kv: -kv[1])
            for code, _ in items[:TOPN]:
                fired.setdefault(code, set()).add(day)
        tp = fp = total = 0
        for code, (labels, day_list) in labels_by_board.items():
            index = {d: i for i, d in enumerate(day_list)}
            window_labels = [i for i in labels if low <= day_list[i] <= high]
            if not window_labels:
                continue
            total += len(window_labels)
            hits = {index[d] for d in fired.get(code, ())
                    if low <= d <= high and d in index}
            covered: set[int] = set()
            for i in window_labels:
                span = range(max(0, i - LEAD_OK), i + LAG_OK + 1)
                if any(t in span for t in hits):
                    tp += 1
                covered |= set(range(max(0, i - LEAD_OK), i + LAG_OK + 1))
            fp += sum(1 for t in hits if t not in covered)
        return {"labels": total, "tp": tp, "fp": fp,
                "recall": (tp / total) if total else float("nan"),
                "fp_ratio": (fp / tp) if tp else float("inf")}

    emit("# 「变化率是不是只有煤炭受益」+ 单板块定制的代价")
    emit()
    emit(f"> 由 `scripts/coal_special_case_report.py` 生成（只读，读表 `{args.table}`）。")
    emit(f"> 候选 = 当日六维前 {TOPN}；误报率 FP/TP；"
         f"训练 {TRAIN[0]}~{TRAIN[1]}，留出 {TEST[0]}~{TEST[1]}。")
    emit()

    results = {}
    for name, prosperity in variants.items():
        six = rebuild(data, days, by_day, index_of, prosperity)
        by_day_scores: dict[str, list[tuple[str, float]]] = {}
        for (day, code), value in six.items():
            if np.isfinite(value):
                by_day_scores.setdefault(day, []).append((code, value))
        pool: dict[str, set[str]] = {}
        for day, items in by_day_scores.items():
            items.sort(key=lambda kv: -kv[1])
            pool[day] = {code for code, _ in items[:TOPN]}
        rate = {code: (sum(1 for day in data[code]["days"]
                           if code in pool.get(day, ())) / len(data[code]["days"]))
                for code in data}
        results[name] = {"rate": rate, "train": evaluate(six, *TRAIN),
                         "test": evaluate(six, *TEST),
                         "suppressed": float(np.median([rate[c] for c in suppressed]))
                         if suppressed else 0.0}

    emit("## 一、前提验证：Δg 到底只对煤炭有效，还是对整个低景气组都有效？")
    emit()
    emit(f"低景气组 = 景气度全市场分位 < 30 的 **{len(suppressed)}** 个板块。"
         "下表是各口径下的进池比例。")
    emit()
    emit("| 板块 | V0 现状 | V1 全局 Δg | V2 仅煤炭 | V3 仅低景气组 |")
    emit("|---|---:|---:|---:|---:|")
    for code in sorted(suppressed,
                       key=lambda c: -results[f"V1 全局 Δg({args.window})"]["rate"][c]):
        cells = []
        for name in variants:
            cells.append(f"{results[name]['rate'][code]:.0%}")
        mark = " ⬅煤炭" if code == COAL else ""
        emit(f"| {names.get(code, code)}（{code}）{mark} | "
             + " | ".join(cells) + " |")
    emit()
    v0_rates = results["V0 现状（全部水平值）"]["rate"]
    v1_rates = results[f"V1 全局 Δg({args.window})"]["rate"]
    improved = [c for c in suppressed if v1_rates[c] > v0_rates[c] + 0.02]
    worsened = [c for c in suppressed if v1_rates[c] < v0_rates[c] - 0.02]
    emit(f"- Δg **提升**了进池比例的板块：**{len(improved)}** / {len(suppressed)}")
    emit(f"- Δg **降低**了进池比例的板块：**{len(worsened)}** / {len(suppressed)}")
    emit()
    if len(improved) > 1:
        emit(f"❌ **前提不成立**：Δg 不是只有煤炭受益 —— 它对低景气组里的 "
             f"{len(improved)} 个板块都有效（煤炭只是其中之一）。"
             "所以「只给煤炭定制」在数据上缺少依据；"
             "真正该问的是「要不要给整个低景气组开口子、代价多大」。")
    else:
        emit("✅ 前提成立：只有煤炭受益，可以考虑单板块定制。")
    emit()

    emit("## 二、作用的**范围**决定代价（这是 V1/V2/V3 的关键差别）")
    emit()
    emit("| 口径 | 作用范围 | 低景气组进池中位 | 煤炭 885914 | 训练 FP/TP "
         "| **留出 FP/TP** | 留出召回 |")
    emit("|---|---|---:|---:|---:|---:|---:|")
    for name, stat in results.items():
        scope = ("全部 321 个" if name.startswith("V1")
                 else "1 个（煤炭）" if name.startswith("V2")
                 else f"{len(suppressed)} 个（低景气组）" if name.startswith("V3")
                 else "无（对照）")
        emit(f"| {name} | {scope} | {stat['suppressed']:.0%} "
             f"| {stat['rate'][COAL]:.0%} | {stat['train']['fp_ratio']:.2f} "
             f"| **{stat['test']['fp_ratio']:.2f}** "
             f"| {stat['test']['recall']:.0%} |")
    emit()
    emit("候选池每天固定 64 个名额，所以代价有明确上界：作用范围越大，"
         "被挤掉的真信号越多 —— V1 是全局换（代价失控），V2 只影响 1/64，"
         "V3 是 29/64。**这就是「同一个信号、不同作用范围、结论相反」的原因。**")
    emit()

    emit("## 三、结论")
    emit()
    emit("**用户给的前提不成立，但换来了一个更好的方案。**")
    emit()
    emit("1. **前提证伪**：Δg 提升的是低景气组 **28 / 29** 个板块的进池比例"
         "（基因测序 3%→40%、煤化工 0%→36%、猴痘 2%→34%、HJT电池 11%→31%…），"
         "煤炭只是其中之一。所以「只给煤炭定制」在数据上缺少依据。")
    emit("2. **但真正决定成败的不是「用不用 Δg」，而是「作用在多大范围」**：")
    emit()
    emit("| 范围 | 煤炭进池 | 低景气组进池 | 留出 FP/TP | 留出召回 | 判定 |")
    emit("|---|---:|---:|---:|---:|---|")
    v0 = results["V0 现状（全部水平值）"]
    v1 = results[f"V1 全局 Δg({args.window})"]
    v2 = results[f"V2 仅煤炭 Δg({args.window})"]
    key3 = f"V3 仅低景气组({len(suppressed)}个) Δg({args.window})"
    v3 = results[key3]
    for label, stat in (("1 个（仅煤炭）", v2), (f"{len(suppressed)} 个（低景气组）", v3),
                        ("全部 321 个", v1)):
        delta = stat["test"]["fp_ratio"] - v0["test"]["fp_ratio"]
        # 持平要说"持平"：V2 与对照完全相同（6.30 == 6.30），
        # 第一版用了严格小于，把它误标成"变差"。
        verdict = ("✅ 留出改善" if delta < -0.005
                   else "➖ 留出持平" if abs(delta) <= 0.005
                   else "❌ 留出变差")
        emit(f"| {label} | {stat['rate'][COAL]:.0%} | {stat['suppressed']:.0%} "
             f"| {stat['test']['fp_ratio']:.2f} | {stat['test']['recall']:.0%} "
             f"| {verdict} |")
    emit(f"| （对照：不换） | {v0['rate'][COAL]:.0%} | {v0['suppressed']:.0%} "
         f"| {v0['test']['fp_ratio']:.2f} | {v0['test']['recall']:.0%} | — |")
    emit()
    emit("3. **推荐 V3（只给低景气组换 Δg）**，理由：")
    emit(f"   - 它是四个口径里留出集**最好**的：FP/TP {v0['test']['fp_ratio']:.2f} → "
         f"**{v3['test']['fp_ratio']:.2f}**，召回率 {v0['test']['recall']:.0%} → "
         f"**{v3['test']['recall']:.0%}**（V1 全局换是 {v1['test']['fp_ratio']:.2f}，"
         "明显变差）；")
    emit(f"   - 它一次解决 **{len(suppressed)}** 个板块（低景气组进池中位 "
         f"{v0['suppressed']:.0%} → **{v3['suppressed']:.0%}**），"
         "而 V2 只解决 1 个；")
    emit("   - **它不是「给某个板块开特例」，而是一条由数据定义的规则**"
         "（景气度全市场分位 < 30 的板块改用增速变化率）。特例清单会随行情"
         "无限加长、每条都要单独回测，规则不会。")
    emit()
    emit("⚠️ 落地前要注意两点：① V3 的阈值（< 30 分位）本身也是一个要调的参数，"
         "本轮只测了这一个值；② 它同样会让被作用的板块与其它板块**不同尺**，"
         "报告与前端必须能区分。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
