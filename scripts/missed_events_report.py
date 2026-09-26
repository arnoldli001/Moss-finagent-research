"""漏报诊断：真值事件为什么**一条告警都没有**。

## 为什么这个比调门槛重要

实测（`scripts/precision_recall_curve.py`）：**每降 1pp 误报要丢 1.2~3.3pp 真值**，
门槛这个旋钮已经用尽。而真值事件里**有 11~12 个（共 15 个）窗口内
完全没有中/强信号** —— 这才是主要矛盾。所以必须逐个查清：**卡在哪一环？**

四种可能，处置完全不同：

| 卡点 | 含义 | 该改哪里 |
|---|---|---|
| `never_candidate` | 窗口内从没进过候选池 | 第一层（六维）**选池**能力 |
| `low_score` | 进过池，但 `total` 始终低于门限且无突破 | 分数/门限 |
| `breakout_not_fired` | 分数够或不低，但没越过自己的上沿 | 突破参数（回看/分位/新鲜度） |
| `suppressed` | 触发了但被确认/冷却抑制掉 | 告警状态机 |

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts\\missed_events_report.py
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

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LOOKBACK, LOOKAHEAD = 20, 40


def main() -> int:
    parser = argparse.ArgumentParser(description="漏报事件诊断")
    parser.add_argument("--score-table", default="mainline_score")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    # 交易日序列
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    days = [str(r[0]) for r in conn.execute(
        f"SELECT DISTINCT trade_date FROM {args.score_table}"
        " ORDER BY trade_date")]
    index = {day: position for position, day in enumerate(days)}

    # 板块名 → 代码
    names: dict[str, str] = {}
    in_pool: set[str] = set()
    conn2 = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn2.row_factory = sqlite3.Row
    for row in conn2.execute("SELECT code, name FROM ml_board"):
        names.setdefault(str(row["name"]), str(row["code"]))
        in_pool.add(str(row["code"]))
    conn2.close()

    # 该事件涉及板块的全部评分行
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]

    emit("# 漏报诊断：真值事件卡在哪一环")
    emit()
    emit(f"> 由 `scripts/missed_events_report.py` 生成，评分表 "
         f"`{args.score_table}`、告警表 `{args.alert_table}`。")
    emit(f"> 窗口 = 真值日 −{LOOKBACK} ~ +{LOOKAHEAD} 个交易日。")
    emit()

    emit("| 真值日 | 标签 | 板块 | 窗口内进池天数 | 最高 total | 最好排名 "
         "| 最高 total 日 | 告警 | 卡点 |")
    emit("|---|---|---|---:|---:|---:|---|---|---|")
    summary: dict[str, int] = {}
    for event in events:
        date = str(event.get("date") or "").replace("-", "")
        label = str(event.get("label") or "")[:16]
        if date not in index:
            continue
        codes: list[str] = []
        for code in event.get("codes") or []:
            text = str(code)
            codes.append(names.get(text, text))
        codes = [c for c in codes if c.endswith(".TI") or c.isdigit()]
        if not codes:
            emit(f"| {date} | {label} | （代码待补） | — | — | — | — | — | ❓ |")
            continue
        center = index[date]
        low = max(0, center - LOOKBACK)
        high = min(len(days) - 1, center + LOOKAHEAD)
        window = days[low:high + 1]
        marks = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT trade_date, board_code, total, payload FROM "
            f"{args.score_table} WHERE board_code IN ({marks})"
            " AND trade_date BETWEEN ? AND ?", (*codes, window[0], window[-1])
        ).fetchall()
        cand_days = 0
        best_total = -1.0
        best_day = ""
        best_rank: int | None = None
        for row in rows:
            try:
                payload = json.loads(str(row["payload"] or "{}"))
            except ValueError:
                continue
            if not payload.get("candidate"):
                continue
            cand_days += 1
            total = float(row["total"] or 0.0)
            if total > best_total:
                best_total = total
                best_day = str(row["trade_date"])
                rank = payload.get("rank")
                best_rank = int(rank) if isinstance(rank, int) else None
        alerts = conn.execute(
            f"SELECT level, COUNT(*) n FROM {args.alert_table}"
            f" WHERE board_code IN ({marks})"
            " AND trade_date BETWEEN ? AND ?"
            " GROUP BY level", (*codes, window[0], window[-1])).fetchall()
        by_level = {str(r["level"]): int(r["n"]) for r in alerts}
        strong_medium = sum(by_level.get(k, 0)
                            for k in ("medium", "strong"))
        total_alerts = sum(by_level.values())
        # 分类：**只把 medium/strong 算"抓到"** ——
        # `weak` 是"单一维度极端异常"，噪声大，时点报告也不计它。
        # 第一版把 `weak` 也算进"有告警"，于是把"从没进池"的事件误判成
        # "已抓到"，卡点汇总整个失真。
        if strong_medium:
            where = f"✅抓到（中/强 {strong_medium} 条）"
        elif not (set(codes) & in_pool):
            # 🩸 真值映射到**不在板块池里**的代码 → 这条事件永远不可能被抓到，
            # 与模型能力无关，是**真值/池口径不匹配**。
            # 必须单独归类，否则会被算成"模型漏报"，把结论引向错误的方向。
            where = "**out_of_pool**（真值代码不在板块池）"
        elif cand_days == 0:
            where = "**never_candidate**（窗口内从没进池）"
        elif best_total < 50.0:
            where = "**low_score**（进池但分低）"
        else:
            where = "breakout_not_fired / 门限"
        key = where.split("（")[0].replace("**", "")
        summary[key] = summary.get(key, 0) + 1
        detail = (f"中/强 {strong_medium}、weak {by_level.get('weak', 0)}"
                  if total_alerts else "0")
        pool_mark = "" if (set(codes) & in_pool) else " ⚠️不在池"
        emit(f"| {date} | {label} | {'、'.join(codes)}{pool_mark} | {cand_days} "
             f"| {best_total:.1f} | {best_rank if best_rank is not None else '—'} "
             f"| {best_day or '—'} | {detail} | {where} |")
    conn.close()
    emit()
    emit("## 卡点汇总")
    emit()
    for key, count in sorted(summary.items(), key=lambda kv: -kv[1]):
        emit(f"- `{key}`：**{count}** 个事件")
    emit()
    emit("**读法**：`never_candidate` 占多数 → 要改**第一层选池**（六维），"
         "而不是告警门限；`breakout_not_fired` 占多数 → 调突破参数"
         "（回看天数/分位/新鲜度）才有效。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
