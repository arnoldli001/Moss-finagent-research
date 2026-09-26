"""补齐"集合竞价"专项数据（为竞价选股长回测准备）。

## 为什么只补这两样

竞价选股的规则要三类数据，来源和可得性完全不同：

| 数据 | 用途 | 来源 | 最早可用 |
|---|---|---|---|
| 日线 OHLCV + 官方涨停价 + 流通市值 | 前置筛选、主板>6%、首板、均线压力 | 本地仓库 `warehouse.db` | **2010-01-04** |
| 1 分钟线 `09:30` bar（= 竞价 bar） | **竞价量比**（今竞价量 ÷ 昨日全天量） | QMT 1m | **2025-09-19** |
| 3 秒 tick（09:15~09:25） | **竞价形态**：急剧下坠 / 抢跑 / 强转弱 | QMT tick | **20260824**（只留约 1 个月）|

日线仓库里已经有，不用补。所以本脚本只补后两样：

  - `--what minute`：全市场 1 分钟线的**竞价段**（09:25~09:31），覆盖 2025-09-19 起
    一整年。为什么只要这一段：竞价量比的分母是"昨日全天成交量"（日线里有），
    分子是"今日竞价量"，而 QMT 的 `0930` bar 就是竞价 bar（已与 `auction_snapshot`
    逐位核对：`open/volume/amount` 完全一致）。
  - `--what tick`：全市场 tick 的**竞价段**（09:15~09:31），覆盖 20260824 起
    22 个交易日。tick 的服务器保留期只有约 1 个月，**过一天少一天**，
    不落盘就再也拿不到。

## 产物

    data/auction_hist/minute_auction.parquet   # code, trade_date, 时间, ohlcv
    data/auction_hist/tick_auction_<date>.parquet
    data/auction_hist/_progress.json           # 断点续跑用

## 用法

    .venv\\Scripts\\python.exe scripts/collect_auction_data.py --what minute
    .venv\\Scripts\\python.exe scripts/collect_auction_data.py --what tick
    .venv\\Scripts\\python.exe scripts/collect_auction_data.py --what all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("collect_auction")

OUT_DIR = ROOT / "data" / "auction_hist"
PROGRESS = OUT_DIR / "_progress.json"
#: 1 分钟线的起点（实测 QMT 1m 最早 2025-09-19）
MINUTE_START = "20250919"
#: tick 实测覆盖的第一天（服务器只保留约 1 个月）
TICK_START = "20260824"
#: 竞价段时间窗
AM_START_MIN = "092500"          # 1m：09:25~09:31
AM_END_MIN = "093100"
TICK_AM_START = "091500"         # tick：09:15~09:31
TICK_AM_END = "093100"


def _load_progress() -> dict:
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            return {}
    return {}


def _save_progress(data: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _universe() -> list[str]:
    from xtquant import xtdata

    xtdata.connect()
    codes = xtdata.get_stock_list_in_sector("沪深A股")
    out = [c for c in codes if c.split(".")[0][:1] in "036"]
    logger.warning("全市场 %d 只", len(out))
    return out


def collect_minute(codes: list[str], resume: bool = True) -> None:
    """全市场 1 分钟线的竞价段（09:25~09:31），覆盖 MINUTE_START 起一整年。"""
    from xtquant import xtdata

    xtdata.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "minute_auction.parquet"
    prog = _load_progress()
    done: set[str] = set(prog.get("minute_done") or []) if resume else set()
    todo = [c for c in codes if c not in done]
    logger.warning("1m 竞价段：待补 %d 只（已完成 %d）", len(todo), len(done))

    frames: list[pd.DataFrame] = []
    began = time.time()
    for i, full in enumerate(todo, 1):
        try:
            xtdata.download_history_data(full, "1m",
                                         start_time=MINUTE_START, end_time="20260922")
            d = xtdata.get_market_data_ex([], [full], period="1m",
                                          start_time=MINUTE_START, end_time="20260922",
                                          count=-1)
        except Exception as exc:                               # noqa: BLE001
            logger.warning("%s 取数失败：%s", full, exc)
            continue
        df = d.get(full)
        if df is None or not len(df):
            done.add(full)
            continue
        stamps = [str(x) for x in df.index]
        mask = [s[8:12] in ("0925", "0926", "0927", "0928", "0929", "0930", "0931")
                for s in stamps]
        if any(mask):
            keep = df[mask].copy()
            keep["trade_date"] = [s[:8] for s, m in zip(stamps, mask) if m]
            keep["bar_time"] = [s for s, m in zip(stamps, mask) if m]
            keep["code"] = full.split(".")[0]
            frames.append(keep.reset_index(drop=True))
        done.add(full)
        if i % 200 == 0:
            el = time.time() - began
            got = sum(len(f) for f in frames)
            logger.warning("  %d/%d  %.0fs (%.2f 秒/只)，已累积 %d 行",
                           i, len(todo), el, el / i, got)
            _save_progress({**prog, "minute_done": sorted(done)})

    # ⚠️ **只在最后写一次**。曾经的写法是每 200 只 `to_parquet(frames)` 覆盖写盘，
    #    但 `frames` 每个批次都被清空重建 → 每次写盘都把上一批冲掉，
    #    最终文件里只剩最后 224 只（实测：5,224 只只落了 104,908 行 / 225 只，
    #    而 `_progress.json` 却记着全部完成 —— 静默丢数据，最坏的一种 bug）。
    #    全部累积（约 250 万行 / 50 MB，完全放得下）再一次性写盘。
    if not frames:
        logger.warning("1m 竞价段：一只都没取到")
        return
    new = pd.concat(frames, ignore_index=True)
    if out_path.exists() and resume:
        old = pd.read_parquet(out_path)
        new = pd.concat([old, new], ignore_index=True)
    new.to_parquet(out_path, index=False)
    _save_progress({**prog, "minute_done": sorted(done)})
    logger.warning("1m 竞价段完成：%d 行 / %d 只，写盘 %s",
                   len(new), new["code"].nunique(), out_path)


def collect_tick(codes: list[str], days: list[str], resume: bool = True) -> None:
    """全市场 tick 的竞价段（09:15~09:31），逐日落盘。"""
    from xtquant import xtdata

    xtdata.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prog = _load_progress()
    done: set[str] = set(prog.get("tick_done") or []) if resume else set()

    for day in days:
        key = f"{day}"
        if key in done:
            logger.warning("%s 已有，跳过", day)
            continue
        out_path = OUT_DIR / f"tick_auction_{day}.parquet"
        frames: list[pd.DataFrame] = []
        began = time.time()
        for i, full in enumerate(codes, 1):
            try:
                xtdata.download_history_data(full, "tick",
                                             start_time=f"{day}{TICK_AM_START}",
                                             end_time=f"{day}{TICK_AM_END}")
                d = xtdata.get_market_data_ex([], [full], period="tick",
                                              start_time=f"{day}{TICK_AM_START}",
                                              end_time=f"{day}{TICK_AM_END}", count=-1)
            except Exception:                                  # noqa: BLE001
                continue
            df = d.get(full)
            if df is None or not len(df):
                continue
            keep = df[["time", "lastPrice", "volume", "amount", "askPrice",
                       "bidPrice", "askVol", "bidVol"]].copy()
            keep = keep.reset_index(drop=True)
            keep["code"] = full.split(".")[0]
            keep["trade_date"] = day
            frames.append(keep)
            if i % 500 == 0:
                el = time.time() - began
                logger.warning("  %s %d/%d  %.0fs", day, i, len(codes), el)
        if frames:
            pd.concat(frames, ignore_index=True).to_parquet(out_path, index=False)
            logger.warning("%s 写盘 %s（%d 只）", day, out_path.name, len(frames))
        done.add(key)
        _save_progress({**prog, "tick_done": sorted(done)})


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐集合竞价专项数据")
    ap.add_argument("--what", choices=("minute", "tick", "all"), default="all")
    ap.add_argument("--days", default="", help="tick 只补指定日期，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（小样本验证用）")
    ap.add_argument("--no-resume", action="store_true", help="忽略断点重跑")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codes = _universe()
    if args.limit:
        codes = codes[: args.limit]
        logger.warning("只处理前 %d 只（--limit）", len(codes))

    if args.what in ("minute", "all"):
        collect_minute(codes, resume=not args.no_resume)

    if args.what in ("tick", "all"):
        if args.days:
            days = [d.strip() for d in args.days.split(",") if d.strip()]
        else:
            avail = ROOT / "data" / "backtest" / "tick_availability.json"
            if avail.exists():
                days = json.loads(avail.read_text(encoding="utf-8")).get("have") or []
            else:
                days = []
        if not days:
            logger.warning("没有可补的 tick 日期（缺 data/backtest/tick_availability.json）")
        else:
            collect_tick(codes, days, resume=not args.no_resume)

    logger.warning("全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
