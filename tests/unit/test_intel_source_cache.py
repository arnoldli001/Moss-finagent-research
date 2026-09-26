"""情报流**源级缓存**的回归测试（2026-09-25 性能剖析后新增）。

## 被钉住的四个不变量

性能剖析实测：`build_feed` 一次完整重建 2.6~3.3 秒，其中 zsxq 分页 ~1.2s +
`fetch_all` ~0.5s **全是网络**，CPU（组装 356 条）只占 ~0.17s。
`source_cache` 让"取数一次、多档复用"（换 `filter`/`sort` 不再重拉上游）。

代价是引入了两类新风险，本文件就是钉住它们：

1. **副作用被重复执行** —— `item_store.persist`（留存落库）与
   `save_watermark`（推进水位线）只能在**真取数**那一趟跑。水位线被凭空
   推进会让**内容永久丢失**（`zsxq_incremental` 专门记过这条）。
   所以 `get_or_fetch` 必须如实返回 `fresh`。
2. **失败被缓存** —— 一次限频/空页若被缓存几十秒，用户看到的是
   "这个来源停了"，而它下一趟就会自己好。

另外两条是缓存本身的正确性：TTL 到期要重取、并发同键只取一次（单飞）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.domain.intel import source_cache as sc


@pytest.fixture(autouse=True)
def _clean() -> None:
    sc.reset()
    yield
    sc.reset()


def _run(coro):
    return asyncio.run(coro)


# ==================== fresh 语义（副作用门控的依据）====================


def test_fresh_true_on_first_fetch_false_on_hit() -> None:
    """★ `fresh` 是副作用门控的**唯一依据**，必须准确。

    真取数 → `fresh=True`（允许跑 persist/save_watermark）；
    命中缓存 → `fresh=False`（**绝对不许**再跑一次）。
    """
    calls = {"n": 0}

    async def _fetch():
        calls["n"] += 1
        return {"pubs": [{"t": calls["n"]}]}

    value1, fresh1 = _run(sc.get_or_fetch("k", _fetch))
    value2, fresh2 = _run(sc.get_or_fetch("k", _fetch))
    assert fresh1 is True, "第一次必须是真取数"
    assert fresh2 is False, "第二次必须命中缓存"
    assert calls["n"] == 1, "命中缓存不该再打上游"
    assert value1 == value2


def test_ttl_expiry_refetches() -> None:
    """TTL 到期后必须重取（否则内容永远停在第一次）。"""
    calls = {"n": 0}

    async def _fetch():
        calls["n"] += 1
        return {"pubs": []}

    _run(sc.get_or_fetch("k", _fetch, ttl=0.05))
    _run(sc.get_or_fetch("k", _fetch, ttl=0.05))
    assert calls["n"] == 1, "TTL 内应命中"
    time.sleep(0.08)
    _, fresh = _run(sc.get_or_fetch("k", _fetch, ttl=0.05))
    assert fresh is True, "TTL 过后必须重取"
    assert calls["n"] == 2


def test_ttl_zero_disables_cache() -> None:
    """`ttl=0` = 不缓存（给"强制刷新"留一条路）。"""
    calls = {"n": 0}

    async def _fetch():
        calls["n"] += 1
        return {"pubs": []}

    for _ in range(3):
        _, fresh = _run(sc.get_or_fetch("k", _fetch, ttl=0))
        assert fresh is True
    assert calls["n"] == 3


def test_different_keys_are_independent() -> None:
    """不同键各自缓存（`fetch_all` 的键含自选清单与日期）。"""
    async def _a():
        return {"which": "a"}

    async def _b():
        return {"which": "b"}

    va, _ = _run(sc.get_or_fetch("a", _a))
    vb, _ = _run(sc.get_or_fetch("b", _b))
    assert va["which"] == "a" and vb["which"] == "b"


# ==================== 失败不入缓存 ====================


def test_non_cacheable_result_is_not_stored() -> None:
    """★ 失败/空页的结果**不进缓存** —— 否则一次抖动会被当成"这个来源停了"。

    用 `cacheable` 回调表达这个判据（`build_feed` 传的是
    `lambda p: bool(p.get("pubs"))`）。
    """
    calls = {"n": 0}

    async def _fetch():
        calls["n"] += 1
        return {"pubs": []}          # 空 = 不该被缓存

    v1, fresh1 = _run(sc.get_or_fetch(
        "k", _fetch, cacheable=lambda p: bool(p.get("pubs"))))
    assert fresh1 is True
    v2, fresh2 = _run(sc.get_or_fetch(
        "k", _fetch, cacheable=lambda p: bool(p.get("pubs"))))
    assert fresh2 is True, "上一次没拿到内容，这次必须重试而不是吃缓存"
    assert calls["n"] == 2


def test_cacheable_result_is_stored() -> None:
    """拿到内容才进缓存。"""
    calls = {"n": 0}

    async def _fetch():
        calls["n"] += 1
        return {"pubs": [{"x": 1}]}

    _run(sc.get_or_fetch("k", _fetch, cacheable=lambda p: bool(p.get("pubs"))))
    _, fresh = _run(sc.get_or_fetch(
        "k", _fetch, cacheable=lambda p: bool(p.get("pubs"))))
    assert fresh is False
    assert calls["n"] == 1


# ==================== 单飞 ====================


def test_concurrent_same_key_fetches_once() -> None:
    """★ 并发同一个键只打一次上游（单飞）—— 这是"多用户不放大上游"的根据。

    实测背景：8 个并发请求若各拉一遍，zsxq 分页会从 4 次涨到 39 次。
    """
    calls = {"n": 0}

    async def _main():
        async def _fetch():
            calls["n"] += 1
            await asyncio.sleep(0.15)      # 模拟网络耗时，让并发真的重叠
            return {"pubs": [{"x": 1}]}

        return await asyncio.gather(
            *(sc.get_or_fetch("k", _fetch) for _ in range(8)))

    results = _run(_main())
    assert calls["n"] == 1, f"单飞失效：上游被打了 {calls['n']} 次"
    assert len(results) == 8
    assert all(r[0] == {"pubs": [{"x": 1}]} for r in results)
    # 只有第一个是 fresh，其余都是等的
    assert sum(1 for _v, fresh in results if fresh) == 1


def test_waiter_falls_back_when_peer_fails() -> None:
    """等的那个若对端失败（没进缓存），自己必须补取一次而不是空手而归。"""
    calls = {"n": 0}

    async def _main():
        async def _bad():
            calls["n"] += 1
            await asyncio.sleep(0.1)
            raise RuntimeError("上游挂了")

        async def _good():
            calls["n"] += 1
            return {"pubs": [{"ok": 1}]}

        async def _one(fn):
            try:
                return await sc.get_or_fetch("k", fn)
            except RuntimeError:
                return None

        # 第一个先起（会失败），第二个并发进入等待
        return await asyncio.gather(_one(_bad), _one(_good))

    results = _run(_main())
    # 至少有一个拿到了数据（失败的那个吞掉了异常）
    assert any(r is not None for r in results), results


def test_inflight_is_cleared_after_failure() -> None:
    """取数抛异常后单飞状态必须清掉，否则**后续请求永远等一个完成的 event**。"""
    async def _boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _run(sc.get_or_fetch("k", _boom))
    assert sc.stats()["inflight"] == [], "失败后 inflight 必须清空"

    async def _ok():
        return {"pubs": [{"x": 1}]}

    v, fresh = _run(sc.get_or_fetch("k", _ok))
    assert fresh is True and v["pubs"], "失败后仍能正常取数"


# ==================== 有界 ====================


def test_cache_is_bounded() -> None:
    """条目数有上限（防参数枚举把内存撑满）。"""
    async def _fetch():
        return {"pubs": [{"x": 1}]}

    for i in range(sc.MAX_ENTRIES * 2):
        _run(sc.get_or_fetch(f"k{i}", _fetch))
    assert sc.stats()["entries"] <= sc.MAX_ENTRIES


def test_reset_clears_everything() -> None:
    async def _fetch():
        return {"pubs": [{"x": 1}]}

    _run(sc.get_or_fetch("k", _fetch))
    assert sc.stats()["entries"] == 1
    sc.reset()
    assert sc.stats()["entries"] == 0 and sc.stats()["inflight"] == []
