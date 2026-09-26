"""每个概念板块的**误报率 + 持有 20 交易日盈亏**全量清单（可导出 Excel）。

## 用户要什么

> 「猪肉板块和中船系都是大周期概念板块，本身走势偏弱，都是小波段行情，
>   误报率高也要看持有 20 交易日亏损率怎么样。概念误报率清单都列出来吧，
>   我统一处理下。」

所以这份清单必须**同时**给两组东西，只给误报率会选错板块：

- **误报口径**：启动集判据（与验收口径一致）—— 启动日 = 板块指数滚动
  20/35 日涨幅 > 15% 的日子、命中后跳 `step` 个交易日；
  TP = 告警落在启动日 ±[−lead, +lag] 个交易日内，FP = 不落在任何容差窗口内。
- **盈亏口径**：以告警日收盘（`entry_close`，缺失时回落板块指数收盘）为买入价，
  看未来 5/10/20 个交易日的收益、亏损率、深亏率、20 日内最大浮亏（MAE）。

⚠️ **两组会给出不同的名单，这是正常的**（`losing_themes_report.py` 的
docstring 早就写过）：一个板块可以"没大跌但也没涨"（无行情但不算亏），
也可以"涨过 15% 但你先亏了 20%"（有行情但拿不住）。
**大周期板块尤其如此** —— 猪肉、中船系这类自身趋势弱的板块，
告警后 20 日经常只是横盘（小波段），误报率高但**亏损率不高**，
把它们和"误报高 + 深亏"的板块混在一起砍掉是错的。

## 为什么还要给「板块自身的强弱」

用户点出的「本身走势偏弱、都是小波段行情」是个**可量化的**判断，
所以每个板块都带上：

    idx_ret      窗口内板块指数总涨幅（正 = 上行趋势，负 = 长期走弱）
    idx_dd       窗口内最大回撤
    launch_n     窗口内启动日数（≥15% 波段出现过多少次）
    alerts_per_launch  告警数 / 启动日数 —— 触发密度相对机会的倍数
                 （>1 说明"报得比机会还多"，是结构性过度触发的信号）

## 产物

- Markdown：全量清单 + 交集榜 + 前 N 名逐条误报日期
- Excel（`--excel`）：`全量清单` / `强中档清单` / `逐条明细` 三个 sheet，
  逐条明细带每条告警的 TP/FP 判定与未来收益，便于人工逐条处置

只读，不写库（除报告与 Excel）。

用法：
    .venv\\Scripts\\python.exe scripts/board_false_positive_report.py \\
        --alert-table mainline_alert_bak_v26_prefloor \\
        --out docs/MAINLINE_FALSE_POSITIVE_BOARDS.md \\
        --excel docs/MAINLINE_CONCEPT_FP_LIST.xlsx
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import _load_alert_scope  # noqa: E402

MAIN_DB = ROOT / os.environ.get("MOSS_SQLITE_PATH", "data/moss_finagent.db")
CACHE_DB = ROOT / "data" / "mainline_cache.db"
SCOPE = ROOT / "configs" / "mainline_alert_exclusions.yaml"
LEAD_OK, LAG_OK = 10, 5
BIG = 0.15
HORIZONS = (5, 10, 20)
MAE_HORIZON = 20


def scan_labels(close: np.ndarray, *, windows: tuple[int, ...], gain: float,
                step: int) -> list[int]:
    """启动集：滚动 `window` 日涨幅 > `gain` 的日子，命中后跳 `step` 继续扫。

    与 `per_board_fit_experiment.scan_window` 逐行等价（多窗口取并集），
    这样本报告的 FP 与验收口径的 FP 是同一个东西。
    """
    n = len(close)
    labels: set[int] = set()
    for window in windows:
        if n <= window + 1:
            continue
        rolling = np.full(n, np.nan)
        rolling[window:] = close[window:] / close[:-window] - 1.0
        pos = window
        while pos < n:
            if np.isfinite(rolling[pos]) and rolling[pos] > gain:
                labels.add(pos)
                pos += max(1, step)
            else:
                pos += 1
    return sorted(labels)


def main() -> int:
    parser = argparse.ArgumentParser(description="概念误报率 + 20 日盈亏全量清单")
    parser.add_argument("--alert-table", default="mainline_alert_bak_v26_prefloor")
    parser.add_argument("--scope", default=str(SCOPE))
    parser.add_argument("--apply-scope", action="store_true", default=True,
                        help="先剔掉已被范围闸门拦掉的告警（默认开）")
    parser.add_argument("--no-apply-scope", dest="apply_scope",
                        action="store_false", help="不过滤，看原始榜")
    parser.add_argument("--windows", default="20,35")
    parser.add_argument("--gain", type=float, default=BIG)
    parser.add_argument("--step", type=int, default=4)
    parser.add_argument("--lead", type=int, default=LEAD_OK)
    parser.add_argument("--lag", type=int, default=LAG_OK)
    parser.add_argument("--top", type=int, default=10,
                        help="逐条误报日期只打印前 N 名（全量清单始终是全部）")
    parser.add_argument("--min-alerts", type=int, default=1,
                        help="进全量清单的告警数下限（默认 1）")
    parser.add_argument("--class-min-alerts", type=int, default=8,
                        help="甲/乙类排序表的告警数下限（默认 8）——"
                             "不加这层，榜首会是「1~2 次告警全亏」的噪声")
    parser.add_argument("--out", default="")
    parser.add_argument("--excel", default="")
    args = parser.parse_args()

    windows = tuple(int(x) for x in str(args.windows).split(",") if x.strip())
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    # ---------- 告警 + 范围闸门 ----------
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = [dict(r) for r in conn.execute(
        f"SELECT trade_date, board_code, board_name, level, score, entry_close"
        f" FROM {args.alert_table} ORDER BY trade_date")]
    conn.close()
    if not alerts:
        print(f"❌ 告警表 {args.alert_table} 为空")
        return 2

    scope = _load_alert_scope(args.scope)
    blocked: Counter[str] = Counter()
    if args.apply_scope:
        kept = []
        for row in alerts:
            # 判定统一走 `permits()`，不在脚本里重复模式字符串比对
            if scope.permits(str(row["board_code"]), str(row["trade_date"]),
                             str(row["level"]), row["score"]):
                kept.append(row)
            else:
                blocked[str(row["board_code"])] += 1
        alerts = kept

    low = min(str(r["trade_date"]) for r in alerts)
    high = max(str(r["trade_date"]) for r in alerts)

    # ---------- 板块指数 ----------
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    bars: dict[str, list[tuple[str, float]]] = {}
    for row in cache.execute(
            "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        bars.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    names = {str(r["code"]): str(r["name"]) for r in
             cache.execute("SELECT code, name FROM ml_board")}
    cache.close()

    prepared: dict[str, dict] = {}
    for code, items in bars.items():
        day_list = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        labels = scan_labels(close, windows=windows, gain=args.gain,
                             step=args.step)
        in_window = [i for i in labels if low <= day_list[i] <= high]
        # 板块自身强弱：只看告警区间那一段，才和告警的盈亏可比
        span = [i for i, d in enumerate(day_list) if low <= d <= high]
        if span:
            seg = close[span[0]: span[-1] + 1]
            idx_ret = float(seg[-1] / seg[0] - 1.0) if seg[0] else float("nan")
            peak = np.maximum.accumulate(seg)
            idx_dd = float((seg / peak - 1.0).min()) if peak.size else float("nan")
        else:
            idx_ret = idx_dd = float("nan")
        prepared[code] = {
            "days": day_list, "close": close,
            "index": {d: i for i, d in enumerate(day_list)},
            "labels": labels, "in_window": in_window,
            "idx_ret": idx_ret, "idx_dd": idx_dd,
            "covered": {t for i in in_window
                        for t in range(max(0, i - args.lead),
                                       i + args.lag + 1)},
        }

    # ---------- 逐条判定（TP/FP + 未来收益） ----------
    per_board: dict[str, dict] = {}
    detail: list[dict] = []
    orphan_days = 0
    for row in alerts:
        code = str(row["board_code"])
        day = str(row["trade_date"])
        item = prepared.get(code)
        if item is None or day not in item["index"]:
            orphan_days += 1
            continue
        pos = item["index"][day]
        close = item["close"]
        entry = float(row["entry_close"] or 0.0) or float(close[pos])
        level = str(row["level"])
        strong = level in ("strong", "medium")
        is_tp = pos in item["covered"]
        bucket = per_board.setdefault(code, {
            "name": names.get(code) or str(row["board_name"] or code),
            "tp": 0, "fp": 0, "fp_days": [], "tp_days": [],
            "fp_strong": 0, "tp_strong": 0,
            "fp_by_level": Counter(), "levels": Counter(),
            "fwd": {h: [] for h in HORIZONS}, "mae": []})
        bucket["levels"][level] += 1
        if is_tp:
            bucket["tp"] += 1
            bucket["tp_days"].append(day)
            bucket["tp_strong"] += int(strong)
        else:
            bucket["fp"] += 1
            bucket["fp_days"].append((day, level))
            bucket["fp_by_level"][level] += 1
            bucket["fp_strong"] += int(strong)
        # 未来收益（板块指数价；买入价用告警日实际收盘）
        row_out = {"板块": bucket["name"], "代码": code, "触发日": day,
                   "档位": level, "总分": float(row["score"] or 0.0),
                   "判定": "命中" if is_tp else "误报"}
        if entry > 0:
            for horizon in HORIZONS:
                target = pos + horizon
                value = (float(close[target]) / entry - 1.0
                         if target < len(close) else float("nan"))
                row_out[f"{horizon}日收益"] = value * 100.0
                if np.isfinite(value):
                    bucket["fwd"][horizon].append(value)
            window = close[pos + 1: pos + 1 + MAE_HORIZON]
            if window.size:
                mae = float(window.min()) / entry - 1.0
                row_out["20日最大浮亏"] = mae * 100.0
                bucket["mae"].append(mae)
        detail.append(row_out)

    for code, bucket in per_board.items():
        item = prepared[code]
        bucket["code"] = code
        bucket["alerts"] = bucket["tp"] + bucket["fp"]
        bucket["launch_n"] = len(item["in_window"])
        bucket["fp_rate"] = bucket["fp"] / bucket["alerts"]
        bucket["idx_ret"] = item["idx_ret"]
        bucket["idx_dd"] = item["idx_dd"]
        bucket["strong_alerts"] = bucket["tp_strong"] + bucket["fp_strong"]
        bucket["strong_fp_rate"] = (bucket["fp_strong"] / bucket["strong_alerts"]
                                    if bucket["strong_alerts"] else float("nan"))
        for horizon in HORIZONS:
            values = np.asarray(bucket["fwd"][horizon])
            bucket[f"mean{horizon}"] = (float(values.mean()) if values.size
                                        else float("nan"))
            bucket[f"sum{horizon}"] = (float(values.sum()) if values.size
                                       else float("nan"))
        values = np.asarray(bucket["fwd"][20])
        bucket["loss20"] = float((values < 0).mean()) if values.size else float("nan")
        bucket["deep20"] = (float((values < -0.10).mean()) if values.size
                            else float("nan"))
        bucket["win20"] = float((values > 0).mean()) if values.size else float("nan")
        mae = np.asarray(bucket["mae"])
        bucket["mae_mean"] = float(mae.mean()) if mae.size else float("nan")
        bucket["mae_worst"] = float(mae.min()) if mae.size else float("nan")

    total_tp = sum(b["tp"] for b in per_board.values())
    total_fp = sum(b["fp"] for b in per_board.values())
    strong_tp = sum(b["tp_strong"] for b in per_board.values())
    strong_fp = sum(b["fp_strong"] for b in per_board.values())

    emit("# 概念板块误报率 + 持有 20 日盈亏 全量清单")
    emit()
    emit(f"> 由 `scripts/board_false_positive_report.py` 生成（只读，告警表 "
         f"`{args.alert_table}`）。")
    emit(f"> 横轴 {low} ~ {high}：过滤后告警 **{len(alerts)}** 条"
         f"（{len(per_board)} 个板块）。")
    if args.apply_scope:
        emit(f"> 已按 `{Path(args.scope).name}` 剔除被拦告警 "
             f"**{sum(blocked.values())}** 条（{len(blocked)} 个板块）。")
    else:
        emit("> **未过范围闸门**（`--no-apply-scope`），是原始榜。")
    if orphan_days:
        emit(f"> ⚠️ {orphan_days} 条告警在板块指数里找不到对应交易日，未计入。")
    emit()
    emit(f"- **误报口径**：启动集 = 滚动 {'/'.join(str(w) for w in windows)} 日"
         f"涨幅 > {args.gain:.0%}、命中后跳 {args.step} 个交易日；"
         f"TP = 告警落在启动日 ±[−{args.lead}, +{args.lag}] 个交易日内。")
    emit(f"- 全档位：**TP {total_tp} / FP {total_fp}** = "
         f"{(total_fp / total_tp if total_tp else float('nan')):.2f}；"
         f"strong+medium：**TP {strong_tp} / FP {strong_fp}** = "
         f"{(strong_fp / strong_tp if strong_tp else float('nan')):.2f}")
    emit(f"- **盈亏口径**：买入价 = 告警日收盘（`entry_close`，缺失回落到板块指数）；"
         f"亏损率 = 20 个交易日后收益 < 0 的占比。")
    emit()
    emit("> ⚠️ **大周期板块要看第二组数字**：猪肉、中船系这类自身趋势弱的板块，")
    emit("> 告警后经常只是横盘（小波段），**误报率高但亏损率不一定高**。")
    emit("> 只按误报率砍，会把「小赚小亏、只是没等到大行情」的板块和")
    emit("> 「真的持续深亏」的板块混在一起。所以下面第四节专门给了两张判据。")
    emit()

    eligible = [b for b in per_board.values() if b["alerts"] >= args.min_alerts]
    full = sorted(eligible, key=lambda b: (-b["fp"], -b["fp_rate"]))
    strong_ranked = sorted([b for b in eligible if b["strong_alerts"] > 0],
                           key=lambda b: (-b["fp_strong"],))

    def row_of(rank: int, item: dict) -> str:
        return (f"| {rank} | {item['name']}（{item['code']}） "
                f"| {item['alerts']} | {item['fp']} | {item['fp_rate']:.0%} "
                f"| {item['tp']} | {item['launch_n']} "
                f"| {item.get('mean20', float('nan')):+.2%} "
                f"| **{item.get('loss20', float('nan')):.0%}** "
                f"| {item.get('deep20', float('nan')):.0%} "
                f"| {item.get('mae_mean', float('nan')):+.2%} "
                f"| {item.get('idx_ret', float('nan')):+.1%} |")

    emit(f"## 一、全量清单（{len(full)} 个板块，按误报条数降序）")
    emit()
    emit("| # | 板块 | 告警 | **误报** | 误报率 | 命中 | 启动日 "
         "| 均值20日 | **亏损率20** | 深亏率 | 最大浮亏均 | 指数区间涨幅 |")
    emit("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, item in enumerate(full, 1):
        emit(row_of(i, item))
    emit()

    emit(f"## 二、只看 strong + medium 档（{len(strong_ranked)} 个板块）")
    emit()
    emit("> weak 是观察档、不进推送。这一节把 weak 排除后重排。")
    emit()
    emit("| # | 板块 | 告警(强中) | **误报** | 误报率 | 启动日 "
         "| 均值20日 | **亏损率20** | 深亏率 | 指数区间涨幅 |")
    emit("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, item in enumerate(strong_ranked, 1):
        emit(f"| {i} | {item['name']}（{item['code']}） "
             f"| {item['strong_alerts']} | **{item['fp_strong']}** "
             f"| {item['strong_fp_rate']:.0%} | {item['launch_n']} "
             f"| {item.get('mean20', float('nan')):+.2%} "
             f"| **{item.get('loss20', float('nan')):.0%}** "
             f"| {item.get('deep20', float('nan')):.0%} "
             f"| {item.get('idx_ret', float('nan')):+.1%} |")
    emit()

    # ---------- 两个判据的交集 ----------
    #
    # 「误报高」和「亏钱」是两件事（见脚本 docstring）。用户要的是**同时看**，
    # 所以给三档，而不是一个名单：
    #   甲 误报率 ≥60% 且 20 日均值 < 0   → 报得多 + 真亏钱，最该处理
    #   乙 误报率 ≥60% 但 20 日均值 ≥ 0   → 报得多但不亏（小波段/横盘），
    #                                      砍掉会损失少量真波段机会
    #   丙 误报率 < 60%                   → 不在误报榜前列
    emit("## 三、按「误报率 × 20 日盈亏」分档（用户要的统一处置依据）")
    emit()
    jia = [b for b in full if b["fp_rate"] >= 0.60
           and np.isfinite(b.get("mean20", np.nan)) and b["mean20"] < 0]
    yi = [b for b in full if b["fp_rate"] >= 0.60
          and np.isfinite(b.get("mean20", np.nan)) and b["mean20"] >= 0]
    emit(f"- **甲类：误报率 ≥60% 且 20 日均值为负** —— {len(jia)} 个"
         f"（报得多 + 真亏钱，处置优先级最高）")
    emit(f"- **乙类：误报率 ≥60% 但 20 日均值 ≥0** —— {len(yi)} 个"
         f"（报得多但不亏，多为小波段/横盘，砍之前要想清楚）")
    emit(f"- 丙类：误报率 <60% —— {len(full) - len(jia) - len(yi)} 个")
    emit()
    # ⚠️ 样本量下限：不加这一层，榜首会是「1~2 次告警全亏」的板块
    # （物业管理 2 次告警均值 −12.68%、快手概念 1 次 −9.63%），
    # 那是噪声不是结论。全量清单里仍然保留它们，只是不参与排序处置。
    floor = args.class_min_alerts
    jia_big = [b for b in jia if b["alerts"] >= floor]
    yi_big = [b for b in yi if b["alerts"] >= floor]
    emit(f"> ⚠️ 下面两张表加**样本量下限 {floor} 条告警**，否则榜首全是"
         f"「1~2 次告警全亏」的噪声（甲类里 {len(jia) - len(jia_big)} 个、"
         f"乙类里 {len(yi) - len(yi_big)} 个因样本不足未列入，"
         f"但它们**在全量清单与 Excel 里都在**）。")
    emit()
    emit(f"### 甲类（误报率 ≥60% 且 20 日均值 < 0，告警数 ≥{floor}；"
         f"按**亏损率 20 日**降序）")
    emit()
    emit("| # | 板块 | 告警 | 误报率 | 均值20日 | **亏损率20** | 深亏率 "
         "| 最大浮亏均 | 指数区间涨幅 |")
    emit("|---:|---|---:|---:|---:|---:|---:|---:|---:|")
    jia_sorted = sorted(jia_big, key=lambda b: (-b["loss20"], b["mean20"]))
    for i, item in enumerate(jia_sorted, 1):
        emit(f"| {i} | {item['name']}（{item['code']}） | {item['alerts']} "
             f"| {item['fp_rate']:.0%} "
             f"| {item['mean20']:+.2%} | **{item['loss20']:.0%}** "
             f"| {item['deep20']:.0%} | {item['mae_mean']:+.2%} "
             f"| {item.get('idx_ret', float('nan')):+.1%} |")
    emit()
    emit(f"### 乙类（误报率 ≥60% 但 20 日均值 ≥0 —— 小波段/横盘，"
         f"告警数 ≥{floor}；按误报率降序）")
    emit()
    emit("| # | 板块 | 告警 | 误报率 | 均值20日 | **亏损率20** | 深亏率 "
         "| 指数区间涨幅 |")
    emit("|---:|---|---:|---:|---:|---:|---:|---:|")
    yi_sorted = sorted(yi_big, key=lambda b: -b["fp_rate"])
    for i, item in enumerate(yi_sorted, 1):
        emit(f"| {i} | {item['name']}（{item['code']}） | {item['alerts']} "
             f"| {item['fp_rate']:.0%} | {item['mean20']:+.2%} "
             f"| **{item['loss20']:.0%}** | {item['deep20']:.0%} "
             f"| {item.get('idx_ret', float('nan')):+.1%} |")
    emit()
    emit(f"### 甲类的完整名单（含样本不足的 {len(jia) - len(jia_big)} 个，"
         f"按 20 日均值升序）")
    emit()
    emit("| # | 板块 | 告警 | 误报率 | 均值20日 | 亏损率20 | 深亏率 | 样本足 |")
    emit("|---:|---|---:|---:|---:|---:|---:|:--:|")
    for i, item in enumerate(sorted(jia, key=lambda b: b["mean20"]), 1):
        emit(f"| {i} | {item['name']}（{item['code']}） | {item['alerts']} "
             f"| {item['fp_rate']:.0%} | {item['mean20']:+.2%} "
             f"| {item['loss20']:.0%} | {item['deep20']:.0%} "
             f"| {'是' if item['alerts'] >= floor else ''} |")
    emit()

    # ---------- 用户点名的两个 ----------
    emit("## 四、用户点名的两个（猪肉 885573 / 中船系 885860）")
    emit()
    emit("| 板块 | 告警 | 误报 | 误报率 | 均值20日 | **亏损率20** | 深亏率 "
         "| 最大浮亏均 | 启动日 | 指数区间涨幅 | 最大回撤 |")
    emit("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for code in ("885573.TI", "885860.TI"):
        item = per_board.get(code)
        if item is None:
            emit(f"| {code} | — | — | — | — | — | — | — | — | — | — |")
            continue
        emit(f"| {item['name']}（{code}） | {item['alerts']} | {item['fp']} "
             f"| {item['fp_rate']:.0%} | {item.get('mean20', float('nan')):+.2%} "
             f"| **{item.get('loss20', float('nan')):.0%}** "
             f"| {item.get('deep20', float('nan')):.0%} "
             f"| {item.get('mae_mean', float('nan')):+.2%} "
             f"| {item['launch_n']} "
             f"| {item.get('idx_ret', float('nan')):+.1%} "
             f"| {item.get('idx_dd', float('nan')):+.1%} |")
    emit()

    emit(f"## 五、误报条数前 {args.top} 名的逐条误报日期")
    emit()
    for i, item in enumerate(full[:args.top], 1):
        emit(f"### {i}. {item['name']}（{item['code']}）—— 误报 {item['fp']} 条 / "
             f"告警 {item['alerts']} 条（{item['fp_rate']:.0%}）")
        emit()
        emit("- 等级构成：" + "、".join(f"{k} {v}"
                                   for k, v in item["levels"].most_common()))
        emit(f"- 窗口内启动日 {item['launch_n']} 个，命中 {item['tp']} 条")
        if item["fp_days"]:
            text = "、".join(day if level == "weak" else f"{day}({level})"
                            for day, level in sorted(item["fp_days"]))
            emit(f"- **误报日期（{len(item['fp_days'])} 个）**：{text}")
        if item["tp_days"]:
            emit(f"- 命中日期（{len(item['tp_days'])} 个）："
                 + "、".join(sorted(item["tp_days"])))
        emit()

    by_month = Counter(day[:6] for b in full[:args.top]
                       for day, _lv in b["fp_days"])
    emit(f"## 六、前 {args.top} 名的误报月份分布")
    emit()
    emit("| 月份 | 误报条数 |")
    emit("|---|---:|")
    for month, count in sorted(by_month.items()):
        emit(f"| {month} | {count} |")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")

    if args.excel:
        write_excel(Path(args.excel), full, strong_ranked, detail,
                    source=args.alert_table, low=low, high=high)
    return 0


def write_excel(target: Path, full: list[dict], strong_ranked: list[dict],
                detail: list[dict], *, source: str, low: str, high: str) -> None:
    """三个 sheet：全量清单 / 强中档清单 / 逐条明细。

    ⚠️ 用 `openpyxl` 而不是 `csv`：清单里全是中文板块名，CSV 交给 Excel 打开时
    会按本地代码页解码（`gbk`）而乱码；xlsx 没有这个问题。
    Excel 只是**产物**，源代码里的中文注释不受影响。
    """
    import pandas as pd

    def frame(rows: list[dict], strong: bool) -> pd.DataFrame:
        out = []
        for item in rows:
            out.append({
                "板块": item["name"], "代码": item["code"],
                "告警数": item["alerts"],
                "其中strong+medium": item["strong_alerts"],
                "误报数": item["fp_strong"] if strong else item["fp"],
                "误报率": (item["strong_fp_rate"] if strong else item["fp_rate"]),
                "命中数": item["tp_strong"] if strong else item["tp"],
                "窗口内启动日": item["launch_n"],
                "告警/启动日": (round(item["alerts"] / item["launch_n"], 2)
                            if item["launch_n"] else ""),
                "均值5日": item.get("mean5"), "均值10日": item.get("mean10"),
                "均值20日": item.get("mean20"),
                "亏损率20": item.get("loss20"), "深亏率20": item.get("deep20"),
                "20日最大浮亏均": item.get("mae_mean"),
                "20日最大浮亏最差": item.get("mae_worst"),
                "指数区间涨幅": item.get("idx_ret"),
                "指数区间最大回撤": item.get("idx_dd"),
            })
        return pd.DataFrame(out)

    target.parent.mkdir(parents=True, exist_ok=True)
    notes = pd.DataFrame([
        {"项": "告警表", "值": source},
        {"项": "区间", "值": f"{low} ~ {high}"},
        {"项": "误报口径",
         "值": "启动集=滚动20/35日涨幅>15%、命中后跳4个交易日；"
                "TP=告警落在启动日±[−10,+5]个交易日内，FP=其余"},
        {"项": "盈亏口径",
         "值": "买入价=告警日收盘（entry_close，缺失回落到板块指数收盘）；"
                "收益按板块指数价；亏损率20 = 20个交易日后收益<0 的占比"},
        {"项": "为什么两组都要看",
         "值": "大周期板块（如猪肉、中船系）自身趋势弱、多小波段，"
                "误报率高但亏损率不一定高；只按误报率砍会误伤"},
        {"项": "指数区间涨幅",
         "值": "窗口内板块指数总涨幅，衡量「本身走势偏弱」；"
                "最大回撤衡量长期走弱的幅度"},
    ])
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        frame(full, strong=False).to_excel(writer, sheet_name="全量清单", index=False)
        frame(strong_ranked, strong=True).to_excel(writer, sheet_name="强中档清单",
                                                  index=False)
        pd.DataFrame(detail).to_excel(writer, sheet_name="逐条明细", index=False)
        notes.to_excel(writer, sheet_name="口径说明", index=False)
        _autofit(writer.sheets)
    print(f"Excel → {target}")


def _autofit(sheets: dict) -> None:
    """按内容估个列宽（中文按 2 个字符宽算），并给比率列加百分号格式。

    ⚠️ 比率在 DataFrame 里是小数（`0.8667`）。不加格式的话，用户打开 Excel
    看到的是 `0.866667` 而不是 `86.7%` —— 清单是要人**逐条处置**的，
    这一层不能省。判据用列名关键字：本项目里凡是 `率` / `收益` / `涨幅` /
    `回撤` / `浮亏` / `均值` 结尾的列都是比率。
    """
    percent_keys = ("率", "收益", "涨幅", "回撤", "浮亏", "均值", "误报率")
    for sheet in sheets.values():
        header = [cell.value for cell in sheet[1]]
        for index, title in enumerate(header, 1):
            letter = sheet.cell(row=1, column=index).column_letter
            width = 8
            is_percent = any(str(title).endswith(k) or k in str(title)
                             for k in percent_keys)
            for cell in sheet[letter]:
                text = "" if cell.value is None else str(cell.value)
                size = sum(2 if ord(ch) > 127 else 1 for ch in text)
                width = max(width, min(size + 2, 42))
                if is_percent and isinstance(cell.value, (int, float)):
                    cell.number_format = "0.0%"
            sheet.column_dimensions[letter].width = width
        sheet.freeze_panes = "A2"


if __name__ == "__main__":
    raise SystemExit(main())
