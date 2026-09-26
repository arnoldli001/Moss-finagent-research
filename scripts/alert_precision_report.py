"""回测窗口的**告警准确率 / 误召回率**报告 + 人工核对表。

## 解决两个需求

**需求 A（人工审核）**：45 个真值事件与 885/886 池子对不上时，由人来看
"回测报出来的主线告警（概念 + 触发日期）到底对不对"。本脚本产出两张表：

    表一 逐条告警 + 上报后 5/10/20/60 日走势   ← 判断"这条是不是真主线"
    表二 45 个真值事件 × 窗口内是否被报出      ← 判断"该报的报了没有"

**需求 B（准确率 / 误召回率）**：只看召回不够 —— 如果每天都报十几条、
后期大多不是主线，那和"不报"一样没用。本脚本按**板块指数自身的后期走势**
给每条告警判"是不是真信号"，并汇总：

    准确率(precision)   = 判为真信号的告警 / 全部告警
    误召回率             = 1 - 准确率
    日均告警数           = 全部告警 / 有告警的天数（>10 就要警惕）

## 判"真信号"的口径（**可调，必须写清**）

一条告警算"真"要同时满足：

    上报后 20 日内最大涨幅 >= `--gain`（默认 8%）
    且 20 日收益 > 0（不是冲高回落）

为什么用板块指数而不是"是否命中 45 个真值事件"：真值只有 45 个、
且多数落在池外板块（实测只有 2 个事件有 ETF 映射），
拿它当唯一判据会把分母压到个位数，算不出有意义的比例。
**两者都要看**：前者给出统计意义上可比的准确率，后者给出方向性核对。

用法：
    .venv\\Scripts\\python.exe scripts\\alert_precision_report.py --start 20251001
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
MAIN_DB = ROOT / "data" / "moss_finagent.db"
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"
WINDOWS = (5, 10, 20, 60)


def forward_returns(bars: list[tuple[str, float]]) -> dict[int, dict[str, float]]:
    """给定某板块的 `[(date, close)]`，返回每根 K 线之后 N 日的收益与最大涨幅。"""
    out: dict[int, dict[str, float]] = {}
    closes = [value for _, value in bars]
    dates = [day for day, _ in bars]
    index_of = {day: i for i, day in enumerate(dates)}
    for day, index in index_of.items():
        entry = closes[index]
        if not entry:
            continue
        row: dict[str, float] = {}
        for window in WINDOWS:
            ahead = closes[index + 1: index + 1 + window]
            if not ahead:
                continue
            row[f"max_{window}"] = (max(ahead) / entry - 1.0) * 100.0
            row[f"ret_{window}"] = (ahead[-1] / entry - 1.0) * 100.0
        out[index] = {**row, "_date": day}
    # 转成按日期索引更好用
    return {dates[i]: value for i, value in out.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="告警准确率 / 误召回率")
    parser.add_argument("--start", default="20251001")
    parser.add_argument("--end", default="20261231")
    parser.add_argument("--gain", type=float, default=8.0,
                        help="20 日内最大涨幅达到多少算真信号（默认 8%%）")
    parser.add_argument("--report", default="docs/MAINLINE_ALERT_PRECISION.md")
    args = parser.parse_args()

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    main = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    main.row_factory = sqlite3.Row

    # ⚠️ `mainline_alert` 的列只有 alert_id/trade_date/board_code/board_name/
    # kind/level/score/gate_bonus/resonance/entry_close/max_gain_pct/
    # max_gain_date/payload/created_at —— **没有 `promoted`**，
    # 它在 `payload` 的 JSON 里；`resonance` 是 REAL（0/1）不是布尔。
    alerts = main.execute(
        "SELECT trade_date, board_code, board_name, level, score,"
        " resonance, payload FROM mainline_alert"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, board_code",
        (args.start, args.end)).fetchall()
    if not alerts:
        print(f"❌ 窗口 {args.start}~{args.end} 内 mainline_alert 没有数据"
              "（先把打分跑完：scripts/rescore_mainline.py）")
        return 2
    days = sorted({str(r["trade_date"]) for r in alerts})
    print(f"窗口 {args.start}~{args.end}：告警 {len(alerts)} 条，"
          f"分布在 {len(days)} 个交易日（{days[0]} ~ {days[-1]}）")

    # 板块指数走势（一次读齐）
    codes = sorted({str(r["board_code"]) for r in alerts})
    marks = ",".join("?" for _ in codes)
    bars: dict[str, list[tuple[str, float]]] = {}
    for row in cache.execute(
            f"SELECT board_code, trade_date, close FROM ml_board_bar"
            f" WHERE board_code IN ({marks}) ORDER BY board_code, trade_date",
            tuple(codes)):
        bars.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0)))
    fwd = {code: forward_returns(rows) for code, rows in bars.items()}

    # ---------- 逐条判定 ----------
    rows: list[dict] = []
    for alert in alerts:
        code = str(alert["board_code"])
        day = str(alert["trade_date"])
        info = fwd.get(code, {}).get(day, {})
        gain = info.get("max_20")
        ret = info.get("ret_20")
        if gain is None:
            verdict = "数据不足"
        elif gain >= args.gain and (ret or 0) > 0:
            verdict = "真信号"
        else:
            verdict = "疑似误报"
        # `promoted` 不在列里，只能从 payload 解出来；解不出按 False 处理
        promoted = False
        try:
            promoted = bool(json.loads(str(alert["payload"] or "{}"))
                            .get("promoted"))
        except (ValueError, TypeError):
            pass
        rows.append({
            "date": day, "code": code, "name": str(alert["board_name"]),
            "level": str(alert["level"]), "score": float(alert["score"] or 0),
            "promoted": promoted,
            "resonance": bool(alert["resonance"]),
            "max_5": info.get("max_5"), "ret_5": info.get("ret_5"),
            "max_10": info.get("max_10"), "ret_10": info.get("ret_10"),
            "max_20": gain, "ret_20": ret,
            "max_60": info.get("max_60"), "ret_60": info.get("ret_60"),
            "verdict": verdict})

    judged = [r for r in rows if r["verdict"] != "数据不足"]
    real = [r for r in judged if r["verdict"] == "真信号"]
    precision = len(real) / len(judged) if judged else 0.0
    per_day = Counter(r["date"] for r in rows)

    # ---------- 基准率（**没有它，准确率无法解读**） ----------
    #
    # "20% 准确率"本身说明不了任何事：如果同期**全池随机**也有 18% 的板块
    # 满足同一判据（普涨行情），那这个模型几乎没有增量信息；
    # 反过来若基准率只有 3%，20% 就是 6 倍提升。
    # 必须在**同一批交易日 × 同一批板块**上算，否则不可比。
    base_hits = 0
    base_total = 0
    pool_codes = [str(r["code"]) for r in cache.execute(
        "SELECT code FROM ml_board WHERE source = 'sector_crowding:list'")]
    for code in pool_codes:
        series = fwd.get(code)
        if not series:
            continue
        for day in days:
            info = series.get(day)
            if info is None or info.get("max_20") is None:
                continue
            base_total += 1
            if info["max_20"] >= args.gain and (info.get("ret_20") or 0) > 0:
                base_hits += 1
    base_rate = base_hits / base_total if base_total else 0.0
    lift = precision / base_rate if base_rate > 0 else float("inf")

    print()
    print("=" * 78)
    print("准确率 / 误召回率")
    print("-" * 78)
    print(f"  可判定告警        {len(judged)}（另有 {len(rows) - len(judged)} 条数据不足）")
    print(f"  真信号            {len(real)}")
    print(f"  **准确率**        {precision * 100:.1f}%")
    print(f"  **误召回率**      {(1 - precision) * 100:.1f}%"
          f"   （判据：20 日内最大涨幅 ≥{args.gain:g}% 且 20 日收益 >0）")
    print(f"  **基准率**（全池 {len(pool_codes)} 个板块 × {len(days)} 天"
          f"随机取样 {base_total} 次） {base_rate * 100:.1f}%")
    print(f"  **提升倍数**      {lift:.2f}x"
          f"   ← 这才是「模型有没有用」的判据")
    print(f"  有告警的交易日    {len(days)}")
    print(f"  日均告警          {len(rows) / max(1, len(days)):.1f} 条/天"
          f"   最多 {max(per_day.values())} 条（{per_day.most_common(1)[0][0]}）")
    for level in ("strong", "medium", "weak"):
        subset = [r for r in judged if r["level"] == level]
        if subset:
            hit = sum(1 for r in subset if r["verdict"] == "真信号")
            print(f"     {level:<8}{hit:>4}/{len(subset):<4}"
                  f"{hit / len(subset) * 100:>6.1f}%")
    for flag, label in ((True, "提名"), (False, "非提名")):
        subset = [r for r in judged if r["promoted"] is flag]
        if subset:
            hit = sum(1 for r in subset if r["verdict"] == "真信号")
            print(f"     {label:<8}{hit:>4}/{len(subset):<4}"
                  f"{hit / len(subset) * 100:>6.1f}%")

    # ---------- 真值事件核对 ----------
    truth_rows: list[str] = []
    try:
        import yaml
        raw = yaml.safe_load(TRUTH.read_text(encoding="utf-8"))
        events = [e for e in (raw.get("events") or [])
                  if str(e.get("status")) == "ok"]
        pool = {str(r["code"]) for r in cache.execute(
            "SELECT code FROM ml_board WHERE source = 'sector_crowding:list'")}
        name_to_code = {str(r["name"]): str(r["code"]) for r in cache.execute(
            "SELECT code, name FROM ml_board"
            " WHERE source = 'sector_crowding:list'")}
        alerted = {(str(r["board_code"]), str(r["trade_date"])) for r in alerts}
        print()
        print("=" * 78)
        print("真值事件核对（status=ok，且真值日落在窗口内）")
        print("-" * 78)
        matched = 0
        considered = 0
        for e in events:
            day = str(e.get("date") or "").replace("-", "")
            # ⚠️ 必须**按窗口过滤**：真值文件覆盖 2023-01 起，
            # 不过滤会把窗口外的事件也算成"未报出"，把命中率压成假的 0%。
            if not (args.start <= day <= args.end):
                continue
            considered += 1
            codes = []
            for item in (e.get("codes") or []):
                text = str(item)
                if text in pool:
                    codes.append(text)
                elif text in name_to_code:
                    codes.append(name_to_code[text])
            if not codes:
                truth_rows.append(f"| {day} | {e.get('label')} | ❌ 板块不在池内 | — |")
                continue
            # 命中判定：该板块在真值日起 **45 个自然日内**有告警。
            # ⚠️ 不能用 `int(day) + 30` 比较 —— `20251220+30 = 20251250`
            # 不是合法日期，跨月时会算错（这是本脚本第一版的 bug）。
            from datetime import datetime, timedelta
            try:
                begin = datetime.strptime(day, "%Y%m%d")
            except ValueError:
                truth_rows.append(f"| {day} | {e.get('label')} | ⚠️ 日期无法解析 | — |")
                continue
            limit = (begin + timedelta(days=45)).strftime("%Y%m%d")
            hit = None
            for code in codes:
                for other, other_day in alerted:
                    if other == code and day <= other_day <= limit:
                        hit = other_day
                        break
                if hit:
                    break
            if hit:
                matched += 1
            truth_rows.append(
                f"| {day} | {e.get('label')} | {'✅ 已报出' if hit else '❌ 未报出'}"
                f" | {hit or '—'} |")
        print(f"  窗口内 status=ok 事件：{considered} 个，"
              f"其中已被报出 **{matched}** 个"
              f"（{matched / max(1, considered) * 100:.0f}%）")
        print("  注意：多数真值事件的板块**不在 324 池内**"
              "（引用的是已剔除的 875xxx 申万代码），")
        print("  这类事件模型永远不可能命中 —— 命中率低不等于模型差，"
              "要先看有多少事件是可命中的。")
    except Exception as exc:  # noqa: BLE001 真值文件坏了不该让报告失败
        print(f"  （真值核对跳过：{type(exc).__name__}: {exc}）")

    # ---------- 判据稳健性：换几把尺子看提升倍数是否稳定 ----------
    #
    # 单看一个判据（20 日 ≥8%）容易自我说服。若提升倍数在几把尺子下都接近 1，
    # 那结论是"模型没有增量信息"，而不是"判据选得不好"。
    print()
    print("=" * 78)
    print("判据稳健性（提升倍数 = 告警准确率 / 同判据下的全池基准率）")
    print("-" * 78)
    print(f"  {'判据':<26}{'告警准确率':>12}{'基准率':>10}{'提升':>8}")
    criteria = [
        ("20日最大涨幅 ≥8% 且收益>0", lambda i: (i.get("max_20") or 0) >= 8
         and (i.get("ret_20") or 0) > 0),
        ("20日收益 >0", lambda i: (i.get("ret_20") or 0) > 0),
        ("20日最大涨幅 ≥15%", lambda i: (i.get("max_20") or 0) >= 15),
        ("20日最大涨幅 ≥25%", lambda i: (i.get("max_20") or 0) >= 25),
    ]
    sweep: list[str] = []
    for label, test in criteria:
        hit = sum(1 for code in pool_codes
                  for day in days
                  if (fwd.get(code, {}).get(day) or {}).get("max_20") is not None
                  and test(fwd[code][day]))
        total = sum(1 for code in pool_codes for day in days
                    if (fwd.get(code, {}).get(day) or {}).get("max_20")
                    is not None)
        base = hit / total if total else 0.0
        a_hit = sum(1 for r in rows
                    if (fwd.get(r["code"], {}).get(r["date"]) or {}) and
                    test(fwd[r["code"]][r["date"]]))
        a_total = sum(1 for r in rows
                      if (fwd.get(r["code"], {}).get(r["date"]) or {})
                      .get("max_20") is not None)
        acc = a_hit / a_total if a_total else 0.0
        print(f"  {label:<26}{acc * 100:>11.1f}%{base * 100:>9.1f}%"
              f"{(acc / base if base else 0):>7.2f}x")
        sweep.append(f"| {label} | {acc * 100:.1f}% | {base * 100:.1f}% "
                     f"| {(acc / base if base else 0):.2f}x |")

    # ---------- 写报告 ----------
    out = [f"# 主线告警 准确率 / 误召回率报告（{args.start} ~ {args.end}）\n",
           f"- 告警 {len(rows)} 条 / {len(days)} 个交易日\n",
           f"- **准确率 {precision * 100:.1f}%**，"
           f"**误召回率 {(1 - precision) * 100:.1f}%**"
           f"（判据：20 日内最大涨幅 ≥{args.gain:g}% 且 20 日收益 >0）\n",
           f"- **基准率 {base_rate * 100:.1f}%**、**提升 {lift:.2f}x**"
           f"（同一批交易日 × 全池 {len(pool_codes)} 个板块随机取样）\n",
           f"- 日均告警 {len(rows) / max(1, len(days)):.1f} 条\n\n",
           "> 提升倍数才是「模型有没有用」的判据：普涨行情里全池随机也可能有\n"
           "> 百分之十几满足同一判据，单看准确率会严重高估。\n\n",
           "## 一、真值事件核对（人工复核用）\n\n",
           "| 真值日 | 标签 | 是否报出 | 实际告警日 |", "|---|---|---|---|"]
    out.extend(truth_rows)
    out.append("\n## 二、逐条告警 + 后期走势（人工复核用）\n")
    out.append("| 触发日 | 板块 | 等级 | 总分 | 提名 | 20日最大涨 | 20日收益 "
               "| 60日最大涨 | 判定 |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: x["date"]):
        fmt = lambda v: "—" if v is None else f"{v:+.2f}%"  # noqa: E731
        out.append(f"| {r['date']} | {r['code']} {r['name']} | {r['level']} "
                   f"| {r['score']:.1f} | {'是' if r['promoted'] else ''} "
                   f"| {fmt(r['max_20'])} | {fmt(r['ret_20'])} "
                   f"| {fmt(r['max_60'])} | {r['verdict']} |")
    target = ROOT / args.report
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\n报告 → {target}")
    cache.close()
    main.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
