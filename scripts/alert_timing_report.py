"""告警**时点**报告：真值启动日 vs 实际首次告警日（提前/滞后几个交易日）。

## 为什么需要它

用户报障：**多个板块的告警日滞后真值日很久，基本在板块顶部才报**。
但"滞后"这件事不能靠看几条记录下结论，必须先有一个**统一口径**：

    对每个真值事件 → 取该板块在 [真值 − 前瞻窗口, 真值 + 回看窗口] 内的
    **首次告警** → 算 `告警日 − 真值日` 的**交易日**差 → 分档

分档口径（用户给的验收标准）：

    lead  ≤ 10 个交易日（提前 2 周内）  → ✅ 合格（提前）
    −5 ≤ d ≤ 0（滞后 1 周内）           → ✅ 合格（准时/略迟）
    d > +5                              → ⚠️ 滞后
    d < −10                             → ⚠️ 太早（可能是假启动）
    窗口内没有告警                       → ❌ 漏报

## 两个必须写清楚的口径细节

1. **板块代码优先**：真值文件里 `codes` 可能写着板块**名字**（如"农业种植"），
   而池内名字会重名 —— 这里用 `ml_board` 反查，反查不到就标"代码待补"，
   不猜。
2. **只统计 `status: ok` 的事件**。`proxy`（代理板块）与 `needs_review`
   （代码留空）两类**单独列出**，不混进总体 —— 拿代理板块算出来的"命中"
   不是模型命中了真值，而是代理恰好涨了。

## 只读、不写库

用法：
    .venv\\Scripts\\python.exe scripts\\alert_timing_report.py
    .venv\\Scripts\\python.exe scripts\\alert_timing_report.py \\
        --table mainline_alert_bak_20260921_2015 --label V2.2
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
#: 首次告警的搜索窗口（交易日）：真值日起往后看这么多天
LOOKAHEAD = 40
#: 往前看这么多天（允许"提前启动"的告警落在真值日之前）
LOOKBACK = 20
#: 分档阈值（交易日）
LEAD_OK = 10      # 提前 ≤2 周算合格
LAG_OK = 5        # 滞后 ≤1 周算合格


def trading_days(table: str) -> list[str]:
    """交易日序列（从评分表取，保证与告警表同源）。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    rows = conn.execute(
        f"SELECT DISTINCT trade_date FROM {table} ORDER BY trade_date").fetchall()
    conn.close()
    return [str(r[0]) for r in rows]


def board_name_map() -> dict[str, str]:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    for row in conn.execute("SELECT code, name FROM ml_board"):
        out.setdefault(str(row["name"]), str(row["code"]))
    conn.close()
    return out


def alerts(table: str) -> dict[str, list[tuple[str, str, float]]]:
    """`{board_code: [(日期, 等级, 分数)]}`（升序）。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: dict[str, list[tuple[str, str, float]]] = {}
    for row in conn.execute(
            f"SELECT board_code, trade_date, level, score FROM {table}"
            " ORDER BY trade_date"):
        out.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), str(row["level"]),
             float(row["score"] or 0.0)))
    conn.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="告警时点报告")
    parser.add_argument("--table", default="mainline_alert")
    parser.add_argument("--label", default="当前")
    parser.add_argument("--levels", default="medium,strong",
                        help="算作『主线告警』的等级（逗号分隔）。"
                             "默认排除 `weak` —— weak 是『单一维度极端异常』，"
                             "噪声很大；把它算进去会让『首次告警』大幅提前，"
                             "掩盖真正关心的中/强信号时点。要全算传 `all`")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    wanted = None if args.levels.strip() == "all" else {
        item.strip() for item in args.levels.split(",") if item.strip()}

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    days = trading_days("mainline_score")
    index = {day: position for position, day in enumerate(days)}
    names = board_name_map()
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = truth.get("events") or []
    book = alerts(args.table)

    emit(f"# 告警时点报告（口径：{args.label} / 表 `{args.table}`）")
    emit()
    emit("> 由 `scripts/alert_timing_report.py` 生成。")
    emit(f"> 搜索窗口：真值日 **−{LOOKBACK} ~ +{LOOKAHEAD}** 个交易日内的"
         f"**首次告警**。")
    emit(f"> 计入的等级：**{args.levels}**"
         + ("（`weak` 是『单一维度极端异常』、噪声大，默认不计）"
            if wanted is not None else "（全部等级）"))
    emit(f"> 分档：提前 ≤{LEAD_OK} 日 = ✅合格；滞后 ≤{LAG_OK} 日 = ✅合格；"
         f"滞后 >{LAG_OK} 日 = ⚠️滞后；提前 >{LEAD_OK} 日 = ⚠️太早；"
         "窗口内无告警 = ❌漏报。")
    emit()

    rows: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        date = str(event.get("date") or "").replace("-", "")
        codes = event.get("codes") or []
        resolved: list[str] = []
        unresolved: list[str] = []
        for code in codes:
            text = str(code)
            if text in names:
                resolved.append(names[text])
            elif text.endswith(".TI") or text.isdigit():
                resolved.append(text)
            else:
                unresolved.append(text)
        status = str(event.get("status") or "")
        label = str(event.get("label") or "")
        if not date or date not in index:
            rows.append({"date": date, "label": label, "status": status,
                         "note": "真值日不在交易日历内", "verdict": "❓"})
            continue
        if not resolved:
            rows.append({"date": date, "label": label, "status": status,
                         "note": f"板块代码待补（{'、'.join(unresolved)}）",
                         "verdict": "❓"})
            continue
        center = index[date]
        low = max(0, center - LOOKBACK)
        high = min(len(days) - 1, center + LOOKAHEAD)
        window = set(days[low:high + 1])
        found: tuple[str, str, float] | None = None
        for code in resolved:
            for day, level, score in book.get(code, []):
                if wanted is not None and level not in wanted:
                    continue
                if day in window and (found is None or day < found[0]):
                    found = (day, level, score)
        if found is None:
            rows.append({"date": date, "label": label, "status": status,
                         "codes": resolved, "note": "窗口内无告警",
                         "verdict": "❌漏报"})
            continue
        delta = index.get(found[0], center) - center
        if delta < -LEAD_OK:
            verdict = "⚠️太早"
        elif delta <= LAG_OK:
            verdict = "✅合格"
        else:
            verdict = "⚠️滞后"
        rows.append({"date": date, "label": label, "status": status,
                     "codes": resolved, "alert": found[0], "level": found[1],
                     "delta": delta, "verdict": verdict})

    emit("## 一、逐事件（只有 `status: ok` 的进统计）")
    emit()
    emit("| 真值日 | 标签 | 板块 | 首次告警 | 等级 | 差(交易日) | 判读 |")
    emit("|---|---|---|---|---|---:|---|")
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    for item in rows:
        codes = "、".join(item.get("codes") or []) or item.get("note", "")
        delta = item.get("delta")
        if isinstance(delta, int):
            emit(f"| {item['date']} | {item['label'][:18]} | {codes} "
                 f"| {item['alert']} | {item['level']} | {delta:+d} "
                 f"| {item['verdict']} |")
        else:
            emit(f"| {item['date']} | {item['label'][:18]} | {codes} "
                 f"| — | — | — | {item['verdict']} |")
    emit()

    emit("## 二、汇总（只计 `status: ok`）")
    emit()
    total = len(ok_rows)
    good = sum(1 for r in ok_rows if r["verdict"] == "✅合格")
    late = sum(1 for r in ok_rows if r["verdict"] == "⚠️滞后")
    early = sum(1 for r in ok_rows if r["verdict"] == "⚠️太早")
    missed = sum(1 for r in ok_rows if r["verdict"] == "❌漏报")
    pending = sum(1 for r in ok_rows if r["verdict"] == "❓")
    deltas = [r["delta"] for r in ok_rows if isinstance(r.get("delta"), int)]
    emit(f"- `status: ok` 事件 **{total}** 个：✅合格 **{good}**、"
         f"⚠️滞后 {late}、⚠️太早 {early}、❌漏报 {missed}、❓待补 {pending}")
    if total:
        emit(f"- **合格率 {good / total * 100:.0f}%**")
    if deltas:
        ordered = sorted(deltas)
        middle = ordered[len(ordered) // 2]
        emit(f"- 差值中位 **{middle:+d}** 个交易日，"
             f"范围 {min(deltas):+d} ~ {max(deltas):+d}")
    emit()
    emit("### 未进统计的事件（另列，不混算）")
    emit()
    emit("| 真值日 | 标签 | status | 说明 |")
    emit("|---|---|---|---|")
    for item in rows:
        if item.get("status") != "ok":
            emit(f"| {item['date']} | {item['label'][:22]} | {item['status']} "
                 f"| {item.get('note', item.get('verdict', ''))} |")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
