"""候选闸门用的是「加分前的六维」，把高分板块挡在告警之外 —— 影响量化。

## 发现（煤炭 885914 的诊断副产品）

`_decide_level` 的第一道闸门是：

    if not board.candidate:
        return SignalLevel.NONE

而 `candidate` **只由第一层六维的横截面排名决定**（`six_dim.candidate_cutoff`，
当日前 ~64 名）。问题是 `etf_bonus` / `gate_bonus` 是在**进池之后**才加的：

    20260605 煤炭 885914：six_dim 54.55（排 119）→ 不进池 → 直接 NONE
                            但 etf_bonus 12 + gate_bonus 15 → total 81.55
                            （当日强信号线 80、中信号线 77）

也就是说：一个板块可以同时拿到**龙头共振**和 **ETF 异动**这两个最强的横向
确认信号，把最终分推到强信号线之上，却因为"加分前的六维排名"差 5 分而
**一条告警都发不出来**。全库处于这个状态的板块日有上千条。

## 本脚本量化什么

把「`total` 够线但因为 `candidate=0` 没报」的板块日当作**加上去的告警**，
看四件事：

1. 量级：多出多少条、日均多少；
2. 质量：未来 20 日涨超 10% / 跌超 10% 的比例与均值 —— 与现有告警集对照；
3. 真值覆盖：24 个真值事件的合格/太早/滞后/漏报怎么变；
4. 它们集中在哪些板块。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/candidate_gate_report.py \\
        --out docs/MAINLINE_CANDIDATE_GATE.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LEAD_OK, LAG_OK = 10, 5
LOOKBACK_EVENT, LOOKAHEAD_EVENT = 20, 40
BIG = 0.10
HORIZON = 20


def main() -> int:
    parser = argparse.ArgumentParser(description="候选闸门的影响量化")
    parser.add_argument("--score-table", default="mainline_score")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--strong", type=float, default=80.0)
    parser.add_argument("--medium", type=float, default=77.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    main_db = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    main_db.row_factory = sqlite3.Row
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row

    rows = main_db.execute(
        f"SELECT trade_date, board_code, total, base_total, six_dim, gate_bonus,"
        f" rank, candidate, level FROM {args.score_table}").fetchall()
    alert_rows = main_db.execute(
        f"SELECT trade_date, board_code, level FROM {args.alert_table}").fetchall()
    emit("# 候选闸门：`total` 够线却因 `candidate=0` 不报的影响")
    emit()
    emit("> 由 `scripts/candidate_gate_report.py` 生成（只读）。")
    emit(f"> 判据：`total ≥ {args.medium:g}`（中信号线）/"
         f"`≥ {args.strong:g}`（强信号线）；闸门 = `_decide_level` 的"
         "`if not board.candidate: return NONE`。")
    emit()

    days = sorted({str(r["trade_date"]) for r in rows})
    silenced_medium = [r for r in rows
                       if not r["candidate"] and float(r["total"] or 0) >= args.medium]
    silenced_strong = [r for r in silenced_medium
                       if float(r["total"] or 0) >= args.strong]
    live_alerts = [r for r in rows if r["candidate"]
                   and float(r["total"] or 0) >= args.medium]

    emit("## 一、量级")
    emit()
    emit("| 项 | 条数 | 日均 |")
    emit("|---|---:|---:|")
    emit(f"| 现状：候选池内且 `total ≥ {args.medium:g}` | {len(live_alerts)} "
         f"| {len(live_alerts) / len(days):.2f} |")
    emit(f"| **被闸门挡住的**（池外且 `total ≥ {args.medium:g}`）"
         f" | {len(silenced_medium)} | {len(silenced_medium) / len(days):.2f} |")
    emit(f"| 其中够强信号线（≥ {args.strong:g}） | {len(silenced_strong)} "
         f"| {len(silenced_strong) / len(days):.2f} |")
    emit(f"| 线上实际告警（含冷却/确认后） | {len(alert_rows)} "
         f"| {len(alert_rows) / len(days):.2f} |")
    emit()

    # ---------- 收益质量 ----------
    fwd: dict[tuple[str, str], float] = {}
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    price_rows = conn.execute(
        "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
        " JOIN ml_calendar k ON k.trade_date = b.trade_date"
        " ORDER BY b.board_code, b.trade_date").fetchall()
    conn.close()
    series: dict[str, list[tuple[str, float]]] = {}
    for row in price_rows:
        series.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    for code, items in series.items():
        items.sort()
        for index, (day, close) in enumerate(items):
            target = index + HORIZON
            if target < len(items) and close > 0:
                fwd[(day, code)] = items[target][1] / close - 1.0

    def quality(items: list) -> tuple[int, float, float, float]:
        values = np.asarray([fwd.get((str(r["trade_date"]), str(r["board_code"])),
                                     np.nan) for r in items], dtype=float)
        values = values[np.isfinite(values)]
        if not values.size:
            return 0, float("nan"), float("nan"), float("nan")
        return (int(values.size), float((values > BIG).mean()),
                float((values < -BIG).mean()), float(values.mean()))

    emit("## 二、收益质量：被挡住的那批是不是好东西")
    emit()
    emit(f"| 组 | 可比条数 | P(>+{BIG:.0%}) | P(<−{BIG:.0%}) | 均值 "
         f"| 未来 {HORIZON} 日 |")
    emit("|---|---:|---:|---:|---:|---|")
    for label, items in (("候选池内够线（现状）", live_alerts),
                         ("被闸门挡住（够中信号线）", silenced_medium),
                         ("被闸门挡住（够强信号线）", silenced_strong)):
        count, up, down, mean = quality(items)
        emit(f"| {label} | {count} | {up:.1%} | {down:.1%} | {mean:+.2%} | |")
    emit()

    # ---------- 真值事件 ----------
    names: dict[str, str] = {}
    for row in cache.execute("SELECT code, name FROM ml_board"):
        names.setdefault(str(row["name"]), str(row["code"]))
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    index = {day: position for position, day in enumerate(days)}

    def ledger(fired: dict[str, set[str]]) -> dict:
        good = late = early = missed = 0
        detail: list[str] = []
        for event in events:
            date = str(event.get("date") or "").replace("-", "")
            if date not in index:
                continue
            codes = [names.get(str(c), str(c)) for c in (event.get("codes") or [])]
            center = index[date]
            low = center - LOOKBACK_EVENT
            high = min(len(days) - 1, center + LOOKAHEAD_EVENT)
            window = set(days[low:high + 1])
            first = None
            for code in codes:
                for day in fired.get(code, ()):
                    if day in window and (first is None or day < first):
                        first = day
            if first is None:
                missed += 1
                detail.append(f"{date} {str(event.get('label'))[:16]}：漏报")
                continue
            delta = index[first] - center
            if delta < -LEAD_OK:
                early += 1
                verdict = "太早"
            elif delta <= LAG_OK:
                good += 1
                verdict = "合格"
            else:
                late += 1
                verdict = "滞后"
            detail.append(f"{date} {str(event.get('label'))[:16]}："
                          f"{first}（{delta:+d}）{verdict}")
        total = good + late + early + missed
        return {"good": good, "late": late, "early": early, "missed": missed,
                "total": total,
                "rate": (good / total * 100) if total else float("nan"),
                "detail": detail}

    live_fired: dict[str, set[str]] = {}
    for row in alert_rows:
        live_fired.setdefault(str(row["board_code"]), set()).add(
            str(row["trade_date"]))
    extra_fired = {code: set(value) for code, value in live_fired.items()}
    for row in silenced_medium:
        extra_fired.setdefault(str(row["board_code"]), set()).add(
            str(row["trade_date"]))

    base = ledger(live_fired)
    widened = ledger(extra_fired)
    emit("## 三、真值事件对账：放开闸门会怎样")
    emit()
    emit("| 口径 | 合格 | 太早 | 滞后 | 漏报 | 合格率 |")
    emit("|---|---:|---:|---:|---:|---:|")
    emit(f"| 现状（`mainline_alert`） | {base['good']} | {base['early']} "
         f"| {base['late']} | {base['missed']} | {base['rate']:.0f}% |")
    emit(f"| 再并入被挡住的高分板块日 | {widened['good']} | {widened['early']} "
         f"| {widened['late']} | {widened['missed']} | {widened['rate']:.0f}% |")
    gained = [line for line in widened["detail"] if "漏报" not in line
              and line not in base["detail"]]
    lost = [line for line in base["detail"] if "漏报" not in line
            and line not in widened["detail"]]
    emit()
    emit(f"- 新增覆盖：{'；'.join(gained) if gained else '无'}")
    emit(f"- 反而丢失：{'；'.join(lost) if lost else '无'}")
    emit()

    tally: dict[str, int] = {}
    for row in silenced_medium:
        code = str(row["board_code"])
        tally[code] = tally.get(code, 0) + 1
    reverse = {code: name for name, code in names.items()}
    emit("## 四、被挡住的集中在哪些板块（前 15）")
    emit()
    emit("| 板块 | 被挡条数 | 六维均值 | total 均值 |")
    emit("|---|---:|---:|---:|")
    for code, count in sorted(tally.items(), key=lambda kv: -kv[1])[:15]:
        subset = [r for r in silenced_medium if str(r["board_code"]) == code]
        six = np.mean([float(r["six_dim"] or 0) for r in subset])
        tot = np.mean([float(r["total"] or 0) for r in subset])
        emit(f"| {reverse.get(code, code)}（{code}） | {count} | {six:.1f} "
             f"| {tot:.1f} |")
    emit()
    emit("## 五、怎么读")
    emit()
    emit("- 如果「被挡住」那批的 `P(>+10%)` 与 `P(<−10%)` 都和现状同档，"
         "那闸门就是纯粹在丢信号，应当放开（或改成按 `total` 判定候选）；")
    emit("- 如果它们的 `P(<−10%)` 明显更高，那闸门其实在无意中起保护作用，"
         "此时应当改成**只对够强信号线的池外板块**开口子。")

    main_db.close()
    cache.close()
    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
