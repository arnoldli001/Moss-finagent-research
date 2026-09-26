"""幂等键存储的单元测试（`src/core/idempotency.py`）。

## 这个模块为什么值得单独测

它守的是"写请求重试"这件事的**安全性**。三条性质缺一不可：

1. **重复请求不重复执行** —— 否则自动重试会开出两个账号；
2. **失败必须释放占位** —— 否则一次失败的请求会把该键锁在"执行中"，
   用户之后每一次重试都卡在等待里（比重复执行更糟，且完全不可见）；
3. **跨主体不可命中** —— 幂等键来自客户端，响应体里含**初始密码**，
   键不做主体命名空间就等于让人枚举别人的开号结果。

第 2 条最容易写漏，所以这里**直接对着它写了回归测试**。
"""

from __future__ import annotations

import threading
import time

import pytest

from src.core.idempotency import (
    HEADER_NAME,
    IdempotencyStore,
    build_key,
    is_valid_key,
    sanitize,
)

# ======================================================================
# 一、键的形态：非法键必须"忽略"而不是"报错"
# ======================================================================

@pytest.mark.parametrize("key", [
    "3f2504e0-4f89-11d3-9a0c-0305e82c3301",   # UUID 形态（前端主用）
    "0123456789abcdef0123456789abcdef",       # 32 位 hex
    "admin.users.create:2026-09-23T10:00:00",  # 带分隔符
    "abcdefgh",                               # 恰好下限 8 位
])
def test_valid_keys_accepted(key: str) -> None:
    assert is_valid_key(key)


@pytest.mark.parametrize("key", [
    "", "short", "a" * 129, "有中文的键", "key with space", "key/with/slash",
])
def test_invalid_keys_rejected(key: str) -> None:
    assert not is_valid_key(key)


def test_sanitize_downgrades_invalid_to_empty_not_error() -> None:
    """非法键 → 空串（= 不做幂等保护），**不是** 400。

    幂等键是可选优化、不是业务参数。中间代理多塞一个畸形头，
    不该让一次本来能成功的开号失败 —— 那正是要修的症状。
    """
    assert sanitize("  good-key-123456  ") == "good-key-123456"
    assert sanitize("bad key") == ""
    assert sanitize(None) == ""
    assert sanitize("") == ""


def test_build_key_namespaces_by_principal() -> None:
    """同样一个客户端键，不同主体必须落到不同的存储键。"""
    a = build_key("POST /x", "u_aaa", "same-key-1234")
    b = build_key("POST /x", "u_bbb", "same-key-1234")
    assert a != b
    assert "u_aaa" in a and "u_bbb" in b


# ======================================================================
# 二、核心语义：最多执行一次
# ======================================================================

def test_put_then_get_returns_cached_value() -> None:
    store = IdempotencyStore()
    assert store.get("k1") == (False, None)
    store.put("k1", {"user_id": "u_1"})
    assert store.get("k1") == (True, {"user_id": "u_1"})


def test_claim_is_exclusive_while_in_flight() -> None:
    """第一个 claim 成功，第二个必须失败 —— 这就是去重的关键一步。"""
    store = IdempotencyStore()
    assert store.claim("k2") is True
    assert store.claim("k2") is False


def test_claim_after_result_returns_false_then_get_hits() -> None:
    store = IdempotencyStore()
    assert store.claim("k3") is True
    store.put("k3", "done")
    assert store.claim("k3") is False
    assert store.get("k3") == (True, "done")


def test_release_after_failure_allows_retry() -> None:
    """★ 回归测试：失败路径不 release 会**永久锁死**这个键。

    症状：管理员点创建 → 后端报错（比如密码不合规）→ 管理员改好密码再点
    → 前端一直转圈/超时，因为服务端认为"上一个请求还在执行中"。
    """
    store = IdempotencyStore()
    assert store.claim("k4") is True
    # 执行失败 → 路由的 except 分支必须调 release
    store.release("k4")
    # 现在重试应该能重新拿到执行权
    assert store.claim("k4") is True


def test_empty_key_is_never_deduplicated() -> None:
    """空键 = 不做幂等保护（退化成改动前的行为），不能把请求吞掉。"""
    store = IdempotencyStore()
    assert store.claim("") is True
    assert store.claim("") is True
    store.put("", "x")
    assert store.get("") == (False, None)


# ======================================================================
# 三、TTL 与容量
# ======================================================================

def test_expired_entry_is_not_returned() -> None:
    store = IdempotencyStore(ttl_seconds=0.05)
    store.put("k5", "old")
    assert store.get("k5") == (True, "old")
    time.sleep(0.08)
    assert store.get("k5") == (False, None)


def test_expired_entry_can_be_reclaimed() -> None:
    """过期的键必须能重新被 claim —— 否则 TTL 只挡住了读、没放开写。"""
    store = IdempotencyStore(ttl_seconds=0.05)
    assert store.claim("k6") is True
    store.put("k6", "old")
    time.sleep(0.08)
    assert store.claim("k6") is True


def test_capacity_evicts_oldest_first() -> None:
    store = IdempotencyStore(max_entries=3, ttl_seconds=60)
    for i in range(5):
        store.put(f"k{i}", i)
    stats = store.stats()
    assert stats["entries"] == 3
    # 最旧的 k0/k1 被淘汰，最新的三个还在
    assert store.get("k0") == (False, None)
    assert store.get("k4") == (True, 4)


def test_wait_returns_result_written_by_another_thread() -> None:
    """模拟真实的自动重试：请求 B 等待请求 A 的结果，而不是自己再跑一遍。"""
    store = IdempotencyStore(wait_seconds=5.0)
    assert store.claim("k7") is True

    def worker() -> None:
        time.sleep(0.15)
        store.put("k7", {"user_id": "u_9"})

    t = threading.Thread(target=worker)
    t.start()
    found, value = store.wait("k7")
    t.join()
    assert found is True
    assert value == {"user_id": "u_9"}


def test_wait_gives_up_when_claimer_died_without_result() -> None:
    """执行者收工却没写结果（等于它失败了）→ 不要再等满超时。"""
    store = IdempotencyStore(wait_seconds=10.0)
    assert store.claim("k8") is True
    store.release("k8")
    t0 = time.monotonic()
    found, value = store.wait("k8")
    assert (found, value) == (False, None)
    assert time.monotonic() - t0 < 1.0, "释放后应立即返回，不应等满 wait_seconds"


# ======================================================================
# 四、自省（管理台/诊断用）
# ======================================================================

def test_stats_reports_backend_limits() -> None:
    store = IdempotencyStore(ttl_seconds=120, max_entries=8)
    store.put("k9", 1)
    store.get("k9")
    store.get("missing")
    stats = store.stats()
    assert stats["backend"] == "in-process-memory"
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["ttl_seconds"] == 120 and stats["max_entries"] == 8
    # 多副本时必须换 Redis —— 这条提示要留在自省里，别只写在文档里
    assert "Redis" in stats["note"]


def test_header_name_matches_frontend_contract() -> None:
    """前端 `api.ts` 里写死的是 `X-Idempotency-Key`，两边必须一致。"""
    assert HEADER_NAME == "X-Idempotency-Key"


def test_reset_clears_entries_and_counters() -> None:
    store = IdempotencyStore()
    store.put("k10", 1)
    store.get("k10")
    store.reset()
    assert store.get("k10") == (False, None)
    assert store.stats()["hits"] == 0
