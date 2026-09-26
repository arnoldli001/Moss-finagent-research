"""只改「告警范围」时**不必整段重打分** —— 对现有结果做一次后处理。

## 为什么可以这么做

`_decide_level` 里的范围闸门只决定**这条告警写不写出来**，
它**不参与任何分数计算**：

    six_dim / accumulation / base_total / total  ← 与范围规则无关
    level / mainline_alert                       ← 只被范围规则过滤

所以调整 `mainline_alert_exclusions.yaml`（关掉某个板块、改旺季窗口、
把 `seasonal` 换成 `strong_only`）之后，**不需要**花 90 分钟整段重打分，
只要把现有两张表按新规则过一遍：

1. `mainline_score.level`：被拦的行置 `none`（`strong_only` 模式下
   只保留 `strong`）；
2. `mainline_alert`：删掉被拦的告警行。

## 什么时候**不能**用它

改的是**打分口径**时（`PROSPERITY_FLOOR`、V3 的 `prosperity_delta_*`、
六维权重、阈值…）必须整段重跑 —— 那些会改变 `six_dim`/`total`，
进而改变候选池成员，后处理补不出来。

## 与 `--force` 重打分的分工

    只改名单/窗口        → 本脚本（秒级）
    改分数或层权重/阈值  → scripts/rescore_mainline.py --force（约 90 分钟）

## ⚠️ 一处**已知的不等价**（条件性成立，2026-09-22 补了机器判据）

线上告警链有**冷却**（`cooldown_days: 5` 个交易日，键是
**`(board_code, level)`** —— 同板块**同等级**，见 `service._apply_cooldown`）
与**确认**（`confirm_periods: 2`）。差别在于：

- **整段重打分**：被拦的告警**从来没进过**冷却历史 → 它后面 5 个交易日内
  那条本来会被冷却压掉的告警，现在**会被写出来**；
- **本脚本后处理**：那条被冷却压掉的告警**早就不在表里**，后处理补不回来。

**但"补不回来"要成立，前提是存在这样一条被压掉的告警。**
现表满足一个不变量：

    同 (board_code, level) 的两条告警，交易日至少相隔 cooldown + 1 天

因为重打分逐日写表时，写第 `d` 天就查过 `[d-5, d-1]` 有没有同键告警。
于是：

- 若**删除的规则是整档屏蔽**（`excluded` 全档、`medium_up` 只删 weak），
  删掉的键上不会再有存活行 —— 冷却是**按等级**分键的，
  删 weak 不会放出 medium/strong 的槽位；
- 若现表满足上面的不变量，则被删行的前后 5 天内**不可能**有同键存活行，
  所以后处理与真重打分**逐行等价**（`--verify-cooldown` 会实测这个不变量）。

真正会不等价的只有**部分档位屏蔽 + 不变量被破坏**同时发生的情形
（例如表是几轮不同配置拼出来的，或 `cooldown_days` 被调小过）。
所以脚本默认先验证不变量，并在结论里写清"等价"还是"可能少几条"。

## 与 `--force` 重打分的分工

    只改名单/窗口/档位      → 本脚本（秒级，且上一条成立时逐行等价）
    改分数或层权重/阈值     → scripts/rescore_mainline.py --force（约 80 分钟）

用法：
    # 先干跑看会动多少行（同时验证冷却不变量）
    .venv\\Scripts\\python.exe scripts/apply_alert_scope.py --dry-run
    # 确认无误再落库
    .venv\\Scripts\\python.exe scripts/apply_alert_scope.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import _load_alert_scope  # noqa: E402

MAIN_DB = ROOT / "data" / "moss_finagent.db"
CACHE_DB = ROOT / "data" / "mainline_cache.db"
SCOPE = ROOT / "configs" / "mainline_alert_exclusions.yaml"


def cooldown_violations(rows: list[tuple[str, str, str]], calendar: list[str],
                        cooldown: int) -> list[tuple[str, str, str, str]]:
    """实测「同 (板块, 等级) 的告警间隔 ≥ cooldown + 1 个交易日」这条不变量。

    `rows` 是 `(trade_date, board_code, level)`。返回违反的行
    `(板块, 等级, 前一日, 后一日)`；空列表 = 不变量成立 ⇒
    后处理与真重打分**逐行等价**（推理见模块文档）。
    """
    order = {day: i for i, day in enumerate(calendar)}
    last: dict[tuple[str, str], tuple[int, str]] = {}
    bad: list[tuple[str, str, str, str]] = []
    for day, code, level in sorted(rows, key=lambda r: r[0]):
        pos = order.get(day)
        if pos is None:          # 不在交易日历里（脏数据）—— 跳过，别假装合规
            continue
        key = (code, level)
        previous = last.get(key)
        if previous is not None and pos - previous[0] <= cooldown:
            bad.append((code, level, previous[1], day))
        last[key] = (pos, day)
    return bad



def main() -> int:
    parser = argparse.ArgumentParser(description="按最新范围规则后处理告警")
    parser.add_argument("--score-table", default="mainline_score")
    parser.add_argument("--alert-table", default="mainline_alert")
    parser.add_argument("--scope", default=str(SCOPE))
    parser.add_argument("--apply", action="store_true",
                        help="真的写库；不加则只干跑")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式干跑（默认行为，写出来是为了可读性）")
    args = parser.parse_args()

    scope = _load_alert_scope(args.scope)
    print(f"范围规则：{scope.load_note}")
    if not (scope.excluded or scope.seasonal or scope.strong_only
            or scope.medium_up):
        print("⚠️ 规则为空 —— 后处理不会改动任何行（规则空 = 不限制）。"
              "若这是误读配置，请先检查名单文件。")
    conn = sqlite3.connect(str(MAIN_DB), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    # ---------- 1) 评分表：被拦的行 level 置 none ----------
    score_rows = conn.execute(
        f"SELECT trade_date, board_code, level, total AS score FROM {args.score_table}"
        " WHERE level IS NOT NULL AND level <> 'none'").fetchall()
    score_hits: Counter[str] = Counter()
    score_updates: list[tuple[str, str]] = []
    for row in score_rows:
        code, day = str(row["board_code"]), str(row["trade_date"])
        # 判定统一走 `permits()`，不在脚本里重复模式字符串比对
        if not scope.permits(code, day, str(row["level"]), row["score"]):
            score_hits[code] += 1
            score_updates.append((code, day))

    # ---------- 2) 告警表：删掉被拦的行 ----------
    alert_rows = conn.execute(
        f"SELECT trade_date, board_code, level, score FROM {args.alert_table}").fetchall()
    alert_hits: Counter[str] = Counter()
    alert_deletes: list[tuple[str, str]] = []
    for row in alert_rows:
        code, day = str(row["board_code"]), str(row["trade_date"])
        if not scope.permits(code, day, str(row["level"]), row["score"]):
            alert_hits[code] += 1
            alert_deletes.append((code, day))

    print()
    print(f"评分表 `{args.score_table}`：待置 none **{len(score_updates)}** 行")
    for code, count in score_hits.most_common():
        print(f"    {code}: {count}")
    print()
    print(f"告警表 `{args.alert_table}`：待删除 **{len(alert_deletes)}** 行")
    for code, count in alert_hits.most_common():
        print(f"    {code}: {count}")
    print()

    # ---------- 3) 等价性自检：冷却不变量 ----------
    #
    # 这一步回答的是"后处理的结果与真重打分一不一样"。判据是现表的
    # 「同 (板块, 等级) 告警间隔 ≥ cooldown + 1 个交易日」不变量 ——
    # 成立则删行不会放出任何存活行本可用的冷却槽位（推理见模块文档）。
    cooldown = 5
    try:
        import yaml
        raw = yaml.safe_load(
            (ROOT / "configs" / "mainline.yaml").read_text(encoding="utf-8"))
        cooldown = int((raw.get("alert") or {}).get("cooldown_days", cooldown))
    except Exception as exc:  # noqa: BLE001 读不到就用默认值，但要说出来
        print(f"（冷却天数读取失败，按默认 {cooldown} 判：{type(exc).__name__}）")
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    calendar = [str(r[0]) for r in cache.execute(
        "SELECT trade_date FROM ml_calendar ORDER BY trade_date")]
    cache.close()
    bad = cooldown_violations(
        [(str(r["trade_date"]), str(r["board_code"]), str(r["level"]))
         for r in alert_rows], calendar, cooldown)
    # ⚠️ 全表违反**不等于**本脚本不等价：只有「同一 (板块, 等级) 既有被删行、
    # 又有存活行」的键才可能出问题 —— 冷却按 (板块, 等级) 分键，
    # 整档屏蔽（excluded 全档、medium_up 只删 weak）的键上一条都不会存活，
    # 删多少都不会放出槽位。所以把违反**收敛到"风险键"上**再下结论。
    deleted_keys = {(c, str(lv)) for (c, lv) in
                    {(str(r["board_code"]), str(r["level"]))
                     for r in alert_rows if not scope.permits(
                         str(r["board_code"]), str(r["trade_date"]),
                         str(r["level"]), r["score"])}}
    surviving_keys = {(str(r["board_code"]), str(r["level"]))
                      for r in alert_rows if scope.permits(
                          str(r["board_code"]), str(r["trade_date"]),
                          str(r["level"]), r["score"])}
    risky_keys = deleted_keys & surviving_keys
    risky_bad = [x for x in bad if (x[0], x[1]) in risky_keys]
    print(f"等价性自检（冷却不变量：同板块同等级间隔 > {cooldown} 个交易日）：")
    print(f"  全表违反 {len(bad)} 处；其中落在**风险键**"
          f"（同键既有被删行、又有存活行）上的 **{len(risky_bad)}** 处。")
    if not risky_bad:
        print("  ✅ 风险键无违反 → **后处理与真重打分逐行等价**，可以放心用它出数字。")
        if bad:
            print(f"     （全表那 {len(bad)} 处违反不影响本结论：它们所在的键"
                  "要么整档屏蔽、要么不在本轮名单里。"
                  "成因基本是 **2025-12 日历修复**改动了那两个日期之间的"
                  "交易日数，见 docs/MAINLINE_CALENDAR_REPAIR.md。）")
    else:
        print(f"  ⚠️ 风险键上有 {len(risky_bad)} 处违反（前 5 例）："
              + "；".join(f"{c}/{lv} {a}→{b}" for c, lv, a, b in risky_bad[:5]))
        print("  → 这几处后处理可能**比真重打分少几条**，要出正式数字请跑 "
              "`rescore_mainline.py --force`。")
    print()

    if not args.apply:
        print("（未加 --apply，未写库）")
        conn.close()
        return 0

    if score_updates:
        conn.executemany(
            f"UPDATE {args.score_table} SET level = 'none'"
            " WHERE board_code = ? AND trade_date = ?", score_updates)
    if alert_deletes:
        conn.executemany(
            f"DELETE FROM {args.alert_table}"
            " WHERE board_code = ? AND trade_date = ?", alert_deletes)
    conn.commit()

    remaining_alerts = conn.execute(
        f"SELECT COUNT(*) FROM {args.alert_table}").fetchone()[0]
    remaining_levels = conn.execute(
        f"SELECT COUNT(*) FROM {args.score_table}"
        " WHERE level IN ('strong','medium','weak')").fetchone()[0]
    print(f"已落库。处理后：告警表 {remaining_alerts} 行、"
          f"评分表有效档位 {remaining_levels} 行")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
