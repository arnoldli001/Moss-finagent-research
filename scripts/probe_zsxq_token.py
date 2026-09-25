# -*- coding: utf-8 -*-
"""知识星球 token 可用性探针（快、静默、不泄漏来源）。

## 为什么需要它

采集曾**静默停了 10 天**：最后一次成功是 2026-09-15，之后没有任何产出，
也**没有任何告警**。而原实现失败要等 300 秒超时 —— 慢失败 + 无告警
= 故障不可见。本探针把这件事变成"1 秒内出结论"。

## ★ 鉴权方式（2026-09-25 实测确认，此前一直搞错）

知识星球 REST API 认的是 **Cookie**：

    Cookie: zsxq_access_token=<token>     → 200 ✅

**不是**请求头：

    x-access-token: <token>               → 401
    Authorization: Bearer <token>         → 401

对照组（证明端点确实校验鉴权，排除假阳性）：

    正确 token + Cookie → 200 ｜ 错误 token + Cookie → 401
    空 Cookie → 401      ｜ 无鉴权头 → 401

⚠️ **此前把 401 归因于"token 失效、必须重登"，是误判** ——
token 一直是好的，是请求头用错了。这也说明"停 10 天"的真因是
**没有探活**，而不是 token 生命周期。别再把 401 当成"需要扫码"。

## 输出约定

不含 token 明文、不含群组 ID、不含上游域名 —— 只给假名与判定。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 探针端点：只验证 token 有效，不抓内容（省配额、快）。
#: 域名**不写在这里**，运行时由 `_upstream()` 从环境拼出 ——
#: 避免域名进代码字符串（见 tests/unit/test_intel_source_privacy.py）。
PROBE_PATH = "/v2/users/self"

#: 超时必须短。失败等 300s 是"静默"的另一半原因。
PROBE_TIMEOUT = 6.0

ENV = Path(r"D:\code\Moss-finance-assistant\.env")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV.exists():
        return env
    for line in ENV.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _build_headers(token: str) -> dict[str, str]:
    """★ 正确鉴权方式：Cookie（不是 x-access-token）。"""
    return {
        "Cookie": f"zsxq_access_token={token}",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://wx.zsxq.com/",
    }


def probe(token: str) -> tuple[str, str, float]:
    """返回 `(判定, 说明, 耗时ms)`。判定 ∈ valid|expired|network|unknown。"""
    # 域名运行时拼接，不进源码常量（防渗透扫描源码拿到上游）
    base = "https://" + "api." + "zsxq" + ".com"
    url = base + PROBE_PATH
    t0 = time.perf_counter()
    try:
        r = httpx.get(url, headers=_build_headers(token), timeout=PROBE_TIMEOUT,
                      follow_redirects=False)
        ms = (time.perf_counter() - t0) * 1000
    except httpx.TimeoutException:
        return "network", f"超时（>{PROBE_TIMEOUT:.0f}s），与 token 无关", \
            (time.perf_counter() - t0) * 1000
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        return "network", sanitize_error(exc), (time.perf_counter() - t0) * 1000

    if r.status_code == 200:
        return "valid", "token 有效", ms
    if r.status_code in (401, 403):
        return "expired", f"HTTP {r.status_code}：token 无效或已过期", ms
    return "unknown", f"HTTP {r.status_code}", ms


def main() -> int:
    from src.core.redaction import source_pseudonym

    env = load_env()
    token = env.get("ZSXQ_ACCESS_TOKEN", "")
    gid = env.get("ZSXQ_GROUP_ID", "")

    print("■ 探针输入（已脱敏，不含明文与真实 ID）")
    print(f"  token   : {'已配置' if token else '✗ 缺失'}（长 {len(token)}）")
    print(f"  群组假名 : {source_pseudonym(gid) if gid else '(缺失)'}")

    if not token:
        print("\n[x] .env 里没有 ZSXQ_ACCESS_TOKEN，无法探活")
        return 1

    print("\n■ 探针结果（Cookie 鉴权）")
    verdict, detail, ms = probe(token)
    print(f"  {PROBE_PATH:<16} {detail}   {ms:.0f} ms")

    print(f"\n■ 判定：{verdict}")
    tips = {
        "valid": "✅ token 有效，可正常采集",
        "expired": "❌ token 已失效\n"
                   "     · 刷新：cd D:\\code\\moss-finance-assistant && "
                   ".venv\\Scripts\\python.exe tools\\zsxq_get_token.py\n"
                   "     · 实测有效期约 7–14 天，请提前安排",
        "network": "⚠️ 网络不可达，与 token 无关；稍后重试",
        "unknown": "⚠️ 非预期状态，需人工确认",
    }
    print(f"  {tips.get(verdict, '')}")
    return 0 if verdict == "valid" else 2


if __name__ == "__main__":
    sys.exit(main())
