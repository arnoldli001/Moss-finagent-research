"""概念→成分股提纯：跑指定概念（试点）或全量，落 `ml_member_pure`。

## 链路

    build_tasks  按股票分组，挑出"需要 LLM 判定"的 (股票, 概念) 对
      ↓          —— corr≥0.6 / 新股 / 有缓存 的三类已被排除，不花 token
    score_pending 并发调 LLM 拿 business_score（每只股票一次调用）
      ↓
    select       逐概念执行：corr 排序截断 70% → 直接归属 / 新股归属 / 主营达标
      ↓
    write_pure   落库（与旧 `ml_member_clean` 并存，便于对照）

## 用法

    python scripts/purify_members.py --boards 30      # 试点（默认那 30 个概念）
    python scripts/purify_members.py --all            # 全量 335 个概念
    python scripts/purify_members.py --boards 30 --no-llm   # 只用缓存分（不花钱）

## ⚠️ 成本护栏（2026-09-26 加）

`score_pending` 是全项目**第二贵**的动作：实测 **117.26 元 / 9582 次调用**
（每只股票一次调用）。这个脚本是一次性的全量重算，**后续极少用到**，
所以入口按 1 元上限拦：

    python scripts/purify_members.py --boards 30      # → 拒绝启动（预计 > 1 元）
    python scripts/purify_members.py --boards 30 --no-llm   # → 允许（不调 LLM）

判两次：入口按**预计**花费（待判定只数 × 单次估算）拒绝启动，跑到一半按
**实际**花费中止（见 `src/core/budget.py::ScriptCostGuard`，中止前会把
已拿到的分数落盘）。要跑全量必须显式抬闸：
`MOSS_SCRIPT_COST_CAP_CNY=200 python scripts/purify_members.py --all`。
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
from src.core.config import get_settings  # noqa: E402
from src.core.errors import BRIEF_TIGHT, brief  # noqa: E402
from src.mainline.config import load_config  # noqa: E402
from src.mainline.member_pure import (BUSINESS_PASS, CORR_DIRECT, CORR_MISSING, KEEP_RATIO,
                                      build_tasks, is_st, load_cache,
                                      load_market_caps, score_pending, select,
                                      write_pure)  # noqa: E402

CACHE_DB = "data/mainline_cache.db"
WAREHOUSE = "data/quant/warehouse.db"

#: 试点概念：混入真值清单里的主线题材、大小概念、以及**存储芯片**
#: （用来回看长鑫科技 688825 是否经"新股直接归属"进来）
PILOT = [
    "人形机器人", "低空经济", "创新药", "CRO概念", "固态电池", "铜缆高速连接",
    "AI眼镜", "商业航天", "农业种植", "粮食概念", "芯片概念", "人工智能",
    "AIGC概念", "算力租赁", "东数西算(算力)", "共封装光学(CPO)", "锂电池概念",
    "稀土永磁", "黄金概念", "金属铜", "文化传媒", "网络游戏", "短剧游戏",
    "消费电子概念", "宠物经济", "IP经济(谷子经济)", "军工", "煤炭概念",
    "白酒概念", "存储芯片",
]


def resolve_boards(conn: sqlite3.Connection, names: list[str]) -> list[str]:
    """概念名 → 板块代码；报告对不上的名字（而不是静默丢弃）。"""
    table = {str(r["name"]): str(r["code"]) for r in
             conn.execute("SELECT code, name FROM ml_board")}
    found, missing = [], []
    for name in names:
        code = table.get(name)
        if code:
            found.append(code)
        else:
            missing.append(name)
    if missing:
        print(f"⚠️ 池内找不到 {len(missing)} 个概念名：{missing}")
    return found


async def run(*, boards: list[str], use_llm: bool, tier: str,
              concurrency: int, replace: bool, redo_over: int,
              business_pass: float, business_veto: bool,
              corr_direct: float, keep_ratio: float,
              cost_guard: ScriptCostGuard | None = None) -> int:
    conn = sqlite3.connect(CACHE_DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    caps = load_market_caps(Path(WAREHOUSE))
    cache = load_cache(conn)
    businesses = {str(r["code"]): str(r["business"] or "") for r in conn.execute(
        "SELECT code, business FROM ml_company_business WHERE business <> ''")}
    names = {str(r["code"]): str(r["name"] or "") for r in conn.execute(
        "SELECT code, name FROM ml_stock_directory")
        if True} if _has_table(conn, "ml_stock_directory") else {}
    if not names:
        names = {str(r["code"]): str(r["name"] or "") for r in conn.execute(
            "SELECT code, name FROM ml_member")}
    board_names = {str(r["code"]): str(r["name"]) for r in
                   conn.execute("SELECT code, name FROM ml_board")}
    print(f"候选池 {len(boards)} 个概念；已载入主营描述 {len(businesses)} 只、"
          f"缓存主营分 {len(cache)} 对、市值 {len(caps)} 只")

    force: set[str] = set()
    if redo_over > 0:
        # 已出结果、且候选数超过 redo_over 的股票**重新判定**。
        #
        # 为什么要重跑这一批：本轮早先用的是**固定输出上限 4096**，而思维链
        # 长度随候选数增长 —— 候选多时预算是空的、答案是空字符串，
        # 结果被静默丢弃。现在上限按 ÷10 放大（见 `max_tokens_for`），
        # 所以要回头把候选多的重打一遍。
        #
        # 候选数必须用 `cache={}` 的**全量视图**来数：正常调用里已完成的
        # 股票会被缓存整体跳过，`pending` 里根本查不到它们（第一次统计
        # 因此得出"需重跑 0 只"的错误结论）。
        _, full_map = build_tasks(conn, caps=caps, cache={},
                                  businesses=businesses, boards=boards)
        counts = {stock: len(themes) for stock, themes in full_map.items()}
        done = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT code FROM ml_stock_theme WHERE prompt_sig = ?",
            (PROMPT_SIG,))}
        force = {s for s in done if counts.get(s, 0) > redo_over}
        print(f"强制重跑：已出结果 {len(done)} 只中，候选 >{redo_over} 的 "
              f"{len(force)} 只")

    pending, corr_map = build_tasks(
        conn, caps=caps, cache=cache, businesses=businesses, boards=boards,
        ignore_cache=force)
    pairs = sum(len(v) for v in pending.values())
    print(f"需 LLM 判定：{pairs} 对 / {len(pending)} 只股票"
          f"（每只股票一次调用）")
    if not use_llm:
        print("--no-llm：跳过调用，只用已有缓存分")
        fresh = {}
    elif not pending:
        print("没有需要新判定的对，直接进入选择")
        fresh = {}
    else:
        # ★ 成本护栏：判在"确定真有一次调用都不发"的位置 —— 即 `--no-llm`
        # 与"没有待判定对"两个分支**之后**。这两条路径本来就不花钱，
        # 拦它们只会让"只想重新选择一遍"的人拿到一个莫名其妙的拒绝。
        guard = cost_guard if cost_guard is not None else ScriptCostGuard(
            "mainline_member_pure")
        print(f"{guard.describe()}")
        guard.check_entry(estimate_script_cost(len(pending)))
        begun = time.monotonic()
        from src.infrastructure.llm import LLMGateway

        gateway = LLMGateway(settings=get_settings())
        # 分批落盘：全量约 4000 次调用、一个多小时，只在结束时写库意味着
        # 中途任何崩溃/断网都让已花的钱作废。`merge_scores` 是按 code
        # 删后插、幂等，所以可以反复调用（传全量快照）。
        def flush_cb(snapshot: dict) -> None:
            if snapshot:
                merge_scores(conn, snapshot, model=tier)

        sc: dict = {}
        fresh = await score_pending(
            gateway, pending, businesses=businesses, names=names, tier=tier,
            concurrency=concurrency, flush=flush_cb, flush_every=100,
            stats=sc, cost_guard=guard,
            progress=lambda done, total: print(
                f"    LLM {done}/{total}（{time.monotonic() - begun:.0f}s）",
                flush=True))
        print(f"LLM 完成：拿到 {len(fresh)} 个分数对，"
              f"耗时 {time.monotonic() - begun:.0f}s")
        ok = sc.get("ok", 0)
        emp = sc.get("empty", 0)
        par = sc.get("partial", 0)
        err = sc.get("error", 0)
        print(f"  打分质量：成功 {ok} / 解析为空 {emp} / 解析不全 {par} / "
              f"调用异常 {err}")
        for code in (sc.get("bad_codes") or [])[:10]:
            print(f"    失败样例 {code}")

    # 把新分数并入本轮缓存（内存，一定成功）
    merged = dict(cache)
    merged.update(fresh)
    # ⚠️ `merge_scores`（缓存写回）**刻意放在结果落库之后**，见文件末尾。
    # 之前它排在前面，一次 "database is locked" 就抛出去、
    # 让 `write_pure` 永远执行不到 —— 一个可选的缓存优化摧毁了真正的交付物。

    rows: list[tuple] = []
    stats = {"direct": 0, "business": 0, "new": 0, "board_young": 0,
             "capped": 0, "weak": 0}
    # 板块自身的行情长度：用来区分「个股是新股」与「**整个板块**太年轻」。
    # 后者会让该概念的每一只成分股都拿不到 corr，等于一次过滤都没做，
    # 但表里看起来只是"一堆新股"（见 `member_pure.select` 的说明）。
    board_samples = {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT board_code, COUNT(*) FROM ml_board_bar GROUP BY board_code")}
    for board in boards:
        corr = {}
        for stock, themes in corr_map.items():
            if board_names.get(board) in themes:
                value = themes[board_names[board]]
                corr[stock] = None if value == CORR_MISSING else value
        if not corr:
            continue
        total = conn.execute("SELECT COUNT(*) FROM ml_member WHERE board_code = ?",
                             (board,)).fetchone()[0]
        picks = select(corr, board_name=board_names[board], cache=merged,
                       total=total, business_pass=business_pass,
                       corr_direct=corr_direct, keep_ratio=keep_ratio,
                       board_samples=board_samples.get(board),
                       business_veto=business_veto)
        for rank, pick in enumerate(picks, 1):
            stats[pick.decision] = stats.get(pick.decision, 0) + 1
            rows.append((board, pick.code, pick.corr, pick.business_score,
                         pick.final_score, rank,
                         pick.decision in ("direct", "business", "new",
                                           "board_young"),
                         "corr" if pick.decision == "direct" else
                         ("new" if pick.decision == "new" else
                          ("board_young" if pick.decision == "board_young"
                           else "llm")),
                         pick.note))
    written = write_pure(conn, rows, replace=replace)
    conn.commit()
    print(f"\n写入 {PURE_TABLE_NAME} {written} 行")
    print("决策分布:", {k: v for k, v in sorted(stats.items()) if v})
    included = (stats.get("direct", 0) + stats.get("business", 0)
                + stats.get("new", 0) + stats.get("board_young", 0))
    print(f"纳入 {included} 对 / 候选 {sum(stats.values())} 对 "
          f"= {included / max(sum(stats.values()), 1):.0%}")
    if stats.get("board_young"):
        print(f"  ⚠️ 其中 {stats['board_young']} 对来自**板块自身行情不足**的概念"
              "（这些概念本轮**未做任何过滤**，只是全量放行）")

    # 交付物已落库，再写缓存 —— 且**失败不致命**。
    # 写回 `ml_stock_theme` 只为下次跑省调用（不重复付费），属于优化项；
    # 它撞锁失败不该影响已经写好的结果。
    if fresh:
        try:
            saved = _retry(lambda: merge_scores(conn, fresh, model=tier))
            print(f"已把 {len(fresh)} 个新分数并回 ml_stock_theme"
                  f"（该表 {saved} 行）")
        except sqlite3.Error as exc:
            print(f"⚠️ 缓存写回失败（不影响已落库的结果）：{exc}")
    conn.close()
    return 0


def _retry(fn, *, attempts: int = 5, base_delay: float = 2.0):
    """撞锁重试。

    SQLite 只允许**一个写者**。即使设了 `busy_timeout`，多进程/多任务并发
    写同一个库时仍可能超时抛出 `database is locked`。实测踩过一次：
    提纯任务跑到一半时并发起了 `fina_mainbz` 同步，导致分批落盘连续失败、
    最终写库抛异常，**两小时的 LLM 成果大部分丢失**。

    所以写操作一律包一层重试（线性退避）。只重试"锁"类错误，
    其它异常原样抛出 —— 把约束违反当成锁竞争来重试只会掩盖真问题。
    """
    import time as _time

    for index in range(max(int(attempts), 1)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "lock" not in str(exc).lower() or index == attempts - 1:
                raise
            _time.sleep(base_delay * (index + 1))
    return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


PURE_TABLE_NAME = "ml_member_pure"

#: 本次 prompt 的签名，写回 `ml_stock_theme.prompt_sig` 用。
#: 与旧跑的值不同 —— 这意味着这些行在旧逻辑下不会命中缓存，
#: 但我们的复用判据是 `(code, theme)` 对是否存在，不看签名。
PROMPT_SIG = "member_pure_v1"


def merge_scores(conn: sqlite3.Connection, fresh: dict[tuple[str, str], float],
                 *, model: str) -> int:
    """把本轮新拿到的主营分并回 `ml_stock_theme`（供后续复用）。

    ## ⚠️ 为什么不能"按题材 upsert"

    `ml_stock_theme` 的**主键是 `(code, rank)` 而不是 `(code, theme)`** ——
    它存的是"这只股票的题材榜（第 1..N 名）"，不是"这只股票在每个题材下的分数"。
    所以：

    - 直接 `INSERT` 一个新题材会和该股票**既有的 rank** 撞主键；
    - 现有的 `RelevanceStore.save_scores` 因此是**先 `DELETE WHERE code=?` 再插**。

    复用那个函数会**把该股票原有的其他题材缓存一起删掉**（旧跑判过的行业/旧体系
    题材就没了），那是净损失。所以这里做**合并重写**：读出原有题材、并入新分数、
    按相关性重排 rank、整体写回。

    重排 rank 会改变"题材榜名次"的含义，但 `rank` 只用于展示与旧 `top_themes`
    截断，而新链路（概念→股票）已不再依赖它，所以可以接受。
    """
    import datetime

    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    by_stock: dict[str, dict[str, float]] = {}
    for (stock, theme), score in fresh.items():
        by_stock.setdefault(stock, {})[theme] = score
    written = 0
    for stock, new in by_stock.items():
        merged: dict[str, dict] = {}
        for row in conn.execute("SELECT * FROM ml_stock_theme WHERE code = ?",
                                (stock,)):
            merged[str(row["theme"])] = {
                "theme": str(row["theme"]), "raw_name": str(row["raw_name"] or ""),
                "business_score": float(row["business_score"] or 0),
                "corr": row["corr"], "final_score": float(row["final_score"] or 0),
                "reason": str(row["reason"] or ""), "model": str(row["model"] or "")}
        for theme, score in new.items():
            item = merged.setdefault(theme, {
                "theme": theme, "raw_name": theme, "business_score": 0.0,
                "corr": None, "final_score": 0.0, "reason": "",
                "model": model})
            item["business_score"] = float(score)
            item["final_score"] = float(score)
            item["model"] = model
        ordered = sorted(merged.values(),
                         key=lambda r: -(r["corr"] if r["corr"] is not None else -9))
        conn.execute("DELETE FROM ml_stock_theme WHERE code = ?", (stock,))
        conn.executemany(
            "INSERT INTO ml_stock_theme(code, rank, theme, raw_name,"
            " business_score, corr, final_score, reason, model, prompt_sig,"
            " scored_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(stock, index + 1, item["theme"], item["raw_name"],
              item["business_score"], item["corr"], item["final_score"],
              item["reason"], item["model"], PROMPT_SIG, now)
             for index, item in enumerate(ordered)])
        written += len(ordered)
    conn.commit()
    return written


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass
    parser = argparse.ArgumentParser(description="概念→成分股提纯")
    parser.add_argument("--all", action="store_true", help="全部概念（335）")
    parser.add_argument("--only", default="",
                        help="只跑这些板块代码（逗号分隔），用于局部重跑")
    parser.add_argument("--boards", type=int, default=30,
                        help="试点：前 N 个默认概念（--all 时忽略）")
    parser.add_argument("--no-llm", action="store_true",
                        help="不调 LLM，只用缓存主营分（零成本）")
    parser.add_argument("--tier", default="reasoning",
                        help="LLM 路由层（decision 会显著降质，见验证结论）")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--business-pass", type=float, default=BUSINESS_PASS,
                        help="主营分阈值（默认 70）")
    # ⚠️ 默认值走 `configs/mainline.yaml`（现在是 **false**），不在这里写死。
    #    用 `default=None` 让"没传参"与"显式传 --no-business-veto"可区分：
    #    前者读配置，后者强制关。写死 `default=False` 会让配置文件形同虚设。
    parser.add_argument("--business-veto", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="业务判定否决相关性直通（Task 1；不传则读 "
                             "mainline.yaml 的 relevance.business_veto，现为 false）")
    parser.add_argument("--corr-direct", type=float, default=CORR_DIRECT,
                        help="相关性直接归属阈值（默认 0.6）")
    parser.add_argument("--keep-ratio", type=float, default=KEEP_RATIO,
                        help="保留上限比例（默认 0.7）")
    parser.add_argument("--redo-over", type=int, default=0,
                        help="已出结果且候选数超过该值的股票重新判定（0=关闭）")
    parser.add_argument("--replace", action="store_true",
                        help="先清空 ml_member_pure 再写")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(CACHE_DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # SQLite 只允许一个写者：并发写时靠 busy_timeout 排队而不是立刻报错。
    conn.execute("PRAGMA busy_timeout=30000")
    if args.only:
        # 局部重跑：只写这几个板块的行（`write_pure` 是 upsert，不动其它板块）。
        # 为什么需要它：口径修复（如"板块太年轻"的标注）只影响个别板块时，
        # 为了 2 个板块把 324 个概念全部重算一遍既慢又难核对。
        boards = [item.strip() for item in args.only.split(",") if item.strip()]
        known = {str(r["code"]) for r in conn.execute("SELECT code FROM ml_board")}
        unknown = [code for code in boards if code not in known]
        if unknown:
            print(f"❌ 不在板块池里：{'、'.join(unknown)}")
            conn.close()
            return 2
    elif args.all:
        boards = [str(r["code"]) for r in
                  conn.execute("SELECT code FROM ml_board ORDER BY code")]
    else:
        boards = resolve_boards(conn, PILOT[: max(args.boards, 1)])
    conn.close()
    if not boards:
        print("❌ 没有可跑的概念")
        return 2
    # `--business-veto` 没传时读配置（唯一真源 `configs/mainline.yaml`）。
    # 读不到就退回**关闭**：现行 `ml_member_pure` 就是未否决的产物，
    # 静默启用会让池子与历史分数错位 —— 不确定时必须选"与现状一致"的那一侧。
    business_veto = args.business_veto
    if business_veto is None:
        try:
            business_veto = bool(load_config().relevance.business_veto)
            print(f"业务否决权（读 mainline.yaml）："
                  f"{'开启' if business_veto else '关闭'}")
        except Exception as exc:  # noqa: BLE001 配置读不到不该让提纯跑不动
            business_veto = False
            print(f"⚠️ 读 mainline.yaml 失败（{brief(exc, BRIEF_TIGHT)}）——"
                  f"业务否决权按**关闭**处理（与现行数据一致）")
    else:
        print(f"业务否决权（命令行覆盖）："
              f"{'开启' if business_veto else '关闭'}")
    if business_veto:
        print("⚠️ 已开启业务否决权：本次提纯会剔除「LLM 判过但 <"
              f"{args.business_pass:.0f} 分」的相关性入池成员（全池约 5322 条）。")
        print("   跑完必须重算历史分数与拥挤度，否则会停在"
              "「新池子 + 旧分数」的混合态：")
        print("     python scripts/rescore_mainline.py --force")
        print("     python scripts/refresh_crowding_metric.py")

    # 只有真的要调 LLM 时才建护栏：`--no-llm` 是零成本路径，不该被拦。
    # （`run()` 里还有一次更精确的判断 —— 那里才知道"待判定的只有几只"。）
    guard = (ScriptCostGuard("mainline_member_pure")
             if not args.no_llm else None)
    try:
        return asyncio.run(run(boards=boards, use_llm=not args.no_llm,
                               tier=args.tier, concurrency=args.concurrency,
                               replace=args.replace, redo_over=args.redo_over,
                               business_pass=args.business_pass,
                               business_veto=business_veto,
                               corr_direct=args.corr_direct,
                               keep_ratio=args.keep_ratio,
                               cost_guard=guard))
    except ScriptCostError as exc:
        # ⚠️ 必须排在通用分支之前：`brief()` 会把多行指引压成一句，
        # 而这个错误的全部价值就在"下一步怎么做"。退出码 3 = 被护栏拦下。
        print(f"\n⛔ {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
