"""主线挖掘：按新板块池重算历史评分并落库。

## 为什么需要这一步

板块池变了（440 → 387：剔除 34 个 GICS 行业分类 + 12 个地域 + 7 个统计板块）。
但 `mainline_score` 里的历史分数是**按旧池算的** —— 同一份报告里混着两个
池子的分数，排名与 IC 都无法解释：旧池里排第 5 的板块，在新池里可能是第 3
（因为前面 2 个被剔了），而库里两条记录都写着"排名 5"。

分数是**截面百分位**，所以池子一变，**所有板块的所有历史分数都会变**，
不存在"只补新增板块"的增量做法。

## 断点续跑

890 个交易日 × 约 5 秒 ≈ 80 分钟。中途中断（网络抖动、Ctrl-C、机器重启）
必须能接着跑，否则每次都要从头再来一遍。
`--resume` 会跳过 `mainline_score` 里**已有该日记录**的交易日。

⚠️ 但 `--resume` 的判据是"这天有没有分数行"，**不是"这天的分数是不是按新池算的"**。
所以换池子之后的**第一次**必须**不加** `--resume`（或用 `--force`），
否则旧池的分数会被当成已完成而全部跳过 —— 那正是这一步要修的东西。

## 为什么 `--force` 必须同时清理两张表（2026-09-21 修）

`--force` 的意思是"这段区间全部重算"，但**只重算是不够的**：

- `mainline_score` 按 `(trade_date, board_code)` upsert，所以**换池后消失的
  板块**那一行会永远留着旧分数 —— 它们会混进截面，IC / 分位价差全部失真；
- `mainline_alert` 按 `(trade_date, board_code, level)` upsert，于是**同一个
  板块在旧配置下的 medium 行、在新配置下的 strong 行会同时存在**。
  实测 `20260918` 一天就有 20 行、其中同一板块出现两个等级 ——
  `alert_trade_stats.py` / `alert_precision_report.py` 读全部行，
  于是"告警胜率"变成**两套配置混在一起**的数字，而报告上完全看不出来。

所以 `--force` 现在默认先把区间内的两张表清空再算（`--keep-alerts` 可关）。
清空是**不可逆**的，但这段数据本来就要被新配置覆盖；真要保底请先
`CREATE TABLE ... AS SELECT` 备份。

## ⚠️ 改任何影响 `total` 尺度的配置之前，先跑阈值标定

`alert.medium_score` / `strong_score` 是**绝对阈值**，所以
`layer_weights`、各层内部权重、`gate_bonus_min/max`、`etf.bonus_*`
里的**任何一项**改动都会顺带改掉**告警政策**（告警量变多/变少），
而这一点是静默的。

实测过一次：`layer_weights` 50/50 → 100/0 后沿用原阈值，
告警量变成 **1.84x**；而按"候选池整体比例"重标定的 68/78 也是错的
（真实判据是条件分布，MEDIUM 还要求龙头共振）。

正确顺序：

    1. 改配置（层权重/阈值随意）
    2. 先跑一次「标定」：scripts/calibrate_alert_thresholds.py
       （用重叠区间匹配参考轮的**条件占比**，它会给出阈值与预计倍数）
    3. 把阈值写回 configs/mainline.yaml
    4. 再跑本脚本 --force

跑完之后用 `scripts/compare_rescore_runs.py` 核对两轮的采样告警量：
偏离 0.7~1.4x 之外就说明政策没对齐，两轮指标不可直接比较。

## 用法

    python scripts/rescore_mainline.py --start 20221201 --end 20260917
    python scripts/rescore_mainline.py --resume          # 续跑
    python scripts/rescore_mainline.py --force           # 全量重算 + 清空区间
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import get_settings  # noqa: E402
from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402
from src.mainline.service import MainlineService  # noqa: E402
from src.mainline.storage import build_mainline_repository  # noqa: E402


def _trading_days(store: MainlineDataStore, start: str, end: str) -> list[str]:
    """本地日历里的交易日（升序）；日历不可用时退回 ETF 数据里的交易日。"""
    try:
        rows = store._read(  # noqa: SLF001 只读探测
            "SELECT DISTINCT trade_date FROM ml_calendar"
            " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start, end))
        days = [str(row["trade_date"]) for row in rows]
        if days:
            return days
    except Exception as exc:  # noqa: BLE001
        print(f"  日历读取失败（退回 ETF 交易日）：{type(exc).__name__}")
    rows = store._read(  # noqa: SLF001
        "SELECT DISTINCT trade_date FROM ml_etf"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date", (start, end))
    return [str(row["trade_date"]) for row in rows]


def _done_days(path: str) -> set[str]:
    """已有评分行的交易日（`--resume` 的判据）。"""
    if not path or not Path(path).exists():
        return set()
    try:
        connection = sqlite3.connect(path)
        rows = connection.execute(
            "SELECT DISTINCT trade_date FROM mainline_score").fetchall()
        connection.close()
        return {str(row[0]) for row in rows}
    except sqlite3.Error:
        return set()


def purge_range(path: str, start: str, end: str, *,
                keep_alerts: bool) -> dict[str, int]:
    """清空 `[start, end]` 区间内的评分与告警（详见模块文档）。

    返回 `{表名: 删除行数}`。表不存在时记 0 而不是抛错 ——
    新库里可能还没建过告警表。
    """
    if not path or not Path(path).exists():
        return {}
    connection = sqlite3.connect(path, timeout=60.0)
    out: dict[str, int] = {}
    try:
        targets = ["mainline_score"] if keep_alerts else \
            ["mainline_score", "mainline_alert"]
        for table in targets:
            try:
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE trade_date BETWEEN ? AND ?",
                    (start, end))
                out[table] = cursor.rowcount or 0
            except sqlite3.Error as exc:
                print(f"  ⚠ 清理 {table} 失败（跳过）：{type(exc).__name__}")
                out[table] = 0
        connection.commit()
    finally:
        connection.close()
    return out


async def _run(start: str, end: str, resume: bool, force: bool,
               keep_alerts: bool = False) -> int:
    config = load_config()
    store = MainlineDataStore(config=config)
    settings = get_settings()
    repo = build_mainline_repository(settings)
    if repo is None:
        print("❌ 存储不可用，重打分没有意义（分数无法落库）")
        return 2
    service = MainlineService(config=config, store=store, repo=repo)

    days = _trading_days(store, start, end)
    if not days:
        print(f"❌ {start} ~ {end} 没有交易日")
        return 2

    if force:
        # ⚠️ 必须先把区间清空：否则换池后消失的板块、以及旧配置的告警等级
        # 会和新结果混在同一张表里，报告完全看不出是两套配置（见模块文档）。
        purged = purge_range(settings.sqlite_path, start, end,
                             keep_alerts=keep_alerts)
        detail = "、".join(f"{name} {count} 行"
                           for name, count in purged.items()) or "无"
        print(f"已清空区间 {start} ~ {end}：{detail}")

    done = set() if force else (_done_days(settings.sqlite_path) if resume else set())
    todo = [day for day in days if day not in done]
    print(f"区间 {start} ~ {end}：{len(days)} 个交易日，"
          f"跳过 {len(days) - len(todo)} 个，待算 {len(todo)} 个")
    if not todo:
        print("✅ 没有需要重算的交易日")
        return 0

    begun = time.monotonic()
    failed: list[str] = []
    for index, day in enumerate(todo, start=1):
        try:
            snapshot = await service.score_date(day, save=True)
        except Exception as exc:  # noqa: BLE001 单日失败不该中断整轮
            failed.append(f"{day}: {type(exc).__name__}")
            print(f"  [{index}/{len(todo)}] {day} 失败：{type(exc).__name__}")
            continue
        if index == 1 or index % 20 == 0 or index == len(todo):
            elapsed = time.monotonic() - begun
            rate = elapsed / index
            left = rate * (len(todo) - index)
            print(f"  [{index}/{len(todo)}] {day} "
                  f"板块 {len(snapshot.scores)} 告警 {len(snapshot.alerts)} "
                  f"| 已用 {elapsed / 60:.1f} 分，预计还需 {left / 60:.1f} 分",
                  flush=True)

    elapsed = time.monotonic() - begun
    print(f"\n完成：{len(todo) - len(failed)}/{len(todo)} 个交易日，"
          f"耗时 {elapsed / 60:.1f} 分")
    if failed:
        print(f"失败 {len(failed)} 天：{failed[:10]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="主线挖掘：按新板块池重算历史评分")
    parser.add_argument("--start", default="20221201",
                        help="起点（含）；默认提前一个月给告警确认留热身")
    parser.add_argument("--end", default="", help="终点（含）；留空取本地最新交易日")
    parser.add_argument("--resume", action="store_true",
                        help="跳过已有分数的交易日（换池后第一次不要用）")
    parser.add_argument("--force", action="store_true",
                        help="忽略 --resume，全部重算（并清空区间内的评分/告警）")
    parser.add_argument("--keep-alerts", action="store_true",
                        help="--force 时保留 mainline_alert（默认清空，见模块文档）")
    args = parser.parse_args(argv)

    end = args.end
    if not end:
        store = MainlineDataStore(config=load_config())
        rows = store._read("SELECT MAX(trade_date) AS d FROM ml_etf")  # noqa: SLF001
        end = str(rows[0]["d"] or "") if rows else ""
    if not end:
        print("❌ 无法确定终点（本地 ml_etf 为空）")
        return 2
    return asyncio.run(_run(args.start, end, args.resume, args.force,
                            keep_alerts=args.keep_alerts))


if __name__ == "__main__":
    raise SystemExit(main())
