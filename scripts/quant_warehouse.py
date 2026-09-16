"""量化数据仓库 CLI：把 Tushare 全量历史集中入库（MySQL / PostgreSQL / SQLite）。

用法：

    uv run python scripts/quant_warehouse.py status              # 连接与各表行数
    uv run python scripts/quant_warehouse.py bench               # 数据库 vs CSV 取数耗时
    uv run python scripts/quant_warehouse.py ingest              # 全量入库（幂等，可重跑）
    uv run python scripts/quant_warehouse.py ingest --dataset daily --dataset daily_basic
    uv run python scripts/quant_warehouse.py ingest --since 20240101
    uv run python scripts/quant_warehouse.py query --dataset daily_basic --start 20260901 \
        --code 000001 --code 600000 --limit 20

## 为什么要入库（不是"多此一举"）

回测反复按"日期区间 × 股票池"取数：数据库走索引 5~20 ms/截面，
CSV.gz 解压解析 10~40 ms，Tushare API 66~248 ms 且受 500 次/分限流。
全量历史约 3000 万行级别，入库一次、之后所有回测都受益。

## 去重是怎么保证的

每个数据集一张表，主键/唯一键就是去重键（日频 `(trade_date, code)`、
财务 `(code, report_period, ann_date)`），写入走方言原生 UPSERT。
所以 `ingest` **重复执行是安全的**：不会产生重复行，也支持"断点重跑"。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.quant.warehouse import (  # noqa: E402
    DATASET_TABLES,
    QuantWarehouse,
    WarehouseConfig,
    benchmark_sources,
    ingest_all,
)


def _progress(dataset: str, index: int, total: int, rows: int) -> None:
    if index % 200 == 0 or index == total:
        print(f"    {dataset}: {index}/{total} 分区，累计 {rows:,} 行", flush=True)


def cmd_status(args: argparse.Namespace) -> int:
    config = WarehouseConfig.from_env()
    print(f"方言      : {config.dialect or '（未配置）'}")
    print(f"连接说明  : {config.description}")
    warehouse = QuantWarehouse(config)
    if not warehouse.available():
        print("状态      : 不可用")
        return 1
    stats = warehouse.stats()
    print(f"状态      : 可用（{stats['total_rows']:,} 行）")
    print(f"{'数据集':<20}{'表名':<24}{'行数':>14}  {'起':<10}{'止':<10}")
    for item in stats["tables"]:
        print(f"{item['dataset']:<20}{item['table']:<24}{item['rows']:>14,}  "
              f"{item['first']:<10}{item['last']:<10}")
    warehouse.close()
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    report = benchmark_sources(args.dataset, days=args.days)
    print(f"数据集    : {report['dataset']}（{len(report['partitions'])} 个截面）")
    if report.get("dialect"):
        print(f"数据库    : {report['dialect']}")
    for name, value in report["timings_ms"].items():
        unit = "ms/截面" if isinstance(value, (int, float)) else ""
        print(f"{name:<10}: {value} {unit}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    datasets = args.dataset or list(DATASET_TABLES)
    unknown = [name for name in datasets if name not in DATASET_TABLES]
    if unknown:
        print(f"未知数据集：{unknown}；可选：{list(DATASET_TABLES)}")
        return 2
    if args.since:
        # 只灌增量分区（按分区键过滤，避免每次全量扫 30 万文件）
        from src.quant.dataset_store import DatasetStore

        print(f"增量模式：只入库分区键 >= {args.since}")
        for name in datasets:
            store = DatasetStore(name)
            keys = [key for key in store.keys() if key >= args.since]
            print(f"  {name}: {len(keys)} 个分区待入库", flush=True)
            config = WarehouseConfig.from_env()
            warehouse = QuantWarehouse(config)
            if not warehouse.available():
                print("  数据库不可用，退出")
                return 1
            warehouse.ensure_database()
            result = warehouse.ingest_dataset(name, keys=keys,
                                              progress=_progress)
            print(f"  {name}: {result.partitions} 分区 / {result.rows:,} 行 / "
                  f"{result.seconds:.1f}s / 失败 {len(result.failed)}")
            if result.failed:
                for key, message in list(result.failed.items())[:3]:
                    print(f"    失败 {key}: {message}")
            warehouse.close()
        return 0

    started = time.perf_counter()
    summary = ingest_all(datasets=datasets, progress=_progress)
    if summary.get("error"):
        print(f"入库失败：{summary['error']}（{summary['description']}）")
        return 1
    print(f"\n入库完成：{summary['rows']:,} 行，耗时 "
          f"{time.perf_counter() - started:.1f}s（{summary['dialect']}）")
    for item in summary["datasets"]:
        print(f"  {item['dataset']:<20} {item['partitions']:>5} 分区 "
              f"{item['rows']:>12,} 行  {item['seconds']:>7.1f}s")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    config = WarehouseConfig.from_env()
    warehouse = QuantWarehouse(config)
    if not warehouse.available():
        print(f"数据库不可用：{config.description}")
        return 1
    started = time.perf_counter()
    frame = warehouse.load(args.dataset, start=args.start or "", end=args.end or "",
                           codes=args.code, limit=args.limit)
    elapsed = (time.perf_counter() - started) * 1000
    print(f"{len(frame):,} 行，{elapsed:.1f} ms（{config.dialect}）")
    if len(frame):
        print(frame.head(args.head).to_string(index=False))
    warehouse.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="量化数据仓库（集中入库 + 去重）")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="连接状态与各表行数")
    status.set_defaults(func=cmd_status)

    bench = sub.add_parser("bench", help="数据库 vs CSV 取数耗时")
    bench.add_argument("--dataset", default="daily_basic")
    bench.add_argument("--days", type=int, default=5)
    bench.set_defaults(func=cmd_bench)

    ingest = sub.add_parser("ingest", help="CSV 分区入库（幂等）")
    ingest.add_argument("--dataset", action="append", default=None,
                        help="可重复；缺省为全部数据集")
    ingest.add_argument("--since", default="", help="只入库该分区键之后的（增量）")
    ingest.set_defaults(func=cmd_ingest)

    query = sub.add_parser("query", help="查询仓库")
    query.add_argument("--dataset", default="daily_basic")
    query.add_argument("--start", default="")
    query.add_argument("--end", default="")
    query.add_argument("--code", action="append", default=None)
    query.add_argument("--limit", type=int, default=0)
    query.add_argument("--head", type=int, default=10)
    query.set_defaults(func=cmd_query)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
