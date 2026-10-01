"""判据：熔断按 `provider × 租户` 隔离。

## 为什么这一组判据必须存在（先讲事故）

2026-09-25 实测（教训注释在 `src/api/routes/research.py` @107-109）：
熔断桶**只按 provider 分**。一个用户的突发流量在 60s 内打出 3 次 deepseek
失败 ⇒ `deepseek` 桶 OPEN ⇒ **所有租户**的请求连 API 都不试、一起降级到备模型。
失败是按**请求方**累积的，却按**提供商**生效 —— 两边不匹配。

## 自证（本项目铁律：不会红的判据等于没有判据）

`test_the_judge_would_have_caught_the_shared_bucket` 把**修复前**的取桶方式
（同一个 key 给两个租户用）装回注册表，跑**与第一条判据完全相同**的场景，
断言租户 B **会**被拒 —— 它证明第一条判据不是恒绿：机制一旦回退，它立刻变红。

## 锚点

`decision` 层链首是付费的 `deepseek`（`configs/models.yaml`），判据**现读配置**
取锚点而不是硬编码模型名/次序；配置真变了会以一句人话断言失败，而不是静默变绿。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.core.accounting import AccountingContext, use
from src.core.exceptions import LLMGatewayError
from src.core.tenancy import Principal, principal_scope
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.circuit_breaker import (
    CircuitBreakerRegistry,
    TimeWindowCircuitBreaker,
    breaker_key,
    caller_tenant_id,
)
from src.infrastructure.llm.gateway import LLMGateway
from src.infrastructure.llm.models import LLMResponse

#: 判据锚定的提供商（decision 层链首所属，见 `configs/models.yaml`）
PAID = "deepseek"

#: 判据锚定的任务层级
TIER = "decision"


class _FakeProvider:
    """可控替身：`error` 一设即失败（默认 `count_as_failure=True` = 瞬时故障）。"""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[str] = []

    async def chat(self, spec, system, prompt, *, json_mode=False):
        self.calls.append(spec.model_name)
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=f"answer::{spec.model_name}", model_used=spec.model_name,
            provider=spec.provider, tokens_in=100, tokens_out=20,
            prompt_hash="ph", response_hash="rh",
        )


def _defaults(provider: str = PAID) -> dict[str, float]:
    """该 provider 的默认熔断参数（**配置的唯一事实源**，判据里不抄数字）。"""
    return CircuitBreakerRegistry._DEFAULTS[provider]  # noqa: SLF001


def _threshold(provider: str = PAID) -> int:
    return int(_defaults(provider)["failure_threshold"])


def _chain_head_provider() -> str:
    """`TIER` 层链首的 provider —— **现读配置**，不硬编码模型名与次序。"""
    from src.infrastructure.llm.gateway import _load_model_config

    specs, routing, _lo = _load_model_config("configs/models.yaml")
    return specs[routing[TIER][0]].provider


def _settings(tmp_dir: str):
    from src.core.config import Settings

    return Settings(
        model_config_path="configs/models.yaml",
        llm_cache_dir=tmp_dir,
        llm_audit_dir=tmp_dir,
        # 关缓存：判据只看**准入决策**，别被缓存命中混淆
        llm_cache_enabled=False,
    )


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    """每个用例一套干净熔断桶（进程级单例，跨用例会串同一个桶）。"""
    from src.infrastructure.llm import circuit_breaker as cbmod

    monkeypatch.setattr(cbmod, "_registry", CircuitBreakerRegistry())


@pytest.fixture(autouse=True)
def _fresh_rate_guard(tmp_dir, monkeypatch):
    """限流护栏也要干净，否则"B 被放行"可能因为**别的原因**成立。

    限流锁定的模型会被直接从降级链上跳过（`gateway.py` @502），
    那是另一条护栏，不能让它替熔断器作答。
    """
    from src.infrastructure.llm.rate_limit_guard import reset_guard

    monkeypatch.setenv("MOSS_RATE_GUARD_PATH",
                       str(Path(tmp_dir) / "rate_guard.json"))
    reset_guard()
    yield
    reset_guard()


async def _tenant_a_burst_then_tenant_b(
    monkeypatch, tmp_dir: str, registry: CircuitBreakerRegistry,
) -> tuple[dict[str, dict[str, object]], bool, str]:
    """一条场景，换注册表实现就得到不同结论 ⇒ `(快照, B 是否被放行, B 的观察)`。

    ① 租户 A 连续失败到阈值（瞬时故障，计入熔断）；
    ② 提供商"恢复"（替身不再报错）—— 此后**只有桶的准入**决定 B 的命运。
    """
    from src.infrastructure.llm import circuit_breaker as cbmod

    assert _chain_head_provider() == PAID, (
        f"{TIER} 层链首不再是 {PAID} —— 判据锚定的前提变了，请按新配置改锚点")
    monkeypatch.setattr(cbmod, "_registry", registry)

    provider = _FakeProvider()
    gw = LLMGateway(settings=_settings(tmp_dir),
                    providers={PAID: provider}, cache=None)

    provider.error = LLMGatewayError("连接超时")     # 瞬时故障：count_as_failure=True
    with use(AccountingContext(path="/api/v1/research/analyze",
                              tenant_id="a", user_id="user-a")):
        for _ in range(_threshold()):
            with pytest.raises(LLMGatewayError):
                await gw.complete(TIER, "系统", "租户A的任务")

    provider.error = None
    provider.calls.clear()
    try:
        with use(AccountingContext(path="/api/v1/research/analyze",
                                  tenant_id="b", user_id="user-b")):
            resp = await gw.complete(TIER, "系统", "租户B的任务")
        return registry.snapshot_all(), True, str(resp.content)
    except LLMGatewayError as exc:
        return registry.snapshot_all(), False, str(exc)


# ---------------------------------------------------------------------------
# ① 租户隔离（缺陷的直接判据）
# ---------------------------------------------------------------------------


async def test_tenant_a_failures_do_not_open_tenant_b(monkeypatch, tmp_dir):
    """A 打到阈值 ⇒ 只有 A 的桶 OPEN；B 仍 CLOSED 且**真的被放行**。"""
    registry = CircuitBreakerRegistry()
    snap, b_allowed, detail = await _tenant_a_burst_then_tenant_b(
        monkeypatch, tmp_dir, registry)

    assert snap[f"{PAID}:a"]["state"] == "OPEN", "A 的桶没熔断 —— 判据前提不成立"
    assert snap[f"{PAID}:b"]["state"] == "CLOSED", "B 的桶被 A 的失败打开了"
    assert b_allowed is True, f"B 被 A 的失败连坐（缺陷原样复现）：{detail}"
    assert snap[f"{PAID}:b"]["total_rejected"] == 0, "B 的桶出现了被拒计数"
    assert snap[f"{PAID}:b"]["total_successes"] == 1, "B 的调用没走到提供商"


# ---------------------------------------------------------------------------
# ② 无租户上下文（后台作业/脚本）⇒ provider 级全局桶，**仍然熔断**
# ---------------------------------------------------------------------------


async def test_missing_tenant_falls_back_to_a_global_bucket(monkeypatch, tmp_dir):
    """取不到租户 ⇒ 落在 provider 级全局桶，而不是"每个调用一个桶"或"不熔断"。"""
    from src.infrastructure.llm import circuit_breaker as cbmod

    registry = CircuitBreakerRegistry()
    monkeypatch.setattr(cbmod, "_registry", registry)

    # 前提：本用例里确实**没有**租户身份（不是我们没去取）
    assert caller_tenant_id() == ""
    assert breaker_key(PAID, "") == PAID, "取不到租户时 key 就是 provider 级"

    provider = _FakeProvider()
    gw = LLMGateway(settings=_settings(tmp_dir),
                    providers={PAID: provider}, cache=None)
    provider.error = LLMGatewayError("连接超时")
    for _ in range(_threshold()):
        with pytest.raises(LLMGatewayError):
            await gw.complete(TIER, "系统", "后台作业")

    snap = registry.snapshot_all()
    assert PAID in snap, "全局桶不见了"
    assert snap[PAID]["tenant"] == "", "全局桶被标成了某个租户"
    assert snap[PAID]["scope"] == "global", "快照看不出这是全局桶"
    assert snap[PAID]["state"] == "OPEN", "没有租户身份就不熔断了 —— 护栏失效"
    assert [k for k in snap if k != PAID] == [], "凭空造出了租户桶"

    # 「仍然熔断」的**机器证据**：提供商不再被调用 + 审计里有 circuit_open
    before = len(provider.calls)
    with pytest.raises(LLMGatewayError):
        await gw.complete(TIER, "系统", "后台作业2")
    assert len(provider.calls) == before, "熔断中还在尝试提供商"
    assert registry.snapshot_all()[PAID]["total_rejected"] >= 1
    errors = [str(e.get("error") or "") for e in LLMAuditLog(tmp_dir).read_all()]
    assert any(e.startswith(f"circuit_open: {PAID}") for e in errors), errors[-3:]


# ---------------------------------------------------------------------------
# ③ 快照同时列出各租户的桶（运维页 / `/health` 的可见面）
# ---------------------------------------------------------------------------


def test_snapshot_lists_each_tenant_separately(monkeypatch):
    """`snapshot_all()` 同时列出 `deepseek:a` 与 `deepseek:b`，字段沿用现有快照。"""
    from src.infrastructure.llm import circuit_breaker as cbmod

    registry = CircuitBreakerRegistry()
    monkeypatch.setattr(cbmod, "_registry", registry)

    bucket_a = registry.for_call(PAID, "a")
    bucket_b = registry.for_call(PAID, "b")
    assert bucket_a is not bucket_b, "两个租户拿到同一个桶 —— 又回到按 provider 分桶"
    for _ in range(_threshold()):
        bucket_a.record_failure()

    snap = registry.snapshot_all()
    assert set(snap) == {f"{PAID}:a", f"{PAID}:b"}
    # 现有快照字段一个不少（运维页/health 的读法不改）
    for field in ("name", "state", "failures_in_window", "failure_threshold",
                  "total_failures", "total_successes", "total_rejected",
                  "uptime_sec"):
        assert field in snap[f"{PAID}:a"], f"快照字段 {field} 丢了"
    assert snap[f"{PAID}:a"]["state"] == "OPEN"
    assert snap[f"{PAID}:b"]["state"] == "CLOSED"
    assert snap[f"{PAID}:b"]["failures_in_window"] == 0, "A 的失败漏进了 B 的桶"
    # 隔离维度只分桶，**不改配置**：`_DEFAULTS` 仍按 provider 取
    assert _defaults("ollama")["failure_threshold"] != \
        _defaults(PAID)["failure_threshold"], \
        "两个 provider 的默认阈值一样 —— 下面那条证明不了'按 provider 取默认值'"
    assert (snap[f"{PAID}:a"]["failure_threshold"]
            == _defaults(PAID)["failure_threshold"])
    assert (snap[f"{PAID}:b"]["failure_threshold"]
            == _defaults(PAID)["failure_threshold"])
    assert (registry.for_call("ollama", "a").failure_threshold
            == _defaults("ollama")["failure_threshold"]), \
        "租户桶拿错了 provider 的默认参数"
    # 快照里的身份字段（运维页据此把桶按租户列出来）
    assert snap[f"{PAID}:a"]["provider"] == PAID
    assert snap[f"{PAID}:a"]["tenant"] == "a"
    assert snap[f"{PAID}:a"]["scope"] == "tenant"


# ---------------------------------------------------------------------------
# ④ 自证：修复前的共享桶实现下，① 必须变红
# ---------------------------------------------------------------------------


class _LegacyProviderOnlyRegistry(CircuitBreakerRegistry):
    """复现**修复前**的取桶方式：丢掉租户维度。

    它等价于旧的 `get_or_create(spec.provider)`（`gateway.py` 改动前那一行）。
    存在的唯一目的就是自证 —— 同一条场景在它下面必须变红。
    """

    def for_call(self, provider: str, tenant: str = "") -> TimeWindowCircuitBreaker:
        _ = tenant                 # 刻意丢掉：这正是缺陷本身
        return self.get_or_create(provider)


async def test_the_judge_would_have_caught_the_shared_bucket(monkeypatch, tmp_dir):
    """**自证**：同一个 key 给两个租户用时，B 会被拒 —— ① 的靶子确实存在。"""
    # ① 字面上复现旧调用点：`get_or_create(spec.provider)` 拿到的桶给两个租户共用
    legacy = CircuitBreakerRegistry()
    shared = legacy.get_or_create(PAID)
    for _ in range(_threshold()):
        shared.record_failure()
    assert legacy.get_or_create(PAID).allow_request() is False, \
        "同一个 key 给两个租户用时竟然还放行 —— 判据的靶子不见了"

    # ② 把旧实现装回网关，跑与 ① 判据**完全相同**的场景
    snap, b_allowed, detail = await _tenant_a_burst_then_tenant_b(
        monkeypatch, tmp_dir, _LegacyProviderOnlyRegistry())
    assert snap[PAID]["state"] == "OPEN"
    assert b_allowed is False, (
        "共享桶下 B 竟然被放行了 —— test_tenant_a_failures_do_not_open_"
        "tenant_b 就是一条恒绿判据，抓不到共享桶缺陷")
    # 「B 是被**熔断**拒的」要看审计：`last_error` 会被链上后续跳的报错覆盖，
    # 最终异常消息里不一定还留着熔断字样（gateway 既有行为，不在这里改）。
    errors = [str(e.get("error") or "") for e in LLMAuditLog(tmp_dir).read_all()]
    assert any(err.startswith(f"circuit_open: {PAID}") for err in errors), \
        f"B 不是被熔断拒的（失败原因另有其人）：{errors[-4:]} {detail}"


# ---------------------------------------------------------------------------
# ⑤ 租户来源必须与 LLM 审计同源（会话身份 > Principal；都没有 ⇒ 空串）
# ---------------------------------------------------------------------------


def test_tenant_comes_from_the_audit_identity_rule():
    """口径同源判据：口径一漂移，"审计记在 T、熔断按 U 分桶"就会长期共存。"""
    assert caller_tenant_id() == "", "无身份时必须是空串（不许编一个默认租户）"

    with use(AccountingContext(path="/api/v1/x", tenant_id="vip", user_id="u1")):
        assert caller_tenant_id() == "vip", "会话身份没被用上"

    with principal_scope(Principal(user_id="u2", tenant_id="dept-a")):
        assert caller_tenant_id() == "dept-a", "没有会话时该退回 tenancy Principal"

    # 两者都有时：**会话身份优先**（与 `audit._resolve_identity` 逐字一致）
    with principal_scope(Principal(user_id="u3", tenant_id="dept-b")):
        with use(AccountingContext(path="/api/v1/x", tenant_id="vip",
                                   user_id="u3")):
            assert caller_tenant_id() == "vip", "会话身份必须优先于 Principal"

    assert caller_tenant_id() == "", "上下文退出后必须干净"


# ---------------------------------------------------------------------------
# ⑥ 并发：同一个桶只能建一份（计数被劈开 = 阈值永远达不到的静默失效）
# ---------------------------------------------------------------------------


def test_one_bucket_per_key_under_concurrent_first_touch(monkeypatch):
    """并发首触同一个桶只能建出**一份**，且动态键空间下读快照不能炸。

    两半都是**确定性的**（不靠运气）：
    ① 用慢 `__init__` 把"检查-创建"窗口拉到 50ms —— 没有锁时 8 个线程
       必然各建一份，失败计数被劈开（桶永远达不到阈值，且不报错）；
    ② 用慢 `snapshot()` 把"正在遍历"的窗口拉长，让插入**必然**落在遍历中间 ——
       没有"锁内先复制"就会抛 `RuntimeError: dictionary changed size`。
    """
    registry = CircuitBreakerRegistry()
    original_init = TimeWindowCircuitBreaker.__init__

    def slow_init(self, *args, **kwargs):
        time.sleep(0.05)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(TimeWindowCircuitBreaker, "__init__", slow_init)

    created: list[TimeWindowCircuitBreaker] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            created.append(registry.for_call(PAID, "a"))
        except Exception as exc:  # noqa: BLE001 线程里的异常要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert len({id(bucket) for bucket in created}) == 1, \
        "同一个桶被建了多份 —— 失败计数被劈开，桶永远达不到阈值"
    assert len(registry.snapshot_all()) == 1

    # ② 键空间是**动态**的（每见一个新租户多一个键）：一边建桶一边读快照
    monkeypatch.setattr(TimeWindowCircuitBreaker, "__init__", original_init)
    original_snapshot = TimeWindowCircuitBreaker.snapshot

    def slow_snapshot(self):
        time.sleep(0.002)
        return original_snapshot(self)

    monkeypatch.setattr(TimeWindowCircuitBreaker, "snapshot", slow_snapshot)

    stop = threading.Event()
    reader_errors: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                registry.snapshot_all()
            except Exception as exc:  # noqa: BLE001
                reader_errors.append(exc)
                stop.set()

    def churn() -> None:
        for i in range(60):
            registry.for_call(PAID, f"t{i}")
        stop.set()

    reader_thread = threading.Thread(target=reader)
    churn_thread = threading.Thread(target=churn)
    reader_thread.start()
    churn_thread.start()
    churn_thread.join()
    reader_thread.join()
    assert not reader_errors, f"边遍历边插入把快照读炸了：{reader_errors}"
    assert len(registry.snapshot_all()) == 61
