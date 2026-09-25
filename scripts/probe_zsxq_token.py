# -*- coding: utf-8 -*-
"""实测：知识星球 token 可用性探针。

要点：探针必须**快**（当前实现失败要等 300s，这才是"静默停 10 天"的帮凶）。
只读，不写任何数据。探针输出**不含任何来源标识**。
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

# ── 探针候选端点：越靠前越"轻" ──
# 目标：只验证 token 有效，不抓内容（省配额、快）
PROBES = [
    ("users/self", "https://api.zsxq.com/v2/users/self"),
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    f = Path(r"D:\code\moss-finance-assistant\.env")
    if not f.exists():
        return env
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    from src.core.redaction import redact, source_pseudonym  # noqa: E402

    env = load_env()
    token = env.get("ZSXQ_ACCESS_TOKEN", "")
    gid = env.get("ZSXQ_GROUP_ID", "")

    print("■ 探针输入（已脱敏）")
    print(f"  token : {redact('zsxq_access_token=' + token)[:60] if token else '(缺失)'}")
    print(f"  group : {source_pseudonym(gid) if gid else '(缺失)'}")
    print(f"  伪名说明：真实 group_id 不进日志，只留稳定假名用于归因")

    if not token:
        print("\n[x] 无 token，无法探活")
        return 1

    headers = {
        "x-access-token": token,
        "x-version": "2.77.0",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://wx.zsxq.com",
        "Referer": "https://wx.zsxq.com/",
    }

    print("\n■ 探针结果")
    verdict = ""
    for name, url in PROBES:
        t0 = time.perf_counter()
        try:
            # ⚠️ 超时必须短：失败等 300s 是"静默"的另一半原因
            r = httpx.get(url, headers=headers, timeout=6.0)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  {name:<14} HTTP {r.status_code}  {ms:.0f} ms")
            if r.status_code == 200:
                verdict = "valid"
            elif r.status_code in (401, 403):
                verdict = "expired"
            else:
                verdict = f"unexpected_{r.status_code}"
        except httpx.TimeoutException:
            print(f"  {name:<14} 超时（>6s）→ 网络问题，非 token 问题")
            verdict = "network"
        except Exception as e:
            print(f"  {name:<14} 异常 {type(e).__name__}: {str(e)[:70]}")
            verdict = "network"

    print(f"\n■ 判定：{verdict}")
    tips = {
        "valid": "✅ token 有效，可正常采集",
        "expired": "❌ token 已失效 → 需要重新登录一次刷新\n"
                   "     命令：.venv/Scripts/python.exe tools/zsxq_get_token.py",
        "network": "⚠️ 网络不可达，与 token 无关；稍后重试",
    }
    print(f"  {tips.get(verdict, '⚠️ 非预期状态，需人工确认')}")
    return 0 if verdict == "valid" else 2


if __name__ == "__main__":
    sys.exit(main())
