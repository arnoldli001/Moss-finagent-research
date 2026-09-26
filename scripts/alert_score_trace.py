"""把某个板块**每一次告警**的打分过程原样摊开（公式 + 每一维的取值）。

## 用户要什么

> 「几次上报告警的各个权重参数的数值多少，计算分数的过程公式我看下。」

所以要的是**可核对的算式**，不是"得分 81.55"这种结果。

## 线上真实的公式（从 `service.py` 读出来的，不是复述）

    第一层 six_dim = Σ(dim_i.score × w_i) / Σ(w_i)
                     · 只算 `available=True` 的维度（缺项剔出分母）
                     · `reversed=True` 的维度（trading / technical）取 `100 − score`

    景气度 dim = Σ(sub_j 的当日横截面分位 × sub_weight_j) / Σ(sub_weight_j)
                 sub 权重：roe_yoy 0.50 / profit_yoy 0.30 / revenue_yoy 0.20
                 （`six_dim.SUB_WEIGHTS`）

    第二层 accumulation = 同样规则，4 个子维（leverage/northbound/volume_price/etf）

    base_total = Σ(layer.score × layer_w) / Σ(layer_w)     仅对**候选池内**
                 = [six×80 + acc×20] / (80+20)             两者都可用时
                 = six_dim                                  池外（第二层没算过）

    gate_bonus 分两种形态，这是最容易看错的一处：
        bonus_potential = 共振算出来的加分（**排序时用**）
        gate_bonus      = 只有**入选精选前 10** 才兑现，否则置 0

    total = clamp(base_total + etf_bonus + gate_bonus, 0, 100)
            `etf_bonus` 无条件兑现

## 输出

每个告警日一块：六维表 → 景气度展开（含子因子的**均值原始值**与
**当日横截面分位**）→ 蓄势层 → base_total → 加分 → total → 档位判定。
最后附一张"简表"，方便横着看 16 次告警的差异。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/alert_score_trace.py \\
        --board 885497.TI --score-table mainline_score_bak_v26_prefloor \\
        --alert-table mainline_alert_bak_v26_prefloor \\
        --out docs/MAINLINE_TRACE_885497.md
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

from scripts.prosperity_variant_experiment import percentile_within_day  # noqa: E402

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
SUB = {"roe_yoy": 0.50, "profit_yoy": 0.30, "revenue_yoy": 0.20}
RAW_KEYS = {"roe_yoy": "roe_yoy", "profit_yoy": "netprofit_yoy",
            "revenue_yoy": "or_yoy"}
LAYER_W = {"six_dim": 80.0, "accumulation": 20.0}


def main() -> int:
    parser = argparse.ArgumentParser(description="告警打分全过程回放")
    parser.add_argument("--board", required=True)
    parser.add_argument("--score-table", default="mainline_score_bak_v26_prefloor")
    parser.add_argument("--alert-table", default="mainline_alert_bak_v26_prefloor")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    alerts = [dict(row) for row in conn.execute(
        f"SELECT trade_date, level, score, entry_close FROM {args.alert_table}"
        " WHERE board_code = ? ORDER BY trade_date", (args.board,))]
    # 当日横截面：景气度子因子的分位要**跨板块**算，所以每天都要全池
    days_needed = {str(row["trade_date"]) for row in alerts}
    cross: dict[str, dict[str, dict[str, float]]] = {}
    for day in days_needed:
        columns: dict[str, dict[str, float]] = {sub: {} for sub in SUB}
        for row in conn.execute(
                f"SELECT board_code, payload FROM {args.score_table}"
                " WHERE trade_date = ?", (day,)):
            payload = json.loads(str(row["payload"] or "{}"))
            for layer in (payload.get("layers") or []):
                for dim in (layer.get("dimensions") or []):
                    if str(dim.get("key")) != "prosperity":
                        continue
                    raw = dim.get("raw") or {}
                    for sub, key in RAW_KEYS.items():
                        value = raw.get(sub if sub != "profit_yoy" else
                                        "profit_yoy")
                        if not isinstance(value, (int, float)):
                            value = raw.get(key)
                        if isinstance(value, (int, float)):
                            columns[sub][str(row["board_code"])] = float(value)
        cross[day] = {sub: percentile_within_day(columns[sub]) for sub in SUB}

    traces: list[dict] = []
    for row in alerts:
        day = str(row["trade_date"])
        score_row = conn.execute(
            f"SELECT total, base_total, six_dim, accumulation, gate_bonus,"
            f" rank, candidate, selected, level, payload FROM {args.score_table}"
            " WHERE board_code = ? AND trade_date = ?",
            (args.board, day)).fetchone()
        if score_row is None:
            continue
        payload = json.loads(str(score_row["payload"] or "{}"))
        dims: list[dict] = []
        leader_dims: list[dict] = []
        prosperity_sub: dict[str, float] = {}
        accumulation_dims: list[dict] = []
        for layer in (payload.get("layers") or []):
            key = str(layer.get("key"))
            for dim in (layer.get("dimensions") or []):
                entry = {"key": str(dim.get("key")),
                         "score": dim.get("score"),
                         "weight": dim.get("weight"),
                         "available": bool(dim.get("available")),
                         "reversed": bool(dim.get("reversed")),
                         "note": str(dim.get("note") or ""),
                         "raw": dim.get("raw") or {}}
                # ⚠️ 必须**按层**分流：payload 里除了 six_dim / accumulation，
                # 还有第三层 `leader`（concentration / seat）。第一版把非
                # accumulation 的维度全塞进了六维表，于是权重和变成 170、200
                # 而不是 100 —— 而"六维分"看起来还是对的，只有权重列是错的，
                # 属于最容易糊弄过去的错法。
                if key == "accumulation":
                    accumulation_dims.append(entry)
                elif key == "leader":
                    leader_dims.append(entry)
                elif key == "six_dim":
                    dims.append(entry)
                if key == "six_dim" and entry["key"] == "prosperity":
                    for sub in SUB:
                        value = entry["raw"].get(sub)
                        if isinstance(value, (int, float)):
                            prosperity_sub[sub] = float(value)
        traces.append({"day": day, "level": str(row["level"]),
                       "alert_score": float(row["score"] or 0.0),
                       "entry_close": row["entry_close"],
                       "total": float(score_row["total"] or 0.0),
                       "base_total": float(score_row["base_total"] or 0.0),
                       "six_dim": float(score_row["six_dim"] or 0.0),
                       "accumulation": float(score_row["accumulation"] or 0.0),
                       "gate_bonus": float(score_row["gate_bonus"] or 0.0),
                       "bonus_potential": float(payload.get("bonus_potential") or 0.0),
                       "etf_bonus": float(payload.get("etf_bonus") or 0.0),
                       "etf_level": payload.get("etf_level"),
                       "resonance": bool(payload.get("resonance")),
                       "rank": int(score_row["rank"] or 0),
                       "candidate": bool(score_row["candidate"]),
                       "selected": bool(score_row["selected"]),
                       "dims": dims, "acc_dims": accumulation_dims,
                       "leader_dims": leader_dims,
                       "prosperity_sub": prosperity_sub,
                       "pcts": {sub: cross.get(day, {}).get(sub, {}).get(
                           args.board) for sub in SUB}})

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    hit = cache.execute("SELECT name FROM ml_board WHERE code = ?",
                        (args.board,)).fetchone()
    name = str(hit["name"]) if hit else args.board
    cache.close()
    conn.close()

    emit(f"# {name}（{args.board}）告警打分全过程")
    emit()
    emit(f"> 由 `scripts/alert_score_trace.py` 生成（只读）。评分表 "
         f"`{args.score_table}`、告警表 `{args.alert_table}`。")
    emit(f"> 共 **{len(traces)}** 次告警。")
    emit()
    emit("## 公式（与 `service.py` 一致）")
    emit()
    emit("```")
    emit("六维    six_dim = Σ(dim.score × dim.weight) / Σ(dim.weight)")
    emit("                 只算 available=True 的维度；reversed 维取 (100 − score)")
    emit("景气度  prosperity = Σ(子因子当日横截面分位 × 子权重) / Σ(子权重)")
    emit("                    子权重 roe_yoy 0.50 / profit_yoy 0.30 / revenue_yoy 0.20")
    emit("蓄势层  accumulation = 同规则，4 个子维")
    emit("基线分  base_total = [six_dim×80 + accumulation×20] / (80+20)   ← 池内")
    emit("                     = six_dim                                  ← 池外")
    emit("加分    bonus_potential = 龙头共振加分（**排序时**用）")
    emit("        gate_bonus      = 只有**入选精选前 10** 才兑现，否则为 0")
    emit("        etf_bonus       = 无条件兑现")
    emit("最终分  total = clamp(base_total + etf_bonus + gate_bonus, 0, 100)")
    emit("```")
    emit()

    for item in traces:
        emit(f"## {item['day']}　告警 `{item['level']}`"
             f"（存档分 {item['alert_score']:.2f}，买入价 {item['entry_close']}）")
        emit()
        usable = [d for d in item["dims"] if d["available"]]
        total_weight = sum(float(d["weight"] or 0.0) for d in usable)
        emit("### 1) 六维"
              f"　（当日全市场排名 {item['rank']}，"
              f"{'进' if item['candidate'] else '**未**进'}候选池）")
        emit()
        emit("| 维度 | 原始分 | 反向? | 参与合成的值 | 权重 | 贡献 |")
        emit("|---|---:|:---:|---:|---:|---:|")
        for dim in item["dims"]:
            score = dim["score"]
            if score is None or not dim["available"]:
                emit(f"| {dim['key']} | — | "
                     f"{'是' if dim['reversed'] else '否'} | — "
                     f"| {dim['weight']} | 不可用（剔出分母） |")
                continue
            used = (100.0 - float(score)) if dim["reversed"] else float(score)
            weight = float(dim["weight"] or 0.0)
            contribution = used * weight / total_weight if total_weight else 0.0
            emit(f"| {dim['key']} | {float(score):.2f} "
                 f"| {'**是**' if dim['reversed'] else '否'} | {used:.2f} "
                 f"| {weight:g} | {contribution:.2f} |")
        emit(f"| **six_dim** | | | | Σ={total_weight:g} "
             f"| **{item['six_dim']:.2f}** |")
        emit()
        if item["prosperity_sub"]:
            emit("### 2) 景气度展开（三个子因子）")
            emit()
            emit("| 子因子 | 成分股均值(原始) | 当日横截面分位 | 子权重 | 贡献 |")
            emit("|---|---:|---:|---:|---:|")
            den = sum(SUB[sub] for sub in SUB
                      if item["pcts"].get(sub) is not None)
            for sub in SUB:
                value = item["prosperity_sub"].get(sub)
                pct = item["pcts"].get(sub)
                shown = f"{value:.2f}" if isinstance(value, (int, float)) else "—"
                pct_text = f"{pct:.1f}" if pct is not None else "—"
                contrib = (f"{pct * SUB[sub] / den:.2f}"
                           if pct is not None and den else "—")
                emit(f"| {sub} | {shown} | {pct_text} | {SUB[sub]:.2f} "
                     f"| {contrib} |")
            emit()
        if item["acc_dims"]:
            emit("### 3) 蓄势层（第二层，仅池内计算）")
            emit()
            emit("| 子维度 | 分数 | 权重 |")
            emit("|---|---:|---:|")
            for dim in item["acc_dims"]:
                emit(f"| {dim['key']} | "
                     f"{float(dim['score']):.2f} | {dim['weight']} |")
            emit()
        if item["leader_dims"]:
            emit("### 3b) 第三层：龙头（**不进六维**，只决定 `gate_bonus`）")
            emit()
            emit("| 子维度 | 分数 | 权重 | 备注 |")
            emit("|---|---:|---:|---|")
            for dim in item["leader_dims"]:
                score = dim["score"]
                shown = f"{float(score):.2f}" if isinstance(score, (int, float)) \
                    else "不可用"
                emit(f"| {dim['key']} | {shown} | {dim['weight']} "
                     f"| {dim['note'][:40]} |")
            emit()
        emit("### 4) 合成与判定")
        emit()
        emit("```")
        if item["candidate"] and item["acc_dims"]:
            emit(f"base_total = (six_dim {item['six_dim']:.2f} × 80 "
                 f"+ accumulation {item['accumulation']:.2f} × 20) / 100 "
                 f"= {item['base_total']:.2f}")
        else:
            emit(f"base_total = six_dim = {item['six_dim']:.2f}"
                  "   ← 池外板块第二层没算过")
        emit(f"gate_bonus = {item['gate_bonus']:.2f}"
             f"（共振潜力 {item['bonus_potential']:.2f}，"
             f"{'已入选精选→兑现' if item['gate_bonus'] else '未入选精选→不兑现'}）")
        emit(f"etf_bonus  = {item['etf_bonus']:.2f}"
             f"（ETF 异动级别 {item['etf_level']}）")
        emit(f"total = clamp({item['base_total']:.2f} + {item['etf_bonus']:.2f}"
             f" + {item['gate_bonus']:.2f}) = {item['total']:.2f}")
        emit(f"共振 resonance = {item['resonance']}")
        emit(f"存档 level = {item['level']}")
        emit("```")
        emit()

    # 简表
    emit("## 汇总简表（横着看每次告警的差异）")
    emit()
    emit("| 日期 | 档位 | rank | 进池 | 六维 | 蓄势 | 基线分 | ETF加分 "
         "| 共振加分 | 共振潜力 | total | 存档分 |")
    emit("|---|---|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for item in traces:
        emit(f"| {item['day']} | {item['level']} | {item['rank']} "
             f"| {'✅' if item['candidate'] else '❌'} "
             f"| {item['six_dim']:.2f} | {item['accumulation']:.2f} "
             f"| {item['base_total']:.2f} | {item['etf_bonus']:.1f} "
             f"| {item['gate_bonus']:.1f} | {item['bonus_potential']:.1f} "
             f"| **{item['total']:.2f}** | {item['alert_score']:.2f} |")
    emit()
    emit("⚠️ 「存档分」是告警行里记的分数，与 `total` 应当一致；"
         "不一致就说明当时用的配置与本次重算不同（例如阈值改过）。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
