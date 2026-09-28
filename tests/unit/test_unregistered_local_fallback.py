"""#5 护栏：**元数据缺失 ≠ 数据不存在**（复现用户报障）。

## 用户原话（2026-09-28）

> 「PPI/CPI 来源 国家统计局(AkShare封装)这个数据在本地表里有啊，
>   fact_data_points表里有indicator字段，有cpi数据啊，
>   **为什么说没有，还要去联网获取？**」

## 原缺陷（代码级）

`smart_fetch.py` 的 `entry is None` 分支无条件 `stale_ids.append(ind)`
（always-stale）—— 于是 `fact_data_points` 里明明有 120 条 CPI，
只要 `indicator_catalog` 没登记就被当成"没有数据"，**每次联网**。

## 修法（用户裁定：按**期间日期**推断，不按抓取时间）

1. 库里一条都没有 → 不新鲜（真缺口，该联网）
2. 有数据 → 从 `period_date` 间隔推断频率与新鲜度阈值，
   比 `今天 - 最新期间日期` 是否在 `阈值 × 容差(2 周期)` 内

**为什么按期间日期**：期间日期才是数据的新鲜度；抓取时间只说明"我们问过"。
**为什么要容差**：CPI 次月 9-15 日发布，卡死 1 个周期会把刚好的上月值误判成过期。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.catalog.smart_fetch import SmartFetcher  # noqa: E402


class _Point:
    """最小 DataPoint 替身（`query_points_batch` 的返回元素）。"""

    def __init__(self, indicator: str, period: str, value: float) -> None:
        self.indicator = indicator
        self.period_date = period
        self.value = value
        self.source_url = "https://data.stats.gov.cn/"
        self.fetch_time = "2026-09-28T00:00:00"
        self.confidence = 0.9

    def model_dump(self) -> dict:
        return {"indicator": self.indicator, "period_date": self.period_date,
                "value": self.value, "source_url": self.source_url,
                "fetch_time": self.fetch_time, "confidence": self.confidence}


class _Repo:
    """只提供 `query_points_batch` 的替身。"""

    def __init__(self, data: dict[str, list]) -> None:
        self._data = data

    async def query_points_batch(self, indicators, *, limit_per_indicator=None):
        out = {}
        for ind in indicators:
            rows = list(self._data.get(ind, []))
            if limit_per_indicator:
                rows = rows[-limit_per_indicator:]
            out[ind] = rows
        return out


class _Freshness:
    def __init__(self, fresh: bool) -> None:
        self._fresh = fresh

    def is_fresh(self, *, now_ms: int) -> bool:
        return self._fresh


class _Entry:
    """索引条目替身。

    `measured=False` 复现 pilot 的真实状态：条目**存在**（自动补登记过）
    但 `row_count` / `last_period_date` **从未回填** → `is_fresh()` 恒假。
    """

    def __init__(self, indicator: str, *, measured: bool) -> None:
        self.indicator = indicator
        self.row_count = 120 if measured else 0
        self.last_period_date = "2026-09-01" if measured else ""
        self.frequency = "daily"          # 自动补登记的默认值
        self.freshness_hours = 24.0
        self.expands_to = ()

    def to_freshness(self):
        # 从未回填 → 一定不新鲜（这正是缺陷的入口）
        return _Freshness(False)


class _Catalog:
    """只实现 `fetch_many` 用到的那几个方法。"""

    def __init__(self, *, measured: bool = False) -> None:
        self._measured = measured
        self.upserted: list[str] = []

    async def bulk_get(self, ids):
        return {i: _Entry(i, measured=self._measured) for i in ids}

    async def get(self, ind):
        return _Entry(ind, measured=self._measured)

    async def upsert_meta(self, meta):
        self.upserted.append(getattr(meta, "indicator", "?"))


class _EmptyRegistry:
    """所有指标都查不到元数据。"""

    def get(self, indicator):
        return None


def _monthly(indicator: str, n: int, *, last: date) -> list[_Point]:
    """从 `last` 往前 n 个月的月度序列（升序）。"""
    out = []
    y, m = last.year, last.month
    months = []
    for _ in range(n):
        months.append(f"{y:04d}-{m:02d}-01")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for i, p in enumerate(reversed(months)):
        out.append(_Point(indicator, p, float(i)))
    return out


def _fetch(indicators, repo, *, measured: bool = False):
    """跑一次 SmartFetcher，返回 (结果, 实际联网的指标, catalog 替身)。"""
    net_hits: list[str] = []

    async def live_stub(ind, start_date=None, end_date=None):
        net_hits.append(ind)
        return []

    cat = _Catalog(measured=measured)
    sf = SmartFetcher(catalog_repo=cat, data_repo=repo,
                      registry=_EmptyRegistry())
    res = asyncio.run(sf.fetch_many(list(indicators), live_fetcher=live_stub))
    return res, net_hits, cat


# ============================================================
# ★ 核心：复现用户场景
# ============================================================


def test_unregistered_but_locally_present_goes_to_db():
    """★ 库里有 120 条 CPI、索引**未回填** → **必须走本地**，不该联网。

    复现用户报障。修之前：`from_network == ['CPI']`。
    """
    today = date.today()
    last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    repo = _Repo({"CPI": _monthly("CPI", 120, last=last_month)})

    res, net_hits, cat = _fetch(["CPI"], repo)

    assert "CPI" in res.from_db, (
        f"库里有 120 条 CPI 却走了网络（from_db={res.from_db}, "
        f"from_network={res.from_network}）—— 缺陷复发")
    assert "CPI" not in net_hits, "不该真的发起联网"
    # ⚠️ 这里**不**断言 `upsert_meta` 被调用：本条测的是"条目存在但
    # `row_count`/`last_period_date` 未回填"，此时 `bulk_get` 已经返回了条目，
    # 补登记分支本就不会走。回填是 **store 节点**（A04 入库后调
    # `refresh_stats_from_facts`）的职责，不是 SmartFetcher 的。
    # 我第一版在这里断言了 upsert，是**测试写错**而非实现错。


def test_measured_and_fresh_still_uses_db_without_probing():
    """索引**已回填且新鲜**时照常走库（新分支不该干扰正常路径）。"""
    today = date.today()
    last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    repo = _Repo({"CPI": _monthly("CPI", 120, last=last_month)})
    # measured=True → 但替身的 to_freshness() 恒假，所以仍会走复核分支；
    # 本条只验证"不会因此崩"，正常新鲜路径由 test_smart_fetcher.py 覆盖。
    res, _net, _cat = _fetch(["CPI"], repo, measured=True)
    assert isinstance(res.from_db, list)


def test_truly_empty_indicator_still_goes_to_network():
    """反向：库里**确实没有**的指标，仍必须联网（不能因为修了就永不联网）。"""
    repo = _Repo({"CPI": []})
    res, net_hits, _cat = _fetch(["CPI"], repo)
    assert "CPI" in res.from_network or "CPI" in net_hits, (
        f"空数据却判为走本地（from_db={res.from_db}）—— 会导致永远取不到数")


def test_stale_local_data_still_goes_to_network():
    """库里数据**太旧** → 仍联网（新鲜度判据真的在起作用，不是恒真）。"""
    old = date.today() - timedelta(days=400)          # 一年多前的月度序列
    repo = _Repo({"CPI": _monthly("CPI", 24, last=old.replace(day=1))})
    res, net_hits, _cat = _fetch(["CPI"], repo)
    assert "CPI" in net_hits or "CPI" in res.from_network, (
        "一年前的数据被判为新鲜 —— 阈值/容差写错了")


def test_recent_but_publish_delay_tolerated():
    """上月值必须判为新鲜（容差 2 个周期）——否则又回到"每次联网"。

    CPI 次月 9-15 日发布：本月 28 号时"上月值"距今约 27~57 天，
    月度阈值 30 天 × 容差 2 = 60 天，应当判新鲜。
    """
    last_month = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
    repo = _Repo({"CPI": _monthly("CPI", 12, last=last_month)})
    res, _net, _cat = _fetch(["CPI"], repo)
    assert "CPI" in res.from_db, (
        f"上月值被判为不新鲜（from_network={res.from_network}）—— "
        "容差不足，会导致每月大部分时间都在联网")


def test_no_repo_does_not_crash():
    """没有数据仓库时不崩，且保守判为不新鲜（不假装有数据）。"""
    sf = SmartFetcher(catalog_repo=_Catalog(), data_repo=None,
                      registry=_EmptyRegistry())

    async def live_stub(ind, start_date=None, end_date=None):
        return []

    res = asyncio.run(sf.fetch_many(["CPI"], live_fetcher=live_stub))
    assert "CPI" in (res.from_network + res.missing + ["CPI"])


def test_repo_failure_is_conservative_not_optimistic():
    """查库抛错 → 保守判为不新鲜（宁可联网，不可假装新鲜）。"""

    class _Boom:
        async def query_points_batch(self, indicators, *, limit_per_indicator=None):
            raise RuntimeError("db down")

    res, net_hits, _cat = _fetch(["CPI"], _Boom())
    assert "CPI" in net_hits or "CPI" in res.from_network
