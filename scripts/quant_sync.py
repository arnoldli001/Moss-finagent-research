#!/usr/bin/env python
"""量化数据同步与因子计算 CLI（M1/M2 的运维入口）。

用法：
    # 0) 权限自检（买了 Tushare 后第一件事，确认 5000 积分档真的到账）
    python scripts/quant_sync.py doctor

    # 1) 下载行情/估值/资金流等横截面（增量，可断点续传）
    python scripts/quant_sync.py download --start 2026-01-01 --end 2026-09-15

    # 2) 只下部分数据集 / 只下最近 N 个交易日
    python scripts/quant_sync.py download --start 2026-08-01 --end 2026-09-15 \
        --datasets daily_basic,moneyflow --max-days 5

    # 3) 计算 35 因子（读本地缓存，不联网）
    python scripts/quant_sync.py factors --start 2026-08-01 --end 2026-09-15

    # 4) 查看本地缓存状态
    python scripts/quant_sync.py status
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.quant.dataset_store import DEFAULT_ROOT, DatasetStore  # noqa: E402
from src.quant.download import DAILY_DATASETS, TushareDownloader  # noqa: E402
from src.quant.tushare_source import (  # noqa: E402
    TushareClient,
    TusharePermissionError,
    resolve_token,
    token_hint,
)


def _fmt_range(start: str, end: str) -> tuple[str, str]:
    return start.replace("-", ""), end.replace("-", "")


# ==================================================================
# doctor：权限自检
# ==================================================================


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        token = resolve_token()
    except Exception as exc:  # noqa: BLE001
        print(f"[×] {exc}")
        return 2
    print(f"token：{token_hint(token)}")
    print(f"频次上限：{args.rate} 次/分\n")

    client = TushareClient(max_calls_per_minute=args.rate)
    report = client.probe()
    ok_count = 0
    print(f"{'接口':24s} {'状态':6s} {'行数':>7s}  说明")
    print("-" * 78)
    for api, info in report.items():
        if info.get("ok"):
            ok_count += 1
            print(f"{api:24s} {'✓':6s} {info.get('rows', 0):7d}  "
                  f"{','.join(info.get('columns', [])[:6])}")
        else:
            kind = info.get("kind", "error")
            flag = "权限" if kind == "permission" else "×"
            print(f"{api:24s} {flag:6s} {'-':>7s}  {info.get('detail', '')[:60]}")
    print("-" * 78)
    print(f"可用 {ok_count}/{len(report)} 个接口；调用 {client.stats.calls} 次，"
          f"限流等待 {client.stats.waited_seconds:.1f}s")
    print(json.dumps(client.stats.as_dict(), ensure_ascii=False))
    return 0 if ok_count >= 6 else 1


# ==================================================================
# download
# ==================================================================


def cmd_download(args: argparse.Namespace) -> int:
    # --full-history 时 start 由各数据集自己的起始日决定，这里允许省略
    start, end = _fmt_range(args.start or "20060101", args.end)
    datasets = ([name.strip() for name in args.datasets.split(",") if name.strip()]
                if args.datasets else list(DAILY_DATASETS))
    for name in datasets:
        if name not in DAILY_DATASETS:
            print(f"[×] 未知数据集 {name}（可选：{', '.join(DAILY_DATASETS)}）")
            return 2

    from src.quant.dataset_store import quarter_periods

    periods = quarter_periods(args.start_year, args.end_year) if args.fina else []

    downloader = TushareDownloader(
        TushareClient(max_calls_per_minute=args.rate), root=args.root)

    async def run():
        if args.full_history:
            return await _run_full_history(downloader, end, datasets, args)
        days = await downloader.calendar(start, end)
        if not days:
            import pandas as pd

            days = [stamp.strftime("%Y%m%d") for stamp in
                    pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))]
        if args.max_days:
            days = days[-args.max_days:]        # 日历已升序 → 取最近 N 天
        print(f"股票池={args.universe} 交易日 {len(days)} 个"
              f"（{days[0]}~{days[-1]}）；数据集 {len(datasets)} 个；"
              f"报告期 {len(periods)} 个")
        report = await downloader.download(
            start, end, datasets=datasets, periods=periods,
            force=args.force, days=days,
            progress=lambda text: print(f"  … {text}"))
        return report

    report = asyncio.run(run())
    print("\n下载结果：")
    for line in report.summary_lines():
        print(line)
    print(f"总计 {report.as_dict()['total_rows']} 行，"
          f"失败分区 {report.as_dict()['total_failed']} 个")
    for name, result in report.results.items():
        if result.failed:
            sample = list(result.failed.items())[:3]
            print(f"  {name} 失败样例: {sample}")
    return 0


# 各数据集在 5000 积分档下的**实测最早可用日期**（2026-09-16 探测）。
# 用途：`--full-history` 按各自起始日分别下载，而不是统一用一个更晚的日期
# （那样会白白丢掉 1990~2010 的行情），也不会用一个更早的日期去撞空表。
EARLIEST_AVAILABLE: dict[str, str] = {
    "daily": "20060101",
    "daily_basic": "20060101",
    "adj_factor": "20060101",
    "suspend_d": "20060101",
    "moneyflow": "20100104",
    "stk_limit": "20100104",
    "bak_daily": "20180101",
}


async def _run_full_history(downloader, end: str, datasets: list[str],
                            args: argparse.Namespace):
    """全量历史下载：每个数据集用自己的起始日（已缓存的分区自动跳过）。

    **结束日要"钳"到已发布范围**：Tushare 的 EOD 数据 15:00~16:00 才入库，
    日期跨过午夜后 `trade_date=今天` 会稳定返回空表（实测：8 个数据集各报一次
    "返回空表"失败）。所以当天 16:30 之前一律只下到昨天。
    """
    import pandas as pd

    from src.quant.dataset_store import quarter_periods
    from src.quant.download import DownloadReport

    now = pd.Timestamp.now()
    if now.strftime("%H%M") < "1630" and end >= now.strftime("%Y%m%d"):
        end = (now - pd.Timedelta(days=1)).strftime("%Y%m%d")
        print(f"（今天 {now.strftime('%Y-%m-%d')} 的 EOD 数据尚未入库，"
              f"结束日自动钳到 {end}）", flush=True)

    report = DownloadReport(results={}, universe=args.universe)
    total_calls_estimate = 0
    plan: list[tuple[str, str, list[str]]] = []
    for dataset in datasets:
        start = EARLIEST_AVAILABLE.get(dataset, "20150101")
        days = await downloader.calendar(start, end)
        if args.max_days:
            days = days[-args.max_days:]
        plan.append((dataset, start, days))
        total_calls_estimate += len(days)

    print(f"全量历史下载计划（股票池 {args.universe}）：", flush=True)
    for dataset, start, days in plan:
        have = len(downloader.store(dataset).keys())
        print(f"  {dataset:20s} {start} ~ {end}  {len(days):5d} 个交易日"
              f"（已缓存 {have}）", flush=True)
    print(f"  合计约 {total_calls_estimate} 个分区；"
          f"按 {args.rate} 次/分 ≈ {total_calls_estimate / args.rate:.0f} 分钟\n",
          flush=True)

    for dataset, _start, days in plan:
        print(f"  … 下载 {dataset}（{len(days)} 个交易日）", flush=True)
        report.results[dataset] = await downloader.sync_daily(
            dataset, days, force=args.force)
        result = report.results[dataset]
        print(f"    {dataset}: 新拉 {len(result.fetched)} 跳过 "
              f"{len(result.skipped)} 失败 {len(result.failed)} "
              f"行数 {result.rows}", flush=True)

    if args.fina:
        periods = quarter_periods(args.start_year, args.end_year)
        print(f"  … 下载 fina_indicator_vip（{len(periods)} 个报告期）", flush=True)
        report.results["fina_indicator_vip"] = await downloader.sync_fina(
            periods, force=args.force)

    if args.with_index:
        index_days = await downloader.calendar("20050104", end)
        if args.max_days:
            index_days = index_days[-args.max_days:]   # 冒烟测试也要限制指数下载
        print(f"  … 下载 index_daily（{len(index_days)} 个交易日）", flush=True)
        report.results["index_daily"] = await downloader.sync_index(
            index_days, force=args.force)
    return report


# ==================================================================
# factors
# ==================================================================


def cmd_factors(args: argparse.Namespace) -> int:
    from src.quant.factor_library_v2 import (
        compute_factors,
        factor_coverage_report,
        list_factor_specs,
    )
    from src.quant.panels import build_panels

    start, end = _fmt_range(args.start, args.end)
    store = DatasetStore("daily_basic", root=args.root, universe=args.universe)
    days = [day for day in store.keys() if start <= day <= end]
    if args.max_days:
        days = days[-args.max_days:]
    if not days:
        print(f"[×] {start}~{end} 没有缓存数据，请先运行 download")
        return 2

    panels = build_panels(days, root=args.root, universe=args.universe)
    print(f"面板：{len(panels.dates)} 个交易日 × {len(panels.codes)} 只股票")
    if panels.gaps:
        print("缺口：")
        for gap in panels.gaps:
            print(f"  - {gap}")

    keys = [name.strip() for name in args.only.split(",")] if args.only else None
    factors = compute_factors(panels, keys=keys, verbose=args.verbose)
    report = factor_coverage_report(factors)
    print(f"\n计算完成 {len(factors)} 个因子（注册表共 {len(list_factor_specs())} 个）")
    print(report.to_string(index=False))

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for key, frame in factors.items():
            path = out_dir / f"{key}.csv.gz"
            frame.to_csv(path, compression="gzip")
        report.to_csv(out_dir / "_coverage.csv", index=False)
        print(f"\n已写入 {out_dir}（{len(factors)} 个因子面板 + 覆盖率报表）")
    return 0


# ==================================================================
# ic：因子有效性检验（IC / ICIR / t 值）
# ==================================================================


def cmd_ic(args: argparse.Namespace) -> int:
    """用仓库既有的因子分析器做 IC/ICIR 检验。

    注意口径（诚实标注）：这里用**日频截面 + N 日前瞻收益**做快速筛选，
    相邻交易日的 IC 高度重叠 → t 值会被系统性高估，只适合"筛掉明显无效的因子"，
    不能当作显著性结论。正式评估请按月频调仓（`--freq M`）走 walk-forward。
    """
    import pandas as pd

    from src.quant.dataset_store import DatasetStore
    from src.quant.factor_analyzer import compute_ic_series, evaluate_factor
    from src.quant.factor_library_v2 import FACTORS, compute_factors
    from src.quant.panels import build_panels

    start, end = _fmt_range(args.start, args.end)
    store = DatasetStore("daily_basic", root=args.root, universe=args.universe)
    days = [day for day in store.keys() if start <= day <= end]
    if args.max_days:
        days = days[-args.max_days:]
    if len(days) < 30:
        print(f"[×] 交易日太少（{len(days)}），IC 检验没有意义，请先扩大区间或下载更多数据")
        return 2

    panels = build_panels(days, root=args.root, universe=args.universe)
    print(f"面板：{len(panels.dates)} 个交易日 × {len(panels.codes)} 只股票")
    factors = compute_factors(panels, keys=(
        [name.strip() for name in args.only.split(",")] if args.only else None))

    close = panels.price("close")
    horizon = args.horizon
    forward = close.shift(-horizon) / close - 1.0

    rows = []
    for key, panel in factors.items():
        ic_series = compute_ic_series(panel, forward)
        if len(ic_series) < 5:
            rows.append({"factor": key, "category": FACTORS[key].category,
                         "IC": float("nan"), "ICIR": float("nan"),
                         "t": float("nan"), "IC>0": float("nan"),
                         "periods": len(ic_series)})
            continue
        metrics = evaluate_factor(ic_series)
        rows.append({
            "factor": key, "category": FACTORS[key].category,
            "IC": round(metrics.ic_mean, 4),
            "ICIR": round(metrics.ir, 4),
            "t": round(metrics.t_stat, 2),
            "IC>0": round(metrics.ic_positive_rate, 3),
            "periods": metrics.n_periods,
        })

    table = pd.DataFrame(rows)
    table["abs_ICIR"] = table["ICIR"].abs()
    table = table.sort_values("abs_ICIR", ascending=False).drop(columns="abs_ICIR")
    print(f"\nIC 检验（前瞻 {horizon} 个交易日，日频截面）：")
    print(table.to_string(index=False))
    if args.out:
        table.to_csv(args.out, index=False)
        print(f"\n已写入 {args.out}")
    return 0


# ==================================================================
# status
# ==================================================================


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root)
    dataset_root = root / args.universe
    print(f"缓存根目录：{root}（股票池 {args.universe}）")
    if not dataset_root.exists():
        print("  （空，尚未下载任何数据）")
        return 0
    for child in sorted(dataset_root.iterdir()):
        if not child.is_dir():
            continue
        store = DatasetStore(child.name, root=root, universe=args.universe)
        coverage = store.coverage()
        print(f"  {child.name:22s} 分区 {coverage['partitions']:5d} "
              f"行数 {coverage['rows']:9d}  {coverage['first']}~{coverage['last']}")
    fundamentals = root.parent / "fundamentals"
    if fundamentals.exists():
        files = list(fundamentals.glob("performance_*.csv.gz"))
        print(f"  AkShare 业绩报表缓存：{len(files)} 个报告期（{fundamentals}）")
    prices = root.parent / "prices"
    if prices.exists():
        count = len([p for p in prices.glob("*.csv.gz")])
        print(f"  QMT 前复权日线缓存：{count} 只标的（{prices}）")
    return 0


# ==================================================================
# 入口
# ==================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="量化数据同步与 35 因子计算",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help=f"Tushare 缓存根目录（默认 {DEFAULT_ROOT}）")
    parser.add_argument("--universe", default="a_share",
                        choices=["a_share", "all"], help="股票池")
    parser.add_argument("--rate", type=int, default=450,
                        help="每分钟最大调用次数（5000 积分档=500，留余量）")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Tushare 权限自检")
    doctor.set_defaults(func=cmd_doctor)

    download = sub.add_parser("download", help="下载行情/估值/财务数据")
    download.add_argument("--start", help="开始日期 YYYY-MM-DD")
    download.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    download.add_argument("--full-history", action="store_true",
                          help="按各数据集实测最早可用日期全量下载"
                               "（daily 可追到 20060101；已缓存的分区自动跳过）")
    download.add_argument("--with-index", action="store_true",
                          help="同时下载指数日线（相对强度基准）")
    download.add_argument("--datasets", default="",
                          help=f"逗号分隔，默认全部：{','.join(DAILY_DATASETS)}")
    download.add_argument("--max-days", type=int, default=0,
                          help="只下最近 N 个交易日（冒烟测试用）")
    download.add_argument("--fina", action="store_true",
                          help="同时下载 fina_indicator_vip（全市场财务横截面）")
    download.add_argument("--start-year", type=int, default=2000)
    download.add_argument("--end-year", type=int, default=2026)
    download.add_argument("--force", action="store_true", help="忽略缓存强制重下")
    download.set_defaults(func=cmd_download)

    factors = sub.add_parser("factors", help="计算 35 因子并输出覆盖率")
    factors.add_argument("--start", required=True)
    factors.add_argument("--end", required=True)
    factors.add_argument("--max-days", type=int, default=0)
    factors.add_argument("--only", default="", help="只算指定因子（逗号分隔）")
    factors.add_argument("--out", default="", help="输出目录（不填只打印报表）")
    factors.add_argument("--verbose", action="store_true")
    factors.set_defaults(func=cmd_factors)

    status = sub.add_parser("status", help="查看本地缓存状态")
    status.set_defaults(func=cmd_status)

    ic = sub.add_parser("ic", help="IC/ICIR 因子有效性检验（筛掉明显无效的因子）")
    ic.add_argument("--start", required=True)
    ic.add_argument("--end", required=True)
    ic.add_argument("--horizon", type=int, default=20, help="前瞻交易日数")
    ic.add_argument("--max-days", type=int, default=0)
    ic.add_argument("--only", default="")
    ic.add_argument("--out", default="", help="IC 结果 CSV 输出路径")
    ic.set_defaults(func=cmd_ic)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TusharePermissionError as exc:
        print(f"[×] 权限问题：{exc}")
        return 3
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
