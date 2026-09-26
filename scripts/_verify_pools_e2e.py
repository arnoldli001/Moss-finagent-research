"""「我的自选池」端到端验证（真实 HTTP + Cookie，与前端同一条链路）。

重点验证**多用户隔离**与**额度服务端强制**这两件事在真实服务上成立：

  1. 管理员登录并建一个试用档用户
  2. 用该用户建池、加票
  3. **另一个用户看不到他的池，也读不到他的 pool_id**
  4. 达到试用单池 5 只后被拒（409 + pool_full）
  5. 批量加入：能加的都加上，被跳过的逐条给原因
  6. 删池归还额度
  7. 未登录访问一律 401

用法：
    .venv/Scripts/python.exe scripts/_verify_pools_e2e.py --admin admin --password '<pwd>'
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

    s1, s2 = secrets.token_hex(4), secrets.token_hex(4)
    u1, u2 = f"pool_a_{s1}", f"pool_b_{s2}"
    pwd = "PoolE2E#2026x"

    with httpx.Client(base_url=BASE, timeout=25.0) as admin:
        print("=== 1) 管理员登录并开两个试用档用户 ===")
        r = admin.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": False})
        if r.status_code != 200:
            print(f"  管理员登录失败：{r.status_code} {r.text[:160]}")
            return 1
        ids = {}
        for name in (u1, u2):
            r = admin.post("/api/v1/admin/users", json={
                "username": name, "password": pwd, "tier": "trial",
                "days": 30, "must_change_password": False})
            check(f"开号 {name}", r.status_code == 200, r.text[:120])
            if r.status_code == 200:
                ids[name] = r.json()["user"]["user_id"]

        print()
        print("=== 2) A 建池、加票 ===")
        with httpx.Client(base_url=BASE, timeout=25.0) as a:
            a.post("/api/v1/auth/login", json={
                "account": u1, "password": pwd, "remember_me": False})
            r = a.get("/api/v1/me/pools")
            check("GET /me/pools = 200", r.status_code == 200, r.text[:120])
            check("新用户没有池", r.json()["pools"] == [])
            check("返回额度信息", "quota" in r.json(),
                  f"limit={r.json().get('quota', {}).get('stock_limit')}")
            check("试用档总数上限 = 25",
                  r.json()["quota"]["stock_limit"] == 25,
                  f"实际 {r.json()['quota']['stock_limit']}")

            r = a.post("/api/v1/me/pools", json={"name": "A 的池"})
            check("建池 = 200", r.status_code == 200, r.text[:160])
            pool_a = r.json()["pool_id"]

            for i in range(5):
                r = a.post(f"/api/v1/me/pools/{pool_a}/stocks",
                           json={"code": f"{600000 + i}"})
                check(f"加第 {i + 1} 只 = 200", r.status_code == 200,
                      r.text[:120])

            r = a.post(f"/api/v1/me/pools/{pool_a}/stocks",
                       json={"code": "699999"})
            check("第 6 只被拒 = 409", r.status_code == 409,
                  f"实际 {r.status_code} {r.text[:160]}")
            check("拒绝原因是 pool_full",
                  r.json().get("detail", {}).get("code") == "pool_full",
                  r.text[:160])

            usage = a.get("/api/v1/me/pools/usage").json()
            check("已用量 = 5/25",
                  usage["stocks_used"] == 5 and usage["stock_limit"] == 25,
                  f"{usage['stocks_used']}/{usage['stock_limit']}")

        print()
        print("=== 3) B 看不到 A 的池（隔离）===")
        with httpx.Client(base_url=BASE, timeout=25.0) as b:
            b.post("/api/v1/auth/login", json={
                "account": u2, "password": pwd, "remember_me": False})
            r = b.get("/api/v1/me/pools")
            check("B 的池列表为空", r.json()["pools"] == [],
                  f"实际 {[p['name'] for p in r.json()['pools']]}")
            check("B 的已用量为 0", r.json()["quota"]["stocks_used"] == 0)
            check("B 用 A 的 pool_id 读标的 → 404",
                  b.get(f"/api/v1/me/pools/{pool_a}/stocks").status_code == 404)
            check("B 用 A 的 pool_id 加票 → 404",
                  b.post(f"/api/v1/me/pools/{pool_a}/stocks",
                         json={"code": "600000"}).status_code == 404)
            check("B 用 A 的 pool_id 删池 → 404",
                  b.delete(f"/api/v1/me/pools/{pool_a}").status_code == 404)

        print()
        print("=== 4) 批量加入（放进一个**还有空间**的新池）===")
        with httpx.Client(base_url=BASE, timeout=25.0) as a2:
            a2.post("/api/v1/auth/login", json={
                "account": u1, "password": pwd, "remember_me": False})
            # ⚠️ 必须新建池：原来的池在第 2 步已被 5 只填满，
            #    再批量加只会得到"8 只全被拒"，验证不出"部分成功"。
            r = a2.post("/api/v1/me/pools", json={"name": "批量池"})
            check("为批量新建池 = 200", r.status_code == 200, r.text[:160])
            pool_bulk = r.json()["pool_id"]
            r = a2.post(f"/api/v1/me/pools/{pool_bulk}/stocks/bulk",
                        json={"codes": [f"{601000 + i}" for i in range(8)]})
            check("批量加入 = 200", r.status_code == 200, r.text[:200])
            if r.status_code == 200:
                body = r.json()
                check("部分成功（能加的都加上，不是全有或全无）",
                      0 < len(body["added"]) < 8,
                      f"added={len(body['added'])} rejected={len(body['rejected'])}")
                check("被跳过的逐条给出原因",
                      len(body["reasons"]) == len(body["rejected"]),
                      f"reasons={body['reasons'][:1]}")

            print()
            print("=== 5) 删池归还额度 ===")
            usage = a2.get("/api/v1/me/pools/usage").json()
            before = usage["stocks_used"]
            check("此时有两个池", usage["pools_used"] == 2,
                  f"pools_used={usage['pools_used']}")
            removed_total = 0
            # 两个池都要删 —— 只删一个的话额度当然不会归零
            for pid in (pool_a, pool_bulk):
                rr = a2.delete(f"/api/v1/me/pools/{pid}")
                if rr.status_code == 200:
                    removed_total += int(rr.json().get("removed", 0))
            check("删池共移除条目", removed_total == before,
                  f"移除 {removed_total} 只，删前额度占用 {before}")
            after = a2.get("/api/v1/me/pools/usage").json()
            check("额度已归还", after["stocks_used"] == 0,
                  f"删前 {before} → 删后 {after['stocks_used']}")
            check("池数归零", after["pools_used"] == 0)

    print()
    print("=== 6) 未登录一律 401 ===")
    with httpx.Client(base_url=BASE, timeout=15.0) as anon:
        check("GET /me/pools → 401",
              anon.get("/api/v1/me/pools").status_code == 401)
        check("GET /me/pools/usage → 401",
              anon.get("/api/v1/me/pools/usage").status_code == 401)

    print()
    print(f"=== 结果：{ok_n} 通过 / {fail_n} 失败 ===")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
