"""幂等键存储（写操作"重试不重复执行"的最小实现）。

## 为什么需要它

浏览器不会自动重试非幂等请求（POST/PATCH/PUT/DELETE）—— 这是对的，因为
"创建用户"跑两次就真的建了两个。但**网络层失败**有两种完全不同的含义：

1. 请求**没有到达**服务端（连接被关闭、代理截断、进程刚重启）；
2. 请求**到达并已执行**，只是响应在回程丢了。

客户端无法区分这两者。所以只要网络层报错就自动重试，会带来"重复开号"；
完全不重试，用户就只能对着 `Failed to fetch` 反复手点 —— 而这同样会重复。

幂等键把判断权交回**服务端**：客户端对一次"用户意图"生成一个随机键，
重试时复用同一个键；服务端见过这个键就直接回放上次的结果，不重新执行。
这是 Stripe / PayPal / AWS 对写接口的标准做法。

## 边界（必须如实说明，别当成分布式方案）

- 存储在**本进程内存**里。单进程部署（本项目的现状：
  `uvicorn src.api.main:app` 单 worker）完全够用。
- 一旦多 worker / 多副本，命中率按副本数摊薄，**必须换成 Redis**
  （`SET key value NX EX ttl` + 执行中标记），接口不变。
- 进程重启后已缓存的键消失 —— 这只会退化成"可能重复执行一次"，
  不会造成错误结果：本项目所有写接口都有数据库唯一约束兜底
  （用户名唯一、池/自选股唯一），重复执行表现为一条可读的 409/400。

## 为什么键要按"主体"再命名一次空间

幂等键是**客户端**提供的字符串。如果不加主体前缀，A 用户猜到/枚举到
B 用户的键，就能读到 B 的响应体（本项目创建用户的响应里含**初始密码**）。
所以键一律拼成 `scope:principal:client_key`，跨主体不可命中。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

#: 客户端键的合法形态。设上限是**安全**要求而非风格：键来自请求，
#: 不限制长度/字符集等于让匿名请求往内存里写任意内容。
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")

#: 与前端 `X-Idempotency-Key` 头对应。
HEADER_NAME = "X-Idempotency-Key"

#: 结果保留时长。窗口太短则"用户过一会儿点重试"仍会重复执行；
#: 太长则白占内存。15 分钟覆盖了"看到报错 → 手动重试"的正常节奏。
DEFAULT_TTL_SECONDS = 900.0

#: 内存上限。超过后按**最旧的先淘汰**（插入序），避免无限增长。
DEFAULT_MAX_ENTRIES = 4096

#: 重复请求等待"首个请求出结果"的最长时间。
#: 必须大于一次开号的最坏耗时（实测 ~1.2s，含 600k 轮 pbkdf2），
#: 否则重试会绕过等待、变成真正的并发执行。
DEFAULT_WAIT_SECONDS = 30.0


def is_valid_key(key: str) -> bool:
    """键是否合法（非法键**忽略**而不是报错：见 `sanitize` 的说明）。"""
    return bool(key) and KEY_PATTERN.match(key) is not None


def sanitize(key: str) -> str:
    """把任意输入收敛成"合法键或空串"。

    为什么非法键是"忽略"而不是 400：幂等键是**可选**的优化，
    不是业务参数。客户端（或中间代理）多带了一个畸形键，
    不应该让一次本来能成功的开号失败 —— 那正是我们要修的症状。
    没有键 = 不做幂等保护 = 退回改动前的行为。
    """
    key = (key or "").strip()
    return key if is_valid_key(key) else ""


def build_key(scope: str, principal: str, client_key: str) -> str:
    """拼出**跨主体不可碰撞**的存储键。"""
    return f"{scope}:{principal}:{client_key}"


class _Entry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class IdempotencyStore:
    """线程安全的 TTL + 插入序淘汰的键值存储，外带"执行中"占位。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._wait = float(wait_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._in_flight: set[str] = set()
        self._hits = 0
        self._misses = 0
        self._claimed = 0
        self._replayed = 0

    # ---- 基本读写 ----------------------------------------------------

    def get(self, key: str) -> tuple[bool, Any]:
        """读已完成的缓存结果 → `(found, value)`。"""
        if not key:
            return False, None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return False, None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                self._misses += 1
                return False, None
            self._hits += 1
            return True, entry.value

    def put(self, key: str, value: Any) -> None:
        """写入结果，并解除"执行中"占位。"""
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            self._entries[key] = _Entry(value, now + self._ttl)
            self._in_flight.discard(key)
            self._evict_locked(now)

    def claim(self, key: str) -> bool:
        """尝试成为该键的**执行者**。

        返回 True：没有缓存结果、也没有别的请求在执行 → 你去执行。
        返回 False：要么已有结果（直接读缓存），要么另一个请求在执行中
        （调用方应等待，见 `wait`）。
        """
        if not key:
            return True
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > now:
                return False
            if entry is not None:
                self._entries.pop(key, None)
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            self._claimed += 1
            return True

    def release(self, key: str) -> None:
        """执行失败/抛异常时**必须**释放占位。

        否则一次失败的请求会让这个键在 TTL 内永久"执行中"，
        用户的重试全部被挡在等待里 —— 那是比重复执行更糟的故障。
        """
        if not key:
            return
        with self._lock:
            self._in_flight.discard(key)
            self._evict_locked(time.monotonic())

    def wait(self, key: str) -> tuple[bool, Any]:
        """同步等待执行者写入结果（给非 async 调用方用）。"""
        if not key:
            return False, None
        deadline = time.monotonic() + self._wait
        while time.monotonic() < deadline:
            found, value = self.get(key)
            if found:
                with self._lock:
                    self._replayed += 1
                return True, value
            with self._lock:
                if key not in self._in_flight:
                    # 执行者已经收工却没写结果 → 它失败了，别再等
                    return False, None
            time.sleep(0.05)
        return False, None

    # ---- 维护 --------------------------------------------------------

    def _evict_locked(self, now: float) -> None:
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for k in expired:
            self._entries.pop(k, None)
        overflow = len(self._entries) - self._max
        if overflow > 0:
            # dict 保持插入序 → 前 overflow 个就是最旧的
            for k in list(self._entries)[:overflow]:
                self._entries.pop(k, None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "in_flight": len(self._in_flight),
                "hits": self._hits,
                "misses": self._misses,
                "claimed": self._claimed,
                "replayed": self._replayed,
                "ttl_seconds": self._ttl,
                "max_entries": self._max,
                "backend": "in-process-memory",
                "note": ("单进程内存实现；多 worker/多副本时必须换 Redis，"
                         "否则命中率按副本数摊薄"),
            }

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._in_flight.clear()
            self._hits = self._misses = self._claimed = self._replayed = 0


_store: IdempotencyStore | None = None
_store_lock = threading.Lock()


def get_idempotency_store() -> IdempotencyStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = IdempotencyStore()
        return _store


def reset_idempotency_store() -> None:
    """测试用：清空单例内容（不换实例，避免持有旧引用的调用方失联）。"""
    get_idempotency_store().reset()


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_WAIT_SECONDS",
    "HEADER_NAME",
    "KEY_PATTERN",
    "IdempotencyStore",
    "build_key",
    "get_idempotency_store",
    "is_valid_key",
    "reset_idempotency_store",
    "sanitize",
]
