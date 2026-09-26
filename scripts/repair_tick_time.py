"""修复已落盘 tick 文件里的 `time_seconds`（时区偏移 bug 的数据订正）。

## 这个 bug 是什么

QMT tick 的 `time` 列是 **epoch 毫秒（UTC 基准）**，而它的 DataFrame 索引
是**本地时间**（实测：`time=1789694100000` ↔ 索引 `20260918091500` ↔
本地 UTC+8 的 09:15:00）。

旧版 `series_from_stored_tick` 用 `(ms // 1000) % 86400` 求"当天秒数"，
得到的是 **UTC** 的当天秒数，比本地**少 8 小时**（28800 秒）：
09:15 变成 01:15，于是 `09:19:20~09:25:00` 窗口里一个点都取不到，
形态判定全线退化成「未识别」。

## 为什么能离线修复

tick 文件里**同时保留了 `time`（epoch 毫秒）与 `time_seconds`（算错的）**，
所以订正量是确定的：`正确 = 旧的 + 本地偏移秒数`。
不需要重新向服务器下载（而服务器只保留约 1 个月，重下还会失败）。

## 用法

    .venv\\Scripts\\python.exe scripts/repair_tick_time.py            # 全部文件
    .venv\\Scripts\\python.exe scripts/repair_tick_time.py --dry-run  # 只看要改什么
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("repair_tick_time")
OUT = ROOT / "data" / "auction_hist"


def offset_seconds() -> int:
    """本机相对 UTC 的偏移秒数（东八区 = 28800）。"""
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


def fix_file(path: Path, off: int, *, dry: bool = False) -> dict:
    df = pd.read_parquet(path)
    if not len(df) or "time" not in df.columns:
        return {"file": path.name, "rows": 0, "note": "无 time 列"}
    ms = df["time"].astype("int64")
    # 正确值：本地 HHMMSS → 当天秒数。用 epoch + 本地偏移才是本地墙上时间。
    local = pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert(
        datetime.now().astimezone().tzinfo)
    correct = (local.dt.hour * 3600 + local.dt.minute * 60 + local.dt.second)
    changed = 0
    if "time_seconds" in df.columns:
        old = pd.to_numeric(df["time_seconds"], errors="coerce")
        changed = int((old != correct).sum())
    else:
        changed = len(df)
    info = {"file": path.name, "rows": len(df), "changed": changed,
            "old_min": int(pd.to_numeric(df.get("time_seconds"), errors="coerce").min())
            if "time_seconds" in df.columns else None,
            "new_min": int(correct.min()), "new_max": int(correct.max())}
    if dry or not changed:
        return info
    df["time_seconds"] = correct.astype("int64")
    # 顺手补一列可读的本地时间，往后就不必再靠 epoch 反推
    df["time_local"] = local.dt.strftime("%H:%M:%S")
    df.to_parquet(path, index=False)
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="订正 tick 文件的 time_seconds")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    off = offset_seconds()
    print(f"本机 UTC 偏移 = {off} 秒（{off // 3600} 小时）")
    files = sorted(OUT.glob("tick_auction_*.parquet"))
    print(f"待处理 {len(files)} 个文件" + ("（dry-run）" if args.dry_run else ""))
    total_changed = 0
    for p in files:
        info = fix_file(p, off, dry=args.dry_run)
        total_changed += info.get("changed") or 0
        print("  %-32s %7d 行  订正 %6d  秒数 %s~%s" % (
            info["file"], info["rows"], info.get("changed") or 0,
            info.get("new_min"), info.get("new_max")))
    print(f"\n合计订正 {total_changed:,} 行")
    if not args.dry_run:
        print("提示：`time_local` 列已写入，后续重建数据集会自动用对的秒数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
