"""一次性诊断：LLM 判定已覆盖多少、要补齐还差多少（用来判断是否需要再花钱）。

背景：用户问"是不是又要买 token"。需要给出确切数字：
  - 已付钱的 LLM 判定存了多少（`ml_stock_theme` / `ml_member_pure`）
  - 池内还有多少 (板块,股票) 对**从没被评估过**
  - 重跑默认会不会重复调用（缓存跳过）

用法：.venv\\Scripts\\python.exe scripts\\_audit_llm_coverage.py
"""

from __future__ import annotations

import sqlite3

CACHE_DB = "data/mainline_cache.db"
POOL = "SELECT code FROM ml_board WHERE source = 'sector_crowding:list'"


def main() -> int:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    total = conn.execute(
        f"SELECT COUNT(*) n FROM ml_member WHERE board_code IN ({POOL})"
    ).fetchone()["n"]
    covered = conn.execute(
        f"SELECT COUNT(*) n FROM ml_member m WHERE m.board_code IN ({POOL})"
        " AND EXISTS(SELECT 1 FROM ml_member_pure p"
        "            WHERE p.board_code = m.board_code AND p.code = m.code)"
    ).fetchone()["n"]
    print("池内 (板块,股票) 对")
    print(f"   合计                {total}")
    print(f"   ml_member_pure 已覆盖 {covered}（{covered / total * 100:.1f}%）")
    print(f"   未覆盖              {total - covered}")

    miss = conn.execute(
        f"SELECT COUNT(DISTINCT m.code) n FROM ml_member m"
        f" WHERE m.board_code IN ({POOL})"
        " AND NOT EXISTS(SELECT 1 FROM ml_member_pure p"
        "                WHERE p.board_code = m.board_code"
        "                  AND p.code = m.code)"
    ).fetchone()["n"]
    print(f"   未覆盖涉及股票      {miss} 只")

    scored = conn.execute(
        "SELECT COUNT(DISTINCT code) n FROM ml_stock_theme").fetchone()["n"]
    print()
    print("LLM 判定（已付钱、已落库）")
    print(f"   ml_stock_theme 覆盖股票  {scored} 只")
    print(f"   ml_stock_theme 判定对数  "
          f"{conn.execute('SELECT COUNT(*) n FROM ml_stock_theme').fetchone()['n']}")
    llm_rows = conn.execute(
        "SELECT COUNT(*) n FROM ml_member_pure WHERE source = ?",
        ("llm",)).fetchone()["n"]
    print(f"   ml_member_pure 中 llm 来源 {llm_rows} 行")

    never = conn.execute(
        f"SELECT COUNT(DISTINCT m.code) n FROM ml_member m"
        f" WHERE m.board_code IN ({POOL})"
        " AND m.code NOT IN (SELECT DISTINCT code FROM ml_stock_theme)"
    ).fetchone()["n"]
    print()
    print("还需要花钱的部分（若要把覆盖补齐）")
    print(f"   池内**从未被评估过**的股票  {never} 只")
    print("   ⚠️ 调用是按**股票**发的（一次调用判该股对多个题材），"
          "不是按 (板块,股票) 对")
    if never == 0:
        print("   ⇒ 补齐成本 = 0：所有池内股票都已有 LLM 判定，"
              "剩下的只是把它们**接进运行时**")
    else:
        print(f"   ⇒ 上限约 {never} 次调用；"
              "但重跑默认走缓存（`--redo-over 0`），只有这 {} 只会被调用"
              .format(never))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
