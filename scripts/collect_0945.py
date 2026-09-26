"""补采 **09:45 bar**（"9:45 不能涨停就卖"这条止盈规则的价格来源）。

## 为什么需要它

用户口径的策略要用 **09:45 的价格**判断两件事：
  ① 09:45 时是否已涨停（没涨停就止盈卖出）；
  ② 是否触发日内 -4% 止损。

现有数据里没有这个点：
  - `data/auction_hist/minute_auction.parquet` 只存了竞价段（09:25~09:31）；
  - `tick_auction_*.parquet` 只存了竞价过程（09:15~09:31），虽然原始 tick 覆盖全天，
    但要重下 22 天 × 5200 只代价大。

所以这里单独补 09:45 那根 1 分钟 bar，**覆盖 1 分钟线全区间（2025-09-19 起）**，
让"有过程"和"无过程"两组都能用同一口径回测。

## 产物

    data/auction_hist/minute_0945.parquet   # code, trade_date, open/high/low/close/volume/amount
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd
from src.core.errors import BRIEF_TIGHT

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("collect_0945")
OUT = ROOT / "data" / "auction_hist"
PROGRESS = OUT / "_progress_0945.json"
START = "20250919"


def main() -> int:
    ap = argparse.ArgumentParser(description="补采 09:45 bar")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    from xtquant import xtdata

    # ⚠️ QMT 重启后监听端口会变（实测默认 58610 失效、实际在 **58600**）。
    #    `connect()` 不传参会去扫可用地址，但扫不到就抛异常，
    #    所以这里显式按候选端口逐个试。
    connected = False
    for port in (58600, 58610, (58600, 58620)):
        try:
            xtdata.connect(port=port)
            connected = True
            logger.warning("QMT 连接端口: %s", port)
            break
        except Exception as exc:                               # noqa: BLE001
            logger.warning("端口 %s 连接失败：%s", port, brief(exc, BRIEF_TIGHT))
    if not connected:
        logger.warning("QMT 无法连接，退出")
        return 1
    codes = [c for c in xtdata.get_stock_list_in_sector("沪深A股")
             if c.split(".")[0][:1] in "036"]
    if args.limit:
        codes = codes[: args.limit]
    logger.warning("全市场 %d 只", len(codes))

    frames: list[pd.DataFrame] = []
    began = time.time()
    for i, full in enumerate(codes, 1):
        try:
            xtdata.download_history_data(full, "1m", start_time=START,
                                         end_time="20260922")
            d = xtdata.get_market_data_ex([], [full], period="1m", start_time=START,
                                          end_time="20260922", count=-1)
        except Exception:                                      # noqa: BLE001
            continue
        df = d.get(full)
        if df is None or not len(df):
            continue
        stamps = [str(x) for x in df.index]
        mask = [s[8:12] in ("0944", "0945", "0946") for s in stamps]
        if not any(mask):
            continue
        keep = df[mask].copy()
        keep["trade_date"] = [s[:8] for s, m in zip(stamps, mask) if m]
        keep["bar_time"] = [s for s, m in zip(stamps, mask) if m]
        keep["code"] = full.split(".")[0]
        frames.append(keep.reset_index(drop=True))
        if i % 300 == 0:
            logger.warning("  %d/%d  %.0fs", i, len(codes), time.time() - began)

    if not frames:
        logger.warning("一只都没取到")
        return 1
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(OUT / "minute_0945.parquet", index=False)
    logger.warning("09:45 bar 落盘：%d 行 / %d 只 / %d 天",
                   len(out), out["code"].nunique(), out["trade_date"].nunique())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
