"""把突破路径放到**候选池外**：值不值？该放多宽？

## 起因（用户三条诉求里唯一还没动过的一条）

用户问过：「观测到煤炭概念 885914、石油石化、金属铜 885973、黄金概念 885530
在回测时反馈板块不在池内，漏掉这几个板块了吗？」

`missed_events_report.py` 的结论是 15 个可评估真值事件里
**7 个抓到 / 4 个 never_candidate / 4 个突破·门限没够**。
`never_candidate` 最狠 —— 那些板块**从来没有一天进过候选池**：

- 第一层（六维粗筛）就把它排在外面；
- 而 `_decide_level` 的第一道闸门是
  `if not board.candidate: return SignalLevel.NONE`；
- 于是绝对阈值、龙头共振、突破触发**三条路全部走不通**。

农业种植 885812 在 `20260706~20261006` 里候选天数为 0，
这就是"厄尔尼诺主线确认启动了、系统一声不响"的机制。

## 口径（关键：要跟**现状**比，不是跟"只有突破路径"比）

线上现在能报警的路径是：候选池内的绝对阈值 / 共振 / 突破，加上池外的
`promoted`（龙头共振提名）。所以拿"只有突破路径"当基准会高估收益。
本脚本因此以 `mainline_alert` 表的**真实告警集**为基准，比较：

- **现状**：`mainline_alert`（就是用户现在看到的东西）；
- **放开后**：现状 ∪（池外板块的突破命中）。

对每个档位给：新增告警量与日均、新增命中的收益分布（涨超 10% 与跌超 10%
必须一起看）、**净新增覆盖的真值事件数**、以及系统整体的合格率与漏报。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/noncandidate_breakout_report.py \\
        --out docs/MAINLINE_NONCANDIDATE_BREAKOUT.md
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

from src.mainline.scoring import breakout_ceiling, is_breakout  # noqa: E402

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LEAD_OK, LAG_OK = 10, 5
LOOKBACK_EVENT, LOOKAHEAD_EVENT = 20, 40
BIG = 0.10
HORIZON = 20


def load_rows(table: str) -> list[dict]:
    """每个 (交易日, 板块)：`total` 与是否候选。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, total, candidate FROM {table}"
        " ORDER BY board_code, trade_date").fetchall()
    conn.close()
    return [{"day": str(r["trade_date"]), "code": str(r["board_code"]),
             "total": float(r["total"] or 0.0),
             "candidate": bool(r["candidate"])} for r in rows]


def load_alerts(table: str = "mainline_alert") -> dict[str, set[str]]:
    """线上真实告警集 `{板块: {告警日}}`。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    rows = conn.execute(
        f"SELECT trade_date, board_code FROM {table}").fetchall()
    conn.close()
    out: dict[str, set[str]] = {}
    for day, code in rows:
        out.setdefault(str(code), set()).add(str(day))
    return out


def replay(rows: list[dict], *, lookback_days: int, quantile: float,
           min_samples: int, min_score: float, fresh_days: int,
           mode: str) -> dict[str, set[str]]:
    """逐日重放突破规则（严格 PIT）。

    `mode`：`in` 只用候选池内的行 / `out` 只用池外的行 / `all` 全用。
    """
    series: dict[str, list[dict]] = {}
    for row in rows:
        series.setdefault(row["code"], []).append(row)
    fired: dict[str, set[str]] = {}
    for code, items in series.items():
        for index, item in enumerate(items):
            if mode == "in" and not item["candidate"]:
                continue
            if mode == "out" and item["candidate"]:
                continue
            history = [one["total"] for one in items[max(0, index - lookback_days):
                                                    index]]
            ceiling = breakout_ceiling(history, quantile=quantile,
                                       min_samples=min_samples)
            if is_breakout(item["total"], ceiling, min_score=min_score,
                           recent=history, fresh_days=fresh_days):
                fired.setdefault(code, set()).add(item["day"])
    return fired


def forward_returns() -> dict[tuple[str, str], float]:
    """`{(day, code): 未来 HORIZON 日收盘收益}`，只取日历内的交易日。"""
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
        " JOIN ml_calendar k ON k.trade_date = b.trade_date"
        " ORDER BY b.board_code, b.trade_date").fetchall()
    conn.close()
    series: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        series.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    out: dict[tuple[str, str], float] = {}
    for code, items in series.items():
        items.sort()
        for index, (day, close) in enumerate(items):
            target = index + HORIZON
            if target < len(items) and close > 0:
                out[(day, code)] = items[target][1] / close - 1.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="池外突破路径评估")
    parser.add_argument("--table", default="mainline_score")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--quantile", type=float, default=0.90)
    parser.add_argument("--min-samples", type=int, default=60)
    parser.add_argument("--min-score", type=float, default=50.0)
    parser.add_argument("--fresh-days", type=int, default=10)
    parser.add_argument("--sweep", default="0.90:50:10,0.95:50:10,0.95:60:10,"
                                        "0.97:50:10,0.95:60:5,0.90:60:5",
                        help="分位:下限:新鲜度，逗号分隔")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    rows = load_rows(args.table)
    if not rows:
        emit("❌ 没有评分数据")
        return 2
    days = sorted({row["day"] for row in rows})
    index = {day: position for position, day in enumerate(days)}
    boards = len({row["code"] for row in rows})
    candidate_share = sum(1 for row in rows if row["candidate"]) / len(rows)

    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    names: dict[str, str] = {}
    for row in conn.execute("SELECT code, name FROM ml_board"):
        names.setdefault(str(row["name"]), str(row["code"]))
    conn.close()
    reverse = {code: name for name, code in names.items()}
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    fwd = forward_returns()
    system = load_alerts(args.alert_table)

    def timing(fired: dict[str, set[str]]) -> dict:
        good = late = early = missed = 0
        deltas: list[int] = []
        caught: list[str] = []
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
                continue
            caught.append(str(event.get("label") or date)[:20])
            delta = index[first] - center
            deltas.append(delta)
            if delta < -LEAD_OK:
                early += 1
            elif delta <= LAG_OK:
                good += 1
            else:
                late += 1
        total = good + late + early + missed
        return {"total": total, "good": good, "late": late, "early": early,
                "missed": missed,
                "rate": (good / total * 100) if total else float("nan"),
                "median": (float(np.median(deltas)) if deltas else float("nan")),
                "caught": caught}

    def stats_of(hits: set[tuple[str, str]]) -> tuple[int, float, float, float]:
        values = np.array([fwd[key] for key in hits if key in fwd], dtype=float)
        if not values.size:
            return len(hits), float("nan"), float("nan"), float("nan")
        return (len(hits), float((values > BIG).mean()),
                float((values < -BIG).mean()), float(values.mean()))

    system_stat = timing(system)
    emit("# 把突破路径放到候选池外：值不值，该放多宽？")
    emit()
    emit(f"> 由 `scripts/noncandidate_breakout_report.py` 生成，"
         f"评分表 `{args.table}`、告警表 `{args.alert_table}`。")
    emit(f"> 全表 {len(rows)} 行、{boards} 个板块、{len(days)} 个交易日，"
         f"候选占比 {candidate_share:.1%}。")
    emit("> 基准是**线上真实告警集**（`mainline_alert`），不是"
         "「只有突破路径」—— 后者会高估收益。")
    emit(f"> 离线重放不模拟确认与冷却，所以池外那部分是上界；"
         f"验收：提前 ≤{LEAD_OK} 或滞后 ≤{LAG_OK} 个交易日 = 合格。")
    emit()
    emit("## 零、现状基准")
    emit()
    emit(f"- 线上告警 {sum(len(v) for v in system.values())} 条，"
         f"覆盖 {len(system)} 个板块")
    emit(f"- 真值事件：合格 {system_stat['good']} / 滞后 {system_stat['late']} / "
         f"太早 {system_stat['early']} / **漏报 {system_stat['missed']}**，"
         f"合格率 {system_stat['rate']:.0f}%")
    emit()

    emit("## 一、池外突破：放宽到什么程度")
    emit()
    emit("| 分位 | 下限 | 新鲜度 | 新增条数 | 日均 | P(>+10%) | P(<−10%) | "
         "均值 | 净新增事件 | 漏报 | 合格 | 太早 | 滞后 | 合格率 |")
    emit("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    sweep = []
    for item in args.sweep.split(","):
        parts = item.split(":")
        if len(parts) != 3:
            continue
        quantile, min_score, fresh = float(parts[0]), float(parts[1]), int(parts[2])
        extra = replay(rows, lookback_days=args.lookback, quantile=quantile,
                       min_samples=args.min_samples, min_score=min_score,
                       fresh_days=fresh, mode="out")
        hits = {(day, code) for code, values in extra.items() for day in values}
        union: dict[str, set[str]] = {code: set(values)
                                      for code, values in system.items()}
        for code, values in extra.items():
            union.setdefault(code, set()).update(values)
        stat = timing(union)
        newly = sorted(set(stat["caught"]) - set(system_stat["caught"]))
        count, up, down, mean = stats_of(hits)
        sweep.append({"quantile": quantile, "min_score": min_score,
                      "fresh": fresh, "count": count, "newly": newly,
                      "missed": stat["missed"], "rate": stat["rate"],
                      "up": up, "down": down})
        emit(f"| {quantile:g} | {min_score:g} | {fresh} | {count} "
             f"| {count / len(days):.2f} | {up:.1%} | {down:.1%} | {mean:+.2%} "
             f"| **{len(newly)}** | {stat['missed']} | {stat['good']} "
             f"| {stat['early']} | {stat['late']} | {stat['rate']:.0f}% |")
    emit()

    if sweep:
        best = max(sweep, key=lambda item: (len(item["newly"]), -item["count"]))
        emit(f"- 净新增事件最多的一档：分位 {best['quantile']:g} / 下限 "
             f"{best['min_score']:g} / 新鲜度 {best['fresh']}，"
             f"净新增 **{len(best['newly'])}** 个事件，"
             f"漏报 {system_stat['missed']} → {best['missed']}，"
             f"新增告警 {best['count']} 条（日均 {best['count'] / len(days):.2f}）")
        emit(f"- 该档净新增的事件：{'、'.join(best['newly'])}")
        emit()

    emit("## 二、增量命中落在哪些板块")
    emit()
    base_extra = replay(rows, lookback_days=args.lookback, quantile=args.quantile,
                        min_samples=args.min_samples, min_score=args.min_score,
                        fresh_days=args.fresh_days, mode="out")
    tally: dict[str, int] = {}
    for code, values in base_extra.items():
        tally[code] = len(values)
    emit(f"默认参数（分位 {args.quantile:g} / 下限 {args.min_score:g} / "
         f"新鲜度 {args.fresh_days}）下池外命中 {sum(tally.values())} 条，"
         f"覆盖 {len(tally)} 个板块。命中最多的前 15：")
    emit()
    emit("| 板块 | 池外命中 | 其中是否线上已有告警 |")
    emit("|---|---:|---|")
    for code, count in sorted(tally.items(), key=lambda kv: -kv[1])[:15]:
        known = "是" if code in system else "**否**"
        emit(f"| {reverse.get(code, code)}（{code}） | {count} | {known} |")
    emit()

    emit("## 三、怎么读")
    emit()
    emit("- 「净新增事件」= 放开后系统能抓到、而现在抓不到的真值事件数，"
         "这是用户诉求「补充这几个板块」的直接度量；")
    emit("- 「漏报」下降的同时必须看「新增条数 / 日均」与「P(<−10%)」："
         "如果新增都落在跌超 10% 的板块上，那是把漏报换成了误报；")
    emit("- 分位抬高（0.95/0.97）与下限抬高都会显著收窄增量，"
         "所以**先选净新增不掉的最高分位**，再用下限微调。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    print(json.dumps({"sweep": [{k: v for k, v in item.items() if k != "newly"}
                                for item in sweep]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
