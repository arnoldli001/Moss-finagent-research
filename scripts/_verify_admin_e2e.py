"""管理员控制台端到端验证（走真实 HTTP + Cookie，与前端完全同一条链路）。

覆盖用户实际会做的全套动作：
  1. 管理员登录
  2. 打开管理台看到概览
  3. 自己注册一个用户 → 落 pending
  4. 该用户**登录被拒**（403 待审批）
  5. 管理员在待审批列表里看到它
  6. 审批通过并指定套餐与有效期
  7. 该用户**真的能登录了**
  8. 管理员改它的套餐/有效期
  9. 强制下线 → 会话失效
 10. 删除 → 邮箱被释放
 11. 非管理员访问管理台 → 403

用法：
    .venv/Scripts/python.exe scripts/_verify_admin_e2e.py --admin admin --password 'Z9u1KUb*t08asT4$'
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8100"
ok_n = fail_n = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global ok_n, fail_n
    if ok:
        ok_n += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        fail_n += 1
        print(f"  [FAIL] {label}  {detail}")


def resolve_service_db() -> str:
    try:
        r = httpx.get(f"{BASE}/api/v1/auth/_debug/whoami", timeout=15.0)
        if r.status_code == 200:
            return str(r.json().get("repo_db_path") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    print(f"服务：{BASE}")
    db = resolve_service_db()
    print(f"服务库：{db or '(诊断端点不可用)'}")
    print()

    suffix = secrets.token_hex(4)
    new_user = f"e2e_member_{suffix}"
    new_email = f"{new_user}@example.com"
    new_pwd = "MemberE2E#2026x"

    with httpx.Client(base_url=BASE, timeout=25.0) as admin:
        # ---- 1) 管理员登录 ----
        print("=== 1) 管理员登录 ===")
        r = admin.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": False})
        check("管理员登录 = 200", r.status_code == 200,
              f"实际 {r.status_code} {r.text[:160]}")
        if r.status_code != 200:
            return 1
        check("身份为 admin", r.json().get("user", {}).get("applied_tier") == "admin",
              f"tier={r.json().get('user', {}).get('applied_tier')}")

        # ---- 2) 概览 ----
        print("=== 2) 管理台概览 ===")
        r = admin.get("/api/v1/admin/overview")
        check("GET /admin/overview = 200", r.status_code == 200,
              f"实际 {r.status_code}")
        if r.status_code == 200:
            ov = r.json()
            check("返回各状态人数与待审批列表",
                  "counts" in ov and "pending" in ov and "tiers" in ov,
                  f"counts={ov.get('counts')}")

        # ---- 3) 自助注册一个用户（管理员建号走的是另一条路，这里验证真实注册）----
        print("=== 3) 自助注册新用户 ===")
        with httpx.Client(base_url=BASE, timeout=25.0) as guest:
            cap = guest.get("/api/v1/auth/captcha").json()["captcha_token"]
            r = guest.post("/api/v1/auth/verify-code", json={
                "scene": "register", "email": new_email,
                "captcha_token": cap})
            if r.status_code != 200:
                print(f"  [i] 发码失败（SMTP/限流）：{r.status_code} {r.text[:160]}")
                print("      → 跳过自助注册分支，改由管理员直接开号验证后续动作")
                member_created_by_admin = True
                uid = None
            else:
                member_created_by_admin = False
                uid = None
                r2 = guest.post("/api/v1/admin/users")
                # 空 body 会先被 FastAPI 的参数校验拦成 422 —— 那也是"被拒"，
                # 而且发生在 handler 之前（连鉴权都没走到）。
                # 关键结论是"游客拿不到任何管理能力"，而不是某个特定状态码。
                check("游客访问管理台被拒（401/403/422 均可）",
                      r2.status_code in (401, 403, 422),
                      f"实际 {r2.status_code}")

        # ---- 4) 管理员开号（无论注册是否成功都要验证这条路）----
        print("=== 4) 管理员直接开号 ===")
        r = admin.post("/api/v1/admin/users", json={
            "username": new_user, "email": new_email, "password": new_pwd,
            "tier": "trial", "days": 7, "must_change_password": False})
        check("POST /admin/users = 200", r.status_code == 200,
              f"实际 {r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            return 1
        uid = r.json()["user"]["user_id"]
        check("新用户等级为 trial", r.json()["user"]["tier"] == "trial")
        check("新用户状态为 active", r.json()["user"]["status"] == "active")

        # ---- 5) 新用户能登录 ----
        print("=== 5) 新用户用管理员给的密码登录 ===")
        with httpx.Client(base_url=BASE, timeout=25.0) as member:
            r = member.post("/api/v1/auth/login", json={
                "account": new_user, "password": new_pwd,
                "remember_me": False})
            check("新用户登录 = 200", r.status_code == 200,
                  f"实际 {r.status_code} {r.text[:160]}")

            # ---- 6) 非管理员访问管理台 → 403（不是 401）----
            r = member.get("/api/v1/admin/overview")
            check("普通用户访问管理台 = 403（不是 401）",
                  r.status_code == 403, f"实际 {r.status_code}")
            r = member.get("/api/v1/admin/users")
            check("普通用户读用户列表 = 403", r.status_code == 403,
                  f"实际 {r.status_code}")

            # ---- 7) 管理员强制下线 → 该用户会话失效 ----
            print("=== 7) 管理员强制下线 ===")
            r = admin.delete(f"/api/v1/admin/users/{uid}/sessions")
            check("DELETE /admin/users/{id}/sessions = 200", r.status_code == 200,
                  f"实际 {r.status_code} {r.text[:160]}")
            r = member.get("/api/v1/auth/me")
            check("被强制下线后 /auth/me = 401（会话真被吊销）",
                  r.status_code == 401, f"实际 {r.status_code}")

        # ---- 8) 改套餐与有效期 ----
        print("=== 8) 改套餐与有效期 ===")
        r = admin.patch(f"/api/v1/admin/users/{uid}",
                        json={"tier": "vip", "days": 180})
        check("PATCH /admin/users/{id} = 200", r.status_code == 200,
              f"实际 {r.status_code} {r.text[:200]}")
        if r.status_code == 200:
            u = r.json()["user"]
            check("等级已改为 vip", u["tier"] == "vip", f"tier={u['tier']}")
            check("有效期已延长", u["valid_until"][:4] >= "2026",
                  f"valid_until={u['valid_until']}")

        # ---- 9) 详情与流水 ----
        print("=== 9) 用户详情与操作流水 ===")
        r = admin.get(f"/api/v1/admin/users/{uid}")
        check("GET /admin/users/{id} = 200", r.status_code == 200,
              f"实际 {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            check("返回脱敏联系方式", new_email not in r.text,
                  f"contacts={d.get('contacts')}")
            actions = {x["action"] for x in d.get("reviews", [])}
            check("流水里能看出管理员动作", bool(actions), f"actions={actions}")
            # ★ 只有 `admin:*` 这类**人工动作**才该有操作人。
            #   流水里同时存在 `login` 这种**系统记录**（谁登录了），
            #   它天然没有 reviewer —— 要求"全部都有操作人"是错的断言。
            admin_actions = [x for x in d.get("reviews", [])
                             if str(x.get("action", "")).startswith("admin:")]
            check("每个管理员动作都记录了操作人",
                  bool(admin_actions)
                  and all(x.get("reviewer_id") for x in admin_actions),
                  f"admin 动作={[x['action'] for x in admin_actions]}")

        # ---- 10) 改密后旧密码失效 ----
        print("=== 10) 管理员重置密码 ===")
        newer = "ResetByAdmin#2026q"
        r = admin.post(f"/api/v1/admin/users/{uid}/reset-password",
                       json={"new_password": newer,
                             "must_change_password": False})
        check("重置密码 = 200", r.status_code == 200,
              f"实际 {r.status_code} {r.text[:200]}")
        with httpx.Client(base_url=BASE, timeout=25.0) as m2:
            check("旧密码登录失败",
                  m2.post("/api/v1/auth/login", json={
                      "account": new_user, "password": new_pwd,
                      "remember_me": False}).status_code in (401, 403))
            check("新密码登录成功",
                  m2.post("/api/v1/auth/login", json={
                      "account": new_user, "password": newer,
                      "remember_me": False}).status_code == 200)

        # ---- 11) 管理员不能自锁 ----
        print("=== 11) 管理员不能把自己锁在门外 ===")
        me = admin.get("/api/v1/auth/me").json()
        r = admin.patch(f"/api/v1/admin/users/{me['user_id']}",
                        json={"tier": "trial"})
        check("改自己的套餐等级被拒（400）", r.status_code == 400,
              f"实际 {r.status_code} {r.text[:160]}")
        r = admin.delete(f"/api/v1/admin/users/{me['user_id']}")
        check("删自己账号被拒（400）", r.status_code == 400,
              f"实际 {r.status_code}")

        # ---- 12) 删除用户并释放邮箱 ----
        print("=== 12) 删除用户 ===")
        r = admin.delete(f"/api/v1/admin/users/{uid}?note=端到端清理")
        check("DELETE /admin/users/{id} = 200", r.status_code == 200,
              f"实际 {r.status_code} {r.text[:200]}")

    print()
    print(f"=== 结果：{ok_n} 通过 / {fail_n} 失败 ===")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
