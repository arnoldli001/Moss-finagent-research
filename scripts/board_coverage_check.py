"""用户点名的那几个板块，到底有没有被覆盖到？

## 用户原话

「观测到煤炭概念 885914、石油石化、金属铜 885973、黄金概念 885530
在回测时反馈板块**不在池内**，漏掉这几个板块了吗，如果有遗漏，
帮我补充这几个板块，并提纯成分股。」

这是可以直接查表回答的问题，不该靠推理。本脚本对每个点名板块给出：

1. 它在 `ml_board` 里存不存在（**石油石化** 是申万一级行业，
   而 `ml_board` 只有 885xxx/886xxx 概念与行业指数，要单独说明）；
2. 有多少个交易日被打分、有多少天进过候选池（`candidate=1`）；
3. 有多少条线上告警（`mainline_alert`）；
4. 纯化后成分股数量（`ml_member_pure` 里 `relevant=1`）；
5. 与真值事件的对应关系。

另外把 47 个真值事件里**系统没抓到**的列出来，这是"漏报"的准确清单。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/board_coverage_check.py \\
        --out docs/MAINLINE_BOARD_COVERAGE.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LEAD_OK, LAG_OK = 10, 5
LOOKBACK_EVENT, LOOKAHEAD_EVENT = 20, 40

#: 用户点名的板块（代码 + 用户用的名字）
WATCHED = (
    ("885914.TI", "煤炭概念"),
    ("885973.TI", "金属铜"),
    ("885530.TI", "黄金概念"),
    ("885812.TI", "农业种植"),
    ("885343.TI", "稀土永磁"),
    ("886015.TI", "创新药"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="点名板块覆盖核查")
    parser.add_argument("--score-table", default="mainline_score")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    main = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    main.row_factory = sqlite3.Row

    boards = {str(r["code"]): (str(r["name"]), str(r["kind"]))
              for r in cache.execute("SELECT code, name, kind FROM ml_board")}
    kinds: dict[str, int] = {}
    for _, kind in boards.values():
        kinds[kind] = kinds.get(kind, 0) + 1

    emit("# 点名板块覆盖核查")
    emit()
    emit("> 由 `scripts/board_coverage_check.py` 生成，只读。")
    emit(f"> 板块池 `ml_board` 共 {len(boards)} 个（"
         + "、".join(f"{k} {v}" for k, v in sorted(kinds.items())) + "）。")
    emit()

    emit("## 一、点名板块")
    emit()
    emit("| 板块 | 代码 | 在池内 | 被打分天数 | 进候选池天数 | 进候选占比 "
         "| 线上告警 | 纯化成分股 |")
    emit("|---|---|---|---:|---:|---:|---:|---:|")
    for code, label in WATCHED:
        info = boards.get(code)
        name = info[0] if info else label
        scored = main.execute(
            f"SELECT COUNT(*) n, SUM(candidate) c FROM {args.score_table}"
            " WHERE board_code=?", (code,)).fetchone()
        alerts = main.execute(
            f"SELECT COUNT(*) n FROM {args.alert_table} WHERE board_code=?",
            (code,)).fetchone()["n"]
        pure = cache.execute(
            "SELECT COUNT(*) n FROM ml_member_pure"
            " WHERE board_code=? AND relevant=1", (code,)).fetchone()["n"]
        if not info:
            emit(f"| {label} | `{code}` | **否** | — | — | — | — | — |")
            continue
        total = int(scored["n"] or 0)
        cand = int(scored["c"] or 0)
        share = f"{cand / total:.0%}" if total else "—"
        emit(f"| {name} | `{code}` | 是 | {total} | **{cand}** | {share} "
             f"| {int(alerts)} | {int(pure)} |")
    emit()
    emit("⚠️ **石油石化不在 `ml_board` 里**：它是申万一级行业，而本项目的板块池"
         "只有 885xxx/886xxx 的东财概念与行业指数。真值事件里"
         "「有色金属/石油石化」映射到的是 `885530`（黄金概念）与 `885973`"
         "（金属铜）这类**同源概念**，不是申万一级。"
         "要真正覆盖申万一级，需要扩数据源（`sw_daily` 已在 `sources.py` 里，"
         "但 `ml_board` 没有导入），这是另一个独立的改动。")
    emit()

    # ---------- 漏报清单 ----------
    alert_days: dict[str, set[str]] = {}
    for row in main.execute(
            f"SELECT trade_date, board_code FROM {args.alert_table}"):
        alert_days.setdefault(str(row["board_code"]), set()).add(
            str(row["trade_date"]))
    scored_days = sorted({str(r[0]) for r in main.execute(
        f"SELECT DISTINCT trade_date FROM {args.score_table}")})
    index = {day: position for position, day in enumerate(scored_days)}

    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    emit(f"## 二、{len(events)} 个真值事件的逐条对账")
    emit()
    emit("| 真值日 | 标签 | 首次告警日 | 差值（交易日） | 判定 |")
    emit("|---|---|---|---:|---|")
    missed: list[str] = []
    buckets = {"合格": 0, "太早": 0, "滞后": 0, "漏报": 0}
    for event in events:
        date = str(event.get("date") or "").replace("-", "")
        label = str(event.get("label") or date)[:22]
        if date not in index:
            emit(f"| {date} | {label} | — | — | 窗口外 |")
            continue
        center = index[date]
        low = center - LOOKBACK_EVENT
        high = min(len(scored_days) - 1, center + LOOKAHEAD_EVENT)
        window = set(scored_days[low:high + 1])
        first = None
        for code in (event.get("codes") or []):
            for day in alert_days.get(str(code), ()):
                if day in window and (first is None or day < first):
                    first = day
        if first is None:
            buckets["漏报"] += 1
            missed.append(f"{date} {label}")
            emit(f"| {date} | {label} | — | — | **漏报** |")
            continue
        delta = index[first] - center
        if delta < -LEAD_OK:
            verdict = "太早"
        elif delta <= LAG_OK:
            verdict = "合格"
        else:
            verdict = "滞后"
        buckets[verdict] += 1
        emit(f"| {date} | {label} | {first} | {delta:+d} | {verdict} |")
    emit()
    emit("## 三、汇总")
    emit()
    emit(f"- 合格 {buckets['合格']} / 太早 {buckets['太早']} / "
         f"滞后 {buckets['滞后']} / **漏报 {buckets['漏报']}**，"
         f"共 {sum(buckets.values())} 条（窗口内的）")
    if missed:
        emit("- 漏报清单：" + "；".join(missed))
    else:
        emit("- **没有漏报**：所有落在打分窗口内的真值事件都至少报出过一次。")
    emit()
    emit("⚠️ 「合格」的口径是**提前 ≤10 或滞后 ≤5 个交易日**。"
         "用户的表述是「不要超过启动日一周，可以提前 1-2 周」，"
         "所以「太早」那几行属于**在用户容忍范围附近或之外**，"
         "是时点问题、不是覆盖问题。")

    cache.close()
    main.close()
    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
