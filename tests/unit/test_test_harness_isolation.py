"""判据：**测试台架自身的隔离不变量**（它们的失效形态是"别的用例莫名全红"）。

这一组不测业务，测的是"跑测试这件事本身不会互相污染"。
它们的价值由两次真实事故定价（都发生在 2026-09-30）：

| 事故 | 症状 | 归宿 |
|---|---|---|
| 用例对**真实进程**发停止信号 | 线上 dev 被停、worker 差一步被停 | `_forbid_real_process_signals` |
| 用例把**别的环境的 Settings** 缓存进进程 | 四条 `/health` 判据合跑全红、单跑全绿 | `_reset_settings_cache` |
| 用例之间**共享可变的熔断桶** | `test_circuit_breaker_open_refuses` 合并跑偶发红、单跑全绿 | `_isolate_circuit_registry` |
| 用例读/写**真实的花费账本** | 判据红绿取决于"今天线上花了多少钱" | `_isolate_llm_spend_ledger` |

三件事的形状相同：**夹具只管自己那一格，而进程是全局的**。
"""
from __future__ import annotations

import os

import pytest


def test_settings_cache_clearer_actually_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `_reset_settings_cache` 必须真的清缓存 —— 判据不依赖用例执行顺序。

    为什么这样写（而不是"上一个用例污染、下一个用例断言干净"）：
    那取决于执行顺序（本仓库装了 `pytest-randomly`），顺序一变判据就恒真。
    这里**直接调用夹具本体**：进入它之后，缓存里那份"别人的环境"必须已经没了。
    """
    from src.core import config
    from tests.conftest import _reset_settings_cache as _fixture

    # pytest 会把夹具包一层（直接调用它会被拒绝）⇒ 取 `__wrapped__` 拿本体
    body = getattr(_fixture, "__wrapped__", _fixture)

    # ① 先人为污染：在 pilot 环境下构建一份 Settings（= 事故现场）
    monkeypatch.setenv("MOSS_ENV", "pilot")
    poisoned = config.get_settings()
    assert poisoned.env == "pilot", "哨兵没生效，这条判据什么也没证明"

    # ② 进入夹具：它必须把那份缓存清掉
    gen = body()
    next(gen)
    try:
        fresh = config.get_settings()
        # 当下这份环境就是 conftest 给测试用的那一档（不写死字面量：
        # 环境名由 conftest 决定，判据只要求"与当下 env 一致"）
        assert fresh.env != "pilot" or os.environ.get("MOSS_ENV") == "pilot", (
            "夹具没有清掉上一个用例留下的 Settings ⇒ 后面的用例会在**别的环境**"
            "的配置下运行（实测后果：登录门槛被当成公网实例，四条 /health 判据 401）")
        assert fresh is not poisoned, "缓存没清：拿到的还是污染时那一份对象"
    finally:
        with pytest.raises(StopIteration):
            next(gen)          # 夹具是 yield 型，收尾也要走完（它自己会再清一次）


def test_real_process_signals_are_blocked_for_live_pids() -> None:
    """★ 测试进程内不许对**活着的** PID 发信号（`_forbid_real_process_signals`）。

    这条判据**故意打真实的 `manage.py` 函数**（不是测夹具是否存在）：
    它断言"活着的进程号会被拒绝"。用当前进程自己的 PID 当样本 ——
    它必然活着，而且不可能被误伤（函数只会拒绝，不会真的下手）。
    """
    import manage

    me = os.getpid()
    assert manage.request_graceful_stop(me) is False, (
        "测试里竟然真的向活着的进程发了停止信号 —— 实测后果是**线上 dev 被停掉**")
    assert manage.kill_pid_tree(me) is False, (
        "测试里竟然真的对活着的进程下了硬杀")


def test_blocking_only_applies_to_live_pids() -> None:
    """反向：**假 PID 必须继续走真实实现**（否则判据变成"测一个替身"）。

    有些用例故意验证真实分支（例如"对不存在的 PID 必须返回 False 而不是抛异常"）。
    一律拦住它们，会让那些判据看起来绿、实际什么都没证明。
    """
    import manage

    assert manage.request_graceful_stop(999_999_999) is False


# ======================================================================
# 共享可变单例：熔断注册表 / 花费账本
# ======================================================================

def test_circuit_registry_is_reset_by_the_fixture() -> None:
    """★ `_isolate_circuit_registry` 必须真的换掉注册表。

    为什么这样写（而不是"上一个用例污染、下一个断言干净"）：
    那取决于执行顺序（本仓库装了 `pytest-randomly`），顺序一变判据就恒真。
    这里**直接调用夹具本体**：进入它之后，之前那个桶必须已经不在注册表里。
    """
    from src.infrastructure.llm import circuit_breaker as cb_mod
    from tests.conftest import _isolate_circuit_registry as _fixture

    body = getattr(_fixture, "__wrapped__", _fixture)

    # ① 人为污染：打满一个桶的阈值（= 事故现场：桶带着状态跨用例存活）
    cb = cb_mod.get_circuit_registry().get_or_create("fallback:_ConnZ")
    for _ in range(200):
        cb.record_failure()
    assert cb.snapshot()["state"] == "OPEN", "哨兵没生效，这条判据什么也没证明"

    # ② 进入夹具：它必须换成一份全新的注册表
    gen = body()
    next(gen)
    try:
        fresh = cb_mod.get_circuit_registry()
        assert "fallback:_ConnZ" not in fresh.snapshot_all(), (
            "夹具没有换掉注册表 ⇒ 后面的用例会继承别人留下的熔断状态，"
            "而症状是「合并跑偶发红、单独跑全绿」——最容易被当成抖动忽略")
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_spend_ledger_dir_is_redirected_for_tests(tmp_path_factory, monkeypatch) -> None:
    """★ 花费账本必须被指到临时目录：否则判据红绿取决于"今天线上花了多少钱"。"""
    from src.core import budget as budget_mod
    from tests.conftest import _isolate_llm_spend_ledger as _fixture

    body = getattr(_fixture, "__wrapped__", _fixture)
    monkeypatch.delenv(budget_mod.LEDGER_DIR_ENV, raising=False)

    # 夹具本体要两个依赖 ⇒ 直接调用时显式传进去（本判据测的就是它的效果）
    gen = body(tmp_path_factory, monkeypatch)
    next(gen)
    try:
        resolved = budget_mod.resolve_ledger_dir()
        assert resolved is not None, "夹具没设目录（设成空串会**关闭**账本，语义不同）"
        assert "llm_spend" in str(resolved) or "pytest" in str(resolved), (
            f"账本目录没有指向临时目录：{resolved} ⇒ 测试会读/写真实花费")
    finally:
        with pytest.raises(StopIteration):
            next(gen)
