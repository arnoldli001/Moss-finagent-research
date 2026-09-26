"""季节性限制的**效果评估**：只保留某几个月的告警，代价与收益各是多少。

## 为什么要单独算

用户给的月份是**领域知识**（「旅游 1/3/4/8/9 月」「电力 2-7 月」），
而 3.7 年的样本里两个板块的月度启动分布都**不显著**（置换检验 p=0.47 / 0.88）。
所以不能只按"猜的月份"关掉告警 —— 必须把**每个候选月份集合**的效果摊开：

    保留哪几个月 → 剩多少条告警 / 命中率变化 / 均值变化 / 有没有踩掉真值事件

同时给出**数据给出的月份集合**作为对照（按各月"告警后 20 日均值"排序取前 N 个），
让用户能看到"按数据选"和"按经验选"差多少。

## 口径

- 基础告警集 = 全量告警（可按 `--level` 限定档位）；
- 对每个候选月份集合：把不在集合内的月份的**该板块告警**删掉，重算整体质量；
- 真值事件台账：看有没有事件的首报因此丢失。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/seasonal_effect_report.py \\
        --out docs/MAINLINE_SEASONAL_EFFECT.md
"""

from __future__ import annotations

import argparse
import json
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
BIG = 0.15
HORIZON = 20
MONTH_LABEL = ("1月", "2月", "3月", "4月", "5月", "6月",
               "7月", "8月", "9月", "10月", "11月", "12月")

#: 待评估的候选月份集合（用户给的 + 数据给的）
CASES = {
    "885497.TI": {
        "用户给的：1/3/4/8/9月": (1, 3, 4, 8, 9),
        "去掉最差的1月：3/4/8/9月": (3, 4, 8, 9),
        "仅数据里出过启动的月：3/4/5/9/11/12月": (3, 4, 5, 9, 11, 12),
        "数据里告警后均值为正的月：2/3/4/12月": (2, 3, 4, 12),
    },
    "885936.TI": {
        "用户猜的：2-7月": (2, 3, 4, 5, 6, 7),
        "数据里出过启动的月：2/3/4/9/10/12月": (2, 3, 4, 9, 10, 12),
        "数据里告警后均值为正的月：2/4/7/10/11月": (2, 4, 7, 10, 11),
        "只留 9-12月（数据里的启动密集区）": (9, 10, 11, 12),
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="季节性限制效果评估")
    parser.add_argument("--alert-table", default="mainline_alert_bak_v26_prefloor")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = [dict(row) for row in conn.execute(
        f"SELECT trade_date, board_code, board_name, level, entry_close"
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
        close = np.asarray([c for _, c in items])
        day_list = [d for d, _ in items]
        advance = np.zeros(len(close), dtype=bool)
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            advance |= np.isfinite(rolling) & (rolling > BIG)
        prepared[code] = {"index": {d: i for i, d in enumerate(day_list)},
                          "close": close, "advance": advance, "days": day_list}

    def quality(rows: list[dict]) -> tuple[int, float, float, float, float]:
        fwd, nomove = [], 0
        for row in rows:
            item = prepared.get(str(row["board_code"]))
            if item is None:
                continue
            pos = item["index"].get(str(row["trade_date"]))
            if pos is None:
                continue
            if pos + HORIZON < len(item["close"]):
                fwd.append(item["close"][pos + HORIZON]
                           / float(row["entry_close"] or item["close"][pos]) - 1.0)
            if not item["advance"][pos: pos + 21].any():
                nomove += 1
        if not fwd:
            return len(rows), float("nan"), float("nan"), float("nan"), float("nan")
        arr = np.asarray(fwd)
        return (len(rows), float((arr > 0.10).mean()),
                float((arr < -0.10).mean()), float(arr.mean()),
                nomove / len(rows))

    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    all_days = sorted({str(row["trade_date"]) for row in alerts}
                      | {d for item in prepared.values() for d in item["days"]})
    day_index = {d: i for i, d in enumerate(all_days)}
    reverse = {name: code for code, name in names.items()}

    def ledger(rows: list[dict]) -> tuple[int, int, int, int]:
        fired: dict[str, set[str]] = {}
        for row in rows:
            fired.setdefault(str(row["board_code"]), set()).add(
                str(row["trade_date"]))
        good = late = early = missed = 0
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
                continue
            delta = day_index[first] - center
            if delta < -LEAD_OK:
                early += 1
            elif delta <= LAG_OK:
                good += 1
            else:
                late += 1
        return good, early, late, missed

    base_count, base_up, base_down, base_mean, base_nomove = quality(alerts)
    base_ledger = ledger(alerts)

    emit("# 季节性限制的效果评估")
    emit()
    emit(f"> 由 `scripts/seasonal_effect_report.py` 生成（只读，告警表 "
         f"`{args.alert_table}`）。")
    emit(f"> 「无行情」= 告警后 20 个交易日内从未出现滚动 20/35 日涨幅 > "
         f"{BIG:.0%}；买入价 = 告警日收盘。")
    emit()
    emit(f"**全量基线**：{base_count} 条告警，P(>+10%) {base_up:.1%}、"
         f"P(<−10%) {base_down:.1%}、均值 {base_mean:+.2%}、"
         f"无行情率 {base_nomove:.1%}；真值台账 "
         f"{base_ledger[0]}/{base_ledger[1]}/{base_ledger[2]}/{base_ledger[3]}"
         "（合格/太早/滞后/漏报）。")
    emit()
    emit("⚠️ 每个自然月只有 3~4 个观测，下面所有「按月份」的结论都是**小样本**；"
         "面板上的差异要按方向性看，不要当成精确估计。")
    emit()

    for code, cases in CASES.items():
        own = [row for row in alerts if str(row["board_code"]) == code]
        if not own:
            continue
        emit(f"## {names.get(code, code)}（{code}）")
        emit()
        emit(f"该板块共 {len(own)} 条告警。逐月效果：")
        emit()
        emit("| 月份 | 告警数 | P(>+10%) | P(<−10%) | 均值20日 | 无行情率 |")
        emit("|---:|---:|---:|---:|---:|---:|")
        for month in range(1, 13):
            subset = [r for r in own if int(str(r["trade_date"])[4:6]) == month]
            if not subset:
                continue
            count, up, down, mean, nomove = quality(subset)
            emit(f"| {MONTH_LABEL[month - 1]} | {count} | {up:.0%} "
                 f"| {down:.0%} | {mean:+.2%} | {nomove:.0%} |")
        emit()
        emit("### 各候选月份集合的效果（把这个板块其余月份的告警删掉）")
        emit()
        emit("| 月份集合 | 保留告警 | 删掉 | 全体 P(>+10%) | P(<−10%) "
             "| 均值 | 全体告警数 | 真值台账 |")
        emit("|---|---:|---:|---:|---:|---:|---:|---|")
        for label, months in cases.items():
            kept = [row for row in alerts
                    if not (str(row["board_code"]) == code
                            and int(str(row["trade_date"])[4:6]) not in months)]
            removed = len(alerts) - len(kept)
            count, up, down, mean, _nomove = quality(kept)
            good, early, late, missed = ledger(kept)
            flag = "✅" if (missed <= base_ledger[3] and mean >= base_mean) else "⚠️"
            emit(f"| {label}{flag} | {count - (base_count - len(own))} "
                 f"| {removed} | {up:.1%} | {down:.1%} | {mean:+.2%} "
                 f"| {count} | {good}/{early}/{late}/{missed} |")
        emit()

    emit("## 怎么读")
    emit()
    emit("- 看两个东西：**全体均值/命中率有没有改善**，以及**漏报有没有增加**；")
    emit("- 「保留告警」那一列是该板块剩下的条数（其余是全局基线，不变）；")
    emit("- 若某个月份集合让均值变好但漏报增加，那就是用覆盖换精度 —— "
         "本项目的口径是**不划算**，应缩小集合或改成降级为观察。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    print(json.dumps({"cases": {k: list(v) for k, v in CASES.items()}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
