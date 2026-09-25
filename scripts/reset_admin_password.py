#!/usr/bin/env python
"""重置**已有**账号的密码 —— `bootstrap_admin.py` 缺失的配套入口。

## 为什么需要这个脚本

`scripts/bootstrap_admin.py` 只能**新建**或**提升**，对已存在的用户名会直接
退出（`[!] 用户名已存在`），并提示改用 `--promote`。而 `--promote` 只改等级、
**不碰密码**。于是出现一个真实缺口：

    管理员忘了密码 → 界面进不去（重置密码需要管理员身份）
                  → API 用不了（需登录）
                  → CLI 只能新建/提升，不能重置
                  → **只能上服务器改库**

本脚本就是补这个洞：用**服务器访问权**重置任意账号密码，
与 `bootstrap_admin.py` 同一层级、同一套留痕口径。

## 为什么用脚本而不是"让后端支持"

后端**已有** `POST /admin/users/{user_id}/reset-password`（见 `routes/admin.py:567`），
但那条路需要**先用管理员身份登录** —— 正是我们做不到的事。
所以这不是"功能缺失"，而是**引导路径**（bootstrap path），
必须由持有服务器访问权的人执行，不能从界面产生。

## 用法

    # 看有哪些账号（只读）
    .venv/Scripts/python.exe scripts/reset_admin_password.py --list

    # 重置为随机强密码（打印一次）
    .venv/Scripts/python.exe scripts/reset_admin_password.py --username admin

    # 指定密码
    .venv/Scripts/python.exe scripts/reset_admin_password.py --username admin \\
        --password 'li12345671'

    # ⚠️ 必须指定到**你正在用的那个库**（见下方"目标库"）

## 目标库（本脚本最容易踩的坑）

本机实测存在**两个各自有 admin 的库**：

    data/dev/moss_dev.db          1 管理员   ← manage.py start 默认走这里
    data/pilot/moss_pilot.db      1 管理员
    data/moss_finagent.db         0 用户

`manage.py start` 默认起的是 **dev 隔离实例**，所以浏览器登录的是
`data/dev/moss_dev.db`。**不加 `--db` 时本脚本读 `MOSS_ENV`/`MOSS_SQLITE_PATH`**，
可能与你在用的那个不一致 —— 表现为"我明明重置了，还是登不上"。
**先看脚本第一行打印的库路径和体积，核对清楚再操作。**
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 项目根。本文件在 `<root>/scripts/` 下，上溯一层即根。
ROOT = Path(__file__).resolve().parents[1]

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.infrastructure.repositories.auth_sqlite_repo import (  # noqa: E402
    AuthSqliteRepository,
)

#: 重置后保留的密码历史条数。与 `set_password` 默认值一致（§8.6.5⑤）。
KEEP_HISTORY = 5


def gen_password() -> str:
    """生成一个**必定通过强度校验**的密码。

    强度规则要求"大小写字母 + 数字 + 符号中的至少三类"，
    所以每类各取一个再补足，而不是随机拼 16 位后指望它恰好满足。
    """
    pools = [string.ascii_uppercase, string.ascii_lowercase,
             string.digits, "!@#$%^&*"]
    chars = [secrets.choice(p) for p in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(12)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def fingerprint(db_path: str) -> str:
    """把人能核对的库信息打出来（**本脚本最重要的一行输出**）。"""
    p = Path(db_path)
    if not p.exists():
        return f"{p.resolve()}  (⚠️ 不存在，将新建 —— 多半选错了库)"
    size = float(p.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{p.resolve()}  ({size:.1f} {unit}, 已存在)"
        size /= 1024
    return f"{p.resolve()}  (已存在)"


def main() -> int:
    ap = argparse.ArgumentParser(description="重置已有账号的密码（引导入口）")
    ap.add_argument("--username", default="", help="要重置的账号")
    ap.add_argument("--password", default="", help="新密码（缺省则随机生成）")
    ap.add_argument("--db", default="", help="SQLite 路径（缺省按环境变量）")
    ap.add_argument("--list", action="store_true", help="只列出账号，不重置")
    ap.add_argument("--keep-must-change", action="store_true",
                    help="保持『首登强制改密』（默认关闭，避免管理员再次登不上）")
    ap.add_argument("--clip", action="store_true",
                    help="把新密码复制到剪贴板（推荐：无需手工抄写，避免 O/0、l/1 看错）")
    ap.add_argument("--one-line", action="store_true",
                    help="把凭据打成一行（防终端折行导致抄错）")
    ap.add_argument("--expect-db", default="",
                    help="安全保险：若目标库路径不含该子串则拒绝执行（防改错库）")
    args = ap.parse_args()

    from src.core.config import get_settings

    db_path = args.db or get_settings().sqlite_path

    # ── 保险：改错库是这条路径唯一会"白忙一场"的失败模式 ──
    if args.expect_db and args.expect_db not in str(Path(db_path).as_posix()):
        print(f"[x] 拒绝执行：目标库不包含 {args.expect_db!r}")
        print(f"    实际目标：{Path(db_path).resolve()}")
        print("    （本机有多个库各带一个 admin，改错库表现为『重置了却登不上』）")
        return 2

    print(f"目标库：{fingerprint(db_path)}")
    print(f"（env={get_settings().env}；"
          f"manage.py start 默认走 data/dev/moss_dev.db，需加 --db 才对得上）")
    print()

    repo = AuthSqliteRepository(db_path)
    repo.ensure_schema()

    users = repo.list_users()
    if args.list or not args.username:
        print(f"共 {len(users)} 个账号：")
        for u in sorted(users, key=lambda x: (x.applied_tier != "admin", x.username)):
            mark = "★" if u.applied_tier == "admin" else " "
            print(f" {mark} {u.username:<20} tier={u.applied_tier or '(空)':<8} "
                  f"status={u.status:<9} 到期={u.valid_until[:10] or '-'}")
        if not users and not args.db:
            # 实测踩到的误导：不加 --db 时读到 data/moss_finagent.db（6.3 GB 但是空库），
            # 打印"0 个账号"看起来像"还没建账号"，实际是**读错库**。
            # 本机有三个库，只有指定对了才看得到真实账号。
            print()
            print("  ⚠️ 结果是 0 —— 大概率是【读错库】，不是真的没有账号。")
            print(f"     本次目标：{Path(db_path).resolve()}")
            print("     本机已知的库（只有指定对了才看得到账号）：")
            for rel, note in (
                ("data/pilot/moss_pilot.db", "对外试点 · 正在对外服务的就是它"),
                ("data/dev/moss_dev.db", "本地 dev 实例"),
                ("data/moss_finagent.db", "遗留空库 · 就是默认读到的这个"),
            ):
                p = ROOT / rel
                mark = "←" if p.resolve() == Path(db_path).resolve() else " "
                print(f"       {mark} {rel:<30} {note}")
            print()
            print("     正确写法：--db data/pilot/moss_pilot.db")
        if not args.username:
            print("\n请用 --username 指定要重置的账号。")
        return 0

    user = repo.get_user_by_username(args.username)
    if user is None:
        print(f"[x] 账号不存在：{args.username}（用 --list 看全部）")
        return 1

    from src.domain.auth.service import check_password_strength

    password = args.password or gen_password()
    ok, why = check_password_strength(password, username=user.username)
    if not ok:
        print(f"[x] 密码不符合要求：{why}")
        return 1

    # 「禁止改回旧密码」不应把重置路径堵死：管理员忘了密码时无从判断是否重复。
    # 这里只提示，不阻断 —— 随机生成的密码本来也几乎不可能命中历史。
    try:
        if repo.password_was_used(user.user_id, password):
            print("[!] 该密码在最近 5 次历史中使用过，建议换一个。")
    except Exception:  # noqa: BLE001 历史检查失败不应阻断救援路径
        pass

    # keep_must_change 默认 False：救急场景下若强制改密，
    # 管理员可能再次陷入"进不去"；需要更严就显式加 --keep-must-change。
    must_change = bool(args.keep_must_change)

    repo.set_password(user.user_id, password,
                      keep_history=KEEP_HISTORY, must_change=must_change)
    # 重置后踢掉全部设备：怀疑被盗号时这也是必要动作（与 API 端点口径一致）
    revoked = 0
    try:
        revoked = repo.revoke_user_sessions(user.user_id, "cli:reset_password")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 会话吊销失败（不影响改密）：{e}")

    # 留痕：与 bootstrap/API 两条路径同一张审计表
    try:
        repo._record_review(  # noqa: SLF001 引导动作，留痕
            user_id=user.user_id, action="cli:reset_password",
            from_status=user.status, to_status=user.status,
            reviewer_id="cli",
            tier_code=user.applied_tier,
            valid_until=user.valid_until,
            note="scripts/reset_admin_password.py 本地重置密码")
    except Exception as e:  # noqa: BLE001 留痕失败不回滚改密
        print(f"[!] 留痕失败（密码已改）：{e}")

    print("[✓] 密码已重置")
    print(f"    账号　：{user.username}（{user.user_id}）")
    print(f"    等级　：{user.applied_tier or '(空)'}   状态：{user.status}")
    print(f"    已吊销会话：{revoked} 个")
    print(f"    首登强制改密：{'是' if must_change else '否'}")

    # ── 交付密码：默认剪贴板 ──
    #  不写文件是刻意的：落盘即长期明文，而剪贴板用完即走。
    #  （Windows 剪贴板对 # $ & ! 等字符实测完全保真，无转义问题。）
    copied = False
    if args.clip or not args.password:
        try:
            import subprocess
            subprocess.run("clip", input=password.encode("utf-16le"),
                           check=True, shell=False)
            copied = True
        except Exception as e:  # noqa: BLE001 剪贴板失败不应让改密算失败
            print(f"[!] 复制到剪贴板失败：{e}")

    if args.one_line:
        print()
        print(f"    {user.username}\t{password}")
    else:
        print(f"    新密码：{password}")

    print()
    if copied:
        print("  ✅ 新密码【已复制到剪贴板】—— 现在直接粘贴进密码管理器，不要手抄。")
    else:
        print("  ⚠️ 新密码只显示这一次；请立刻选中复制，关掉终端就没了。")
    print("  ⚠️ 本脚本【不落盘】任何明文密码（写文件等于长期留一份明文）。")
    print()
    print("  下一步（三选一）：")
    print("    ① 浏览器自动记：登录 http://127.0.0.1:%d/ ，"
          "Edge 会弹出『保存密码』——点保存即可" % get_settings().api_port)
    print("    ② 手工入库：打开密码管理器 → 新建条目 → 粘贴")
    print("       标题=Moss 投研管理台   用户名=%s   地址=http://127.0.0.1:%d/"
          % (user.username, get_settings().api_port))
    print("    ③ 首次登录后系统会强制改密，改完再存最终密码（推荐）")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
