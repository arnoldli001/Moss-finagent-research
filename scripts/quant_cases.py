"""开源策略案例抓取 CLI（供定时任务与手动执行）。

用法：

    uv run python scripts/quant_cases.py fetch                 # 抓全部启用的源
    uv run python scripts/quant_cases.py fetch --source GitHub
    uv run python scripts/quant_cases.py list --theme 动量/趋势
    uv run python scripts/quant_cases.py themes

## 定位（必须写在最显眼处）

抓来的是**网络公开说法**，不是已验证的结论。它们普遍存在样本区间不明、
费用/滑点未计、未来函数、以及"只发盈利案例"的选择性报告问题。
所以每条都带 `verified=0`、来源链接与发布时间，界面照原样展示这句免责声明。
**本命令的输出不能直接用于交易决策。**

## 放进定时任务

    # 每周一 08:10 抓一次（多因子页"最新开源回测策略"）
    10 8 * * 1  uv run python scripts/quant_cases.py fetch
    # 每天 08:20 抓一次（单股票页"开源策略库"）
    20 8 * * *  uv run python scripts/quant_cases.py fetch --source GitHub --source 雪球
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.quant.strategy_cases import (  # noqa: E402
    DISCLAIMER,
    crawl,
    load_sources,
)
from src.quant.warehouse import strategy_case_store  # noqa: E402


def cmd_fetch(args: argparse.Namespace) -> int:
    sources = load_sources(args.config)
    if args.source:
        wanted = {name.lower() for name in args.source}
        sources = [spec for spec in sources if spec.name.lower() in wanted]
        if not sources:
            print(f"[×] 没有匹配的源：{args.source}")
            return 2
    print(f"启用源 {len(sources)} 个："
          f"{', '.join(spec.name for spec in sources)}\n")
    started = time.perf_counter()
    cases, results = crawl(sources, progress=lambda text: print(f"  {text}"))
    print(f"\n抓取完成，用时 {time.perf_counter() - started:.1f}s")
    print(f"{'源':<14}{'结果':<8}{'条数':>6}{'耗时':>8}  说明")
    print("-" * 72)
    for item in results:
        mark = "已跳过" if item.get("skipped") else (
            "成功" if item.get("ok") else "失败")
        print(f"{item['source']:<14}{mark:<8}{item.get('count', 0):>6}"
              f"{item.get('seconds', 0):>8}  {item.get('error', '')[:44]}")
    skipped = [item for item in results if item.get("skipped")]
    if skipped:
        print(f"\n注：{len(skipped)} 个源按配置跳过（原因见上），"
              f"它们**没有**被抓取 —— 报告里保留它们是为了不让人误以为已覆盖全网。")

    if not cases:
        print("\n[!] 一条都没抓到：所有源都失败或没解析出内容。"
              "这**不是**策略库为空，而是抓取链路有问题，请检查网络与源配置。")
        return 1

    store = strategy_case_store(disclaimer=DISCLAIMER)
    written = store.upsert(cases)
    print(f"\n入库：{written['inserted']} 条（去重后表内共 {written['total']} 条）")
    print("主题分布：")
    for item in store.themes():
        print(f"  {item['theme']:<12}{item['count']:>5}")
    print(f"\n{DISCLAIMER}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = strategy_case_store()
    items = store.list(theme=args.theme, source=args.source, limit=args.limit)
    if not items:
        print("案例库为空。先执行：uv run python scripts/quant_cases.py fetch")
        return 1
    print(f"{'主题':<12}{'来源':<12}{'发布':<12}标题")
    print("-" * 96)
    for item in items:
        print(f"{(item.get('theme') or '')[:10]:<12}"
              f"{(item.get('source_name') or '')[:10]:<12}"
              f"{(item.get('published_at') or '')[:10]:<12}"
              f"{(item.get('title') or '')[:52]}")
        print(f"{'':<36}{item.get('source_url', '')[:58]}")
    print(f"\n共 {len(items)} 条。{DISCLAIMER}")
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    """按当前关键词表重跑分类（改了主题/类型关键词后不必重抓）。"""
    store = strategy_case_store()
    before = {item["theme"]: item["count"] for item in store.themes()}
    updated = store.reclassify()
    print(f"重新分类 {updated} 条")
    after = store.themes()
    print("\n主题分布（前 → 后）：")
    for item in after:
        old = before.get(item["theme"], 0)
        print(f"  {item['theme']:<14}{old:>5} → {item['count']:>5}")
    print("\n类型分布：")
    for item in store.kind_stats():
        print(f"  {item['kind']:<12}{item['count']:>5}")
    return 0


def cmd_themes(args: argparse.Namespace) -> int:
    store = strategy_case_store()
    themes = store.themes()
    if not themes:
        print("案例库为空。")
        return 1
    for item in themes:
        print(f"  {item['theme']:<14}{item['count']:>5}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="开源策略案例抓取与查看")
    parser.add_argument("--config", default="configs/strategy_sources.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="抓取并入库")
    fetch.add_argument("--source", action="append", default=None,
                       help="只抓指定源（可重复）")
    fetch.set_defaults(func=cmd_fetch)

    listing = sub.add_parser("list", help="查看案例")
    listing.add_argument("--theme", default="")
    listing.add_argument("--source", default="")
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    themes = sub.add_parser("themes", help="主题分布")
    themes.set_defaults(func=cmd_themes)

    reclassify = sub.add_parser("reclassify", help="按当前关键词表重跑分类")
    reclassify.set_defaults(func=cmd_reclassify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
