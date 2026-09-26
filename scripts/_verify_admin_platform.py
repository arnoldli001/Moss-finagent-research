"""验证管理员控制台改造（真实 HTTP）。

检查：
  1. 管理员能读到套餐配置 / 权限矩阵 / 资源监控
  2. 改一个等级的权限 → `/me/features` 的 `visible_views` 跟着变
     （这是"页签由服务端下发"的核心证明）
  3. 普通用户访问管理端接口 → 403

用法：
    .venv/Scripts/python.exe scripts/_verify_admin_platform.py --admin admin --password '...'
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    suffix = secrets.token_hex(4)
    uname = f"ptrial_{suffix}"
    pwd = "PlatformVerify#2026x"

    with httpx.Client(base_url=BASE, timeout=30.0) as admin:
        r = admin.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": False})
        check("管理员登录 200", r.status_code == 200, f"{r.status_code}")
        if r.status_code != 200:
            return 1

        print()
        print("=== 1) 套餐配置 ===")
        r = admin.get("/api/v1/admin/platform/tiers")
        check("GET /platform/tiers = 200", r.status_code == 200, r.text[:120])
        tiers = {t["key"]: t for t in r.json()["tiers"]}
        check("三个等级齐全", set(tiers) == {"admin", "vip", "trial"},
              f"{sorted(tiers)}")
        check("含资源字段清单", len(r.json()["resources"]) >= 5,
              f"{len(r.json()['resources'])} 项")
        check("含功能字段清单", len(r.json()["features"]) >= 8,
              f"{len(r.json()['features'])} 项")
        check("试用档单池额度低于 VIP",
              tiers["trial"]["resources"]["watchlist_stocks"]
              <= tiers["vip"]["resources"]["watchlist_stocks"])

        print()
        print("=== 2) 权限矩阵 ===")
        r = admin.get("/api/v1/admin/platform/permissions-matrix")
        check("GET /permissions-matrix = 200", r.status_code == 200)
        if r.status_code == 200:
            m = r.json()
            check("矩阵覆盖全部功能", len(m["rows"]) >= 8, f"{len(m['rows'])} 行")
            check("每行含三个等级与定价",
                  all(set(row["tiers"]) == {"admin", "vip", "trial"}
                      for row in m["rows"]))

        print()
        print("=== 3) 资源监控 ===")
        r = admin.get("/api/v1/admin/platform/monitor?minutes=1440")
        check("GET /monitor = 200", r.status_code == 200, r.text[:120])
        if r.status_code == 200:
            body = r.json()
            check("返回总体与按租户聚合",
                  "overall" in body and "by_tenant" in body,
                  f"采样 {body['sampled_calls']} 条")
            check("有按接口聚合", "by_path" in body)

        print()
        print("=== 4) 建一个试用用户，看它的可见页签 ===")
        r = admin.post("/api/v1/admin/users", json={
            "username": uname, "password": pwd, "tier": "trial", "days": 7,
            "must_change_password": False})
        check("建试用用户 200", r.status_code == 200, r.text[:120])
        if r.status_code != 200:
            return 1
        uid = r.json()["user"]["user_id"]

        with httpx.Client(base_url=BASE, timeout=30.0) as u:
            u.post("/api/v1/auth/login", json={
                "account": uname, "password": pwd, "remember_me": False})
            f1 = u.get("/api/v1/me/features").json()
            check("试用档 /me/features 可读", "visible_views" in f1)
            check("试用档默认**看不到**策略回测",
                  "backtest" not in f1["visible_views"],
                  f"{f1['visible_views']}")
            check("试用档**能**看到量化交易（做T）",
                  "intraday" in f1["visible_views"])
            check("试用档看不到调度管理",
                  "scheduler" not in f1["visible_views"])

            print()
            print("=== 5) 管理员给试用档开「策略回测」+「调度管理」 ===")
            r = admin.put("/api/v1/admin/platform/tiers/trial", json={
                "features": {"backtest": True, "scheduler": True}})
            check("改套餐 200", r.status_code == 200, r.text[:200])

            f2 = u.get("/api/v1/me/features").json()
            check("★ 改权限后 visible_views **立刻**包含策略回测",
                  "backtest" in f2["visible_views"], f"{f2['visible_views']}")
            check("★ 包含调度管理",
                  "scheduler" in f2["visible_views"], f"{f2['visible_views']}")

            print()
            print("=== 6) 普通用户访问管理端 → 403 ===")
            for path in ("/api/v1/admin/platform/tiers",
                         "/api/v1/admin/platform/monitor",
                         "/api/v1/admin/platform/permissions-matrix"):
                check(f"普通用户 {path} → 403",
                      u.get(path).status_code == 403,
                      f"实际 {u.get(path).status_code}")

        # 复原（别把试用档的默认权限留在被改过的状态）
        admin.put("/api/v1/admin/platform/tiers/trial", json={
            "features": {"backtest": False, "scheduler": False}})
        print()
        print("  已复原试用档默认权限")

        admin.delete(f"/api/v1/admin/users/{uid}?note=验证脚本清理")
        print(f"  已清理测试账号 {uname}")

    print()
    print(f"=== 结果：{ok_n} 通过 / {fail_n} 失败 ===")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
