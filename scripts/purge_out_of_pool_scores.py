"""清除评分/告警表里**已不在板块池**的历史残留行。

## 为什么需要它

用户对板块的删除是**池级**的语义：「不计入主线挖掘监控范围、也不计入板块
拥挤度检测范围、不上报」。但删除只改了 `ml_board`（池本身），
`mainline_score` / `mainline_alert` 里**已经算过的历史行不会自动消失** ——
每轮换池后必须重算才会清掉，而重算要 90 分钟。

不重算、又想让残留消失时，就用这个脚本按「板块不在 `ml_board` 里」这一条
不变量精确删除。它做的事和全量重算在**行集合**上等价
（历史行的**分位数值**仍是旧截面算出来的，差一个板块约 0.7% 排名，
见下方 ⚠️）。

⚠️ 本脚本**不重算分数**：它保证"池外板块不再出现在结果里"，
但不保证"历史分位是按新池算的"。真要两者都对，只能全量重算
（`scripts/rescore_mainline.py --force`）。

## 用法

    python scripts/purge_out_of_pool_scores.py --dry-run
    python scripts/purge_out_of_pool_scores.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE = ROOT / "data" / "mainline_cache.db"
MOSS = ROOT / "data" / "moss_finagent.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="清除评分/告警表里池外板块的残留行")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不删除")
    args = parser.parse_args()

    cache = sqlite3.connect(f"file:{CACHE.as_posix()}?mode=ro", uri=True)
    pool = {str(r[0]) for r in cache.execute("SELECT code FROM ml_board")}
    cache.close()
    if not pool:
        print("❌ ml_board 是空的，拒绝执行（否则会删掉整张评分表）")
        return 2
    print(f"板块池 {len(pool)} 个")

    conn = sqlite3.connect(MOSS, timeout=30)
    try:
        for table in ("mainline_score", "mainline_alert"):
            rows = conn.execute(
                f"SELECT board_code, COUNT(*) FROM {table}"
                " GROUP BY board_code ORDER BY COUNT(*) DESC").fetchall()
            stray = [(str(code), n) for code, n in rows if str(code) not in pool]
            if not stray:
                print(f"✅ {table}: 没有池外残留")
                continue
            total = sum(n for _c, n in stray)
            print(f"\n{table}: {len(stray)} 个池外板块 / {total} 行")
            for code, n in stray[:20]:
                print(f"  {code}  {n} 行")
            if len(stray) > 20:
                print(f"  …另 {len(stray) - 20} 个")
            if args.dry_run:
                continue
            marks = ",".join("?" * len(stray))
            params = [code for code, _n in stray]
            cur = conn.execute(
                f"DELETE FROM {table} WHERE board_code IN ({marks})", params)
            print(f"  已删除 {cur.rowcount} 行")
        if not args.dry_run:
            conn.commit()
            print("\n已提交。")
    finally:
        conn.close()

    if not args.dry_run:
        # 复核：再查一遍，确认不变量成立
        conn = sqlite3.connect(f"file:{MOSS.as_posix()}?mode=ro", uri=True)
        left = 0
        for table in ("mainline_score", "mainline_alert"):
            codes = {str(r[0]) for r in conn.execute(
                f"SELECT DISTINCT board_code FROM {table}")}
            bad = codes - pool
            left += len(bad)
            print(f"复核 {table}: 池外板块 {len(bad)} 个")
        conn.close()
        if left:
            print("❌ 复核未通过")
            return 1
        print("✅ 复核通过：评分/告警表里没有池外板块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
