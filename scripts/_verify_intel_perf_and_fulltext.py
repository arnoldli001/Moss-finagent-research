"""临时验证脚本：全文端点（TestClient 真实请求）+ build_feed 冷热耗时。

⚠️ 这是排障用的探针（`_` 前缀与仓库里既有的 `scripts/_verify_*.py` 同口径），
不是产品代码：它**只读**，不改任何存储。

跑的判据：
  ① 从 `data/intel/item_bodies.jsonl` 里取一个**真实存在**的 hash，
     走完整的 HTTP 栈（`TestClient` + 真路由 + 真中间件）拿全文 ——
     "读路径通不通"不能靠读代码断言。
  ② `build_feed` 冷/热耗时 + `/feed` 端点冷/热耗时（缓存有没有真的生效）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BODIES = ROOT / "data" / "intel" / "item_bodies.jsonl"


def pick_real_hash() -> tuple[str, str]:
    """从真实存储里取一条（返回 hash 与正文前 40 字，供人工核对）。"""
    if not BODIES.exists():
        raise SystemExit(f"存储不存在：{BODIES}")
    rows = [json.loads(s) for s in BODIES.read_text(
        encoding="utf-8").splitlines() if s.strip()]
    # 挑一条正文最长的（最能证明"点开看到的是全文，不是展示摘要"）
    rows.sort(key=lambda r: len(str(r.get("text") or "")), reverse=True)
    r = rows[0]
    return str(r["content_hash"]), str(r["text"])[:40]


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes import intel as INTEL_ROUTE

    # 试点是登录门控的，dev 下中间件放行；这里再替掉功能闸门
    # （`require_feature` 直接调用、不是 Depends，所以只能替模块属性）。
    async def _open_feature(request, feature):        # noqa: ANN001
        return ("probe", "pro")

    INTEL_ROUTE.require_feature = _open_feature

    app = FastAPI()
    app.include_router(INTEL_ROUTE.router)

    h, head = pick_real_hash()
    print(f"[取样本] content_hash={h!r} 正文前 40 字={head!r}")

    with TestClient(app) as c:
        # ── ① 全文端点：真实 HTTP 请求 ──
        t0 = time.monotonic()
        resp = c.get(f"/api/v1/intel/item/{h}")
        dt = (time.monotonic() - t0) * 1000
        print(f"[全文端点] HTTP {resp.status_code} in {dt:.0f}ms")
        print(f"[全文端点] 响应体 {resp.text[:200]}")
        if resp.status_code == 200:
            body = resp.json()
            print(f"[全文端点] text 长度 = {len(body.get('text') or '')}")
            print(f"[全文端点] 键 = {sorted(body)}")

        # ── ② 404 分支：形态合法但没存过 ──
        r404 = c.get("/api/v1/intel/item/deadbeefdeadbeef")
        print(f"[404 分支] HTTP {r404.status_code} {r404.text[:120]}")

        # ── ③ /feed：冷（后台重建中）/ 热（缓存命中）/ 显式刷新 ──
        t0 = time.monotonic()
        f1 = c.get("/api/v1/intel/feed?limit=60")
        cold = time.monotonic() - t0
        b1 = f1.json()
        print(f"[/feed 冷] {cold:.3f}s HTTP {f1.status_code} "
              f"refreshing={b1.get('refreshing')} items={len(b1.get('items') or [])}")

        if b1.get("refreshing"):
            for _ in range(20):
                time.sleep(0.5)
                st = c.get("/api/v1/intel/feed/status").json()
                if not st["building"] and st["seq"] > 0:
                    print(f"[/feed 后台建完] status={st}")
                    break

        t0 = time.monotonic()
        f2 = c.get("/api/v1/intel/feed?limit=60")
        warm = time.monotonic() - t0
        b2 = f2.json()
        print(f"[/feed 热] {warm:.3f}s HTTP {f2.status_code} "
              f"refreshing={b2.get('refreshing')} cached={b2.get('cached')} "
              f"seq={b2.get('seq')} items={len(b2.get('items') or [])} "
              f"age={b2.get('age_seconds')}")

        t0 = time.monotonic()
        f3 = c.get("/api/v1/intel/feed?limit=60&refresh=true")
        rf = time.monotonic() - t0
        b3 = f3.json()
        print(f"[/feed refresh=true] {rf:.3f}s HTTP {f3.status_code} "
              f"refreshing={b3.get('refreshing')} seq={b3.get('seq')}")

        t0 = time.monotonic()
        c.get("/api/v1/intel/feed/status")
        print(f"[/feed/status] {(time.monotonic() - t0) * 1000:.0f}ms")

    # ── ④ build_feed 本身（不走端点）──
    import asyncio

    from src.domain.intel.service import build_feed

    t0 = time.monotonic()
    feed1 = asyncio.run(build_feed(limit=60))
    bf_cold = time.monotonic() - t0
    t0 = time.monotonic()
    feed2 = asyncio.run(build_feed(limit=60))
    bf_warm = time.monotonic() - t0
    print(f"[build_feed] 冷 {bf_cold:.2f}s｜热 {bf_warm:.2f}s"
          f"｜items={len(feed1.items)}/{len(feed2.items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
