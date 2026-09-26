"""板块池查询：按关键词/代码查板块是否在池内，并给出近邻。

## 为什么需要它

这份工作里"某个板块到底在不在池里"被反复问到，而且问错了代价不小：

- 用户报障「煤炭/石油石化/金属铜/黄金概念不在池内」→ 实际是**在池内但从不进
  候选池**（煤炭只 3% 的天数进池），不是缺行；
- 真值事件里 3 个的 `codes` 是 **875xxx**，那批板块在把池子收窄到 885/886 之后
  **已经被移除** → 这三条事件**永远不可能被抓到**，与模型能力无关；
- 「石油石化」在池里**根本没有**这一行（池里只有概念板块，没有申万一级行业）。

不把这三件事分开，就会把"口径不匹配"算成"模型漏报"，
把结论引向错误的方向。

用法：
    .venv\\Scripts\\python.exe scripts\\board_lookup.py 有色 黄金 铜
    .venv\\Scripts\\python.exe scripts\\board_lookup.py --code 885914.TI
    .venv\\Scripts\\python.exe scripts\\board_lookup.py --stats
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
MAIN_DB = ROOT / "data" / "moss_finagent.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="板块池查询")
    parser.add_argument("keywords", nargs="*", help="按名称关键词查")
    parser.add_argument("--code", default="", help="按精确代码查")
    parser.add_argument("--stats", action="store_true",
                        help="打印池子概况（代码前缀分布）")
    args = parser.parse_args()

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    boards = [(str(r["code"]), str(r["name"])) for r in
              cache.execute("SELECT code, name FROM ml_board ORDER BY code")]
    cache.close()

    if args.stats or not (args.keywords or args.code):
        print(f"板块池共 **{len(boards)}** 个板块")
        prefixes: dict[str, int] = {}
        for code, _ in boards:
            prefixes[code[:3]] = prefixes.get(code[:3], 0) + 1
        for prefix, count in sorted(prefixes.items(), key=lambda kv: -kv[1]):
            print(f"   {prefix}xxx: {count}")
        print()
        print("提示：若某个真值事件用的是 875xxx 而池里只有 885/886，"
              "那条事件永远抓不到 —— 属于**口径不匹配**，不是模型漏报。")
        if not args.keywords and not args.code:
            return 0

    if args.code:
        hit = next((item for item in boards if item[0] == args.code), None)
        print(f"{args.code}: "
              + (f"在池内 → {hit[1]}" if hit else "**不在池内**"))

    for keyword in args.keywords:
        hits = [(code, name) for code, name in boards if keyword in name]
        print(f"[{keyword}] 命中 {len(hits)} 个：")
        for code, name in hits:
            print(f"    {code}  {name}")

    # 顺带报"有没有评分行"——在池内但从未被评分的板块是数据问题
    if args.code or args.keywords:
        conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
        print()
        print("近邻板块的评分覆盖：")
        for code, name in boards:
            if args.code and code != args.code:
                continue
            if args.keywords and not any(k in name for k in args.keywords):
                continue
            total = conn.execute(
                "SELECT COUNT(*) FROM mainline_score WHERE board_code = ?",
                (code,)).fetchone()[0]
            cand = conn.execute(
                "SELECT COUNT(*) FROM mainline_score WHERE board_code = ?"
                " AND candidate = 1", (code,)).fetchone()[0]
            share = f"{cand / total * 100:.0f}%" if total else "—"
            print(f"    {code}  {name}: 评分 {total} 天，进候选池 {cand} 天"
                  f"（{share}）")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
