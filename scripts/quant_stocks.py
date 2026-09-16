"""本地股票字典 CLI：建库、查询、补录。

用法：

    uv run python scripts/quant_stocks.py rebuild        # 从 stock_basic 全量建库
    uv run python scripts/quant_stocks.py search 平安      # 联动联想（代码/名称/拼音）
    uv run python scripts/quant_stocks.py search jqkj
    uv run python scripts/quant_stocks.py get 603083      # 单个代码的名称
    uv run python scripts/quant_stocks.py enrich 588170 510300   # 补录 ETF/指数

## 这个字典解决什么问题

项目里所有股票输入框原来都只收 6 位数字，且**自选股里出现过"名称就是代码"的脏条目**
（前端加自选时名称为空，后端只能写代码）。字典建成后：

- 输入框支持 `代码 / 中文名 / 拼音首字母 / 全拼` 四路联想；
- 任何只拿到代码的地方都能补出中文名，不会再退化成"显示 603083"。

## 数据来源

- **A 股主来源**：Tushare `stock_basic`（5562 只，含行业/市场/上市日）——离线、权威；
- **拼音**：`pypinyin` 本地生成（离线、确定）；
- **ETF/指数**：不在 stock_basic 里，按需从东财 suggest 补录一次并落库。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.quant.stock_directory import (  # noqa: E402
    DirectoryError,
    stock_directory,
)


def cmd_rebuild(args: argparse.Namespace) -> int:
    directory = stock_directory()
    before = directory.count()
    try:
        report = directory.build_from_stock_basic()
    except DirectoryError as exc:
        print(f"[×] {exc}")
        return 2
    print(f"建库完成：写入 {report['written']} 条，字典内共 {report['total']} 条"
          f"（原有 {before} 条）")
    print("提示：ETF/指数不在 stock_basic 里，会在首次查询时按需补录；"
          "也可用 `enrich` 主动补。")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    directory = stock_directory()
    hits = directory.search(args.query, limit=args.limit)
    print(f"字典共 {directory.count()} 条，查询 {args.query!r} 命中 {len(hits)} 条：")
    for entry in hits:
        print(f"  {entry.code}  {entry.name:<12} {entry.pinyin_initials:<10} "
              f"{entry.instrument_type:<6} {entry.industry}")
    if not hits:
        print("  （无命中；试试代码前缀、拼音首字母如 jqkj，或中文名片段）")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    directory = stock_directory()
    for code in args.code:
        entry = directory.get(code)
        if entry is None:
            print(f"  {code}  → 字典里没有（可用 enrich 补录）")
            continue
        print(f"  {entry.code}  {entry.name}  {entry.pinyin_initials}  "
              f"{entry.instrument_type}  来源={entry.source}")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    directory = stock_directory()
    found = directory.enrich(list(args.code))
    print(f"补录 {len(found)}/{len(args.code)} 条：")
    for entry in found:
        print(f"  {entry.code}  {entry.name}  {entry.pinyin_initials}  "
              f"{entry.instrument_type}")
    missing = [code for code in args.code
               if not any(entry.code == code for entry in found)]
    if missing:
        print(f"  未补到：{missing}（东财也查不到，或网络不可用）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地股票字典（代码/名称/拼音）")
    sub = parser.add_subparsers(dest="command", required=True)

    rebuild = sub.add_parser("rebuild", help="从 stock_basic 全量建库")
    rebuild.set_defaults(func=cmd_rebuild)

    search = sub.add_parser("search", help="联动联想")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=15)
    search.set_defaults(func=cmd_search)

    get = sub.add_parser("get", help="按代码取名称")
    get.add_argument("code", nargs="+")
    get.set_defaults(func=cmd_get)

    enrich = sub.add_parser("enrich", help="补录 ETF/指数等")
    enrich.add_argument("code", nargs="+")
    enrich.set_defaults(func=cmd_enrich)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
