"""把用户点名给出的成分股补进板块（可审计、幂等、只动指定板块）。

## 为什么需要一个人工入口

板块成分股平时来自数据商（`ths_member` / 拥挤度清单）。但实测
**煤炭概念 885914 的 90 只成分股里没有煤炭龙头** —— 中国神华、陕西煤业、
兖矿能源、中煤能源这些大市值煤炭股被登记在 `700xxx` 那批行业板块下，
不在 `885914` 这份概念名单里；名单里反而是一堆焦化/化工/电力设备
（北方国际、湖北宜化、焦作万方、美达股份、华电国际…）。

后果不是"少了几只"，而是**整个板块的六维输入跑偏**：景气度用的是这些
边缘公司的 ROE/净利/营收同比，实测 885914 的景气度排在全体板块的
**第 5.6 百分位**，于是它 719 天里只有 21 天进过候选池（3%）。

所以需要一个明确的人工补充入口，并且**必须留下痕迹**：写进去的行带
`source='manual:<谁>:<日期>'`，重跑 `sync_members` 时能被认出来，
不会在下一次同步里被无声清掉。

## 名字 → 代码怎么解析

⚠️ 不能用 `ml_stock_meta.name`：实测这张表 5562 行的 `name` **全是空串**，
按名字查必然全部查不到（第一版诊断就是这么得出"18 只全都不在成分股里"
这个错误结论的）。可用的名字源是 `ml_member.name`（成分股表自带名字），
它覆盖了所有出现在任意板块里的股票。

用法：
    .venv\\Scripts\\python.exe scripts/add_board_members.py \\
        --board 885914.TI --dry-run
    .venv\\Scripts\\python.exe scripts/add_board_members.py \\
        --board 885914.TI --names 中国神华,陕西煤业,...
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"

#: 用户 2026-09-21 直接给出的煤炭概念成分股
COAL_NAMES = ("中国神华", "陕西煤业", "兖矿能源", "中煤能源", "神火股份",
              "潞安环能", "山西焦煤", "陕西能源", "晋控煤业", "山煤国际",
              "淮河能源", "平煤股份", "昊华能源", "兰花科创", "恒源煤电",
              "山西焦化", "陕西黑猫", "郑州煤电")


def name_map(conn: sqlite3.Connection) -> dict[str, str]:
    """`{股票名: 代码}`。用 `ml_member.name`（`ml_stock_meta.name` 是空的）。"""
    out: dict[str, str] = {}
    for row in conn.execute(
            "SELECT code, name FROM ml_member WHERE name <> ''"):
        out.setdefault(str(row["name"]), str(row["code"]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="人工补充板块成分股")
    parser.add_argument("--board", required=True, help="板块代码，如 885914.TI")
    parser.add_argument("--names", default="",
                        help="股票名（逗号分隔）；留空则用内置的煤炭清单")
    parser.add_argument("--source", default="", help="来源标记（默认 manual:cli:日期）")
    parser.add_argument("--note", default="", help="备注（写入日志）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    names = [x.strip() for x in (args.names or "").split(",") if x.strip()]
    if not names:
        names = list(COAL_NAMES)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
    source = args.source or f"manual:user:{stamp}"

    conn = sqlite3.connect(str(CACHE_DB), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    board = conn.execute("SELECT code, name FROM ml_board WHERE code = ?",
                         (args.board,)).fetchone()
    if board is None:
        print(f"❌ 板块不在池内：{args.board}")
        return 2
    lookup = name_map(conn)
    existing = {str(r["code"]) for r in conn.execute(
        "SELECT code FROM ml_member WHERE board_code = ?", (args.board,))}

    added: list[tuple[str, str]] = []
    already: list[tuple[str, str]] = []
    unknown: list[str] = []
    for name in names:
        code = lookup.get(name)
        if not code:
            unknown.append(name)
            continue
        if code in existing:
            already.append((name, code))
        else:
            added.append((name, code))

    print(f"板块 {board['code']} {board['name']}")
    print(f"  现有成分股 {len(existing)} 只")
    print(f"  待补 {len(added)} 只、已在 {len(already)} 只、"
          f"名字解析不到 {len(unknown)} 只")
    for name, code in added:
        print(f"    + {name}（{code}）")
    for name, code in already:
        print(f"    = {name}（{code}）已在")
    for name in unknown:
        print(f"    ❓ {name}：`ml_member` 里没有这个名字，无法解析代码")

    if not added:
        print("\n没有需要补的（幂等）。")
        conn.close()
        return 0
    if args.dry_run:
        print("\n（--dry-run：未写库）")
        conn.close()
        return 0

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO ml_member(board_code, code, name, in_date, out_date,"
        " source, updated_at) VALUES(?, ?, ?, '', '', ?, ?)"
        " ON CONFLICT(board_code, code) DO UPDATE SET name=excluded.name,"
        " source=excluded.source, updated_at=excluded.updated_at",
        [(args.board, code, name, source, now) for name, code in added])
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM ml_member WHERE board_code = ?",
                         (args.board,)).fetchone()[0]
    print(f"\n写入 {len(added)} 行（source={source}"
          + (f"，备注：{args.note}" if args.note else "") + "）")
    print(f"补给后成分股 {total} 只")
    print("⚠️ 成分股一变，六维/蓄势的输入就变了 → 必须重跑：")
    print("   1) .venv\\Scripts\\python.exe scripts/mainline_relevance.py --stage corr")
    print(f"   2) .venv\\Scripts\\python.exe scripts/purify_members.py"
          f" --only {args.board} --no-llm")
    print("   3) 整段重打分（横截面分位与候选池都会变）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
