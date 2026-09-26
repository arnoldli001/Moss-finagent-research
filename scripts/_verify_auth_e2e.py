"""端到端验证：前端要用的那条认证链路，在真实服务上是否真的通。

## 关键设计：**先问服务用的是哪个库，再对齐**

本项目的 `manage.py start` 默认走 **dev 隔离实例**
（`dev_isolation_env()` → `MOSS_SQLITE_PATH=data/dev/moss_dev.db`），
而脚本若按 `Settings()` 的默认值去连 `data/moss_finagent.db`，
就会出现"脚本建了用户、服务却报 401"的假故障 —— **实测踩过这个坑**，
排查了很久（症状只是"账号或密码错误"，两边看起来都正常）。

所以本脚本第一步就是问服务的诊断端点 `/auth/_debug/whoami` 拿到**真实库路径**，
再按同一个库建号。这样无论服务跑在 dev 隔离还是生产配置上都能对齐。

验证的是**前端实际会走的路径**，全程走 HTTP + Cookie：

  1. 建临时账号（活跃、VIP）——模拟"管理员已审批"
  2. `POST /auth/login`          → 200，且 Set-Cookie 下发三层 Cookie
  3. `GET  /auth/me`             → 200，邮箱脱敏
  4. `GET  /auth/sessions`       → 列出当前设备
  5. `POST /auth/password/change` → 200
  6. 用新密码重登 → 200；用旧密码 → 401
  7. `POST /auth/logout` → 200，之后 `/auth/me` → 401

用法：
    .venv/Scripts/python.exe scripts/_verify_auth_e2e.py
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.core import config as config_module  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.infrastructure.repositories.auth_sqlite_repo import (  # noqa: E402
    AuthSqliteRepository,
)

BASE = "http://127.0.0.1:8100"
SUFFIX = secrets.token_hex(4)
USERNAME = f"e2e_{SUFFIX}"
EMAIL = f"e2e_{SUFFIX}@example.com"
USER_ID = f"u_e2e_{SUFFIX}"
PASSWORD = "E2eFrontendCheck#2026x"
NEW_PASSWORD = "E2eFrontendChanged#2026y"

ok_count = 0
fail_count = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if ok:
        ok_count += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        fail_count += 1
        print(f"  [FAIL] {label}  {detail}")


def resolve_service_db() -> tuple[str, str]:
    """问服务它连的是哪个库；返回 (库路径, 说明)。"""
    try:
        r = httpx.get(f"{BASE}/api/v1/auth/_debug/whoami", timeout=15.0)
        if r.status_code == 200:
            data = r.json()
            return str(data.get("repo_db_path") or ""), (
                f"服务自报（env={data.get('env')}, "
                f"notifier={data.get('notifier')}）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [i] 无法读取服务诊断端点：{exc}")
    return "", "回退到本地 Settings()"


def main() -> int:
    print(f"服务：{BASE}")
    print()

    print("=== 步骤 0：确认服务连的是哪个库 ===")
    db_path, source = resolve_service_db()
    if db_path:
        print(f"  服务库：{db_path}   ← {source}")
        # ★ 把脚本切到同一个库。`get_settings()` 是 lru_cache 单例，
        #   改环境变量后必须 cache_clear，否则拿到的还是旧值
        #   （这正是本轮在 config.py 里修过的同一类陷阱）。
        import os

        os.environ["MOSS_SQLITE_PATH"] = db_path
        config_module.get_settings.cache_clear()
    else:
        db_path = get_settings().sqlite_path
        print(f"  本地库：{db_path}   ← {source}")
    settings = get_settings()
    print(f"  脚本库：{settings.sqlite_path}")
    if str(settings.sqlite_path) != db_path:
        print("  [!] 两边不一致，脚本会写到别处 —— 中止")
        return 1
    print()

    repo = AuthSqliteRepository(settings.sqlite_path)
    repo.ensure_schema()

    repo.create_user(user_id=USER_ID, username=USERNAME,
                     display_name="端到端校验", status="active",
                     valid_until="2099-12-31T00:00:00+00:00",
                     applied_tier="vip")
    repo.set_password(USER_ID, PASSWORD)
    repo.bind_contact(user_id=USER_ID, kind="email", value=EMAIL, verified=True)
    print(f"=== 步骤 1：建临时账号 {USERNAME}（vip，有效期 2099）===")
    print()

    try:
        with httpx.Client(base_url=BASE, timeout=20.0) as c:
            # ---- 登录 ----
            print("=== 步骤 2：登录 ===")
            r = c.post("/api/v1/auth/login", json={
                "account": USERNAME, "password": PASSWORD, "remember_me": True})
            check("POST /auth/login = 200", r.status_code == 200,
                  f"实际 {r.status_code} {r.text[:140]}")
            if r.status_code != 200:
                return 1
            body = r.json()
            check("响应体含 user", bool(body.get("user")),
                  f"user={body.get('user', {}).get('username')}")
            check("响应体**不含**任何令牌（令牌只走 Cookie）",
                  "access_token" not in r.text and "refresh_token" not in r.text)

            names = set(c.cookies.keys())
            check("下发 moss_sid（会话）", "moss_sid" in names, f"{sorted(names)}")
            check("下发 moss_rt（记住我 7 天）", "moss_rt" in names)
            check("下发 moss_csrf（前端可读，供 CSRF 头）", "moss_csrf" in names)
            set_cookie = r.headers.get("set-cookie", "")
            check("moss_sid 带 HttpOnly（XSS 读不到）",
                  "httponly" in set_cookie.lower())

            # ---- /auth/me ----
            print("=== 步骤 3：/auth/me（Cookie 回发是否生效）===")
            r = c.get("/api/v1/auth/me")
            check("GET /auth/me = 200", r.status_code == 200,
                  f"实际 {r.status_code} {r.text[:140]}")
            if r.status_code == 200:
                me = r.json()
                check("user_id 与 status 正确",
                      me.get("user_id") == USER_ID and me.get("status") == "active")
                check("邮箱已脱敏（响应中无明文）", EMAIL not in r.text,
                      f"contacts={me.get('contacts')}")

            # ---- 会话列表 ----
            print("=== 步骤 4：设备管理 ===")
            r = c.get("/api/v1/auth/sessions")
            check("GET /auth/sessions = 200", r.status_code == 200,
                  f"实际 {r.status_code}")
            if r.status_code == 200:
                ss = r.json().get("sessions", [])
                check("含当前设备且标记 current",
                      any(s.get("current") for s in ss), f"共 {len(ss)} 个")

            # ---- 改密 ----
            print("=== 步骤 5：改密 ===")
            r = c.post("/api/v1/auth/password/change", json={
                "old_password": PASSWORD, "new_password": NEW_PASSWORD})
            check("POST /auth/password/change = 200", r.status_code == 200,
                  f"实际 {r.status_code} {r.text[:160]}")

            print("=== 步骤 6：新密码可登 / 旧密码被拒 ===")
            with httpx.Client(base_url=BASE, timeout=20.0) as c2:
                r = c2.post("/api/v1/auth/login", json={
                    "account": USERNAME, "password": NEW_PASSWORD,
                    "remember_me": False})
                check("新密码登录 = 200", r.status_code == 200,
                      f"实际 {r.status_code} {r.text[:140]}")

                r = c2.post("/api/v1/auth/login", json={
                    "account": USERNAME, "password": PASSWORD,
                    "remember_me": False})
                check("旧密码登录 = 401", r.status_code == 401,
                      f"实际 {r.status_code}")

                print("=== 步骤 7：登出与吊销 ===")
                r = c2.post("/api/v1/auth/logout")
                check("POST /auth/logout = 200", r.status_code == 200,
                      f"实际 {r.status_code}")
                r = c2.get("/api/v1/auth/me")
                check("登出后 /auth/me = 401（会话真被吊销）",
                      r.status_code == 401, f"实际 {r.status_code}")

    finally:
        repo.update_user_status(USER_ID, "deleted")
        print()
        print("已软删临时账号（status=deleted）")

    print()
    print(f"=== 结果：{ok_count} 通过 / {fail_count} 失败 ===")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
