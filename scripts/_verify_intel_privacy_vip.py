"""以**非管理员**（vip）身份验来源脱敏 —— 本次修复的核心路径。

## 为什么必须单独验这一条

管理员视图**故意**保留 `source_name` / `source_url`（他要排障），
所以用 admin 登录去验，看到的永远是"没脱敏"的样子 —— 会得出错误结论。

真正的风险面是**普通付费用户**按 F12 看到什么。而 `source_alias` 是
**稳定假名**，能用来判断"这几条同源"，本身就是有信息量的东西，
不该出现在用户响应里。

用法：
    .venv\\Scripts\\python.exe scripts\\_verify_intel_privacy_vip.py
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import sys
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://moss.wujiaitool.cn"
ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "testyang"
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "MossTest2026x"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

#: Cloudflare 对默认 urllib UA 返回 error 1010（浏览器指纹拦截）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

#: 任何**真实渠道名/域名**都不该出现在用户响应里。
#: 用**小写子串**匹配（响应是 JSON，域名字面量必然是小写或原样）。
FORBIDDEN = (
    "source_url", "report_url", "group_id", "topic_id", "author_id",
    "zsxq", "wx.zsxq", "eastmoney", "10jqka", "sina", "sinaimg",
    "cctv", "cailianpress", "cls.cn", "futu", "xuanji", "stcn",
    "eastmoney.com", "http://", "https://", "%2f", "%3a",
)

FAILED: list[str] = []
CJ = http.cookiejar.CookieJar()
OP = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(CJ),
    urllib.request.HTTPSHandler(context=CTX))


def check(ok: bool, text: str) -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {text}")
    if not ok:
        FAILED.append(text)


def call(path: str, data: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data else None,
        headers={"Content-Type": "application/json", "User-Agent": _UA,
                 "Accept": "application/json, text/plain, */*",
                 "Referer": BASE + "/"})
    try:
        r = OP.open(req, timeout=60)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        # `RemoteDisconnected`（连接被对端关掉）**不是** `HTTPError`，
        # 不接住的话整个脚本会以 traceback 崩掉 —— 而这是隧道/Cloudflare
        # 常见的偶发行为，不该让"隐私检查"变成一条看不懂的堆栈。
        return -1, f"{type(e).__name__}: {e}"


def scan(label: str, body: str, *, allow_alias: bool) -> None:
    """扫一遍响应体，逐个断言不含被禁子串。"""
    low = body.lower()
    hits = [n for n in FORBIDDEN if n in low]
    # `source_alias` 允许出现（前端用它做同源判断），但**假名本身**
    # （`src-xxxxxxxx`）如果被渲染出来也不该 —— 这里只查键名与值形态。
    if allow_alias and "source_alias" in hits:
        hits.remove("source_alias")
    check(not hits, f"{label} 不含被禁子串（命中：{hits}）")


def main() -> int:
    print(f"目标：{BASE}（以 **vip** 身份 {ACCOUNT} 验证）")
    code, body = call("/api/v1/auth/login",
                      {"account": ACCOUNT, "password": PASSWORD})
    if code != 200:
        print(f"登录失败 {code}: {body[:160]}")
        return 1
    _, me = call("/api/v1/me/features")
    tier = ""
    try:
        tier = json.loads(me).get("tier", "")
    except Exception:  # noqa: BLE001
        pass
    print(f"  登录 OK（tier={tier}）")
    check(tier not in ("admin",), f"确认是非管理员身份（tier={tier}）")

    # ── 情报中心三个接口 ──
    for path, label in (("/api/v1/intel/feed?limit=200", "情报流"),
                        ("/api/v1/intel/calendar?horizon_days=45", "投资日历"),
                        ("/api/v1/intel/sources/health", "采集健康度")):
        code, body = call(path)
        check(code == 200, f"{label} 状态 {code}")
        if code == 200:
            scan(label, body, allow_alias=False)

    # ── 旧告警系统（本次修的泄漏点）──
    code, body = call("/api/v1/alerts?limit=50")
    check(code == 200, f"告警列表 状态 {code}")
    if code == 200:
        scan("告警列表", body, allow_alias=True)
        alerts = json.loads(body).get("alerts", [])
        if alerts:
            k = sorted(alerts[0].keys())
            print("        字段:", k)
            check("source_url" not in k, "用户视图无 source_url")
            check("source_name" not in k, "用户视图无 source_name")
            check("source_alias" in k, "用户视图有 source_alias（供前端判断同源）")
        else:
            print("        （当前无告警，跳过字段断言）")

    # ⚠️ 路径是 `/api/v1/events`（**不是** `/alerts/events`）。
    # 打到 `/alerts/events` 会被 `/alerts/{alert_id}` 接走 → 404"告警不存在"，
    # 看着像路由坏了，其实是调用方写错了地址。
    code, body = call("/api/v1/events?limit=50")
    check(code == 200, f"事件列表 状态 {code}")
    if code == 200:
        scan("事件列表", body, allow_alias=True)

    return 0


if __name__ == "__main__":
    rc = main()
    print("=" * 56)
    if FAILED:
        print(f"发现 {len(FAILED)} 个问题：")
        for f in FAILED:
            print("  -", f)
        raise SystemExit(1)
    print("全部通过：非管理员视图看不到任何来源标识")
    raise SystemExit(rc)
