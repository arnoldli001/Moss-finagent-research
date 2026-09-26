"""按板块拟合打分参数：**用户给的启动集定义 + 22% 误报约束**。

## 标签（用户 2026-09-21 给的算法）

> 「通过对板块历史交易数据，**从远到近**用 20 个交易日、60 个交易日窗口
>   平滑扫描其涨幅，找到 20 个交易日涨幅大于 15% 的板块，其第一个交易日
>   就是期望的启动告警日期。后续然后**跳过 4 个交易日继续扫描**。」
>
> 「再加一个，**35 个交易日内涨幅大于 15% 的也算主线区间**，
>   起始第一个交易日算主线启动日。」

实现（`--windows 20,35 --gain 0.15 --step 4`）：

    g_w[t] = close[t] / close[t-w] - 1         # 滚动 w 日涨幅，只用 t 及之前
    t 从最老往前走：
        g_w[t] > 15%  →  记 t 为启动告警日，**t += 4**（跳过 4 个交易日）
        否则           →  t += 1

20 日与 35 日各扫一遍取**并集**：20 日那条抓急涨，35 日那条把「涨得慢一些
但 35 日内也涨过 15%」的行情也算成主线区间。所以一次连续上涨会每隔 4 个
交易日产生一个标签（用户要的就是这一点：把这些连续上涨真值集都找出来）。
另外单独记每个"启动段"的**首个交叉日** —— 那是用户说的"主线启动日"。

## 验收判据（用户给的）

> 「参数调整目标：**误报率是真值召回率的 22% 以下**，策略就是合格的。」

    TP      = 在某个启动日 ±[−10, +5] 个交易日内报出过的启动日数
    召回率  = TP / 启动日总数
    FP      = 不在任何启动窗口内的告警日数       ← "对其他窗口时间产生的误报"
    误报率  = FP / TP                            ← 与召回率同量纲的比率
    合格   ⟺ 误报率 ≤ 22%

拟合目标直接对齐这条判据（不是某个不相干的 IC）：

    score = TP - 10 × max(0, FP - 0.22 × TP)

罚的是**违反约束的部分**而不是 FP 本身 —— 否则"永远不报"（TP=0, FP=0）
会赢过"报得多也控得住"的解，那显然不是用户要的。

## 四个方案

| 方案 | 参数 | 说明 |
|---|---|---|
| A0 全局绝对线 | 0 | 现状线上口径：`total ≥ 77` |
| A 全局权重 + 全局分位 | 0 | 全局权重，统一 0.90 自参照分位 |
| B 全局权重 + **按板块分位** | 每板块 1 个 | 只让门槛按板块走 |
| C **按板块权重 + 按板块分位** | 每板块 2 个 | 用户提的方案 |

B/C 的参数**只在训练窗口上定**，留出窗口只做评估。
训练 `20231009~20250930`，留出 `20251001~20260918`。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/per_board_fit_experiment.py \\
        --out docs/MAINLINE_PER_BOARD_FIT.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"

DIMS = ("trading", "prosperity", "moneyflow", "chips", "macro", "technical")
COMPONENTS = (*DIMS, "accumulation")
#: 反向维度：层合成里用 `100 - score`（与 `six_dim.py` 同一口径）
REVERSED = ("trading", "technical")
#: 六维内部现行权重（`configs/mainline.yaml`）与层权重，用来构造全局合成
GLOBAL_DIM_WEIGHTS = {"trading": 20.0, "prosperity": 30.0, "moneyflow": 20.0,
                      "chips": 10.0, "macro": 5.0, "technical": 1.0}
LAYER_WEIGHTS = {"six_dim": 80.0, "accumulation": 20.0}
LOOKBACK = 120              # 自参照分位的回看交易日
LEAD_OK, LAG_OK = 10, 5     # 命中窗口：提前 ≤10 / 滞后 ≤5
FP_RATIO = 0.22             # 用户给的验收线
TRAIN = ("20231009", "20250930")
TEST = ("20251001", "20260918")
#: 绝对下限候选（方案 D/E）。为什么要有这一维：自参照分位只问"它是不是
#: **自己**的高位"，不问"它是不是**市场**的高位" —— 熊市里所有板块都在
#: 各自低位横盘，分位照样能顶到 1%，于是"整个市场都没启动"的日子里
#: 告警照发。线上现有的突破触发正是这么做的（自身分位 AND `total ≥ 50`）。
FLOORS = (0.0, 40.0, 50.0, 60.0, 70.0)


# ======================================================================
# 数据
# ======================================================================

def load_components(start: str, end: str) -> dict[str, dict]:
    """`{板块: {"days": [...], 7 个分量: np.array, "total": np.array}}`。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_date, board_code, total, payload FROM mainline_score"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY board_code, trade_date",
        (start, end)).fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        code = str(row["board_code"])
        item = out.setdefault(code, {"days": [], "total": [],
                                     **{name: [] for name in COMPONENTS}})
        item["days"].append(str(row["trade_date"]))
        item["total"].append(float(row["total"] or 0.0))
        scores: dict[str, float] = {}
        for layer in (payload.get("layers") or []):
            for dim in (layer.get("dimensions") or []):
                if dim.get("available"):
                    scores[str(dim.get("key"))] = float(dim.get("score") or 0.0)
        for name in DIMS:
            value = scores.get(name)
            item[name].append(np.nan if value is None
                              else (100.0 - value if name in REVERSED
                                    else value))
        acc = payload.get("accumulation_score")
        item["accumulation"].append(
            float(acc) if payload.get("accumulation_coverage") else np.nan)
    for item in out.values():
        for name in COMPONENTS:
            item[name] = np.asarray(item[name], dtype=float)
        item["total"] = np.asarray(item["total"], dtype=float)
    return out


def load_closes() -> dict[str, tuple[list[str], np.ndarray]]:
    """`{板块: (有行情的交易日, 收盘价)}` —— 日期与价格来自同一次查询。

    ⚠️ 不能让日期和价格分两次查再靠"顺序一致"对齐：那是隐式耦合，
    换一次 `ORDER BY` 就会静默错位（本项目栽过同类跟头）。
    """
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
        " JOIN ml_calendar k ON k.trade_date = b.trade_date"
        " ORDER BY b.board_code, b.trade_date").fetchall()
    conn.close()
    out: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        out.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    return {code: ([d for d, _ in items], np.asarray([c for _, c in items]))
            for code, items in out.items()}


# ======================================================================
# 标签：用户给的扫描算法
# ======================================================================

def scan_window(days: list[str], close: np.ndarray, *, window: int,
                gain: float, step: int) -> tuple[list[int], list[int]]:
    """单个窗口的扫描：`(全部启动日, 每个启动段的首个交叉日)`。

    只用当日及之前的数据（`close[t]/close[t-window]`），严格 PIT。
    """
    n = len(close)
    if n <= window + 1:
        return [], []
    rolling = np.full(n, np.nan)
    rolling[window:] = close[window:] / close[:-window] - 1.0
    labels: list[int] = []
    starts: list[int] = []
    previous = False
    index = window
    while index < n:
        value = rolling[index]
        above = bool(np.isfinite(value) and value > gain)
        if above:
            labels.append(index)
            if not previous:
                starts.append(index)
            index += max(1, step)          # 跳过 step 个交易日继续扫描
        else:
            index += 1
        previous = above
    return labels, starts


def scan_launches(days: list[str], close: np.ndarray, *, windows: list[int],
                  gain: float, step: int) -> tuple[list[int], list[int]]:
    """多个窗口各扫一遍取并集（20 日抓急涨，35 日把慢一些的主线也算进来）。"""
    labels: set[int] = set()
    starts: set[int] = set()
    for window in windows:
        one_labels, one_starts = scan_window(days, close, window=window,
                                             gain=gain, step=step)
        labels |= set(one_labels)
        starts |= set(one_starts)
    return sorted(labels), sorted(starts)


# ======================================================================
# 打分与触发
# ======================================================================

def composite(item: dict, weights: np.ndarray) -> np.ndarray:
    """7 个分量按权重合成；缺项按可用权重归一（同线上 `weighted_score`）。"""
    stack = np.vstack([item[name] for name in COMPONENTS])
    mask = np.isfinite(stack) & (weights[:, None] > 0)
    num = np.where(mask, stack, 0.0).T @ weights
    den = mask.T @ weights
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def rolling_percentile(values: np.ndarray, *, lookback: int) -> np.ndarray:
    """当日值在**自身过去 lookback 天**里的分位（严格 PIT）。

    ⚠️ 用"分位"而不是"对每个分位档各算一次门槛"：按板块拟合时这个函数要被
    调用上万次，一算四档等于白做四倍功；分位算一次，四档只是同一个数组比大小。
    也必须用滑动窗口矩阵而不是 Python 循环 —— 逐点循环会让整个实验从
    "一两分钟"变成"一两个小时"（上一版就是这么超时的）。
    """
    n = len(values)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    index = np.arange(n)[:, None] - np.arange(lookback, 0, -1)[None, :]
    valid = index >= 0
    windows = np.where(valid, values[np.where(valid, index, 0)], np.nan)
    finite = np.isfinite(windows)
    counts = finite.sum(axis=1)
    le = (finite & (windows <= values[:, None])).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(counts >= 20, le / np.maximum(counts, 1), np.nan)
    return out


def global_weights() -> np.ndarray:
    dim_total = sum(GLOBAL_DIM_WEIGHTS.values())
    layer_total = sum(LAYER_WEIGHTS.values())
    six = np.asarray([GLOBAL_DIM_WEIGHTS[name] / dim_total
                      * LAYER_WEIGHTS["six_dim"] / layer_total for name in DIMS])
    acc = np.asarray([LAYER_WEIGHTS["accumulation"] / layer_total])
    return np.concatenate([six, acc])


def score_board(triggers: np.ndarray, labels: list[int], days: list[str],
                low: str, high: str) -> dict:
    """用户口径的 TP / FP / 召回率 / 误报率。"""
    inside = np.asarray([low <= day <= high for day in days])
    fired = np.flatnonzero(triggers & inside)
    window_labels = [i for i in labels if inside[i]]
    covered: set[int] = set()
    tp = 0
    for index in window_labels:
        window = range(max(0, index - LEAD_OK), index + LAG_OK + 1)
        if any(t in window for t in fired):
            tp += 1
        covered |= set(range(max(0, index - LEAD_OK), index + LAG_OK + 1))
    fp = int(sum(1 for t in fired if t not in covered))
    total = len(window_labels)
    return {"labels": total, "tp": tp, "fp": fp, "alerts": len(fired),
            "recall": (tp / total) if total else float("nan"),
            "fp_ratio": (fp / tp) if tp else float("inf"),
            "ok": bool(tp) and fp <= FP_RATIO * tp}


def objective(stat: dict) -> float:
    """`TP - 10 × 超限部分`：罚的是违反 22% 约束的量，不是 FP 本身。"""
    return stat["tp"] - 10.0 * max(0.0, stat["fp"] - FP_RATIO * stat["tp"])


def triggers_with(pct: np.ndarray, quantile: float, floor: float = 0.0,
                  total: np.ndarray | None = None) -> np.ndarray:
    """触发条件：**自身历史分位 > 分位档**，可选再加**绝对下限**。"""
    mask = np.isfinite(pct) & (pct > quantile)
    if floor > 0.0 and total is not None:
        mask &= np.isfinite(total) & (total >= floor)
    return mask


# ======================================================================
# 主流程
# ======================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="按板块拟合实验（用户口径）")
    parser.add_argument("--gain", type=float, default=0.15)
    parser.add_argument("--windows", default="20,35",
                        help="启动集扫描窗口（逗号分隔，取并集）")
    parser.add_argument("--step", type=int, default=4)
    parser.add_argument("--draws", type=int, default=30,
                        help="按板块拟合权重时随机搜索的向量个数")
    parser.add_argument("--min-train-labels", type=int, default=2,
                        help="训练集里至少这么多启动日才允许按板块拟合权重")
    parser.add_argument("--quantiles", default="0.90,0.95,0.97,0.98,0.99,0.995",
                        help="自参照分位档（拟合只在训练集上挑）")
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--json",
                        default="docs/mainline_iterations/per_board_fit.json")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    started = time.monotonic()
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    quantiles = [float(x) for x in args.quantiles.split(",") if x.strip()]
    scan_windows = [int(x) for x in args.windows.split(",") if x.strip()]
    scores = load_components(TRAIN[0], TEST[1])
    closes = load_closes()
    rng = np.random.default_rng(args.seed)

    boards: list[dict] = []
    for code, (close_days, close) in closes.items():
        item = scores.get(code)
        if item is None:
            continue
        lookup = {day: position for position, day in enumerate(close_days)}
        keep = [position for position, day in enumerate(item["days"])
                if day in lookup]
        if len(keep) < 150:
            continue
        rows = [lookup[item["days"][position]] for position in keep]
        days = [item["days"][position] for position in keep]
        trimmed = {"days": days,
                   **{name: item[name][keep]
                      for name in (*COMPONENTS, "total")}}
        labels, starts = scan_launches(days, close[rows], windows=scan_windows,
                                       gain=args.gain, step=args.step)
        boards.append({"code": code, "days": days, "item": trimmed,
                       "labels": labels, "starts": starts})

    emit("# 按板块拟合打分参数：用户口径的启动集 + 22% 误报约束")
    emit()
    emit("> 由 `scripts/per_board_fit_experiment.py` 生成（只读）。")
    emit(f"> 启动集：滚动 {'/'.join(str(w) for w in scan_windows)} 日涨幅 "
         f"> {args.gain:.0%} 记当日，随后**跳过 {args.step} 个交易日**继续扫描，"
         "多窗口取并集。")
    emit(f"> 命中窗口：提前 ≤{LEAD_OK} / 滞后 ≤{LAG_OK} 个交易日；"
         f"验收：**误报率（FP/TP）≤ {FP_RATIO:.0%}**。")
    emit(f"> 训练 {TRAIN[0]}~{TRAIN[1]}；留出 {TEST[0]}~{TEST[1]}"
         "（参数只在训练集上定）。")
    emit()

    # ---------- 一、启动集规模 ----------
    train_labels = sum(1 for b in boards for i in b["labels"]
                       if TRAIN[0] <= b["days"][i] <= TRAIN[1])
    test_labels = sum(1 for b in boards for i in b["labels"]
                      if TEST[0] <= b["days"][i] <= TEST[1])
    train_starts = sum(1 for b in boards for i in b["starts"]
                       if TRAIN[0] <= b["days"][i] <= TRAIN[1])
    test_starts = sum(1 for b in boards for i in b["starts"]
                      if TEST[0] <= b["days"][i] <= TEST[1])
    per_board = sorted(sum(1 for i in b["labels"]
                           if TRAIN[0] <= b["days"][i] <= TRAIN[1])
                       for b in boards)
    emit("## 一、启动集规模（决定能不能按板块拟合）")
    emit()
    emit(f"- 参与实验的板块：**{len(boards)}** 个")
    emit(f"- 训练窗口：启动日 **{train_labels}** 个"
         f"（其中启动段首日 {train_starts} 个）")
    emit(f"- 留出窗口：启动日 **{test_labels}** 个"
         f"（其中启动段首日 {test_starts} 个）")
    emit(f"- 每板块训练样本：中位 **{int(np.median(per_board))}**、"
         f"最少 {min(per_board)}、最多 {max(per_board)}；"
         f"≥{args.min_train_labels} 个的板块 "
         f"**{sum(1 for c in per_board if c >= args.min_train_labels)}** 个")
    emit()
    emit("| 训练样本数 | 板块数 |")
    emit("|---:|---:|")
    for bucket in (0, 1, 2, 3):
        emit(f"| = {bucket} | {sum(1 for c in per_board if c == bucket)} |")
    for bucket in (4, 8, 16):
        emit(f"| ≥ {bucket} | {sum(1 for c in per_board if c >= bucket)} |")
    emit()

    # ---------- 二、定参数（只在训练集） ----------
    global_w = global_weights()
    fitted_q = {b["code"]: 0.90 for b in boards}
    fitted_w = {b["code"]: global_w for b in boards}
    fitted_cq = {b["code"]: 0.90 for b in boards}
    # 方案 D/E：在自参照分位之外再要一个**市场层面的绝对下限**
    fitted_df = {b["code"]: 0.0 for b in boards}
    fitted_ef = {b["code"]: 0.0 for b in boards}

    for board in boards:
        pct = rolling_percentile(board["item"]["total"], lookback=LOOKBACK)
        total = board["item"]["total"]
        best: tuple[float, float, float] | None = None
        for quantile in quantiles:
            for floor in FLOORS:
                stat = score_board(triggers_with(pct, quantile, floor, total),
                                   board["labels"], board["days"], *TRAIN)
                value = objective(stat)
                if best is None or value > best[0]:
                    best = (value, quantile, floor)
        if best is not None:
            fitted_q[board["code"]] = best[1]
            fitted_df[board["code"]] = best[2]

    fitted = 0
    fitted_e = 0
    fitted_e_q = {b["code"]: 0.90 for b in boards}
    fitted_e_f = {b["code"]: 0.0 for b in boards}
    for board in boards:
        count = sum(1 for i in board["labels"]
                    if TRAIN[0] <= board["days"][i] <= TRAIN[1])
        if count < args.min_train_labels:
            continue
        total = board["item"]["total"]
        candidates = [global_w] + [rng.dirichlet(np.ones(len(COMPONENTS)) * 0.7)
                                   for _ in range(max(0, args.draws))]
        best: tuple[float, np.ndarray, float] | None = None
        best_e: tuple[float, np.ndarray, float, float] | None = None
        for weights in candidates:
            pct = rolling_percentile(composite(board["item"], weights),
                                     lookback=LOOKBACK)
            for quantile in quantiles:
                stat = score_board(triggers_with(pct, quantile),
                                   board["labels"], board["days"], *TRAIN)
                value = objective(stat)
                if best is None or value > best[0]:
                    best = (value, weights, quantile)
                for floor in FLOORS:
                    stat_e = score_board(
                        triggers_with(pct, quantile, floor, total),
                        board["labels"], board["days"], *TRAIN)
                    value_e = objective(stat_e)
                    if best_e is None or value_e > best_e[0]:
                        best_e = (value_e, weights, quantile, floor)
        if best is not None:
            fitted_w[board["code"]] = best[1]
            fitted_cq[board["code"]] = best[2]
            fitted += 1
        if best_e is not None:
            fitted_ef[board["code"]] = best_e[1]
            fitted_e_q[board["code"]] = best_e[2]
            fitted_e_f[board["code"]] = best_e[3]
            fitted_e += 1

    # ---------- 三、训练 vs 留出 ----------
    emit("## 二、四个方案：训练内 vs 留出外")
    emit()
    emit(f"方案 C 真正按板块拟合了权重：**{fitted}** / {len(boards)} 个板块"
         f"（其余训练样本不足 {args.min_train_labels} 个，沿用全局权重）。")
    emit()
    emit("| 方案 | 窗口 | 启动日 | TP | FP | 召回率 | **误报率 FP/TP** | "
         "告警/板块年 | 达标 |")
    emit("|---|---|---:|---:|---:|---:|---:|---:|---|")

    def evaluate(weights_for, quantile_for, floor_for, low: str,
                 high: str) -> dict:
        total = {"labels": 0, "tp": 0, "fp": 0, "alerts": 0, "days": 0}
        for board in boards:
            pct = rolling_percentile(
                composite(board["item"], weights_for(board["code"])),
                lookback=LOOKBACK)
            stat = score_board(
                triggers_with(pct, quantile_for(board["code"]),
                              floor_for(board["code"]), board["item"]["total"]),
                board["labels"], board["days"], low, high)
            for key in ("labels", "tp", "fp", "alerts"):
                total[key] += stat[key]
            total["days"] += sum(1 for day in board["days"] if low <= day <= high)
        total["recall"] = ((total["tp"] / total["labels"]) if total["labels"]
                           else float("nan"))
        total["fp_ratio"] = ((total["fp"] / total["tp"]) if total["tp"]
                             else float("inf"))
        total["ok"] = bool(total["tp"]) and total["fp"] <= FP_RATIO * total["tp"]
        return total

    def evaluate_absolute(low: str, high: str) -> dict:
        total = {"labels": 0, "tp": 0, "fp": 0, "alerts": 0, "days": 0}
        for board in boards:
            values = board["item"]["total"]
            triggers = np.isfinite(values) & (values >= 77.0)
            stat = score_board(triggers, board["labels"], board["days"],
                               low, high)
            for key in ("labels", "tp", "fp", "alerts"):
                total[key] += stat[key]
            total["days"] += sum(1 for day in board["days"] if low <= day <= high)
        total["recall"] = ((total["tp"] / total["labels"]) if total["labels"]
                           else float("nan"))
        total["fp_ratio"] = ((total["fp"] / total["tp"]) if total["tp"]
                             else float("inf"))
        total["ok"] = bool(total["tp"]) and total["fp"] <= FP_RATIO * total["tp"]
        return total

    def emit_row(name: str, tag: str, stat: dict) -> None:
        # ⚠️ 分母是"板块×交易日"。第一版除了板年还多除了一个板块数，
        # 于是把"每板块每年 8.9 条"显示成 0.03 条 —— 只看那个数会以为
        # 这套规则几乎不报警。
        board_years = stat["days"] / 244.0
        per_year = stat["alerts"] / max(board_years, 1e-6)
        emit(f"| {name} | {tag} | {stat['labels']} | {stat['tp']} "
             f"| {stat['fp']} | {stat['recall']:.0%} "
             f"| {stat['fp_ratio']:.2f} | {per_year:.2f} "
             f"| {'✅' if stat['ok'] else '❌'} |")

    results: dict[str, dict] = {}
    zero = lambda _code: 0.0  # noqa: E731 下限：0 表示"不设绝对门槛"
    plan = [
        ("A0 全局绝对线 77", None),
        ("A 全局权重+全局分位", (lambda _code: global_w,
                            lambda _code: 0.90, zero)),
        ("B 全局权重+按板块分位", (lambda _code: global_w,
                            lambda code: fitted_q[code], zero)),
        ("C 按板块权重+按板块分位", (lambda code: fitted_w[code],
                              lambda code: fitted_cq[code], zero)),
        ("D 全局权重+按板块(分位,下限)", (lambda _code: global_w,
                                  lambda code: fitted_df[code], zero)),
        ("E 按板块权重+按板块(分位,下限)", (lambda code: fitted_ef[code],
                                    lambda code: fitted_e_q[code],
                                    lambda code: fitted_e_f[code])),
    ]
    for name, handlers in plan:
        for tag, (low, high) in (("训练", TRAIN), ("留出", TEST)):
            if handlers is None:
                stat = evaluate_absolute(low, high)
            else:
                stat = evaluate(handlers[0], handlers[1], handlers[2], low, high)
            results.setdefault(name, {})[tag] = stat
            emit_row(name, tag, stat)
    emit()

    # ---------- 三、阈值前沿：**22% 到底能不能达到** ----------
    emit("## 三、天花板在哪：随机触发能拿到多少")
    emit()
    emit("判据 FP/TP 的分母分子都由**标签密度**决定：如果启动窗口本身覆盖了"
         "全体板块日的 X%，那么一个**随机**触发落在窗口内的概率就是 X%，"
         "对应的 FP/TP = (1−X)/X。这是不看任何因子的下限参照 —— "
         "任何因子方案都要先明显好过它，才谈得上「有信号」。")
    emit()
    emit("| 窗口 | 板块日总数 | 落在启动窗口内 | 覆盖比例 X | 随机触发的 FP/TP |")
    emit("|---|---:|---:|---:|---:|")
    for tag, (low, high) in (("训练", TRAIN), ("留出", TEST)):
        inside = total_days = 0
        for board in boards:
            covered: set[int] = set()
            for index in board["labels"]:
                if low <= board["days"][index] <= high:
                    covered |= set(range(max(0, index - LEAD_OK),
                                         index + LAG_OK + 1))
            for position, day in enumerate(board["days"]):
                if low <= day <= high:
                    total_days += 1
                    if position in covered:
                        inside += 1
        share = inside / max(total_days, 1)
        emit(f"| {tag} | {total_days} | {inside} | {share:.1%} "
             f"| {(1 - share) / share:.2f} |")
    emit()

    emit("## 四、阈值前沿：**22% 到底能不能达到**")
    emit()
    emit("方案 C 的**权重固定**（按板块拟合好的那份），只把阈值从松到紧扫一遍。"
         "这张表回答的是「这个判据有没有可行解」—— 如果连最紧的一端"
         "都下不到 0.22，那就不是参数没调好，而是这套打分在这套标签上"
         "本来就分不开「启动」和「没启动」。")
    emit()
    frontier = [0.90, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999]
    emit("| 分位 | 窗口 | TP | FP | 召回率 | 误报率 FP/TP | 达标 |")
    emit("|---:|---|---:|---:|---:|---:|---|")
    for quantile in frontier:
        for tag, (low, high) in (("训练", TRAIN), ("留出", TEST)):
            stat = evaluate(lambda code: fitted_w[code],
                            lambda _code, q=quantile: q, zero, low, high)
            ok = bool(stat["tp"]) and stat["fp"] <= FP_RATIO * stat["tp"]
            emit(f"| {quantile:g} | {tag} | {stat['tp']} | {stat['fp']} "
                 f"| {stat['recall']:.0%} | {stat['fp_ratio']:.2f} "
                 f"| {'✅' if ok else '❌'} |")
    emit()

    # ---------- 五、只看"启动段首日" ----------
    emit("## 五、只看每个启动段的**首个**交叉日（用户说的「主线启动日」）")
    emit()
    emit("| 方案 | 窗口 | 首日数 | TP | FP | 召回率 | 误报率 | 达标 |")
    emit("|---|---|---:|---:|---:|---:|---:|---|")
    for name, handlers in plan:
        for tag, (low, high) in (("训练", TRAIN), ("留出", TEST)):
            tp = fp = labels = 0
            for board in boards:
                starts = [i for i in board["starts"]
                          if low <= board["days"][i] <= high]
                if not starts:
                    continue
                labels += len(starts)
                if handlers is None:
                    values = board["item"]["total"]
                    triggers = np.isfinite(values) & (values >= 77.0)
                else:
                    total = board["item"]["total"]
                    pct = rolling_percentile(
                        composite(board["item"], handlers[0](board["code"])),
                        lookback=LOOKBACK)
                    triggers = triggers_with(pct, handlers[1](board["code"]),
                                             handlers[2](board["code"]), total)
                inside = np.asarray([low <= day <= high
                                     for day in board["days"]])
                fired = np.flatnonzero(triggers & inside)
                covered: set[int] = set()
                for index in starts:
                    window = range(max(0, index - LEAD_OK), index + LAG_OK + 1)
                    if any(t in window for t in fired):
                        tp += 1
                    covered |= set(range(max(0, index - LEAD_OK),
                                         index + LAG_OK + 1))
                fp += sum(1 for t in fired if t not in covered)
            ratio = (fp / tp) if tp else float("inf")
            ok = bool(tp) and fp <= FP_RATIO * tp
            emit(f"| {name} | {tag} | {labels} | {tp} | {fp} "
                 f"| {(tp / labels) if labels else 0:.0%} | {ratio:.2f} "
                 f"| {'✅' if ok else '❌'} |")
    emit()

    emit("## 六、怎么读")
    emit()
    emit(f"- 判据只有一条：**FP/TP ≤ {FP_RATIO:.0%}**。"
         "`达标` 是训练/留出各自的结论；**留出达标才算数**。")
    emit("- 方案 C 在训练集里必然不差（它比 B 多一个自由度级别）；"
         "留出列与 A/B 的差距才是「按板块拟合」的真实收益。")
    emit("- 方案 B 只放开门槛、权重仍全局，参数少一个量级；"
         "如果 B 和 C 的留出结论接近，就该选 B —— 每板块 2 个参数在"
         f"中位 {int(np.median(per_board))} 个训练样本上是很难撑住的。")
    emit(f"- 本实验耗时 {time.monotonic() - started:.0f} 秒。")

    if args.json:
        target = ROOT / args.json
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(
            {"results": results, "boards": len(boards), "fitted": fitted,
             "train_labels": train_labels, "test_labels": test_labels,
             "train_starts": train_starts, "test_starts": test_starts,
             "gain": args.gain, "windows": scan_windows, "step": args.step,
             "fp_ratio": FP_RATIO}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"实验数据 → {target}")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
