"""外部主机的 TCP 可达性预检（快速失败）。

## 为什么需要它（2026-09-26 实测）

CME FedWatch 的库（`cme_fedwatch`）内部用 pycurl 连 `www.cmegroup.com`。
本机实测该主机 **TCP 443 不通**（DNS 解析得到 108.160.165.211，属被阻断网段），
于是调用要等满 **21.3 秒**才抛 `curl: (28) Failed to connect`。
而 A01 用 `asyncio.gather` 并发采集 —— 整条数据采集阶段的墙钟被这一个
指标拖到 **23.4 秒**。

关键问题：**这个失败是确定性的**（主机根本连不上），却每次都付满超时。
失败冷却（`source_cooldown.py`）只在**首次**之后生效，首次仍要等 23 秒；
启动预探把首次挪到后台，但预探与首个请求之间仍有窗口。

## 做法

在真正发请求前，先做一个**独立于业务库**的 TCP 连接探测，超时 1~2 秒。
连不上就立即判定"该主机不可达"，不进入那个 21 秒的等待。

与"失败冷却"的分工：
  · **预检**回答"这台主机现在通不通"（网络层，秒级，每次调用前都可做）
  · **冷却**回答"这个源刚才失败过，别再打"（应用层，跨调用记忆）

两者叠加后，不可达主机的代价从 23.4s 降到 ~1s；而主机恢复时预检会放行，
不会像纯冷却那样把恢复后的源冷落整个窗口。

## 为什么不用 DNS 判断

DNS **能解析出 IP**（`108.160.165.211`）但仍然连不上 —— 所以"解析成功"
不代表可达。必须真的做一次 TCP connect。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

logger = logging.getLogger(__name__)

#: 默认预检超时（秒）。取 2 秒：局域网/正常公网握手远快于此，
#: 而"被阻断"会一直等到超时 —— 我们只想等很短就跑。
DEFAULT_PROBE_TIMEOUT = 2.0

#: 预检结论的缓存时长（秒）：同一次分析里同一主机会被问多次，
#: 不该每次都重探。60 秒与失败冷却同量级。
_PROBE_TTL = 60.0

#: host:port → (探测时刻 monotonic, 是否可达)
_cache: dict[tuple[str, int], tuple[float, bool]] = {}
_lock = threading.Lock()


def probe_tcp_sync(host: str, port: int = 443,
                   timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
    """同步 TCP 预检（带结论缓存）。True = 能建立连接。

    **绝不抛**：任何异常都视为"不可达" —— 预检失败只该让调用方跳过这一跳，
    不该把一个网络问题升级成业务异常。
    """
    key = (host, port)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _PROBE_TTL:
            return hit[1]

    ok = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False

    with _lock:
        _cache[key] = (now, ok)
    if not ok:
        logger.info("TCP 预检失败：%s:%d 不可达（缓存 %.0fs，避免后续超时等待）",
                    host, port, _PROBE_TTL)
    return ok


async def probe_tcp(host: str, port: int = 443,
                    timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
    """异步包装：探测走线程池，不阻塞事件循环。"""
    return await asyncio.to_thread(probe_tcp_sync, host, port, timeout)


def clear_probe_cache() -> None:
    """清空预检缓存（测试用 / 手动恢复探测）。"""
    with _lock:
        _cache.clear()


def snapshot() -> dict[str, bool]:
    """当前已知的主机可达性（供 /health 观测）。"""
    with _lock:
        return {f"{h}:{p}": ok for (h, p), (_t, ok) in sorted(_cache.items())}


__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "clear_probe_cache",
    "probe_tcp",
    "probe_tcp_sync",
    "snapshot",
]
