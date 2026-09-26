"""公网 pilot 端到端验证：情报接口 + 来源脱敏。

跑的是**真实公网地址**，不是本机 —— 因为要验的正是"客户打开浏览器
按下 F12 能看到什么"。

用法：
    .venv\\Scripts\\python.exe scripts\\_verify_intel_live.py
    .venv\\Scripts\\python.exe scripts\\_verify_intel_live.py <password>
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import sys
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://moss.wujiaitool.cn"
ACCOUNT = "admin"
PASSWORD = sys.argv[1] if len(sys.argv) > 1 else "MossPilot2026x"

# 公网用的是 Cloudflare 源证书，本机校验会失败 —— 这里显式放宽（仅本脚本）
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

CJ = http.cookiejar.CookieJar()
OP = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(CJ),
    urllib.request.HTTPSHandler(context=CTX))

#: Cloudflare 会对默认的 `Python-urllib/3.x` UA 直接返回 **error code: 1010**
#: （浏览器指纹拦截），连登录都到不了。用一个普通浏览器 UA 即可 ——
#: 这只是让请求"看起来像浏览器"，不绕过任何鉴权。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

FAILED: list[str] = []


def check(ok: bool, text: str) -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {text}")
    if not ok:
        FAILED.append(text)


def call(path: str, data: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data else None,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE + "/",
        })
    try:
        r = OP.open(req, timeout=60)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    print(f"目标：{BASE}")
    code, body = call("/api/v1/auth/login",
                      {"account": ACCOUNT, "password": PASSWORD})
    if code != 200:
        # ⚠️ 必须在这里就退出：登录失败时后面的断言全都无从谈起，
        # 继续跑只会打印一堆 FAIL，最后还可能打出"全部通过"（假的绿）。
        print(f"登录失败 {code}: {body[:200]}")
        check(False, f"登录（{code}）—— 后续检查无法进行")
        print(f"\n发现 {len(FAILED)} 个问题：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("  登录 OK")

    # ── 1) 情报接口可用 + kind 中文名 ──
    code, body = call("/api/v1/intel/feed?limit=200")
    check(code == 200, f"/intel/feed 状态 {code}")
    if code == 200:
        feed = json.loads(body)
        pairs = sorted({(i["kind"], i["kind_label"]) for i in feed["items"]})
        print("        kind → label:", pairs)
        bad = [k for k, l in pairs if k == l]
        check(not bad, f"kind_label 无机器名（可疑：{bad}）")
        # `degraded` 是**正常状态**不是故障：它表示"有来源这次没取满"
        # （实测：知识星球停机后首次回填会命中上限，下次继续追平）。
        # 所以这里只提示，不算失败 —— 把它当失败会让人去修一个不存在的问题。
        if feed.get("degraded"):
            msgs = [g.get("message") for g in feed.get("gaps", [])]
            print(f"        提示：数据不完整（正常，下次采集补齐）{msgs}")
        else:
            print("        数据完整（无缺口）")
        # 来源字段：只应有假名，不应有真名/链接
        blob = body
        for needle in ("source_url", "report_url", "group_id", "zsxq",
                       "eastmoney", "sina", "10jqka", "cctv"):
            check(needle not in blob, f"情报流响应不含 {needle!r}")

    # ── 2) 日历接口 + 预期差 ──
    code, body = call("/api/v1/intel/calendar?horizon_days=45")
    check(code == 200, f"/intel/calendar 状态 {code}")
    if code == 200:
        cal = json.loads(body)
        macro = [e for e in cal["events"] if e["kind"] == "macro"]
        states = sorted({e["metrics"].get("state") for e in macro})
        print(f"        宏观 {len(macro)} 条，状态集合 {states}")
        check(bool(macro), "日历含宏观事件")
        check(all(e["metrics"].get("note") is not None or
                  e["metrics"].get("expected") is not None
                  for e in macro),
              "宏观事件的数值缺失时有说明（不静默为空）")

    # ── 3) 旧告警接口：来源脱敏（本次修复的重点）──
    code, body = call("/api/v1/alerts?limit=20")
    check(code == 200, f"/alerts 状态 {code}")
    if code == 200:
        alerts = json.loads(body).get("alerts", [])
        print(f"        告警 {len(alerts)} 条")
        if alerts:
            k = sorted(alerts[0].keys())
            print("        字段:", k)
            # 管理员视图**应该**有真名（他要排障）—— 但**用户**视图不该有。
            # 这里登录的就是管理员，所以只断言"契约形状正确"。
            check("source_alias" in alerts[0] or "source_name" in alerts[0],
                  "告警带来源字段（假名或管理员真名）")
            for a in alerts:
                # 文本字段里的裸 URL 一律应被抹掉（管理员也一样）
                for f in ("description", "impact_path"):
                    v = str(a.get(f) or "")
                    check("http://" not in v and "https://" not in v,
                          f"{f} 无裸 URL")
    return 0


if __name__ == "__main__":
    rc = main()
    print("=" * 56)
    if FAILED:
        print(f"发现 {len(FAILED)} 个问题：")
        for f in FAILED:
            print("  -", f)
        raise SystemExit(1)
    print("全部通过")
    raise SystemExit(rc)
