"""哪些概念板块「误报率高 + 买入后亏得多」—— 排名与剔除代价。

## 用户要什么

> 「有哪些误报率较高且买入后亏损多的概念，挑出来最多的我看看，
>   不行就去掉这类题材的主线告警监控。」

所以要回答两件事：**挑出哪些板块**，以及**去掉它们的代价**。

## 两个口径必须同时看（只看一个会选错）

- **亏损口径**：以告警日收盘为买入价（`mainline_alert.entry_close`），
  看未来 5/10/20 个交易日的收益、以及 20 日内的**最大浮亏（MAE）**。
  「买入后亏损多」说的是这个。
- **无行情口径**：告警后 20/35 个交易日内，**有没有出现过** ≥15% 的上涨
  （沿用用户给的启动定义：滚动 20/35 日涨幅 > 15%）。没有 = 这次告警
  根本没等到主线。「误报率高」说的是这个。

⚠️ 两个口径会给出**不同的名单**，这是正常的：一个板块可能"没大跌但也没涨"
（无行情但不算亏），也可能"涨过 15% 但你先亏了 20%"（有行情但拿不住）。
所以输出两张榜，并给出"两者都差"的交集 —— 那才是能放心剔除的。

## 剔除的代价（必须一起给）

只看"剔掉最差的 N 个板块能提升多少"是不够的 —— 还要看
**24 个真值事件里有没有事件的首报就落在这批板块上**。剔错了会把
真行情的告警一起关掉。所以脚本同时算"剔除后的真值台账"。

## 样本量下限

289 个板块平均只有 ~17 条告警，单板块均值噪声很大。所以：
- 只对告警数 ≥ `--min-alerts` 的板块排名；
- 同时给出均值的 t 值（`mean / (std/√n)`），t 小的即便均值很差也标出来。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/losing_themes_report.py \\
        --alert-table mainline_alert_bak_v26_prefloor \\
        --out docs/MAINLINE_LOSING_THEMES.md
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
BIG = 0.15           # "出现过主线行情"的门槛（用户口径）
HORIZONS = (5, 10, 20)
MAE_HORIZON = 20


def main() -> int:
    parser = argparse.ArgumentParser(description="亏损题材排名与剔除代价")
    parser.add_argument("--alert-table", default="mainline_alert_bak_v26_prefloor")
    parser.add_argument("--min-alerts", type=int, default=12)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--cut", type=int, default=15,
                        help="剔除场景里去掉多少个板块")
    parser.add_argument("--protect-truth", action="store_true", default=True,
                        help="保护真值事件的首报见证板块，不参与剔除（默认开）")
    parser.add_argument("--no-protect-truth", dest="protect_truth",
                        action="store_false",
                        help="关掉保护，看「未保护」时的代价（对照用）")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = [dict(row) for row in conn.execute(
        f"SELECT trade_date, board_code, board_name, level, score, entry_close"
        f" FROM {args.alert_table} ORDER BY trade_date")]
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

    # 每个板块：日期→下标、收盘价、以及"出现过 ≥15% 行情"的日集合
    prepared: dict[str, dict] = {}
    for code, items in bars.items():
        day_list = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        index = {d: i for i, d in enumerate(day_list)}
        advance = np.zeros(len(close), dtype=bool)
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            advance |= np.isfinite(rolling) & (rolling > BIG)
        prepared[code] = {"days": day_list, "close": close,
                          "index": index, "advance": advance}

    def stats_for(code: str, rows: list[dict]) -> dict | None:
        item = prepared.get(code)
        if item is None or not rows:
            return None
        index, close = item["index"], item["close"]
        advance = item["advance"]
        fwd: dict[int, list[float]] = {h: [] for h in HORIZONS}
        mae: list[float] = []
        no_move_20 = no_move_35 = 0
        usable = 0
        for row in rows:
            day = str(row["trade_date"])
            pos = index.get(day)
            if pos is None:
                continue
            entry = float(row["entry_close"] or 0.0) or float(close[pos])
            if entry <= 0:
                continue
            usable += 1
            for horizon in HORIZONS:
                target = pos + horizon
                if target < len(close):
                    fwd[horizon].append(close[target] / entry - 1.0)
            window = close[pos + 1: pos + 1 + MAE_HORIZON]
            if window.size:
                mae.append(float(window.min()) / entry - 1.0)
            # 「告警后 N 日内出现过 ≥15% 行情」：从告警当天算起
            if not advance[pos: pos + 21].any():
                no_move_20 += 1
            if not advance[pos: pos + 36].any():
                no_move_35 += 1
        if not usable:
            return None
        out = {"alerts": usable, "strong": sum(1 for r in rows
                                               if r["level"] == "strong")}
        for horizon in HORIZONS:
            values = np.asarray(fwd[horizon])
            if values.size:
                out[f"mean{horizon}"] = float(values.mean())
                out[f"med{horizon}"] = float(np.median(values))
                out[f"win{horizon}"] = float((values > 0).mean())
                out[f"n{horizon}"] = int(values.size)
                std = float(values.std(ddof=1)) if values.size > 1 else 0.0
                out[f"t{horizon}"] = (float(values.mean())
                                      / (std / np.sqrt(values.size))
                                      if std else float("nan"))
                out[f"sum{horizon}"] = float(values.sum())
        values = np.asarray(fwd[20])
        if values.size:
            out["loss_rate"] = float((values < 0).mean())
            out["deep_loss"] = float((values < -0.10).mean())
            out["big_win"] = float((values > 0.10).mean())
        mae_values = np.asarray(mae)
        if mae_values.size:
            out["mae_mean"] = float(mae_values.mean())
            out["mae_worst"] = float(mae_values.min())
        out["no_move20"] = no_move_20 / usable
        out["no_move35"] = no_move_35 / usable
        return out

    by_board: dict[str, list[dict]] = {}
    for row in alerts:
        by_board.setdefault(str(row["board_code"]), []).append(row)

    ranked: list[dict] = []
    for code, rows in by_board.items():
        stat = stats_for(code, rows)
        if stat is None:
            continue
        stat["code"] = code
        stat["name"] = names.get(code, rows[0].get("board_name") or code)
        ranked.append(stat)

    emit("# 误报率高 + 买入后亏得多的概念板块")
    emit()
    emit(f"> 由 `scripts/losing_themes_report.py` 生成（只读，告警表 "
         f"`{args.alert_table}`）。")
    emit(f"> 买入价 = 告警日收盘（`entry_close`）；"
         f"「无行情」= 告警后 20/35 个交易日内**从未**出现 "
         f"滚动 20/35 日涨幅 > {BIG:.0%}。")
    emit(f"> 只对告警数 ≥ {args.min_alerts} 的板块排名"
         f"（共 {len(ranked)} 个达标 / 全池有告警的 {len(by_board)} 个）。")
    emit()

    eligible = [item for item in ranked if item["alerts"] >= args.min_alerts]
    by_loss = sorted(eligible, key=lambda d: d.get("sum20", 0.0))
    emit(f"## 一、按「累计亏损」排序（等权买每次告警，未来 20 日累计收益）")
    emit()
    emit("| 板块 | 告警数 | 累计20日 | 均值20日 | t值 | 亏损率 | 深亏率 "
         "| 最大浮亏均 | 无行情率20 |")
    emit("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for item in by_loss[:args.top]:
        emit(f"| {item['name']}（{item['code']}） | {item['alerts']} "
             f"| **{item.get('sum20', float('nan')):+.2f}** "
             f"| {item.get('mean20', float('nan')):+.2%} "
             f"| {item.get('t20', float('nan')):+.2f} "
             f"| {item.get('loss_rate', float('nan')):.0%} "
             f"| {item.get('deep_loss', float('nan')):.0%} "
             f"| {item.get('mae_mean', float('nan')):+.2%} "
             f"| {item.get('no_move20', float('nan')):.0%} |")
    emit()

    by_nomove = sorted(eligible, key=lambda d: -d.get("no_move20", 0.0))
    emit(f"## 二、按「无行情率」排序（告警后 20 日内从未涨过 {BIG:.0%}）")
    emit()
    emit("| 板块 | 告警数 | 无行情率20 | 无行情率35 | 均值20日 | 亏损率 "
         "| 深亏率 | 累计20日 |")
    emit("|---|---:|---:|---:|---:|---:|---:|---:|")
    for item in by_nomove[:args.top]:
        emit(f"| {item['name']}（{item['code']}） | {item['alerts']} "
             f"| **{item.get('no_move20', float('nan')):.0%}** "
             f"| {item.get('no_move35', float('nan')):.0%} "
             f"| {item.get('mean20', float('nan')):+.2%} "
             f"| {item.get('loss_rate', float('nan')):.0%} "
             f"| {item.get('deep_loss', float('nan')):.0%} "
             f"| {item.get('sum20', float('nan')):+.2f} |")
    emit()

    # 两个口径的交集：无行情率 ≥ 50% 且 均值20日 < 0
    worst = [item for item in eligible
             if item.get("no_move20", 0.0) >= 0.5 and item.get("mean20", 0.0) < 0]
    worst.sort(key=lambda d: d.get("sum20", 0.0))
    emit("## 三、**两个口径都差**的板块（无行情率 ≥ 50% 且 20 日均值为负）")
    emit()
    emit(f"共 **{len(worst)}** 个：")
    emit()
    emit("| 板块 | 告警数 | 无行情率20 | 均值20日 | 累计20日 | 亏损率 |")
    emit("|---|---:|---:|---:|---:|---:|")
    for item in worst[:args.top]:
        emit(f"| {item['name']}（{item['code']}） | {item['alerts']} "
             f"| {item.get('no_move20', 0):.0%} "
             f"| {item.get('mean20', 0):+.2%} "
             f"| {item.get('sum20', 0):+.2f} "
             f"| {item.get('loss_rate', 0):.0%} |")
    emit()

    # 真值事件与交易日轴：**必须在剔除场景之前就绪** ——
    # "保护首报见证者"要用到它们（第一版把它们放在后面，直接 UnboundLocalError）。
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    events = [e for e in (truth.get("events") or [])
              if isinstance(e, dict) and e.get("status") == "ok"]
    days = sorted({str(row["trade_date"]) for row in alerts}
                  | {d for item in prepared.values() for d in item["days"]})
    day_index = {day: i for i, day in enumerate(days)}
    reverse = {name: code for code, name in names.items()}

    # ---------- 剔除代价 ----------
    #
    # ⚠️ 先**保护**"真值事件的首报见证者"：某些板块的告警虽然长期很差，
    # 却是某个真值事件唯一/最早抓到的来源（实测：文化传媒概念 885418 抓到
    # 20231201 短剧游戏/AIGC，滞后 +15 天）。把它一起剔掉，指标会好看一点，
    # 但代价是多一个漏报 —— 这种"用漏报换精度"的买卖本项目已经拒绝过很多次。
    # 所以默认把这类板块从剔除名单里摘出来，而不是让调用方自己发现。
    truth_owners: set[str] = set()
    if args.protect_truth:
        for row in alerts:
            truth_owners.add(str(row["board_code"]))
        # 逐个事件找出"首报来自哪个板块"，只保护那些**首报级**的见证者
        fired: dict[str, list[tuple[str, str]]] = {}
        for row in alerts:
            fired.setdefault(str(row["board_code"]), []).append(
                (str(row["trade_date"]), str(row["board_code"])))
        protect: set[str] = set()
        for event in events:
            date = str(event.get("date") or "").replace("-", "")
            if date not in day_index:
                continue
            codes = [reverse.get(str(c), str(c))
                     for c in (event.get("codes") or [])]
            center = day_index[date]
            window = set(days[max(0, center - 20):
                              min(len(days) - 1, center + 40) + 1])
            best: tuple[str, str] | None = None
            for code in codes:
                for day, owner in fired.get(code, ()):
                    if day in window and (best is None or day < best[0]):
                        best = (day, owner)
            if best:
                protect.add(best[1])
        truth_owners = protect

    cut_pool = [item for item in worst if item["code"] not in truth_owners]
    protected = [item for item in worst if item["code"] in truth_owners]
    cut_codes = {item["code"] for item in cut_pool[:args.cut]}
    kept = [row for row in alerts if str(row["board_code"]) not in cut_codes]
    removed = len(alerts) - len(kept)

    def quality(rows: list[dict]) -> tuple[int, float, float, float]:
        values = []
        for row in rows:
            item = prepared.get(str(row["board_code"]))
            if item is None:
                continue
            pos = item["index"].get(str(row["trade_date"]))
            if pos is None or pos + 20 >= len(item["close"]):
                continue
            entry = float(row["entry_close"] or 0.0) or float(item["close"][pos])
            if entry > 0:
                values.append(item["close"][pos + 20] / entry - 1.0)
        arr = np.asarray(values)
        if not arr.size:
            return 0, float("nan"), float("nan"), float("nan")
        return (int(arr.size), float((arr > 0.10).mean()),
                float((arr < -0.10).mean()), float(arr.mean()))

    emit("## 四、去掉这批板块的代价")
    emit()
    if protected:
        emit(f"🛡️ **已保护 {len(protected)} 个板块**（它们是某个真值事件的首报来源，"
             "不参与剔除）："
             + "、".join(f"{item['name']}({item['code']})" for item in protected))
        emit()
    emit(f"剔除名单 = 「两个口径都差」里累计最差的 **{len(cut_codes)}** 个板块："
         + "、".join(f"{item['name']}({item['code']})"
                     for item in cut_pool[:args.cut]))
    emit()
    emit("| 口径 | 告警条数 | P(>+10%) | P(<−10%) | 均值 |")
    emit("|---|---:|---:|---:|---:")
    for label, rows in (("剔除前", alerts), ("剔除后", kept)):
        count, up, down, mean = quality(rows)
        emit(f"| {label} | {count} | {up:.1%} | {down:.1%} | {mean:+.2%} |")
    emit(f"| 变化 | {len(kept) - len(alerts)} | — | — | — |")
    emit()
    emit(f"- 剔除 {len(cut_codes)} 个板块（{len(cut_codes) / max(len(by_board), 1):.0%} "
         f"的板块）会去掉 **{removed}** 条告警"
         f"（{removed / max(len(alerts), 1):.1%}）")
    emit()

    # 真值事件覆盖代价
    def ledger(rows: list[dict]) -> dict:
        fired: dict[str, set[str]] = {}
        for row in rows:
            fired.setdefault(str(row["board_code"]), set()).add(
                str(row["trade_date"]))
        good = late = early = missed = 0
        lost: list[str] = []
        detail: dict[str, dict] = {}
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
            owner = ""
            for code in codes:
                for day in fired.get(code, ()):
                    if day in window and (first is None or day < first):
                        first, owner = day, code
            key = f"{date} {str(event.get('label'))[:16]}"
            if first is None:
                missed += 1
                lost.append(key)
                detail[key] = {"verdict": "漏报", "day": "", "code": ""}
                continue
            delta = day_index[first] - center
            if delta < -LEAD_OK:
                early += 1
                verdict = "太早"
            elif delta <= LAG_OK:
                good += 1
                verdict = "合格"
            else:
                late += 1
                verdict = "滞后"
            detail[key] = {"verdict": verdict, "day": first, "code": owner,
                           "delta": delta}
        return {"good": good, "late": late, "early": early, "missed": missed,
                "lost": lost, "detail": detail}

    before, after = ledger(alerts), ledger(kept)
    emit("### 真值事件台账（这是剔除的真正代价）")
    emit()
    emit("| 口径 | 合格 | 太早 | 滞后 | 漏报 |")
    emit("|---|---:|---:|---:|---:|")
    emit(f"| 剔除前 | {before['good']} | {before['early']} | {before['late']} "
         f"| {before['missed']} |")
    emit(f"| 剔除后 | {after['good']} | {after['early']} | {after['late']} "
         f"| {after['missed']} |")
    emit()
    newly = sorted(set(after["lost"]) - set(before["lost"]))
    if newly:
        emit(f"⚠️ **剔除后新增漏报 {len(newly)} 个事件**：{'；'.join(newly)}")
        emit()
        emit("这几个事件原本靠哪些板块的告警才被抓到（这就是剔除踩掉的东西）：")
        emit()
        emit("| 事件 | 剔除前的首报日 | 差 | 靠哪个板块 |")
        emit("|---|---|---:|---|")
        for key in newly:
            was = before["detail"].get(key) or {}
            code = str(was.get("code") or "")
            emit(f"| {key} | {was.get('day', '—')} "
                 f"| {was.get('delta', '—'):+d} "
                 f"| {names.get(code, code)}（{code}） |"
                 if isinstance(was.get("delta"), int) else
                 f"| {key} | {was.get('day', '—')} | — "
                 f"| {names.get(code, code)}（{code}） |")
        emit()
        emit("→ 这批板块里有真值事件的首报，**不能整体剔除**。"
             "要么缩小名单（去掉那个事件所在的板块），"
             "要么改成「降级为观察」而不是完全不报。")
    else:
        emit("✅ 剔除后**没有**新增漏报 —— 这批板块上的告警确实没有覆盖到"
             "任何真值事件的首报。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    print(json.dumps({"cut": sorted(cut_codes), "min_alerts": args.min_alerts},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
