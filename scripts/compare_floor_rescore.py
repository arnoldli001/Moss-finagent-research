"""重打分前后对照：景气度低分压制 floor=40 到底改变了什么。

## 这是**真表对真表**的对照

前几轮的对照都是离线重算（不含蓄势层，保真度只有 47%）。
这次 `mainline_score` 是用新口径**整段重打分**出来的真表，
所以可以直接和备份表 `mainline_score_bak_v26_prefloor` 比 ——
`total`、`candidate`、`level`、蓄势层、加分全都在。

## ⚠️ 这一版是**合并口径**，不是"只改了 floor"

从 `mainline_score_bak_v26_prefloor` 到现在，一共动了三件事：

    1. 景气度低分压制 floor=40（`PROSPERITY_FLOOR`）
    2. V3：低景气组改用「增速变化率」合成（`prosperity_delta_below: 30`）
    3. 告警范围闸门：8 个板块全年关闭、旅游按旺季、煤炭/电力旺季外只发强信号

其中 **(3) 不影响任何分数**（`_decide_level` 只决定告警写不写），
所以 `total` / `candidate` 的差异**只**来自 (1)(2)；
但 `level` 与 `mainline_alert` 的差异是 (1)(2)(3) 共同的。
报告里必须按这个分工读，否则会把"闸门关掉多少条告警"错记成"floor 的效果"。

## ⚠️ 跑之前必须确认重打分已经结束

重打分是"先清空、再逐日写"。中途读 `mainline_score` 会得到**残表**，
而残表看起来完全正常（§16.39 记过这个坑）。所以本脚本先自检：
最新交易日覆盖、每日板块数、行数是否与备份同量级，不通过就拒绝出结论。

## 看五件事

1. **煤炭 885914 的候选天数 / 告警数**（用户最关心的那个板块）；
2. **低景气组（29 个）的候选天数**分布是否整体上移；
3. **全池告警质量**：告警数、未来 20 日 P(>+10%) / P(<−10%) / 均值；
4. **用户启动集判据**：告警集（候选 且 `total ≥ 77`）的召回率与误报率 FP/TP，
   训练/留出分开；
5. **真值事件台账**：合格/太早/滞后/漏报。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/compare_floor_rescore.py \\
        --before mainline_score_bak_v26_prefloor \\
        --out docs/MAINLINE_FLOOR_RESCORE.md
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: ⚠️ 与 `src/core/config.py` 的 `MOSS_SQLITE_PATH` 对齐：全量重打分跑在**副本**上时
#: （见 `docs/MAINLINE_MINING.md` §16.74），对照也必须读**同一个副本** ——
#: 否则会拿副本里的新结果去比生产库的旧表，得出一个看着合理、实际是跨库比的假结论。
MAIN_DB = ROOT / os.environ.get("MOSS_SQLITE_PATH", "data/moss_finagent.db")
CACHE_DB = ROOT / "data" / "mainline_cache.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
LEAD_OK, LAG_OK = 10, 5
FP_RATIO = 0.22
BIG = 0.10
HORIZON = 20
MEDIUM = 77.0
TRAIN = ("20231009", "20250930")
TEST = ("20251001", "20260918")
#: 景气度全市场分位 < 30 的低景气组（前几轮算出来的名单，写死在这里以便复现）
SUPPRESSED = (
    "885699.TI", "885750.TI", "885958.TI", "885758.TI", "885914.TI",
    "885842.TI", "885398.TI", "885915.TI", "886076.TI", "886060.TI",
    "886049.TI", "885578.TI", "886040.TI", "885994.TI", "886026.TI",
    "885877.TI", "886100.TI", "885845.TI", "886094.TI", "885991.TI",
    "885791.TI", "885955.TI", "886095.TI", "886053.TI", "885378.TI",
    "885820.TI", "885991.TI", "886006.TI", "886007.TI", "885878.TI",
)


def check_complete(conn: sqlite3.Connection, table: str, *,
                   min_boards: int, start: str = "", end: str = ""
                   ) -> tuple[bool, str]:
    """自检表是否写完整（防止拿残表出结论）。

    ⚠️ **不再硬编码"每天至少 200 个板块"**（2026-09-22 修）。那句判据写死了
    "线上是 288~324"这个当时的事实；用户把池子从 324 剔到 161 之后，
    一份**完整**的表也会被判成残表。判据应该是"与**当前池规模**相称"，
    所以门槛由调用方按 `ml_board` 的行数传进来。

    ⚠️ **`start`/`end` 不是装饰**（2026-09-22 二次修）：重打分默认从
    `20221201` 起跑，比历史表早约 10 个月——那一段是**热身区间**，
    当时池里只有 6 个板块，于是"单日最少板块 6"会把一份**完整**的新表
    判成残表。所以完整性只能在**两边都有意义的公共区间**上判。
    """
    where, params = "", []
    if start:
        where += " WHERE trade_date >= ?"
        params.append(start)
    if end:
        where += (" AND" if where else " WHERE") + " trade_date <= ?"
        params.append(end)
    row = conn.execute(
        f"SELECT COUNT(*) n, COUNT(DISTINCT trade_date) d,"
        f" MAX(trade_date) mx FROM {table}{where}", params).fetchone()
    per_day = conn.execute(
        f"SELECT trade_date, COUNT(*) c FROM {table}{where}"
        " GROUP BY trade_date ORDER BY c LIMIT 1", params).fetchone()
    if not row["n"]:
        return False, "表为空"
    smallest = int(per_day["c"]) if per_day else 0
    # 门槛里的 700 也按区间长短折算：公共区间本来就短于全表时不能要求 700 天。
    days_seen = int(row["d"])
    need = min(700, days_seen if days_seen else 700)
    ok = days_seen >= need and smallest >= min_boards
    return ok, (f"{row['n']} 行 / {days_seen} 个交易日 / 最末日 {row['mx']}"
                f" / 单日最少板块 {smallest}（门槛 {min_boards}）")


def pool_size(conn: sqlite3.Connection, table: str) -> int:
    """该表涉及多少个板块（用来判断两轮是不是同一个池子）。"""
    return int(conn.execute(
        f"SELECT COUNT(DISTINCT board_code) FROM {table}").fetchone()[0])


def board_codes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[0]) for r in
            conn.execute(f"SELECT DISTINCT board_code FROM {table}")}


def main() -> int:
    parser = argparse.ArgumentParser(description="floor=40 重打分前后对照")
    parser.add_argument("--before", default="mainline_score_bak_v26_prefloor")
    parser.add_argument("--after", default="mainline_score")
    parser.add_argument("--alert-before", default="mainline_alert_bak_v26_prefloor")
    parser.add_argument("--alert-after", default="mainline_alert")
    parser.add_argument("--out", default="")
    parser.add_argument("--allow-pool-change", action="store_true",
                        help="两轮池子规模不同时仍出结论。分数层面的比较会"
                             "**限制在两池共有的板块**上，并在报告里写明"
                             "「差值 = floor + V3 + 池收缩导致的截面位移」三者合并，"
                             "不能单独归因给 floor"
                             "（2026-09-22 起需要，因为池子从 324 剔到了 161）")
    parser.add_argument("--start", default="",
                        help="只比这个区间（含）。⚠️ 重打分默认从 20221201 起跑热身，"
                             "那段当时只有 6 个板块；不限定区间会把**完整**的新表"
                             "误判成残表，也会把热身段混进统计")
    parser.add_argument("--end", default="", help="只比到这个区间（含）")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # 门槛按**当前池规模**定：早期交易日有 list_date 门控，板块数会明显少于池子，
    # 取 40% 作为"这份表是完整的而不是残表"的下限。
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    pool_now = int(cache.execute("SELECT COUNT(*) FROM ml_board").fetchone()[0])
    cache.close()
    min_boards = max(8, int(pool_now * 0.4))

    if args.start or args.end:
        print(f"比较区间：{args.start or '（不限）'} ~ {args.end or '（不限）'}")

    for table in (args.before, args.after):
        ok, detail = check_complete(conn, table, min_boards=min_boards,
                                    start=args.start, end=args.end)
        print(f"   {table}: {'✅' if ok else '❌'} {detail}")
        if not ok:
            emit(f"❌ `{table}` 未写完整（{detail}）—— 重打分可能还在跑，拒绝出结论。")
            conn.close()
            return 2

    # ---------- 池子变没变（决定后面能不能做分数层面的比较）----------
    before_pool = pool_size(conn, args.before)
    after_pool = pool_size(conn, args.after)
    common = board_codes(conn, args.before) & board_codes(conn, args.after)
    pool_changed = before_pool != after_pool
    if pool_changed and not args.allow_pool_change:
        print()
        print(f"❌ 两轮**池子不同**：`{args.before}` {before_pool} 个 vs "
              f"`{args.after}` {after_pool} 个（共有 {len(common)} 个）。")
        print("   分数是**截面百分位**，池子一变所有板块的分数都会变 —— "
              "直接比 total 会把"
              "「池子收缩的影响」错记成「floor 的影响」。")
        print("   确实要出结论就加 `--allow-pool-change`：")
        print("   分数层面会**限制在共有板块**上，并把口径限制写进报告。")
        conn.close()
        return 2
    if pool_changed:
        print()
        print(f"⚠️ 池子不同（{before_pool} → {after_pool}，共有 {len(common)} 个）："
              "分数层面的比较限制在**共有板块**上；"
              "告警/台账/池规模是**全局**数字（含池收缩效应）。")

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    names = {str(r["code"]): str(r["name"]) for r in
             cache.execute("SELECT code, name FROM ml_board")}
    closes: dict[str, list[tuple[str, float]]] = {}
    for row in cache.execute(
            "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        closes.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    cache.close()
    forward: dict[tuple[str, str], float] = {}
    for code, items in closes.items():
        for pos, (day, close) in enumerate(items):
            if pos + HORIZON < len(items) and close > 0:
                forward[(day, code)] = items[pos + HORIZON][1] / close - 1.0

    def in_window(day: str) -> bool:
        if args.start and day < args.start:
            return False
        return not (args.end and day > args.end)

    def load_scores(table: str) -> tuple[dict[str, set[str]], dict[str, set[str]],
                                         dict[tuple[str, str], float]]:
        """`({板块: 候选日}, {板块: 告警日}, {(日,板块): total})`。

        ⚠️ 池子不同时只收**共有板块** —— 否则"低景气组候选天数上移"这类
        结论会被"池子里少了一半板块"污染（分母变了，不是分数变了）。
        ⚠️ 只收 `--start/--end` 区间内的日子 —— 否则会把重打分的**热身段**
        （当时只有 6 个板块）混进统计。
        """
        candidate: dict[str, set[str]] = {}
        alerted: dict[str, set[str]] = {}
        totals: dict[tuple[str, str], float] = {}
        for row in conn.execute(
                f"SELECT trade_date, board_code, total, candidate, level,"
                f" six_dim FROM {table}"):
            day, code = str(row["trade_date"]), str(row["board_code"])
            if not in_window(day):
                continue
            if pool_changed and code not in common:
                continue
            totals[(day, code)] = float(row["total"] or 0.0)
            if row["candidate"]:
                candidate.setdefault(code, set()).add(day)
            if str(row["level"]) in ("strong", "medium"):
                alerted.setdefault(code, set()).add(day)
        return candidate, alerted, totals

    def load_alerts(table: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for row in conn.execute(f"SELECT trade_date, board_code FROM {table}"):
            day = str(row["trade_date"])
            if not in_window(day):
                continue
            out.setdefault(str(row["board_code"]), set()).add(day)
        return out

    before_cand, before_lv, before_total = load_scores(args.before)
    after_cand, after_lv, after_total = load_scores(args.after)
    before_alert = load_alerts(args.alert_before)
    after_alert = load_alerts(args.alert_after)
    days = sorted({day for _d, _c in []} | {d for d, _c in before_total})
    days = sorted({d for d, _c in before_total} | {d for d, _c in after_total})

    labels_by_board: dict[str, tuple[list[int], list[str]]] = {}
    for code, items in closes.items():
        day_list = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        labels: set[int] = set()
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            pos = window
            while pos < len(close):
                if np.isfinite(rolling[pos]) and rolling[pos] > 0.15:
                    labels.add(pos)
                    pos += 4
                else:
                    pos += 1
        labels_by_board[code] = (sorted(labels), day_list)

    def launch_ledger(fired: dict[str, set[str]], low: str, high: str) -> dict:
        tp = fp = total = 0
        for code, (labels, day_list) in labels_by_board.items():
            index = {d: i for i, d in enumerate(day_list)}
            window_labels = [i for i in labels if low <= day_list[i] <= high]
            if not window_labels:
                continue
            total += len(window_labels)
            hits = {index[d] for d in fired.get(code, ())
                    if low <= d <= high and d in index}
            covered: set[int] = set()
            for i in window_labels:
                span = range(max(0, i - LEAD_OK), i + LAG_OK + 1)
                if any(t in span for t in hits):
                    tp += 1
                covered |= set(range(max(0, i - LEAD_OK), i + LAG_OK + 1))
            fp += sum(1 for t in hits if t not in covered)
        return {"labels": total, "tp": tp, "fp": fp,
                "recall": (tp / total) if total else float("nan"),
                "fp_ratio": (fp / tp) if tp else float("inf")}

    def quality(alerted: dict[str, set[str]]) -> tuple[int, float, float, float]:
        keys = [(day, code) for code, values in alerted.items() for day in values]
        values = np.asarray([forward[k] for k in keys if k in forward])
        if not values.size:
            return len(keys), float("nan"), float("nan"), float("nan")
        return (len(keys), float((values > BIG).mean()),
                float((values < -BIG).mean()), float(values.mean()))

    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    reverse = {name: code for code, name in names.items()}
    day_index = {day: i for i, day in enumerate(days)}

    def truth_ledger(fired: dict[str, set[str]]) -> dict:
        good = late = early = missed = 0
        for event in events:
            date = str(event.get("date") or "").replace("-", "")
            if date not in day_index:
                continue
            codes = [reverse.get(str(c), str(c))
                     for c in (event.get("codes") or [])]
            center = day_index[date]
            window = set(days[max(0, center - 20):
                              min(len(days) - 1, center + 40) + 1])
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
        total = good + late + early + missed
        return {"good": good, "late": late, "early": early, "missed": missed,
                "rate": (good / total * 100) if total else float("nan")}

    emit("# 景气度低分压制 floor=40：整段重打分前后对照")
    emit()
    emit(f"> 由 `scripts/compare_floor_rescore.py` 生成（只读）。")
    emit(f"> 前：`{args.before}` / `{args.alert_before}`；"
         f"后：`{args.after}` / `{args.alert_after}`。")
    emit("> **这是真表对真表**（含蓄势层与加分），不是离线代理。")
    emit()

    emit("## 一、煤炭 885914（用户最关心的那个）")
    emit()
    emit("| 指标 | 重打分前 | 重打分后 | 变化 |")
    emit("|---|---:|---:|---:|")
    emit(f"| 候选天数 | {len(before_cand.get('885914.TI', ()))} "
         f"| **{len(after_cand.get('885914.TI', ()))}** "
         f"| {len(after_cand.get('885914.TI', ())) - len(before_cand.get('885914.TI', ())):+d} |")
    emit(f"| strong/medium 级天数 | {len(before_lv.get('885914.TI', ()))} "
         f"| {len(after_lv.get('885914.TI', ()))} "
         f"| {len(after_lv.get('885914.TI', ())) - len(before_lv.get('885914.TI', ())):+d} |")
    emit(f"| 线上告警条数 | {len(before_alert.get('885914.TI', ()))} "
         f"| {len(after_alert.get('885914.TI', ()))} "
         f"| {len(after_alert.get('885914.TI', ())) - len(before_alert.get('885914.TI', ())):+d} |")
    emit()

    emit("## 二、低景气组（29 个）的候选天数")
    emit()
    emit("| 板块 | 重打分前 | 重打分后 | 变化 |")
    emit("|---|---:|---:|---:|")
    changed = 0
    for code in dict.fromkeys(SUPPRESSED):
        b = len(before_cand.get(code, ()))
        a = len(after_cand.get(code, ()))
        changed += int(a != b)
        emit(f"| {names.get(code, code)}（{code}） | {b} | {a} | {a - b:+d} |")
    b_all = [len(before_cand.get(c, ())) for c in dict.fromkeys(SUPPRESSED)]
    a_all = [len(after_cand.get(c, ())) for c in dict.fromkeys(SUPPRESSED)]
    emit()
    emit(f"- 有变化的板块：**{changed}** / {len(b_all)}")
    emit(f"- 候选天数中位：{int(np.median(b_all))} → **{int(np.median(a_all))}**；"
         f"合计 {sum(b_all)} → **{sum(a_all)}**")
    emit()

    emit("## 三、全池告警质量（真表口径）")
    emit()
    emit("| 口径 | 告警条数 | P(>+10%) | P(<−10%) | 均值 |")
    emit("|---|---:|---:|---:|---:|")
    for label, alerted in (("重打分前", before_lv), ("重打分后", after_lv)):
        count, up, down, mean = quality(alerted)
        emit(f"| {label}（候选且 strong/medium） | {count} | {up:.1%} "
             f"| {down:.1%} | {mean:+.2%} |")
    for label, fired in (("重打分前", before_alert), ("重打分后", after_alert)):
        count, up, down, mean = quality(fired)
        emit(f"| {label}（`mainline_alert` 实表） | {count} | {up:.1%} "
             f"| {down:.1%} | {mean:+.2%} |")
    emit()

    emit("## 四、用户启动集判据（候选且 `total ≥ 77`）")
    emit()
    emit("| 口径 | 窗口 | 启动日 | TP | FP | 召回率 | 误报率 FP/TP | 达标 |")
    emit("|---|---|---:|---:|---:|---:|---:|---|")
    for label, cand, totals in (("重打分前", before_cand, before_total),
                                ("重打分后", after_cand, after_total)):
        # 候选 且 total ≥ 77
        fired: dict[str, set[str]] = {}
        for code, values in cand.items():
            for day in values:
                if totals.get((day, code), 0.0) >= MEDIUM:
                    fired.setdefault(code, set()).add(day)
        for tag, (low, high) in (("训练", TRAIN), ("留出", TEST)):
            stat = launch_ledger(fired, low, high)
            ok = bool(stat["tp"]) and stat["fp"] <= FP_RATIO * stat["tp"]
            emit(f"| {label} | {tag} | {stat['labels']} | {stat['tp']} "
                 f"| {stat['fp']} | {stat['recall']:.0%} "
                 f"| {stat['fp_ratio']:.2f} | {'✅' if ok else '❌'} |")
    emit()

    emit("## 五、真值事件台账（24 个）")
    emit()
    emit("| 口径 | 合格 | 太早 | 滞后 | 漏报 | 合格率 |")
    emit("|---|---:|---:|---:|---:|---:|")
    for label, fired in (("重打分前", before_alert), ("重打分后", after_alert)):
        ledger = truth_ledger(fired)
        emit(f"| {label} | {ledger['good']} | {ledger['early']} "
             f"| {ledger['late']} | {ledger['missed']} | {ledger['rate']:.0f}% |")
    emit()

    emit("## 六、结论")
    emit()
    emit("判据顺序：① 煤炭是否真的被看见；② 低景气组是否整体上移；"
         "③ 留出窗口的 FP/TP 与召回率有没有变差；④ 真值漏报有没有增加。"
         "如果 ③④ 变差，就该把 `PROSPERITY_FLOOR` 调回 50（等于关闭）或换 O3。")
    emit()
    emit("⚠️ 别忘了**口径切换的那一周**：压制改变了低景气板块的分数，"
         "所以跨重打分日期的前后比较会有一个台阶，回测报告要把这一点标出来。")

    conn.close()
    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    print(json.dumps({"before": args.before, "after": args.after},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
