"""验证「我的自选池」在盘面上真的生效（端到端）。

检查：
  1. 不给任何池的新用户 → `/intraday/watchlist/mine` 回退到共享列表（非空）
  2. 建一个只含 600519 的池 → 同一端点**只返回 600519**
     （证明"盘面按我自己的池算"真的通了）

⚠️ 这一步会触发**真实取数**（估值 + 轻量快照），冷启动可能要几十秒。
   脚本给了 240 秒超时，并在失败时如实报告而不是伪造成功。

用法：
    .venv/Scripts/python.exe scripts/_verify_mine_endpoint.py --admin admin --password '...'
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
#: 取数慢：冷启动要跑估值链 + 轻量快照（实测整表可达几十秒）
READ_TIMEOUT = 240.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    name = f"mine_{secrets.token_hex(4)}"
    pwd = "MineEndpoint#2026x"

    with httpx.Client(base_url=BASE, timeout=30.0) as admin:
        r = admin.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": False})
        print(f"  admin 登录：{r.status_code}")
        if r.status_code != 200:
            return 1
        r = admin.post("/api/v1/admin/users", json={
            "username": name, "password": pwd, "tier": "vip", "days": 30,
            "must_change_password": False})
        print(f"  建号：{r.status_code}")
        if r.status_code != 200:
            return 1

    ok = True
    with httpx.Client(base_url=BASE, timeout=READ_TIMEOUT) as u:
        u.post("/api/v1/auth/login", json={
            "account": name, "password": pwd, "remember_me": False})

        print()
        print("  === 1) 未建池：应回退到共享列表（非空）===")
        try:
            r = u.get("/api/v1/intraday/watchlist/mine?limit=200")
            print(f"  HTTP {r.status_code}")
            if r.status_code == 200:
                body = r.json()
                codes = [i["code"] for i in body.get("items", [])]
                print(f"  source = {body.get('source')} | 只数 = {len(codes)}")
                print(f"  前 8 个 = {codes[:8]}")
                if body.get("source") == "user" and len(codes) > 0:
                    print("  → [PASS] 按用户端点可用且回退非空")
                else:
                    print("  → [FAIL] 端点返回空或 source 不对")
                    ok = False
            else:
                print(f"  → [FAIL] 非 200：{r.text[:200]}")
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"  → [FAIL] 请求异常（可能取数超时）：{exc}")
            ok = False

        print()
        print("  === 2) 建只含 600519 的池 ===")
        pool = u.post("/api/v1/me/pools", json={"name": "盘面验证池"}).json()["pool_id"]
        u.post(f"/api/v1/me/pools/{pool}/stocks", json={"code": "600519"})
        d = u.get("/api/v1/me/pools/_diag/resolution").json()
        print(f"  解析：source={d['resolved_source']} codes={d['resolved_codes']}")
        print(f"  管线：{d['pipeline_source']} migrated={d['pipeline_migrated']}")
        print(f"  按用户端点：{d.get('user_endpoint')} "
              f"available={d.get('user_endpoint_available')}")

        print()
        print("  === 3) 同一端点应变成长度 1（只有我池里的票）===")
        try:
            r = u.get("/api/v1/intraday/watchlist/mine?limit=200")
            if r.status_code == 200:
                codes = [i["code"] for i in r.json().get("items", [])]
                print(f"  只数 = {len(codes)} | codes = {codes}")
                if codes == ["600519"]:
                    print("  → [PASS] 盘面已按我自己的池返回")
                else:
                    print(f"  → [FAIL] 期望 ['600519']，实际 {codes}")
                    ok = False
            else:
                print(f"  → [FAIL] 非 200：{r.text[:200]}")
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"  → [FAIL] 请求异常：{exc}")
            ok = False

    # 清理
    with httpx.Client(base_url=BASE, timeout=30.0) as admin2:
        admin2.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": False})
        r = admin2.get(f"/api/v1/admin/users?keyword={name}")
        for row in r.json().get("users", []):
            if row["username"] == name:
                admin2.delete(f"/api/v1/admin/users/{row['user_id']}"
                              "?note=验证脚本清理")
                print(f"\n  已清理测试账号 {name}")

    print()
    print(f"=== 结果：{'全部通过' if ok else '有失败项'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
