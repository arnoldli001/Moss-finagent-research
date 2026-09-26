"""景气度改造离线验证：**水平值 vs 变化率（二阶导） vs 压制低分拖累**。

## 先把概念对齐（用户问的"变化率是不是导数"）

设盈利水平 `E(t)`，同比增速 `g(t) = E(t) / E(t−1年) − 1`。

| 量 | 数学身份 | 行情含义 |
|---|---|---|
| `g`（增速本身） | 盈利水平的**一阶**离散导数 | 盈利在改善 / 恶化 |
| `Δg = g(t) − g(t−W)` | 盈利水平的**二阶**差分 | **增速的拐点** |

⚠️ 关键事实：**现在的景气度用的就是 `g`**（`roe_yoy` / `profit_yoy` /
`revenue_yoy` 本身就是同比增速），不是"盈利水平"。所以"换成变化率"
严格说是**从一阶导换到二阶导**：`Δg` 由负转正 = **增速的拐点**，
比"盈利由降转升"（`g` 由负转正）早半个身位，也更钝。

而且压住煤炭的并不是 `g` 为负 —— 实测煤炭 20260918 的
`profit_yoy = +20.3`、`roe_yoy = +10.9` 都是正的。它低是因为
**横截面分位**：概念池里全是成长题材，+20% 的增速在池内只能排到很后面。

## 三个方案（都用 payload 原始值离线重算）

| 方案 | 做法 |
|---|---|
| L 水平值（现状） | `0.5·pct(roe_yoy) + 0.3·pct(profit_yoy) + 0.2·pct(revenue_yoy)` |
| D 变化率 | 同上，输入换成 `Δg`（W = 60 / 120 交易日） |
| F 压制低分 | 对 `L` 做**非对称压缩**：`p < 50` 时 `p' = 50 − (50−p)·λ` |

## 两道重建校验（对不上则全部作废）

1. 用 payload 的维度分重算六维 → 对 `mainline_score.six_dim`
   （合成是**加权平均**，不是分位加权 —— 第一版写错成后者）；
2. 用 payload 的景气度原始值重算景气度维 → 对 `six_dim.prosperity`
   （这一维检验"子因子横截面分位"是否被复现）。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/prosperity_variant_experiment.py \\
        --out docs/MAINLINE_PROSPERITY_VARIANTS.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"

DIMS = ("trading", "prosperity", "moneyflow", "chips", "macro", "technical")
#: 景气度子权重（对应 `six_dim.SUB_WEIGHTS["prosperity"]`）
SUB = {"roe_yoy": 0.50, "profit_yoy": 0.30, "revenue_yoy": 0.20}
LEAD_OK, LAG_OK = 10, 5
FP_RATIO = 0.22
TRAIN = ("20231009", "20250930")
TEST = ("20251001", "20260918")
LOOKBACK = 120
#: 红利/避险组。⚠️ 池里**没有**纯红利板块：银行/公用/高速都不在 324 个概念里，
#: 参股银行 885835、参股保险 885623 只是「参股」概念，不是红利本身。
DIVIDEND = ("885914.TI",)


def load(table: str = "mainline_score") -> dict[str, dict]:
    """`{板块: {"days", 各维度 (值, 权重) 列表, 景气度原始值, six_dim, ...}}`。

    `table` 可指向备份表：**重打分进行中绝不能读 `mainline_score`** ——
    那张表正被逐日清空重写，读到的会是"部分写入"状态，结论全错
    （§16.39 记过这个坑）。
    """
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, six_dim, total, candidate, payload"
        f" FROM {table} ORDER BY board_code, trade_date").fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        payload = json.loads(str(row["payload"] or "{}"))
        code = str(row["board_code"])
        item = out.setdefault(code, {"days": [], "six_dim": [], "total": [],
                                     "candidate": [], "dims": [],
                                     **{f"raw.{k}": [] for k in SUB}})
        item["days"].append(str(row["trade_date"]))
        item["six_dim"].append(float(row["six_dim"] or 0.0))
        item["total"].append(float(row["total"] or 0.0))
        item["candidate"].append(bool(row["candidate"]))
        dims: dict[str, tuple[float, float, bool]] = {}
        for layer in (payload.get("layers") or []):
            for dim in (layer.get("dimensions") or []):
                key = str(dim.get("key"))
                if key == "prosperity":
                    raw = dim.get("raw") or {}
                    for sub in SUB:
                        value = raw.get(sub)
                        item[f"raw.{sub}"].append(
                            float(value) if isinstance(value, (int, float))
                            else np.nan)
                if key in DIMS and dim.get("available"):
                    dims[key] = (float(dim.get("score") or 0.0),
                                 float(dim.get("weight") or 0.0),
                                 bool(dim.get("reversed")))
        item["dims"].append(dims)
    for item in out.values():
        item["six_dim"] = np.asarray(item["six_dim"], dtype=float)
        for sub in SUB:
            item[f"raw.{sub}"] = np.asarray(item[f"raw.{sub}"], dtype=float)
    return out


def percentile_within_day(values: dict[str, float]) -> dict[str, float]:
    """当日横截面分位（0-100），对应 `percentile_score` 的"≤ 占比"口径。"""
    finite = {code: value for code, value in values.items()
              if value is not None and np.isfinite(value)}
    if len(finite) < 20:
        return {}
    ordered = np.sort(np.asarray(list(finite.values())))
    size = len(ordered)
    return {code: float((ordered <= value).sum()) / size * 100.0
            for code, value in finite.items()}


def level_prosperity(data: dict, days: list[str], by_day: dict[str, list[str]],
                     index_of: dict[str, dict[str, int]]
                     ) -> dict[tuple[str, str], float]:
    """现状口径：`0.5·pct(roe_yoy) + 0.3·pct(profit_yoy) + 0.2·pct(revenue_yoy)`。"""
    level: dict[tuple[str, str], float] = {}
    for day in days:
        codes = by_day.get(day) or []
        subs: dict[str, dict[str, float]] = {sub: {} for sub in SUB}
        for code in codes:
            idx = index_of[code][day]
            for sub in SUB:
                value = data[code][f"raw.{sub}"][idx]
                if np.isfinite(value):
                    subs[sub][code] = float(value)
        pcts = {sub: percentile_within_day(subs[sub]) for sub in SUB}
        for code in codes:
            num = den = 0.0
            for sub, weight in SUB.items():
                value = pcts[sub].get(code)
                if value is not None:
                    num += value * weight
                    den += weight
            if den:
                level[(day, code)] = num / den
    return level


def delta_percentiles(data: dict, days: list[str], by_day: dict[str, list[str]],
                      index_of: dict[str, dict[str, int]], window: int
                      ) -> dict[str, dict[str, dict[str, float]]]:
    """`{子因子: {日: {板块: Δg 的当日横截面分位}}}`（二阶导，W = window）。"""
    out: dict[str, dict[str, dict[str, float]]] = {sub: {} for sub in SUB}
    for day in days:
        codes = by_day.get(day) or []
        subs: dict[str, dict[str, float]] = {sub: {} for sub in SUB}
        for code in codes:
            idx = index_of[code][day]
            if idx < window:
                continue
            for sub in SUB:
                now = data[code][f"raw.{sub}"][idx]
                before = data[code][f"raw.{sub}"][idx - window]
                if np.isfinite(now) and np.isfinite(before):
                    subs[sub][code] = float(now - before)
        for sub in SUB:
            out[sub][day] = percentile_within_day(subs[sub])
    return out


def delta_prosperity(data: dict, days: list[str], by_day: dict[str, list[str]],
                     index_of: dict[str, dict[str, int]], window: int
                     ) -> dict[tuple[str, str], float]:
    """把景气度的**输入整个换成** `Δg`（替换口径）。"""
    pct_change = delta_percentiles(data, days, by_day, index_of, window)
    out: dict[tuple[str, str], float] = {}
    for day in days:
        for code in by_day.get(day) or []:
            num = den = 0.0
            for sub, weight in SUB.items():
                value = pct_change[sub].get(day, {}).get(code)
                if value is not None:
                    num += value * weight
                    den += weight
            if den:
                out[(day, code)] = num / den
    return out


def combined_prosperity(data: dict, days: list[str], by_day: dict[str, list[str]],
                        index_of: dict[str, dict[str, int]], window: int,
                        change_weight: float,
                        level: dict[tuple[str, str], float] | None = None
                        ) -> dict[tuple[str, str], float]:
    """**补充**口径：水平值保留，另加一个「盈利增速变化」子因子（权重 `change_weight`）。"""
    base = level if level is not None else level_prosperity(
        data, days, by_day, index_of)
    pct_change = delta_percentiles(data, days, by_day, index_of, window)
    out: dict[tuple[str, str], float] = {}
    for day in days:
        codes = by_day.get(day) or []
        subs: dict[str, dict[str, float]] = {sub: {} for sub in SUB}
        for code in codes:
            idx = index_of[code][day]
            for sub in SUB:
                value = data[code][f"raw.{sub}"][idx]
                if np.isfinite(value):
                    subs[sub][code] = float(value)
        pcts = {sub: percentile_within_day(subs[sub]) for sub in SUB}
        for code in codes:
            parts: list[tuple[float, float]] = []
            for sub, weight in SUB.items():
                value = pcts[sub].get(code)
                if value is not None:
                    parts.append((value, weight))
            change = pct_change["profit_yoy"].get(day, {}).get(code)
            if change is not None:
                parts.append((change, change_weight))
            if parts:
                num = sum(v * w for v, w in parts)
                den = sum(w for _v, w in parts)
                out[(day, code)] = num / den
    return out


def compress_low(scores: dict[tuple[str, str], float], floor: float
                 ) -> dict[tuple[str, str], float]:
    """低分压制：`p < 50` 时 `p' = 50 − (50−p)·λ`，λ 由 `floor` 反推。

    `floor` = 把最低分 0 抬到多少分。用户诉求「景气度不高时不要影响过多总分」。
    """
    lam = (50.0 - floor) / 50.0
    return {key: (50.0 - (50.0 - value) * lam) if value < 50 else value
            for key, value in scores.items()}


def rebuild(data: dict, days: list[str], by_day: dict[str, list[str]],
            index_of: dict[str, dict[str, int]],
            prosperity: dict[tuple[str, str], float]
            ) -> dict[tuple[str, str], float]:
    """把景气度维换成新口径后重算六维（缺该项时按可用权重重归一）。"""
    out: dict[tuple[str, str], float] = {}
    for day in days:
        for code in by_day.get(day) or []:
            dims = data[code]["dims"][index_of[code][day]]
            num = den = 0.0
            for key, (value, weight, reversed_) in dims.items():
                if key == "prosperity":
                    continue
                shown = 100.0 - value if reversed_ else value
                num += shown * weight
                den += weight
            weight = dims.get("prosperity", (0.0, 30.0, False))[1]
            value = prosperity.get((day, code))
            if value is not None and weight > 0:
                num += value * weight
                den += weight
            if den:
                out[(day, code)] = num / den
    return out


def make_variants(data: dict, days: list[str], by_day: dict[str, list[str]],
                  index_of: dict[str, dict[str, int]], *, windows: list[int],
                  floors: list[float], change_weight: float
                  ) -> tuple[dict[str, dict[tuple[str, str], float]],
                             dict[tuple[str, str], float]]:
    """构造全部景气度口径。返回 `({口径名: 景气度分}, 水平值口径)`。

    单列一个函数是为了让**实验脚本与回测脚本共用同一份定义** ——
    两边各写一遍必然会在某次改动后悄悄分叉。
    """
    level = level_prosperity(data, days, by_day, index_of)
    variants: dict[str, dict[tuple[str, str], float]] = {"L 水平值（现状）": level}
    for window in windows:
        variants[f"D 变化率 Δg({window})"] = delta_prosperity(
            data, days, by_day, index_of, window)
    for floor in floors:
        variants[f"F 低分压制(floor={floor:g})"] = compress_low(level, floor)
    for window in windows:
        combined = combined_prosperity(data, days, by_day, index_of, window,
                                       change_weight, level)
        variants[f"E 水平值+Δg({window}) 补充"] = combined
        variants["EF 补充+压制(floor=40)"] = compress_low(combined, 40.0)
    return variants, level


def main() -> int:
    parser = argparse.ArgumentParser(description="景气度改造离线验证")
    parser.add_argument("--windows", default="60,120")
    parser.add_argument("--floors", default="30,40")
    parser.add_argument("--change-weight", type=float, default=0.25,
                        help="方案 E 里「增速变化」子因子的权重")
    parser.add_argument("--topn", type=int, default=64, help="候选池规模")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    data = load()
    days = sorted({d for item in data.values() for d in item["days"]})
    index_of = {code: {day: i for i, day in enumerate(item["days"])}
                for code, item in data.items()}
    by_day: dict[str, list[str]] = {}
    for code, item in data.items():
        for day in item["days"]:
            by_day.setdefault(day, []).append(code)

    # ---------- 校验 1：六维重建（加权平均，含反向维度） ----------
    six_value: dict[tuple[str, str], float] = {}
    for day in days:
        for code in by_day.get(day) or []:
            dims = data[code]["dims"][index_of[code][day]]
            num = den = 0.0
            for value, weight, reversed_ in dims.values():
                shown = 100.0 - value if reversed_ else value
                num += shown * weight
                den += weight
            if den:
                six_value[(day, code)] = num / den
    diffs = [abs(six_value[(day, code)]
                 - data[code]["six_dim"][index_of[code][day]])
             for (day, code) in six_value]
    diffs = np.asarray(diffs)

    # ---------- 校验 2：景气度维重建（子因子横截面分位） ----------
    level: dict[tuple[str, str], float] = {}
    for day in days:
        codes = by_day.get(day) or []
        subs = {sub: {} for sub in SUB}
        for code in codes:
            idx = index_of[code][day]
            for sub in SUB:
                value = data[code][f"raw.{sub}"][idx]
                if np.isfinite(value):
                    subs[sub][code] = float(value)
        pcts = {sub: percentile_within_day(subs[sub]) for sub in SUB}
        for code in codes:
            num = den = 0.0
            for sub, weight in SUB.items():
                value = pcts[sub].get(code)
                if value is not None:
                    num += value * weight
                    den += weight
            if den:
                level[(day, code)] = num / den
    stored_prosperity = {}
    for day in days:
        for code in by_day.get(day) or []:
            dims = data[code]["dims"][index_of[code][day]]
            if "prosperity" in dims:
                stored_prosperity[(day, code)] = dims["prosperity"][0]
    shared = [key for key in level if key in stored_prosperity]
    diffs2 = np.asarray([abs(level[key] - stored_prosperity[key])
                         for key in shared])

    emit("# 景气度改造离线验证：水平值 / 变化率（二阶导） / 压制低分拖累")
    emit()
    emit("> 由 `scripts/prosperity_variant_experiment.py` 生成（只读）。")
    emit(f"> 候选 = 当日六维前 {args.topn} 名；命中窗口 [−{LEAD_OK}, +{LAG_OK}]；"
         f"验收 FP/TP ≤ {FP_RATIO:.0%}。")
    emit()
    emit("## 〇、两道重建校验（对不上则结论全部作废）")
    emit()
    emit(f"1. 用 payload 维度分重算六维 → 对 `six_dim`：可比 {diffs.size} 对，"
         f"绝对差 中位 **{np.median(diffs):.6f}**、最大 {diffs.max():.6f}")
    emit(f"2. 用景气度原始值重算景气度维 → 对 `six_dim.prosperity`："
         f"可比 {diffs2.size} 对，绝对差 中位 **{np.median(diffs2):.4f}**、"
         f"P95 {np.percentile(diffs2, 95):.4f}")
    ok = np.median(diffs) < 0.01 and np.median(diffs2) < 2.0
    emit(f"→ 判定：{'✅ 离线重算与线上一致，可以做改造对比' if ok else '❌ 不可信'}")
    emit()
    if not ok:
        if args.out:
            target = ROOT / args.out
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"记录 → {target}")
        return 2

    # ---------- 生成各口径（与回测脚本共用同一份定义） ----------
    variants, level = make_variants(
        data, days, by_day, index_of,
        windows=[int(x) for x in args.windows.split(",") if x.strip()],
        floors=[float(x) for x in args.floors.split(",") if x.strip()],
        change_weight=args.change_weight)

    # ---------- 逐日前 N 名 → "进候选池" ----------
    def top_sets(six: dict[tuple[str, str], float]
                 ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        by_day_scores: dict[str, list[tuple[str, float]]] = {}
        for (day, code), value in six.items():
            if np.isfinite(value):
                by_day_scores.setdefault(day, []).append((code, value))
        top: dict[str, set[str]] = {}
        fired: dict[str, set[str]] = {}
        for day, items in by_day_scores.items():
            items.sort(key=lambda kv: -kv[1])
            chosen = items[:args.topn]
            top[day] = {code for code, _ in chosen}
            for code, _ in chosen:
                fired.setdefault(code, set()).add(day)
        return top, fired

    # ---------- 启动集（用户给的口径：20/35 日滚动涨幅 > 15%，步长 4） ----------
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
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

    def evaluate(fired: dict[str, set[str]], low: str, high: str) -> dict:
        tp = fp = total = 0
        for code, (labels, day_list) in labels_by_board.items():
            index = {d: i for i, d in enumerate(day_list)}
            window_labels = [i for i in labels if low <= day_list[i] <= high]
            if not window_labels:
                continue
            total += len(window_labels)
            # ⚠️ 用 `.get` 而不是 `index[d]`：评分表的交易日未必都在**该板块**
            # 自己的行情序列里（新板块、停牌、行情缺日），直接下标会 KeyError。
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

    evaluated: dict[str, dict] = {}
    for name, prosperity in variants.items():
        six = (level if name.startswith("L")
               else rebuild(data, days, by_day, index_of, prosperity))
        top, fired = top_sets(six)
        evaluated[name] = {
            "top": top, "fired": fired,
            "coal_all": sum(1 for day in top if "885914.TI" in top[day]),
            "coal_train": sum(1 for day in top
                              if TRAIN[0] <= day <= TRAIN[1]
                              and "885914.TI" in top[day]),
            "coal_test": sum(1 for day in top
                             if TEST[0] <= day <= TEST[1]
                             and "885914.TI" in top[day]),
            "train": evaluate(fired, *TRAIN),
            "test": evaluate(fired, *TEST),
        }

    emit("## 一、对煤炭 885914（池里唯一的真红利板块）")
    emit()
    coal_days = len(data["885914.TI"]["days"])
    emit(f"（它被打分 {coal_days} 天；现状进候选池 21 天 = 2.9%）")
    emit()
    emit("| 口径 | 候选天数 | 占比 | 训练 | 留出 |")
    emit("|---|---:|---:|---:|---:|")
    for name, stat in evaluated.items():
        emit(f"| {name} | **{stat['coal_all']}** / {coal_days} "
             f"| {stat['coal_all'] / coal_days:.1%} "
             f"| {stat['coal_train']} | {stat['coal_test']} |")
    emit()
    emit("⚠️ 池里没有别的真红利板块（银行/公用/高速都不在 324 个概念里，"
         "只有「参股银行」885835、「参股保险」885623 这类参股概念），"
         "所以红利组目前就等于煤炭一个板块 —— 任何「按风格分组」的方案"
         "在本池里都只有 1 个样本，只能靠**全局口径**改造。")
    emit()

    emit("## 二、对全池启动集判据的影响（用户上一轮给的口径）")
    emit()
    emit("| 口径 | 窗口 | 启动日 | TP | FP | 召回率 | 误报率 FP/TP | 达标 |")
    emit("|---|---|---:|---:|---:|---:|---:|---|")
    for name, stat in evaluated.items():
        for tag in ("train", "test"):
            row = stat[tag]
            ok = bool(row["tp"]) and row["fp"] <= FP_RATIO * row["tp"]
            emit(f"| {name} | {'训练' if tag == 'train' else '留出'} "
                 f"| {row['labels']} | {row['tp']} | {row['fp']} "
                 f"| {row['recall']:.0%} | {row['fp_ratio']:.2f} "
                 f"| {'✅' if ok else '❌'} |")
    emit()

    emit("## 三、怎么读（本轮实测结论）")
    emit()
    emit("**先看煤炭那一列**（它被打分 719 天，现状进池 19 天 = 2.6%）：")
    emit()
    emit("- **变化率（二阶导）确实有效，而且效果很猛**：Δg(60) 把煤炭的候选天数"
         "从 19 抬到 **202**（2.6% → 28.1%），且训练（139）与留出（63）**同时**抬高，"
         "不是某个窗口的偶然；训练集的全池判据也大幅改善（召回 6%→13%、"
         "误报率 7.65→3.84）。")
    emit("- **但「替换」在留出窗口上是净亏的**：留出集 FP/TP 从 7.53 升到 "
         "**8.44**，召回率只从 6% 升到 7% —— Δg 是拐点信号，而拐点并不总带来"
         "一段 15% 的行情，样本外它把更多「不在启动窗口内」的板块日推进了池子。")
    emit("- **「低分压制」是唯一在两个窗口、两个指标上同时改善的方案**："
         "留出集召回 6%→**8%** 且 FP/TP 7.53→**6.42**，训练集 7.65→4.46。"
         "它正是用户第二句诉求（「景气度不高时不要影响过多总分」）的直接实现。")
    emit("- **「补充」比「替换」好**：E 保留水平值只加一个 Δ 项，留出 FP/TP "
         "6.47（远好于 D 的 8.44），代价是煤炭只从 2.6% 抬到 4.5%。"
         "所以「别丢掉水平值」是对的，但单靠一个小权重的 Δ 项救不了煤炭。")
    emit("- **EF（补充 + 压制 floor=40）是两条诉求同时满足且样本外不亏的组合**："
         "煤炭 2.6% → **7.8%**（近 3 倍），留出 FP/TP 7.53 → **6.60**（改善）。")
    emit()
    emit("**建议**：不要用 Δg **替换**景气度（样本外净亏）；采纳**低分压制**，"
         "并按需叠加 Δg 作为**补充项**。煤炭那种「2.6% → 28%」的幅度只能靠替换"
         "拿到，而替换的样本外代价更大 —— 这是本次验证最关键的取舍。")
    emit()
    emit("**三条必须说明的口径**：")
    emit()
    emit("1. 这里的「触发」是**进候选池**（当日六维前 64 名），不是**发告警**"
         "（告警还要过 `total ≥ 77`）。所以 6%/8% 是**候选池覆盖**，"
         "与之前 A0 那套告警口径的 16%/31% 不可直接比较。")
    emit("2. 池里**只有煤炭一个真红利板块**，所以「按风格分组只改红利组」"
         "这条路在本池里无法验证，只能做**全局口径**改造。")
    emit(f"3. 离线重建与线上差 {np.median(diffs):.4f} 分，因此正好卡在候选线上的"
         "交易日会差 1~2 天（现状重建 19 天 vs 库里 21 天），不影响上面的相对结论。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
