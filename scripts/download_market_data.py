"""全市场行情全量下载（**取代原 `download_qmt_data.py`**）。

## 为什么替换

原脚本用迅投 QMT 的 `download_history_data2` 把全市场日线拉进 QMT 本地库
（`userdata_mini/datadir`），再由 `XtQuantConnector` 读。2026-09 本机 QMT 终端
**失去行情权限且不再运行**（`127.0.0.1:58610` 拒连），这条路彻底断了；
而且 QMT 的数据只在它的私有目录里，换源时拿不走。

新脚本把数据落到**项目自己的目录**（`data/quant/prices/`），
取数走项目统一采集链（`build_daily_connector_chain()`），所以：

  - 源可换（AkShare → 腾讯 → Tushare → baostock，QMT 可选），脚本本身与源无关；
  - 落盘格式就是 `PriceStore` 的格式（一日一行的前复权 OHLCV + manifest），
    量化选股/回测/缠论直接可读，不需要再经 QMT；
  - **增量**：manifest 记录已覆盖区间，重复执行只补缺口（可断点续跑）。

## 与项目既有下载器的分工（不要重复造）

| 脚本 | 覆盖 | 说明 |
|---|---|---|
| `scripts/quant_sync.py` / `src/quant/download.py` | Tushare 35 因子数据集（daily/daily_basic/资金流/财务…） | 按**交易日**分区，给多因子用 |
| `scripts/quant_warehouse.py` | 上面那些落进数据库（MySQL/PG/SQLite） | 回测取数走库更快 |
| **本脚本** | 全市场**前复权 OHLCV 宽表**（按标的落盘） | 给价格面板/缠论/回测，**免 token 也能跑** |
| `src/quant/price_panel.fetch_daily_bars` | 单只按需取 | 本脚本是它的批量预取 |

## 用法

    # 小批量验证（10 只，近两年）
    .venv\\Scripts\\python.exe scripts/download_market_data.py --limit 10

    # 全量：沪深A股全历史前复权日线（增量，可重复执行）
    .venv\\Scripts\\python.exe scripts/download_market_data.py

    # 指定区间 + 并发
    .venv\\Scripts\\python.exe scripts/download_market_data.py \\
        --start 20200101 --end 20260930 --concurrency 16

    # 一并下载宽基指数与 ETF（默认都下）
    .venv\\Scripts\\python.exe scripts/download_market_data.py --no-index --no-etf

    # 只看会下哪些标的，不取数
    .venv\\Scripts\\python.exe scripts/download_market_data.py --dry-run

## 断点续跑

`PriceStore.sync` 已按 manifest 跳过"已覆盖区间"的标的，所以**中断后重跑即可**：
已下好的会被跳过，只有缺口会重新取数。`--force` 可强制全量重下。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quant.price_panel import DEFAULT_ROOT, PriceStore  # noqa: E402

LOG_DIR = ROOT / "data" / "qmt"

#: 宽基指数（与旧 `download_qmt_data.py` 的 INDEX_CODES 对齐，改用项目指标前缀）
INDEX_CODES = [
    "000001",  # 上证综指
    "399001",  # 深证成指
    "000300",  # 沪深300
    "000905",  # 中证500
    "000852",  # 中证1000
    "399006",  # 创业板指
    "000688",  # 科创50
]

#: 常用宽基 ETF（有前复权，量化做 ETF 池时的基础）
ETF_CODES = [
    "510300",  # 沪深300ETF
    "510500",  # 中证500ETF
    "588000",  # 科创50ETF
    "159915",  # 创业板ETF
    "512880",  # 证券ETF
    "512480",  # 半导体ETF
]

#: 默认起始日：给足多因子/缠论的回看窗口。更早的历史对回测边际价值低而下载成本线性增长。
DEFAULT_START = "20150101"


def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"download_market_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("market_download")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.info("日志文件: %s", log_path)
    return logger


def resolve_universe(logger: logging.Logger) -> list[str]:
    """沪深A股代码表（6 位数字，剔除北交所）。

    用 Tushare `stock_basic` 拿权威名录（含 `list_status`），**不再**用 QMT 的
    `get_stock_list_in_sector("沪深A股")`。拿不到时退回"已有本地缓存 + 指数ETF"，
    而不是编一份名单出来。
    """
    try:
        from src.quant.tushare_source import TushareClient, resolve_token

        client = TushareClient(resolve_token())
        frame = client.call(api="stock_basic", exchange="", list_status="L")
        if frame is not None and len(frame):
            codes = [
                str(symbol).zfill(6)
                for symbol in frame["symbol"].tolist()
                if str(symbol).strip().isdigit()
            ]
            # 剔除北交所：腾讯/新浪/baostock 覆盖不全，取数会大面积失败
            codes = [c for c in codes if not c.startswith(("4", "8", "920"))]
            logger.info("标的池：Tushare stock_basic 沪深A股 %d 只", len(codes))
            return sorted(set(codes))
    except Exception as exc:  # noqa: BLE001 token缺失/无权限都不该让脚本直接死
        logger.warning("Tushare 名录不可用（%s），退回本地缓存名录", exc)
    cached = PriceStore(DEFAULT_ROOT).cached_codes()
    logger.warning("标的池：本地已缓存 %d 只（名录接口不可用，只做增量刷新）", len(cached))
    return sorted(cached)


def _fmt(info: object) -> str:
    data = info.as_dict() if hasattr(info, "as_dict") else {}
    return (f"取到 {data.get('fetched', '?')} / 跳过 {data.get('skipped', '?')} / "
            f"失败 {data.get('failed', '?')}，累计 {data.get('rows', 0):,} 行")


async def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    end = (args.end or datetime.now().strftime("%Y%m%d")).replace("-", "")
    start = (args.start or DEFAULT_START).replace("-", "")
    if start > end:
        logger.error("起始日 %s 晚于结束日 %s", start, end)
        return 2

    universe = resolve_universe(logger)
    if args.limit:
        universe = universe[: args.limit]
    if not universe and not args.no_index:
        logger.error("标的池为空且未禁用指数：没有任何可下载的标的")
        return 2

    index_codes = [] if args.no_index else INDEX_CODES
    etf_codes = [] if args.no_etf else ETF_CODES

    logger.info("区间 %s ~ %s；个股 %d 只、指数 %d 只、ETF %d 只；并发 %d",
                start, end, len(universe), len(index_codes), len(etf_codes),
                args.concurrency)
    if args.dry_run:
        logger.info("[dry-run] 个股前 20 只: %s", universe[:20])
        logger.info("[dry-run] 指数: %s", index_codes)
        logger.info("[dry-run] ETF: %s", etf_codes)
        return 0

    store = PriceStore(args.root)
    # 指数与 ETF 各用一个**独立子目录**：`PriceStore` 按 6 位代码命名文件，
    # 若三类共用同一个目录，`index_close:000001`（上证综指）与
    # `stock_close:000001`（平安银行）会互相覆盖 —— 而且覆盖是静默的，
    # 表现为"某只票的历史莫名变成指数点位"。
    index_store = PriceStore(Path(args.root) / "index")
    etf_store = PriceStore(Path(args.root) / "etf")

    began = time.time()
    totals = {"fetched": 0, "skipped": 0, "failed": 0, "rows": 0}
    failures: dict[str, str] = {}

    # 三批分开跑：个股是主体，指数/ETF 走不同指标前缀（各自有独立容灾链）。
    batches: list[tuple[str, list[str], Any, PriceStore]] = []
    if universe:
        batches.append((
            "stock", universe, lambda code: f"stock_close:{code}", store))
    if index_codes:
        batches.append((
            "index", index_codes, lambda code: f"index_close:{code}", index_store))
    if etf_codes:
        batches.append((
            "etf", etf_codes, lambda code: f"etf_close:{code}", etf_store))

    for tag, codes, indicator_of, target_store in batches:
        logger.info("[%s] 开始 %d 只 ...", tag, len(codes))
        info = await target_store.sync(
            codes, start, end,
            force=args.force, concurrency=args.concurrency,
            indicator_of=indicator_of)
        logger.info("[%s] %s", tag, _fmt(info))
        data = info.as_dict()
        for key in totals:
            totals[key] += int(data.get(key) or 0)
        failures.update({f"{tag}:{k}": v for k, v in (info.failed or {}).items()})

    elapsed = time.time() - began
    logger.info("=" * 72)
    logger.info("完成：取到 %d、跳过 %d、失败 %d，落盘 %s 行，耗时 %.1fs",
                totals["fetched"], totals["skipped"], totals["failed"],
                f"{totals['rows']:,}", elapsed)
    if failures:
        logger.error("失败 %d 只（前 20）：%s", len(failures),
                     "; ".join(f"{k}={v}" for k, v in list(failures.items())[:20]))
        return 1
    logger.info("全部成功")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场行情全量下载（替代 download_qmt_data.py）")
    parser.add_argument("--limit", type=int, default=0, help="只下载前N只（验证用）")
    parser.add_argument("--start", default="", help=f"起始日 YYYYMMDD（默认 {DEFAULT_START}）")
    parser.add_argument("--end", default="", help="结束日 YYYYMMDD（默认今天）")
    parser.add_argument("--root", default=DEFAULT_ROOT, help=f"落盘目录（默认 {DEFAULT_ROOT}）")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="并发数（Tushare 500次/分、腾讯无明确限频，8~16 较稳）")
    parser.add_argument("--force", action="store_true", help="忽略已覆盖区间，全量重下")
    parser.add_argument("--no-index", action="store_true", help="跳过宽基指数")
    parser.add_argument("--no-etf", action="store_true", help="跳过 ETF")
    parser.add_argument("--dry-run", action="store_true", help="只打印标的池，不取数")
    args = parser.parse_args()

    logger = setup_logger()
    return asyncio.run(run(args, logger))


if __name__ == "__main__":
    sys.exit(main())
