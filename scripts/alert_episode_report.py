"""告警的**事件级**评估：持有收益 + 同一波主线去重。

## 为什么要改评估口径（用户指出的方法论问题）

原先把每条告警**独立**评估（上报后 20 日涨没涨）。这有个系统性偏差：

    9/1  板块启动 → 触发告警 ✅（后面确实涨了）
    11/1 主升快见顶 → 又触发一次告警
         → 再往后 20~30 个交易日大概率**下跌**
         → 原口径把这条记成"误报"

但它是**同一波主线的第二次告警**，不是模型看错了。用户的原话：

> 如果两次主线告警间隔期间出现的概念连续大涨，则后面那次告警可以忽略，
> 作为默认成功召回的才对。

所以正确口径是**事件级**：同一板块的连续告警聚成一个 episode，
只评估**第一次**（启动告警）；episode 内后续的告警，若期间板块已经涨过，
记为「延续（豁免）」而不是误报。

## 本脚本产出

1. **持有收益**（用户直接问的）：全部信号按告警计，持有 10/20/30/40 个交易日
   （约 0.5 / 1 / 1.5 / 2 个月）的**均值 / 中位数 / 正收益比例**，
   并与全池基准对比得出**超额**。
2. **事件级去重**：episode 切分、启动告警 vs 延续告警、豁免后的准确率与误召回率。

用法：
    .venv\\Scripts\\python.exe scripts\\alert_episode_report.py
    .venv\\Scripts\\python.exe scripts\\alert_episode_report.py --start 20251001
"""

from __future__ import annotations

import argparse

import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
MAIN_DB = ROOT / "data" / "moss_finagent.db"
#: 持有期（交易日）。40 ≈ 2 个月。
HOLDS = (10, 20, 30, 40)
#: 同一板块两条告警间隔超过这么多交易日，就算**新的一波**（新 episode）
EPISODE_GAP = 20


def _read_pool() -> list[sqlite3.Row]:
    """池内 324 个概念板块（基准的可比universe）。"""
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT code FROM ml_board WHERE source = 'sector_crowding:list'"
        ).fetchall()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="告警事件级评估")
    parser.add_argument("--start", default="20231009")
    parser.add_argument("--end", default="20260918")
    parser.add_argument("--gap", type=int, default=EPISODE_GAP,
                        help="episode 切分间隔（交易日）")
    parser.add_argument("--continuation-gain", type=float, default=8.0,
                        help="启动告警到延续告警期间涨幅达到多少算"
                             "「期间已大涨」→ 豁免")
    parser.add_argument("--report", default="docs/MAINLINE_ALERT_EPISODES.md")
    parser.add_argument("--min-coverage", type=float, default=0.9,
                        help="板块行情覆盖率下限（低于它的板块不参与基准计算）")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = conn.execute(
        "SELECT trade_date, board_code, board_name, level, score, payload"
        " FROM mainline_alert WHERE trade_date BETWEEN ? AND ?"
        " ORDER BY board_code, trade_date", (args.start, args.end)).fetchall()
    conn.close()
    if not alerts:
        print("❌ 窗口内没有告警")
        return 2
    print(f"窗口 {args.start} ~ {args.end}：告警 {len(alerts)} 条")

    # ---------- 行情 ----------
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT board_code, trade_date, close FROM ml_board_bar"
        " WHERE trade_date BETWEEN ? AND ?", (args.start, args.end)).fetchall()
    conn.close()
    wide = pd.DataFrame(
        [{"board_code": str(r["board_code"]), "trade_date": str(r["trade_date"]),
          "close": float(r["close"] or 0)} for r in rows]
    ).pivot(index="trade_date", columns="board_code", values="close").sort_index()

    # ---------- 分析池 + 行情覆盖率过滤 ----------
    #
    # ⚠️ 必须过滤，否则基准会被严重污染。实测：`ml_board_bar` 有 2243 个板块，
    # **1892 个在窗口内覆盖率 <90%**（含池外的 861xxx/875xxx 等）。
    # 稀疏板块上 `wide.shift(-20)` 取到的是"下一个有数据的观测"，
    # 可能隔了几个月 —— 于是 20 日收益的 p99.9 高达 **+649%**，
    # 基准均值被拉到 199%~684%，"超额"变成 −684% 这种荒谬数字。
    #
    # 限定为**池内 + 覆盖 ≥90%** 之后，20 日均值 +2.01% / 中位 +0.85%
    # （合理），这才是信号应当对比的口径。
    pool_codes = {str(r["code"]) for r in _read_pool()}
    coverage = wide.notna().mean()
    keep = [code for code in wide.columns
            if code in pool_codes
            and coverage.get(code, 0.0) >= args.min_coverage]
    dropped = wide.shape[1] - len(keep)
    wide = wide[keep]
    print(f"基准口径：池内且覆盖率 ≥{args.min_coverage:.0%} 的板块 "
          f"{len(keep)} 个（剔除 {dropped} 个稀疏/池外板块）")
    if not keep:
        print("❌ 没有可用的板块")
        return 2

    dates = list(wide.index)
    position = {day: index for index, day in enumerate(dates)}
    fwd = {h: wide.shift(-h) / wide - 1.0 for h in HOLDS}

    def ret_of(code: str, day: str, hold: int) -> float | None:
        if code not in wide.columns or day not in position:
            return None
        column = fwd[hold][code] if code in fwd[hold].columns else None
        if column is None:
            return None
        value = column.get(day)
        return None if value is None or not np.isfinite(value) else float(value)

    # ---------- 1) 持有收益（按告警计，不做去重） ----------
    print()
    print("=" * 92)
    print("一、全部告警的持有收益（按告警计）")
    print("-" * 92)
    print(f"{'持有期':>7}{'样本':>7}{'均值':>9}{'中位数':>9}{'正收益比例':>11}"
          f"{'基准均值':>10}{'超额':>9}")
    lines: list[str] = []
    hold_stats: dict[int, dict[str, float]] = {}
    for hold in HOLDS:
        values = [v for a in alerts
                  if (v := ret_of(str(a["board_code"]),
                                  str(a["trade_date"]), hold)) is not None]
        if not values:
            continue
        # 基准：同一批交易日 × 全池所有板块
        #
        # ⚠️ 必须过滤 **非有限值**：`wide.shift(-h)/wide` 在收盘价为 0 的
        # 板块上会算出 ±inf，而 `pandas.dropna()` **不删 inf**。
        # 第一版没过滤，基准均值被 inf 拉到 199%~684%，
        # 于是"超额"变成 −684% 这种荒谬数字。截面统计里
        # `np.isfinite` 与 `dropna` 不是一回事，必须显式过滤。
        base_values: list[float] = []
        day_set = {str(a["trade_date"]) for a in alerts}
        for day in dates:
            if day not in day_set:
                continue
            row = fwd[hold].loc[day].dropna() if day in fwd[hold].index else None
            if row is not None:
                base_values.extend(float(v) for v in row.to_numpy()
                                   if np.isfinite(v))
        mean = float(np.mean(values))
        base_mean = float(np.mean(base_values)) if base_values else 0.0
        stat = {"n": len(values), "mean": mean,
                "median": float(np.median(values)),
                "win": float(np.mean([v > 0 for v in values])),
                "base": base_mean, "excess": mean - base_mean}
        hold_stats[hold] = stat
        print(f"{hold:>6}日{len(values):>7}{mean * 100:>8.2f}%"
              f"{stat['median'] * 100:>8.2f}%{stat['win'] * 100:>10.0f}%"
              f"{base_mean * 100:>9.2f}%{stat['excess'] * 100:>8.2f}%")
        lines.append(f"| {hold} | {len(values)} | {mean * 100:.2f}% | "
                     f"{stat['median'] * 100:.2f}% | {stat['win'] * 100:.0f}% | "
                     f"{base_mean * 100:.2f}% | {stat['excess'] * 100:+.2f}% |")

    # ---------- 2) episode 切分 ----------
    by_board: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for alert in alerts:
        by_board[str(alert["board_code"])].append(alert)
    episodes: list[dict] = []
    for code, items in by_board.items():
        items.sort(key=lambda a: str(a["trade_date"]))
        current: list[sqlite3.Row] = []
        for alert in items:
            if not current:
                current = [alert]
                continue
            gap = (position.get(str(alert["trade_date"]), 0)
                   - position.get(str(current[-1]["trade_date"]), 0))
            if gap > args.gap:
                episodes.append({"code": code, "items": current})
                current = [alert]
            else:
                current.append(alert)
        if current:
            episodes.append({"code": code, "items": current})

    starters = [ep for ep in episodes]
    continuation_total = sum(len(ep["items"]) - 1 for ep in episodes)
    print()
    print("=" * 92)
    print(f"二、事件级去重（间隔 > {args.gap} 个交易日算新的一波）")
    print("-" * 92)
    print(f"  episode（主线波段）       {len(episodes)}")
    print(f"  启动告警（只评估这些）      {len(starters)}")
    print(f"  延续告警                   {continuation_total}"
          f"（占全部 {continuation_total / len(alerts) * 100:.0f}%）")

    # ---------- 3) 豁免判定 ----------
    excused = 0
    kept_as_error = 0
    for ep in episodes:
        first = ep["items"][0]
        first_day = str(first["trade_date"])
        for later in ep["items"][1:]:
            later_day = str(later["trade_date"])
            # 期间涨幅：启动告警日收盘 → 延续告警日收盘
            try:
                a = wide.at[first_day, ep["code"]]
                b = wide.at[later_day, ep["code"]]
                rise = (b / a - 1.0) * 100 if a else 0.0
            except KeyError:
                rise = 0.0
            if rise >= args.continuation_gain:
                excused += 1
            else:
                kept_as_error += 1
    print(f"  延续告警中「期间已涨 ≥{args.continuation_gain:g}%」→ **豁免**  "
          f"{excused}（{excused / max(1, continuation_total) * 100:.0f}%）")
    print(f"  延续告警中期间未大涨 → 仍算误报            {kept_as_error}")

    # ---------- 4) 修正前后的准确率 ----------
    base_hit = 0
    base_n = 0
    day_set = {str(a["trade_date"]) for a in alerts}
    peak20 = wide.rolling(20, min_periods=1).max().shift(-20) / wide - 1.0
    for day in dates:
        if day not in day_set:
            continue
        p = peak20.loc[day].dropna()
        r = fwd[20].loc[day].dropna() if day in fwd[20].index else None
        if r is None:
            continue
        shared = p.index.intersection(r.index)
        base_n += len(shared)
        base_hit += int(((p[shared] >= 0.08) & (r[shared] > 0)).sum())
    base_rate = base_hit / base_n if base_n else 0.0

    def is_real(alert) -> bool | None:
        code, day = str(alert["board_code"]), str(alert["trade_date"])
        if code not in wide.columns or day not in position:
            return None
        try:
            p = peak20.at[day, code]
            r = fwd[20].at[day, code]
        except KeyError:
            return None
        if not np.isfinite(p) or not np.isfinite(r):
            return None
        return bool(p >= 0.08 and r > 0)

    start_real = [a for a in (ep["items"][0] for ep in episodes)
                  if is_real(a) is not None]
    start_hits = sum(1 for a in start_real if is_real(a))
    print()
    print("=" * 92)
    print("三、修正前后的准确率（20 日：最大涨幅 ≥8% 且收益 >0）")
    print("-" * 92)
    judged_all = [a for a in alerts if is_real(a) is not None]
    acc_all = sum(1 for a in judged_all if is_real(a)) / max(1, len(judged_all))
    print(f"  基准率（全池随机）                          {base_rate * 100:>5.1f}%")
    print(f"  现行口径（按告警计）                        {acc_all * 100:>5.1f}%"
          f"  提升 {acc_all / base_rate:>4.2f}x")
    acc_start = start_hits / max(1, len(start_real))
    print(f"  只算**启动告警**                            {acc_start * 100:>5.1f}%"
          f"  提升 {acc_start / base_rate:>4.2f}x")
    if excused or kept_as_error:
        adj_hits = start_hits + excused
        adj_n = len(start_real) + excused + kept_as_error
        acc_adj = adj_hits / max(1, adj_n)
        print(f"  启动 + 豁免延续（用户口径）                  "
              f"{acc_adj * 100:>5.1f}%  提升 {acc_adj / base_rate:>4.2f}x")
        print(f"    注：豁免了 {excused} 条延续告警；"
              f"{kept_as_error} 条延续告警仍计为误报（期间没涨）")

    out = [f"# 主线告警 事件级评估（{args.start} ~ {args.end}）\n",
           f"- 告警 {len(alerts)} 条 → episode {len(episodes)} 个"
           f"（启动 {len(starters)} / 延续 {continuation_total}）\n",
           f"- episode 切分间隔 {args.gap} 个交易日；"
           f"延续豁免门槛「期间涨 ≥{args.continuation_gain:g}%」\n\n",
           "## 一、全部告警的持有收益\n\n",
           "| 持有期(交易日) | 样本 | 均值 | 中位数 | 正收益比例 | 基准均值 | 超额 |",
           "|---:|---:|---:|---:|---:|---:|---:|"]
    out.extend(lines)
    out.append(f"\n## 二、事件级去重\n\n")
    out.append(f"- episode {len(episodes)} 个：启动告警 {len(starters)} 条、"
               f"延续告警 {continuation_total} 条\n")
    out.append(f"- 延续告警中**豁免** {excused} 条（期间已涨 "
               f"≥{args.continuation_gain:g}%），仍计误报 {kept_as_error} 条\n")
    out.append(f"\n## 三、准确率对比\n\n")
    out.append(f"- 基准率：{base_rate * 100:.1f}%\n")
    out.append(f"- 只算启动告警：{acc_start * 100:.1f}%"
               f"（提升 {acc_start / base_rate:.2f}x）\n")
    target = ROOT / args.report
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\n报告 → {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
