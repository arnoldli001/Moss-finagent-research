"""逐事件验证：换 `lookback_days` 能不能救回"差一点"的事件。

## 要回答的问题

漏报诊断把 15 个真值事件分成三类（`scripts/missed_events_report.py`）：

    ✅抓到 7 个 / never_candidate 4 个 / 阈值·突破没触发 4 个

后 4 个是**进了候选池但没越线**（最高 total 只有 57~74）。用户的直觉是
"把回看天数从 120 降到 30 就能越过更低的自身上沿"。这可以**离线逐事件验证**：

    对每个事件、每个 lookback：
        窗口内是否存在某天   total > ceiling(该板块自己过去 lookback 天的分位)
                            且 total ≥ floor

若某事件在 lookback=30 下能触发、在 120 下不能 → 用户的直觉成立。

## 口径

- **严格 PIT**：ceiling 只用当日之前的数据；
- `min_samples` 必须 ≤ lookback，否则整条路径不触发（实测过这个坑）；
- 只对**候选池内**的日子判定（与线上一致）。

只读、不写库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.scoring import breakout_ceiling, is_breakout  # noqa: E402

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LOOKBACK, LOOKAHEAD = 20, 40


def main() -> int:
    parser = argparse.ArgumentParser(description="逐事件的 lookback 验证")
    parser.add_argument("--table", default="mainline_score")
    parser.add_argument("--lookbacks", default="20,30,45,60,90,120")
    parser.add_argument("--quantile", type=float, default=0.90)
    parser.add_argument("--fresh-days", type=int, default=10)
    parser.add_argument("--floor", type=float, default=50.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    lookbacks = [int(x) for x in args.lookbacks.split(",") if x.strip()]

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    days = [str(r[0]) for r in conn.execute(
        f"SELECT DISTINCT trade_date FROM {args.table} ORDER BY trade_date")]
    index = {day: position for position, day in enumerate(days)}

    # 全部候选日的 (板块 → [(日, total)])，一次读入后面反复用
    series: dict[str, list[tuple[str, float]]] = {}
    for row in conn.execute(
            f"SELECT trade_date, board_code, total, payload FROM {args.table}"
            " ORDER BY trade_date"):
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        # 保留全部候选日（含窗口外的），因为 ceiling 需要窗口之前的连续历史
        series.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["total"] or 0.0)))
    conn.close()

    names: dict[str, str] = {}
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    for row in cache.execute("SELECT code, name FROM ml_board"):
        names.setdefault(str(row["name"]), str(row["code"]))
    cache.close()

    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]

    emit("# 逐事件：换 `lookback_days` 能不能救回「差一点」的事件")
    emit()
    emit(f"> 由 `scripts/lookback_event_check.py` 生成，表 `{args.table}`；"
         f"分位 {args.quantile:g}、新鲜度 {args.fresh_days}、下限 {args.floor:g}。")
    emit("> 判定：窗口内某天 `total > 自身过去 lookback 天的分位` 且 "
         "`total ≥ 下限`。")
    emit()
    emit("| 真值日 | 标签 | 窗口内进池天 | 最高 total | "
         + " | ".join(f"lb{lb}" for lb in lookbacks) + " |")
    emit("|---|---|---:|---:|" + "---:|" * len(lookbacks))

    catch: dict[int, int] = {lb: 0 for lb in lookbacks}
    evaluated = 0
    for event in events:
        date = str(event.get("date") or "").replace("-", "")
        if date not in index:
            continue
        codes: list[str] = []
        for code in event.get("codes") or []:
            text = str(code)
            codes.append(names.get(text, text))
        codes = [c for c in codes if c in series]
        if not codes:
            continue
        evaluated += 1
        center = index[date]
        window = set(days[max(0, center - LOOKBACK):
                          min(len(days) - 1, center + LOOKAHEAD) + 1])
        in_window = 0
        best = -1.0
        for code in codes:
            for day, total in series.get(code, []):
                if day in window:
                    in_window += 1
                    best = max(best, total)
        marks: list[str] = []
        for lb in lookbacks:
            fired = False
            for code in codes:
                items = series.get(code, [])
                for position, (day, total) in enumerate(items):
                    if day not in window:
                        continue
                    history = [value for _, value in
                               items[max(0, position - lb):position]]
                    ceiling = breakout_ceiling(
                        history, quantile=args.quantile,
                        min_samples=min(lb, 20))
                    if is_breakout(total, ceiling, min_score=args.floor,
                                   recent=history,
                                   fresh_days=args.fresh_days):
                        fired = True
                        break
                if fired:
                    break
            marks.append("✅" if fired else "—")
            catch[lb] += int(fired)
        emit(f"| {date} | {str(event.get('label'))[:16]} | {in_window} "
             f"| {best:.1f} | " + " | ".join(marks) + " |")
    emit()
    emit("## 各 lookback 的事件覆盖")
    emit()
    emit("| lookback | 触发事件数 | 占可评估事件 |")
    emit("|---:|---:|---:|")
    for lb in lookbacks:
        pct = catch[lb] / evaluated * 100 if evaluated else float("nan")
        emit(f"| {lb} | {catch[lb]} | {pct:.0f}%（共 {evaluated} 个） |")
    emit()
    emit("**注**：这里只问「突破路径能不能触发」，不含绝对阈值路径"
         "（`total ≥ medium_score`）与确认/冷却。所以它是**上界**，"
         "实际告警数会略少。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
