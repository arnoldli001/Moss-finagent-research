#!/usr/bin/env python
"""创建/提升一个管理员账号 —— 管理台的引导入口。

## 为什么必须有这个脚本（鸡生蛋问题）

本系统的管理台门槛是 `applied_tier == 'admin'`，而**新注册用户一律落 `pending`
且等级为空**：注册流本身就需要管理员审批。所以第一个管理员**不可能**通过界面产生
—— 必须由部署者用服务器访问权显式创建。这不是设计缺陷，而是刻意的：
"能自己把自己变成管理员"的界面本身就是最大的越权漏洞。

## 用法

    # 新建管理员（自动生成初始密码并打印一次，首次登录强制改密）
    .venv/Scripts/python.exe scripts/bootstrap_admin.py --username admin \\
        --email you@example.com

    # 指定密码（仍强制首登改密）
    .venv/Scripts/python.exe scripts/bootstrap_admin.py --username admin \\
        --email you@example.com --password 'YourStrong#Pass2026'

    # 把**已有**用户提升为管理员（如审批完自己后想再给同事权限）
    .venv/Scripts/python.exe scripts/bootstrap_admin.py --promote existing_user

    # 查看当前有哪些管理员
    .venv/Scripts/python.exe scripts/bootstrap_admin.py --list

## 目标库

⚠️ 默认按当前 `MOSS_ENV` / `MOSS_SQLITE_PATH` 决定，与 `manage.py start` 一致。
`manage.py start` 默认走 **dev 隔离实例**（`data/dev/moss_dev.db`），
所以要建到"你正在用的那个实例"上，请用同样的方式启动本脚本：

    # 与 manage.py start（默认 dev 隔离）一致
    .venv/Scripts/python.exe scripts/bootstrap_admin.py --username admin

    # 明确指定某个库
    .venv/Scripts/python.exe scripts/bootstrap_admin.py --db data/moss_finagent.db \\
        --username admin
"""

from __future__ import annotations

import argparse
import os
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.infrastructure.repositories.auth_sqlite_repo import (  # noqa: E402
    AuthSqliteRepository,
)


def gen_password() -> str:
    """生成一个**必定通过强度校验**的初始密码。

    强度规则要求"大小写字母 + 数字 + 符号中的至少三类"，所以这里每类都取，
    而不是随机拼 16 位后祈祷它恰好满足（实测随机串有一定概率只有两类）。
    """
    pools = [string.ascii_uppercase, string.ascii_lowercase,
             string.digits, "!@#$%^&*"]
    chars = [secrets.choice(p) for p in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(12)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def fingerprint(db_path: str) -> str:
    """把库路径显示成人能核对的形式（**必须让用户看清写进了哪个库**）。

    ⚠️ 这是本脚本最重要的一行输出。实测踩过：服务跑在 dev 隔离库上，
    而迁移/建号脚本默认写到生产库 —— 两边都"成功"，但互相看不见，
    表现为"我明明建了管理员，登录却说账号不存在"，排查很久。
    """
    p = Path(db_path)
    if not p.exists():
        return f"{p.resolve()}  (将新建)"
    size = float(p.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            # 旧的 KB 写法在 6.8 GB 库上显示成 "6966448 KB"，人要数零才能读懂
            return f"{p.resolve()}  ({size:.1f} {unit}, 已存在)"
        size /= 1024
    return f"{p.resolve()}  (已存在)"


def main() -> int:
    ap = argparse.ArgumentParser(description="创建/提升管理员账号")
    ap.add_argument("--username", default="", help="新建管理员的用户名")
    ap.add_argument("--email", default="", help="邮箱（用于找回密码）")
    ap.add_argument("--password", default="", help="初始密码（缺省则随机生成）")
    ap.add_argument("--display-name", default="", help="显示名")
    ap.add_argument("--promote", default="", help="把已有用户名提升为管理员")
    ap.add_argument("--list", action="store_true", help="只列出当前管理员")
    ap.add_argument("--db", default="", help="SQLite 路径（缺省按环境变量）")
    ap.add_argument("--days", type=int, default=3650,
                    help="有效期天数（默认 3650 ≈ 10 年）")
    args = ap.parse_args()

    from src.core.config import get_settings

    db_path = args.db or get_settings().sqlite_path
    print(f"目标库：{fingerprint(db_path)}")
    print(f"（env={get_settings().env}；如需写 dev 隔离库请加 --db data/dev/moss_dev.db）")
    print()

    repo = AuthSqliteRepository(db_path)
    repo.ensure_schema()

    admins = [u for u in repo.list_users() if u.applied_tier == "admin"]
    if args.list:
        print(f"当前管理员 {len(admins)} 个：")
        for u in admins:
            print(f"  {u.user_id:<26} {u.username:<20} status={u.status:<9} "
                  f"到期={u.valid_until[:10] or '-'}")
        return 0

    if args.promote:
        user = repo.get_user_by_username(args.promote)
        if user is None:
            print(f"[x] 用户不存在：{args.promote}")
            return 1
        repo.set_user_tier_and_validity(
            user.user_id, tier="admin", operator="bootstrap",
            note="scripts/bootstrap_admin.py 提升为管理员")
        if user.status != "active":
            repo.update_user_status(
                user.user_id, "active", reviewed_by="bootstrap",
                review_note="提升为管理员时一并启用", operator="bootstrap",
                action="bootstrap:promote")
        print(f"[✓] 已把 {user.username}（{user.user_id}）提升为管理员")
        return 0

    if not args.username:
        print("[x] 请给出 --username，或用 --promote / --list")
        return 2

    existing = repo.get_user_by_username(args.username)
    if existing is not None:
        print(f"[!] 用户名 {args.username} 已存在（{existing.user_id}，"
              f"tier={existing.applied_tier}）。")
        print(f"    要提升它为管理员请用：--promote {args.username}")
        return 1

    from src.domain.auth.service import check_password_strength
    from src.infrastructure.repositories.auth_sqlite_repo import (
        hash_password,
        iso,
        utc_now,
    )

    password = args.password or gen_password()
    ok, why = check_password_strength(password, username=args.username)
    if not ok:
        print(f"[x] 密码不符合要求：{why}")
        return 1

    user_id = f"u_{secrets.token_hex(8)}"
    from datetime import datetime, timedelta

    valid_until = (datetime.now().astimezone()
                   + timedelta(days=args.days)).isoformat(timespec="seconds")
    repo.create_user(
        user_id=user_id, username=args.username,
        display_name=args.display_name or args.username,
        status="active", valid_until=valid_until, applied_tier="admin")
    repo.set_password(user_id, password, must_change=True)
    if args.email:
        repo.bind_contact(user_id=user_id, kind="email", value=args.email,
                          verified=True)
    repo._record_review(  # noqa: SLF001 引导动作，留痕
        user_id=user_id, action="bootstrap:admin", from_status="",
        to_status="active", reviewer_id="bootstrap", tier_code="admin",
        valid_until=valid_until,
        note="scripts/bootstrap_admin.py 创建首个管理员")
    assert hash_password  # 保持导入被使用（hash 由 set_password 内部完成）
    assert iso and utc_now

    print(f"[✓] 管理员已创建")
    print(f"    用户名：{args.username}")
    print(f"    密码　：{password}")
    print(f"    user_id：{user_id}")
    print(f"    有效期：{valid_until[:10]}（{args.days} 天）")
    print()
    print("  ⚠️ 这个密码只显示这一次，请立刻保存。")
    print("  ⚠️ 首次登录会强制修改密码（初始密码由脚本生成，不等于只有你知道）。")
    print()
    print(f"  登录地址：http://127.0.0.1:{get_settings().api_port}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
