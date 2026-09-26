"""扫描每个板块的**分数分档收益**，按用户的两步口径产出 `min_score`/`max_score` 配置。

## 用户的口径（2026-09-22 定稿）

> 「折中 —— **按赚钱口径判，留下误报但是不亏钱的，剩余的按主线口径判**。」

翻译成可执行的规则（**顺序不能颠倒**）：

    第一步（赚钱口径，否决权）：若某个分数档**不亏钱**（未来 20 日均值 ≥ 0），
        就不许动它 —— 哪怕它在"误报主线"口径下被判成误报。
        「误报」在启动集口径里只说明"没走出 ≥15% 的主线"，**不等于亏钱**；
        中小波段板块上这两个口径会系统性背离（环氧丙烷就是典型：
        高分档 +1.37% / +9.00%，但被判误报）。
    第二步（主线口径，只在第一步放行后才用）：
        对**确实亏钱**的档位，用启动集口径挑切点，
        要求「砍掉的误报多、且 FP/TP 不变差」。

## 两种切点

    高分档亏钱 → `max_score` 封顶（用户提的「阻止高位报主线」）
    低分档亏钱 → `min_score` 抬下限（镜像情形，养鸡就是这种）

## 切点怎么选

对每个候选切点 `c`：

    砍掉的部分 = {score > c}（封顶）或 {score < c}（抬下限）
    硬门槛：被砍部分的未来 20 日均值 **< 0**（第一步的否决权）
    再加一条：砍完 FP/TP **不变差**（否则是"用命中换数字"）

满足的切点里取**砍掉误报最多**的那个。**一条都不满足就什么都不配** ——
这个脚本的默认结果就是"大多数板块不动"，那是对的。

## 为什么必须先重打分再看

分数是**截面百分位**，池子一变分档边界全变。所以 `--alert-table` /
`--score-table` 必须指向**同一轮**重打分的结果；拿旧表的切点套新表是错的。

用法：
    # 干跑：只打印建议
    python scripts/scan_score_bands.py --alert-table mainline_alert
    # 产出可直接粘进 mainline_alert_exclusions.yaml 的片段
    python scripts/scan_score_bands.py --alert-table mainline_alert --yaml-out /tmp/band.yaml
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
LEAD_OK, LAG_OK = 10, 5
BIG = 0.15
HORIZON = 20


def scan_labels(close: np.ndarray) -> set[int]:
    """启动集（与验收口径一致）：滚动 20/35 日涨幅 > 15%，命中后跳 4 天。"""
    n = len(close)
    labels: set[int] = set()
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
    parser = argparse.ArgumentParser(description="按分档收益产出分数开关配置")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--score-table", default="")
    parser.add_argument("--min-alerts", type=int, default=8,
                        help="少于这么多条可算收益的告警就不动它（默认 8）")
    parser.add_argument("--min-cut", type=int, default=2,
                        help="被砍掉的那一档至少要这么多条（默认 2）")
    parser.add_argument("--yaml-out", default="")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
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

    alerts: dict[str, list[dict]] = {}
    for row in conn.execute(
            f"SELECT board_code, trade_date, level, score, entry_close"
            f" FROM {args.alert_table}"):
        alerts.setdefault(str(row["board_code"]), []).append(dict(row))
    conn.close()

    results = []
    for code, items in bars.items():
        if code not in pool:            # 已出池的板块不参与（用户已明确剔除）
            continue
        rows = alerts.get(code) or []
        if len(rows) < args.min_alerts:
            continue
        days = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        index = {d: i for i, d in enumerate(days)}
        labels = scan_labels(close)
        covered = {t for i in labels
                   for t in range(max(0, i - LEAD_OK), i + LAG_OK + 1)}
        points = []
        for row in rows:
            pos = index.get(str(row["trade_date"]))
            if pos is None or pos + HORIZON >= len(close):
                continue
            entry = float(row["entry_close"] or 0.0) or float(close[pos])
            if entry <= 0:
                continue
            points.append({"score": float(row["score"] or 0.0),
                           "ret": float(close[pos + HORIZON] / entry - 1.0),
                           "tp": pos in covered})
        if len(points) < args.min_alerts:
            continue
        scores = np.asarray([p["score"] for p in points])
        rets = np.asarray([p["ret"] for p in points])
        tps = np.asarray([p["tp"] for p in points])
        base_fp, base_tp = int((~tps).sum()), int(tps.sum())
        base_ratio = base_fp / base_tp if base_tp else float("inf")

        best = None
        for kind in ("max_score", "min_score"):
            for cut in np.unique(np.round(scores, 1)):
                cut_mask = scores > cut if kind == "max_score" else scores < cut
                if int(cut_mask.sum()) < args.min_cut:
                    continue
                # 第一步：被砍掉的这一档**必须真的亏钱**（赚钱口径的否决权）
                if rets[cut_mask].mean() >= 0:
                    continue
                keep = ~cut_mask
                fp, tp = int((~tps[keep]).sum()), int(tps[keep].sum())
                ratio = fp / tp if tp else float("inf")
                # 第二步：砍完 FP/TP 不能变差，且误报要真的少
                if ratio > base_ratio + 1e-9 or fp >= base_fp:
                    continue
                gain = base_fp - fp
                if best is None or gain > best["gain"]:
                    best = {"kind": kind, "cut": float(cut), "gain": gain,
                            "fp": fp, "tp": tp, "ratio": ratio,
                            "cut_ret": float(rets[cut_mask].mean()),
                            "cut_n": int(cut_mask.sum())}
        if best:
            results.append({"code": code, "name": pool[code], "n": len(points),
                            "base_fp": base_fp, "base_tp": base_tp,
                            "base_ratio": base_ratio, **best})

    results.sort(key=lambda d: -d["gain"])
    print(f"扫描板块 {len(pool)} 个（只含在池的）；"
          f"告警数 ≥{args.min_alerts} 且能算出未来 {HORIZON} 日收益的才评估")
    print(f"**建议配置的板块：{len(results)} 个**（其余不动 —— "
          "这是预期结果，不是漏扫）\n")
    if not results:
        print("（没有板块同时满足两条：被砍档位亏钱、且砍完 FP/TP 不变差）")
        return 0
    print(f"{'板块':<18}{'开关':<11}{'切点':>7}{'告警':>6}{'砍掉':>6}"
          f"{'砍掉档收益':>11}{'误报':>7}{'FP/TP':>8}{'→':>8}")
    for item in results:
        print(f"{item['name'][:16]:<18}{item['kind']:<11}{item['cut']:>7.1f}"
              f"{item['n']:>6}{item['cut_n']:>6}{item['cut_ret']:>11.2%}"
              f"{item['base_fp']:>7}{item['base_ratio']:>8.2f}"
              f"{'→':>4}{item['ratio']:>4.2f}")

    if args.yaml_out:
        lines = ["# 由 scripts/scan_score_bands.py 生成（分数分档口径）",
                 "# 规则：先按赚钱口径否决（被砍档位必须亏钱），再按主线口径挑切点",
                 ""]
        for kind in ("min_score", "max_score"):
            picked = [r for r in results if r["kind"] == kind]
            if not picked:
                continue
            lines.append(f"{kind}:")
            for item in picked:
                lines.append(f'  - code: "{item["code"]}"')
                lines.append(f'    name: "{item["name"]}"')
                lines.append(f'    score: {item["cut"]:g}')
                lines.append(
                    f'    reason: >-')
                lines.append(
                    f'      {kind} 档（{item["cut"]:g} 以上/以下）'
                    f'{item["cut_n"]} 条告警未来 20 日均值 '
                    f'{item["cut_ret"]:+.2%}（亏钱）→ 屏蔽；'
                    f'砍掉误报 {item["gain"]} 条，FP/TP '
                    f'{item["base_ratio"]:.2f} → {item["ratio"]:.2f}')
                lines.append(f'    decided: "2026-09-22"')
            lines.append("")
        Path(args.yaml_out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nYAML → {args.yaml_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
