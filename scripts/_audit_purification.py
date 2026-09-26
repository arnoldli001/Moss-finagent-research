"""审计：成分股提纯数据到底可不可靠（在重打分**之前**必须做）。

## 要回答的三个问题

1. **新鲜度**：`ml_member_clean` / `ml_member_pure` / `ml_stock_theme` 是什么时候
   建的？比 `ml_member` 新还是旧？用旧的结论去滤新的成分股名单＝滤错。
2. **覆盖完整性**：`ml_member` 有 2418 个板块；提纯表覆盖了多少？
   关键：`ml_member_clean` 里的 (板块,股票) 对是**全量**还是**只评过一部分**？
   —— `ml_stock_theme` 只对每只股票的"相关性前 N 题材"打分，
   那么没被评估的对**没有行**，而 `clean_member_map` 会把"没有行"当成
   `rel_ok=False` **剔除**，即把"未评估"误当作"不相关"。
3. **两套口径对比**：`clean`（运行时用）与 `pure`（用户要的提纯股池）差多少。

用法：.venv\\Scripts\\python.exe scripts\\_audit_purification.py
"""

from __future__ import annotations

import sqlite3

CACHE_DB = "data/mainline_cache.db"


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]


def main() -> int:
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("=" * 88)
    print("① 各表的规模与新鲜度")
    print("-" * 88)
    for table in ("ml_member", "ml_member_clean", "ml_member_pure",
                  "ml_member_corr", "ml_stock_theme", "ml_theme_board"):
        cols = columns(conn, table)
        stamps = [c for c in cols if any(k in c for k in
                                         ("updated", "refreshed", "at"))]
        row = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()
        boards = conn.execute(
            f"SELECT COUNT(DISTINCT board_code) n FROM {table}"
        ).fetchone() if "board_code" in cols else None
        info = f"   {table:<18}{row['n']:>8} 行"
        if boards:
            info += f" / {boards['n']:>4} 板块"
        for col in stamps[:1]:
            r = conn.execute(
                f"SELECT MIN({col}) a, MAX({col}) b FROM {table}").fetchone()
            info += f"   {col}: {str(r['a'])[:19]} ~ {str(r['b'])[:19]}"
        print(info)
        if "updated_at" in cols and table == "ml_member":
            r = conn.execute(
                "SELECT updated_at u, COUNT(*) n FROM ml_member"
                " GROUP BY u ORDER BY n DESC LIMIT 3").fetchall()
            for item in r:
                print(f"      updated_at {item['u'][:19]}  {item['n']} 行")

    print()
    print("=" * 88)
    print("② 覆盖完整性：ml_member_clean 的行是「全量评估」还是「只评了一部分」")
    print("-" * 88)
    total_pairs = conn.execute(
        "SELECT COUNT(*) n FROM ml_member").fetchone()["n"]
    clean_pairs = conn.execute(
        "SELECT COUNT(*) n FROM ml_member_clean").fetchone()["n"]
    pure_pairs = conn.execute(
        "SELECT COUNT(*) n FROM ml_member_pure").fetchone()["n"]
    print(f"   ml_member（原始）        {total_pairs:>8} 个 (板块,股票) 对")
    print(f"   ml_member_clean          {clean_pairs:>8}"
          f"（占原始 {clean_pairs / total_pairs * 100:.1f}%）")
    print(f"   ml_member_pure           {pure_pairs:>8}"
          f"（占原始 {pure_pairs / total_pairs * 100:.1f}%）")
    print("   ⚠️ 若 clean 只占原始的一小部分，说明**大量对从未被评估** ——")
    print("      而 `clean_member_map` 会把「没有行」当 `rel_ok=False` 剔除，")
    print("      即把「未评估」当成「不相关」。这是系统性过度裁剪。")

    print()
    print("=" * 88)
    print("③ 池内 324 个板块：原始 / clean(rel=1) / pure(rel=1) 逐板块对比")
    print("-" * 88)
    rows = conn.execute("""
        SELECT b.code, b.name,
               (SELECT COUNT(*) FROM ml_member m WHERE m.board_code = b.code) raw,
               (SELECT COUNT(*) FROM ml_member_clean c
                 WHERE c.board_code = b.code) clean_all,
               (SELECT COUNT(*) FROM ml_member_clean c
                 WHERE c.board_code = b.code AND c.relevant = 1) clean_rel,
               (SELECT COUNT(*) FROM ml_member_pure p
                 WHERE p.board_code = b.code) pure_all,
               (SELECT COUNT(*) FROM ml_member_pure p
                 WHERE p.board_code = b.code AND p.relevant = 1) pure_rel
        FROM ml_board b WHERE b.source = 'sector_crowding:list'
        ORDER BY b.code""").fetchall()
    print(f"   {'板块':<12}{'名称':<16}{'原始':>6}{'clean总':>8}{'clean相关':>10}"
          f"{'pure总':>7}{'pure相关':>9}{'clean覆盖':>10}")
    thin = []
    for r in rows:
        raw = r["raw"] or 0
        cov = (r["clean_all"] or 0) / raw * 100 if raw else 0
        flag = ""
        if raw >= 50 and (r["clean_rel"] or 0) < max(5, raw * 0.05):
            flag = "  ⚠️过裁"
            thin.append(f"{r['code']} {r['name']}（原始 {raw} → 相关 "
                        f"{r['clean_rel']}）")
        if len(thin) <= 18 or flag:
            print(f"   {r['code']:<12}{r['name'][:14]:<16}{raw:>6}"
                  f"{r['clean_all'] or 0:>8}{r['clean_rel'] or 0:>10}"
                  f"{r['pure_all'] or 0:>7}{r['pure_rel'] or 0:>9}"
                  f"{cov:>9.0f}%{flag}")

    print()
    print(f"   疑似过度裁剪的板块：{len(thin)} 个")
    for item in thin[:15]:
        print(f"      · {item}")

    print()
    print("=" * 88)
    print("④ clean 的「未评估」规模：在 ml_member 里但 clean 里没有的对")
    print("-" * 88)
    r = conn.execute("""
        SELECT COUNT(*) n FROM ml_member m
        WHERE NOT EXISTS (SELECT 1 FROM ml_member_clean c
                          WHERE c.board_code = m.board_code
                            AND c.code = m.code)""").fetchone()
    print(f"   原始里有、clean 里没有：{r['n']} 对"
          f"（占 {r['n'] / total_pairs * 100:.1f}%）")
    print("   ⇒ 这些对在 `clean_member_map` 里 **rel_ok=False**，会被直接剔除。")
    print("      但它们**从未被评估过**，不是「判定为不相关」。")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
