"""导出**每个板块每次触发买入告警的时间** + **命中与否** + **20 日赚亏**。

## 用户要什么

> 「请告诉我每个板块 每次触发买入告警的时间，输出文件」
> 「要区分 命中和没命中 20 日亏钱还是赚钱」

所以每一条告警要落到**四象限**里的一个格：

                 20 日赚钱        20 日亏钱
    命中主线      ① 命中且赚       ② 命中但亏
    没命中主线    ③ 误报但赚       ④ 误报且亏

## 两个口径，**必须分开看**（这是本项目反复强调的一条）

    **命中**：启动集判据 —— 告警落在某个"启动日 ±[−10, +5] 个交易日"内。
              启动日 = 板块指数滚动 20/35 日涨幅 > 15% 且命中后跳 4 个交易日。
              ⚠️ 它回答的是"有没有走出**主线级**行情"，**不是**"有没有赚钱"。
    **赚亏**：以告警日收盘为买入价、看未来 **20 个交易日**的收益（板块指数价）。

**两者会背离，而且背离得有信息量**：中小波段板块的告警常被判"误报"，
但它其实是赚钱的（环氧丙烷高分档就是：未来 20 日 +1.37% / +9.00%，
却在启动集口径下算误报）。用户 2026-09-22 明确要求按
「**先赚钱口径判、留下误报但不亏钱的；剩余的按主线口径判**」处置 ——
所以这份表把两个口径并列，**不下"该不该砍"的结论**。

## 三个 sheet

    ① 板块汇总     每个板块：告警数 / 命中 / 误报 / 误报率 / 四象限计数
    ② 每板块时间轴  命中时间轴 与 误报时间轴 分开两列 + 四象限各自的时间串
    ③ 逐条明细     一行一次告警：触发日 / 档位 / 总分 / 判定 / 20日收益 / 结果

## ⚠️ 默认只导出**当前池内**的板块

剔板块不会自动删历史分数行（要等下一次 `rescore --force`），
所以现表里可能还留着已出池板块的旧告警。默认按 `ml_board` 过滤。

只读，除写出文件外不改任何东西。

用法：
    .venv\\Scripts\\python.exe scripts/export_board_alerts.py
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

LEVEL_TEXT = {"strong": "🔴 强", "medium": "🟡 中", "weak": "🟢 弱", "none": "—"}
LEAD_OK, LAG_OK = 10, 5
BIG = 0.15
HORIZON = 20


def scan_labels(close: np.ndarray) -> set[int]:
    """启动集：滚动 20/35 日涨幅 > 15% 的日子，命中后跳 4 个交易日。"""
    labels: set[int] = set()
    n = len(close)
    for window in (20, 35):
        if n <= window + 1:
            continue
        rolling = np.full(n, np.nan)
        rolling[window:] = close[window:] / close[:-window] - 1.0
        pos = window
        while pos < n:
            if np.isfinite(rolling[pos]) and rolling[pos] > BIG:
                labels.add(pos)
                pos += 4
            else:
                pos += 1
    return labels


def main() -> int:
    parser = argparse.ArgumentParser(description="导出各板块告警触发时间 + 命中 + 20日赚亏")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--excel", default="docs/MAINLINE_BOARD_ALERT_TIMES.xlsx")
    parser.add_argument("--md", default="docs/MAINLINE_BOARD_ALERT_TIMES.md")
    parser.add_argument("--include-out-of-pool", action="store_true")
    args = parser.parse_args()

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    pool = {str(r["code"]): str(r["name"]) for r in
            cache.execute("SELECT code, name FROM ml_board")}
    bars: dict[str, list[tuple[str, float]]] = {}
    for row in cache.execute(
            "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        bars.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    cache.close()

    # 预先算好每个板块的启动集覆盖区间
    prepared: dict[str, dict] = {}
    for code, items in bars.items():
        days = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        labels = scan_labels(close)
        prepared[code] = {
            "index": {d: i for i, d in enumerate(days)},
            "close": close,
            "covered": {t for i in labels
                        for t in range(max(0, i - LEAD_OK), i + LAG_OK + 1)}}

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        f"SELECT trade_date, board_code, board_name, level, score,"
        f" entry_close, resonance FROM {args.alert_table}"
        f" ORDER BY board_code, trade_date")]
    conn.close()

    dropped = Counter()
    if not args.include_out_of_pool:
        kept = []
        for row in rows:
            if str(row["board_code"]) in pool:
                kept.append(row)
            else:
                dropped[str(row["board_code"])] += 1
        rows = kept

    # ---------- 逐条判定 ----------
    detail_rows: list[dict] = []
    skipped_no_data = 0
    by_board: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        code = str(row["board_code"])
        item = prepared.get(code)
        day = str(row["trade_date"])
        entry = None
        forward = None
        mae = None
        hit = None
        if item is not None and day in item["index"]:
            pos = item["index"][day]
            close = item["close"]
            entry = float(row["entry_close"] or 0.0) or float(close[pos])
            hit = pos in item["covered"]
            if entry > 0:
                if pos + HORIZON < len(close):
                    forward = float(close[pos + HORIZON] / entry - 1.0)
                window = close[pos + 1: pos + 1 + HORIZON]
                if window.size:
                    mae = float(window.min() / entry - 1.0)
        if hit is None:
            skipped_no_data += 1
        rec = {
            "板块": pool.get(code) or str(row["board_name"] or code),
            "代码": code, "触发日": day,
            "档位": LEVEL_TEXT.get(str(row["level"]), str(row["level"])),
            "总分": round(float(row["score"] or 0.0), 1),
            "龙头共振": "是" if row["resonance"] else "",
            "判定": ("命中" if hit else "误报") if hit is not None else "无数据",
            "20日收益": None if forward is None else round(forward * 100, 2),
            "20日最大浮亏": None if mae is None else round(mae * 100, 2),
        }
        if forward is None:
            rec["结果"] = "—"
            rec["四象限"] = "无数据"
        else:
            win = "赚" if forward > 0 else "亏"
            rec["结果"] = f"{win} {forward * 100:+.2f}%"
            rec["四象限"] = (("① 命中且赚" if forward > 0 else "② 命中但亏")
                           if hit else
                           ("③ 误报但赚" if forward > 0 else "④ 误报且亏"))
        detail_rows.append(rec)
        by_board[code].append(rec)

    # ---------- 每板块汇总 ----------
    axis_rows, summary_rows = [], []
    for code, items in by_board.items():
        items.sort(key=lambda r: str(r["触发日"]))
        name = items[0]["板块"]
        levels = Counter(r["档位"] for r in items)
        quad = Counter(r["四象限"] for r in items)
        judged = [r for r in items if r["判定"] in ("命中", "误报")]
        tp = sum(1 for r in judged if r["判定"] == "命中")
        fp = sum(1 for r in judged if r["判定"] == "误报")
        rets = [r["20日收益"] for r in items if r["20日收益"] is not None]
        wins = [v for v in rets if v > 0]
        losses = [v for v in rets if v <= 0]
        # 盈亏比 = 平均盈利 / |平均亏损|（经典定义）。
        # ⚠️ 第一版写成了 "盈利比例 / 亏损比例"，那不是盈亏比；而且
        # 在某一侧为空时 `np.mean([])` 会抛 RuntimeWarning 并返回 nan。
        ratio = (round(float(np.mean(wins)) / abs(float(np.mean(losses))), 2)
                 if wins and losses else None)
        summary_rows.append({
            "板块": name, "代码": code, "告警次数": len(items),
            "强": levels.get("🔴 强", 0), "中": levels.get("🟡 中", 0),
            "弱": levels.get("🟢 弱", 0),
            "命中": tp, "误报": fp,
            "误报率": (round(fp / (tp + fp), 3) if tp + fp else None),
            "①命中且赚": quad.get("① 命中且赚", 0),
            "②命中但亏": quad.get("② 命中但亏", 0),
            "③误报但赚": quad.get("③ 误报但赚", 0),
            "④误报且亏": quad.get("④ 误报且亏", 0),
            "20日均值%": (round(float(np.mean(rets)), 2) if rets else None),
            "胜率": (round(len(wins) / len(rets), 3) if rets else None),
            "盈亏比": ratio,
            "首次": items[0]["触发日"], "末次": items[-1]["触发日"]})

        def timeline(kind: str, rows_in: list[dict] = items) -> str:
            # ⚠️ 必须把 `items` 绑成默认参数：直接闭包引用循环变量会被
            # ruff B023 抓出来（函数对象在循环结束后才被调用，那时
            # `items` 已经指向下一个板块 —— 时间轴会**整列错位**）。
            picked = [r for r in rows_in if r["四象限"] == kind]
            return "、".join(f"{r['触发日']}" for r in picked) or "—"

        axis_rows.append({
            "板块": name, "代码": code, "告警次数": len(items),
            "命中": tp, "误报": fp,
            "①命中且赚": timeline("① 命中且赚"),
            "②命中但亏": timeline("② 命中但亏"),
            "③误报但赚": timeline("③ 误报但赚"),
            "④误报且亏": timeline("④ 误报且亏"),
        })

    total_quad = Counter(r["四象限"] for r in detail_rows)
    print(f"告警 {len(detail_rows)} 条 / {len(by_board)} 个板块"
          + (f"；{skipped_no_data} 条在板块指数里找不到对应交易日" if skipped_no_data else ""))
    if dropped:
        print(f"  （已过滤 {sum(dropped.values())} 条属于**已出池**板块的旧告警，"
              f"涉及 {len(dropped)} 个板块）")
    print(f"  四象限：① 命中且赚 {total_quad.get('① 命中且赚', 0)}，"
          f"② 命中但亏 {total_quad.get('② 命中但亏', 0)}，"
          f"③ 误报但赚 {total_quad.get('③ 误报但赚', 0)}，"
          f"④ 误报且亏 {total_quad.get('④ 误报且亏', 0)}")

    # ---------- Excel ----------
    excel = ROOT / args.excel
    excel.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd

        summary_df = pd.DataFrame(summary_rows).sort_values(
            ["告警次数", "代码"], ascending=[False, True])
        axis_df = pd.DataFrame(axis_rows).sort_values(
            ["告警次数", "代码"], ascending=[False, True])
        detail_df = pd.DataFrame(detail_rows).sort_values(
            ["板块", "触发日"])
        with pd.ExcelWriter(excel, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="板块汇总", index=False)
            axis_df.to_excel(writer, sheet_name="每板块时间轴", index=False)
            detail_df.to_excel(writer, sheet_name="逐条明细", index=False)
            for sheet in writer.sheets.values():
                for col in sheet.columns:
                    width = max((sum(2 if ord(ch) > 127 else 1
                                     for ch in str(cell.value or "")) + 2)
                                for cell in col)
                    sheet.column_dimensions[
                        col[0].column_letter].width = min(width, 64)
                sheet.freeze_panes = "A2"
        print(f"Excel → {excel}")
    except Exception as exc:  # noqa: BLE001 缺 openpyxl 时仍要出 Markdown
        print(f"⚠️ Excel 写出失败（{type(exc).__name__}: {exc}），只出 Markdown")

    # ---------- Markdown ----------
    lines: list[str] = []
    lines.append("# 各板块「买入告警」触发时间 × 命中判定 × 20 日赚亏\n")
    lines.append(f"> 由 `scripts/export_board_alerts.py` 生成（只读，告警表 "
                 f"`{args.alert_table}`）。")
    if not args.include_out_of_pool:
        lines.append(f"> 已按**当前在池板块**过滤"
                     f"（剔除 {sum(dropped.values())} 条属于已出池板块的旧告警）。")
    lines.append(f"> 共 {len(detail_rows)} 条告警 / {len(by_board)} 个板块。\n")
    lines.append("## 口径（两个判据**必须分开看**）\n")
    lines.append("- **命中**：启动集判据 —— 告警落在某启动日 ±[−10, +5] 个交易日内；"
                 "启动日 = 板块指数滚动 20/35 日涨幅 > 15%、命中后跳 4 个交易日。"
                 "**它问的是「有没有走出主线级行情」，不是「有没有赚钱」**。")
    lines.append("- **赚亏**：告警日收盘买入，未来 **20 个交易日**的收益（板块指数价）。\n")
    lines.append("| 四象限 | 条数 |")
    lines.append("|---|---:|")
    for key in ("① 命中且赚", "② 命中但亏", "③ 误报但赚", "④ 误报且亏", "无数据"):
        lines.append(f"| {key} | {total_quad.get(key, 0)} |")
    lines.append("")
    lines.append("> ⚠️ 「**误报但赚**」这一格是用户 2026-09-22 特别要看的："
                 "启动集口径判它「没走出主线」，但它其实是赚钱的 ——"
                 "按「先赚钱口径、再主线口径」的处置顺序，这一格不该一刀砍掉。\n")
    lines.append("## 一、板块汇总（按告警次数降序）\n")
    lines.append("| # | 板块 | 告警 | 强 | 中 | 弱 | 命中 | 误报 | 误报率 "
                 "| ①命中且赚 | ②命中但亏 | ③误报但赚 | ④误报且亏 | 20日均值 |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, item in enumerate(summary_df.to_dict("records"), 1):
        rate = "—" if item["误报率"] is None else f"{item['误报率']:.0%}"
        mean = "—" if item["20日均值%"] is None else f"{item['20日均值%']:+.2f}%"
        lines.append(
            f"| {i} | {item['板块']}（{item['代码'].replace('.TI','')}） "
            f"| {item['告警次数']} | {item['强']} | {item['中']} | {item['弱']} "
            f"| {item['命中']} | {item['误报']} | {rate} "
            f"| {item['①命中且赚']} | {item['②命中但亏']} "
            f"| {item['③误报但赚']} | {item['④误报且亏']} | {mean} |")
    lines.append("")
    lines.append("## 二、四象限的时间轴（每条告警落在哪一格，逐日可查）\n")
    for item in axis_df.to_dict("records"):
        lines.append(f"### {item['板块']}（{item['代码'].replace('.TI','')}）"
                     f" —— 告警 {item['告警次数']} 条，命中 {item['命中']} / "
                     f"误报 {item['误报']}")
        lines.append("")
        for key in ("①命中且赚", "②命中但亏", "③误报但赚", "④误报且亏"):
            lines.append(f"- **{key}**：{item[key]}")
        lines.append("")
    md = ROOT / args.md
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Markdown → {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
