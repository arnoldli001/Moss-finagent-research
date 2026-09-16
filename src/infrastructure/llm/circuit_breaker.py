"""时间窗口三态熔断器（CLOSED/OPEN/HALF_OPEN）。

保护外部LLM API（DeepSeek）免受连续故障冲击：
- CLOSED → 正常放行，记录时间窗口内失败次数
- OPEN  → 熔断中，直接拒绝请求（跳过API调用，立即降级到备模型）
- HALF_OPEN → 探测阶段，放行少量请求验证恢复

触发条件：failure_window_sec 内失败次数 ≥ failure_threshold
恢复条件：OPEN 保持 recovery_cooldown_sec 后 → HALF_OPEN；
         HALF_OPEN 连续 half_open_success_needed 次成功 → CLOSED；
         HALF_OPEN 任意 1 次失败 → 立即回 OPEN。

参考：moss-finance-assistant governance/guardrails/circuit_breaker.py
简化：去掉Actor模型桥接（单进程Demo不需要），保留核心三态状态机。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class CircuitState:
    """熔断器运行时状态快照。"""

    name: str
    state: str = "CLOSED"  # CLOSED / OPEN / HALF_OPEN
    last_failure_ts: float = 0.0
    last_state_change_ts: float = field(default_factory=time.time)
    half_open_successes: int = 0
    total_failures: int = 0
    total_successes: int = 0
    total_rejected: int = 0


class TimeWindowCircuitBreaker:
    """单被保护对象的三态熔断器。"""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        failure_window_sec: float = 60.0,
        recovery_cooldown_sec: float = 30.0,
        half_open_success_needed: int = 2,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.failure_window_sec = failure_window_sec
        self.recovery_cooldown_sec = recovery_cooldown_sec
        self.half_open_success_needed = half_open_success_needed
        self._state = CircuitState(name=name)
        self._failure_ts: deque[float] = deque()
        self._lock = threading.Lock()

    def allow_request(self) -> bool:
        """是否允许请求通过。False=被熔断拒绝，应立即降级。"""
        with self._lock:
            now = time.time()
            if self._state.state == "CLOSED":
                return True
            if self._state.state == "OPEN":
                if now - self._state.last_failure_ts >= self.recovery_cooldown_sec:
                    self._transition("HALF_OPEN", "cooldown elapsed, probing")
                    return True
                self._state.total_rejected += 1
                return False
            # HALF_OPEN
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state.total_successes += 1
            if self._state.state == "HALF_OPEN":
                self._state.half_open_successes += 1
                if self._state.half_open_successes >= self.half_open_success_needed:
                    self._transition("CLOSED", "probe succeeded, healthy")
                    self._failure_ts.clear()

    def record_failure(self) -> None:
        with self._lock:
            now = time.time()
            self._state.total_failures += 1
            self._state.last_failure_ts = now
            self._failure_ts.append(now)
            if self._state.state == "HALF_OPEN":
                self._transition("OPEN", "probe failed")
                return
            if self._state.state == "CLOSED":
                self._gc_failure_window(now)
                if len(self._failure_ts) >= self.failure_threshold:
                    self._transition(
                        "OPEN",
                        f"{len(self._failure_ts)} failures within "
                        f"{self.failure_window_sec}s",
                    )

    def _gc_failure_window(self, now: float) -> None:
        threshold = now - self.failure_window_sec
        while self._failure_ts and self._failure_ts[0] < threshold:
            self._failure_ts.popleft()

    def _transition(self, new_state: str, reason: str = "") -> None:
        old = self._state.state
        self._state.state = new_state
        self._state.last_state_change_ts = time.time()
        if new_state == "HALF_OPEN":
            self._state.half_open_successes = 0
        if old == "HALF_OPEN" and new_state == "CLOSED":
            self._failure_ts.clear()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = time.time()
            self._gc_failure_window(now)
            return {
                "name": self._state.name,
                "state": self._state.state,
                "failures_in_window": len(self._failure_ts),
                "failure_threshold": self.failure_threshold,
                "total_failures": self._state.total_failures,
                "total_successes": self._state.total_successes,
                "total_rejected": self._state.total_rejected,
                "uptime_sec": round(now - self._state.last_state_change_ts, 1),
            }


class CircuitBreakerRegistry:
    """熔断器注册中心：按 provider name 隔离。"""

    _DEFAULTS = {
        "deepseek": {
            "failure_threshold": 3,
            "failure_window_sec": 60.0,
            "recovery_cooldown_sec": 30.0,
            "half_open_success_needed": 2,
        },
        "ollama": {
            "failure_threshold": 5,
            "failure_window_sec": 60.0,
            "recovery_cooldown_sec": 15.0,
            "half_open_success_needed": 1,
        },
    }

    def __init__(self) -> None:
        self._breakers: dict[str, TimeWindowCircuitBreaker] = {}

    def get_or_create(self, name: str) -> TimeWindowCircuitBreaker:
        if name not in self._breakers:
            cfg = self._DEFAULTS.get(name, self._DEFAULTS["deepseek"])
            self._breakers[name] = TimeWindowCircuitBreaker(name=name, **cfg)
        return self._breakers[name]

    def snapshot_all(self) -> dict[str, dict[str, object]]:
        return {n: cb.snapshot() for n, cb in self._breakers.items()}


_registry: CircuitBreakerRegistry | None = None


def get_circuit_registry() -> CircuitBreakerRegistry:
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry
