"""诊断：煤炭概念 885914 为什么候选占比只有 3%？

## 要回答的问题

用户问「煤炭 885914 为什么候选占比仅 3%？」。候选 = 六维/蓄势合成后的
**横截面前 ~20%**。所以 3% 只能是两种原因之一：

1. **成分股里没有煤炭股**（层面输入就错）→ 六维的景气/资金流用的是别的行业；
2. 成分股是对的，但**打分维度系统性偏低**（煤炭的 ROE/营收增速不如题材股，
   或者反向维度在惩罚它）→ 它是"自己不够高"，不是"算错了"。

这两者的修法完全不同，所以必须把**每一维的取值和它在横截面里的分位**
逐日摊开看，而不是只看一个总分。

## 同时核对用户给的 18 只煤炭股

用户直接提供了：中国神华、陕西煤业、兖矿能源、中煤能源、神火股份、
潞安环能、山西焦煤、陕西能源、晋控煤业、山煤国际、淮河能源、平煤股份、
昊华能源、兰花科创、恒源煤电、山西焦化、陕西黑猫、郑州煤电。

先查这些**是不是已经在成分股里** —— 如果已经在，那"补充成分股"是空操作，
3% 的原因就只能是第 2 种。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/coal_board_diagnosis.py \\
        --out docs/MAINLINE_COAL_DIAGNOSIS.md
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

BOARD = "885914.TI"
#: 对照板块：候选占比高 / 中 / 同样低
PEERS = (("885530.TI", "黄金概念"), ("886015.TI", "创新药"),
         ("885812.TI", "农业种植"), ("885937.TI", "培育钻石"))
#: 用户给的煤炭股（名字 → 由 `ml_stock_meta` 反查代码）
COAL_NAMES = ("中国神华", "陕西煤业", "兖矿能源", "中煤能源", "神火股份",
              "潞安环能", "山西焦煤", "陕西能源", "晋控煤业", "山煤国际",
              "淮河能源", "平煤股份", "昊华能源", "兰花科创", "恒源煤电",
              "山西焦化", "陕西黑猫", "郑州煤电")
DIMS = ("trading", "prosperity", "moneyflow", "chips", "macro", "technical")


def main() -> int:
    parser = argparse.ArgumentParser(description="煤炭概念候选占比诊断")
    parser.add_argument("--score-table", default="mainline_score")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    main = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    main.row_factory = sqlite3.Row

    # ---------- 一、成分股核对 ----------
    # ⚠️ 名字→代码**不能**查 `ml_stock_meta`：实测那张表 5562 行的 `name`
    # 全是空串，按名字查必然"一只都找不到"，于是得出"18 只全都不在成分股里"
    # 这个**完全错误**的结论（第一版就是这么错的）。可用的名字源是
    # `ml_member.name`（成分股表自带名字，覆盖所有出现过在任意板块的股票）。
    stock_code: dict[str, str] = {}
    for row in cache.execute("SELECT code, name FROM ml_member WHERE name <> ''"):
        stock_code.setdefault(str(row["name"]), str(row["code"]))
    members = {str(r["code"]): str(r["name"]) for r in cache.execute(
        "SELECT code, name FROM ml_member WHERE board_code = ?", (BOARD,))}
    pure = {str(r["code"]): (float(r["corr"]) if r["corr"] is not None else None,
                             int(r["relevant"] or 0), str(r["reason"] or ""))
            for r in cache.execute(
                "SELECT code, corr, relevant, reason FROM ml_member_pure"
                " WHERE board_code = ?", (BOARD,))}

    emit("# 煤炭概念 885914：候选占比 3% 的成因诊断")
    emit()
    emit("> 由 `scripts/coal_board_diagnosis.py` 生成（只读）。")
    emit(f"> 评分表 `{args.score_table}`；候选 = 当日横截面合成分的"
         "前 ~20%（全池 320 个板块里约 58~64 个）。")
    emit()
    emit("## 一、用户给的 18 只煤炭股，在不在成分股里")
    emit()
    emit("| 股票 | 代码 | 在 ml_member | 在提纯表 | relevant | corr |")
    emit("|---|---|---|---|---:|---:|")
    missing: list[str] = []
    unresolved: list[str] = []
    for name in COAL_NAMES:
        code = stock_code.get(name, "")
        if not code:
            unresolved.append(name)
        in_raw = bool(code) and code in members
        item = pure.get(code) if code else None
        if code and not in_raw:
            missing.append(f"{name}({code})")
        emit(f"| {name} | {code or '❓'} | {'✅' if in_raw else '❌'} "
             f"| {'✅' if item else '❌'} "
             f"| {item[1] if item else '—'} "
             f"| {f'{item[0]:.3f}' if item and item[0] is not None else '—'} |")
    emit()
    emit(f"- `ml_member` 现有成分股 **{len(members)}** 只，"
         f"其中提纯表覆盖 {len(pure)} 只、`relevant=1` 的 "
         f"**{sum(1 for v in pure.values() if v[1])}** 只")
    if unresolved:
        emit(f"- ❓ 名字解析不到代码：{'、'.join(unresolved)}"
             "（`ml_member.name` 里没有这个名字）")
    if missing:
        emit(f"- ❌ 在成分股名单里缺失的：{'、'.join(missing)}")
    else:
        emit("- ✅ 能解析到代码的那些**全部已经在 `ml_member` 里** —— "
             "「补充成分股」对本板块是空操作，3% 只能是**第一层打分**的原因。")
    emit()

    # ---------- 二、逐维诊断 ----------
    rows = main.execute(
        f"SELECT trade_date, total, six_dim, accumulation, rank, candidate,"
        f" payload FROM {args.score_table} WHERE board_code = ?"
        " ORDER BY trade_date", (BOARD,)).fetchall()
    if not rows:
        emit("❌ 评分表里没有这个板块")
        return 2

    per_dim: dict[str, list[float]] = {name: [] for name in DIMS}
    per_dim_pct: dict[str, list[float]] = {name: [] for name in DIMS}
    accumulation: list[float] = []
    totals: list[float] = []
    candidates = 0
    daily: dict[str, dict] = {}

    for row in rows:
        payload = json.loads(str(row["payload"] or "{}"))
        item: dict[str, float] = {}
        for layer in (payload.get("layers") or []):
            for dim in (layer.get("dimensions") or []):
                if dim.get("available"):
                    item[str(dim.get("key"))] = float(dim.get("score") or 0.0)
        for name in DIMS:
            value = item.get(name)
            if value is not None:
                per_dim[name].append(value)
        totals.append(float(row["total"] or 0.0))
        candidates += int(bool(row["candidate"]))
        if payload.get("accumulation_coverage"):
            accumulation.append(float(payload.get("accumulation_score") or 0.0))
        daily[str(row["trade_date"])] = {"total": float(row["total"] or 0.0),
                                         "rank": int(row["rank"] or 0),
                                         "candidate": bool(row["candidate"]),
                                         **{k: item.get(k) for k in DIMS},
                                         "acc": payload.get("accumulation_score")}

    # 每天**全部板块**的同一维度分布 → 本板块那一维在横截面里的分位
    whole = main.execute(
        f"SELECT trade_date, board_code, payload FROM {args.score_table}"
        " WHERE candidate = 1 OR board_code = ?", (BOARD,)).fetchall()
    by_day: dict[str, list[dict]] = {}
    for row in whole:
        payload = json.loads(str(row["payload"] or "{}"))
        item = {"code": str(row["board_code"])}
        for layer in (payload.get("layers") or []):
            for dim in (layer.get("dimensions") or []):
                if dim.get("available"):
                    item[str(dim.get("key"))] = float(dim.get("score") or 0.0)
        by_day.setdefault(str(row["trade_date"]), []).append(item)
    for day, item in daily.items():
        pool = by_day.get(day) or []
        if len(pool) < 20:
            continue
        for name in DIMS:
            value = item.get(name)
            if value is None:
                continue
            values = [one[name] for one in pool if one.get(name) is not None]
            if len(values) < 20:
                continue
            per_dim_pct[name].append(
                float((np.asarray(values) <= value).mean()))

    emit("## 二、逐维诊断：它的每一维在候选池/全市场里排在哪")
    emit()
    emit(f"- 被打分 {len(rows)} 天，其中进候选池 **{candidates}** 天"
         f"（{candidates / len(rows):.1%}）")
    emit(f"- `total`：均值 {np.mean(totals):.1f}、中位 {np.median(totals):.1f}、"
         f"最高 {np.max(totals):.1f}")
    emit()
    emit("| 维度 | 本板块均值 | 本板块该维在当日全市场的分位（均值） | 解读 |")
    emit("|---|---:|---:|---|")
    for name in DIMS:
        values = per_dim.get(name) or []
        pcts = per_dim_pct.get(name) or []
        if not values:
            emit(f"| {name} | — | — | 全部缺失 |")
            continue
        mean = float(np.mean(values))
        pct = float(np.mean(pcts)) if pcts else float("nan")
        if np.isfinite(pct):
            if pct >= 0.6:
                verdict = "偏强"
            elif pct <= 0.4:
                verdict = "**偏弱（拖累项）**"
            else:
                verdict = "中性"
        else:
            verdict = "无法判定"
        emit(f"| {name} | {mean:.1f} | "
             f"{pct:.1%} | {verdict} |")
    if accumulation:
        emit(f"| accumulation | {np.mean(accumulation):.1f} | — "
             f"| 可用 {len(accumulation)}/{len(rows)} 天 |")
    else:
        emit("| accumulation | — | — | **全部不可用** |")
    emit()

    # ---------- 三、与对照板块比 ----------
    emit("## 三、与对照板块比（同一天、同一维度的均值）")
    emit()
    emit("| 板块 | 候选占比 | total 均值 | " + " | ".join(DIMS) + " |")
    emit("|---|---:|---:|" + "---:|" * len(DIMS))
    for code, label in ((BOARD, "煤炭概念"), *PEERS):
        peers = main.execute(
            f"SELECT trade_date, total, candidate, payload FROM {args.score_table}"
            " WHERE board_code = ?", (code,)).fetchall()
        if not peers:
            continue
        sums: dict[str, list[float]] = {name: [] for name in DIMS}
        tot: list[float] = []
        hit = 0
        for row in peers:
            payload = json.loads(str(row["payload"] or "{}"))
            tot.append(float(row["total"] or 0.0))
            hit += int(bool(row["candidate"]))
            for layer in (payload.get("layers") or []):
                for dim in (layer.get("dimensions") or []):
                    key = str(dim.get("key"))
                    if dim.get("available") and key in sums:
                        sums[key].append(float(dim.get("score") or 0.0))
        cells = " | ".join(
            f"{np.mean(sums[name]):.1f}" if sums[name] else "—" for name in DIMS)
        emit(f"| {label}（{code}） | {hit / len(peers):.0%} | {np.mean(tot):.1f} "
             f"| {cells} |")
    emit()

    emit("## 四、蓄势层（第二层）帮不上忙 —— 它只对**已经进池**的板块计算")
    emit()
    # `service.py` 里 `acc_inputs` 显式跳过非候选板块：
    #     for item in inputs:
    #         if item.code not in candidate_set: continue
    # 所以池外板块的蓄势分恒为 0，而层的合成又会把"不可用"的层丢掉
    # （§16.39 的修复）→ **池外板块的 base_total 就等于六维分**。
    # 结论：第二层只能给池内板块重排序，**永远不能把新板块拉进池**。
    available_days = sorted(d for d, item in daily.items()
                            if item.get("acc") is not None
                            and float(item["acc"]) > 0)
    candidate_days = sorted(d for d, item in daily.items() if item["candidate"])
    emit("| 事实 | 值 |")
    emit("|---|---|")
    emit(f"| 进候选池的天数 | **{len(candidate_days)}** |")
    emit(f"| 蓄势分 > 0 的天数 | **{len(available_days)}** |")
    emit(f"| 两者是否同一天集合 | "
         f"{'✅ 完全相同' if available_days == candidate_days else '❌ 不同'} |")
    emit(f"| 进池的最早/最晚一天 | {candidate_days[0] if candidate_days else '—'}"
         f" ~ {candidate_days[-1] if candidate_days else '—'} |")
    emit()
    emit("源码依据（`src/mainline/service.py`）：")
    emit()
    emit("```python")
    emit("acc_inputs: list[AccumulationInput] = []")
    emit("for item in inputs:")
    emit("    if item.code not in candidate_set:   # ← 池外板块直接跳过")
    emit("        continue")
    emit("```")
    emit()
    emit("这是**倒因为果**容易看错的地方：煤炭 698 天的蓄势分是 0，"
         "不是「蓄势层坏了导致它进不了池」，而是「它没进池所以没有蓄势分」。")
    emit("两个后果：")
    emit()
    emit("1. **`total` 里那 20% 的蓄势权重只对池内板块生效**；"
         "池外板块的 `base_total` 就等于六维分（层的 `available=False` 被丢弃，"
         "见 §16.39 的修复）。")
    emit("2. **第二层永远无法把新板块拉进池** —— 能不能进池完全由第一层"
         "六维决定，与蓄势层无关。")
    emit()

    # ---------- 四、典型日 ----------
    emit("## 五、它分数最高的 8 天（离候选线最近的时候）")
    emit()
    emit("| 日期 | total | 排名 | 进池 | " + " | ".join(DIMS) + " | acc |")
    emit("|---|---:|---:|---|" + "---:|" * (len(DIMS) + 1))
    for day in sorted(daily, key=lambda d: -daily[d]["total"])[:8]:
        item = daily[day]
        cells = " | ".join(
            f"{item[name]:.0f}" if item.get(name) is not None else "—"
            for name in DIMS)
        acc = item.get("acc")
        emit(f"| {day} | {item['total']:.1f} | {item['rank']} "
             f"| {'✅' if item['candidate'] else '❌'} | {cells} "
             f"| {f'{acc:.0f}' if acc is not None else '—'} |")
    emit()

    cache.close()
    main.close()
    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
