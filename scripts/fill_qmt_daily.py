"""把 QMT 本地日线补到最新交易日（逐只 `download_history_data`）。

为什么需要它：
    `get_market_data_ex` 只会返回**本地已有**的日线。实测本机 QMT 的日线
    在 20260918 之后对约 4000 只股票返回"沿用一个旧价、成交量为 0"的陈旧 bar
    （看起来像停牌，其实是没有数据）。这种 bar 一旦参与回测/指标计算，
    会静默污染结果 —— 所以必须先补齐，再算。

为什么不用 `download_history_data2`：
    回调式接口在本机实测极慢（3 分钟一个 200 只分片都回不来）；
    逐只 `download_history_data` 实测 ~22 只/秒（50 只用 2.1 秒）。

只补"缺的那几天"：先批量读一遍，找出末根 < 目标日的代码，只对它们下载。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("fill_qmt_daily")


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 QMT 本地日线")
    ap.add_argument("--target", default="20260922", help="目标最新交易日")
    ap.add_argument("--from", dest="start", default="20260801", help="下载起点")
    ap.add_argument("--all", action="store_true", help="不管缺不缺，全部重下")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    from xtquant import xtdata

    xtdata.connect()
    codes = [c for c in xtdata.get_stock_list_in_sector("沪深A股")
             if c.split(".")[0][:1] in "036"]
    logger.info("全市场 %d 只，目标 %s", len(codes), args.target)

    # 先批量读一遍，找出末根不够新的代码
    stale: list[str] = []
    if args.all:
        stale = list(codes)
    else:
        for i in range(0, len(codes), 600):
            chunk = codes[i:i + 600]
            data = xtdata.get_market_data_ex([], chunk, period="1d",
                                             start_time=args.target, end_time=args.target,
                                             count=-1)
            for full in chunk:
                df = (data or {}).get(full)
                fresh = False
                if df is not None and len(df):
                    for ix, row in df.iterrows():
                        if str(ix)[:8] == args.target and float(row["volume"] or 0) > 0:
                            fresh = True
                            break
                if not fresh:
                    stale.append(full)
    logger.info("缺 %s 的代码 %d 只，开始逐只补", args.target, len(stale))

    began = time.time()
    done = 0
    for i, full in enumerate(stale, 1):
        try:
            xtdata.download_history_data(full, "1d",
                                         start_time=args.start, end_time=args.target)
        except Exception as exc:                               # noqa: BLE001
            logger.warning("%s 下载失败：%s", full, exc)
        done += 1
        if done % 200 == 0:
            el = time.time() - began
            logger.info("  %d/%d  %.0fs  (%.1f 只/秒)",
                        done, len(stale), el, done / max(el, 1e-9))
    el = time.time() - began
    logger.info("下载完成 %d 只，用时 %.0fs（%.1f 只/秒）",
                done, el, done / max(el, 1e-9))

    # 复核
    still = 0
    for i in range(0, len(codes), 600):
        chunk = codes[i:i + 600]
        data = xtdata.get_market_data_ex([], chunk, period="1d",
                                         start_time=args.target, end_time=args.target,
                                         count=-1)
        for full in chunk:
            df = (data or {}).get(full)
            ok = False
            if df is not None and len(df):
                for ix, row in df.iterrows():
                    if str(ix)[:8] == args.target and float(row["volume"] or 0) > 0:
                        ok = True
                        break
            if not ok:
                still += 1
    logger.info("复核：%s 仍缺 %d 只（真的停牌/退市的不算问题）", args.target, still)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
