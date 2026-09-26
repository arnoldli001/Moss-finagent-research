"""重算板块拥挤度的资金流列（用**主线提纯后**的成分股），并留下对照记录。

## 为什么要重算

拥挤度第 4 列 = `Σ成分股主力净流入(近20日) / 板块流通市值(基准日)`。
在接入提纯名单之前它用的是 `sector_member`（东财 `ths_member` **原始**名单），
而主线挖掘早已换成 `ml_member_pure`。原始名单里的沾边个股会把主力集中度
稀释掉，所以这次把拥挤度也切到同一份提纯名单，并重算落库。

## 这份脚本验证什么

1. **来源是否真的生效**：`sector_crowding_metric.member_source` 的
   `purified` / `raw` 计数（提纯表只覆盖 324 个板块，其余 1554 个必须
   回退原始名单 —— 回退不是缺陷，但**必须可审计**）。
2. **点名板块的对照**：培育钻石 885937、玻璃基板 886111 重算前后的
   成分股只数、净流入、流通市值、净流入占比。
3. **全局口径漂移**：提纯前后 `flow_ratio` 的中位数变化 —— 提纯后参与
   计算的股票少了，同一个板块的比率会系统性变化，幅度要看清楚。

只读主线库；只写拥挤度库（`sector_crowding_metric`）。

用法：
    .venv\\Scripts\\python.exe scripts/refresh_crowding_metric.py --dry-run
    .venv\\Scripts\\python.exe scripts/refresh_crowding_metric.py \\
        --out docs/MAINLINE_CROWDING_PURIFIED.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.sector_crowding import db, metrics  # noqa: E402
from src.sector_crowding.config import load_config  # noqa: E402

WATCHED = (("885937.TI", "培育钻石"), ("886111.TI", "玻璃基板"))


def snapshot(conn: sqlite3.Connection, week: str) -> dict[str, dict]:
    rows = db.query_metrics(conn, week=week) if week else {}
    return {str(code): row for code, row in (rows or {}).items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="用提纯成分股重算拥挤度资金流")
    parser.add_argument("--dry-run", action="store_true",
                        help="只看来源判定，不写库")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    config = load_config()
    conn = db.get_db_connection(config)
    db.init_tables(conn)
    try:
        week = db.latest_metric_week(conn)
        before = snapshot(conn, week)
        meta_before = db.get_metric_meta(conn)
        pure_path = config.mainline_cache_path
        pure_conn = metrics.open_pure_db(pure_path)

        emit("# 板块拥挤度：切到主线提纯成分股后的重算")
        emit()
        emit("> 由 `scripts/refresh_crowding_metric.py` 生成。")
        emit(f"> 提纯来源：`{pure_path}`（只读）")
        emit(f"> 开关：`use_purified_members={config.metrics.use_purified_members}`，"
             f"`min_purified_members={config.metrics.min_purified_members}`")
        emit(f"> 重算前：周 `{week}`，{len(before)} 个板块，"
             f"上次计算于 {meta_before.get('last_computed_at', '—')}")
        emit()

        emit("## 一、点名板块的成分股来源")
        emit()
        emit("| 板块 | 原始名单 | 提纯名单(relevant=1) | 本次将用 |")
        emit("|---|---:|---:|---|")
        for code, label in WATCHED:
            raw = conn.execute(
                f"SELECT COUNT(*) n FROM {db.MEMBER_TABLE} WHERE sector_code=?",
                (code,)).fetchone()["n"]
            pure = 0
            if pure_conn is not None:
                row = pure_conn.execute(
                    "SELECT COUNT(*) n FROM ml_member_pure"
                    " WHERE board_code=? AND relevant=1", (code,)).fetchone()
                pure = int(row["n"] or 0)
            use = "提纯" if pure >= config.metrics.min_purified_members else "原始（提纯不足）"
            emit(f"| {label}（{code}） | {int(raw)} | {pure} | **{use}** |")
        emit()

        if args.dry_run:
            emit("（`--dry-run`：未写库）")
            if pure_conn is not None:
                pure_conn.close()
            if args.out:
                Path(ROOT / args.out).write_text("\n".join(lines) + "\n",
                                                 encoding="utf-8")
            return 0

        if pure_conn is not None:
            pure_conn.close()

        emit("## 二、重算（全板块，force）")
        emit()
        task = metrics.compute_all_metrics(config=config, force=True)
        emit(f"- 状态 `{task.status}`，写入 {task.rows_written} 行，"
             f"耗时 {task.seconds:.1f} 秒")
        if task.failed_sectors:
            emit(f"- 失败板块 {len(task.failed_sectors)} 个")
        emit()

        after_week = db.latest_metric_week(conn)
        after = snapshot(conn, after_week)
        meta_after = db.get_metric_meta(conn)
        emit("## 三、来源落库核对")
        emit()
        emit(f"- `purified_sectors = {meta_after.get('purified_sectors', '—')}`，"
             f"`raw_sectors = {meta_after.get('raw_sectors', '—')}`")
        emit(f"- `use_purified_members = "
             f"{meta_after.get('use_purified_members', '—')}`")
        tally: dict[str, int] = {}
        for row in db.query_metrics(conn, week=after_week).values():
            key = str(row.get("member_source") or "（空）")
            tally[key] = tally.get(key, 0) + 1
        emit("- 逐行统计：" + "、".join(f"{k} {v} 个板块"
                                      for k, v in sorted(tally.items())))
        emit()

        emit("## 四、点名板块：重算前后")
        emit()
        emit("| 板块 | 指标 | 重算前 | 重算后 | 变化 |")
        emit("|---|---|---:|---:|---:|")
        for code, label in WATCHED:
            old = before.get(code) or {}
            new = after.get(code) or {}
            for field, name in (("flow_ratio", "净流入占比 %"),
                                ("net_inflow", "净流入"),
                                ("circ_mv_base", "流通市值")):
                left, right = old.get(field), new.get(field)
                if left is None or right is None:
                    emit(f"| {label} | {name} | {left} | {right} | — |")
                    continue
                delta = (right - left)
                emit(f"| {label} | {name} | {left:,.4f} | {right:,.4f} "
                     f"| {delta:+,.4f} |")
            emit(f"| {label} | 名单来源 | {old.get('member_source', '—')} "
                 f"| {new.get('member_source', '—')} | — |")
        emit()

        emit("## 五、全局口径漂移")
        emit()
        drift = []
        for code, new in after.items():
            old = before.get(code)
            if not old:
                continue
            left, right = old.get("flow_ratio"), new.get("flow_ratio")
            if left is None or right is None:
                continue
            drift.append((right - left, code))
        if drift:
            drift.sort()
            values = [item[0] for item in drift]
            mid = values[len(values) // 2]
            emit(f"- 可比板块 {len(drift)} 个；`flow_ratio` 变化："
                 f"中位 {mid:+.4f}pp，最小 {values[0]:+.4f}pp，"
                 f"最大 {values[-1]:+.4f}pp")
            emit("- 变化最大的 5 个（升序）："
                 + "、".join(f"{code} {delta:+.3f}" for delta, code in drift[:5]))
            emit("- 变化最大的 5 个（降序）："
                 + "、".join(f"{code} {delta:+.3f}" for delta, code in drift[-5:]))
        else:
            emit("- 没有可比行（首次重算？）")
        emit()
        emit("⚠️ 提纯后参与计算的股票变少（实测压缩比中位 0.51），"
             "`flow_ratio` 会系统性变化 —— 它衡量的是**核心成分股**的资金集中度，"
             "不是原来那个被沾边股稀释过的数。跨周比较时要注意口径切换的那一周。")
    finally:
        conn.close()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
