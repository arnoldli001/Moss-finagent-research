"""验证「分步迁移」在真实服务上确实生效。

检查两件事：
  1. **没建池**的用户 → 解析回退到 `configs/intraday.yaml`（不是空列表！）
  2. **建了池**的用户 → 解析切到他自己库里的那份

第 1 条是刚修的关键点：provider 装配时若漏传 YAML 回退，新用户会解析出
**空列表** —— 界面上表现为"登录进去自选是空的"。

用法：
    .venv/Scripts/python.exe scripts/_verify_fallback.py --admin admin --password '...'
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    name = f"fb_{secrets.token_hex(4)}"
    pwd = "FallbackCheck#2026x"

    with httpx.Client(base_url=BASE, timeout=25.0) as admin:
        r = admin.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": False})
        print(f"  admin 登录：{r.status_code}")
        if r.status_code != 200:
            return 1
        r = admin.post("/api/v1/admin/users", json={
            "username": name, "password": pwd, "tier": "vip", "days": 30,
            "must_change_password": False})
        print(f"  建号：{r.status_code} {r.json().get('message', '')[:40]}")
        if r.status_code != 200:
            return 1

    with httpx.Client(base_url=BASE, timeout=25.0) as u:
        u.post("/api/v1/auth/login", json={
            "account": name, "password": pwd, "remember_me": False})

        d = u.get("/api/v1/me/pools/_diag/resolution").json()
        print()
        print("  === 新用户（未建池）===")
        print(f"  resolved_source = {d['resolved_source']}")
        print(f"  resolved_count  = {d['resolved_count']}")
        print(f"  is_user_owned   = {d['is_user_owned']}")
        print(f"  reason          = {(d['reason'] or '')[:80]}")
        print(f"  codes 前 6 个    = {d['resolved_codes'][:6]}")
        ok1 = d["resolved_source"] == "yaml" and d["resolved_count"] > 0
        print(f"  → {'[PASS]' if ok1 else '[FAIL]'} 未建池时回退到 YAML 且非空")

        pool = u.post("/api/v1/me/pools", json={"name": "验证池"}).json()["pool_id"]
        u.post(f"/api/v1/me/pools/{pool}/stocks", json={"code": "600519"})
        d2 = u.get("/api/v1/me/pools/_diag/resolution").json()
        print()
        print("  === 建池加票之后 ===")
        print(f"  resolved_source = {d2['resolved_source']}")
        print(f"  resolved_codes  = {d2['resolved_codes']}")
        ok2 = d2["resolved_source"] == "db" and d2["resolved_codes"] == ["600519"]
        print(f"  → {'[PASS]' if ok2 else '[FAIL]'} 建池后切换到自己的库")

        print()
        print(f"  pipeline_source = {d2['pipeline_source']}"
              f" | migrated = {d2['pipeline_migrated']}")
        print(f"  note = {d2['note'][:80]}")

    # 清理
    with httpx.Client(base_url=BASE, timeout=25.0) as admin2:
        admin2.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": False})
        r = admin2.get(f"/api/v1/admin/users?keyword={name}")
        for row in r.json().get("users", []):
            if row["username"] == name:
                admin2.delete(f"/api/v1/admin/users/{row['user_id']}"
                              "?note=验证脚本清理")
                print(f"\n  已清理测试账号 {name}")

    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
