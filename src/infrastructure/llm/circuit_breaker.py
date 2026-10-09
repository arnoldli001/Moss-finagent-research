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

## ★ 隔离维度是 `provider × 租户`，不是 `provider`

缺陷（`src/api/routes/research.py` @107-109 有实测教训注释）：桶按 provider 分，
于是**一个用户的突发流量把所有人打到 circuit_open** —— deepseek 60s 内 3 次
失败即 OPEN，此后**每个租户**的请求连 API 都不试、全部降级到备模型。
失败是按**请求方**累积的，却按**提供商**生效，两边不匹配。

### 取舍一：**不**做「租户桶 + 全局桶」双闸

「deepseek 整体挂了，要不要让所有人都立刻快速失败」——**不**。理由都能在
本仓库里指到：

1. 双闸会**原样复现**这次缺陷：AND 语义下全局桶单独就能否决，
   于是 A 的 3 次失败又变成所有人的快速失败 —— 等于白改。
2. 想避开它就得给全局桶配**另一套阈值**，那是第二份熔断配置，
   与「配置只写一遍」（`_DEFAULTS` 按 provider 取）直接冲突。
3. 另一侧本来就有兜底，代价**有界**：真·整体故障时，每个桶各自累计
   `failure_threshold` 次失败后同样 OPEN（deepseek 3 次/60s），差别只是
   「**新租户要重新踩一遍坑**」—— 最多 3 次注定失败的尝试，而这几次尝试
   本来就在降级链上（`gateway.LLMGateway.complete` 的 for 循环），
   用户拿到的是备模型的答案，不是错误。
4. 「提供商限流 / 额度耗尽」这个**确实是全局**的信号，已经有跨租户护栏：
   `rate_limit_guard`（按模型名判、落盘、`is_locked` 直接跳过该跳，
   见 `gateway.py` @502-509）。再叠一个全局熔断桶属于重复设施。

代价如实登记：整体故障时**没有**「秒级全网快速失败」，而是「每个活跃租户
各自 3 次失败后快速失败」。换来的是：一个租户的失败**永远不会**否决另一个
租户本来会成功的请求 —— 拿备模型答案换真答案，比多试 3 次更贵。

### 取舍二：取不到租户 ⇒ 退回 **provider 级**桶，不是「不熔断」

没有租户身份的是后台作业 / 脚本 / 定时任务（`accounting.current()` 为空且
`is_authenticated()` 为假）—— 它们恰恰是批量烧配额的那一类
（`mainline_relevance` 实测 484 元），必须继续被同一条桶挡住。
退化方向是**保守侧**：全局桶汇总所有无身份调用方的失败 ⇒ 只会更快 OPEN。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final

logger = logging.getLogger(__name__)

#: 桶 key 里 provider 与租户的分隔符。**只有 `breaker_key` / `breaker_provider`
#: 用到它** —— 放一处定义，避免格式漂移（本项目实测过"同一 key 写在 3 处，
#: 只改一处"的事故）。
_KEY_SEP: Final[str] = ":"

#: 无租户身份时的作用域标记。它**只进快照，不进 key** —— 理由见 `breaker_key`。
GLOBAL_SCOPE: Final[str] = "global"

# ============================================================================
# ★ 判定客户端的熔断策略（`RerankClient` / `EmbeddingClient` **共用这一份**）
#
# 为什么单独一套、不复用 `_DEFAULTS` 里的 provider 参数：那套是给**生成**路径的。
# 生成失败还有降级链（`gateway.complete` 的 for 循环）兜底，判定失败只是
# "这一次语义未命中"，两者对**误熔断**的容忍度不同，阈值不该共用一个数。
#
# 为什么放在这里而不是各自的客户端里：两个客户端要的是**同一条策略**。
# 写在两处就是「同一判断两份实现」—— 实测代价：`RerankClient` 有熔断、
# `EmbeddingClient` 没有，于是端点变成网络黑洞（不是 402 那种立即返回的错误）时，
# 一次缓存查找要等满 **1.5 + 3.0 = 4.5 s**；一次投研分析有 4~15 次查找
# ⇒ 改后最坏 **18~69 s** 只花在缓存上（改前只有 embedding，1.5 s ⇒ 6~23 s）。
# **改后把最坏情况放大了 3 倍，而且 embedding 侧永不恢复**（每次都重付）。
# ============================================================================

#: 连续失败多少次后熔断。
JUDGE_FAILURES_TO_OPEN: Final[int] = 3

#: 熔断后的冷却时长（秒）。一次投研分析里缓存查找的间隔是秒级，
#: 60 s 足够覆盖"这一波抖动"，又不会让一个已恢复的端点被冷落一整轮演示。
JUDGE_COOLDOWN_SEC: Final[float] = 60.0

#: 失败计数窗口（秒）。取值**远大于**连续失败的实际间隔即可 ——
#: 配合 `reset_on_success=True`，语义就是"**连续**失败"
#: （成功会把窗口清空，见 `record_success`）。不取 `inf` 是为了让
#: `_failure_ts` 有界：熔断后每个冷却周期最多再追加 1 条。
JUDGE_FAILURE_WINDOW_SEC: Final[float] = 600.0


def judge_breaker(name: str) -> TimeWindowCircuitBreaker:
    """建一个**判定客户端**用的熔断器（`RerankClient` / `EmbeddingClient` 共用）。

    `name` 只进快照（`"rerank"` / `"embed"`）—— 两个客户端的桶**必须分开**：
    合桶的话"rerank 挂了"会顺手把 embedding 也熔断掉，
    而那正是 `_recall_and_judge` 用来兜底的那一层（fail-open 链会整条失效）。

    `half_open_success_needed=1`：判定层**误熔断的代价**是"语义层这段不工作"，
    而它的兜底是 3-gram/阈值（仍有答案，只是差些）⇒ 宁可从宽恢复。
    这与生成路径取 2 的取舍方向相反，理由就是兜底成本不同。
    """
    return TimeWindowCircuitBreaker(
        name=str(name),
        failure_threshold=JUDGE_FAILURES_TO_OPEN,
        failure_window_sec=JUDGE_FAILURE_WINDOW_SEC,
        recovery_cooldown_sec=JUDGE_COOLDOWN_SEC,
        half_open_success_needed=1,
        reset_on_success=True,
    )


def breaker_key(provider: str, tenant: str = "") -> str:
    """熔断桶 key 的**唯一构造点**：`provider` 或 `provider:tenant`。

    ## 为什么无租户时是裸 provider，而不是 `provider:global`

    `deepseek:global` 看起来更自证，但它是**新字形**，会当场撞两处既有事实：

    1. `network_fallback._sources_snapshot()` 把 `snapshot_all()` 的 key
       原样交回 `get_or_create()`（`network_fallback.py` @1481-1489）——
       已注册的运行期桶字形必须稳定；
    2. 既有判据按 provider 级字形读（`tests/unit/test_circuit_breaker.py`
       断言 `"deepseek" in snapshot_all()`）。

    而且 provider 级 key 本来就是这一维缺失时**该有**的语义。
    「这是全局桶」由快照的 `scope=global` / `tenant=""` 如实标出 ——
    运维页与 `/health` 读的正是快照。

    `tenant` 为空串 ⇒ provider 级桶（后台作业 / 脚本 / 定时任务共用）。
    """
    name = str(provider or "").strip()
    scope = str(tenant or "").strip()
    return f"{name}{_KEY_SEP}{scope}" if scope else name


def breaker_provider(key: str) -> str:
    """从桶 key 反解 provider —— **只用于查 `_DEFAULTS`**。

    租户只多一维 key，**不改变熔断配置**：`deepseek:a` 与 `deepseek:b`
    必须拿到同一份 deepseek 阈值。所以取默认值前要把租户那一维切掉；
    否则每个租户一份阈值 = 配置写两遍，改一处漏一处。

    ⚠️ 这里**不能**用同一招反解租户：注册表里还有 `fallback:<源名>`
    这类 key（`network_fallback.py` @138），把 `<源名>` 当租户读是错的。
    租户只由**调用方**如实传入（见 `for_call`）。
    """
    return str(key or "").split(_KEY_SEP, 1)[0]


def caller_tenant_id() -> str:
    """**调用发生时**的租户 id；取不到就空串（不抛、不编默认租户）。

    ⚠️ 这里**不新造**取租户的方式：口径与 LLM 审计逐字一致 —— 直接复用
    `audit._resolve_identity`（**会话身份 > tenancy Principal**，
    见 `src/infrastructure/llm/audit.py` @53-71）。两份口径必须同源：
    审计把一次调用记在租户 T 名下、熔断却按租户 U 分桶的话，
    排障时两个界面会互相矛盾，而且没人能从任一侧看出问题。

    为什么 catch 住异常退回空串：熔断是**护栏**，不是功能 —— 取身份失败
    不该让"钱已经花了"的调用整体失败。退回 provider 级桶是保守侧
    （桶更大 ⇒ 更快 OPEN），不会静默放行，见模块 docstring 取舍二。
    """
    try:
        from src.core import accounting
        from src.infrastructure.llm.audit import _resolve_identity

        tenant_id, _user_id, _source = _resolve_identity(accounting.current())
        return str(tenant_id or "")
    except Exception:  # noqa: BLE001 护栏不因取身份失败而中断 LLM 调用
        logger.debug("熔断器取租户身份失败（退回 provider 级桶）", exc_info=True)
        return ""


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
    """单被保护对象的三态熔断器。

    `provider` / `tenant` 是**身份元数据**（只进快照，不参与状态机）：
    快照要能如实回答「这个桶是谁的」—— 修复前只有 provider 一个维度，
    运维页看到 `deepseek OPEN` 时无法区分「全站 deepseek 挂了」
    与「某个租户把额度打爆了」。
    """

    def __init__(
        self,
        name: str,
        *,
        provider: str = "",
        tenant: str = "",
        failure_threshold: int = 3,
        failure_window_sec: float = 60.0,
        recovery_cooldown_sec: float = 30.0,
        half_open_success_needed: int = 2,
        reset_on_success: bool = False,
    ) -> None:
        self.name = name
        self.provider = str(provider or "")
        self.tenant = str(tenant or "")
        self.failure_threshold = failure_threshold
        self.failure_window_sec = failure_window_sec
        self.recovery_cooldown_sec = recovery_cooldown_sec
        self.half_open_success_needed = half_open_success_needed
        #: ★ `False`（默认）= 窗口内失败数达到阈值就熔断 —— 生成路径要的语义，
        #: **默认值即护栏**：不改这个位，所有既有调用方行为逐位不变。
        #: `True` = 成功即清空失败窗口 ⇒ 语义变成"**连续**失败"
        #: （抖动不累积成假熔断）。判定客户端走 `judge_breaker()` 用这个。
        self.reset_on_success = bool(reset_on_success)
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
            if self.reset_on_success and self._state.state == "CLOSED":
                # ★ "连续失败"语义：一次成功就把失败窗口清空。
                #   不清的话，抖动的端点（失败/成功交替）会在窗口内攒够
                #   `failure_threshold` 次失败 ⇒ 假熔断，症状是
                #   "语义层莫名不工作"（有实测教训：`test_success_resets_the_failure_streak`）。
                self._failure_ts.clear()
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
                # `name` **就是桶 key**（`breaker_key` 的输出：`provider`
                # 或 `provider:tenant`）—— 现有字段与读法保持不变。
                "name": self._state.name,
                # ★ 新增：隔离维度，让运维页能把桶**按租户列出来**
                #   （`tenant=""` + `scope=global` = 无身份调用方共用的全局桶）。
                "provider": self.provider,
                "tenant": self.tenant,
                "scope": "tenant" if self.tenant else GLOBAL_SCOPE,
                "state": self._state.state,
                "failures_in_window": len(self._failure_ts),
                "failure_threshold": self.failure_threshold,
                "total_failures": self._state.total_failures,
                "total_successes": self._state.total_successes,
                "total_rejected": self._state.total_rejected,
                "uptime_sec": round(now - self._state.last_state_change_ts, 1),
            }


class CircuitBreakerRegistry:
    """熔断器注册中心：按 `provider × 租户` 隔离。

    key 由 `breaker_key` **唯一构造**（本类自己不拼 key，只收发 key）；
    默认参数仍**按 provider** 取 —— 见 `get_or_create`。
    """

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
        # ★ 注册表自己也要锁（键空间变成**动态**之后就必要了）：
        #   ① `snapshot_all()` 是在**遍历**字典，而键空间里现在每见一个新租户
        #      就多一个键 —— 边遍历边插入会抛
        #      `RuntimeError: dictionary changed size during iteration`，
        #      而读快照的正是 `/health` / 运维页（在请求路径上）。
        #   ② 两个线程同时首触同一个桶会各建一个，计数被劈成两半
        #      ⇒ **桶永远达不到阈值**（护栏静默失效，且不报错）。
        # 锁序：注册表锁 → 桶锁（`snapshot()` 内部），没有反向路径，不成环。
        self._lock = threading.Lock()

    def get_or_create(
        self,
        name: str,
        *,
        provider: str | None = None,
        tenant: str = "",
    ) -> TimeWindowCircuitBreaker:
        """按 key 取桶；不存在则**按 provider 的默认参数**建一个。

        `name` 是 `breaker_key(...)` 的结果。`provider` 缺省时从 key 反解
        （`breaker_provider`）—— 旧调用方（`network_fallback` 的
        `fallback:<源名>`）不改一行也拿到与改动前**完全一样**的默认参数。

        `tenant` **只进快照**：如实标出这是租户桶还是无身份的全局桶，
        不进 key、也不改任何阈值。
        """
        key = str(name or "")
        with self._lock:
            cb = self._breakers.get(key)
            if cb is None:
                scope = provider if provider is not None else breaker_provider(key)
                cfg = self._DEFAULTS.get(scope, self._DEFAULTS["deepseek"])
                cb = TimeWindowCircuitBreaker(
                    name=key, provider=scope, tenant=tenant, **cfg)
                self._breakers[key] = cb
            return cb

    def for_call(self, provider: str, tenant: str = "") -> TimeWindowCircuitBreaker:
        """**LLM 调用点的入口**：按 `provider × 租户` 取桶。

        `tenant=""` ⇒ provider 级桶 —— 这是「取不到租户身份」时的保守退化
        （见模块 docstring 取舍二），不是「不熔断」。
        建 key 仍只走 `breaker_key` 一处。
        """
        return self.get_or_create(
            breaker_key(provider, tenant), provider=provider, tenant=tenant)

    def snapshot_all(self) -> dict[str, dict[str, object]]:
        """所有桶的快照：`{breaker_key: snapshot}`（各租户的桶都在里面）。

        ⚠️ 在锁内**先复制键列表**再遍历：`/health`、运维页与
        `network_fallback._sources_snapshot()` 都是在请求路径上读它，
        而同一条路径上随时可能有新租户建桶（见 `__init__` 的锁说明）。
        """
        with self._lock:
            items = list(self._breakers.items())
        return {n: cb.snapshot() for n, cb in items}


_registry: CircuitBreakerRegistry | None = None


def get_circuit_registry() -> CircuitBreakerRegistry:
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


def reset_circuit_registry_for_test() -> CircuitBreakerRegistry:
    r"""重建注册表单例（**仅测试隔离用**）。

    ## 为什么必须有它（2026-10-01，与 `reset_prices_cache` / `reset_budget_for_test` 同一类）

    注册表是**进程级单例**，而它承载的是**可变的熔断状态**。于是任何"先打满阈值、
    再断言被拒"的判据，其**前提**都依赖"这个桶此刻是 CLOSED 的初始态"：

        cb = get_circuit_registry().get_or_create("fallback:_ConnA")
        for _ in range(200): cb.record_failure()
        assert cb.allow_request() is False, "熔断器没有打开 —— 测试前提不成立"

    一旦同进程里别的用例（或应用后台线程）碰过同一个桶，
    这条断言就不是在测被测代码，而是在测**用例执行顺序** ——
    表现为"合并跑偶发红、单独跑全绿"，而排障时最容易被当成"抖动"忽略。
    本轮实测到过一次这种红（`test_fallback_wiring.py::test_circuit_breaker_open_refuses`
    在合并跑时失败一次），5 次重跑未复现 —— 这正是需要**结构性隔离**而不是
    "多跑几次看看"的信号。

    用法：`tests/conftest.py` 的 autouse 夹具每个用例前后各调一次。
    """
    global _registry  # noqa: PLW0603
    _registry = CircuitBreakerRegistry()
    return _registry


__all__ = [
    "GLOBAL_SCOPE",
    "JUDGE_COOLDOWN_SEC",
    "JUDGE_FAILURES_TO_OPEN",
    "JUDGE_FAILURE_WINDOW_SEC",
    "CircuitBreakerRegistry",
    "CircuitState",
    "TimeWindowCircuitBreaker",
    "breaker_key",
    "breaker_provider",
    "caller_tenant_id",
    "get_circuit_registry",
    "judge_breaker",
    "reset_circuit_registry_for_test",
]
