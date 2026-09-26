"""把 `mainline_alert_exclusions.yaml` 的规则**套到历史告警上**，看效果。

## 为什么必须做这一步

规则写进配置不等于有效果。这份脚本用**同一份 YAML**（不是另抄一遍参数）
把规则套到历史告警表上，回答三件事：

1. **删掉多少条告警**、各板块各删多少；
2. **告警质量变了多少**：P(>+10%) / P(<−10%) / 均值 / 无行情率；
3. **真值事件台账有没有变差** —— 这才是代价。任何"精度提升"只要让漏报增加，
   按本项目一贯口径就是**不划算**。

⚠️ 读的是**备份表**（重打分进行中，`mainline_alert` 是残表）。

用法：
    .venv\\Scripts\\python.exe scripts/alert_scope_effect.py \\
        --out docs/MAINLINE_SCOPE_EFFECT.md
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

from src.mainline.config import _load_alert_scope  # noqa: E402

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
SCOPE = ROOT / "configs" / "mainline_alert_exclusions.yaml"
LEAD_OK, LAG_OK = 10, 5
BIG = 0.15
HORIZON = 20


def main() -> int:
    parser = argparse.ArgumentParser(description="告警范围规则的效果")
    parser.add_argument("--alert-table", default="mainline_alert_bak_v26_prefloor")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    scope = _load_alert_scope(str(SCOPE))
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = [dict(row) for row in conn.execute(
        f"SELECT trade_date, board_code, board_name, level, entry_close, score"
        f" FROM {args.alert_table}")]
    conn.close()
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
        advance = np.zeros(len(close), dtype=bool)
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            advance |= np.isfinite(rolling) & (rolling > BIG)
        prepared[code] = {"index": {d: i for i, d in enumerate(day_list)},
                          "close": close, "advance": advance, "days": day_list}

    kept: list[dict] = []
    dropped: dict[str, list[dict]] = {}
    for row in alerts:
        mode, why = scope.restriction(str(row["board_code"]),
                                      str(row["trade_date"]))
        # 判定统一走 `permits()`，不在脚本里重复模式字符串比对
        if not scope.permits(str(row["board_code"]), str(row["trade_date"]),
                             str(row["level"]), row.get("score")):
            dropped.setdefault(str(row["board_code"]), []).append(
                {**row, "mode": mode, "why": why})
            continue
        kept.append(row)

    def quality(rows: list[dict]) -> tuple[int, float, float, float, float]:
        fwd, nomove = [], 0
        for row in rows:
            item = prepared.get(str(row["board_code"]))
            if item is None:
                continue
            pos = item["index"].get(str(row["trade_date"]))
            if pos is None:
                continue
            entry = float(row["entry_close"] or 0.0) or float(item["close"][pos])
            if pos + HORIZON < len(item["close"]) and entry > 0:
                fwd.append(item["close"][pos + HORIZON] / entry - 1.0)
            if not item["advance"][pos: pos + 21].any():
                nomove += 1
        if not fwd:
            return len(rows), float("nan"), float("nan"), float("nan"), float("nan")
        arr = np.asarray(fwd)
        return (len(rows), float((arr > 0.10).mean()),
                float((arr < -0.10).mean()), float(arr.mean()),
                nomove / max(len(rows), 1))

    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    all_days = sorted({str(r["trade_date"]) for r in alerts}
                      | {d for item in prepared.values() for d in item["days"]})
    day_index = {d: i for i, d in enumerate(all_days)}
    reverse = {name: code for code, name in names.items()}

    def ledger(rows: list[dict]) -> tuple[int, int, int, int, list[str]]:
        fired: dict[str, set[str]] = {}
        for row in rows:
            fired.setdefault(str(row["board_code"]), set()).add(
                str(row["trade_date"]))
        good = late = early = missed = 0
        lost: list[str] = []
        for event in events:
            date = str(event.get("date") or "").replace("-", "")
            if date not in day_index:
                continue
            codes = [reverse.get(str(c), str(c))
                     for c in (event.get("codes") or [])]
            center = day_index[date]
            window = set(all_days[max(0, center - 20):
                                  min(len(all_days) - 1, center + 40) + 1])
            first = None
            for code in codes:
                for day in fired.get(code, ()):
                    if day in window and (first is None or day < first):
                        first = day
            if first is None:
                missed += 1
                lost.append(f"{date} {str(event.get('label'))[:16]}")
                continue
            delta = day_index[first] - center
            if delta < -LEAD_OK:
                early += 1
            elif delta <= LAG_OK:
                good += 1
            else:
                late += 1
        return good, early, late, missed, lost

    before = quality(alerts)
    after = quality(kept)
    before_ledger = ledger(alerts)
    after_ledger = ledger(kept)

    emit("# 告警范围规则的效果（套到历史告警上）")
    emit()
    emit(f"> 由 `scripts/alert_scope_effect.py` 生成（只读，告警表 "
         f"`{args.alert_table}`，规则文件 `{SCOPE.name}`）。")
    emit(f"> {scope.load_note}")
    emit()
    emit("## 一、规则清单")
    emit()
    emit("| 类型 | 板块 | 允许告警窗口（含起点前 14 天） | 理由 |")
    emit("|---|---|---|---|")

    def spans_text(spans) -> str:
        """把 `((起, 止), ...)` 写成人能读的一段。

        ⚠️ 别直接把元组塞进 f-string：`('02-01', '02-28')月` 这种输出
        在报告里完全没法看。
        """
        parts = []
        for start, end in spans:
            parts.append(f"{start} ~ {end}")
        return "、".join(parts)

    for code, why in scope.excluded.items():
        emit(f"| 完全关闭 | {names.get(code, code)}（{code}） | 全年 | {why[:46]} |")
    for code, spans in scope.seasonal.items():
        emit(f"| 季节性关闭 | {names.get(code, code)}（{code}） "
             f"| 仅 {spans_text(spans)} | 其余时间不发 |")
    for code, spans in scope.strong_only.items():
        emit(f"| 区间外只发 strong | {names.get(code, code)}（{code}） "
             f"| {spans_text(spans)} | 其余时间只放行 strong |")
    emit()

    emit("## 二、删掉了多少")
    emit()
    emit("| 板块 | 删掉条数 | 其中 strong | 该板块原有 |")
    emit("|---|---:|---:|---:|")
    for code, rows in sorted(dropped.items(), key=lambda kv: -len(kv[1])):
        strong = sum(1 for r in rows if str(r["level"]) == "strong")
        total = sum(1 for r in alerts if str(r["board_code"]) == code)
        emit(f"| {names.get(code, code)}（{code}） | {len(rows)} | {strong} "
             f"| {total} |")
    emit(f"| **合计** | **{len(alerts) - len(kept)}** "
         f"| {sum(1 for r in dropped.values() for r in r if str(r['level']) == 'strong')} "
         f"| {len(alerts)} |")
    emit()

    emit("## 三、告警质量变化")
    emit()
    emit("| 口径 | 告警条数 | P(>+10%) | P(<−10%) | 均值20日 | 无行情率 |")
    emit("|---|---:|---:|---:|---:|---:|")
    for label, stat in (("规则前", before), ("规则后", after)):
        emit(f"| {label} | {stat[0]} | {stat[1]:.1%} | {stat[2]:.1%} "
             f"| {stat[3]:+.2%} | {stat[4]:.1%} |")
    emit(f"| 变化 | {after[0] - before[0]} | {after[1] - before[1]:+.1%} "
         f"| {after[2] - before[2]:+.1%} | {after[3] - before[3]:+.2%} "
         f"| {after[4] - before[4]:+.1%} |")
    emit()

    emit("## 四、真值事件台账（这是代价）")
    emit()
    emit("| 口径 | 合格 | 太早 | 滞后 | 漏报 |")
    emit("|---|---:|---:|---:|---:|")
    emit(f"| 规则前 | {before_ledger[0]} | {before_ledger[1]} "
         f"| {before_ledger[2]} | {before_ledger[3]} |")
    emit(f"| 规则后 | {after_ledger[0]} | {after_ledger[1]} "
         f"| {after_ledger[2]} | {after_ledger[3]} |")
    emit()
    new_lost = sorted(set(after_ledger[4]) - set(before_ledger[4]))
    if new_lost:
        emit(f"⚠️ **新增漏报 {len(new_lost)} 个**：{'；'.join(new_lost)}")
        emit("→ 规则踩掉了真行情，需要缩小名单或把该板块从 `seasonal` 改成 "
             "`strong_only`。")
    else:
        emit("✅ **零新增漏报** —— 规则只删掉了没有覆盖真值事件的告警。")
    emit()
    emit("## 五、怎么读")
    emit()
    emit("判据顺序：**先看漏报有没有增加**，再看质量有没有改善。"
         "这批规则的定位是**降噪（少打扰）**，不是提精度 —— "
         "被删掉的条数只占总量百分之几，别指望它改变整体命中率。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
