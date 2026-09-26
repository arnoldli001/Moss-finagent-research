#!/usr/bin/env python
"""把旧 `dim_intraday_profile`（`code` 单主键）迁移到 v2 多用户口径。

## 为什么必须显式指定归属人

旧表的主键是 **`code` 一列** —— 一行数据在结构上就**没有"属于谁"这个字段**。
所以迁移时唯一能确定归属的办法是**人工指定**：线上这套数据此前只有
一个使用者（管理员/所有者），把他作为 `--user-id` 传进来即可。

脚本**不会猜**，也**不会**默认迁移给所有人 ——
"猜一个归属人"等于凭空给某人一份别人的调参记录。

## 行为

| 旧表列 | 落点 |
|---|---|
| `weights_json` / `thresholds_json` / `levels_json` | v2 `mode='intraday'` |
| `daily_weights_json` / `daily_thresholds_json` | v2 `mode='daily'`（独立一行） |
| `character_profile_json` | v2 `character_json` |
| `template` / `visibility` | v2 同名列 |
| `boards_json` / `overseas_json` | v2 `boards_json` / `overseas_json`（随同一行） |
| `name` | v2 `name`（回填，避免自选列表出现"只有代码没有名字"） |

默认 **dry-run**：只打印将要写入什么，一个字节都不改库。
确认无误后再加 `--apply`。

用法：
    # 1) 先看会发生什么（不改库）
    .venv/Scripts/python.exe scripts/migrate_intraday_profile_to_v2.py \\
        --tenant-id t_default --user-id admin@example.com

    # 2) 确认后真正写入
    .venv/Scripts/python.exe scripts/migrate_intraday_profile_to_v2.py \\
        --tenant-id t_default --user-id admin@example.com --apply

幂等：v2 用 `ON CONFLICT DO UPDATE`，重复跑只会覆盖同样的值，
不会产生第二行，也不会把用户后来改过的值"迁移回去"
（`--skip-existing` 可让已存在的行完全不动）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ★ Windows 控制台默认是 GBK（cp936），打印 "✓" 会直接抛
#   `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`。
#   而这个异常发生在**写库之后、收尾打印时** —— 用户会看到"脚本崩了"，
#   却不知道数据其实已经迁完。所以这里先把 stdout 切成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # 被重定向到不支持的对象时忽略
        pass

from src.infrastructure.repositories.intraday_profile_sqlite_repo import (  # noqa: E402
    TABLE as LEGACY_TABLE,
)
from src.infrastructure.repositories.user_pool_sqlite_repo import (  # noqa: E402
    TABLE_PROFILE,
    PoolValidationError,
    UserPoolSqliteRepository,
    normalize_code,
)


def _loads(raw: object, default):
    if not raw:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return default


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def main() -> int:
    parser = argparse.ArgumentParser(description="旧口径表 → v2 多用户表迁移")
    parser.add_argument("--db", default="data/moss_finagent.db",
                        help="SQLite 路径（默认 data/moss_finagent.db）")
    parser.add_argument("--tenant-id", required=True, help="归属租户")
    parser.add_argument("--user-id", required=True,
                        help="归属用户（旧数据没有归属人，必须显式指定）")
    parser.add_argument("--apply", action="store_true",
                        help="真正写入；缺省为 dry-run")
    parser.add_argument("--skip-existing", action="store_true",
                        help="用户已调过的口径完全不覆盖")
    args = parser.parse_args()

    db_path = args.db
    if not Path(db_path).exists():
        print(f"[x] 数据库不存在：{db_path}")
        return 2

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if LEGACY_TABLE not in tables:
            print(f"[i] 没有 {LEGACY_TABLE} 表，无需迁移。")
            return 0
        columns = _existing_columns(conn, LEGACY_TABLE)
        rows = conn.execute(f"SELECT * FROM {LEGACY_TABLE}").fetchall()

    if not rows:
        print(f"[i] {LEGACY_TABLE} 是空表，无需迁移。")
        return 0

    def col(row: sqlite3.Row, name: str, default=""):
        return row[name] if name in columns else default

    print(f"[i] 库：{db_path}")
    print(f"[i] 旧表 {LEGACY_TABLE} 有 {len(rows)} 行；"
          f"归属 tenant={args.tenant_id} user={args.user_id}")
    print(f"[i] 模式：{'APPLY（写库）' if args.apply else 'DRY-RUN（不写库）'}"
          f"{'，跳过已存在口径' if args.skip_existing else ''}")
    print()

    repo = None
    if args.apply:
        repo = UserPoolSqliteRepository(db_path)
        repo.ensure_schema()

    written = skipped = 0
    for row in rows:
        raw_code = str(row["code"]).strip()
        try:
            code = normalize_code(raw_code)
        except PoolValidationError as exc:
            # 旧库里的脏代码（非 6 位 / 北交所）：报出来但**不**擅自补零改写，
            # 因为 `zfill` 会把 "519" 变成 "000519"（一只毫不相干的票）。
            print(f"  [!!] 跳过非法代码 {raw_code!r}：{exc.message}")
            skipped += 1
            continue
        name = str(col(row, "name", "") or "")
        template = str(col(row, "template", "") or "")
        visibility = str(col(row, "visibility", "private") or "private")
        character = _loads(col(row, "character_profile_json", "{}"), {})
        boards = _loads(col(row, "boards_json", "[]"), [])
        overseas = _loads(col(row, "overseas_json", "[]"), [])

        # intraday 与 daily 在旧表里是同一行的两组列 → v2 拆成两行（mode 是主键的一部分）
        plans = [("intraday",
                  _loads(col(row, "weights_json", "{}"), {}),
                  _loads(col(row, "thresholds_json", "{}"), {}),
                  _loads(col(row, "levels_json", "{}"), {}))]
        daily_w = _loads(col(row, "daily_weights_json", "{}"), {})
        daily_t = _loads(col(row, "daily_thresholds_json", "{}"), {})
        if daily_w or daily_t:
            plans.append(("daily", daily_w, daily_t, {}))

        # ★ "空口径"不等于"没数据"（线上真实数据踩到过）：
        #   实测有一行 `002409`，权重/阈值/档位**全空**，但
        #   `boards_json=["存储芯片"]` 非空 —— 那是用户手工维护的关联板块。
        #   若按"空口径就跳过"处理，这条**板块绑定会被静默丢掉**，
        #   表现为"迁移完，加自选时板块联想不出来了"。
        #   所以：只要这一行**承载了任何用户信息**，就必须迁。
        has_payload = bool(boards or overseas or name or template or character)

        for mode, weights, thresholds, levels in plans:
            # daily 行不携带板块/名称（它们与 mode 无关，只该落在 intraday 行上）
            payload_here = has_payload and mode == "intraday"
            if not any((weights, thresholds, levels)) and not payload_here:
                continue   # 真正空的一行：没有任何信息可迁
            already = (repo is not None
                       and repo.effective_profile(tenant_id=args.tenant_id,
                                                  user_id=args.user_id,
                                                  code=code,
                                                  mode=mode).from_user)
            if already and args.skip_existing:
                skipped += 1
                print(f"  [skip] {code} {name} mode={mode}（用户已调过）")
                continue
            tag = "覆盖" if already else "新增"
            note = ""
            if not any((weights, thresholds, levels)):
                note = "  ← 权重为空，仅为保住板块/海外绑定而迁"
            print(f"  [{tag}] {code} {name or '(无名)'} mode={mode} "
                  f"权重={len(weights)} 阈值={len(thresholds)} "
                  f"档位={len(levels)} 板块={len(boards)} 海外={len(overseas)}"
                  f"{note}")
            if repo is not None:
                repo.save_profile(
                    tenant_id=args.tenant_id, user_id=args.user_id,
                    code=code, mode=mode, weights=weights,
                    thresholds=thresholds, levels=levels,
                    character=character if mode == "intraday" else {},
                    template=template, visibility=visibility,
                    name=name, boards=boards, overseas=overseas)
            written += 1

    print()
    if args.apply:
        print(f"[✓] 已写入/更新 {written} 行，跳过 {skipped} 行。"
              f"旧表 {LEGACY_TABLE} **未改动**（回滚只需丢弃 v2 表）。")
    else:
        print(f"[i] DRY-RUN：将写入/更新 {written} 行，跳过 {skipped} 行。"
              f"确认后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
