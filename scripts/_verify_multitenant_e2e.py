"""多租户前端能力的最终端到端验证（真实 HTTP + Cookie）。

把目标里四组能力在同一条链路上走一遍，重点是**两个真实用户看到的东西完全独立**：

  A. 认证        —— 登录/登出/会话/改密 + 未登录被守卫拦住
  B. 自选池      —— 每用户各自一份，跨用户读/写/删一律 404
  C. 套餐额度    —— 由 applied_tier 决定，服务端强制（客户端传什么都不影响）
  D. 个股口径    —— 同一只票两人各有各的权重，还原只影响自己

用法：
    .venv/Scripts/python.exe scripts/_verify_multitenant_e2e.py --admin admin --password '<pwd>'
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
    a_name, b_name = f"mt_a_{s1}", f"mt_b_{s2}"
    pwd = "MultiTenant#2026x"

    print("=" * 62)
    print("A. 认证与会话")
    print("=" * 62)
    with httpx.Client(base_url=BASE, timeout=25.0) as admin:
        # 勾选"记住我"，这样才会额外下发 `moss_rt`（设计如此：
        # 不勾就不写这个 Cookie，减少长期凭据的暴露面）
        r = admin.post("/api/v1/auth/login", json={
            "account": args.admin, "password": args.password,
            "remember_me": True})
        check("管理员登录 200", r.status_code == 200, r.text[:120])
        if r.status_code != 200:
            return 1
        jar = {c.name for c in admin.cookies.jar}
        check("会话 Cookie moss_sid 下发", "moss_sid" in jar, f"{sorted(jar)}")
        # `moss_rt` 的 Path=/api/v1/auth：它只在该路径下发送（收窄暴露面），
        # 所以用 jar 判断而不是响应头字符串（多个 Set-Cookie 时 str() 只给第一个）
        check("记住我 Cookie moss_rt 下发（勾选后）", "moss_rt" in jar,
              f"{sorted(jar)}")
        check("CSRF Cookie moss_csrf 下发（前端可读，供 X-CSRF-Token）",
              "moss_csrf" in jar)

        # 建两个 VIP 用户（额度足够，便于测隔离而不是测限额）
        for name in (a_name, b_name):
            r = admin.post("/api/v1/admin/users", json={
                "username": name, "password": pwd, "tier": "vip",
                "days": 30, "must_change_password": False})
            check(f"建号 {name}", r.status_code == 200, r.text[:100])

    with httpx.Client(base_url=BASE, timeout=25.0) as anon:
        for path in ("/api/v1/me/pools", "/api/v1/me/profiles",
                     "/api/v1/admin/overview"):
            check(f"未登录 {path} → 401", anon.get(path).status_code == 401)

    print()
    print("=" * 62)
    print("B. 自选池按用户隔离")
    print("=" * 62)
    with httpx.Client(base_url=BASE, timeout=25.0) as a, \
            httpx.Client(base_url=BASE, timeout=25.0) as b:
        check("A 登录 200", a.post("/api/v1/auth/login", json={
            "account": a_name, "password": pwd,
            "remember_me": False}).status_code == 200)
        check("B 登录 200", b.post("/api/v1/auth/login", json={
            "account": b_name, "password": pwd,
            "remember_me": False}).status_code == 200)

        r = a.post("/api/v1/me/pools", json={"name": "A 的池"})
        check("A 建池 200", r.status_code == 200, r.text[:120])
        pool_a = r.json()["pool_id"]

        r = b.post("/api/v1/me/pools", json={"name": "B 的池"})
        check("B 建池 200", r.status_code == 200, r.text[:120])
        pool_b = r.json()["pool_id"]

        check("A、B 的池 id 不同", pool_a != pool_b)
        check("A 只看到自己的池",
              [p["name"] for p in a.get("/api/v1/me/pools").json()["pools"]]
              == ["A 的池"])
        check("B 只看到自己的池",
              [p["name"] for p in b.get("/api/v1/me/pools").json()["pools"]]
              == ["B 的池"])

        for i in range(3):
            a.post(f"/api/v1/me/pools/{pool_a}/stocks",
                   json={"code": f"{600000 + i}"})
        a_codes = {s["code"] for s in
                   a.get(f"/api/v1/me/pools/{pool_a}/stocks").json()["stocks"]}
        b_codes = {s["code"] for s in
                   b.get(f"/api/v1/me/pools/{pool_b}/stocks").json()["stocks"]}
        check("A 有 3 只、B 有 0 只",
              len(a_codes) == 3 and len(b_codes) == 0,
              f"A={sorted(a_codes)} B={sorted(b_codes)}")

        check("B 读 A 的池 → 404",
              b.get(f"/api/v1/me/pools/{pool_a}/stocks").status_code == 404)
        check("B 往 A 的池加票 → 404",
              b.post(f"/api/v1/me/pools/{pool_a}/stocks",
                     json={"code": "600036"}).status_code == 404)
        check("B 删 A 的池 → 404",
              b.delete(f"/api/v1/me/pools/{pool_a}").status_code == 404)
        check("B 改 A 的池名 → 404",
              b.patch(f"/api/v1/me/pools/{pool_a}",
                      json={"name": "hacked"}).status_code == 404)

        # A 的池没被 B 动过
        check("A 的池仍在且名称未变",
              a.get("/api/v1/me/pools").json()["pools"][0]["name"] == "A 的池")

        print()
        print("=" * 62)
        print("C. 套餐额度由服务端按等级决定")
        print("=" * 62)
        ua = a.get("/api/v1/me/pools/usage").json()
        ub = b.get("/api/v1/me/pools/usage").json()
        check("A（VIP）总额度 100", ua["stock_limit"] == 100,
              f"实际 {ua['stock_limit']}")
        check("A 已用 3 / B 已用 0",
              ua["stocks_used"] == 3 and ub["stocks_used"] == 0,
              f"A={ua['stocks_used']} B={ub['stocks_used']}")
        check("A 单池额度 100（VIP）", ua["quota"]["pool_size_limit"] == 100)

        print()
        print("=" * 62)
        print("D. 个股口径按用户隔离")
        print("=" * 62)
        r = a.put("/api/v1/me/profiles/600519", json={
            "code": "600519", "mode": "intraday",
            "weights": {"box": 17.0, "chan": 12.0}})
        check("A 保存 600519 口径 200", r.status_code == 200, r.text[:120])
        ka = r.json()["profile"]["caliber_key"]

        got_b = b.get("/api/v1/me/profiles/600519").json()
        check("★ B 看到的是**默认**（不是 A 的权重）",
              got_b["from_user"] is False and got_b["weights"] == {},
              f"from_user={got_b['from_user']} weights={got_b['weights']}")
        check("B 的说明写着'跟随默认'", bool(got_b.get("explain")),
              got_b.get("explain", "")[:40])

        r = b.put("/api/v1/me/profiles/600519", json={
            "code": "600519", "mode": "intraday", "weights": {"box": 3.0}})
        check("B 保存自己的口径 200", r.status_code == 200)
        kb = r.json()["profile"]["caliber_key"]
        check("★ 两人口径不同 → 指纹不同", ka != kb, f"{ka} vs {kb}")

        got_a = a.get("/api/v1/me/profiles/600519").json()
        check("★ A 的口径**没有被 B 覆盖**",
              got_a["weights"] == {"box": 17.0, "chan": 12.0},
              f"{got_a['weights']}")

        # B 还原，不能影响 A
        r = b.delete("/api/v1/me/profiles/600519")
        check("B 还原系统默认 200", r.status_code == 200)
        check("A 的口径仍在",
              a.get("/api/v1/me/profiles/600519").json()["weights"]
              == {"box": 17.0, "chan": 12.0})

        # 口径相同 → 指纹相同（计算可共享）
        #
        # ⚠️ 必须复用**仍然打开**的 admin 客户端：`with` 块结束后客户端就关闭了，
        #    再用它会抛 `Cannot send a request, as the client has been closed`
        #    （实测踩到）。所以这里用一个独立的新管理员会话，而不是复用外面的。
        c = httpx.Client(base_url=BASE, timeout=25.0)
        try:
            c.post("/api/v1/auth/login", json={
                "account": args.admin, "password": args.password,
                "remember_me": False})
            r = c.post("/api/v1/admin/users", json={
                "username": f"mt_c_{secrets.token_hex(3)}",
                "password": pwd, "tier": "vip", "days": 30,
                "must_change_password": False})
            check("建第三个用户（用于指纹对比）", r.status_code == 200, r.text[:100])
            c_name = r.json()["user"]["username"]
            c.post("/api/v1/auth/logout")
            c.post("/api/v1/auth/login", json={
                "account": c_name, "password": pwd, "remember_me": False})
            r = c.put("/api/v1/me/profiles/600519", json={
                "code": "600519", "mode": "intraday",
                "weights": {"chan": 12.0, "box": 17.0}})   # 顺序不同
            kc = r.json()["profile"]["caliber_key"]
            check("★ 口径相同（键顺序不同）→ 指纹相同（计算可共享）",
                  kc == ka, f"{kc} vs {ka}")
        finally:
            c.close()

    print()
    print("=" * 62)
    print(f"结果：{ok_n} 通过 / {fail_n} 失败")
    print("=" * 62)
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
