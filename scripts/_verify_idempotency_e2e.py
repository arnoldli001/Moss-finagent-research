"""端到端验证「写请求重试不会开出两个账号」（真实 HTTP + Cookie 罐）。

## 为什么必须走真实 HTTP，而不是只跑单元测试

单元测试证明的是 `IdempotencyStore` 自己的语义；这里证明的是**接线对了**：
  浏览器 → `X-Idempotency-Key` 头 → FastAPI 参数 → 路由里的 claim/get/put
  → 真的只有一个 `dim_user` 行。

链路上任何一环断掉（头名字拼错、忘记 release、键没按主体命名），
单测都还是绿的。这是本项目反复踩过的那类"单测全过、功能不工作"。

## 复现的真实故障

管理员点「直接开号 → 创建」→ 浏览器报 `Failed to fetch` → 反复重点。
服务端日志里**看不到**这些失败请求（请求没到达 / 连接被关），
所以真实原因（uvicorn 默认 5 秒 keep-alive 与浏览器连接复用竞态）
完全不可见。修法见 `manage.py` 里的 `--timeout-keep-alive 65`；
本脚本验证配套的"重试安全"这一半。

用法：
    .venv\\Scripts\\python.exe scripts/_verify_idempotency_e2e.py
"""

from __future__ import annotations

import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import httpx  # noqa: E402

from src.infrastructure.repositories.auth_sqlite_repo import (  # noqa: E402
    AuthSqliteRepository,
)

BASE = "http://127.0.0.1:8100"
DB = "data/dev/moss_dev.db"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    suffix = secrets.token_hex(4)
    admin_name = f"idem_{suffix}"
    admin_pwd = "TmpProbe#2026x"
    admin_id = f"u_{suffix}"

    repo = AuthSqliteRepository(DB)
    repo.ensure_schema()
    repo.create_user(user_id=admin_id, username=admin_name, display_name="幂等验证",
                     status="active", applied_tier="admin",
                     valid_until=(datetime.now().astimezone()
                                  + timedelta(days=1)).isoformat(timespec="seconds"))
    repo.set_password(admin_id, admin_pwd)

    created: list[str] = []
    try:
        with httpx.Client(base_url=BASE, timeout=30.0) as c:
            r = c.post("/api/v1/auth/login", json={
                "account": admin_name, "password": admin_pwd, "remember_me": False})
            check("临时管理员登录", r.status_code == 200, f"HTTP {r.status_code}")
            if r.status_code != 200:
                return 1

            print()
            print("=== 1. 同一个幂等键重复提交（= 前端自动重试）===")
            key = f"e2e-{suffix}-0001"
            body = {"username": f"idem_a_{suffix}", "password": "Created#2026x",
                    "tier": "trial", "days": 7, "must_change_password": False}
            r1 = c.post("/api/v1/admin/users", json=body,
                        headers={"X-Idempotency-Key": key})
            r2 = c.post("/api/v1/admin/users", json=body,
                        headers={"X-Idempotency-Key": key})
            check("第一次提交 200", r1.status_code == 200,
                  f"HTTP {r1.status_code} {r1.text[:120]}")
            check("重试提交 200（不是 409）", r2.status_code == 200,
                  f"HTTP {r2.status_code} {r2.text[:120]}")
            if r1.status_code == 200 and r2.status_code == 200:
                u1, u2 = r1.json()["user"], r2.json()["user"]
                created.append(u1["user_id"])
                check("两次返回同一个 user_id", u1["user_id"] == u2["user_id"],
                      f"{u1['user_id']} vs {u2['user_id']}")
                check("初始密码逐字一致（回放而非重跑）",
                      r1.json()["initial_password"] == r2.json()["initial_password"])

            print()
            print("=== 2. 库里确实只有一个该用户名的账号 ===")
            listed = c.get("/api/v1/admin/users").json()["users"]
            same = [u for u in listed if u["username"] == body["username"]]
            check("同名账号数量 == 1", len(same) == 1, f"实际 {len(same)} 个")

            print()
            print("=== 3. 换一个键 = 一次新的用户意图，必须真的再建一个 ===")
            body2 = {**body, "username": f"idem_b_{suffix}"}
            r3 = c.post("/api/v1/admin/users", json=body2,
                        headers={"X-Idempotency-Key": f"e2e-{suffix}-0002"})
            check("新键提交 200", r3.status_code == 200,
                  f"HTTP {r3.status_code} {r3.text[:120]}")
            if r3.status_code == 200:
                created.append(r3.json()["user"]["user_id"])
                check("新键拿到了不同的 user_id",
                      r3.json()["user"]["user_id"] != created[0])

            print()
            print("=== 4. 不带键时行为不变（向后兼容）===")
            plain = {"username": f"idem_c_{suffix}", "password": "Created#2026x",
                     "tier": "trial", "days": 7, "must_change_password": False}
            p1 = c.post("/api/v1/admin/users", json=plain)
            p2 = c.post("/api/v1/admin/users", json=plain)
            check("第一次 200", p1.status_code == 200, f"HTTP {p1.status_code}")
            check("第二次 409（唯一约束照常生效）", p2.status_code == 409,
                  f"HTTP {p2.status_code}")
            if p1.status_code == 200:
                created.append(p1.json()["user"]["user_id"])

            print()
            print("=== 5. 长连接头已生效（keep-alive 65s）===")
            # 服务端连接不会被 5 秒空闲关掉：等 6 秒再打一个写请求，
            # 若仍是 200，说明 --timeout-keep-alive 65 生效前的竞态窗口已消除。
            import time
            time.sleep(6.0)
            r4 = c.post("/api/v1/admin/users", json={
                **plain, "username": f"idem_d_{suffix}"},
                headers={"X-Idempotency-Key": f"e2e-{suffix}-0003"})
            check("空闲 6 秒后复用连接仍 200", r4.status_code == 200,
                  f"HTTP {r4.status_code} {r4.text[:120]}")
            if r4.status_code == 200:
                created.append(r4.json()["user"]["user_id"])
    finally:
        with sqlite3.connect(DB) as conn:
            for uid in created:
                conn.execute("DELETE FROM dim_user_credential WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM dim_user_contact WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM dim_user WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM dim_user_credential WHERE user_id=?", (admin_id,))
            conn.execute("DELETE FROM dim_user WHERE user_id=?", (admin_id,))
        print(f"\n  已清理：探测管理员 + 创建的 {len(created)} 个账号")

    print()
    if failures:
        print(f"❌ 失败 {len(failures)} 项：" + "；".join(failures))
        return 1
    print("✅ 全部通过：幂等键让重试安全，且不影响正常建号")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
