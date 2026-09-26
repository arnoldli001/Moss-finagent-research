"""用 Cloudflare API 把「命名隧道 + 自定义域名 + Access 白名单」一次配好。

## 为什么不用 `cloudflared tunnel login`

本机实测两次都卡在**回调取证书**这一环（浏览器授权成功、CLI 报
`Failed to fetch resource`，`~/.cloudflared/cert.pem` 始终没生成）。
改用 API token 就绕开了整个 OAuth 回调：建隧道、取隧道 token、写 DNS、
建 Access 应用与策略全是普通 HTTPS 调用，脚本自己就能跑完。

## 需要你做的只有一件事（其余本脚本全包）

Cloudflare 面板 → 右上头像 → **My Profile → API Tokens → Create Token
→ Create Custom Token**，权限给这四项（都是 Edit）：

    Zone     → DNS              → Edit       （写 moss.wujiaitool.cn 的 CNAME）
    Account  → Cloudflare Tunnel→ Edit       （建隧道 / 取隧道 token / 写 ingress）
    Account  → Access: Apps and Policies → Edit （建 Access 应用与邮箱白名单）
    Zone     → Zone             → Read       （查 zone id，可选）

然后把 token 写进项目根目录 `.env`（**不要贴到聊天里**）：

    CLOUDFLARE_API_TOKEN=xxxxxxxx
    CLOUDFLARE_ACCESS_EMAILS=2693888583@qq.com,另一位客户@example.com

## 用法

    .venv\\Scripts\\python.exe scripts\\cloudflare_setup.py --status   # 查 zone 是否 Active
    .venv\\Scripts\\python.exe scripts\\cloudflare_setup.py --all      # 建隧道+DNS+Access，并写入 .env
    .venv\\Scripts\\python.exe scripts\\cloudflare_setup.py --run      # 用写入的 token 起隧道

`--all` 会把 `CLOUDFLARE_TUNNEL_ID` / `CLOUDFLARE_TUNNEL_TOKEN` 追加进 `.env`，
之后 `manage.py` 起的服务与 `--run` 都读得到。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
API = "https://api.cloudflare.com/client/v4"
DOMAIN = "wujiaitool.cn"
HOSTNAME = "moss.wujiaitool.cn"
TUNNEL_NAME = "moss-pilot"
ORIGIN = "http://127.0.0.1:8110"


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value.strip()
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line.startswith(f"{name}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    return ""


def _call(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"× {method} {path} → HTTP {exc.code}\n  {detail}") from exc


def _write_env(pairs: dict[str, str]) -> None:
    """把结果追加/覆盖进 .env（幂等：已存在的键就地替换）。"""
    lines = open(ENV_PATH, encoding="utf-8").read().splitlines() if os.path.exists(ENV_PATH) else []
    for key, value in pairs.items():
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    print(f"  已写入 .env：{', '.join(pairs)}（值不外显）")


def zone(token: str) -> dict:
    data = _call("GET", f"/zones?name={DOMAIN}", token)
    if not data.get("result"):
        raise SystemExit(f"× 账号里没有 {DOMAIN} 这个 zone（先 Add a site）")
    z = data["result"][0]
    print(f"zone {z['name']}  id={z['id'][:12]}…  status={z['status']}")
    print(f"     NS: {', '.join(z.get('name_servers') or [])}")
    if z["status"] != "active":
        print("    ⚠️ 还不是 active：NS 尚未完全生效，DNS/隧道可能建不上")
    return z


def tunnel(token: str, account_id: str) -> tuple[str, str]:
    """复用同名隧道或新建；返回 (tunnel_id, tunnel_token)。"""
    listed = _call("GET", f"/accounts/{account_id}/cfd_tunnel?name={TUNNEL_NAME}"
                          "&is_deleted=false", token)
    found = listed.get("result") or []
    if found:
        tid = found[0]["id"]
        print(f"隧道 {TUNNEL_NAME} 已存在：{tid[:12]}…（复用）")
    else:
        created = _call("POST", f"/accounts/{account_id}/cfd_tunnel", token, {
            "name": TUNNEL_NAME,
            # cloudflare = ingress 由面板/API 管（远端配置）；本地只需 --token 起进程
            "config_src": "cloudflare",
        })
        tid = created["result"]["id"]
        print(f"隧道 {TUNNEL_NAME} 已创建：{tid[:12]}…")
    tok = _call("GET", f"/accounts/{account_id}/cfd_tunnel/{tid}/token", token)
    return tid, tok["result"]


def ingress(token: str, account_id: str, tid: str) -> None:
    _call("PUT", f"/accounts/{account_id}/cfd_tunnel/{tid}/configurations", token, {
        "config": {"ingress": [
            {"hostname": HOSTNAME, "service": ORIGIN},
            {"service": "http_status:404"},
        ]},
    })
    print(f"远端 ingress 已写入：{HOSTNAME} → {ORIGIN}")


def dns(token: str, zone_id: str, tid: str) -> None:
    target = f"{tid}.cfargotunnel.com"
    existing = _call("GET", f"/zones/{zone_id}/dns_records?name={HOSTNAME}", token)
    body = {"type": "CNAME", "name": HOSTNAME, "content": target,
            "proxied": True, "comment": "moss-pilot tunnel"}
    if existing.get("result"):
        rid = existing["result"][0]["id"]
        _call("PUT", f"/zones/{zone_id}/dns_records/{rid}", token, body)
        print(f"DNS 记录已更新：{HOSTNAME} → {target}")
    else:
        _call("POST", f"/zones/{zone_id}/dns_records", token, body)
        print(f"DNS 记录已创建：{HOSTNAME} → {target}")


def access(token: str, account_id: str, emails: list[str]) -> None:
    apps = _call("GET", f"/accounts/{account_id}/access/apps", token)
    app = next((a for a in apps.get("result") or []
                if a.get("domain") == HOSTNAME), None)
    if app is None:
        created = _call("POST", f"/accounts/{account_id}/access/apps", token, {
            "name": TUNNEL_NAME, "domain": HOSTNAME, "type": "self_hosted",
            "session_duration": "24h",
            "app_launcher_visible": False,
        })
        app = created["result"]
        print(f"Access 应用已创建：{HOSTNAME}（id={app['id'][:12]}…）")
    else:
        print(f"Access 应用已存在：{HOSTNAME}（复用）")

    policies = _call("GET", f"/accounts/{account_id}/access/apps/{app['id']}/policies", token)
    for p in policies.get("result") or []:
        if p.get("name") == "allow-customers":
            _call("DELETE", f"/accounts/{account_id}/access/apps/{app['id']}"
                           f"/policies/{p['id']}", token)
    _call("POST", f"/accounts/{account_id}/access/apps/{app['id']}/policies", token, {
        "name": "allow-customers", "decision": "allow",
        "include": [{"email": {"email": e}} for e in emails],
    })
    print(f"白名单策略已写入：{'、'.join(emails)}")
    print("  没在白名单里的访问者在**边缘**就被挡掉，看不到登录页。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cloudflare 隧道/域名/Access 一键配置")
    parser.add_argument("--status", action="store_true", help="只看 zone 状态")
    parser.add_argument("--all", action="store_true", help="建隧道+DNS+Access 并写入 .env")
    parser.add_argument("--run", action="store_true", help="用 .env 里的 token 起隧道")
    args = parser.parse_args()

    if args.run:
        tok = _env("CLOUDFLARE_TUNNEL_TOKEN")
        if not tok:
            raise SystemExit("× .env 里没有 CLOUDFLARE_TUNNEL_TOKEN（先跑 --all）")
        exe = os.path.join(os.environ.get("LOCALAPPDATA", ""), "cloudflared",
                           "cloudflared.exe")
        print(f"▶ 启动隧道（{exe} tunnel run --token …）")
        os.execv(exe, [exe, "tunnel", "--no-autoupdate", "run", "--token", tok])
        return

    token = _env("CLOUDFLARE_API_TOKEN")
    if not token:
        raise SystemExit(
            "× .env 里没有 CLOUDFLARE_API_TOKEN。\n"
            "  Cloudflare → My Profile → API Tokens → Create Custom Token，"
            "权限：Zone/DNS=Edit、Account/Cloudflare Tunnel=Edit、"
            "Account/Access: Apps and Policies=Edit")

    z = zone(token)
    if args.status:
        return
    if not args.all:
        print("\n（只查了状态；加 --all 才会真建隧道/DNS/Access）")
        return

    account_id = z["account"]["id"]
    tid, ttok = tunnel(token, account_id)
    ingress(token, account_id, tid)
    dns(token, z["id"], tid)
    emails = [e.strip() for e in
              _env("CLOUDFLARE_ACCESS_EMAILS").replace(";", ",").split(",") if e.strip()]
    if emails:
        access(token, account_id, emails)
    else:
        print("⚠️ .env 没设 CLOUDFLARE_ACCESS_EMAILS，跳过 Access 白名单")
    _write_env({"CLOUDFLARE_TUNNEL_ID": tid, "CLOUDFLARE_TUNNEL_TOKEN": ttok})
    print("\n下一步：python scripts\\cloudflare_setup.py --run")
    print(f"        然后访问 https://{HOSTNAME}")


if __name__ == "__main__":
    main()
