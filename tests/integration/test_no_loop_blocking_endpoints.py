"""判据：**没有哪个 API 端点会把事件循环按住不放**（`CHG-0146`）。

## 它守的是什么

FastAPI 的语义决定了这是一类**结构性**缺陷：

* `def`（同步处理器）→ Starlette **自动丢线程池**，不堵循环；
* `async def` 里做同步重活（读 SQLite、聚合、`time.sleep`、算 pandas）
  → **直接按住整个事件循环** ⇒ 同一时刻所有人的请求一起排队 ——
  这正是用户看到的「前端显示后端不可达」（探针 4 秒超时）。

实测（2026-09-30，`loop_lag` + 在飞点名 + 进程内 harness 三次收敛）：

| 端点 | 修前堵循环 | 修后 |
|---|---|---|
| `/api/v1/auction_select/sentiment_cycle` | **3,056.8 ms** | 80.1 ms |
| `/api/v1/mainline/data/status`（冷） | **3,766.7 ms** | 12.4 ms |
| `/api/v1/mainline/relevance` | 679.1 ms | 15.2 ms |
| `/api/v1/auction_select/preheat` | 179.7 ms | 12.2 ms |

## 为什么不能靠"看 latency_ms 排序"

`access_audit.latency_ms` 排第一的是 `/research/{task_id}`（p50 8.3 秒），
而它只是**内存字典查表** —— 它慢是因为研究期间前端**轮询它最频繁**，
于是它总在队列里，把**别人的阻塞**记在自己账上。
⇒ 必须量"**这个请求存活期间事件循环最长一次没被调度多久**"（本文件的 `gap_max_ms`），
它与 `latency_ms` 不是一回事。

## 量法（进程内，同一个事件循环）

用 `httpx.ASGITransport` 把 app 装进**本进程**，发请求的同时用 20 ms 心跳采样：
handler 里任何同步重活都会让心跳**直接漏拍**。这是确定性的 —— 不靠线上偶遇。

## 覆盖面（诚实登记）

* 只测 **WATCHED** 里那些"曾经慢过"的端点（全量 107 条要跑几分钟，不适合当门禁）；
  全量扫描用 `MOSS_LOOPBLOCK_ALL=1 pytest … -s` 打开（会打印整张表）；
* 阈值 250 ms 是**经验值**：修后实测最坏 167 ms（一个 debug 端点，不在清单里），
  清单内 ≤ 140 ms；取 250 ms 留了余量又足以抓住"又有人在 handler 里读库"；
* 无参 GET 才进全量扫描（带必填参数的路径会 422，量不到真实成本）。
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

#: 常跑集合：**已修的那四个** + 一批快端点（合计约 9 秒）。
#: ⚠️ 已修的那四条必须常跑 —— 它们是**回归锁**（谁把它们改回同步调用，这里就红）。
FAST_WATCHED: tuple[str, ...] = (
    "/api/v1/auction_select/sentiment_cycle",     # 修前堵 3,056.8 ms
    "/api/v1/mainline/data/status",               # 修前冷路径堵 3,766.7 ms
    "/api/v1/mainline/relevance",                 # 修前堵 679.1 ms
    "/api/v1/auction_select/preheat",             # 修前堵 179.7 ms
    "/api/v1/mainline/alert-returns",
    "/api/v1/mainline/futures",
    "/api/v1/mainline/refresh_status",
    "/api/v1/sector_crowding/config_list",
    "/api/v1/intel/calendar",
    "/api/v1/intel/feed",
    "/api/v1/intraday/snapshot",
    "/api/v1/health",
    "/api/v1/metrics",
)

#: 重量级端点：**已经**走 `asyncio.to_thread`（`mainline/snapshot` 一次 14 秒、
#: `quant/data-status` 一次 10 秒），常跑会把门禁拖到 4 分钟 ⇒ 默认不测，
#: 用 `MOSS_LOOPBLOCK_ALL=1` 时**必须**一起测（它们是同一条判据的另一半）。
HEAVY_WATCHED: tuple[str, ...] = (
    "/api/v1/mainline/snapshot",
    "/api/v1/quant/data-status",
    "/api/v1/quant/factors",
    "/api/v1/fundflow/snapshot",
    "/api/v1/intraday/daily",
)

#: 单个端点允许的最大"堵循环"毫秒（见模块 docstring 的取值依据）
THRESHOLD_MS = 250.0
TICK = 0.02


async def _measure(client, path: str, *, timeout: float = 25.0) -> dict:
    """打一次请求，同时量循环空档（`gap_max_ms` = 它按住循环的最长时间）。"""
    gaps: list[float] = []
    t0 = time.perf_counter()
    task = asyncio.create_task(client.get(path, timeout=timeout))
    while not task.done():
        s = time.perf_counter()
        await asyncio.sleep(TICK)
        gaps.append(max(0.0, (time.perf_counter() - s - TICK) * 1000.0))
        if time.perf_counter() - t0 > timeout:
            task.cancel()
            break
    status: object
    try:
        status = (await task).status_code
    except asyncio.CancelledError:
        status = "TIMEOUT"
    except BaseException as exc:  # noqa: BLE001 端点自己报错不是本判据的事
        status = f"ERR {type(exc).__name__}"
    return {"path": path, "status": status,
            "gap_max_ms": round(max(gaps) if gaps else 0.0, 1),
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1)}


@pytest.mark.asyncio
async def test_no_watched_endpoint_blocks_the_event_loop() -> None:
    """★ 常跑集合里每个端点的「堵循环」都必须小于阈值。

    成本实测：import 1.8 s + 装配 0.1 s + 这 13 条约 9 s（`data/_cost_breakdown.py`）。
    `MOSS_LOOPBLOCK_ALL=1` 时把重量级与其余全部无参 GET 一起测（几分钟，供定期体检）。
    """
    import httpx

    from src.api.main import app

    rows: list[dict] = []
    paths = list(FAST_WATCHED)
    if os.environ.get("MOSS_LOOPBLOCK_ALL") == "1":
        spec = app.openapi()
        extras = [p for p, ops in sorted(spec.get("paths", {}).items())
                  if "{" not in p and ops.get("get")
                  and not [q for q in ops["get"].get("parameters", [])
                           if q.get("required")]]
        paths = list(dict.fromkeys([*FAST_WATCHED, *HEAVY_WATCHED, *extras]))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                base_url="http://loopblock.test") as client:
        await _measure(client, "/api/v1/health/live", timeout=60)   # 预热装配
        for path in paths:
            rows.append(await _measure(client, path))

    worst = sorted(rows, key=lambda r: -r["gap_max_ms"])[:6]
    detail = "\n  ".join(
        f"{r['gap_max_ms']:>8.1f} ms（总 {r['total_ms']:>8.0f} ms）  {r['path']}"
        for r in worst)
    offenders = [r for r in rows if r["gap_max_ms"] >= THRESHOLD_MS]
    assert not offenders, (
        f"有端点在事件循环上做同步重活（阈值 {THRESHOLD_MS:.0f} ms）：\n  "
        + "\n  ".join(f"{r['gap_max_ms']:.0f} ms  {r['path']}" for r in offenders)
        + "\n\n最坏 6 条：\n  " + detail
        + "\n\n修法：把同步段搬进 `await asyncio.to_thread(...)`（仓库既有范式），"
          "见 `src/api/routes/auction_select.py` 的 `build_cycle` 与 "
          "`src/api/routes/mainline.py` 的 `_relevance_stats_sync`。")
    print("\n[堵循环] 最坏 6 条：\n  " + detail)


@pytest.mark.asyncio
async def test_the_measurement_can_actually_detect_blocking() -> None:
    """**自证**：这套量法必须能抓到"同步重活"——否则它是一条永远绿的假护栏。

    造一个"故意堵 400 ms"的 ASGI app，用同一个 `_measure` 去量：
    它必须报出 ≥ 300 ms。第一版判据若把 `asyncio.sleep`（让出循环）当成阻塞，
    这里也会露馅 —— 所以顺带断言"纯 `await sleep` **不**算阻塞"。
    """
    import httpx

    async def _blocking_app(scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            return
        time.sleep(0.4)                       # 同步睡 = 真堵
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    async def _yielding_app(scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            return
        await asyncio.sleep(0.4)              # 让出循环 = 不算堵
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_blocking_app),
            base_url="http://blocking.test") as client:
        blocked = await _measure(client, "/x", timeout=10)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_yielding_app),
            base_url="http://yielding.test") as client:
        yielded = await _measure(client, "/x", timeout=10)

    assert blocked["gap_max_ms"] >= 300, (
        f"量法抓不到同步阻塞 ⇒ 判据是假护栏：{blocked}")
    assert yielded["gap_max_ms"] < 120, (
        "把「让出循环」也当成阻塞 ⇒ 判据会大面积假红：" + str(yielded))
