"""迭代驱动：把「分析 → 建议 → 快照 → 对比」串成一条命令，并留下可复现记录。

## 为什么需要它

目标要求"根据回测结果自动生成改进建议，自主决定并实施修改，再跑回测验证，
循环迭代"。如果每轮都靠人工敲七八条命令，会出现三个问题：

1. **不可复现**：没人记得上一轮到底改了什么、指标是多少；
2. **无法归因**：改了配置之后指标变好/变坏，没有基线可比；
3. **容易漏跑**：分析脚本跑了一半就下结论（本模块已经犯过两次单窗口假象）。

所以这里把每轮固化成：

    snapshot(第 N 轮) ──► 实施改动 ──► snapshot(第 N+1 轮) ──► diff

`snapshot` 冻结**当时生效的配置 + 各窗口指标**，`diff` 直接输出
"哪些指标变好了、哪些变差了、改动是什么"。历史存
`docs/mainline_iterations/`，一条一轮，永不覆盖。

## 指标从哪里来

- **IC / 权重实验**：直接调 `scripts.factor_ic_report.py` 里的纯函数
  （`load_scores` / `rank_ic` / `summarize` / `composite`）——
  避免"再写一份口径不同的实现"。
- **告警层与策略层**：读 `alert_trade_stats.py` / `alert_strategy_backtest.py`
  产出的 Excel。它们各自有完整口径，这里**只读结论不重算**，
  以免两处口径漂移。

用法：
    .venv\\Scripts\\python.exe scripts\\iterate_mainline.py --snapshot --label before
    .venv\\Scripts\\python.exe scripts\\iterate_mainline.py --diff
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.factor_ic_report import (  # noqa: E402
    combo_plan,
    composite,
    load_returns,
    load_scores,
    rank_ic,
    summarize,
)

HISTORY = ROOT / "docs" / "mainline_iterations"
#: ⚠️ **三个**窗口。2024-09~2025-09 早期没有打分（§16.31 待办第 8 条），
#: 全量重打分补上后它就是一段**独立行情**，也是**此前决策没用过**的数据 ——
#: 正好当样本外窗口。两个窗口同号很容易是巧合：§16.38 就是在加入第三个
#: 窗口之后推翻了"层权重 100/0 已被确认"的结论。所以快照必须记三格。
WINDOWS = (("20231009", "20240806", "旧区间"),
           ("20240901", "20250930", "中间区间"),
           ("20251001", "20260918", "新区间"))
HORIZONS = (20, 60)
#: 要跟踪的因子（层级 + 关键维度）。
#: `leader` 虽然在 `synthesis.layer_weights` 里不占权重（只做门控加分），
#: 但它是 P1 级建议的对象（两个窗口 IC 都为负 → 门控在给反向信号加权），
#: 不跟踪它就无法在 `--diff` 里验证"把 `gate_bonus_max` 设为 0"到底有没有用。
#:
#: `selection.rank_key` = **真实排序键**（`base_total + bonus_potential + etf_bonus`）。
#: 必须跟踪它：`total` 用的是**兑现后**的加分，像"平滑分数 + 稀疏跳变"，
#: IC 天然偏低，只看 `total` 会误判"排序变差了"。见 §16.35。
TRACK = ("total", "selection.rank_key", "six_dim", "accumulation", "leader",
         "six_dim.trading", "six_dim.technical", "six_dim.prosperity",
         "six_dim.moneyflow", "six_dim.chips", "six_dim.macro",
         "accumulation.northbound", "accumulation.leverage",
         "accumulation.volume_price")
#: 权重实验方案由 `factor_ic_report.combo_plan()` 生成（**基线从配置算**）。
#: 这里曾经硬编码一份"现行六维"全正权重 —— 但库里存的是**自然分**，
#: 线上的反向等价于负权重，所以那份"现行"其实是不反向的**对照组**，
#: 会诱导出"取消反向"的自毁建议。详见 `current_six_weights()` 的说明。


def current_config() -> dict:
    """冻结**当时生效**的配置（快照的意义就在于以后能复现）。"""
    from src.mainline.config import load_config
    cfg = load_config(force=True)
    return {
        "six_dim.weights": dict(cfg.six_dim.weights),
        "six_dim.reverse_dims": list(cfg.six_dim.reverse_dims),
        "accumulation.weights": dict(cfg.accumulation.weights),
        "synthesis.layer_weights": dict(cfg.synthesis.layer_weights),
        "etf.breakout_amount_ratio": cfg.etf.breakout_amount_ratio,
        "etf.breakout_percentile": cfg.etf.breakout_percentile,
        "etf.bonus": [cfg.etf.bonus_level1, cfg.etf.bonus_level2,
                      cfg.etf.bonus_resonance, cfg.etf.bonus_cap],
        "alert.nominate_limit": cfg.alert.nominate_limit,
        "alert.nominate_etf_limit": cfg.alert.nominate_etf_limit,
        "alert.medium_score": getattr(cfg.alert, "medium_score", None),
        "alert.strong_score": getattr(cfg.alert, "strong_score", None),
    }


def ic_snapshot() -> dict:
    """逐窗口算 IC。"""
    out: dict = {}
    for start, end, label in WINDOWS:
        scores, dims = load_scores(start, end)
        if scores.empty:
            continue
        returns = load_returns(start, end)
        frames = {"total": scores, **dims}
        entry: dict = {"days": len(scores), "boards": scores.shape[1],
                       "factors": {}, "combos": {}}
        for name in TRACK:
            frame = frames.get(name)
            if frame is None or frame.empty:
                continue
            for horizon in HORIZONS:
                series, _ = rank_ic(frame, returns[horizon])
                stat = summarize(series, horizon)
                if stat:
                    entry["factors"][f"{name}@{horizon}"] = {
                        "ic": round(stat["ic"], 5),
                        "icir": round(stat["icir"], 4),
                        "win": round(stat["win"], 4),
                        "t_ne": round(stat["t_ne"], 3),
                        "days": stat["days"],
                    }
        six = {n.split(".", 1)[1]: f for n, f in dims.items()
               if n.startswith("six_dim.")}
        acc = {n.split(".", 1)[1]: f for n, f in dims.items()
               if n.startswith("accumulation.")}
        for item in combo_plan():
            source = acc if item["source"] == "accumulation" else six
            frame = composite(source, item["weights"])
            if frame.empty:
                continue
            for horizon in HORIZONS:
                series, _ = rank_ic(frame, returns[horizon])
                stat = summarize(series, horizon)
                if stat:
                    key = f"{item['label']}@{horizon}"
                    entry["combos"][key] = {
                        "ic": round(stat["ic"], 5),
                        "win": round(stat["win"], 4),
                        "baseline": item["baseline"],
                    }
        out[label] = entry
    return out


def excel_snapshot() -> dict:
    """读告警层与策略层的 Excel 结论（不重算，避免口径漂移）。"""
    out: dict = {}
    trades = ROOT / "docs" / "MAINLINE_ALERT_TRADES.xlsx"
    if trades.exists():
        try:
            summary = pd.read_excel(trades, sheet_name="汇总")
            slot: dict = {}
            for _, row in summary.iterrows():
                horizon = int(row["持有期(交易日)"])
                slot[f"h{horizon}"] = {
                    "样本": int(row["样本数"]),
                    "胜率": float(row["胜率"]),
                    "盈亏比": float(row["盈亏比"]),
                    "期望收益": float(row["期望收益"]),
                    "基准胜率": float(row["基准胜率"]),
                }
            out["告警持有收益"] = slot
        except Exception as exc:  # noqa: BLE001
            out["告警持有收益"] = f"读取失败：{type(exc).__name__}"
    strategy = ROOT / "docs" / "MAINLINE_STRATEGY_BACKTEST.xlsx"
    if strategy.exists():
        try:
            detail = pd.read_excel(strategy, sheet_name="逐笔明细")
            capital = detail["资金收益率"]
            out["策略(3笔×1成/60日/止损7%/止盈10%)"] = {
                "笔数": int(len(detail)),
                "资金收益率均值": round(float(capital.mean()), 3),
                "中位数": round(float(capital.median()), 3),
                "胜率": round(float((capital > 0).mean() * 100), 2),
                "止损比例": round(float(detail["止损"].mean() * 100), 2),
            }
        except Exception as exc:  # noqa: BLE001
            out["策略"] = f"读取失败：{type(exc).__name__}"
    return out


def take(label: str, note: str) -> Path:
    HISTORY.mkdir(parents=True, exist_ok=True)
    existing = sorted(HISTORY.glob("*.json"))
    payload = {
        "iteration": len(existing) + 1,
        "label": label,
        "note": note,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": current_config(),
        "ic": ic_snapshot(),
        "layers": excel_snapshot(),
    }
    path = HISTORY / f"{len(existing) + 1:02d}_{label}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"快照 #{payload['iteration']} `{label}` → {path.name}")
    for window, entry in payload["ic"].items():
        line = entry["factors"].get("total@20", {})
        print(f"   {window}: total@20 IC={line.get('ic', '—')} "
              f"IC>0={line.get('win', '—')}")
    return path


def diff() -> int:
    files = sorted(HISTORY.glob("*.json"))
    if len(files) < 2:
        print(f"只有 {len(files)} 个快照，无法对比（至少需要 2 个）")
        return 1
    before = json.loads(files[-2].read_text(encoding="utf-8"))
    after = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"对比：#{before['iteration']} `{before['label']}` "
          f"({before['at'][:19]})  →  #{after['iteration']} "
          f"`{after['label']}` ({after['at'][:19]})")
    print()
    print("=== 配置变化 ===")
    changed = False
    for key in sorted(set(before["config"]) | set(after["config"])):
        old, new = before["config"].get(key), after["config"].get(key)
        if old != new:
            changed = True
            print(f"  {key}:\n     旧 {old}\n     新 {new}")
    if not changed:
        print("  （配置未变 —— 这轮只重算了数据）")

    print()
    print("=== IC 变化（只列有变化的）===")
    print(f"  {'指标':<34}{'旧':>11}{'新':>11}{'变化':>11}")
    for window in sorted(set(before["ic"]) | set(after["ic"])):
        b = before["ic"].get(window, {}).get("factors", {})
        a = after["ic"].get(window, {}).get("factors", {})
        rows = []
        for key in sorted(set(b) | set(a)):
            ob, nb = b.get(key, {}).get("ic"), a.get(key, {}).get("ic")
            if ob is None or nb is None or abs(nb - ob) < 1e-4:
                continue
            rows.append((key, ob, nb, nb - ob))
        if not rows:
            continue
        print(f"  —— {window} ——")
        for key, ob, nb, delta in rows:
            print(f"  {key:<34}{ob:>11.4f}{nb:>11.4f}{delta:>+11.4f}")

    print()
    print("=== 离线权重实验变化（同一批分数上的合成对比）===")
    for window in sorted(set(before["ic"]) | set(after["ic"])):
        b = before["ic"].get(window, {}).get("combos", {})
        a = after["ic"].get(window, {}).get("combos", {})
        rows = []
        for key in sorted(set(b) | set(a)):
            ob, nb = b.get(key, {}).get("ic"), a.get(key, {}).get("ic")
            if ob is None or nb is None or abs(nb - ob) < 1e-4:
                continue
            rows.append((key, ob, nb, nb - ob))
        if not rows:
            continue
        print(f"  —— {window} ——")
        for key, ob, nb, delta in rows:
            print(f"  {key:<44}{ob:>11.4f}{nb:>11.4f}{delta:>+11.4f}")

    print()
    print("=== 层级指标变化 ===")
    for name in sorted(set(before["layers"]) | set(after["layers"])):
        b, a = before["layers"].get(name), after["layers"].get(name)
        if b != a:
            print(f"  {name}:\n     旧 {b}\n     新 {a}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="主线迭代驱动")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--label", default="")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    if args.diff:
        return diff()
    if args.snapshot:
        label = args.label or datetime.now().strftime("%m%d_%H%M")
        take(label, args.note)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
