"""季节性检验：某个板块的主线行情到底集中在哪几个月（用数据说话）。

## 用户的问题

> 「绿色电力其实跟景气度无关，看下是否这个参数导致的。他和旅游一样也是
>   季节性炒作，比如 **2-7 月炒电力**，其他月份禁止主线告警上报。
>   你可以回溯下最近几年的股市数据，看下电力在每年哪几个月大涨一波出主线，
>   然后只对这几个月监控。」

所以要做两件事：**① 绿色电力差是不是景气度造成的**；
**② 逐月统计它的主线行情，用数据定出该监控哪几个月**（而不是照抄"2-7 月"）。

## 样本量必须说清楚

`ml_board_bar` 里这些概念板块的行情**最早到 20230103**，到 20260918 是
约 3.7 年 —— 每个自然月只有 **3~4 个观测**。所以：

- 逐月统计只能看**方向性**，不能当精确的季节性模型；
- 因此同时给出一个**置换检验**（permutation test）：把"启动日"随机
  重新分配到各月，看观测到的月度集中度有多罕见。p 值大就说明
  "季节性"在这段样本里根本立不住，此时应回到用户给的领域知识。

## 口径

- **启动日**沿用用户定义：滚动 20/35 日涨幅 > 15% 的首个交叉日；
- 按月统计：启动日数、该月收益率、该月出现启动的"月数占比"；
- 同时统计该板块的**告警**在各月的分布与告警后 20 日收益。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/seasonality_report.py \\
        --boards 885936.TI,885497.TI --out docs/MAINLINE_SEASONALITY.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
BIG = 0.15
HORIZON = 20
MONTH_LABEL = ("1月", "2月", "3月", "4月", "5月", "6月",
               "7月", "8月", "9月", "10月", "11月", "12月")


def main() -> int:
    parser = argparse.ArgumentParser(description="板块季节性检验")
    parser.add_argument("--boards", default="885936.TI,885497.TI")
    parser.add_argument("--windows", default="20,35",
                        help="启动判定的滚动窗口（交易日），逗号分隔")
    parser.add_argument("--alert-table", default="mainline_alert_bak_v26_prefloor")
    parser.add_argument("--score-table", default="mainline_score_bak_v26_prefloor")
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    names = {str(r["code"]): str(r["name"]) for r in
             cache.execute("SELECT code, name FROM ml_board")}
    # 全市场日收益（用所有板块的**横截面中位数**当大盘代理）：
    # 用来检验用户说的「煤炭通常和大盘走势相反」。用中位数而不是均值，
    # 是为了不被少数暴涨板块带偏。
    market: dict[str, float] = {}
    per_day: dict[str, list[float]] = {}
    for row in cache.execute(
            "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        per_day.setdefault(str(row["trade_date"]), []).append(
            float(row["close"] or 0.0))
    prev: dict[str, float] = {}
    for day in sorted(per_day):
        values = [v for v in per_day[day] if v > 0]
        if values:
            prev[day] = float(np.median(values))
    ordered = sorted(prev)
    for index in range(1, len(ordered)):
        a, b = ordered[index - 1], ordered[index]
        if prev[a] > 0:
            market[b] = prev[b] / prev[a] - 1.0
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rng = np.random.default_rng(args.seed)

    emit("# 板块季节性检验：主线行情集中在哪几个月")
    emit()
    emit("> 由 `scripts/seasonality_report.py` 生成（只读）。")
    emit(f"> 启动日定义：滚动 {'/'.join(str(w) for w in windows)} 日涨幅 > "
         f"{BIG:.0%} 的**首个交叉日**；样本 = `ml_board_bar` 全部可用历史"
         f"（最早 20230103，约 3.7 年）。")
    emit("> 大盘代理 = 全市场板块日收益的**横截面中位数**（不被少数暴涨板块带偏）。")
    emit()
    emit("⚠️ **每个自然月只有 3~4 个观测**，所以同时给置换检验的 p 值："
         "p 大就说明「季节性」在这段样本里立不住，此时应回到领域知识，"
         "而不是编一个数据驱动的月份表。")
    emit()
    emit("**关于 15% 门槛**：`window=20/30` 的「涨幅超 15%」，日度数据上等价于"
         "「20 或 30 个交易日内涨过 15%」；这里统计的是**每个启动段的首个交叉日**"
         "（同一波行情只算一次），所以下面是「行情次数」而不是「满足条件的天数」。")
    emit()

    for code in [item.strip() for item in args.boards.split(",") if item.strip()]:
        # ⚠️ 行情表在**缓存库**（`cache`），不是结果库（`conn`）。
        # 第一版在结果库上查 `ml_board_bar`，直接 "no such table" ——
        # 与突破触发那次把历史查询放错库是同一类错。
        rows = cache.execute(
            "SELECT b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " WHERE b.board_code = ? ORDER BY b.trade_date", (code,)).fetchall()
        if len(rows) < 60:
            emit(f"## {names.get(code, code)}（{code}）：行情不足，跳过")
            continue
        day_list = [str(r["trade_date"]) for r in rows]
        close = np.asarray([float(r["close"] or 0.0) for r in rows])
        index = {d: i for i, d in enumerate(day_list)}

        # 启动日：各窗口滚动涨幅 > BIG 的**首个交叉日**（同一波只算一次）
        advance = np.zeros(len(close), dtype=bool)
        for window in windows:
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            advance |= np.isfinite(rolling) & (rolling > BIG)
        starts: list[int] = []
        previous = False
        for pos, flag in enumerate(advance):
            if flag and not previous:
                starts.append(pos)
            previous = flag

        months = np.asarray([int(d[4:6]) for d in day_list])
        # 月度收益：每月最后一个交易日的收盘 / 上月最后一个收盘 − 1
        month_keys = sorted({d[:6] for d in day_list})
        month_return: dict[str, float] = {}
        for i, key in enumerate(month_keys):
            if i == 0:
                continue
            current = [p for p, d in enumerate(day_list) if d[:6] == key]
            previous_key = month_keys[i - 1]
            before = [p for p, d in enumerate(day_list) if d[:6] == previous_key]
            if current and before:
                month_return[key] = close[current[-1]] / close[before[-1]] - 1.0

        emit(f"## {names.get(code, code)}（{code}）")
        emit()
        emit(f"- 行情区间 {day_list[0]}~{day_list[-1]}（{len(day_list)} 个交易日，"
             f"{len(month_keys)} 个自然月）")
        emit(f"- 启动段 **{len(starts)}** 个")
        emit()

        # 每一次启动的明细（用户要的"每次发生时间 + 每年什么月份"）
        emit("### 每一次启动的明细")
        emit()
        emit("| # | 启动日 | 月份 | 该次启动后 20 日涨幅 | 同期大盘 20 日 |")
        emit("|---:|---|---:|---:|---:|")
        for order, pos in enumerate(starts, 1):
            forward = (close[pos + HORIZON] / close[pos] - 1.0
                       if pos + HORIZON < len(close) else float("nan"))
            m_forward = float("nan")
            day = day_list[pos]
            if day in market and pos + HORIZON < len(close):
                target_day = day_list[min(pos + HORIZON, len(close) - 1)]
                if target_day in market:
                    acc = 1.0
                    for offset in range(1, HORIZON + 1):
                        if pos + offset >= len(close):
                            break
                        acc *= 1.0 + market.get(day_list[pos + offset], 0.0)
                    m_forward = acc - 1.0
            emit(f"| {order} | {day} | {MONTH_LABEL[int(day[4:6]) - 1]} "
                 f"| {forward:+.2%} | "
                 f"{m_forward:+.2%}" if np.isfinite(m_forward) else
                 f"| {order} | {day} | {MONTH_LABEL[int(day[4:6]) - 1]} "
                 f"| {forward:+.2%} | — |")
        emit()

        # 「与大盘相反」检验
        board_returns, market_returns = [], []
        for pos in range(1, len(close)):
            day = day_list[pos]
            if close[pos - 1] > 0 and day in market:
                board_returns.append(close[pos] / close[pos - 1] - 1.0)
                market_returns.append(market[day])
        if len(board_returns) > 60:
            br = np.asarray(board_returns)
            mr = np.asarray(market_returns)
            corr = float(np.corrcoef(br, mr)[0, 1])
            launch_market = np.asarray([
                market.get(day_list[min(pos + 1, len(close) - 1)], np.nan)
                for pos in starts])
            launch_market = launch_market[np.isfinite(launch_market)]
            down = mr < 0
            emit("### 「与大盘相反」检验")
            emit()
            emit(f"- 日收益与大盘代理的相关系数：**{corr:+.3f}**"
                 f"（{'同向' if corr > 0.1 else '反向' if corr < -0.1 else '几乎无关'}）")
            emit(f"- 大盘下跌日的该板块平均收益："
                 f"{br[down].mean():+.3%}（大盘 {mr[down].mean():+.3%}）")
            emit(f"- 启动当日大盘平均收益："
                 f"{(launch_market.mean() if launch_market.size else float('nan')):+.3%}"
                 f"（全样本大盘日均 {mr.mean():+.3%}）")
            emit("- 「与大盘相反」若成立，应当看到**相关为负**、且启动日多出现在"
                 "大盘下跌或横盘时。上面的数字若都是同向/无关，"
                 "说明这条经验在这段样本里**没有体现**。")
            emit()

        # 逐月统计
        start_months = [int(day_list[pos][4:6]) for pos in starts]
        emit("| 月份 | 启动段数 | 出现过启动的月份数/该月总月数 | 该月平均收益 "
             "| 该月最好一次 |")
        emit("|---:|---:|---:|---:|---:|")
        for month in range(1, 13):
            count = start_months.count(month)
            keys = [k for k in month_return if int(k[4:6]) == month]
            values = [month_return[k] for k in keys]
            total_months = len([k for k in {d[:6] for d in day_list}
                                if int(k[4:6]) == month])
            with_start = len({day_list[pos][:6] for pos in starts
                              if int(day_list[pos][4:6]) == month})
            mean = f"{np.mean(values):+.1%}" if values else "—"
            best = f"{max(values):+.1%}" if values else "—"
            emit(f"| {MONTH_LABEL[month - 1]} | {count} "
                 f"| {with_start}/{total_months} | {mean} | {best} |")
        emit()

        # 置换检验：启动日的月份分布是否非随机
        observed = np.bincount(start_months, minlength=13)[1:13]
        pool = months.copy()
        best_stat = int(observed.max())
        hits = 0
        for _ in range(args.draws):
            sample = rng.choice(pool, size=len(starts), replace=False)
            counts = np.bincount(sample, minlength=13)[1:13]
            if int(counts.max()) >= best_stat:
                hits += 1
        p_value = (hits + 1) / (args.draws + 1)
        hot = [MONTH_LABEL[m - 1] for m in range(1, 13) if observed[m - 1] > 0]
        emit(f"- 启动段最多的月份：**{MONTH_LABEL[int(observed.argmax())]}**"
             f"（{best_stat} 段）；有启动的月份共 {len(hot)} 个")
        verdict = ("季节性显著（p<0.05）" if p_value < 0.05
                   else "**不显著** —— 这段样本里看不出季节性")
        emit(f"- 置换检验 p = **{p_value:.3f}**（{verdict}）")
        emit()

        # 告警的逐月分布与效果
        alerts = [dict(r) for r in conn.execute(
            f"SELECT trade_date, level FROM {args.alert_table}"
            " WHERE board_code = ?", (code,))]
        if alerts:
            emit("### 该板块告警的逐月分布与效果（告警后 20 日）")
            emit()
            emit("| 月份 | 告警数 | 告警后20日均值 | 无行情率 |")
            emit("|---:|---:|---:|---:|")
            for month in range(1, 13):
                subset = [a for a in alerts
                          if int(str(a["trade_date"])[4:6]) == month]
                if not subset:
                    continue
                fwd, nomove = [], 0
                for item in subset:
                    pos = index.get(str(item["trade_date"]))
                    if pos is None:
                        continue
                    if pos + HORIZON < len(close):
                        fwd.append(close[pos + HORIZON] / close[pos] - 1.0)
                    if not advance[pos: pos + 21].any():
                        nomove += 1
                mean = f"{np.mean(fwd):+.2%}" if fwd else "—"
                rate = f"{nomove / len(subset):.0%}"
                emit(f"| {MONTH_LABEL[month - 1]} | {len(subset)} | {mean} "
                     f"| {rate} |")
            emit()

        # 景气度是不是原因
        prosperity: list[float] = []
        for row in conn.execute(
                f"SELECT payload FROM {args.score_table}"
                " WHERE board_code = ?", (code,)):
            payload = json.loads(str(row["payload"] or "{}"))
            for layer in (payload.get("layers") or []):
                for dim in (layer.get("dimensions") or []):
                    if str(dim.get("key")) == "prosperity" and dim.get("available"):
                        prosperity.append(float(dim.get("score") or 0.0))
        if prosperity:
            values = np.asarray(prosperity)
            why = ("偏低，属于被景气度压制的类型" if values.mean() < 40
                   else "并不低，压制不是主因")
            emit(f"- 景气度维度：均值 **{values.mean():.1f}**、中位 "
                 f"{np.median(values):.1f}、最高 {values.max():.1f}（{why}）")
            emit()

    conn.close()
    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
