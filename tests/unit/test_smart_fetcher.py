"""SmartFetcher 测试（含 DB 优先 + 网络 fallback + 入库）。"""

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

import pytest

from src.core.schemas import (
    Confidence,
    DataPoint,
    DataSourceType,
    FetchMethod,
)
from src.infrastructure.catalog import (
    FreshnessState,
    IndicatorMeta,
    get_registry,
    reset_registry_for_test,
)
from src.infrastructure.catalog.catalog_repo import (
    CatalogRepository,
    reset_catalog_repo_for_test,
)
from src.infrastructure.catalog.smart_fetch import SmartFetcher, SmartFetchResult
from src.infrastructure.repositories.macro_repo import MacroRepository


@pytest.fixture
def repos(tmp_path) -> tuple[CatalogRepository, MacroRepository]:
    db_path = str(tmp_path / "smart_test.db")
    reset_catalog_repo_for_test()
    reset_registry_for_test()
    catalog = CatalogRepository(db_path=db_path)
    data = MacroRepository(db_path=db_path)
    asyncio.run(catalog.ensure_schema())
    asyncio.run(data.ensure_schema())
    return catalog, data


def _make_point(ind: str, period: str, value: float = 1.0,
                fetch_time: datetime | None = None) -> DataPoint:
    ft = fetch_time or datetime.now(timezone.utc)
    return DataPoint(
        data_id=f"d_{ind}_{period}_{value}", indicator=ind,
        value=value, unit="", period_date=period, extra={},
        source_name="test", source_url="",
        source_type=DataSourceType.OFFICIAL,
        publish_time=ft, fetch_time=ft,
        fetch_method=FetchMethod.API_CALL,
        raw_content_hash=f"h_{ind}_{period}_{value}", processed_by="t",
        process_time=ft, confidence=0.9,
    )


def _live_fetcher_factory(mapping: dict[str, list[DataPoint]]):
    """构造一个 live_fetcher：mapping[indicator] = 要返回的 points。"""
    async def fetcher(ind: str, start, end) -> list[DataPoint]:
        return list(mapping.get(ind, []))
    return fetcher


# ============================================================
# 基础路由
# ============================================================


async def test_smart_fetcher_all_fresh_no_network(repos):
    """全部 fresh → 全部走 DB，不调用网络。"""
    catalog, data = repos
    # 入库 2 个 daily 指标（freshness=26h，刚 fetch）
    now = datetime.now(timezone.utc)
    for ind in ["CPI", "PPI"]:
        await data.save_points(
            [_make_point(ind, "2026-09-01", 1.0, fetch_time=now)],
            task_id="t1",
        )
    # 注册元数据（freshness=26h，now → fresh）
    for ind in ["CPI", "PPI"]:
        await catalog.upsert_meta(IndicatorMeta(
            indicator=ind, category="x", frequency="daily",
            freshness_hours=26, primary_source="", source_url="",
            enabled=True, ttl_days=365,
        ))
        await catalog.update_from_points(
            [_make_point(ind, "2026-09-01", 1.0, fetch_time=now)], ind,
        )

    called = []
    async def live(ind, s, e):
        called.append(ind)
        return []

    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_many(["CPI", "PPI"], live_fetcher=live)

    assert set(result.from_db) == {"CPI", "PPI"}
    assert result.from_network == []
    assert called == []  # ★ 没联网


async def test_smart_fetcher_stale_triggers_network(repos):
    """stale → 走网络。"""
    catalog, data = repos
    # 注册元数据（freshness=1h）
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    long_ago_ms = now_ms - 2 * 3600 * 1000  # 2 小时前

    for ind in ["CPI"]:
        await catalog.upsert_meta(IndicatorMeta(
            indicator=ind, category="x", frequency="daily",
            freshness_hours=1, primary_source="", source_url="",
            enabled=True, ttl_days=365,
        ))
    # DB 内有数据但是 2 小时前 → stale
    old_fetch = datetime.fromtimestamp(long_ago_ms / 1000, tz=timezone.utc)
    await data.save_points(
        [_make_point("CPI", "2026-09-01", 1.0, fetch_time=old_fetch)],
        task_id="t1",
    )
    await catalog.update_from_points(
        [_make_point("CPI", "2026-09-01", 1.0, fetch_time=old_fetch)], "CPI",
    )

    called = []
    async def live(ind, s, e):
        called.append(ind)
        return [_make_point("CPI", "2026-10-01", 99.0)]

    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_many(["CPI"], live_fetcher=live)
    assert "CPI" in result.from_network
    assert "CPI" not in result.from_db
    assert called == ["CPI"]


async def test_smart_fetcher_missing_indicator_triggers_network(repos):
    """DB 内没数据 → 走网络（即使 freshness 是 fresh 但 row_count=0）。"""
    catalog, data = repos
    await catalog.upsert_meta(IndicatorMeta(
        indicator="NEW", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    ))
    # DB 没数据

    async def live(ind, s, e):
        return [_make_point("NEW", "2026-10-01", 5.0)]

    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_many(["NEW"], live_fetcher=live)
    assert "NEW" in result.from_network
    assert result.data["NEW"][0].value == 5.0


async def test_smart_fetcher_unregistered_auto_registers(repos):
    """未登记的指标 → 自动 upsert 默认元数据 → 走网络。"""
    catalog, data = repos
    async def live(ind, s, e):
        return [_make_point("XX:UNREG", "2026-10-01", 1.0)]
    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_many(["XX:UNREG"], live_fetcher=live)
    assert "XX:UNREG" in result.unregistered
    assert "XX:UNREG" in result.from_network
    # 索引表已登记
    got = await catalog.get("XX:UNREG")
    assert got is not None
    assert got.frequency == "daily"


async def test_smart_fetcher_network_failure_marks_missing(repos):
    """网络抛错 → 标记 missing，data 不含该指标。"""
    catalog, data = repos
    await catalog.upsert_meta(IndicatorMeta(
        indicator="BAD", category="x", frequency="daily",
        freshness_hours=1, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    ))

    async def live(ind, s, e):
        raise RuntimeError("network down")

    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_many(["BAD"], live_fetcher=live)
    assert "BAD" in result.missing
    assert "BAD" not in result.data


async def test_smart_fetcher_db_only_mode_no_network(repos):
    """fetch_db_only：不联网，只读 DB。"""
    catalog, data = repos
    now = datetime.now(timezone.utc)
    await catalog.upsert_meta(IndicatorMeta(
        indicator="X", category="x", frequency="daily",
        freshness_hours=26, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    ))
    await data.save_points(
        [_make_point("X", "2026-09-01", 1.0, fetch_time=now)], task_id="t1",
    )
    await catalog.update_from_points(
        [_make_point("X", "2026-09-01", 1.0, fetch_time=now)], "X",
    )
    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_db_only(["X"])
    assert "X" in result.from_db
    assert "X" not in result.from_network


async def test_smart_fetcher_writes_back_to_db(repos):
    """网络回来的数据必须写库（让下次查询走 DB）。"""
    catalog, data = repos
    await catalog.upsert_meta(IndicatorMeta(
        indicator="W", category="x", frequency="daily",
        freshness_hours=1, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    ))

    async def live(ind, s, e):
        return [_make_point("W", "2026-10-01", 7.0)]

    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    await fetcher.fetch_many(["W"], live_fetcher=live)

    # DB 内有 W
    pts = await data.query_points("W")
    assert len(pts) == 1
    assert pts[0].value == 7.0

    # catalog 索引也更新了
    entry = await catalog.get("W")
    assert entry.row_count == 1
    assert entry.last_period_date == "2026-10-01"


async def test_smart_fetcher_force_refresh(repos):
    """force_refresh=True 时跳过 freshness 检查，全部联网。"""
    catalog, data = repos
    now = datetime.now(timezone.utc)
    # CPI 是 fresh 的（刚入库）
    await catalog.upsert_meta(IndicatorMeta(
        indicator="CPI", category="x", frequency="daily",
        freshness_hours=26, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    ))
    await data.save_points(
        [_make_point("CPI", "2026-09-01", 1.0, fetch_time=now)], task_id="t1",
    )
    await catalog.update_from_points(
        [_make_point("CPI", "2026-09-01", 1.0, fetch_time=now)], "CPI",
    )

    called = []
    async def live(ind, s, e):
        called.append(ind)
        return [_make_point("CPI", "2026-10-15", 99.0)]

    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_many(["CPI"], live_fetcher=live, force_refresh=True)
    assert called == ["CPI"]
    assert "CPI" in result.from_network
    assert result.data["CPI"][0].value == 99.0


async def test_smart_fetcher_mixed_fresh_and_stale(repos):
    """混合：fresh+stale+missing 同时处理。"""
    catalog, data = repos
    now = datetime.now(timezone.utc)
    old_fetch = datetime.fromtimestamp(
        (datetime.now(timezone.utc).timestamp() - 3 * 3600) * 1000 / 1000,
        tz=timezone.utc,
    )

    for ind in ["FRESH", "STALE", "MISSING"]:
        await catalog.upsert_meta(IndicatorMeta(
            indicator=ind, category="x", frequency="daily",
            freshness_hours=2, primary_source="", source_url="",
            enabled=True, ttl_days=365,
        ))
    # FRESH: now
    await data.save_points(
        [_make_point("FRESH", "2026-09-01", 1.0, fetch_time=now)], task_id="t1")
    await catalog.update_from_points(
        [_make_point("FRESH", "2026-09-01", 1.0, fetch_time=now)], "FRESH")
    # STALE: 3 小时前
    await data.save_points(
        [_make_point("STALE", "2026-09-01", 2.0, fetch_time=old_fetch)], task_id="t1")
    await catalog.update_from_points(
        [_make_point("STALE", "2026-09-01", 2.0, fetch_time=old_fetch)], "STALE")
    # MISSING: 无数据

    async def live(ind, s, e):
        return [_make_point(ind, "2026-10-01", 10.0)]

    fetcher = SmartFetcher(catalog_repo=catalog, data_repo=data)
    result = await fetcher.fetch_many(["FRESH", "STALE", "MISSING"], live_fetcher=live)
    assert "FRESH" in result.from_db
    assert "STALE" in result.from_network
    assert "MISSING" in result.from_network
    assert result.missing == []
    # 三类都有数据
    assert set(result.data.keys()) == {"FRESH", "STALE", "MISSING"}


async def test_smart_fetcher_result_summary(repos):
    """SmartFetchResult.summary() 输出便于审计。"""
    r = SmartFetchResult()
    r.from_db = ["a"]
    r.from_network = ["b"]
    r.missing = ["c"]
    r.data = {"a": [], "b": [], "c": []}
    s = r.summary()
    assert "DB" in s and "联网" in s and "缺口" in s