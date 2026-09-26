"""季节性旺季"提高敏感度"的候选信号：**先测有没有预测力，再谈怎么落地**。

## 用户的诉求

> 「这种季节性题材应该在旺季对六维度模型的各个参数权重做特别放大，增加敏感度，
>   特别是**资金**和**单日涨幅异常**。有什么可落地思路吗？」

"放大权重"只是手段，前提是**那些维度在旺季确实有预测力**。本项目有一条
反直觉的既有结论：`trading` 与 `technical` 两个维度在 5/10/20/60 四个持有期上
IC **单调为负**（H=20 −0.077 / −0.082），所以它们在层合成里是**反向**用的。
也就是说"涨幅/量能异常"很可能**不是**正信号 —— 因此必须先分别测。

## 测四个候选信号（全部 PIT，只用当日及之前）

| 信号 | 定义 |
|---|---|
| S1 资金异常 | `moneyflow` 维当日横截面分 ≥ 90 |
| S2 单日涨幅异常 | 当日涨幅 ≥ **自身过去 120 日**的 95 分位 |
| S3 量能异常 | `technical.raw.volume` ≥ **自身过去 120 日**的 90 分位 |
| S4 资金+涨幅双异常 | S1 且 S2 |

## 判据

对每个信号看**未来 20 个交易日内是否出现主线启动**（滚动 20/35 日涨幅 > 15%
的首个交叉日）：

    提升倍数 = P(有信号后有启动) / P(基准)
    基准     = 该板块所有交易日的 P(未来 20 日内有启动)

同时给**训练/留出**两个窗口，以及"季节性四板块"与"全池"两组样本 ——
信号若只在四板块有效、全池无效，那就是风格特异，不能当全局规则。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/seasonal_sensitivity_experiment.py \\
        --out docs/MAINLINE_SEASONAL_SENSITIVITY.md
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
LOOKBACK_SELF = 120          # 自身历史分位的回看天数
HORIZON = 20                 # 信号后看多少交易日
TRAIN = ("20231009", "20250930")
TEST = ("20251001", "20260918")
#: 季节性四板块
SEASONAL = ("885914.TI", "885936.TI", "885497.TI")
#: 旺季窗口（与配置文件一致，含起点前 14 天由调用方决定是否启用）
WINDOWS = {
    "885914.TI": (("01-05", "03-26"), ("07-01", "08-31"), ("09-30", "11-17")),
    "885936.TI": (("03-12", "03-28"), ("09-30", "11-14")),
    "885497.TI": (("02-01", "02-28"), ("03-01", "03-31"), ("04-01", "04-30"),
                  ("08-01", "08-31"), ("09-01", "09-30")),
}


def in_window(day: str, spans) -> bool:
    """按**月-日**判断（不看年份），与线上窗口语义一致（不含左侧冗余）。"""
    md = f"{day[4:6]}-{day[6:8]}"
    return any(start <= md <= end for start, end in spans)


def main() -> int:
    parser = argparse.ArgumentParser(description="季节性敏感度信号检验")
    parser.add_argument("--score-table", default="mainline_score_bak_v26_prefloor")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, payload FROM {args.score_table}"
        " ORDER BY board_code, trade_date").fetchall()
    conn.close()

    # 逐板块收集：日期、资金维分、当日涨幅、量能、以及自身分位
    per_board: dict[str, dict] = {}
    for row in rows:
        code = str(row["board_code"])
        payload = json.loads(str(row["payload"] or "{}"))
        item = per_board.setdefault(code, {"days": [], "moneyflow": [],
                                           "change": [], "volume": []})
        item["days"].append(str(row["trade_date"]))
        flow = change = volume = np.nan
        for layer in (payload.get("layers") or []):
            for dim in (layer.get("dimensions") or []):
                key = str(dim.get("key"))
                if key == "moneyflow" and dim.get("available"):
                    flow = float(dim.get("score") or 0.0)
                if key == "technical":
                    raw = dim.get("raw") or {}
                    value = raw.get("volume")
                    if isinstance(value, (int, float)):
                        volume = float(value)
        value = payload.get("change_pct")
        if isinstance(value, (int, float)):
            change = float(value)
        item["moneyflow"].append(flow)
        item["change"].append(change)
        item["volume"].append(volume)

    # 行情（算启动段与未来 20 日是否有启动）
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

    prepared: dict[str, dict] = {}
    for code, items in closes.items():
        day_list = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        advance = np.zeros(len(close), dtype=bool)
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            advance |= np.isfinite(rolling) & (rolling > BIG)
        prepared[code] = {"index": {d: i for i, d in enumerate(day_list)},
                          "advance": advance, "n": len(close)}

    def self_percentile(series: list[float], lookback: int = LOOKBACK_SELF
                        ) -> np.ndarray:
        """当日值在**自身过去 lookback 天**里的分位（严格 PIT，NaN 透传）。"""
        arr = np.asarray(series, dtype=float)
        out = np.full(arr.size, np.nan)
        for i in range(arr.size):
            if not np.isfinite(arr[i]):
                continue
            history = arr[max(0, i - lookback):i]
            history = history[np.isfinite(history)]
            if history.size >= 20:
                out[i] = float((history <= arr[i]).mean())
        return out

    # 逐板块算信号与标签
    #
    # S5 = **横截面**放量分位（当日全市场 `technical.raw.volume` 的前 10%）。
    # 为什么额外测它：S3 用的是"**自身**历史分位"，落地时必须去读历史评分行的
    # payload（多一次查询、多一条 PIT 约束）；横截面版本**当日就能算**，
    # 落地成本低一个量级。若两者效果接近，就该选 S5。
    volume_by_day: dict[str, dict[str, float]] = {}
    for code, item in per_board.items():
        for i, day in enumerate(item["days"]):
            value = item["volume"][i]
            if np.isfinite(value):
                volume_by_day.setdefault(day, {})[code] = float(value)

    def cross_percentile(day: str, code: str) -> float:
        column = volume_by_day.get(day) or {}
        if len(column) < 30 or code not in column:
            return float("nan")
        ordered = np.sort(np.asarray(list(column.values())))
        return float((ordered <= column[code]).mean())

    samples: dict[str, list[dict]] = {}
    for code, item in per_board.items():
        if code not in prepared:
            continue
        market = prepared[code]
        index = market["index"]
        advance = market["advance"]
        flow = np.asarray(item["moneyflow"], dtype=float)
        change_pct = self_percentile(item["change"])
        volume_pct = self_percentile(item["volume"])
        records = []
        for i, day in enumerate(item["days"]):
            pos = index.get(day)
            if pos is None:
                continue
            future = advance[pos + 1: pos + 1 + HORIZON]
            if future.size < HORIZON:            # 末端样本不足，丢弃
                continue
            records.append({
                "day": day,
                "s1": bool(np.isfinite(flow[i]) and flow[i] >= 90.0),
                "s2": bool(np.isfinite(change_pct[i]) and change_pct[i] >= 0.95),
                "s3": bool(np.isfinite(volume_pct[i]) and volume_pct[i] >= 0.90),
                "s5": bool(np.isfinite(cross_percentile(day, code))
                           and cross_percentile(day, code) >= 0.90),
                "launch": bool(future.any()),
                "in_window": (in_window(day, WINDOWS[code])
                              if code in WINDOWS else False),
            })
        if records:
            for rec in records:
                rec["s4"] = rec["s1"] and rec["s2"]
            samples[code] = records

    emit("# 季节性旺季「提高敏感度」的候选信号检验")
    emit()
    emit(f"> 由 `scripts/seasonal_sensitivity_experiment.py` 生成（只读，评分表 "
         f"`{args.score_table}`）。")
    emit(f"> 标签：未来 {HORIZON} 个交易日内**出现过**滚动 20/35 日涨幅 > "
         f"{BIG:.0%}（用户口径的主线启动）。")
    emit(f"> 自身分位回看 {LOOKBACK_SELF} 个交易日；比值 = "
         f"P(有信号后启动) / P(无信号时启动)。")
    emit()

    def evaluate(records: list[dict], tag: str) -> dict:
        base = np.mean([r["launch"] for r in records]) if records else float("nan")
        out = {"tag": tag, "n": len(records), "base": base}
        for signal in ("s1", "s2", "s3", "s4", "s5"):
            hit = [r for r in records if r[signal]]
            miss = [r for r in records if not r[signal]]
            if len(hit) < 20:
                out[signal] = None
                continue
            p_hit = float(np.mean([r["launch"] for r in hit]))
            p_miss = float(np.mean([r["launch"] for r in miss])) if miss else float("nan")
            out[signal] = {"n": len(hit), "p": p_hit, "p_miss": p_miss,
                           "lift": (p_hit / base) if base else float("nan")}
        return out

    def table(title: str, group: list[str], low: str, high: str) -> None:
        emit(f"### {title}（{low}~{high}）")
        emit()
        emit("| 板块 | 样本 | 基准 P(启动) | S1 资金异常 | S2 涨幅异常 "
             "| S3 量能异常(自身) | S5 放量异常(横截面) |")
        emit("|---|---:|---:|---:|---:|---:|---:|")
        pooled: list[dict] = []
        for code in group:
            records = [r for r in samples.get(code, [])
                       if low <= r["day"] <= high]
            if not records:
                continue
            pooled.extend(records)
            stat = evaluate(records, code)
            cells = []
            for signal in ("s1", "s2", "s3", "s5"):
                got = stat[signal]
                cells.append("—" if got is None
                             else f"{got['p']:.0%}（{got['lift']:.2f}x, n={got['n']}）")
            emit(f"| {names.get(code, code)} | {stat['n']} | {stat['base']:.0%} "
                 f"| " + " | ".join(cells) + " |")
        if pooled:
            stat = evaluate(pooled, "池")
            cells = []
            for signal in ("s1", "s2", "s3", "s5"):
                got = stat[signal]
                cells.append("—" if got is None
                             else f"**{got['p']:.0%}（{got['lift']:.2f}x, n={got['n']}）**")
            emit(f"| **合计** | {stat['n']} | **{stat['base']:.0%}** "
                 f"| " + " | ".join(cells) + " |")
        emit()

    all_boards = sorted(samples)
    for tag, (low, high) in (("训练", TRAIN), ("留出", TEST)):
        table(f"① 季节性板块 · {tag}", list(SEASONAL), low, high)
        table(f"② 全池 · {tag}", all_boards, low, high)

    emit("## 怎么读")
    emit()
    emit("- **lift > 1** 才是正信号；lift < 1 说明「越异常、后面越差」，"
         "那就**不该放大权重**（`trading`/`technical` 在层里本来就是反向用的，"
         "原因见 `six_dim.py` 的长注释）；")
    emit("- 只看**留出**窗口那一半：训练集里 lift 漂亮、留出集掉到 1 附近，"
         "就是噪声；")
    emit("- 样本 `n` 小于 20 的信号直接不给数字 —— 小样本的百分比没有意义。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    print(json.dumps({"boards": len(samples)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
