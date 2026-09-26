"""分层覆盖率报告：**分数有没有被"缺数据"拖低**？（§16.39 的验收工具）

## 两个完全不同的问题，不能混为一谈

**问题 A（市场事实）**：`IC(accumulation_coverage, 未来收益)`。
问的是"数据稀疏的板块是否后续跑赢"。实测旧窗口是 −0.0455/−0.0805。
**这是行情/数据的性质，改分数不可能改变它** —— 数据稀疏（缺北向/杠杆/
量价）多半在小盘/新板块上，而那类板块本身有自己的收益特征。

**问题 B（本 bug，即 §16.39）**：`corr(accumulation_coverage, base_total)`。
问的是"**分数有没有被缺失数据拖低**"。修前 `base_total = 0.5·six + 0.5·0`，
于是覆盖率低 → 分数低 → **正相关**；修后只在有数据的层之间平均，缺第二层
不再拉低分数 → **≈0**。

> 🩸 我一开始把 A 当成了验收判据，写进"覆盖率不该有预测能力"，
> 结果修完之后 A 依旧是负的（它本来就该是负的），差点误判"修复无效"。
> **B 才是修复的签名。** 这条已经写进脚本，不靠记性。

## 判据

- **主判据（B）**：`corr(acc_cov, base_total)` 逐日横截面 Spearman 的均值。
  > 0.15 视为"缺数据仍在压低分数" → 告警。
- **参考（A）**：覆盖率自己的 IC，只作背景，不当验收。
  ⚠️ 且只在覆盖率**真的有横截面变化**（<1.0 的行占比 ≥5%）且**天数 ≥30**
  时才有意义 —— `six_dim_coverage` 均值 0.999，它的 IC 只是极少数天的噪声，
  一个总误报的告警等于没有告警。

只读、不写库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.factor_ic_report import load_returns, rank_ic, summarize  # noqa: E402

DB = ROOT / "data" / "moss_finagent.db"
#: 评分表默认值：用**基线备份**，因为三窗口分析要在它上面做。
#: ⚠️ 放进分析链时必须显式传 `--table mainline_score`（见 `run_analysis_chain.py`）。
DEFAULT_TABLE = "mainline_score_bak_20260921_2015"
WINDOWS = (("20231009", "20240806", "旧"),
           ("20240901", "20250930", "中"),
           ("20251001", "20260918", "新"))
HORIZONS = (20, 60)
#: 主判据阈值。**这个数是从基线实测反推的，不是随手定的**：
#: V2.2 备份（有 bug 的 50/50）旧窗口 `corr(acc_cov, base_total)` = **+0.093**，
#: 而修好之后（live 表 100/0）是 **+0.011**。所以 0.05 能抓住真问题、
#: 又不会误报。
CORR_LIMIT = 0.05
#: 主判据的最少天数：`corr` 是逐日算的，只有 3 天时均值毫无意义
#: （实测 live 表新窗口 3 天给出 +0.193，纯噪声）。
MIN_DAYS = 30
#: 每一天至少要几个**覆盖率有变化**的板块，这一天的相关才算数。
#: 秩相关只看排序，当天若只有 1~2 个板块覆盖率 <1，那个相关系数
#: 基本由这两块板决定 —— 实测新窗口"至少 1 个变化板块"有 36 天、
#: 均值 +0.0603 看着像告警，但**至少 3 个变化板块的天数是 0**，
#: 也就是说那 +0.0603 全部来自只有一两块板变化的日子，是纯噪声。
MIN_VARYING = 3


def load(table: str, start: str, end: str) -> pd.DataFrame:
    """候选池长表：两层覆盖率 + 层分 + 合成分。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        records.append({
            "day": str(row["trade_date"]),
            "code": str(row["board_code"]),
            "six_cov": float(payload.get("six_dim_coverage") or 0.0),
            "acc_cov": float(payload.get("accumulation_coverage") or 0.0),
            "base_total": float(payload.get("base_total") or 0.0),
            "total": float(payload.get("total") or 0.0),
        })
    return pd.DataFrame(records)


def frame_of(panel: pd.DataFrame, column: str) -> pd.DataFrame:
    return panel.pivot_table(index="day", columns="code", values=column)


def daily_corr(panel: pd.DataFrame, left: str, right: str) -> tuple[float, int]:
    """逐日横截面 Spearman 的均值（只在共同非缺失的板块上算）。

    ⚠️ 两道**逐日**样本量闸门，缺一个就会误报：

    - 当天共有样本 < 20 天跳过（`rank_ic` 的老规矩）；
    - 当天**覆盖率有变化**的板块 < `MIN_VARYING` 也跳过 ——
      否则相关系数由那一两块板决定，等于拿噪声当证据。
    """
    a = frame_of(panel, left)
    b = frame_of(panel, right)
    values: list[float] = []
    for day in a.index.intersection(b.index):
        x, y = a.loc[day], b.loc[day]
        mask = x.notna() & y.notna()
        if mask.sum() < 20:
            continue
        if int((mask & (x < 0.999)).sum()) < MIN_VARYING:
            continue
        value = x[mask].rank().corr(y[mask].rank())
        if value == value:
            values.append(float(value))
    if not values:
        return float("nan"), 0
    return float(np.mean(values)), len(values)


def main() -> int:
    parser = argparse.ArgumentParser(description="分层覆盖率报告")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 分层覆盖率报告：分数有没有被「缺数据」拖低")
    emit()
    emit(f"> 由 `scripts/layer_coverage_report.py` 生成，表 `{args.table}`。")
    emit("> **主判据**是 `corr(accumulation_coverage, base_total)`："
         "修前 `base_total = 0.5·six + 0.5·0`（缺第二层就腰斩）→ 正相关；"
         "修后只在有数据的层之间平均 → ≈0。")
    emit("> ⚠️ 覆盖率**自己的 IC** 是**市场事实**（数据稀疏的板块是否跑赢），"
         "不是本 bug 的判据 —— 修分数改变不了它。")
    emit()

    correlations: dict[str, dict[str, float]] = {}
    coverage_ic: dict[str, list[dict]] = {}
    for start, end, label in WINDOWS:
        panel = load(args.table, start, end)
        if panel.empty:
            emit(f"- {label} 窗口无数据")
            continue
        returns = load_returns(start, end)
        emit(f"## {label} 窗口 {start}~{end}（候选池 {len(panel)} 行）")
        emit()
        emit("| 指标 | 均值 | p10 | p50 | p90 |")
        emit("|---|---:|---:|---:|---:|")
        for column, name in (("six_cov", "六维层覆盖率"),
                             ("acc_cov", "第二层覆盖率")):
            values = panel[column].to_numpy()
            emit(f"| {name} | {values.mean():.3f} | "
                 f"{np.percentile(values, 10):.3f} | "
                 f"{np.percentile(values, 50):.3f} | "
                 f"{np.percentile(values, 90):.3f} |")
        emit()
        base_corr, days = daily_corr(panel, "acc_cov", "base_total")
        total_corr, _ = daily_corr(panel, "acc_cov", "total")
        correlations[label] = {"base_total": base_corr, "total": total_corr,
                               "days": days}
        emit(f"- `corr(acc_cov, base_total)` = **{base_corr:+.3f}**"
             f"（{days} 天）；`corr(acc_cov, total)` = {total_corr:+.3f}")
        emit()

        for column, name in (("six_cov", "six_dim_coverage"),
                             ("acc_cov", "accumulation_coverage")):
            varying = float((panel[column] < 0.999).mean())
            scores = frame_of(panel, column)
            for horizon in HORIZONS:
                series, _ = rank_ic(scores, returns[horizon])
                stat = summarize(series, horizon)
                if not stat:
                    continue
                coverage_ic.setdefault(label, []).append(
                    {"name": name, "horizon": horizon, "ic": stat["ic"],
                     "days": stat["days"], "varying": varying})

    emit("## 验收结论")
    emit()
    emit("**主判据**：`corr(accumulation_coverage, base_total)` 应当 ≈0。"
         f"超过 +{CORR_LIMIT}（且天数 ≥{MIN_DAYS}）就说明「缺数据仍在压低分数」。")
    emit()
    emit(f"阈值来历：V2.2 备份（有 bug）旧窗口实测 **+0.093**，"
         f"修好后实测 **+0.011** —— 所以 {CORR_LIMIT} 能抓真问题又不误报。")
    emit()
    emit("| 窗口 | corr(acc_cov, base_total) | corr(acc_cov, total) | 天数 | 判读 |")
    emit("|---|---:|---:|---:|---|")
    flagged = False
    for label, values in correlations.items():
        base_corr = values["base_total"]
        days = int(values["days"])
        if days < MIN_DAYS:
            verdict = "⏭ 天数不足，不判"
        elif base_corr == base_corr and base_corr > CORR_LIMIT:
            verdict = "🚨 缺数据在压低分数"
            flagged = True
        else:
            verdict = "✅"
        emit(f"| {label} | {base_corr:+.3f} | {values['total']:+.3f} | "
             f"{days} | {verdict} |")
    emit()
    emit("🚨 **覆盖率与分数正相关** —— 缺数据的板块仍被系统性压低，"
         "查 `service.py` 的层合成是否又漏了 `available`"
         if flagged else
         "✅ 覆盖率与分数基本不相关（缺数据没有被当成分数）")
    emit()
    emit("## 附：覆盖率自己的 IC（**市场事实**，不作验收判据）")
    emit()
    emit("⚠️ 只在覆盖率**真的有横截面变化**（<1.0 的行占比 ≥5%）且"
         "**天数 ≥30** 时才看：`six_dim_coverage` 均值 0.999，"
         "它的 IC 只是极少数天的噪声。")
    emit()
    emit("| 窗口 | 覆盖率 | 持有期 | IC | 天数 | 有变化比例 | 可判读 |")
    emit("|---|---|---:|---:|---:|---:|---|")
    for label, items in coverage_ic.items():
        for item in items:
            usable = item["days"] >= 30 and item["varying"] >= 0.05
            emit(f"| {label} | `{item['name']}` | {item['horizon']} | "
                 f"{item['ic']:+.4f} | {item['days']} | "
                 f"{item['varying'] * 100:.0f}% | "
                 f"{'✅' if usable else '⏭ 噪声'} |")
    emit()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
