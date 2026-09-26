"""突破触发的参数扫描：`lookback_days` 到底该取多少（120 还是 80？）。

## 为什么能离线扫，不用重打分

突破判定只依赖**两样东西**：

1. 该板块**自己**过去的 `total` 序列（已在 `mainline_score` 里）；
2. 当日 `total`。

所以给定 (lookback_days, quantile, fresh_days, min_score)，整段历史都能
**逐日重放**出来 —— 不必为了试一个参数再花 90 分钟重打分。

## 三个必须一起看的指标

只看"命中多少"会掉进"多报就有用"的陷阱，所以同时给：

- **覆盖**：有多少比例的板块**至少报出过一次**。
  （这是本次要解决的问题：统一绝对线让 48~56% 的板块永远报不出来。）
- **频率**：平均每天命中几个板块。它必须与现有告警量同量级，
  否则"解决了滞后"其实是"把告警变成噪声"。
- **时点**：对真值事件算首次命中日 − 真值日（交易日），
  按用户的验收标准（提前 ≤10 或滞后 ≤5 = 合格）算合格率与漏报数。

## 口径说明（近似，但对比公平）

重放**不模拟** `confirm_periods` 与 `cooldown_days`（那要跑完整告警链），
所以这里量的是"**突破信号**的时点与频率"。比较不同 `lookback_days` 时
所有候选都受同一近似影响，因此相对结论可用；绝对条数会略高于实盘。

用法：
    .venv\\Scripts\\python.exe scripts\\breakout_param_sweep.py
    .venv\\Scripts\\python.exe scripts\\breakout_param_sweep.py --lookbacks 60,80,100,120,160,250
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
WINDOWS = (("20231009", "20240806", "旧"), ("20240901", "20250930", "中"),
           ("20251001", "20260918", "新"))
LOOKBACK = 20      # 真值日前看多少交易日
LOOKAHEAD = 40     # 真值日后看多少交易日
LEAD_OK, LAG_OK = 10, 5


def load_series(table: str) -> dict[str, list[tuple[str, float]]]:
    """`{board_code: [(day, total)]}`，**只取候选池**（与线上口径一致）。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, total, payload FROM {table}"
        " ORDER BY trade_date").fetchall()
    conn.close()
    out: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        out.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["total"] or 0.0)))
    for series in out.values():
        series.sort()
    return out


def replay(series: dict[str, list[tuple[str, float]]], *,
           lookback_days: int, quantile: float, fresh_days: int,
           min_score: float, min_samples: int) -> dict[str, set[str]]:
    """逐日重放突破规则 → `{board: {命中的交易日}}`（严格 PIT）。"""
    fired: dict[str, set[str]] = {}
    for code, items in series.items():
        for index, (day, total) in enumerate(items):
            history = [value for _, value in items[max(0, index - lookback_days):
                                                  index]]
            ceiling = breakout_ceiling(history, quantile=quantile,
                                       min_samples=min_samples)
            if is_breakout(total, ceiling, min_score=min_score,
                           recent=history, fresh_days=fresh_days):
                fired.setdefault(code, set()).add(day)
    return fired


def timing(fired: dict[str, set[str]], days: list[str],
           index: dict[str, int]) -> dict:
    """按真值事件算时点：合格率 / 滞后 / 太早 / 漏报。"""
    truth = yaml.safe_load(TRUTH.read_text(encoding="utf-8")) or {}
    names: dict[str, str] = {}
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT code, name FROM ml_board"):
        names.setdefault(str(row["name"]), str(row["code"]))
    conn.close()
    good = late = early = missed = 0
    deltas: list[int] = []
    for event in truth.get("events") or []:
        if not isinstance(event, dict) or event.get("status") != "ok":
            continue
        date = str(event.get("date") or "").replace("-", "")
        if date not in index:
            continue
        codes: list[str] = []
        for code in event.get("codes") or []:
            text = str(code)
            codes.append(names.get(text, text))
        if not codes:
            continue
        center = index[date]
        low = max(0, center - LOOKBACK)
        high = min(len(days) - 1, center + LOOKAHEAD)
        window = set(days[low:high + 1])
        first: str | None = None
        for code in codes:
            for day in fired.get(code, ()):
                if day in window and (first is None or day < first):
                    first = day
        if first is None:
            missed += 1
            continue
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
            "median": (float(np.median(deltas)) if deltas else float("nan"))}


def main() -> int:
    parser = argparse.ArgumentParser(description="突破触发参数扫描")
    parser.add_argument("--table", default="mainline_score")
    parser.add_argument("--lookbacks", default="60,80,100,120,160,250")
    parser.add_argument("--quantiles", default="0.90")
    parser.add_argument("--fresh-days", default="5,10,15")
    parser.add_argument("--min-score", type=float, default=50.0)
    parser.add_argument("--min-samples", type=int, default=60)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    series = load_series(args.table)
    days = sorted({day for items in series.values() for day, _ in items})
    index = {day: position for position, day in enumerate(days)}
    boards = len(series)
    emit(f"# 突破触发参数扫描（表 `{args.table}`，{boards} 个板块，"
         f"{len(days)} 个交易日）")
    emit()
    emit("> 离线**逐日重放**（不模拟 confirm/cooldown），所以量的是"
         "「突破信号」的时点与频率；比较不同参数时近似一致。")
    emit(f"> 验收标准：提前 ≤{LEAD_OK} 或滞后 ≤{LAG_OK} 个交易日 = 合格。")
    emit()

    lookbacks = [int(x) for x in args.lookbacks.split(",") if x.strip()]
    quantiles = [float(x) for x in args.quantiles.split(",") if x.strip()]
    fresh = [int(x) for x in args.fresh_days.split(",") if x.strip()]

    emit("| 回看天数 | 分位 | 新鲜度 | 总命中 | 日均 | 覆盖板块 | "
         "合格率 | 合格 | 滞后 | 太早 | 漏报 | 差值中位 |")
    emit("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    results: list[dict] = []
    for lookback in lookbacks:
        for quantile in quantiles:
            for fresh_days in fresh:
                fired = replay(series, lookback_days=lookback,
                               quantile=quantile, fresh_days=fresh_days,
                               min_score=args.min_score,
                               min_samples=args.min_samples)
                total = sum(len(v) for v in fired.values())
                stat = timing(fired, days, index)
                cover = len(fired) / max(boards, 1) * 100
                emit(f"| {lookback} | {quantile:g} | {fresh_days} | {total} "
                     f"| {total / max(len(days), 1):.2f} | {cover:.0f}% "
                     f"| **{stat['rate']:.0f}%** | {stat['good']} "
                     f"| {stat['late']} | {stat['early']} | {stat['missed']} "
                     f"| {stat['median']:+.0f} |")
                results.append({"lookback": lookback, "quantile": quantile,
                                "fresh": fresh_days, "total": total,
                                "per_day": total / max(len(days), 1),
                                "coverage": cover, **stat})
    emit()
    emit("## 怎么读")
    emit()
    emit("- **覆盖**要越高越好（现在的痛点是近半数板块永远报不出来）；")
    emit("- **日均**要与现有告警量同量级，不能翻几倍；")
    emit("- **合格率**是用户口径，优先看它；漏报下降是加分项。")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
