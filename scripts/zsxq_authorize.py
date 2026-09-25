#!/usr/bin/env python
"""知识星球授权刷新 —— **一次扫码，自动写入，免重启生效**。

## 用法

    .venv/Scripts/python.exe scripts/zsxq_authorize.py

跑完就好，**不需要重启后端**。

## 它做了什么

    ① 弹出浏览器 → 你扫码/确认登录（最长 5 分钟）
    ② 自动从 cookie 提取 zsxq_access_token
    ③ 写入热加载凭证 data/credentials/zsxq.json（权限收到仅当前用户）
    ④ 立即探活验证（8 秒超时，出结论）
    ⑤ 打印状态 + 距下次需要刷新的天数

第 ③ 步是关键：后端读凭证是**每次调用重新读盘**（见
`src/infrastructure/credentials.py`），所以写完即生效 ——
`.env` 只在进程启动时读一次，改它必须重启，这正是本脚本不走 `.env` 的原因。

## 为什么需要它（真实故障）

采集曾**静默停 10 天**：最后一次成功 2026-09-15，之后无产出也无告警。
现在有了探针 + 本脚本，流程变成：

    管理员看到"授权将于 N 天后到期" → 跑本脚本 → 扫码 → 完事

## 谁能跑

需要浏览器和服务器访问权，**因此只能在服务器本机跑**，不从界面提供
（界面上给一个"重新授权"按钮等于给所有管理员一个开浏览器的能力，
且无法在无桌面会话下工作）。

## 依赖

需要 Playwright。本项目的 venv **没有**装它（为省 150 MB），
脚本会**自动回落到 assistant 项目的 venv**（那里有）。
两处都没有时给出明确安装指引，而不是抛 ImportError。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

LOGIN_URL = "https://wx.zsxq.com"
LOGIN_TIMEOUT_SEC = 300
COOKIE_DOMAIN = "https://wx.zsxq.com"

#: 有 Playwright 的备用 venv（本项目的 venv 刻意不装，省 150 MB）
FALLBACK_PYTHONS = (
    Path(r"D:\code\moss-finance-assistant\.venv\Scripts\python.exe"),
)


def _has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _reexec_with_fallback() -> int:
    """本解释器没有 Playwright → 找有它的解释器，把本脚本重跑一遍。"""
    for py in FALLBACK_PYTHONS:
        if not py.exists():
            continue
        probe = subprocess.run(
            [str(py), "-c", "import playwright"],
            capture_output=True, timeout=60)
        if probe.returncode == 0:
            print(f"[环境] 本 venv 无 Playwright，改用：{py}")
            return subprocess.run(
                [str(py), str(Path(__file__).resolve()), *sys.argv[1:]],
                timeout=LOGIN_TIMEOUT_SEC + 120).returncode
    print("[x] 找不到带 Playwright 的解释器。安装其一即可：")
    print(f"    {ROOT}\\.venv\\Scripts\\python.exe -m pip install playwright")
    print(f"    {ROOT}\\.venv\\Scripts\\python.exe -m playwright install chromium")
    return 3


def _probe(token: str) -> tuple[str, str]:
    """就地探活。返回 `(判定, 说明)`。复用主连接器的正确鉴权方式。"""
    import httpx

    host = "https://" + "api." + "zsxq" + ".com"
    try:
        r = httpx.get(host + "/v2/users/self", timeout=8.0, headers={
            "Cookie": f"zsxq_access_token={token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://wx.zsxq.com/",
        })
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        return "network", sanitize_error(exc)
    if r.status_code == 200:
        return "valid", "token 有效"
    if r.status_code in (401, 403):
        return "expired", f"HTTP {r.status_code}"
    return "unknown", f"HTTP {r.status_code}"


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新知识星球授权（一次扫码）")
    ap.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_SEC,
                    help="等待登录的秒数（默认 300）")
    ap.add_argument("--probe-only", action="store_true",
                    help="只探活当前凭证，不打开浏览器")
    args = ap.parse_args()

    from src.infrastructure.credentials import ZSXQ_FILE, load, save, status

    # ── 只探活模式：看当前状态 ──
    if args.probe_only:
        st = status(ZSXQ_FILE)
        print(f"凭证状态：{st['state']}  来源={st['source'] or '(无)'}  "
              f"刷新于={st['refreshed_at'] or '(未知)'}  "
              f"已用={st['age_days']}天")
        cred = load(ZSXQ_FILE)
        if cred is None:
            print("[!] 没有热加载凭证；后端会回落 .env")
            return 1
        verdict, detail = _probe(cred.value)
        print(f"探活：{verdict}  {detail}")
        return 0 if verdict == "valid" else 2

    if not _has_playwright():
        return _reexec_with_fallback()

    from playwright.sync_api import sync_playwright

    print("=" * 66)
    print("知识星球授权刷新")
    print("=" * 66)
    print("即将打开浏览器，请完成扫码/登录（最长 "
          f"{args.timeout // 60} 分钟）…\n")

    token = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            cookies = {c["name"]: c["value"]
                       for c in context.cookies(COOKIE_DOMAIN)}
            token = cookies.get("zsxq_access_token", "")
            # 已登录判定：拿到 token 且不在登录页
            if token and "/login" not in page.url:
                break
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:  # noqa: BLE001 超时是常态，继续轮询
                pass
            time.sleep(1.5)
        browser.close()

    if not token:
        print("\n[x] 超时未检测到登录。请重跑本脚本。")
        return 1

    # ── 写入热加载凭证（后端下次调用即生效，无需重启）──
    path = save(ZSXQ_FILE, token, note="zsxq_authorize.py 扫码刷新")
    print(f"\n[✓] 凭证已写入：{path}")
    print("    后端**无需重启** —— 读取方每次调用重新读盘")

    # ── 立即验证 ──
    print("\n[验证] 探活中…")
    verdict, detail = _probe(token)
    print(f"    {verdict}  {detail}")
    if verdict != "valid":
        print("\n[!] 写入成功但探活未通过 —— 请检查网络或重跑一次。")
        return 2

    st = status(ZSXQ_FILE)
    print(f"\n[状态] {st['state']}  刷新于 {st['refreshed_at']}")
    print("       实测有效期 7–14 天；到第 5 天管理员界面会开始提醒。")
    print("\n下次需要时，重跑：")
    print(f"    {ROOT}\\.venv\\Scripts\\python.exe scripts\\zsxq_authorize.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
