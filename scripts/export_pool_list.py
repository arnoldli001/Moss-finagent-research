"""导出「当前主线监控概念清单」——219 个在监控 + 105 个已剔除。

用户问：「重打分为什么还有 719 个？没这么多概念要监控了吧，请列出来当前
剩余要监控的概念清单」。

**719 是交易日数，不是概念数**（`rescore_mainline.py` 按交易日逐天重算，
进度行 `[100/719] 20240305 板块 199 告警 5` 里 719=交易日、199=当天有分数的
板块数）。概念数由 `ml_board` 决定。

本脚本把两份名单导出到文档，供人工核对。只读。
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
OUT = ROOT / "docs" / "MAINLINE_POOL_CURRENT.md"


def main() -> int:
    import yaml

    from src.mainline.config import _load_alert_scope

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    pool = [dict(r) for r in cache.execute(
        "SELECT code, name FROM ml_board ORDER BY code")]
    # 用**最早的**那份备份当"剔除前"的全集：每批次都会另存一份，
    # 取最早的那份才包含所有被剔板块。
    backups = [str(r[0]) for r in cache.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name LIKE 'ml_board_bak_pre%' ORDER BY name")]
    full: dict[str, str] = {}
    if backups:
        full = {str(r["code"]): str(r["name"]) for r in
                cache.execute(f"SELECT code, name FROM {backups[0]}")}
    days = cache.execute(
        "SELECT COUNT(*) FROM ml_calendar WHERE trade_date BETWEEN"
        " '20231009' AND '20260918'").fetchone()[0]
    cache.close()
    scope = _load_alert_scope(
        str(ROOT / "configs" / "mainline_alert_exclusions.yaml"))
    kept = {str(r["code"]) for r in pool}
    removed = sorted(({"code": c, "name": n} for c, n in full.items()
                      if c not in kept), key=lambda r: r["code"])

    # 批次归属：从台账里反查每个代码是哪一批剔的
    batches_raw = yaml.safe_load(
        (ROOT / "configs" / "concept_removal_batches.yaml")
        .read_text(encoding="utf-8")) or {}
    owner: dict[str, int] = {}
    for item in (batches_raw.get("batches") or []):
        for token in re.findall(r"(\d{6})", str(item.get("raw") or "")):
            owner.setdefault(f"{token}.TI", int(item["id"]))

    lines: list[str] = []
    lines.append("# 当前主线监控概念清单（`ml_board`）\n")
    lines.append(f"> 由 `scripts/export_pool_list.py` 生成（只读，"
                 f"{datetime.now().astimezone().isoformat(timespec='seconds')}）。\n")
    lines.append(f"- **在监控：{len(pool)} 个概念**")
    lines.append(f"- 池级已剔除：{len(removed)} 个概念"
                 f"（批次台账 `configs/concept_removal_batches.yaml`）")
    lines.append(f"- 回测区间 20231009~20260918 共 **{days} 个交易日** "
                 f"—— `rescore_mainline.py` 的 `[N/{days}]` 是**交易日进度**，"
                 f"不是概念数\n")
    lines.append("## ⚠️ 三个数字别混\n")
    lines.append("| 数字 | 含义 | 现在 |")
    lines.append("|---|---|---:|")
    lines.append(f"| 719 | 回测区间的**交易日**数（`[N/719]` 的进度分母） | {days} |")
    lines.append("| 板块 199 | 某一天**有分数**的板块数（新概念上市前不计） "
                 "| 189→219 |")
    lines.append(f"| 概念 219 | **监控范围**（`ml_board` 行数） | {len(pool)} |")
    lines.append("")
    lines.append("进度行长这样，三个数各就各位：\n")
    lines.append("```")
    lines.append("  [100/719] 20240305 板块 199 告警 5 | 已用 10.2 分，预计还需 63.4 分")
    lines.append("   ↑交易日进度   ↑日期    ↑当天板块  ↑当天告警")
    lines.append("```\n")
    lines.append("同一板块在回测早期还没上市（`list_date` 之后才计入），"
                 "所以「当天板块数」会从 189 慢慢涨到 219 —— 这是**正常**的，"
                 "不是漏算。\n")

    lines.append(f"## 一、当前监控的 {len(pool)} 个概念\n")
    lines.append("| # | 概念 | 代码 | 备注 |")
    lines.append("|---:|---|---|---|")
    for index, row in enumerate(pool, 1):
        code = str(row["code"])
        mode, why = scope.restriction(code, "20260918")
        note = ""
        if mode == "block":
            note = "🚫 不发告警（告警级关闭）"
        elif mode == "medium_up":
            note = "🟡 只留中强信号"
        elif code in scope.strong_only:
            note = "🔴 旺季外只发强信号"
        elif code in scope.seasonal:
            note = "📅 季节性（仅旺季发）"
        lines.append(f"| {index} | {row['name']} | {code.replace('.TI', '')} "
                     f"| {note} |")
    lines.append("")
    lines.append("> 「🚫 不发告警」的板块**仍在监控池内**（照常打分、照常有分数行），"
                 "只是 `_decide_level` 不为它产生告警 —— 这是**告警级**剔除。"
                 "要彻底移出监控，改 `configs/sector_blacklist.yaml`。\n")

    lines.append(f"## 二、池级已剔除的 {len(removed)} 个概念\n")
    lines.append("不在 `ml_board` 里 ⇒ 不参与打分、停止拥挤度日更、"
                 "不算拥挤度指标、不产生告警、前端看板不展示。\n")
    lines.append("| # | 概念 | 代码 | 批次 |")
    lines.append("|---:|---|---|---:|")
    for index, row in enumerate(removed, 1):
        code = str(row["code"])
        lines.append(f"| {index} | {row['name']} | {code.replace('.TI', '')} "
                     f"| {owner.get(code, '—')} |")
    lines.append("")
    lines.append("> 回滚：删掉 `configs/sector_blacklist.yaml` 里对应批次那一段，"
                 "再跑一次 `import_crowding_pool()`。"
                 "`ml_board_bar` / `ml_member` / `ml_member_pure` 从未被删，"
                 "数据不缺；个股与概念是两张表，**删概念不动个股**。\n")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写出 {OUT}（监控 {len(pool)} 个 / 已剔 {len(removed)} 个 / "
          f"区间 {days} 个交易日）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
