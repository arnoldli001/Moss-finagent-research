"""修补交易日历：把「多张行情表都有数据、日历却没有」的日期补进去。

## 为什么需要这个脚本

`sync_calendar` 完全信任上游 `trade_cal`。实测 `ml_calendar` **整段缺
20251201~20251231（23 个交易日）**，而 `ml_index` / `ml_future` / `ml_etf`
这 23 天都有连续数据。后果是链式的，而且都不报错：

1. `rescore_mainline._trading_days` 只读日历 → 整个 12 月**不被打分**，
   `mainline_score` 从 20251128 直接跳到 20260105，最新回测窗口少 23 天；
2. 突破触发要取"过去 120 个交易日"的 `total` 序列，洞正好落在 2026 年
   上半年的回看窗口里 → 上沿是用跨过空洞的数据算出来的；
3. `score_total_history_sync` 的连续性闸门在 20260105 判为"表落后 38 天"，
   那条触发路径当天整条失效。

还顺带解释了另一个更难看的症状：`ml_board_bar` 在**休市日**也有零散行
（2026-05-01 有 32 个板块、2025-05-01 有 250 个），这些日期不在日历里，
宽表上就是一整行 NaN；`rolling(20)` 要求窗口内 20 个有效值，
**一个 NaN 就让之后 20 个交易日的滚动特征全部变 NaN**。
实测告警集 42% 的行 `vol20` 缺失，就是从这里来的。

所以本脚本干两件事：把真交易日补进日历（`repair_calendar`），
并把「只有单张表有数据」的可疑日期列出来供人工核对。

用法：
    .venv\\Scripts\\python.exe scripts/repair_calendar.py --dry-run
    .venv\\Scripts\\python.exe scripts/repair_calendar.py
    .venv\\Scripts\\python.exe scripts/repair_calendar.py \\
        --start 20251101 --end 20260110 --out docs/MAINLINE_CALENDAR_REPAIR.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402

WITNESSES = ("ml_index", "ml_future", "ml_etf", "ml_board_bar")
#: 只有这三张表参与「算不算交易日」的判定（与 `MainlineDataStore.CALENDAR_WITNESSES`
#: 保持一致）。`ml_board_bar` 只用于展示"谁有数据"，**不能**参与判定 ——
#: 它在休市日也有零散行（2026-05-01 有 32 个板块），拿它当证人会把休市日
#: 写成交易日，反而制造出更毒的 NaN 空洞。
DECIDING = ("ml_index", "ml_future", "ml_etf")


def witness_counts(path: str, start: str, end: str) -> dict[str, set[str]]:
    """每张行情表在区间内出现过的日期集合。"""
    out: dict[str, set[str]] = {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table in WITNESSES:
            sql = f"SELECT DISTINCT trade_date FROM {table}"
            params: tuple[str, ...] = ()
            if start and end:
                sql += " WHERE trade_date BETWEEN ? AND ?"
                params = (start, end)
            out[table] = {str(row[0]) for row in conn.execute(sql, params)}
    finally:
        conn.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="修补交易日历")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--min-sources", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    config = load_config()
    store = MainlineDataStore(config=config)
    # 日历与所有交叉表都在**缓存仓**里（`config.cache_file`），
    # 不是结果库 `settings.sqlite_path` —— 拿错库会得到"表不存在 → 0 天要补"。
    path = str(store.path)
    if not Path(path).exists():
        emit(f"❌ 数据仓不存在：{path}")
        return 2

    before = store.calendar()
    result = store.repair_calendar(start=args.start, end=args.end,
                                   min_sources=args.min_sources,
                                   apply=not args.dry_run)
    after = store.calendar()

    emit("# 交易日历修补")
    emit()
    emit(f"> 由 `scripts/repair_calendar.py` 生成，数据仓 `{path}`。")
    emit(f"> 判据：{args.min_sources} 张以上独立行情表同日有数据即视为交易日"
         f"（判定用表：{'、'.join(DECIDING)}；展示时另附 `ml_board_bar`）。")
    emit()
    emit(f"- 修补前日历 {len(before)} 天，修补后 {len(after)} 天，"
         f"本轮{'待补' if args.dry_run else '补入'} {result.rows} 天")
    if result.message:
        emit(f"- {result.message}")
    added = sorted(set(after) - set(before))
    if added:
        emit()
        emit("## 新增交易日")
        emit()
        for day in added:
            emit(f"- {day}")
    if result.missing:
        emit()
        emit(f"## 只有单张表有数据、**未**补入的可疑日期（{len(result.missing)} 天）")
        emit()
        emit("这些多半是休市日的脏数据（单表有零散行）。它们会在宽表上留下一整行"
             "NaN，进而毒化之后 20 个交易日的滚动特征，所以**不应**进日历；"
             "消费行情表时应以日历为准做过滤。")
        emit()
        counts = witness_counts(path, args.start, args.end)
        for day in result.missing[:60]:
            who = [name for name in WITNESSES if day in counts.get(name, set())]
            emit(f"- {day}（{'、'.join(who)}）")
        if len(result.missing) > 60:
            emit(f"- …共 {len(result.missing)} 天")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
