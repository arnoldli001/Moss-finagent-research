"""分层查找测试：exact-first, fuzzy-on-miss（第十三轮）。

## 守的是什么

用户问「用 n-gram 索引查找比 schema 查找本地信息更快吧」。
实测量化了这个直觉**不成立**：

| 方法 | 耗时/次（50 条 catalog） |
|---|---:|
| 精确哈希 | **0.32 μs** |
| n-gram 全表 | **126 μs（慢 398×）** |

但 n-gram **救回了 67% 的"精确 miss"** —— 所以它不是"更快的查找"，
而是"更慢但能容错的查找"。正确做法是**分层**：

    第一层 精确（含模板）→ 命中即返回        O(1)
    第二层 n-gram 兜底    → 只在 miss 时跑    O(N×gram)

本文件守三件事：
  1. **快路径必须优先**（不能因为加了 fuzzy 就让精确查询变慢）
  2. **fuzzy 只在 miss 时触发**（命中精确时 `_fuzzy_hits` 不该增长）
  3. **fuzzy 有阈值**（不能把任何输入都硬匹配到一个指标 —— 那比 miss 更危险）
"""

from __future__ import annotations

import pytest

from src.infrastructure.catalog import reset_registry_for_test
from src.infrastructure.catalog.registry import get_registry


@pytest.fixture(autouse=True)
def _clean():
    reset_registry_for_test()
    yield
    reset_registry_for_test()


# ============================================================
# 第一层：精确 / 模板（快路径）
# ============================================================


def test_exact_hit_reports_exact():
    r = get_registry()
    meta, how = r.resolve("CPI")
    assert how == "exact"
    assert meta is not None and meta.indicator == "CPI"


def test_template_hit_reports_template():
    """模板命中的方式必须标成 `template` 而不是 `exact`。

    为什么要区分：`template` 说明这是"带代码的派生指标"，
    它的元数据来自模板；审计与排障时需要能分辨。
    """
    r = get_registry()
    meta, how = r.resolve("stock_close:300308")
    assert how == "template"
    assert meta is not None
    assert meta.indicator == "stock_close:300308"   # 具体化后的 id


def test_exact_hit_does_not_trigger_fuzzy():
    """★ 关键：精确命中时**绝不能**跑 fuzzy（否则每次查询都慢 398×）。"""
    r = get_registry()
    before = r.stats()["fuzzy_hits"]
    for _ in range(50):
        r.resolve("CPI")
    after = r.stats()["fuzzy_hits"]
    assert after == before, (
        f"精确命中却触发了 {after - before} 次 fuzzy —— "
        "快路径被污染了（应只在 miss 时才跑 n-gram）"
    )


def test_template_hit_does_not_trigger_fuzzy():
    """模板命中同样不该跑 fuzzy。"""
    r = get_registry()
    before = r.stats()["fuzzy_hits"]
    for _ in range(20):
        r.resolve("PE(TTM):300308")
    assert r.stats()["fuzzy_hits"] == before


# ============================================================
# 第二层：fuzzy 兜底（慢路径，只在 miss 时）
# ============================================================


def test_fuzzy_rescues_near_miss():
    """n-gram 应救回"标点/半全角"这类近失。"""
    r = get_registry()
    # `fed:policy_range` 的全角括号变体
    meta, how = r.resolve("fed（policy_range）")
    assert how == "fuzzy", f"应靠 fuzzy 救回，实际 {how}"
    assert meta is not None
    assert meta.indicator == "fed（policy_range）"  # 保留查询名（materialize）


def test_fuzzy_does_not_trigger_on_exact_ids():
    """已能精确命中的 id，走 fuzzy 分支是浪费（应直接 exact）。"""
    r = get_registry()
    for ind in ("CPI", "PPI", "M2", "us_fed_rate"):
        _meta, how = r.resolve(ind)
        assert how == "exact", f"{ind} 应精确命中，实际 {how}"


def test_fuzzy_disabled_returns_miss():
    """`fuzzy=False` 时必须直接 miss（给热路径用的开关）。"""
    r = get_registry()
    meta, how = r.resolve("fed（policy_range）", fuzzy=False)
    assert meta is None
    assert how == "miss"


def test_fuzzy_threshold_rejects_unrelated():
    """★ fuzzy 必须有阈值：毫不相关的输入必须 miss，不能硬匹配到一个指标。

    硬匹配比 miss 更危险 —— miss 会退化为"未登记，走网络"（多花时间但正确），
    硬匹配会把 A 指标的数据当成 B 指标的（**数据错位，且不报错**）。
    """
    r = get_registry()
    meta, how = r.resolve("完全不存在的指标XYZ123", fuzzy=True, threshold=0.55)
    assert how == "miss", f"无关输入不该被匹配到 {meta and meta.indicator}"


def test_fuzzy_hits_are_counted():
    """fuzzy 命中要计数（观测上游命名是否系统性不一致）。"""
    r = get_registry()
    before = r.stats()["fuzzy_hits"]
    r.resolve("fed（policy_range）")
    assert r.stats()["fuzzy_hits"] == before + 1


def test_miss_on_empty_input():
    """空输入直接 miss，不跑 fuzzy。"""
    r = get_registry()
    for bad in ("", "   "):
        meta, how = r.resolve(bad)
        assert meta is None and how == "miss"


# ============================================================
# 性能契约（防止有人"顺手"把主路径换成 fuzzy）
# ============================================================


def test_exact_path_is_fast():
    """精确路径必须保持微秒级（实测 0.32μs；给 100× 余量防抖动）。"""
    import time

    r = get_registry()
    r.resolve("CPI")                     # 预热
    n = 2000
    t0 = time.perf_counter()
    for _ in range(n):
        r.resolve("CPI")
    per_us = (time.perf_counter() - t0) / n * 1e6
    assert per_us < 50, (
        f"精确查找 {per_us:.1f}μs —— 明显变慢（实测基线 0.32μs）。"
        "检查是不是把 fuzzy 加进了主路径。"
    )


def test_stats_shape():
    """stats() 要给出可观测字段。"""
    r = get_registry()
    r.resolve("CPI")
    st = r.stats()
    for key in ("exact_entries", "templates", "fuzzy_hits", "fuzzy_index_size"):
        assert key in st, f"stats 缺字段 {key}"
    assert st["exact_entries"] > 0
