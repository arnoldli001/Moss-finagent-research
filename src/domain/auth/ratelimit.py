"""按 IP 的尝试限流（防脚本高频尝试登录/注册）。

对应设计：§8.6.7（安全要点）。

## 为什么必须**按 IP**，而不是只按账号

系统已有的防护是**按账号**的错误锁定（同一账号连错 N 次 → 锁定）。
它拦得住"盯着一个账号猛试"，但拦不住**密码喷洒**：

    一个常见密码 → 试 10 万个账号 → 每个账号只试 1 次。

账号维度永远不触发，而攻击者只要千分之一命中率就有收益。
IP 维度才看得见"同一来源在短时间内大量尝试"这个特征。

## 与图形码的分工（两层，缺一不可）

| 层 | 拦什么 | 用户可见成本 |
|---|---|---|
| IP 限流 | 高频尝试（脚本、撞库） | 正常用户无感 |
| 图形码 | 自动化脚本（解不出图） | 仅在**已经失败过**时才要求 |

所以正常用户第一次登录**不会**看到图形码 —— 只有在他失败过、
或该 IP 有异常频率时才出现。这是"安全与体验"的折中：
永远弹图形码会让每天登录的人烦，而完全不弹等于没防。

## ⚠️ 进程内存储，多副本需换 Redis

与图形码同样的已知约束（见 `human_check.py` 的模块文档）：
多副本时每个实例各算一份计数，实际阈值会变成 N 倍。
迁多副本前把这里换成 Redis 计数器。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 统计窗口（秒）：在这个窗口内累计尝试次数。
DEFAULT_WINDOW_SECONDS = 300.0

#: 窗口内允许的**失败**次数上限。超过即要求图形码 / 拒绝。
#:
#: 取 8：正常用户手滑 1~2 次很常见，8 次留足余量；
#: 而脚本要撞库，8 次/5 分钟/每 IP 的速率低到不划算
#: （10 万账号需要 6 万个 IP·小时）。
DEFAULT_MAX_FAILURES = 8

#: 窗口内允许的**请求**总数上限（含成功）。防"用正确密码刷接口"。
DEFAULT_MAX_REQUESTS = 60

#: 超限后要求图形码的持续时长（从最后一次失败算起）。
CAPTCHA_GRACE_SECONDS = 900.0


@dataclass
class _Bucket:
    """一个 IP 的计数器。"""

    failures: list[float] = field(default_factory=list)
    requests: list[float] = field(default_factory=list)
    #: 最后一次失败的时间（决定要不要图形码）
    last_failure: float = 0.0

    def trim(self, now: float, window: float) -> None:
        cutoff = now - window
        self.failures = [t for t in self.failures if t >= cutoff]
        self.requests = [t for t in self.requests if t >= cutoff]


@dataclass(frozen=True)
class LimitDecision:
    """限流判定结果。"""

    allowed: bool
    #: 是否需要通过图形码才能继续
    require_captcha: bool
    #: 可给用户看的说明（拒绝时）
    reason: str = ""
    #: 剩余等待秒数（拒绝时）
    retry_after: int = 0


class IpRateLimiter:
    """按 IP 的滑动窗口限流（含"失败后要求图形码"的状态）。"""

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_failures: int = DEFAULT_MAX_FAILURES,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        captcha_grace: float = CAPTCHA_GRACE_SECONDS,
    ) -> None:
        self._window = float(window_seconds)
        self._max_failures = int(max_failures)
        self._max_requests = int(max_requests)
        self._captcha_grace = float(captcha_grace)
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    # ---------------- 判定 ----------------

    def check(self, ip: str) -> LimitDecision:
        """请求进来时判定：是否放行、是否要求图形码。

        ⚠️ **必须在验密之前调用**：撞库的密码绝大多数是错的，
        如果先验密再限流，等于每次都白跑一次昂贵的哈希校验
        （argon2/pbkdf2 是**故意**设计成慢的，那正是 DoS 面）。
        """
        if not ip:
            # 拿不到 IP 时不拦（否则会把所有人挡掉）。
            # 反向代理没配好时会出现这种情况 —— 那是部署问题，
            # 应该在 §7.5 的 Cloudflare/Caddy 配置里解决，而不是在这里猜。
            return LimitDecision(allowed=True, require_captcha=False)
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(ip)
            if bucket is None:
                return LimitDecision(allowed=True, require_captcha=False)
            bucket.trim(now, self._window)
            if len(bucket.requests) >= self._max_requests:
                return LimitDecision(
                    allowed=False, require_captcha=True,
                    reason="请求过于频繁，请稍后再试",
                    retry_after=int(self._window))
            need = (len(bucket.failures) >= self._max_failures
                    or (bucket.last_failure
                        and now - bucket.last_failure < self._captcha_grace
                        and len(bucket.failures) >= max(1, self._max_failures // 2)))
            return LimitDecision(allowed=True, require_captcha=bool(need))

    # ---------------- 记账 ----------------

    def note_request(self, ip: str) -> None:
        """记一次请求（**无论成败都记**）——用于总量限流。"""
        if not ip:
            return
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            self._buckets.setdefault(ip, _Bucket()).requests.append(now)

    def note_failure(self, ip: str) -> None:
        """记一次**认证失败**（密码错 / 账号不存在）。"""
        if not ip:
            return
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            bucket = self._buckets.setdefault(ip, _Bucket())
            bucket.failures.append(now)
            bucket.last_failure = now
            if len(bucket.failures) >= self._max_failures:
                logger.warning("IP %s 在 %.0f 秒内认证失败 %d 次（已要求图形码）",
                               ip, self._window, len(bucket.failures))

    def note_success(self, ip: str) -> None:
        """记一次成功登录。

        **刻意只清"失败计数"，不清"请求计数"**：
        成功说明这大概率是本人，不该再让他看图形码；但请求总量仍要计着，
        否则"拿一个能登的账号狂刷接口"就没人管了。
        """
        if not ip:
            return
        with self._lock:
            bucket = self._buckets.get(ip)
            if bucket is not None:
                bucket.failures.clear()
                bucket.last_failure = 0.0

    # ---------------- 维护 ----------------

    def _prune_locked(self, now: float) -> None:
        """清掉窗口内已无记录的 IP（防内存随扫描流量无界增长）。"""
        for ip in list(self._buckets):
            bucket = self._buckets[ip]
            bucket.trim(now, self._window)
            if not bucket.failures and not bucket.requests:
                self._buckets.pop(ip, None)

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            return {
                "tracked_ips": len(self._buckets),
                "window_seconds": int(self._window),
                "max_failures": self._max_failures,
                "max_requests": self._max_requests,
                "captcha_grace_seconds": int(self._captcha_grace),
                "blocked_ips": sorted(
                    ip for ip, b in self._buckets.items()
                    if len(b.failures) >= self._max_failures),
            }

    def reset(self) -> None:
        """清空（**仅测试用**）。"""
        with self._lock:
            self._buckets.clear()


# ======================================================================
# 进程内单例
# ======================================================================

_LIMITER: IpRateLimiter | None = None


def get_ip_rate_limiter() -> IpRateLimiter:
    global _LIMITER  # noqa: PLW0603 进程内单例（多副本注意事项见模块文档）
    if _LIMITER is None:
        _LIMITER = IpRateLimiter()
    return _LIMITER


def reset_ip_rate_limiter() -> None:
    """清掉单例（**仅测试用**）。"""
    global _LIMITER  # noqa: PLW0603
    _LIMITER = None


__all__ = [
    "CAPTCHA_GRACE_SECONDS",
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_WINDOW_SECONDS",
    "IpRateLimiter",
    "LimitDecision",
    "get_ip_rate_limiter",
    "reset_ip_rate_limiter",
]
