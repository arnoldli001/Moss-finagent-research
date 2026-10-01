"""CatalogRepository 单元测试（SQLite in-memory）。"""

from datetime import datetime, timezone

import pytest

from src.core.schemas import (
    Confidence,
    DataPoint,
    DataSourceType,
    FetchMethod,
)
from src.infrastructure.catalog import (
    IndicatorMeta,
    get_registry,
    reset_registry_for_test,
)
from src.infrastructure.catalog.catalog_repo import (
    CatalogRepository,
    get_catalog_repository,
    reset_catalog_repo_for_test,
)


@pytest.fixture
def repo(tmp_path) -> CatalogRepository:
    """每个测试独立的 in-memory 仓储。"""
    db_path = str(tmp_path / "catalog_test.db")
    r = CatalogRepository(db_path=db_path)
    reset_catalog_repo_for_test()
    reset_registry_for_test()
    return r


def test_ensure_schema_idempotent(repo):
    """ensure_schema 重复调用不报错。"""
    import asyncio
    asyncio.run(repo.ensure_schema())
    asyncio.run(repo.ensure_schema())  # 第二次


def test_upsert_meta_inserts_new(repo):
    """upsert_meta 新增条目。"""
    import asyncio
    asyncio.run(repo.ensure_schema())
    meta = IndicatorMeta(
        indicator="test:new", category="x", frequency="daily",
        freshness_hours=24, primary_source="ak", source_url="",
        enabled=True, ttl_days=365,
    )
    asyncio.run(repo.upsert_meta(meta))
    got = asyncio.run(repo.get("test:new"))
    assert got is not None
    assert got.category == "x"
    assert got.frequency == "daily"
    assert got.freshness_hours == 24
    assert got.row_count == 0  # 运行时字段未变
    assert got.first_seen_at > 0


def test_upsert_meta_updates_existing(repo):
    """upsert_meta 更新元数据；only_if_missing=True 不覆盖已有元数据。"""
    import asyncio
    asyncio.run(repo.ensure_schema())
    meta1 = IndicatorMeta(
        indicator="test:upd", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    asyncio.run(repo.upsert_meta(meta1))

    meta2 = IndicatorMeta(
        indicator="test:upd", category="y", frequency="monthly",
        freshness_hours=720, primary_source="new", source_url="",
        enabled=False, ttl_days=1800,
    )
    asyncio.run(repo.upsert_meta(meta2))
    got = asyncio.run(repo.get("test:upd"))
    assert got.category == "y"  # 默认 ON CONFLICT 更新

    asyncio.run(repo.upsert_meta(meta2, only_if_missing=True))
    got2 = asyncio.run(repo.get("test:upd"))
    assert got2.category == "y"  # 已存在（即使已更新过），only_if_missing 不再触发更新


def test_mark_updated_basic(repo):
    """mark_updated 写入运行时字段。"""
    import asyncio
    import time
    asyncio.run(repo.ensure_schema())
    meta = IndicatorMeta(
        indicator="test:mark", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    asyncio.run(repo.upsert_meta(meta))

    now_ms = int(time.time() * 1000)
    asyncio.run(repo.mark_updated(
        "test:mark", period_date="2026-09-28",
        fetch_time_ms=now_ms, row_count_delta=10,
    ))
    got = asyncio.run(repo.get("test:mark"))
    assert got.last_period_date == "2026-09-28"
    assert got.last_fetch_time_ms == now_ms
    assert got.row_count == 10
    assert got.first_seen_at > 0


def test_mark_updated_preserves_max_fetch_time(repo):
    """多次 mark_updated 取 max(fetch_time)，避免旧 fetch 覆盖新 fetch。"""
    import asyncio
    import time
    asyncio.run(repo.ensure_schema())
    meta = IndicatorMeta(
        indicator="test:max", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    asyncio.run(repo.upsert_meta(meta))

    # 第一次：写入 100
    asyncio.run(repo.mark_updated(
        "test:max", period_date="2026-09-27",
        fetch_time_ms=100, row_count_delta=5,
    ))
    # 第二次：写入 50（更早）
    asyncio.run(repo.mark_updated(
        "test:max", period_date="2026-09-26",
        fetch_time_ms=50, row_count_delta=3,
    ))
    got = asyncio.run(repo.get("test:max"))
    # 应该是 100（max）
    assert got.last_fetch_time_ms == 100
    assert got.row_count == 8  # 累加


def test_bulk_get_returns_dict(repo):
    """bulk_get 一次 SELECT 返回 dict。"""
    import asyncio
    asyncio.run(repo.ensure_schema())
    for ind in ["a", "b", "c"]:
        meta = IndicatorMeta(
            indicator=ind, category="x", frequency="daily",
            freshness_hours=24, primary_source="", source_url="",
            enabled=True, ttl_days=365,
        )
        asyncio.run(repo.upsert_meta(meta))

    got = asyncio.run(repo.bulk_get(["a", "b", "missing"]))
    assert "a" in got and "b" in got
    assert "missing" not in got
    assert len(got) == 2


def test_refresh_from_registry_loads_yaml(repo):
    """refresh_from_registry 把 YAML 全量同步到 DB。"""
    import asyncio
    asyncio.run(repo.ensure_schema())
    count = asyncio.run(repo.refresh_from_registry())
    assert count >= 30  # YAML 至少 30+ 条

    got = asyncio.run(repo.get("CPI"))
    assert got is not None
    assert got.frequency == "monthly"
    assert got.freshness_hours == 720


def test_all_only_enabled(repo):
    """all(only_enabled=True) 过滤 disabled。"""
    import asyncio
    asyncio.run(repo.ensure_schema())
    meta_on = IndicatorMeta(
        indicator="on", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    meta_off = IndicatorMeta(
        indicator="off", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=False, ttl_days=365,
    )
    asyncio.run(repo.upsert_meta(meta_on))
    asyncio.run(repo.upsert_meta(meta_off))

    all_entries = asyncio.run(repo.all(only_enabled=False))
    enabled_only = asyncio.run(repo.all(only_enabled=True))
    assert any(e.indicator == "on" for e in all_entries)
    assert any(e.indicator == "off" for e in all_entries)
    assert all(e.indicator != "off" for e in enabled_only)


def test_update_from_points(repo):
    """update_from_points 从 DataPoint 列表反推 last_* 字段。"""
    import asyncio
    asyncio.run(repo.ensure_schema())
    meta = IndicatorMeta(
        indicator="test:frompts", category="x", frequency="daily",
        freshness_hours=24, primary_source="", source_url="",
        enabled=True, ttl_days=365,
    )
    asyncio.run(repo.upsert_meta(meta))

    points = [
        DataPoint(
            data_id=f"d_{i}", indicator="test:frompts", value=float(i),
            unit="", period_date=f"2026-09-{20+i:02d}",
            extra={}, source_name="test", source_url="",
            source_type=DataSourceType.OFFICIAL,
            publish_time=datetime(2026, 9, 20 + i, tzinfo=timezone.utc),
            fetch_time=datetime(2026, 9, 28, 12, i, tzinfo=timezone.utc),
            fetch_method=FetchMethod.API_CALL,
            raw_content_hash=f"h_{i}", processed_by="t1",
            process_time=datetime.now(timezone.utc),
            confidence=0.9,
        )
        for i in range(1, 4)
    ]
    asyncio.run(repo.update_from_points(points, "test:frompts"))
    got = asyncio.run(repo.get("test:frompts"))
    assert got.last_period_date == "2026-09-23"  # 最大
    assert got.row_count == 3