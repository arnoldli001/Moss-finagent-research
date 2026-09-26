"""等重打分跑完再放行后续分析脚本的**自检**（只读，不写库）。

## 为什么需要它

重打分是「先清空区间、再逐日写」。中途读 `mainline_score` 得到的是**残表**，
而残表看起来完全正常（§16.39 记过这个坑：报告照样生成、数字照样合理，
只是口径只剩一半）。所以分析链必须有一个**机器可判**的放行条件，
而不是靠人盯着日志。

判据用「区间内有分数的交易日数」：它随重打分单调增长，到 719 就是跑完了
（719 = `ml_calendar` 在 20231009~20260918 的交易日数）。
**不用"进程还在不在"** —— 机器上同时可能有别的 python 进程；
也**不用"日志有没有 `[719/719]`"** —— `--resume` 与 `--force` 的日志格式不同。

## 为什么写成独立脚本而不是内联

第一版是 PowerShell 里内联 `python -c "..."`，SQL 里的 `COUNT(DISTINCT ...)`
被 PowerShell 当成了命令（`\"` 在 PowerShell 里**不是**转义符，
真正终止字符串的就是那个 `"`）。这类引号嵌套错误在中文日志里很难看出来，
所以挪进 `.py` 文件，用 `--min` / `--stable` 参数表达等待条件。

用法：
    .venv\\Scripts\\python.exe scripts/wait_rescore.py --min 715 --stable 4
    .venv\\Scripts\\python.exe scripts/wait_rescore.py --timeout 150 --check
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: ⚠️ 与 `src/core/config.py` 的 `MOSS_SQLITE_PATH` 对齐：重打分跑在副本上时，
#: 等待器必须看**同一个副本**，否则会盯着没在变的生产库一直等到超时。
MAIN_DB = ROOT / os.environ.get("MOSS_SQLITE_PATH", "data/moss_finagent.db")
CACHE_DB = ROOT / "data" / "mainline_cache.db"
START, END = "20221201", ""


def calendar_days(start: str, end: str) -> int:
    """`ml_calendar` 在 [start, end] 里的交易日数 —— 跑完的标志。

    ⚠️ 不要写死 719：那个数字是"某个特定区间"的交易日数，
    区间一变（比如把起点从 20231009 提前到 20221201 做热身）它就错了，
    而错误的 `--target` 会让等待器**永远等不到**（或提前放行残表）。
    """
    conn = sqlite3.connect(f"file:{CACHE_DB.as_posix()}?mode=ro", uri=True)
    try:
        low, high = conn.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM ml_calendar").fetchone()
        if not end:
            end = str(high)
        if not start:
            start = str(low)
        return int(conn.execute(
            "SELECT COUNT(*) FROM ml_calendar WHERE trade_date BETWEEN ? AND ?",
            (start, end)).fetchone()[0])
    finally:
        conn.close()


def progress(start: str, end: str) -> int:
    """区间内**已有分数行**的交易日数（open 只读，不干扰重打分的写锁）。"""
    conn = sqlite3.connect(f"file:{MAIN_DB.as_posix()}?mode=ro", uri=True)
    try:
        if not end:
            end = str(conn.execute(
                "SELECT MAX(trade_date) FROM mainline_score").fetchone()[0] or "99999999")
        return int(conn.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM mainline_score"
            " WHERE trade_date BETWEEN ? AND ?", (start, end)).fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="等重打分结束（只读）")
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--target", type=int, default=0,
                        help="期望的交易日数（缺省=按 ml_calendar 自动推算）")
    parser.add_argument("--min", type=int, default=0,
                        help="至少要有这么多天才算跑完（默认 = target - 4）")
    parser.add_argument("--stable", type=int, default=4,
                        help="连续多少次读数不变才算停（每次间隔 --interval 秒）")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=150, help="最长等待分钟数")
    parser.add_argument("--check", action="store_true",
                        help="只看一眼，不等待")
    args = parser.parse_args()

    end = args.end
    target = args.target or calendar_days(args.start, end)
    if not end:
        conn = sqlite3.connect(f"file:{CACHE_DB.as_posix()}?mode=ro", uri=True)
        end = str(conn.execute("SELECT MAX(trade_date) FROM ml_calendar").fetchone()[0])
        conn.close()
    print(f"库={MAIN_DB.name}  区间={args.start}~{end}  目标={target} 个交易日")

    minimum = args.min or max(1, target - 4)
    current = progress(args.start, end)
    if args.check:
        print(f"已算 {current} / {target} 个交易日")
        return 0 if current >= minimum else 2

    deadline = time.monotonic() + args.timeout * 60
    last, stable = current, 0
    while time.monotonic() < deadline:
        if last >= minimum and stable >= args.stable:
            break
        time.sleep(args.interval)
        current = progress(args.start, end)
        stable = stable + 1 if current == last else 0
        last = current
        print(f"  {time.strftime('%H:%M:%S')} 已算 {last} / {target}"
              f"（连续不变 {stable} 次）", flush=True)
    print(f"结束：已算 {last} / {target}，stable={stable}，"
          f"门槛={minimum}")
    if last < minimum:
        print("❌ 未达标 —— 不要在此基础上出结论（残表）")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
