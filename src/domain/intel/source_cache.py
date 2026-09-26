"""情报流**源级缓存**：把"取数"与"组装"拆开，让不同筛选档复用同一份取数结果。

## 为什么需要它（2026-09-25 性能剖析）

实测 `build_feed` 一次完整重建 **2.6~3.3 秒**，拆开看：

    zsxq 增量分页（4 次网络请求）      ~1230 ms   ← 最大头
    fetch_all（6 快讯源 + 8 研报路）    ~508 ms
    组装/打分/分组/高亮（356 条）        ~174 ms   ← CPU 只占 7%

**慢的是网络，不是计算。** 而 `build_feed` 原本**每次调用都重新取数**，
于是两个放大效应：

1. **换筛选档 = 重新全量取数**。接口按 `limit|codes|sort|filter` 分缓存键
   （`api/routes/intel.py`），而 `filter` 有多个档、`sort` 有两个 ——
   每换一次档就是一次 4 次 zsxq 分页 + 8 次研报 + 6 路快讯。
   用户来回切档会反复重付完全相同的网络成本。
2. **首次访问（冷启动）必然吃满一次全量取数**。

本模块把"取数结果"按几十秒 TTL 缓存下来，**取数一次、多档复用**：

    首次任何档        → 真取数（2.6s）
    其余档 / 60 秒内  → 0 网络（<0.2s）

## 三条硬约束（写代码时最容易违反的地方）

**① 只缓存数据，不缓存副作用。**
`item_store.persist`（留存落库）与 `save_watermark`（推进水位线）**只允许在
真取数时执行一次**。缓存命中时若也执行：前者是重复写库，后者更危险 ——
水位线语义是"这个区间已处理完"，凭空推进会让**内容永久丢失**
（`zsxq_incremental` 的注释专门记过这条）。所以 `get_or_fetch` 明确返回
`fresh` 标志，调用方据此决定要不要跑副作用。

**② 失败与"上游空页"不进缓存。**
否则一次限频/抖动会被缓存几十秒，用户看到的是"这个来源停了"。
只有拿到数据的响应才落缓存。

**③ 并发同一个键只取一次（单飞）。**
接口层已有单飞，但那只覆盖"同一个缓存键"。这里覆盖"同一个取数键"，
所以换档的并发请求也不会各拉一遍。

## 与接口层缓存的关系

两层缓存解决的问题不同，都需要：

| 层 | 键 | 作用 |
|---|---|---|
| 接口层（`routes/intel.py`） | `limit\\|codes\\|sort\\|filter` | 同一个档**连点/轮询**不重算 |
| **本层（源级）** | `watch\\|policy_date` | **不同档之间**复用取数结果 |

本层 TTL 取得比接口层（60s）短一些（默认 45s），这样"新内容进来"的延迟
不会叠加成两倍。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: 源级缓存有效期（秒）。
#:
#: 依据：情报内容的最小更新节奏是**分钟级**（快讯源分钟级、知识星球游标
#: 2 小时一轮、研报一天几条）。45 秒内不可能有真正的新内容，所以复用
#: 不会让用户错过任何东西；而它比接口层 TTL（60s）短，避免"新内容到达"
#: 被两层 TTL 叠加放大。
DEFAULT_TTL = 45.0

#: 缓存条目上限。键是 `(watch, policy_date)` 组合，正常只有一两个；
#: 上限只为防"参数枚举"把内存撑满（与 `mainline._SNAPSHOT_CACHE` 同策略）。
MAX_ENTRIES = 12

#: 单飞等待上限（秒）。等超了就让调用方自己去取 —— 宁可多付一次取数，
#: 也不要让请求永久挂住。
_COALESCE_WAIT = 60.0

#: `{(key): (写入时刻, 数据)}`。值是**纯 dict/list**（JSON 可序列化形状），
#: 不是 dataclass —— 缓存里存活对象会让"谁改了它"变得无法追查。
_cache: dict[str, tuple[float, Any]] = {}

#: 正在取数的键 → 完成事件（单飞）。
_inflight: dict[str, asyncio.Event] = {}


def reset() -> None:
    """清空缓存与单飞状态（测试用；也让"刷新"能强制重取）。"""
    _cache.clear()
    _inflight.clear()


def stats() -> dict[str, Any]:
    """缓存状态（供 `/feed/status` 之类观测；零 I/O）。"""
    now = time.monotonic()
    with_item = [(k, now - at) for k, (at, _v) in _cache.items()]
    return {
        "entries": len(_cache),
        "keys": [k for k, _ in with_item],
        "ages": {k: round(age, 1) for k, age in with_item},
        "ttl": DEFAULT_TTL,
        "inflight": list(_inflight),
    }


def _fresh(key: str, ttl: float) -> Any | None:
    """取缓存值；没有或已过期返回 `None`。

    ⚠️ `ttl <= 0` 一律视为**缓存关闭**（永远返回 `None`），而不是"永不过期"。
    这两种语义完全相反，写反的后果是"想临时关缓存却变成永久缓存" ——
    而那是最难查的一类问题（看起来一切正常，只是数据永不更新）。
    """
    if ttl <= 0:
        return None
    entry = _cache.get(key)
    if entry is None:
        return None
    at, value = entry
    if (time.monotonic() - at) >= ttl:
        return None
    return value


def _store(key: str, value: Any) -> None:
    if len(_cache) >= MAX_ENTRIES:
        # 整体清空而不是 LRU：与项目里其它小缓存同一策略（简单、无隐藏状态）。
        _cache.clear()
    _cache[key] = (time.monotonic(), value)


async def get_or_fetch(
    key: str,
    fetch: Callable[[], Awaitable[Any]],
    *,
    ttl: float = DEFAULT_TTL,
    cacheable: Callable[[Any], bool] | None = None,
) -> tuple[Any, bool]:
    """取 `key` 的缓存；没有或过期则调用 `fetch` 取一次。

    Args:
        ttl: 有效期（秒）。`<= 0` = **关闭缓存**（每次真取数）——
            给"强制刷新"留一条路，也便于排查"是不是缓存的锅"。
        cacheable: 结果能不能进缓存。默认都进；
            `build_feed` 传 `lambda p: bool(p.get("pubs"))` —— 空结果/失败
            不进缓存，否则一次上游抖动会被缓存几十秒，用户看到的是
            "这个来源停了"。

    Returns:
        `(数据, fresh)` —— `fresh=True` 表示**这次真的取数了**，
        调用方据此决定要不要执行"只该跑一次"的副作用
        （留存落库 / 推进水位线，见模块 docstring ①）。
    """
    hit = _fresh(key, ttl)
    if hit is not None:
        return hit, False

    # ---- 单飞：同键已有请求在取数，就等它，不重复打上游 ----
    event = _inflight.get(key)
    if event is not None:
        try:
            await asyncio.wait_for(event.wait(), timeout=_COALESCE_WAIT)
        except asyncio.TimeoutError:
            logger.warning("源级缓存单飞等待超时（key=%s），改为自行取数", key)
        else:
            hit = _fresh(key, ttl)
            if hit is not None:
                return hit, False
        # 等到了但没数据（对端失败/不可缓存）→ 自己也取一次

    event = asyncio.Event()
    _inflight[key] = event
    try:
        value = await fetch()
    finally:
        _inflight.pop(key, None)
        event.set()

    if cacheable is None or cacheable(value):
        _store(key, value)
    return value, True


__all__ = [
    "DEFAULT_TTL",
    "MAX_ENTRIES",
    "get_or_fetch",
    "reset",
    "stats",
]
