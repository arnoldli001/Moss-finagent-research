"""板块成分股提纯：批量入口。

把「走势相关性 + 主营业务相关性 + 总市值门槛」三个信号算出来并落库，
供主线挖掘在运行期直接读取（`src/mainline/relevance.py`）。

## 分阶段执行（每一步都可独立重跑，且天然幂等/可续跑）

    business     拉 stock_company 缓存主营描述      ~2 秒   免费
    corr         算走势相关性（纯本地）             ~5 分钟  免费
    score        LLM 逐股判主营业务关联             ~25 分钟 约 12.5~25 元
    clean        把「题材前 3」落成 ml_member_clean  ~2 秒   免费
    report       打印提纯前后对比与抽样              ~5 秒   免费

`score` 带签名缓存：主营文本与候选题材集没变就跳过，重跑不花钱。

## 用法

    # 全流程（首次）
    python scripts/mainline_relevance.py --stage all

    # 只重算相关性（成分股变了之后）
    python scripts/mainline_relevance.py --stage corr,clean,report

    # 看某只股票的判定明细
    python scripts/mainline_relevance.py --explain 002050

    # 强制重打分（忽略缓存，会重新花钱）
    python scripts/mainline_relevance.py --stage score --force

## ⚠️ 成本护栏（2026-09-26 加）

`score` 阶段是全项目最贵的动作：实测 **478.38 元 / 31770 次调用**
（2026-09-20 单日 487.57 元，而日预算是 20 元）。这个脚本是一次性的全量
重算，**后续极少用到**，所以入口直接按 1 元上限拦：

    python scripts/mainline_relevance.py --stage score            # → 拒绝启动
    python scripts/mainline_relevance.py --stage score --limit 40 # → 允许（≈0.8 元）

判两次：入口按**预计**花费（只数 × 单次估算）拒绝启动，跑到一半按**实际**
花费中止（见 `src/core/budget.py::ScriptCostGuard`）。要跑全量必须显式抬闸：
`MOSS_SCRIPT_COST_CAP_CNY=500 python scripts/mainline_relevance.py ...`。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.budget import (  # noqa: E402
    ScriptCostError,
    ScriptCostGuard,
    estimate_script_cost,
)
from src.core.errors import BRIEF_DEFAULT, brief  # noqa: E402
from src.mainline import relevance as R  # noqa: E402
from src.mainline.config import load_config  # noqa: E402

STAGES = ("business", "corr", "score", "clean", "report")


def warehouse_path() -> Path:
    return ROOT / "data" / "quant" / "warehouse.db"


def make_store() -> R.RelevanceStore:
    return R.RelevanceStore(load_config().cache_file)


def _bar(done: int, total: int, label: str, tag: str = "") -> None:
    pct = done / total if total else 0
    filled = int(pct * 30)
    sys.stdout.write(f"\r  [{label}] {'█' * filled}{'·' * (30 - filled)} "
                     f"{done}/{total} {tag[:14]:14}")
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


# ======================================================================
# 各阶段
# ======================================================================

def stage_business(store: R.RelevanceStore) -> None:
    print("①  同步主营描述（stock_company 批量接口）…")
    started = time.perf_counter()
    count = R.sync_business(store=store)
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN business <> '' THEN 1 ELSE 0 END)"
            " AS with_text FROM ml_company_business").fetchone()
    finally:
        conn.close()
    print(f"    写入 {count} 条，库内 {rows['n']} 条（有主营文本 {rows['with_text']}）"
          f"，耗时 {time.perf_counter() - started:.1f}s")


def stage_corr(store: R.RelevanceStore, window: int) -> None:
    print(f"②  计算走势相关性（回看 {window} 交易日，本地计算无网络）…")
    stats = R.compute_correlations(
        store=store, warehouse_path=warehouse_path(), window=window,
        progress=lambda d, t, tag: _bar(d, t, "corr", tag))
    conn = store.connect()
    try:
        info = store.corr_stats(conn)
    finally:
        conn.close()
    print(f"    有效相关对 {info['pairs']} 对，覆盖 {info['boards']} 个板块 / "
          f"{info['codes']} 只股票，耗时 {stats.seconds:.0f}s")


def stage_score(store: R.RelevanceStore, *, tier: str, concurrency: int,
                force: bool, limit: int, top: int,
                stall_seconds: float = 300.0, retries: int = 3,
                cost_guard: ScriptCostGuard | None = None) -> None:
    print(f"③  LLM 逐股判定主营业务关联（tier={tier}，并发 {concurrency}）…")
    # 打分范围：真实 A 股 + 有日线 + 非 ST。
    # 不限制的话会给 7559 个合成占位码（00000A…）也打一遍，纯浪费调用。
    universe = R.score_universe(warehouse_path())
    print(f"    打分范围（真实 A 股，非 ST，有日线）：{len(universe)} 只")
    tasks = R.build_tasks(store=store, warehouse_path=warehouse_path(),
                          allowed=universe)
    if not tasks:
        print("    没有候选任务 —— 请先执行 corr 阶段")
        return
    print(f"    待处理 {len(tasks)} 只股票（有相关性数据且在范围内）")
    if limit:
        tasks = tasks[:limit]
        print(f"    （--limit {limit}：只跑前 {limit} 只）")
    # ★ 成本护栏：**入口判一次，判在 `--limit` 之后** —— 估算要按"这次真正
    # 会跑的只数"算，否则 `--limit 40` 这种小批试跑会被全量的估算误杀。
    # 这也是"入口"最贴切的位置：再往前（main 里）还不知道要跑几只。
    guard = cost_guard if cost_guard is not None else ScriptCostGuard(
        "mainline_relevance")
    print(f"    {guard.describe()}")
    guard.check_entry(estimate_script_cost(len(tasks)))
    from src.api.runtime import build_runtime

    runtime = build_runtime()
    stats = asyncio.run(R.score_stocks(
        store=store, gateway=runtime.gateway, tasks=tasks, tier=tier, top=top,
        concurrency=concurrency, force=force, stall_seconds=stall_seconds,
        retries=retries, cost_guard=guard,
        progress=lambda d, t, tag: _bar(d, t, "score", tag)))
    print(f"    新打分 {stats.stocks_scored} 只，缓存命中 {stats.stocks_cached} 只，"
          f"失败 {stats.stocks_failed} 只")
    for note in stats.notes:
        print(f"    · {note}")


def stage_clean(store: R.RelevanceStore, *, dry_run: bool = False) -> None:
    if dry_run:
        print("④  预览过滤结果（--dry-run，不写入库）…")
        cfg = load_config()
        rel = cfg.relevance
        member_map = _member_map(store)
        outcome = R.clean_member_map(
            member_map, store=store, warehouse_path=warehouse_path(),
            trade_date=_latest_trade_date(), kinds=rel.kinds,
            min_total_mv=rel.min_total_mv, exclude_st=rel.exclude_st,
            top=rel.top_themes)
        print(f"    {outcome.summary()}")
        for note in outcome.gaps:
            if note:
                print(f"    · {note}")
        _print_boards(outcome)
        return
    print("④  落库 ml_member_clean（题材前 3）…")
    stats = R.rebuild_clean(store=store)
    print(f"    {stats.summary()}")


def _member_map(store: R.RelevanceStore) -> dict[str, list[str]]:
    """读原始成分股 `{board_code: [code]}`（与 datastore.member_map 同口径）。"""
    conn = store.connect()
    try:
        out: dict[str, list[str]] = {}
        for row in conn.execute(
                "SELECT board_code, code FROM ml_member ORDER BY code"):
            out.setdefault(str(row["board_code"]), []).append(str(row["code"]))
        return out
    finally:
        conn.close()


def _latest_trade_date() -> str:
    wh = sqlite3.connect(f"file:{warehouse_path()}?mode=ro", uri=True)
    try:
        return str(wh.execute(
            "SELECT MAX(trade_date) FROM quant_daily_basic").fetchone()[0] or "")
    finally:
        wh.close()


def _print_boards(outcome: R.CleanOutcome, limit: int = 15) -> None:
    """打印剔除最狠的板块 —— 用来人工判断过滤是否合理。"""
    items = sorted(
        ((code, item) for code, item in outcome.detail.items()
         if item["before"] and item["after"] < item["before"]),
        key=lambda pair: pair[1]["after"] / pair[1]["before"])
    print(f"\n    剔除比例最高的 {limit} 个板块：")
    print(f"      {'板块代码':14} {'前':>5} {'后':>5} {'保留':>6}"
          f" {'市值剔':>6} {'ST剔':>5} {'相关性剔':>7}")
    for code, item in items[:limit]:
        print(f"      {code:14} {item['before']:>5} {item['after']:>5}"
              f" {item['after'] / item['before']:>5.0%}"
              f" {item.get('dropped_mv', 0):>6} {item.get('dropped_st', 0):>5}"
              f" {item.get('dropped_irrelevant', 0):>7}")


def sample_codes(store: R.RelevanceStore, limit: int = 6) -> list[str]:
    """挑几个有代表性的股票做人工抽样（知名制造/消费/金融 + 随机）。"""
    conn = store.connect()
    try:
        known = [code for code in ("002050", "688981", "600519", "000001",
                                   "300308", "002415", "601127", "300750")
                 if conn.execute("SELECT 1 FROM ml_stock_theme WHERE code = ?",
                                 (code,)).fetchone()]
        rest = [str(r["code"]) for r in conn.execute(
            "SELECT code FROM ml_stock_theme WHERE code NOT IN"
            f" ({','.join('?' * len(known))}) ORDER BY code LIMIT ?"
            if known else
            "SELECT code FROM ml_stock_theme ORDER BY code LIMIT ?",
            (*known, limit) if known else (limit,))]
        return (known + rest)[:limit + len(known)]
    finally:
        conn.close()


def stage_report(store: R.RelevanceStore, *, explain: str = "") -> None:
    """打印提纯前后对比 + 抽样明细（人工验收用）。"""
    conn = store.connect()
    try:
        info = store.corr_stats(conn)
        cov = store.score_coverage(conn)
        before = int(conn.execute(
            "SELECT COUNT(*) FROM ml_member m JOIN ml_theme_board t"
            " ON t.board_code = m.board_code").fetchone()[0])
        after = int(conn.execute(
            "SELECT COUNT(*) FROM ml_member_clean WHERE relevant = 1"
        ).fetchone()[0])
        print("⑤  提纯结果总览")
        print(f"    走势相关性      {info['pairs']:>7} 对 / {info['boards']} 板块"
              f" / {info['codes']} 股票")
        print(f"    已打分股票      {cov['codes']:>7} 只（题材记录 {cov['pairs']} 条）")
        print(f"    成分股归属      {before:>7} → {after} 对"
              f"（保留 {after / before * 100:.0f}%）" if before else "")
        print()

        if explain:
            _explain(conn, explain)
            return

        rows = conn.execute(
            "SELECT m.board_code, b.name,"
            " COUNT(*) AS before,"
            " SUM(CASE WHEN c.relevant = 1 THEN 1 ELSE 0 END) AS after"
            " FROM ml_member m JOIN ml_theme_board t ON t.board_code = m.board_code"
            " JOIN ml_board b ON b.code = m.board_code"
            " LEFT JOIN ml_member_clean c ON c.board_code = m.board_code"
            " AND c.code = m.code"
            " GROUP BY m.board_code, b.name ORDER BY before DESC LIMIT 12"
        ).fetchall()
        print("    成分股最多的 12 个板块（提纯前后）：")
        print(f"      {'板块':22} {'前':>6} {'后':>6} {'保留':>6}")
        for row in rows:
            keep = (row["after"] or 0) / row["before"] if row["before"] else 0
            print(f"      {str(row['name'])[:20]:22} {row['before']:>6}"
                  f" {row['after'] or 0:>6} {keep:>5.0%}")
        print()
        for code in sample_codes(store, 4):
            _explain(conn, code, indent="    ")
    finally:
        conn.close()


def _explain(conn: sqlite3.Connection, code: str, *, indent: str = "  ") -> None:
    row = conn.execute(
        "SELECT name, business FROM ml_company_business WHERE code = ?",
        (code,)).fetchone()
    name = str(row["name"]) if row else ""
    text = str(row["business"]) if row else ""
    picked = conn.execute(
        "SELECT rank, theme, raw_name, business_score, corr, final_score, reason"
        " FROM ml_stock_theme WHERE code = ? ORDER BY rank", (code,)).fetchall()
    print(f"{indent}{'=' * 72}")
    print(f"{indent}{code} {name}")
    print(f"{indent}主营：{text[:110]}")
    if not picked:
        print(f"{indent}（无打分记录）")
        return
    print(f"{indent}确定归属题材（走势相关性 60% + 主营 40%）：")
    for item in picked:
        corr = item["corr"]
        corr_text = f"{corr:+.3f}" if corr is not None else "  n/a"
        print(f"{indent}  {item['rank']}. {item['theme']:14}"
              f" 综合 {item['final_score']:5.1f}"
              f"  主营 {item['business_score']:5.1f}"
              f"  走势 {corr_text}   {item['reason']}")
    # 该股所属板块里被保留 / 被剔除的
    kept = conn.execute(
        "SELECT b.name, c.theme, c.rank_in_stock FROM ml_member_clean c"
        " JOIN ml_board b ON b.code = c.board_code"
        " WHERE c.code = ? AND c.relevant = 1 ORDER BY c.rank_in_stock, b.name",
        (code,)).fetchall()
    dropped = conn.execute(
        "SELECT b.name FROM ml_member_clean c JOIN ml_board b ON b.code = c.board_code"
        " WHERE c.code = ? AND c.relevant = 0 ORDER BY b.name LIMIT 12",
        (code,)).fetchall()
    print(f"{indent}保留 {len(kept)} 个板块："
          + "、".join(str(r["name"]) for r in kept[:12]))
    print(f"{indent}剔除 {len(dropped)} 个板块（示意）："
          + "、".join(str(r["name"]) for r in dropped[:10]))


# ======================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="板块成分股提纯")
    parser.add_argument("--stage", default="all",
                        help=f"逗号分隔：{','.join(STAGES)},all")
    parser.add_argument("--tier", default="decision", help="LLM 路由层")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--window", type=int, default=R.CORR_WINDOW,
                        help="走势相关性回看交易日数")
    parser.add_argument("--top", type=int, default=R.TOP_THEMES)
    parser.add_argument("--force", action="store_true", help="忽略缓存重新打分")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 只（试跑）")
    parser.add_argument("--dry-run", action="store_true",
                        help="clean 阶段只预览过滤结果，不写库")
    parser.add_argument("--stall-seconds", type=float, default=300.0,
                        help="打分卡死阈值（秒）：连续这么久没有任何任务结束就中止，"
                             "已落库部分保留、重跑续算。0 = 关闭守卫")
    parser.add_argument("--retries", type=int, default=3,
                        help="单只失败的重试次数。**别设 0**：reasoning 层实测会"
                             "偶发返回空响应，不重试会静默丢记录")
    parser.add_argument("--explain", default="", help="只看某只股票的判定明细")
    args = parser.parse_args()

    wanted = (list(STAGES) if args.stage == "all"
              else [s.strip() for s in args.stage.split(",") if s.strip()])
    # `--explain` 是"看一眼结果"，不该顺带跑一遍完整流程（含 LLM 打分）——
    # 默认 stage=all 会让它默默烧掉几千次调用。
    if args.explain and args.stage == "all":
        wanted = ["report"]
    unknown = [s for s in wanted if s not in STAGES]
    if unknown:
        print(f"未知阶段：{unknown}，可选 {STAGES}")
        return 2

    store = make_store()
    if not Path(store.path).exists():
        print(f"主线数据仓不存在：{store.path}")
        return 1
    print(f"主线数据仓：{store.path}\n")

    # 只有 score 阶段花钱：其余（business/corr/clean/report）全是本地计算。
    # 所以护栏也只在真的要跑 score 时建 —— 免得"看一眼报告"也去打日预算。
    guard = ScriptCostGuard("mainline_relevance") if "score" in wanted else None

    started = time.perf_counter()
    for stage in wanted:
        try:
            if stage == "business":
                stage_business(store)
            elif stage == "corr":
                stage_corr(store, args.window)
            elif stage == "score":
                stage_score(store, tier=args.tier,
                            concurrency=args.concurrency, force=args.force,
                            limit=args.limit, top=args.top,
                            stall_seconds=args.stall_seconds,
                            retries=args.retries, cost_guard=guard)
            elif stage == "clean":
                stage_clean(store, dry_run=args.dry_run)
            elif stage == "report":
                stage_report(store, explain=args.explain)
        except KeyboardInterrupt:
            print("\n已中断（已完成的部分已落库，可直接重跑同一命令续跑）")
            return 130
        except ScriptCostError as exc:
            # ⚠️ 必须排在通用 `except Exception` **之前**：通用分支会走
            # `brief()` 把多行操作指引压成一句，而这个错误的价值全在
            # "下一步该怎么做"上。退出码 3 与"阶段失败(1)"区分开，
            # 便于脚本/CI 判断"是被护栏拦了，不是真出错"。
            print(f"\n⛔ {exc}")
            return 3
        except Exception as exc:  # noqa: BLE001
            print(f"\n阶段 {stage} 失败：{brief(exc, BRIEF_DEFAULT)}")
            return 1
        print()
    print(f"总耗时 {time.perf_counter() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
