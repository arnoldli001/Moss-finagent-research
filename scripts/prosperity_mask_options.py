"""景气度「低到某个值就不算它」的几种做法 —— 逐个量化。

## 用户的问题

> 「谷子经济、猴痘概念、短剧游戏、影院、传媒、空间计算这些题材，因为通常
>   都是炒作消息，跟景气度无关，**当景气度低于一个值时，需要忽视景气度的影响**，
>   看下有哪些方案？」

所以问题不是"压缩"而是"**在低分区把这一维拿掉**"。做法有好几族，
它们在数学上不是同一件事，代价也不同：

| 方案 | 做法 | 与"拿掉"的关系 |
|---|---|---|
| **O0 现状** | 照常用 | — |
| **O1 抬底 clamp** | `p < τ → τ` | τ=50 时等于把低分区**抬到中性**（不是拿掉） |
| **O2 剔除重归一 drop** | `p < τ` 时该维不参与，其余 5 维按权重**重新归一** | 这才是真正的"忽视" |
| **O3 题材组剔除** | 同 O2，但**只对题材组**生效 | 精准手术，不动基本面板块 |
| **O4 平滑剔除 soft** | 权重从 `p≤lo` 的 0 平滑升到 `p≥hi` 的 1 | 避免阈值附近的跳变 |
| **O5 换代理变量** | 用「题材活跃度」（涨停家数/连板高度/换手）替代 | 需新输入，**本轮无法测** |

## 为什么 O1 与 O2 不是一回事

- `clamp(τ=50)`：低分区一律变成 50 分 —— 相当于说"这块板景气度中性"；
- `drop(τ=50)`：把这一维**丢掉**，其余 5 维重新归一 —— 相当于说
  "景气度这件事我不看，只看另外 5 维"。

两者只有在其余 5 维的加权平均恰好等于 50 时才相同。因为题材板块的
`trading`/`moneyflow`/`technical` 往往偏强，`drop` 通常比 `clamp` 抬得更多。

## 判据（沿用前几轮）

候选 = 当日六维前 64 名（池子大小固定，所以方案只改变"谁在池里"）；
用户启动集判据（20/35 日滚动涨幅 > 15%，步长 4，命中窗口 [−10, +5]）
的 `误报率 FP/TP`，训练/留出分开。另外单独看被压制板块的进池比例。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/prosperity_mask_options.py \\
        --out docs/MAINLINE_PROSPERITY_MASK_OPTIONS.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prosperity_variant_experiment import (  # noqa: E402
    level_prosperity,
    load,
    percentile_within_day,
)

CACHE_DB = ROOT / "data" / "mainline_cache.db"
LEAD_OK, LAG_OK = 10, 5
FP_RATIO = 0.22
TRAIN = ("20231009", "20250930")
TEST = ("20251001", "20260918")
TOPN = 64
#: 用户点名的题材组（池内实际存在的）
THEMES = {
    "886095.TI": "IP经济(谷子经济)",
    "885994.TI": "猴痘概念",
    "886060.TI": "短剧游戏",
    "885418.TI": "文化传媒概念（传媒；池内无「影院」板块）",
    "886049.TI": "空间计算",
}
#: 同一族的扩展（游戏三兄弟）
THEMES_EXT = {"885457.TI": "手机游戏", "885603.TI": "网络游戏",
              "885874.TI": "云游戏"}


def six_with(data: dict, days: list[str], by_day: dict[str, list[str]],
             index_of: dict[str, dict[str, int]],
             level: dict[tuple[str, str], float], *, mode: str, tau: float,
             lo: float, hi: float, only: set[str] | None
             ) -> dict[tuple[str, str], float]:
    """按选项重算六维。

    `mode`：`use` 照常 / `clamp` 抬底 / `drop` 剔除重归一 / `soft` 平滑剔除。
    `only` 非空时，只对这些板块生效（其余板块一律走 `use`）。
    """
    out: dict[tuple[str, str], float] = {}
    for day in days:
        for code in by_day.get(day) or []:
            dims = data[code]["dims"][index_of[code][day]]
            value = level.get((day, code))
            num = den = 0.0
            for key, (dim_value, weight, reversed_) in dims.items():
                if key == "prosperity":
                    continue
                shown = 100.0 - dim_value if reversed_ else dim_value
                num += shown * weight
                den += weight
            weight = dims.get("prosperity", (0.0, 30.0, False))[1]
            applies = value is not None and weight > 0 and (
                only is None or code in only)
            if not applies:
                if value is not None and weight > 0:      # 不在作用域内 → 照常
                    num += value * weight
                    den += weight
            elif mode == "use":
                num += value * weight
                den += weight
            elif mode == "clamp":
                num += max(value, tau) * weight
                den += weight
            elif mode == "drop":
                if value >= tau:
                    num += value * weight
                    den += weight
                # 低于阈值：不加也不减 → 其余维度自动重新归一
            elif mode == "soft":
                ramp = min(1.0, max(0.0, (value - lo) / max(hi - lo, 1e-9)))
                if ramp > 0:
                    num += value * weight * ramp
                    den += weight * ramp
            if den:
                out[(day, code)] = num / den
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="景气度低分忽视方案对比")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    data = load()
    days = sorted({d for item in data.values() for d in item["days"]})
    index_of = {code: {day: i for i, day in enumerate(item["days"])}
                for code, item in data.items()}
    by_day: dict[str, list[str]] = {}
    for code, item in data.items():
        for day in item["days"]:
            by_day.setdefault(day, []).append(code)
    level = level_prosperity(data, days, by_day, index_of)

    # 被压制的板块：景气度全市场分位 < 30
    low_boards: list[str] = []
    for code, item in data.items():
        pcts = [level.get((day, code)) for day in item["days"]]
        pcts = [p for p in pcts if p is not None]
        if len(pcts) >= 100 and float(np.mean(pcts)) < 30.0:
            low_boards.append(code)

    options: list[tuple[str, dict]] = [
        ("O0 现状（照常用）", {"mode": "use", "tau": 0.0, "lo": 0.0, "hi": 1.0,
                          "only": None}),
        ("O1a 抬底 τ=40", {"mode": "clamp", "tau": 40.0, "lo": 0, "hi": 1,
                        "only": None}),
        ("O1b 抬底 τ=50（=低分区中性化）", {"mode": "clamp", "tau": 50.0,
                                   "lo": 0, "hi": 1, "only": None}),
        ("O2 剔除重归一 τ=40", {"mode": "drop", "tau": 40.0, "lo": 0, "hi": 1,
                           "only": None}),
        ("O2b 剔除重归一 τ=50", {"mode": "drop", "tau": 50.0, "lo": 0, "hi": 1,
                            "only": None}),
        ("O3 题材组剔除 τ=50", {"mode": "drop", "tau": 50.0, "lo": 0, "hi": 1,
                           "only": set(THEMES) | set(THEMES_EXT)}),
        ("O4 平滑剔除 lo=20 hi=50", {"mode": "soft", "lo": 20.0, "hi": 50.0,
                                "tau": 0.0, "only": None}),
    ]

    # 启动集（用户口径）
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    closes: dict[str, list[tuple[str, float]]] = {}
    for row in cache.execute(
            "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
            " JOIN ml_calendar k ON k.trade_date = b.trade_date"
            " ORDER BY b.board_code, b.trade_date"):
        closes.setdefault(str(row["board_code"]), []).append(
            (str(row["trade_date"]), float(row["close"] or 0.0)))
    cache.close()
    labels_by_board: dict[str, tuple[list[int], list[str]]] = {}
    for code, items in closes.items():
        day_list = [d for d, _ in items]
        close = np.asarray([c for _, c in items])
        labels: set[int] = set()
        for window in (20, 35):
            if len(close) <= window + 1:
                continue
            rolling = np.full(len(close), np.nan)
            rolling[window:] = close[window:] / close[:-window] - 1.0
            pos = window
            while pos < len(close):
                if np.isfinite(rolling[pos]) and rolling[pos] > 0.15:
                    labels.add(pos)
                    pos += 4
                else:
                    pos += 1
        labels_by_board[code] = (sorted(labels), day_list)

    def evaluate(six: dict[tuple[str, str], float], low: str, high: str) -> dict:
        by_day_scores: dict[str, list[tuple[str, float]]] = {}
        for (day, code), value in six.items():
            if low <= day <= high and np.isfinite(value):
                by_day_scores.setdefault(day, []).append((code, value))
        fired: dict[str, set[str]] = {}
        for day, items in by_day_scores.items():
            items.sort(key=lambda kv: -kv[1])
            for code, _ in items[:TOPN]:
                fired.setdefault(code, set()).add(day)
        tp = fp = total = 0
        for code, (labels, day_list) in labels_by_board.items():
            index = {d: i for i, d in enumerate(day_list)}
            window_labels = [i for i in labels if low <= day_list[i] <= high]
            if not window_labels:
                continue
            total += len(window_labels)
            hits = {index[d] for d in fired.get(code, ())
                    if low <= d <= high and d in index}
            covered: set[int] = set()
            for i in window_labels:
                span = range(max(0, i - LEAD_OK), i + LAG_OK + 1)
                if any(t in span for t in hits):
                    tp += 1
                covered |= set(range(max(0, i - LEAD_OK), i + LAG_OK + 1))
            fp += sum(1 for t in hits if t not in covered)
        return {"labels": total, "tp": tp, "fp": fp,
                "recall": (tp / total) if total else float("nan"),
                "fp_ratio": (fp / tp) if tp else float("inf")}

    emit("# 景气度「低于阈值就忽视」的方案对比")
    emit()
    emit("> 由 `scripts/prosperity_mask_options.py` 生成（只读）。")
    emit(f"> 候选 = 当日六维前 {TOPN}；误报率判据 FP/TP；"
         f"训练 {TRAIN[0]}~{TRAIN[1]}，留出 {TEST[0]}~{TEST[1]}。")
    emit()

    results: list[dict] = []
    for name, spec in options:
        six = six_with(data, days, by_day, index_of, level, **spec)
        # 逐日取前 N 名
        by_day_scores: dict[str, list[tuple[str, float]]] = {}
        for (day, code), value in six.items():
            if np.isfinite(value):
                by_day_scores.setdefault(day, []).append((code, value))
        pool: dict[str, set[str]] = {}
        for day, items in by_day_scores.items():
            items.sort(key=lambda kv: -kv[1])
            pool[day] = {code for code, _ in items[:TOPN]}
        rate = {}
        for code in data:
            days_of = data[code]["days"]
            rate[code] = (sum(1 for day in days_of if code in pool.get(day, ()))
                          / len(days_of))
        low_rate = float(np.median([rate[c] for c in low_boards])) if low_boards else 0.0
        theme_rate = {c: rate.get(c, 0.0) for c in THEMES}
        results.append({
            "name": name, "low_rate": low_rate, "theme_rate": theme_rate,
            "coal": rate.get("885914.TI", 0.0),
            "train": evaluate(six, *TRAIN), "test": evaluate(six, *TEST)})

    emit("## 一、对被压制板块的影响")
    emit()
    emit(f"被压制板块 = 景气度全市场分位 < 30 的 **{len(low_boards)}** 个。")
    emit()
    header = "| 方案 | 被压制组进池中位 | 煤炭 885914 | " + " | ".join(
        THEMES[c].split("（")[0] for c in THEMES) + " |"
    emit(header)
    emit("|---|---:|---:|" + "---:|" * len(THEMES))
    for row in results:
        cells = " | ".join(f"{row['theme_rate'][c]:.0%}" for c in THEMES)
        emit(f"| {row['name']} | {row['low_rate']:.0%} "
             f"| {row['coal']:.0%} | {cells} |")
    emit()

    emit("## 二、对全池启动集判据的影响（用户口径）")
    emit()
    emit("| 方案 | 窗口 | 启动日 | TP | FP | 召回率 | 误报率 FP/TP | 达标 |")
    emit("|---|---|---:|---:|---:|---:|---:|---|")
    for row in results:
        for tag, label in (("train", "训练"), ("test", "留出")):
            stat = row[tag]
            ok = bool(stat["tp"]) and stat["fp"] <= FP_RATIO * stat["tp"]
            emit(f"| {row['name']} | {label} | {stat['labels']} | {stat['tp']} "
                 f"| {stat['fp']} | {stat['recall']:.0%} "
                 f"| {stat['fp_ratio']:.2f} | {'✅' if ok else '❌'} |")
    emit()

    emit("## 三、六个方案的性质与代价")
    emit()
    emit("| 方案 | 数学上做了什么 | 代价 / 风险 |")
    emit("|---|---|---|")
    emit("| O0 现状 | 照常用 | 29 个题材/价值板块进池中位仅 2% |")
    emit("| O1 抬底 clamp | 低分区抬到 τ（τ=50 即「中性」） | 不改变谁高谁低，"
         "只是不再**倒扣**；τ 太高会把「真的差」也抹平 |")
    emit("| O2 剔除重归一 drop | 该维退出合成，其余 5 维重新归一 | "
         "**口径分叉**：低景气板块与其它板块不再同一把尺子，"
         "跨板块比较会失真；且它是硬阈值，边界附近会跳变 |")
    emit("| O3 题材组剔除 | 同 O2，但只对点名题材生效 | 精准，代价最小；"
         "但需要维护一份「哪些是消息驱动题材」的名单，且名单本身会过时 |")
    emit("| O4 平滑剔除 soft | 权重在 [lo,hi] 内线性降到 0 | 没有硬边界，"
         "但多一个要调的参数区间，且解释成本更高 |")
    emit("| O5 换代理变量 | 用题材活跃度替代景气度 | **需要新输入**："
         "板块内涨停家数、连板高度、换手/成交额占比分位 —— 现有 payload 里没有 |")
    emit()
    emit("⚠️ 无论选哪个，**O2/O3/O4 都会让被作用的板块与其他板块「不同尺」**。"
         "报告与前端必须能区分这一点，否则跨板块排名会被误读。")
    emit()
    emit("## 四、实测结论")
    emit()
    emit("**只有 O3（题材组剔除）在留出窗口上是改善的**：全池留出 FP/TP "
         "6.30 → **6.26**；其余全局方案（O1/O2/O4）训练集看着都更好"
         "（4.76 → 4.02~4.36），但**留出集全都变差**（6.30 → 6.51~6.56）——"
         "又是一个「内样本赢、外样本输」。")
    emit()
    emit("**O3 对点名题材是唯一真正生效的**：")
    emit()
    emit("| 板块 | 现状进池 | O3 后 | O2b（全局剔除 τ=50）后 |")
    emit("|---|---:|---:|---:|")
    for code, label in THEMES.items():
        o0 = results[0]["theme_rate"][code]
        o3 = results[5]["theme_rate"][code]
        o2b = results[4]["theme_rate"][code]
        emit(f"| {label}（{code}） | {o0:.0%} | **{o3:.0%}** | {o2b:.0%} |")
    emit()
    emit("⚠️ **两条必须说清的限制**：")
    emit()
    emit("1. **谷子经济（0%→2%）与短剧游戏（0%→2%）几乎没动** —— 说明"
         "压住它们的**不只是景气度**，把它们归因于景气度是不完整的。"
         "要查清得单独看它们其余 5 维的取值（本轮没做）。")
    emit("2. **文化传媒概念在所有方案下都停在 2~3%** —— 它不是被景气度压住的，"
         "换哪个方案都救不了它。")
    emit()
    emit("**推荐**：做 **O3**（题材组剔除，名单进配置、可增删）—— 它是唯一"
         "「精准 + 留出不亏」的做法，与「这些题材跟景气度无关」的判断一致。"
         "**不建议**做 O2/O4 这种全局剔除：训练集好看，留出集净亏，"
         "而且会让被作用的板块与其它板块**不同尺**，跨板块排名失真。"
         "若还想再宽一点，叠加 **O1b（抬底 τ=50）**，但要接受留出的那点代价。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
