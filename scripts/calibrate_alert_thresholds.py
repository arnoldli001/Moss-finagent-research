"""告警阈值重标定：换合成分尺度之后，把「告警政策」拉回原样。

## 为什么必须工具化

`alert.medium_score` / `strong_score` 是**绝对阈值**（不是分位）。
所以任何改变 `total` 尺度的改动（层权重、维度权重、加分区间）都会
**顺带改掉告警政策** —— 而这是静默的：配置里只改了一行权重，
告警量却可能翻倍或腰斩，于是"这轮指标变好了"到底是因为排序变好、
还是因为政策放宽，永远说不清。

本项目实测过一次：`layer_weights` 从 50/50 改成 100/0 之后，
`total` 的均值抬高约 17 分、方差减半；沿用 55/70 会让
**100% 的候选板块**满足 `total ≥ 55`（中信号 = 全选）。第一版按
"候选池内超过阈值的比例"重标定为 68/78，结果告警量变成 **1.84x** ——
因为 MEDIUM 还额外要求**龙头共振**，而 `gate_bonus > 0`（只有入选才兑现）
本身就意味着 `total ≥ 70`，所以中信号线实际切的是"共振板块"这一小撮，
用它去匹配**全候选池**的比例根本不是同一件事。

结论：标定必须**直接按 `_decide_level` 的真实判据**算，即
「候选 & 共振 & total ≥ M」与「候选 & 达标维度 ≥ 2 & total ≥ S」这两个
**条件分布**，而不是整体分布。

## 用法

    # 用旧的评分表 + 新的（哪怕只跑了一部分的）评分表来标定
    .venv\\Scripts\\python.exe scripts\\calibrate_alert_thresholds.py \\
        --reference mainline_score_bak_20260921_2015 \\
        --candidate mainline_score --ref-medium 55 --ref-strong 70

输出推荐阈值，并给出"预计告警条数"与参考轮的对比。**只读、不写配置。**
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "moss_finagent.db"
#: 搜索范围（0-100 分制）
GRID = tuple(float(x) for x in range(40, 101))


def load(path: Path, table: str, start: str, end: str,
         dim_thresholds: dict[str, float], default_dim: float,
         ratio: float, *, recompute_total: bool,
         mix_six_weight: float | None = None) -> list[dict]:
    """读出一行一块板的判定所需字段。

    `recompute_total` 决定 `total` 从哪里来，**这是标定正确与否的关键**：

    - `False`（默认）：读库里**存下来的** `total`。参考表必须这样 ——
      它的 `total` 是当时那套层权重算出来的，重算会把它换成另一套口径，
      于是"参考政策占比"根本不是原政策（实测会把参考的强信号占比从
      约 9% 抬到约 21%，推荐阈值随之偏高）。
    - `True`：按 `clamp(six_dim_score + etf_bonus + gate_bonus)` 重算。
      仅用于**尚未重跑**的场景（候选表还是旧层权重，需要预测新尺度）。
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, board_code, total, payload FROM {table}"
        " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchall()
    conn.close()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            continue
        if not payload.get("candidate"):
            continue
        dims = 0
        for layer in (payload.get("layers") or []):
            for dim in (layer.get("dimensions") or []):
                if not dim.get("available"):
                    continue
                base = float(dim_thresholds.get(str(dim.get("key")),
                                                default_dim) or default_dim)
                if base <= 0:
                    base = default_dim
                if float(dim.get("score") or 0.0) >= base * ratio:
                    dims += 1
        if mix_six_weight is not None:
            # 预测**修好之后 + 换层权重之后**的分数尺度：
            #   base_total = 只在**有数据**的层之间按权重平均（这是修复点）
            #   total      = clamp(base_total + etf_bonus + gate_bonus)
            # ⚠️ `gate_bonus` 用的是**库里已兑现的那个**（旧排序下的结果）。
            # 新排序会略微改变"谁入选"，所以这是**近似**；它只影响
            # "兑现加分"这一项，不影响 base_total 的口径。
            six = float(payload.get("six_dim_score") or 0.0)
            acc = float(payload.get("accumulation_score") or 0.0)
            six_ok = acc_ok = False
            for layer in (payload.get("layers") or []):
                key = str(layer.get("key") or "")
                if key == "six_dim":
                    six_ok = bool(layer.get("available"))
                elif key == "accumulation":
                    acc_ok = bool(layer.get("available"))
            parts: list[tuple[float, float]] = []
            if six_ok:
                parts.append((six, mix_six_weight))
            if acc_ok:
                parts.append((acc, 100.0 - mix_six_weight))
            weight = sum(item[1] for item in parts)
            base_total = (sum(score * w for score, w in parts) / weight
                          if weight > 0 else six)
            total = (base_total + float(payload.get("etf_bonus") or 0.0)
                     + float(payload.get("gate_bonus") or 0.0))
        elif recompute_total:
            total = (float(payload.get("six_dim_score") or 0.0)
                     + float(payload.get("etf_bonus") or 0.0)
                     + float(payload.get("gate_bonus") or 0.0))
        else:
            total = float(row["total"] or 0.0)
        out.append({"day": str(row["trade_date"]),
                    "code": str(row["board_code"]),
                    "total": min(100.0, total),
                    "resonance": bool(payload.get("resonance")),
                    "dims": dims})
    return out


def share(rows: list[dict], predicate) -> float:
    if not rows:
        return float("nan")
    return sum(1 for item in rows if predicate(item)) / len(rows)


def best_threshold(rows: list[dict], target: float, predicate) -> tuple[float, float]:
    """在网格上找使 `share` 最接近 `target` 的阈值。返回 `(阈值, 实际占比)`。"""
    best = (GRID[0], float("inf"), float("nan"))
    for value in GRID:
        actual = share(rows, lambda item, v=value: predicate(item, v))
        gap = abs(actual - target)
        if gap < best[1]:
            best = (value, gap, actual)
    return best[0], best[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="告警阈值重标定")
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--reference", default="mainline_score_bak_20260921_2015",
                        help="旧口径的评分表（决定目标告警政策）")
    parser.add_argument("--candidate", default="mainline_score",
                        help="新口径的评分表（只需与参考表有重叠日期）")
    parser.add_argument("--ref-medium", type=float, default=55.0)
    parser.add_argument("--ref-strong", type=float, default=70.0)
    parser.add_argument("--recompute-candidate", action="store_true",
                        help="候选表还是旧层权重时用：按 six+加分 预测新尺度")
    parser.add_argument("--mix-six-weight", type=float, default=None,
                        help="预测**修复+换权重之后**的尺度：按「只在有数据的"
                             "层之间平均」合成 base_total，再叠加加分。"
                             "例如 `--mix-six-weight 80` 对应 80/20。"
                             "⚠️ 修复前必须先跑本模式拿阈值，否则重打分要跑两遍")
    parser.add_argument("--out", default="",
                        help="把标定过程与结论写成 Markdown 记录（可复现产物）")
    parser.add_argument("--start", default="",
                        help="固定标定区间起点（默认取两表重叠）。"
                             "**必须固定**：候选表在重打分过程中会不断变长，"
                             "不固定则每次算出来的推荐值都会漂移，"
                             "事后无法复现当时为什么定这个数")
    parser.add_argument("--end", default="",
                        help="固定标定区间终点（默认取两表重叠）")
    args = parser.parse_args()

    # 同时打印与留存：标定结论是"改配置"的依据，必须能事后复现
    # （哪张表、哪个区间、什么判据、算出多少），否则下一轮无从对照。
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    from src.mainline.config import load_config
    cfg = load_config(force=True)
    dim_thresholds = dict(cfg.alert.dim_thresholds or {})
    default_dim = float(cfg.alert.default_dim_threshold)
    ratio = float(cfg.alert.strong_dim_ratio)
    min_dims = int(cfg.alert.strong_min_dims)

    path = Path(args.db)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    span_ref = conn.execute(
        f"SELECT MIN(trade_date), MAX(trade_date) FROM {args.reference}").fetchone()
    span_new = conn.execute(
        f"SELECT MIN(trade_date), MAX(trade_date) FROM {args.candidate}").fetchone()
    conn.close()
    start = max(str(span_ref[0]), str(span_new[0]))
    end = min(str(span_ref[1]), str(span_new[1]))
    # 固定区间优先：候选表在重打分过程中会变长，不固定就会漂移
    if args.start:
        start = max(start, args.start)
    if args.end:
        end = min(end, args.end)
    if start > end:
        print(f"❌ 两张表没有重叠日期：参考 {span_ref}、候选 {span_new}")
        return 2

    ref = load(path, args.reference, start, end, dim_thresholds, default_dim,
               ratio, recompute_total=False)
    new = load(path, args.candidate, start, end, dim_thresholds, default_dim,
               ratio, recompute_total=args.recompute_candidate,
               mix_six_weight=args.mix_six_weight)
    if not ref or not new:
        print(f"❌ 样本为空（参考 {len(ref)} / 新 {len(new)}），无法标定")
        return 2

    # 参考轮的两个**条件**占比 —— 这就是要保住的政策。
    # 注意不是"候选池里超过阈值的整体比例"：MEDIUM 还要求龙头共振，
    # 而共振是小概率事件，用整体比例标定会让告警量偏移（实测 1.84x）。
    p_strong = share(ref, lambda i: i["dims"] >= min_dims
                     and i["total"] >= args.ref_strong)
    p_medium = share(ref, lambda i: i["resonance"]
                     and i["total"] >= args.ref_medium)
    strong, got_strong = best_threshold(
        new, p_strong,
        lambda i, v: i["dims"] >= min_dims and i["total"] >= v)
    medium, got_medium = best_threshold(
        new, p_medium, lambda i, v: i["resonance"] and i["total"] >= v)

    def counts(rows: list[dict], m: float, s: float) -> tuple[int, int]:
        """预测等级条数（只判"会不会给出等级"，不含确认/冷却的抑制）。"""
        strong_n = sum(1 for i in rows
                       if i["dims"] >= min_dims and i["total"] >= s)
        medium_n = sum(1 for i in rows
                       if i["total"] >= m and i["resonance"]
                       and not (i["dims"] >= min_dims and i["total"] >= s))
        return strong_n, medium_n

    rs, rm = counts(ref, args.ref_medium, args.ref_strong)
    ns, nm = counts(new, medium, strong)

    emit(f"# 告警阈值标定记录（{start} ~ {end}）")
    emit()
    emit("> 由 `scripts/calibrate_alert_thresholds.py` 生成。")
    emit("> **为什么必须标定**：`medium_score`/`strong_score` 是绝对阈值，"
         "任何改变 `total` 尺度的改动（层权重、维度权重、加分区间）都会"
         "**静默改掉告警政策**。实测某次沿用旧阈值让告警量变成 1.84x。")
    emit()
    emit("## 一、输入")
    emit()
    emit(f"- 参考表（决定目标政策）：`{args.reference}`，"
         f"阈值 {args.ref_medium:g}/{args.ref_strong:g}")
    if args.mix_six_weight is not None:
        source_note = (f"**预测**：只在有数据的层之间平均"
                       f"（六维 {args.mix_six_weight:g}/"
                       f"{100 - args.mix_six_weight:g}）+ 兑现加分")
    elif args.recompute_candidate:
        source_note = "**预测**：`six_dim_score` + 兑现加分"
    else:
        source_note = "库里存的 `total`"
    emit(f"- 候选表（新尺度）：`{args.candidate}`，total 来源：{source_note}")
    emit(f"- 两表重叠区间：`{start}` ~ `{end}`")
    emit(f"- 标定判据（与 `service._decide_level` 一致）：")
    emit(f"  - `STRONG = 候选 & 达标维度 ≥ {min_dims} & total ≥ S`"
         f"（`strong_dim_ratio={ratio}`）")
    emit("  - `MEDIUM = 候选 & **龙头共振** & total ≥ M`")
    emit(f"- 候选板块行：参考 {len(ref)} / 新 {len(new)}")
    emit()
    emit("## 二、参考政策占比（要保住的东西）")
    emit()
    emit("| 政策 | 占比 | 含义 |")
    emit("|---|---:|---|")
    emit(f"| 强信号 | {p_strong * 100:.2f}% | 候选 & 达标维度 ≥ {min_dims} "
         f"& total ≥ {args.ref_strong:g} |")
    emit(f"| 中信号 | {p_medium * 100:.2f}% | 候选 & 共振 "
         f"& total ≥ {args.ref_medium:g} |")
    emit()
    emit("## 三、推荐阈值")
    emit()
    emit("| 参数 | 推荐值 | 新表实际占比 | 参考占比 |")
    emit("|---|---:|---:|---:|")
    emit(f"| `alert.strong_score` | **{strong:g}** | "
         f"{got_strong * 100:.2f}% | {p_strong * 100:.2f}% |")
    emit(f"| `alert.medium_score` | **{medium:g}** | "
         f"{got_medium * 100:.2f}% | {p_medium * 100:.2f}% |")
    emit()
    emit(f"预计等级条数（**未计**确认/冷却的抑制）："
         f"参考 强 {rs} / 中 {rm}（合计 {rs + rm}）→ "
         f"新 强 {ns} / 中 {nm}（合计 {ns + nm}）"
         + (f"，**{((ns + nm) / (rs + rm)):.2f}x**" if rs + rm else ""))
    emit()
    emit("## 四、下一步")
    emit()
    emit("1. 把上面两个值写进 `configs/mainline.yaml`；")
    emit("2. 跑 `rescore_mainline.py --start ... --end ... --force`"
         "（它会先清空区间内的评分与告警表）；")
    emit("3. 跑完用 `compare_rescore_runs.py <旧日志> <新日志>` 核对采样告警量 —— "
         "落在 0.7~1.4x 之外说明政策仍没对齐，两轮指标不可直接比较。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n标定记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
