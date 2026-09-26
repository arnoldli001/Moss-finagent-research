"""只读检查 pilot 库里 admin 账号的登录状态（锁定？失败次数？改密时间？）。

⚠️ **只读**：`mode=ro`，不改任何数据。为什么要查这个：
    实测 `admin` / `MossPilot2026x` 通过 API 登录返回 `credentials`
    （"账号或密码错误"），而本会话早前同一套凭据是能登进去的。
    在"密码被改"与"账号被锁"之间要先分清，否则会去重置一个没问题的密码。
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

c = sqlite3.connect("file:data/pilot/moss_pilot.db?mode=ro", uri=True,
                    timeout=10)
c.row_factory = sqlite3.Row

tabs = [r[0] for r in c.execute(
    "select name from sqlite_master where type='table' "
    "and (name like '%user%' or name like '%account%' or name like '%login%' "
    "or name like '%audit%')")]
print("相关表:", tabs)

for t in tabs:
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    print(f"\n=== {t} ===")
    print("  列:", cols)
    n = c.execute(f"select count(*) from {t}").fetchone()[0]
    print("  行数:", n)

# 找 admin 那一行
for t in tabs:
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    key = next((x for x in ("account", "username", "name", "user_id")
                if x in cols), None)
    if not key:
        continue
    try:
        rows = c.execute(
            f"select * from {t} where {key} in ('admin','admin@local') "
            "limit 3").fetchall()
    except sqlite3.Error:
        continue
    if not rows:
        continue
    print(f"\n=== {t} 里的 admin ===")
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        # 密码哈希只打前若干字符，不整段输出
        for k in list(d):
            if "hash" in k.lower() or "password" in k.lower():
                v = str(d[k] or "")
                d[k] = v[:14] + "…" if v else v
        print("  ", d)

c.close()
