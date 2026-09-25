# -*- coding: utf-8 -*-
"""只读：列出各数据库里的管理员账号（不碰密码，不做任何写操作）。

用法：
    .venv/Scripts/python.exe scripts/_list_admins_ro.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.infrastructure.repositories.auth_sqlite_repo import (  # noqa: E402
    AuthSqliteRepository,
)

CANDIDATES = [
    "data/dev/moss_dev.db",
    "data/moss_finagent.db",
    "data/pilot/moss_pilot.db",
]


def size_of(p: Path) -> str:
    if not p.exists():
        return "不存在"
    n = float(p.stat().st_size)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}"
        n /= 1024
    return "?"


def main() -> int:
    print(f"{'库':<34}{'大小':>11}  管理员")
    print("-" * 78)
    total = 0
    for rel in CANDIDATES:
        p = ROOT / rel
        if not p.exists():
            print(f"{rel:<34}{'—':>11}  （不存在）")
            continue
        try:
            repo = AuthSqliteRepository(str(p))
            users = repo.list_users()
        except Exception as e:
            print(f"{rel:<34}{size_of(p):>11}  [读取失败] {type(e).__name__}: {str(e)[:60]}")
            continue
        admins = [u for u in users if u.applied_tier == "admin"]
        total += len(admins)
        print(f"{rel:<34}{size_of(p):>11}  共 {len(users)} 用户 / {len(admins)} 管理员")
        for u in admins:
            must = getattr(u, "must_change_password", None)
            print(f"{'':<34}{'':>11}    · {u.username:<18} status={u.status:<9} "
                  f"到期={u.valid_until[:10] or '-'}"
                  + (f"  首登需改密={must}" if must is not None else ""))
    print("-" * 78)
    print(f"合计管理员：{total}")
    print("\n注意：密码为单向哈希（argon2id/bcrypt/pbkdf2_sha256），无法还原。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
