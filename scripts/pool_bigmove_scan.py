"""候选池的「大行情」因子扫描：谁能挑出未来 20 日涨超 10% 的板块？

## 为什么不再扫阈值

`precision_recall_curve.py` 已经给过一个决定性结论：把门槛往上抬，
每少 5~8 个百分点的误报要付出 **7~27 个百分点**的真值覆盖 —— 比例
1.2~3.3，永远是亏的。到 floor=89 时误报降到 40%，真值却丢了 93%。
**阈值这条路已经走完了**。

所以本脚本换问题：候选池里，有没有**别的因子**能提升「告警之后真的
出现大行情」的命中率？如果有，它就该进打分/门限；如果没有，就该承认
现有六维已经是这套数据能给的上限，不再在权重上折腾。

## 两个标签，必须都看

- `big`：未来 20 日**收盘**收益 > 10%（用户口径的"涨幅大于 10%"）。
- `rel_big`：未来 20 日收益**高于当日全市场板块中位数**。

只报 `big` 会骗自己：`big` 的基准率随大盘整体涨跌大幅波动，
一个因子只要"在大盘好的日子数值高"就能拿到漂亮的池化 AUC，
而它对**横截面选板**毫无贡献。`rel_big` 把当日共同项减掉了，
量的是纯粹的挑选能力。两个一起看，且必须三个窗口同向。

## 复现判据（本项目的老规矩）

三个窗口（旧 / 中 / 新）**都**要满足，不看"两个同号"。而且因为同时
扫了几十个因子，多重比较会制造假阳性，所以：

1. 三个窗口的 `rel_big` AUC 点估计都要 ≥ 阈值；
2. 再对入选者做**按日 block bootstrap**（重采样交易日，不是重采样样本行），
   要求三个窗口的 5% 分位都 > 0.5。

第 2 条是关键：把同一天几十个板块当独立样本会把置信区间压窄一个量级。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/pool_bigmove_scan.py
    .venv\\Scripts\\python.exe scripts/pool_bigmove_scan.py --out docs/MAINLINE_BIGMOVE_SCAN.md
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

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"

WINDOWS = (("20231009", "20240806", "旧"), ("20240901", "20250930", "中"),
           ("20251001", "20260918", "新"))
HORIZON = 20
BIG = 0.10
#: 当日候选池里至少要有这么多板块，横截面分位才有意义
MIN_BOARDS = 8
#: 单因子入选门槛（三个窗口的 rel_big AUC 点估计）
AUC_FLOOR = 0.52
#: bootstrap 重采样次数（按日 block）
BOOT = 200
BOOT_FLOOR = 0.50

SCALARS = ("six_dim_score", "accumulation_score", "leader_score", "base_total",
           "total", "etf_bonus", "gate_bonus", "bonus_potential",
           "breakout_ceiling", "six_dim_coverage", "accumulation_coverage",
           "resonance_ratio", "change_pct")
FLAGS = ("breakout", "resonance", "promoted")


def load_scores(table: str, start: str, end: str) -> tuple[dict[str, pd.DataFrame],
                                                          pd.DataFrame]:
    """读评分表 → `({因子名: 宽表}, 候选掩码宽表)`，索引=日期、列=板块。"""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, rank, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        item: dict = {"day": str(row["trade_date"]),
                      "code": str(row["board_code"]),
                      "candidate": bool(payload.get("candidate"))}
        for name in SCALARS:
            item[name] = float(payload.get(name) or 0.0)
        for name in FLAGS:
            item[name] = 1.0 if payload.get(name) else 0.0
        # 全市场排名：越小越好 → 取负，统一成「越大越好」
        rank_value = row["rank"]
        item["rank_neg"] = (-float(rank_value) if rank_value is not None
                            else float("nan"))
        for layer in (payload.get("layers") or []):
            layer_key = str(layer.get("key") or "")
            for dim in (layer.get("dimensions") or []):
                if not dim.get("available"):
                    continue
                key = f"{layer_key}.{dim.get('key')}"
                item[key] = float(dim.get("score") or 0.0)
                raw = dim.get("raw") or {}
                if isinstance(raw, dict):
                    for sub, value in raw.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            item[f"{key}.raw.{sub}"] = float(value)
        records.append(item)
    long = pd.DataFrame(records)
    if long.empty:
        return {}, pd.DataFrame()
    mask = long.pivot_table(index="day", columns="code", values="candidate",
                            aggfunc="max").astype(float)
    frames: dict[str, pd.DataFrame] = {}
    for column in long.columns:
        if column in ("day", "code", "candidate"):
            continue
        frames[column] = long.pivot_table(index="day", columns="code",
                                          values=column)
    return frames, mask


def load_bars(start: str, end: str) -> dict[str, pd.DataFrame]:
    """板块行情派生特征（全部只用当日及之前的数据，严格 PIT）。

    ⚠️ 必须 `JOIN ml_calendar` 只取真交易日：`ml_board_bar` 在**休市日**
    也有零散行（20260501 有 32 个板块、20240916 有 685 个）。这些日期
    不在日历里，按并集建宽表时就是**一整行 NaN**，而 `rolling(20)`
    要求窗口内 20 个有效值 —— 一个 NaN 让之后 20 个交易日的滚动特征
    全部变 NaN。实测告警集 42% 的行 `vol20` 缺失就是这么来的
    （见 `docs/MAINLINE_CALENDAR_REPAIR.md`）。
    """
    # 多取 400 个自然日做滚动窗口的预热，避免窗口开头的特征全是 NaN
    warm = (pd.Timestamp(start) - pd.Timedelta(days=400)).strftime("%Y%m%d")
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT b.board_code, b.trade_date, b.close, b.amount FROM ml_board_bar b"
        " JOIN ml_calendar k ON k.trade_date = b.trade_date"
        " WHERE b.trade_date BETWEEN ? AND ? ORDER BY b.trade_date",
        (warm, end)).fetchall()
    flow_rows = conn.execute(
        "SELECT f.board_code, f.trade_date, f.net_amount, f.amount"
        " FROM ml_board_flow f JOIN ml_calendar k ON k.trade_date = f.trade_date"
        " WHERE f.trade_date BETWEEN ? AND ?", (warm, end)).fetchall()
    conn.close()

    def wide(items: list[sqlite3.Row], column: str) -> pd.DataFrame:
        frame = pd.DataFrame([{"code": str(r["board_code"]),
                               "day": str(r["trade_date"]),
                               "value": float(r[column] or 0.0)}
                              for r in items])
        return frame.pivot_table(index="day", columns="code", values="value")

    close = wide(rows, "close").sort_index()
    amount = wide(rows, "amount").sort_index()
    pct = close.pct_change()

    out: dict[str, pd.DataFrame] = {}
    out["bar.ret5"] = close / close.shift(5) - 1.0
    out["bar.ret20"] = close / close.shift(20) - 1.0
    out["bar.ret60"] = close / close.shift(60) - 1.0
    out["bar.vol20"] = pct.rolling(20).std()
    out["bar.dist_high60"] = close / close.rolling(60).max() - 1.0
    out["bar.dist_high20"] = close / close.rolling(20).max() - 1.0
    # 量能：5 日均量 / 20 日均量（都只含当日及之前）
    out["bar.amt_ratio5_20"] = amount.rolling(5).mean() / amount.rolling(20).mean()
    # 短长动量差：近 5 日相对近 20 日的斜率
    out["bar.ret5_minus_20"] = out["bar.ret5"] - (close / close.shift(20) - 1.0) / 4.0
    # 距 60 日最低点的位置（0 = 最低，1 = 最高）
    low60 = close.rolling(60).min()
    high60 = close.rolling(60).max()
    out["bar.pos60"] = (close - low60) / (high60 - low60).replace(0.0, np.nan)
    # 连续上涨天数（逐行推进；`groupby` 需要 1 维键，宽表上不成立）
    up = pct.gt(0).to_numpy(dtype=float)
    streak = np.zeros_like(up)
    for step in range(1, up.shape[0]):
        streak[step] = np.where(up[step] > 0, streak[step - 1] + 1.0, 0.0)
    out["bar.up_streak"] = pd.DataFrame(streak, index=close.index,
                                        columns=close.columns)

    flow = pd.DataFrame([{"code": str(r["board_code"]),
                          "day": str(r["trade_date"]),
                          "net": float(r["net_amount"] or 0.0),
                          "amt": float(r["amount"] or 0.0)} for r in flow_rows])
    if not flow.empty:
        net = flow.pivot_table(index="day", columns="code", values="net").sort_index()
        famt = flow.pivot_table(index="day", columns="code",
                                values="amt").sort_index()
        out["flow.net5"] = (net.rolling(5).sum()
                            / famt.rolling(5).sum().replace(0.0, np.nan))
        out["flow.net20"] = (net.rolling(20).sum()
                             / famt.rolling(20).sum().replace(0.0, np.nan))
    return out


def load_labels(start: str, end: str) -> dict[str, pd.DataFrame]:
    """未来 20 日的收益 / 最高收盘涨幅 / 相对全市场中位数的超额（都是宽表）。

    最高收盘用 `concat + groupby(level=0).max()`，比反转滚动更好读，
    也不依赖索引单调性 —— 这个脚本上一版就在这类"看起来能跑"的写法上
    差点埋进一个静默错位。
    """
    tail = (pd.Timestamp(end) + pd.Timedelta(days=45)).strftime("%Y%m%d")
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT b.board_code, b.trade_date, b.close FROM ml_board_bar b"
        " JOIN ml_calendar k ON k.trade_date = b.trade_date"
        " WHERE b.trade_date BETWEEN ? AND ? ORDER BY b.trade_date",
        (start, tail)).fetchall()
    conn.close()
    frame = pd.DataFrame([{"code": str(r["board_code"]), "day": str(r["trade_date"]),
                           "close": float(r["close"] or 0.0)} for r in rows])
    close = frame.pivot_table(index="day", columns="code", values="close").sort_index()
    fwd = close.shift(-HORIZON) / close - 1.0
    peak = pd.concat([close.shift(-step) for step in range(1, HORIZON + 1)]
                     ).groupby(level=0).max()
    fwd_peak = peak / close - 1.0
    return {
        "fwd": fwd,
        "rel": fwd.sub(fwd.median(axis=1), axis=0),
        "fwd_peak": fwd_peak,
        "rel_peak": fwd_peak.sub(fwd_peak.median(axis=1), axis=0),
    }


def pooled(feat: pd.DataFrame, mask: pd.DataFrame, label: pd.DataFrame
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """压成 `(因子当日分位, 标签, 行所属交易日序号, 有效交易日数)`。

    ⚠️ 必须把 `pct` 与 `label` 对齐到**同一套 index/columns** 再 `to_numpy()`：
    行情表的板块集合比评分表大，若各自 `to_numpy()`，列顺序可能不同 ——
    这会让因子和标签错位，而结果依然"看起来像个数"。本项目栽过同类跟头，
    所以这里显式 `reindex`。
    """
    cols = list(mask.columns)
    index = mask.index
    # ⚠️ `astype(bool)` 之前必须先 `fillna(0)`：NaN 转 bool 是 True，
    # 会把"该板块当天根本不在池里"变成"在池里"。
    cond = mask.fillna(0.0).astype(bool)
    x = feat.reindex(index=index, columns=cols).where(cond)
    y = label.reindex(index=index, columns=cols).where(cond)
    pct = x.rank(axis=1, pct=True)
    valid = pct.notna() & y.notna()
    days = [day for day in index if int(valid.loc[day].sum()) >= MIN_BOARDS]
    if not days:
        return np.empty(0), np.empty(0), np.empty(0), 0
    sub_x = pct.loc[days].to_numpy(dtype=float)
    sub_y = y.loc[days].to_numpy(dtype=float)
    keep = np.isfinite(sub_x) & np.isfinite(sub_y)
    day_index = np.repeat(np.arange(len(days)), keep.sum(axis=1))
    return sub_x[keep], sub_y[keep], day_index, len(days)


def auc_rank(x: np.ndarray, y: np.ndarray) -> float:
    """Mann-Whitney AUC（`y` 是 0/1）。"""
    if x.size == 0:
        return float("nan")
    pos = y > 0
    n1 = float(pos.sum())
    n0 = float(x.size - n1)
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = pd.Series(x).rank().to_numpy()
    return float((order[pos].sum() - n1 * (n1 + 1) / 2.0) / (n0 * n1))


def bootstrap_auc(x: np.ndarray, y: np.ndarray, day_index: np.ndarray,
                  *, draws: int, seed: int) -> tuple[float, float]:
    """**按日** block bootstrap：整日重采样，保住日内相关性。"""
    rng = np.random.default_rng(seed)
    groups = [np.flatnonzero(day_index == value) for value in np.unique(day_index)]
    values: list[float] = []
    for _ in range(draws):
        picked = rng.integers(0, len(groups), size=len(groups))
        idx = np.concatenate([groups[i] for i in picked])
        value = auc_rank(x[idx], y[idx])
        if np.isfinite(value):
            values.append(value)
    if not values:
        return float("nan"), float("nan")
    return float(np.percentile(values, 5)), float(np.percentile(values, 95))


def main() -> int:
    parser = argparse.ArgumentParser(description="候选池大行情因子扫描")
    parser.add_argument("--table", default="mainline_score")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", default="docs/mainline_iterations/pool_quality.json")
    parser.add_argument("--seed", type=int, default=20260921)
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    emit("# 候选池的「大行情」因子扫描")
    emit()
    emit(f"> 由 `scripts/pool_bigmove_scan.py` 生成，评分表 `{args.table}`。")
    emit(f"> 标签：未来 {HORIZON} 日收盘收益 > {BIG:.0%}（`big`），"
         f"以及**相对当日全市场中位数**的超额 > 0（`rel_big`，纯横截面口径）。")
    emit("> 特征全部取**当日横截面分位**，消除跨日尺度漂移；"
         "三个窗口同向且 bootstrap 5% 分位 > 0.5 才算规律。")
    emit()

    windows_data: dict[str, dict] = {}
    for start, end, label in WINDOWS:
        frames, mask = load_scores(args.table, start, end)
        if not frames:
            emit(f"【{label}】没有评分数据，跳过")
            continue
        labels = load_labels(start, end)
        for name, frame in load_bars(start, end).items():
            frames[name] = frame
        windows_data[label] = {"frames": frames, "mask": mask, **labels}
        emit(f"- 【{label} {start}~{end}】候选行 "
              f"{int(np.nansum(mask.to_numpy()))}，交易日 {mask.shape[0]}，"
              f"特征 {len(frames)} 个")

    if len(windows_data) < 3:
        emit(f"❌ 只有 {len(windows_data)} 个窗口有数据，无法做跨窗口复现")
        return 2

    labels_all = [label for _, _, label in WINDOWS if label in windows_data]

    # ---------- 逐因子逐窗口 ----------
    names = sorted({name for item in windows_data.values() for name in item["frames"]})
    stats: dict[str, dict[str, dict]] = {}
    for name in names:
        stats[name] = {}
        for label in labels_all:
            shelf = windows_data[label]
            feat = shelf["frames"].get(name)
            if feat is None:
                continue
            entry: dict = {}
            for tag, target, binary in (("big", shelf["fwd"], True),
                                        ("rel", shelf["rel"], False),
                                        ("peak", shelf["fwd_peak"], True),
                                        ("rel_peak", shelf["rel_peak"], False)):
                lab = ((target > BIG) if binary else (target > 0)).astype(float)
                xs, ys, day_index, days = pooled(feat, shelf["mask"], lab)
                if xs.size == 0:
                    continue
                entry[tag] = {"auc": auc_rank(xs, ys), "rows": int(xs.size),
                              "days": days, "base": float(ys.mean()),
                              "x": xs, "y": ys, "day_index": day_index}
            if entry:
                stats[name][label] = entry

    def worst(tag: str, name: str) -> float:
        values = [stats.get(name, {}).get(label, {}).get(tag, {}).get(
            "auc", float("nan")) for label in labels_all]
        values = [v for v in values if np.isfinite(v)]
        return min(values) if len(values) == len(labels_all) else float("nan")

    emit()
    emit("## 一、单因子：`rel_big` AUC（按三窗口最差排序）")
    emit()
    emit("| 因子 | " + " | ".join(f"{lb} AUC" for lb in labels_all)
         + " | 最差 | 行数 | 基准率 |")
    emit("|---|" + "---:|" * (len(labels_all) + 3))
    ranked = sorted((n for n in names if np.isfinite(worst("rel", n))),
                    key=lambda n: -worst("rel", n))
    for name in ranked[:40]:
        cells = []
        rows = 0
        base = float("nan")
        for lb in labels_all:
            got = stats[name][lb].get("rel")
            cells.append(f"{got['auc']:.4f}" if got else "—")
            if got:
                rows = max(rows, got["rows"])
                base = got["base"]
        emit(f"| `{name}` | " + " | ".join(cells)
             + f" | **{worst('rel', name):.4f}** | {rows} | {base:.1%} |")

    emit()
    emit("## 二、基准对照：现有打分的两把尺子")
    emit()
    emit("| 因子 | " + " | ".join(f"{lb} AUC" for lb in labels_all) + " | 最差 |")
    emit("|---|" + "---:|" * (len(labels_all) + 1))
    for name in ("total", "base_total", "six_dim_score", "accumulation_score",
                 "rank_neg"):
        if name not in stats:
            continue
        cells = []
        for lb in labels_all:
            got = stats[name][lb].get("rel")
            cells.append(f"{got['auc']:.4f}" if got else "—")
        emit(f"| `{name}` | " + " | ".join(cells)
             + f" | **{worst('rel', name):.4f}** |")

    # ---------- 入选 + bootstrap ----------
    emit()
    emit("## 三、入选因子与按日 bootstrap")
    emit()
    emit(f"门槛：三窗口 `rel_big` AUC 点估计均 ≥ {AUC_FLOOR}，"
         f"且 bootstrap 5% 分位均 > {BOOT_FLOOR}。")
    emit()
    emit("| 因子 | " + " | ".join(f"{lb} AUC [5%,95%]" for lb in labels_all)
         + " | 判定 |")
    emit("|---|" + "---:|" * (len(labels_all) + 1))
    survivors: list[str] = []
    for name in ranked:
        if worst("rel", name) < AUC_FLOOR:
            continue
        cells = []
        passed = True
        for label in labels_all:
            got = stats[name][label].get("rel")
            if got is None:
                passed = False
                cells.append("—")
                continue
            low, high = bootstrap_auc(got["x"], got["y"], got["day_index"],
                                      draws=BOOT, seed=args.seed)
            cells.append(f"{got['auc']:.4f} [{low:.4f},{high:.4f}]")
            if not np.isfinite(low) or low <= BOOT_FLOOR:
                passed = False
        if passed:
            survivors.append(name)
        emit(f"| `{name}` | " + " | ".join(cells)
             + f" | {'✅ 入选' if passed else '✗'} |")

    if not survivors:
        emit()
        emit("**没有因子通过三窗口 + bootstrap 的双重门槛。** "
             "这说明现有六维打分在候选池内的挑选能力已接近这套数据的上限，"
             "继续调权重/调门限都是在噪声里找规律。")
        _write(lines, args)
        return 0

    # ---------- 贪心组合 ----------
    emit()
    emit("## 四、贪心组合（等权分位，最大化三窗口最差 AUC）")
    emit()
    emit("| 步 | 加入因子 | " + " | ".join(f"{lb}" for lb in labels_all) + " | 最差 |")
    emit("|---:|---|" + "---:|" * (len(labels_all) + 1))

    def blend_of(chosen: list[str], label: str) -> pd.DataFrame | None:
        shelf = windows_data[label]
        # ⚠️ `mask` 是浮点表（NaN = 不是候选）：`.where()` 只接受布尔条件，
        # 且 NaN → bool 是 True，必须先 `fillna(0)` 再 `astype(bool)`。
        cond = shelf["mask"].fillna(0.0).astype(bool)
        parts = []
        for name in chosen:
            got = stats[name][label].get("rel")
            if got is None:
                return None
            sign = 1.0 if got["auc"] >= 0.5 else -1.0
            pct = shelf["frames"][name].reindex(
                index=shelf["mask"].index, columns=shelf["mask"].columns
            ).where(cond).rank(axis=1, pct=True)
            parts.append((pct - 0.5) * 2.0 * sign)
        if not parts:
            return None
        return sum(parts) / len(parts)

    def combined_auc(chosen: list[str]) -> tuple[float, list[float]]:
        per_window: list[float] = []
        for label in labels_all:
            shelf = windows_data[label]
            blend = blend_of(chosen, label)
            lab = (shelf["rel"] > 0).astype(float)
            if blend is None:
                per_window.append(float("nan"))
                continue
            xs, ys, _, _ = pooled(blend, shelf["mask"], lab)
            per_window.append(auc_rank(xs, ys) if xs.size else float("nan"))
        finite = [v for v in per_window if np.isfinite(v)]
        return (min(finite) if len(finite) == len(labels_all) else float("nan"),
                per_window)

    chosen: list[str] = []
    current, per_window = combined_auc(chosen)
    for step in range(1, 9):
        best: tuple[float, str, list[float]] | None = None
        for name in survivors:
            if name in chosen:
                continue
            value, detail = combined_auc([*chosen, name])
            if not np.isfinite(value):
                continue
            if best is None or value > best[0]:
                best = (value, name, detail)
        if best is None or best[0] <= current:
            break
        current, per_window = best[0], best[2]
        chosen.append(best[1])
        emit(f"| {step} | `{best[1]}` | "
             + " | ".join(f"{v:.4f}" for v in per_window)
             + f" | **{current:.4f}** |")
    emit()
    emit(f"最终组合（{len(chosen)} 个因子，等权）：" + "、".join(f"`{n}`" for n in chosen))
    emit(f"三窗口最差 `rel_big` AUC = **{current:.4f}**")

    emit()
    emit("## 五、组合分位的**头部命中率**（这才是能用的东西）")
    emit()
    emit("在候选池内按组合分位取前 20% / 后 20%，看未来 20 日真涨超 10% 的比例。")
    emit()
    emit("| 窗口 | 候选池基准 | 前 20% | 后 20% | 前 20% 相对基准 |")
    emit("|---|---:|---:|---:|---:|")

    def flat(frame: pd.DataFrame) -> np.ndarray:
        values = frame.to_numpy(dtype=float)
        return values[np.isfinite(values)]

    for label in labels_all:
        shelf = windows_data[label]
        blend = blend_of(chosen, label)
        if blend is None:
            continue
        in_pool = shelf["mask"].fillna(0.0).astype(bool)
        rank_pct = blend.where(in_pool).rank(axis=1, pct=True)
        big = (shelf["fwd"] > BIG).astype(float).where(in_pool)
        base = float(flat(big).mean())
        top = float(flat(big.where(rank_pct >= 0.8)).mean())
        bot = float(flat(big.where(rank_pct <= 0.2)).mean())
        emit(f"| {label} | {base:.1%} | **{top:.1%}** | {bot:.1%} "
             f"| {top / base - 1:+.0%} |")

    if args.json:
        target = ROOT / args.json
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "features": chosen, "survivors": survivors, "worst_auc": current,
            "per_window": per_window, "windows": labels_all,
            "auc_floor": AUC_FLOOR, "boot_floor": BOOT_FLOOR,
            "horizon": HORIZON, "big": BIG,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"组合定义 → {target}")

    _write(lines, args)
    return 0


def _write(lines: list[str], args: argparse.Namespace) -> None:
    if not args.out:
        return
    target = ROOT / args.out
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"记录 → {target}")


if __name__ == "__main__":
    raise SystemExit(main())
