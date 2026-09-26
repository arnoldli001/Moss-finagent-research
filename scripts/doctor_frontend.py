#!/usr/bin/env python
"""诊断前端请求为什么失败（Failed to fetch 类问题）。

## 为什么需要这个脚本

`Failed to fetch` 是浏览器在**拿不到任何 HTTP 响应**时给的笼统错误：
请求没发出、连接被拒、响应不是合法 HTTP、或页面 JS 在 fetch 之前就抛了
—— 这几种在界面上**长得一模一样**，而服务端日志里可能什么都没有。

本脚本从"服务端视角"把所有可能性逐个排除，剩下的就是前端问题：

  1. 服务是否在监听（不在 → 请求必然 Failed to fetch）
  2. 前端产物是否可访问、bundle 是否为最新构建
  3. 关键接口逐个实测（含管理台创建用户）
  4. 服务端是否有未捕获异常（看日志）

用法：
    .venv/Scripts/python.exe scripts/doctor_frontend.py
"""

from __future__ import annotations

import re
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8100"
DIST = ROOT / "web" / "dist"

problems: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def bad(msg: str, *, fix: str = "") -> None:
    problems.append(msg)
    print(f"  [BAD]  {msg}")
    if fix:
        print(f"         → 修复：{fix}")


def main() -> int:
    print("=" * 66)
    print("1) 端口是否在监听")
    print("=" * 66)
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", 8100))
        ok("127.0.0.1:8100 可以连接")
    except OSError as exc:
        bad(f"8100 连接失败（{exc}）—— 浏览器此时必然报 Failed to fetch",
            fix=r".\.venv\Scripts\python.exe manage.py start --daemon --replace")
        print()
        print("服务没起来，后面的检查没有意义。先启动服务再跑本脚本。")
        return 1
    finally:
        s.close()

    print()
    print("=" * 66)
    print("2) 前端产物")
    print("=" * 66)
    try:
        with httpx.Client(base_url=BASE, timeout=20.0) as c:
            r = c.get("/")
            ok(f"GET / = {r.status_code}")
            m = re.search(r'src="(/assets/index-[^"]+\.js)"', r.text)
            if not m:
                bad("index.html 里找不到 bundle 引用", fix="pnpm/npm run build")
            else:
                served = m.group(1)
                ok(f"服务托管的 bundle = {served}")
                built = {p.name for p in (DIST / "assets").glob("index-*.js")}
                if Path(served).name in built:
                    ok("该 bundle 就是 dist 里最新的那份")
                else:
                    bad(f"服务托管的 {Path(served).name} 不在 dist 里 "
                        f"（dist 有 {sorted(built)}）",
                        fix="重新 build 前端，然后硬刷新浏览器")
            if r.headers.get("content-type", "").startswith("text/html"):
                ok("返回的是 HTML（不是 JSON 错误页）")
            else:
                bad(f"GET / 的 Content-Type = {r.headers.get('content-type')}")
    except httpx.HTTPError as exc:
        bad(f"GET / 失败：{type(exc).__name__}: {exc}")
        return 1

    print()
    print("=" * 66)
    print("3) 关键接口可达性（未登录也能看的那些）")
    print("=" * 66)
    with httpx.Client(base_url=BASE, timeout=20.0) as c:
        for path, expect in (("/api/v1/auth/login-mode", {200}),
                             ("/api/v1/auth/captcha", {200}),
                             ("/api/v1/me/features", {401}),
                             ("/api/v1/admin/users", {401})):
            try:
                r = c.get(path)
                if r.status_code in expect:
                    ok(f"GET {path} = {r.status_code}（符合预期）")
                else:
                    bad(f"GET {path} = {r.status_code}，预期 {expect}")
            except httpx.HTTPError as exc:
                bad(f"GET {path} 异常：{type(exc).__name__}: {exc}")

    print()
    print("=" * 66)
    print("4) 服务端近期是否有未捕获异常")
    print("=" * 66)
    for name in ("backend-dev.log", "backend.log"):
        path = ROOT / "data" / "run" / name
        if not path.exists():
            continue
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        tracebacks = [ln for ln in tail if "Traceback (most recent call last)" in ln]
        if tracebacks:
            bad(f"{name} 近期有 {len(tracebacks)} 处 Traceback（见该文件）")
        else:
            ok(f"{name} 近期无 Traceback")

    print()
    print("=" * 66)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("未发现服务端问题。")
    print()
    print("如果浏览器仍报 Failed to fetch，说明请求**根本没离开浏览器**：")
    print("  · 打开 F12 → Network，点一次「创建」，看有没有那条请求；")
    print("  · 若没有 → 页面 JS 在 fetch 之前就抛了，看 Console 的红色报错；")
    print("  · 若有且标红 (failed) → 把鼠标悬停在红色状态上，看具体原因。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
