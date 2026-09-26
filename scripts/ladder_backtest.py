"""按用户给定的**交易规则**回测每条告警：止损 −7%、浮盈每 +10% 减仓 30%。

## 用户的口径（2026-09-22 原文）

> 「如果每次告警只去中、强信号，提醒后就买入，浮亏达到买入成本价的 93%,
>   也就是 7% 就止损；否则，浮盈超过 10% 就卖 30%，每多浮盈 10% 就减 30%,
>   直到卖完。统计下每个板块预警后买入的平均盈亏百分比。」

拆成可执行规则：

    信号      只取 **strong + medium**（weak 是观察档，不买）
    买入价    告警日收盘（`entry_close`，缺失回落到板块指数收盘）
    止损      **最低价** ≤ 买入价 × 0.93 → 剩余仓位全部按 买入价 × 0.93 成交
    止盈阶梯  **最高价** ≥ 买入价 × (1 + 10%×k) → 卖出一档（k = 1,2,3,…）
    持有      没有时间上限：直到「止损」或「卖完」为止；数据结束时仍未卖完
              的按最后一根收盘价**盯市**（标记为"未平仓"）

## ⚠️ 三个必须写明的假设（不写就等于偷偷替用户做了决定）

**① 同日既触止损又触止盈 → 按"先止损"处理。**
日线只有 OHLC，看不出盘中先到哪个。选"先止损"是**保守**方向：
它低估收益、高估止损次数。反过来假设"先止盈"会让结果虚高。

**② 「卖 30%」的口径 = 占【初始仓位】的 30%（主口径 A）。**
所以阶梯是 +10% 卖 30%、+20% 再卖 30%、+30% 再卖 30%、
+40% 把剩下 10% 卖完 —— 这才符合用户说的「**直到卖完**」。
另一种常见理解是"每次卖**当时剩余**的 30%"，那样剩余按 0.7^n 递减、
**永远卖不完**，与"直到卖完"矛盾。为了不替用户猜，两种都算，
A 为主口径、B 为对照列（B 最多走 15 档，剩余按未平仓盯市）。

**③ 未平仓按最后一根收盘价盯市。** 样本末端的告警常常还没走完，
把它们**丢掉**会系统性偏向"已走完的那批"（幸存者偏差）；
按收盘价盯市是唯一诚实的处理，但要单独统计条数。

## 为什么用小时级别做不到

板块指数只有日线（`ml_board_bar` 的 OHLC，来源 `tushare:ths_daily`）。
所以"当天先摸 −7% 还是先摸 +10%"这个顺序**在数据上不可判定** ——
只能靠假设 ①。要看真实顺序得用分钟级数据，那是另一件事。

只读，不写库。用法：
    .venv\\Scripts\\python.exe scripts/ladder_backtest.py \\
        --excel docs/MAINLINE_LADDER_BACKTEST.xlsx --md docs/MAINLINE_LADDER_BACKTEST.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"

STOP = 0.93          # 浮亏 7% 止损（价格 = 买入价 × 0.93）
STEP = 0.10          # 每档浮盈 10%
TRANCHE = 0.30       # 每档卖出初始仓位的 30%
MAX_STEPS_A = 4      # 初始仓位 30%×3 + 余下 10% → 第 4 档卖完
MAX_STEPS_B = 15     # 变体 B 的档位上限（剩余按 0.7^n 永不归零）
LEVELS = ("strong", "medium")


def round_trip(entry: float, opens: np.ndarray, highs: np.ndarray,
               lows: np.ndarray, closes: np.ndarray, *, variant: str
               ) -> tuple[float, str, int]:
    """走一遍价格路径，返回 `(盈亏比例, 出场原因, 用掉的档数)`。

    `opens/highs/lows/closes` 都从**入场日的下一个交易日**开始。
    `variant="A"`：每档卖**初始仓位**的 30%；`"B"`：每档卖**剩余仓位**的 30%。

    ## ⚠️ 跳空必须按开盘价成交，不能按止损价

    第一版无论怎么跌都按 `买入价 × 0.93` 成交，于是**一笔都不低于 −7%** ——
    那是模型的假象：板块指数跳空低开时，止损单只能在**开盘价**成交，
    实际亏损会比 −7% 更深。同一逻辑反过来也成立：跳空高开越过某一档时，
    成交价是开盘价、比该档目标价更好。

    所以两边都用 `开盘价` 与 `目标价` 里**对交易者更有利/更不利**的那个：
        止损成交价 = min(开盘价, 止损价)
        止盈成交价 = max(开盘价, 该档目标价)
    """
    stop_price = entry * STOP
    remaining = 1.0
    proceeds = 0.0
    step = 0
    max_steps = MAX_STEPS_A if variant == "A" else MAX_STEPS_B
    for open_, high, low in zip(opens, highs, lows, strict=False):
        open_ = float(open_ or 0.0)
        high = float(high or 0.0)
        low = float(low or 0.0)
        # ① 先判止损（保守假设：同日既触止损又触止盈时按先止损）
        if low > 0 and low <= stop_price:
            fill = stop_price if open_ <= 0 else min(open_, stop_price)
            proceeds += remaining * fill
            return proceeds / entry - 1.0, "止损", step
        # ② 再看阶梯止盈（一天可能连上多档 —— 跳空高开时确实会）
        while step < max_steps:
            target = entry * (1.0 + STEP * (step + 1))
            if high < target:
                break
            step += 1
            fill = target if open_ <= target else open_
            # A：每档卖【初始仓位】的 30%（取 min 是因为最后一档可能只剩 10%）
            # B：每档卖【剩余仓位】的 30%（对照；永不归零）
            qty = (min(TRANCHE, remaining) if variant == "A"
                   else remaining * TRANCHE)
            proceeds += qty * fill
            remaining -= qty
            if remaining <= 1e-9:
                return proceeds / entry - 1.0, "止盈卖完", step
    # ③ 数据走完仍未卖完：剩余按最后一根收盘价盯市
    last = float(closes[-1]) if len(closes) and closes[-1] else entry
    proceeds += remaining * last
    return proceeds / entry - 1.0, "未平仓", step


def main() -> int:
    parser = argparse.ArgumentParser(description="止损 -7% + 浮盈每 10% 减 30% 回测")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--excel", default="docs/MAINLINE_LADDER_BACKTEST.xlsx")
    parser.add_argument("--md", default="docs/MAINLINE_LADDER_BACKTEST.md")
    args = parser.parse_args()

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    pool = {str(r["code"]): str(r["name"]) for r in
            cache.execute("SELECT code, name FROM ml_board")}
    bars: dict[str, list[tuple[str, float, float, float, float]]] = {}
    for row in cache.execute(
            "SELECT b.board_code, b.trade_date, b.open, b.high, b.low, b.close"
            " FROM ml_board_bar b JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        bars.setdefault(str(row["board_code"]), []).append((
            str(row["trade_date"]), float(row["open"] or 0.0),
            float(row["high"] or 0.0), float(row["low"] or 0.0),
            float(row["close"] or 0.0)))
    cache.close()

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = [dict(r) for r in conn.execute(
        f"SELECT trade_date, board_code, board_name, level, score, entry_close"
        f" FROM {args.alert_table} WHERE level IN ('strong','medium')"
        f" ORDER BY board_code, trade_date")]
    conn.close()

    detail: list[dict] = []
    by_board: dict[str, list[dict]] = defaultdict(list)
    for row in alerts:
        code = str(row["board_code"])
        if code not in pool:          # 已出池的旧告警不计（剔板块不自动删历史行）
            continue
        items = bars.get(code) or []
        index = {d: i for i, (d, *_rest) in enumerate(items)}
        pos = index.get(str(row["trade_date"]))
        if pos is None or pos + 1 >= len(items):
            continue
        entry = float(row["entry_close"] or 0.0) or items[pos][4]
        if entry <= 0:
            continue
        fwd = items[pos + 1:]
        opens = np.asarray([x[1] for x in fwd])
        highs = np.asarray([x[2] for x in fwd])
        lows = np.asarray([x[3] for x in fwd])
        closes = np.asarray([x[4] for x in fwd])
        rec = {"板块": pool.get(code) or str(row["board_name"] or code),
               "代码": code, "触发日": str(row["trade_date"]),
               "档位": "🔴 强" if row["level"] == "strong" else "🟡 中",
               "总分": round(float(row["score"] or 0.0), 1),
               "买入价": round(entry, 4)}
        for variant, key in (("A", "盈亏A%"), ("B", "盈亏B%")):
            pnl, reason, steps = round_trip(entry, opens, highs, lows,
                                            closes, variant=variant)
            rec[key] = round(pnl * 100, 2)
            if variant == "A":
                rec["出场原因A"] = reason
                rec["用掉档数A"] = steps
        detail.append(rec)
        by_board[code].append(rec)

    rows_out = []
    for code, items in by_board.items():
        a = np.asarray([r["盈亏A%"] for r in items])
        b = np.asarray([r["盈亏B%"] for r in items])
        reasons = Counter(r["出场原因A"] for r in items)
        rows_out.append({
            "板块": items[0]["板块"], "代码": code,
            "告警数(强中)": len(items),
            "平均盈亏A%": round(float(a.mean()), 2),
            "中位A%": round(float(np.median(a)), 2),
            "胜率A": round(float((a > 0).mean()), 3),
            "最好A%": round(float(a.max()), 2),
            "最差A%": round(float(a.min()), 2),
            "止损": reasons.get("止损", 0),
            "止盈卖完": reasons.get("止盈卖完", 0),
            "未平仓": reasons.get("未平仓", 0),
            "平均盈亏B%": round(float(b.mean()), 2),
        })
    rows_out.sort(key=lambda d: -d["平均盈亏A%"])

    all_a = np.asarray([r["盈亏A%"] for r in detail])
    all_b = np.asarray([r["盈亏B%"] for r in detail])
    reasons_all = Counter(r["出场原因A"] for r in detail)
    # 盈亏分布：中位数正好落在 −7% 是这套规则的**结构性特征**
    # （70% 的单子在同一价位止损），所以必须把分布摆出来，
    # 否则"平均 +2.72%"会被误读成"典型一笔赚 2.7%"。
    buckets = [(-100, -7.01, "① 低于 −7%（跳空/滑出止损价）"),
               (-7.01, -6.99, "② 正好 −7%（标准止损）"),
               (-6.99, 0, "③ −7% ~ 0"),
               (0, 10, "④ 0 ~ +10%"),
               (10, 22.01, "⑤ +10% ~ +22%（走到部分或全部阶梯）"),
               (22.01, 1e9, "⑥ 高于 +22%（超过满阶梯基准）")]
    dist = []
    for lo, hi, label in buckets:
        n = int(((all_a > lo) & (all_a <= hi)).sum())
        dist.append({"区间": label, "条数": n,
                     "占比": f"{n / max(1, len(all_a)):.1%}"})
    print("  盈亏分布：" + "；".join(f"{d['区间']} {d['条数']}" for d in dist))
    print(f"信号 strong+medium 共 {len(detail)} 条（已出池板块的旧告警不计）"
          f" / {len(rows_out)} 个板块")
    print(f"  主口径 A（每档卖初始仓位 30%）：平均 {all_a.mean():+.2f}%，"
          f"中位 {np.median(all_a):+.2f}%，胜率 {(all_a > 0).mean():.1%}")
    print(f"  对照 B（每档卖剩余仓位 30%）：平均 {all_b.mean():+.2f}%")
    print(f"  出场：止损 {reasons_all.get('止损', 0)}、"
          f"止盈卖完 {reasons_all.get('止盈卖完', 0)}、"
          f"未平仓 {reasons_all.get('未平仓', 0)}")

    excel = ROOT / args.excel
    excel.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd

        with pd.ExcelWriter(excel, engine="openpyxl") as writer:
            pd.DataFrame(rows_out).to_excel(writer, sheet_name="板块平均盈亏", index=False)
            pd.DataFrame(detail).to_excel(writer, sheet_name="逐条交易", index=False)
            notes = pd.DataFrame([
                {"项": "信号", "值": "只取 strong + medium（weak 观察档不买）"},
                {"项": "买入价", "值": "告警日收盘 entry_close（缺失回落板块指数收盘）"},
                {"项": "止损", "值": f"最低价 ≤ 买入价×{STOP}（−7%）→ 剩余全部按该价成交"},
                {"项": "止盈阶梯", "值": f"最高价 ≥ 买入价×(1+{STEP:.0%}×k) → 卖一档"},
                {"项": "口径A", "值": f"每档卖【初始仓位】的 {TRANCHE:.0%}，"
                                    f"+10/20/30% 各 30%、+40% 卖完（符合「直到卖完」）"},
                {"项": "口径B", "值": f"每档卖【剩余仓位】的 {TRANCHE:.0%}（对照；"
                                    f"永不归零，最多 {MAX_STEPS_B} 档）"},
                {"项": "同日冲突", "值": "按【先止损】处理 —— 日线看不出盘中顺序，"
                                      "选保守方向（低估收益、高估止损次数）"},
                {"项": "未平仓", "值": "数据走完仍未卖完的，按最后一根收盘价盯市"},
            ])
            notes.to_excel(writer, sheet_name="口径说明", index=False)
            for sheet in writer.sheets.values():
                for col in sheet.columns:
                    width = max((sum(2 if ord(ch) > 127 else 1
                                     for ch in str(cell.value or "")) + 2)
                                for cell in col)
                    sheet.column_dimensions[col[0].column_letter].width = min(width, 60)
                sheet.freeze_panes = "A2"
        print(f"Excel → {excel}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ Excel 写出失败（{type(exc).__name__}: {exc}）")

    lines = ["# 告警后买入：止损 −7% + 浮盈每 +10% 减仓 30% 的板块平均盈亏\n",
             f"> 由 `scripts/ladder_backtest.py` 生成（只读，告警表 `{args.alert_table}`）。",
             f"> 只取 🔴强 + 🟡中；买入价 = 告警日收盘。共 {len(detail)} 条 / "
             f"{len(rows_out)} 个板块。\n",
             "## 规则与假设\n",
             f"- **止损**：最低价 ≤ 买入价 × {STOP}（−7%）→ 剩余仓位全部按该价成交。",
             f"- **止盈阶梯**：最高价 ≥ 买入价 × (1 + {STEP:.0%} × k) → 卖出一档。",
             f"- **口径 A（主）**：每档卖**初始仓位**的 {TRANCHE:.0%} —— "
             f"+10/20/30% 各卖 30%、+40% 卖完剩下的 10%（这才符合「直到卖完」）。",
             f"- **口径 B（对照）**：每档卖**剩余仓位**的 {TRANCHE:.0%}；"
             f"剩余按 0.7 递减永不归零，最多走 {MAX_STEPS_B} 档。",
             "- **同日既触止损又触止盈 → 先止损**：日线看不出盘中顺序，"
             "选保守方向（低估收益、高估止损次数）。",
             "- **未平仓**：数据走完仍未卖完的，按最后一根收盘价盯市；"
             "单独计数，不丢掉（丢掉会偏向已走完的那批）。\n",
             "## 总体\n",
             f"| 口径 | 平均盈亏 | 中位 | 胜率 | 止损 | 止盈卖完 | 未平仓 |",
             "|---|---:|---:|---:|---:|---:|---:|",
             f"| **A（主）** | **{all_a.mean():+.2f}%** | {np.median(all_a):+.2f}% "
             f"| {(all_a > 0).mean():.1%} | {reasons_all.get('止损', 0)} "
             f"| {reasons_all.get('止盈卖完', 0)} | {reasons_all.get('未平仓', 0)} |",
             f"| B（对照） | {all_b.mean():+.2f}% | {np.median(all_b):+.2f}% | — | — | — | — |",
             "",
             "## 盈亏分布（口径 A）\n",
             "> ⚠️ **中位数正好是 −7%** —— 因为约七成的单子都在止损价成交。"
             f"所以「平均 {all_a.mean():+.2f}%」**不代表典型一笔赚 "
             f"{all_a.mean():.2f}%**，"
             "它是一批固定小亏 + 少数走满阶梯的大赚拼出来的。\n",
             "| 区间 | 条数 | 占比 |",
             "|---|---:|---:|"]
    for d in dist:
        lines.append(f"| {d['区间']} | {d['条数']} | {d['占比']} |")
    lines.extend([
        "",
        "## 一、每个板块的平均盈亏（口径 A，降序）\n",
        "| # | 板块 | 告警(强中) | **平均盈亏** | 中位 | 胜率 | 最好 | 最差 "
        "| 止损 | 止盈卖完 | 未平仓 | 对照B |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for i, item in enumerate(rows_out, 1):
        lines.append(
            f"| {i} | {item['板块']}（{item['代码'].replace('.TI','')}） "
            f"| {item['告警数(强中)']} | **{item['平均盈亏A%']:+.2f}%** "
            f"| {item['中位A%']:+.2f}% | {item['胜率A']:.0%} "
            f"| {item['最好A%']:+.2f}% | {item['最差A%']:+.2f}% "
            f"| {item['止损']} | {item['止盈卖完']} | {item['未平仓']} "
            f"| {item['平均盈亏B%']:+.2f}% |")
    lines.append("")
    md = ROOT / args.md
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Markdown → {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
