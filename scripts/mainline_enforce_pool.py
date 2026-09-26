"""强制 `ml_board` 只保留拥挤度分析池（`mainline_restore_boards.py` 的反向操作）。

## 为什么需要它（以及为什么原来那个脚本已经有害）

`ml_board` 有**两个**名录源，它们的意图正好相反：

    source = 'sector_crowding:list'   ← 拥挤度分析池（当前口径：335 个 885/886 概念）
    source = 'tushare:ths_index'      ← 主线自建的完整名录（1800+ 概念）
    source = 'tushare:sw_daily'       ← 申万一级

任何一次 `sync_all(datasets=["board"])`、或旧版 `mainline_restore_boards.py`
（它按"主线需要完整目录"的**旧需求**写，会把 1800+ 行填回来）都会让
`ml_board` 从 335 涨到 2200+。

**这不是无害的**：`_compute` 遍历 `ml_board`，多出来的板块会真的参与打分与排名，
而且**不报错** —— 表现为"排行榜悄悄变长"，或者本应被排除的行业/港股/美股板块
出现在主线里。实测这个状态发生过两次。

## 为什么 `import_crowding_pool()` 修不好它

它的 `_prune_boards()` **刻意限定 `source`**（文档解释了原因：不限定来源的清理
会删掉 1800+ 板块，而在旧需求下那是灾难）。所以它只会清理自己那个源的行，
`tushare:*` 的行会一直存活。

本脚本因此显式删除非分析池来源的行 —— 这正是 `_prune_boards` 当初不敢做的事，
只是**需求变了**：现在的口径就是"只用 885/886 概念板块"。

## 用法

    python scripts/mainline_enforce_pool.py --check    # 只看现状，不改
    python scripts/mainline_enforce_pool.py            # 删除非分析池来源的行
    python scripts/mainline_enforce_pool.py --cache    # 看可复用的主营业务打分缓存
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = "data/mainline_cache.db"
POOL_SOURCE = "sector_crowding:list"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def show_status(conn: sqlite3.Connection) -> int:
    """打印当前目录构成；返回应被删除的行数。"""
    rows = conn.execute("SELECT code, source FROM ml_board").fetchall()
    by_source = Counter(str(r["source"] or "(空)") for r in rows)
    by_seg = Counter(str(r["code"])[:3] for r in rows)
    print(f"ml_board 共 {len(rows)} 行")
    print("  按来源:", dict(by_source))
    print("  按代码段:", dict(sorted(by_seg.items())))
    stale = [str(r["code"]) for r in rows if str(r["source"]) != POOL_SOURCE]
    pool = len(rows) - len(stale)
    print(f"  → 分析池 {pool} 个，非分析池来源 {len(stale)} 个")
    if stale:
        print(f"  ⚠️ 这 {len(stale)} 个板块会真的参与打分与排名，而且不报错。")
    return len(stale)


def enforce(conn: sqlite3.Connection) -> int:
    """删除非分析池来源的行；返回删除数。"""
    stale = show_status(conn)
    if not stale:
        print("✅ 目录干净，无需处理")
        return 0
    conn.execute("DELETE FROM ml_board WHERE source IS NULL OR source <> ?",
                 (POOL_SOURCE,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM ml_board").fetchone()[0]
    print(f"已删除 {stale} 行 → 目录 {after} 个")
    return stale


def show_cache(conn: sqlite3.Connection, limit: int = 8) -> None:
    """可复用的主营业务打分缓存（`ml_stock_theme`）。

    这张表存的是**逐 (股票, 题材) 的 LLM 判定结果**：`business_score`（0-100
    主营业务相关度）+ `reason` + `prompt_sig`。它是可复用的关键 ——
    `prompt_sig` 让"同一对 (股票, 题材) + 同一版 prompt"可以跳过 LLM 调用。

    注意 `relevant` 那类**结论**（`ml_member_clean`）**不可复用**：它是在
    "每只股票全局取 top-N 题材"的旧口径下算的，依赖当时的候选全集；
    候选集一变结论就失效。而 `business_score` 是这一对本身的属性，与候选集无关。
    """
    total = conn.execute("SELECT COUNT(*) FROM ml_stock_theme").fetchone()[0]
    pairs = conn.execute(
        "SELECT COUNT(DISTINCT code || '|' || theme) FROM ml_stock_theme"
        ).fetchone()[0]
    sigs = conn.execute(
        "SELECT COUNT(DISTINCT prompt_sig) FROM ml_stock_theme").fetchone()[0]
    print(f"ml_stock_theme: {total} 行 / {pairs} 个 (股票,题材) 对 / "
          f"{sigs} 个 prompt 签名")
    print("样例（可复用的主营业务判定）:")
    for row in conn.execute(
            "SELECT code, theme, business_score, corr, reason FROM ml_stock_theme"
            " ORDER BY business_score DESC LIMIT ?", (limit,)):
        print(f"  {row['code']} | {row['theme']:<16} "
              f"主营分 {row['business_score']:>5.0f} | corr {row['corr']} "
              f"| {row['reason']}")
    print()
    print("⚠️ `ml_member_clean` 的 `relevant` 结论不可复用（依赖旧候选全集）；"
          "可复用的是上面的 business_score。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="强制 ml_board 只保留拥挤度分析池")
    parser.add_argument("--check", action="store_true", help="只看现状，不改")
    parser.add_argument("--cache", action="store_true",
                        help="查看可复用的主营业务打分缓存")
    parser.add_argument("--limit", type=int, default=8, help="缓存样例条数")
    args = parser.parse_args(argv)

    conn = connect()
    try:
        if args.cache:
            show_cache(conn, args.limit)
            return 0
        if args.check:
            stale = show_status(conn)
            return 0 if stale == 0 else 1
        enforce(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
