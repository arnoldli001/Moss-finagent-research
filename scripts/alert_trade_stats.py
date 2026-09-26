"""告警的**交易视角**统计 + 导出 Excel。

## 口径（用户指定的）

1. **误报判据**：上报后 **20 日收益 > 2%** 就算命中（不再要求"最大涨幅 ≥8%
   且收益 >0"）。判据可用 `--win` 调。
2. **未检测出的只算漏报，不作为失败** —— 漏报不进"失败"分母，
   只单独列出（`真值事件` 表）。
3. **要统计的量**（针对"告警报出来后买入"）：
   - **收益率**：盈利样本的平均/中位收益
   - **亏损率**：亏损样本的平均/中位亏损
   - **盈亏比** = 平均收益率 / |平均亏损率|
   - **胜率**、**期望收益**
   - **最大回撤**：持有期内相对买入价的最大不利偏移（`min(close_t/entry-1)`）
   - **最大涨幅**：持有期内相对买入价的最大有利偏移
4. 持有期取 10 / 20 / 30 / 40 个交易日（约 0.5 / 1 / 1.5 / 2 个月）。

## 为什么基准要过滤稀疏板块

`ml_board_bar` 有 2243 个板块，**1892 个覆盖率 <90%**（池外的 861xxx/875xxx
等）。稀疏板块上 `shift(-20)` 取到的是"下一个有数据的观测"、可能隔几个月，
20 日收益的 p99.9 达 +649%，会把基准均值拉到 199%~684%。
所以基准限定为**池内 + 覆盖 ≥90%**（291 个板块）。

用法：
    .venv\\Scripts\\python.exe scripts\\alert_trade_stats.py
    .venv\\Scripts\\python.exe scripts\\alert_trade_stats.py --win 2 --out docs/x.xlsx
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
MAIN_DB = ROOT / "data" / "moss_finagent.db"
HOLDS = (10, 20, 30, 40, 60, 90)
#: 「收益 ≥ 阈值」的概率表：直接回答"持有 N 日赚到 X% 的概率有多大"。
#: 用户实测：20 日 ≥7% 约 17%、60 日 ≥7% 约 48% —— 说明信号偏**早**，
#: 20 日窗口太短，必须把 60/90 日一并算出来。
THRESHOLDS = (0.0, 2.0, 5.0, 7.0, 10.0, 15.0, 20.0)
TRUTH = ROOT / "configs" / "mainline_ground_truth.yaml"


def load_universe(start: str, end: str, min_coverage: float
                  ) -> tuple[pd.DataFrame, int]:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    pool = {str(r["code"]) for r in conn.execute(
        "SELECT code FROM ml_board WHERE source = 'sector_crowding:list'")}
    rows = conn.execute(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    wide = pd.DataFrame(
        [{"b": str(r["board_code"]), "d": str(r["trade_date"]),
          "c": float(r["close"] or 0)} for r in rows]
    ).pivot(index="d", columns="b", values="c").sort_index()
    coverage = wide.notna().mean()
    keep = [c for c in wide.columns
            if c in pool and coverage.get(c, 0.0) >= min_coverage]
    return wide[keep], wide.shape[1] - len(keep)


def _plus_days(day: str, days: int) -> str:
    """`YYYYMMDD` 加 N 个自然日（不能用 `int(day)+N` —— 20251220+45=20251265
    不是合法日期，跨月会算错）。"""
    from datetime import datetime, timedelta
    try:
        return (datetime.strptime(day, "%Y%m%d")
                + timedelta(days=days)).strftime("%Y%m%d")
    except ValueError:
        return day


def trade_frame(alerts: list[sqlite3.Row], wide: pd.DataFrame,
                win_threshold: float) -> pd.DataFrame:
    """逐条告警算出持有的收益率 / 最大涨幅 / 最大回撤。"""
    values = wide.to_numpy(dtype=float)
    col_of = {code: i for i, code in enumerate(wide.columns)}
    pos_of = {day: i for i, day in enumerate(wide.index)}
    total_days = len(wide.index)
    out: list[dict] = []
    for alert in alerts:
        code, day = str(alert["board_code"]), str(alert["trade_date"])
        i, j = pos_of.get(day), col_of.get(code)
        if i is None or j is None:
            continue                       # 不在池内 / 行情太稀疏
        entry = values[i, j]
        if not np.isfinite(entry) or entry <= 0:
            continue
        row: dict = {"触发日": day, "板块代码": code,
                     "板块名称": str(alert["board_name"]),
                     "等级": str(alert["level"]),
                     "总分": round(float(alert["score"] or 0), 2),
                     "买入价": round(float(entry), 4)}
        for hold in HOLDS:
            end_index = i + hold
            segment = values[i + 1: end_index + 1, j]
            # 最大涨幅 / 最大回撤：用区间内**有限的那些点**，不要求全区间完整
            finite = segment[np.isfinite(segment)] if segment.size else segment
            if finite.size:
                relative = finite / entry - 1.0
                row[f"最大涨幅{hold}"] = relative.max() * 100
                # 最大回撤：持有期内相对买入价的最大**不利**偏移（负数或 0）
                row[f"最大回撤{hold}"] = min(relative.min(), 0.0) * 100
            else:
                row[f"最大涨幅{hold}"] = np.nan
                row[f"最大回撤{hold}"] = np.nan
            # ⚠️ 收益**只需要端点**：`close[T+H]/close[T]`。
            # 第一版额外要求"区间内每一天都有限"，结果 60 日样本从
            # 理论 6419 掉到 1173（覆盖率 90% 的板块上，60 天全齐的概率很低）
            # —— 这是我自己的 bug，凭空砍掉 80% 样本，还让长持有期的
            # 统计只代表"数据最完整的那些板块"，产生选择偏差。
            if end_index < total_days and np.isfinite(values[end_index, j]):
                row[f"收益{hold}"] = (values[end_index, j] / entry - 1.0) * 100
            else:
                row[f"收益{hold}"] = np.nan
        out.append(row)
    frame = pd.DataFrame(out)
    frame["是否盈利"] = frame["收益20"] > win_threshold
    return frame


def summarize(frame: pd.DataFrame, hold: int, base: pd.DataFrame,
              win_threshold: float) -> dict:
    """一个持有期的交易统计。"""
    ret = frame[f"收益{hold}"].dropna()
    gain = frame[f"最大涨幅{hold}"].dropna()
    dd = frame[f"最大回撤{hold}"].dropna()
    wins = ret[ret > win_threshold]
    losses = ret[ret <= win_threshold]
    mean_win = float(wins.mean()) if len(wins) else 0.0
    mean_loss = float(losses.mean()) if len(losses) else 0.0
    win_rate = len(wins) / len(ret) if len(ret) else 0.0
    base_ret = base[f"基准收益{hold}"].dropna() if f"基准收益{hold}" in base \
        else pd.Series(dtype=float)
    return {
        "持有期(交易日)": hold,
        "样本数": len(ret),
        "胜率": round(win_rate * 100, 2),
        "平均收益率": round(mean_win, 2),
        "平均亏损率": round(mean_loss, 2),
        "盈亏比": round(mean_win / abs(mean_loss), 2) if mean_loss else np.nan,
        "期望收益": round(win_rate * mean_win + (1 - win_rate) * mean_loss, 2),
        "全样本均值": round(float(ret.mean()), 2) if len(ret) else np.nan,
        "全样本中位数": round(float(ret.median()), 2) if len(ret) else np.nan,
        "平均最大涨幅": round(float(gain.mean()), 2) if len(gain) else np.nan,
        "平均最大回撤": round(float(dd.mean()), 2) if len(dd) else np.nan,
        "最差单笔回撤": round(float(dd.min()), 2) if len(dd) else np.nan,
        "基准均值": (round(float(base_ret.mean()), 2)
                     if len(base_ret) else np.nan),
        "基准胜率": (round(float((base_ret > win_threshold).mean()) * 100, 2)
                     if len(base_ret) else np.nan),
    }


def baseline(wide: pd.DataFrame, alert_days: set[str],
             win_threshold: float) -> pd.DataFrame:
    """基准：同一批交易日 × 池内全部板块的持有收益。"""
    rows: list[dict] = []
    values = wide.to_numpy(dtype=float)
    pos_of = {day: i for i, day in enumerate(wide.index)}
    for day in sorted(alert_days):
        i = pos_of.get(day)
        if i is None:
            continue
        row: dict = {}
        for hold in HOLDS:
            segment = values[i + 1: i + 1 + hold, :]
            if segment.shape[0] < hold:
                row[f"基准收益{hold}"] = np.nan
                continue
            first = segment[0, :]
            last = segment[-1, :]
            valid = np.isfinite(first) & np.isfinite(last) & (first > 0)
            if not valid.any():
                row[f"基准收益{hold}"] = np.nan
                continue
            row[f"基准收益{hold}"] = float(
                np.mean(last[valid] / first[valid] - 1.0)) * 100
        rows.append(row)
    return pd.DataFrame(rows)


def truth_sheet(alerts: list[sqlite3.Row], window: int = 45) -> pd.DataFrame:
    """真值事件：已报出 / **漏报**（漏报只统计，不作为失败）。

    ⚠️ 命中判定**必须有时间上界**。第一版写成"真值日之后任意一天有告警"，
    那样只要该板块在整个三年里被报过一次就算命中 —— 几乎必然全中，
    数字毫无意义。这里限定为真值日起 `window` 个自然日内。
    """
    try:
        import yaml
    except ImportError:
        return pd.DataFrame()
    raw = yaml.safe_load(TRUTH.read_text(encoding="utf-8"))
    alerted = {(str(a["board_code"]), str(a["trade_date"])) for a in alerts}
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    pool = {str(r["code"]): str(r["name"]) for r in conn.execute(
        "SELECT code, name FROM ml_board WHERE source = 'sector_crowding:list'")}
    name_to_code = {name: code for code, name in pool.items()}
    conn.close()
    out: list[dict] = []
    for event in (raw.get("events") or []):
        day = str(event.get("date") or "").replace("-", "")
        codes = []
        for item in (event.get("codes") or []):
            text = str(item)
            if text in pool:
                codes.append(text)
            elif text in name_to_code:
                codes.append(name_to_code[text])
        hit_days = sorted({d for code, d in alerted
                           if code in codes and day <= d <= _plus_days(day, window)})
        out.append({
            "真值日": day,
            "标签": str(event.get("label")),
            "状态": str(event.get("status")),
            "映射板块": "、".join(codes),
            "结论": ("已报出" if hit_days else
                     ("漏报（板块不在池内，模型不可能命中）" if not codes
                      else "漏报")),
            "实际告警日": "、".join(hit_days[:3]),
        })
    return pd.DataFrame(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="告警交易统计 + Excel")
    parser.add_argument("--start", default="20231009")
    parser.add_argument("--end", default="20260918")
    parser.add_argument("--win", type=float, default=2.0,
                        help="20 日收益超过多少算命中（%%），默认 2")
    parser.add_argument("--min-coverage", type=float, default=0.9)
    parser.add_argument("--out", default="docs/MAINLINE_ALERT_TRADES.xlsx")
    args = parser.parse_args()

    wide, dropped = load_universe(args.start, args.end, args.min_coverage)
    print(f"可比universe：池内且覆盖率 ≥{args.min_coverage:.0%} 的板块 "
          f"{wide.shape[1]} 个（剔除 {dropped} 个稀疏/池外板块）")

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = conn.execute(
        "SELECT trade_date, board_code, board_name, level, score"
        " FROM mainline_alert WHERE trade_date BETWEEN ? AND ?"
        " ORDER BY trade_date", (args.start, args.end)).fetchall()
    conn.close()
    print(f"告警 {len(alerts)} 条")

    frame = trade_frame(alerts, wide, args.win)
    used = frame["收益20"].notna().sum()
    print(f"可统计 {len(frame)} 条（其中 20 日收益可得 {used} 条）")
    base = baseline(wide, set(frame["触发日"]), args.win)

    summary = pd.DataFrame([summarize(frame, h, base, args.win)
                            for h in HOLDS])
    print()
    print(summary.to_string(index=False))

    # 分档 / 分年
    by_level = pd.DataFrame([
        dict(等级=level, **summarize(frame[frame["等级"] == level], 20,
                                     base, args.win))
        for level in ("strong", "medium", "weak")
        if (frame["等级"] == level).any()])
    # 命中概率表：持有 N 日、收益 ≥ 阈值 的比例。
    # 用来回答"信号提醒得早不早、要拿多久"。
    prob_rows: list[dict] = []
    for hold in HOLDS:
        column = frame[f"收益{hold}"].dropna()
        if column.empty:
            continue
        row = {"持有期(交易日)": hold, "样本数": len(column)}
        for threshold in THRESHOLDS:
            row[f"≥{threshold:g}%"] = round(
                float((column >= threshold).mean()) * 100, 1)
        row["均值"] = round(float(column.mean()), 2)
        row["中位数"] = round(float(column.median()), 2)
        prob_rows.append(row)
    prob = pd.DataFrame(prob_rows)
    frame["年"] = frame["触发日"].str[:4]
    by_year = pd.DataFrame([
        dict(年=year, **summarize(frame[frame["年"] == year], 20,
                                  base, args.win))
        for year in sorted(frame["年"].unique())])
    truth = truth_sheet(alerts)

    notes = pd.DataFrame({
        "口径": [
            "误报判据", "漏报处理", "买入价", "持有期",
            "最大回撤", "最大涨幅", "盈亏比", "基准",
            "可比universe", "代价",
        ],
        "说明": [
            f"20 日收益 > {args.win:g}% 算命中（用户指定）",
            "未检测出的只算**漏报**，不作为失败，单独列在「真值事件」表",
            "告警当日收盘价",
            "10 / 20 / 30 / 40 个交易日（约 0.5 / 1 / 1.5 / 2 个月）",
            "持有期内 min(收盘/买入价-1)，即相对买入价的最大不利偏移",
            "持有期内 max(收盘/买入价-1)，即最大有利偏移",
            "平均收益率 / |平均亏损率|",
            f"同一批交易日 × 池内 {wide.shape[1]} 个板块的等权持有收益",
            f"池内且覆盖率 ≥{args.min_coverage:.0%}；"
            f"剔除 {dropped} 个稀疏/池外板块（否则基准被拉到几百 %）",
            "所有数字都是**同一样本内**的；样本外验证（Purged Walk-Forward）未做",
        ]})

    target = ROOT / args.out
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="汇总", index=False)
        prob.to_excel(writer, sheet_name="命中概率", index=False)
        by_level.to_excel(writer, sheet_name="分档统计", index=False)
        by_year.to_excel(writer, sheet_name="分年统计", index=False)
        frame.to_excel(writer, sheet_name="告警明细", index=False)
        base.to_excel(writer, sheet_name="基准", index=False)
        truth.to_excel(writer, sheet_name="真值事件", index=False)
        notes.to_excel(writer, sheet_name="口径说明", index=False)
        for sheet in writer.book.worksheets:
            for column in sheet.columns:
                width = max((len(str(cell.value)) for cell in column
                             if cell.value is not None), default=8)
                sheet.column_dimensions[column[0].column_letter].width = \
                    min(max(width + 2, 10), 40)
    print(f"\nExcel → {target}")
    print("   工作表：汇总 / 分档统计 / 分年统计 / 告警明细 / 基准 / "
          "真值事件 / 口径说明")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
