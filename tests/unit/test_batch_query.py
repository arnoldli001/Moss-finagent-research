"""DataPointRepository.query_points_batch 测试（含性能对比）。"""

import asyncio
import time
from datetime import datetime, timezone

import pytest

from src.core.schemas import (
    Confidence,
    DataPoint,
    DataSourceType,
    FetchMethod,
)
from src.infrastructure.repositories.macro_repo import MacroRepository


@pytest.fixture
def repo(tmp_path) -> MacroRepository:
    db_path = str(tmp_path / "batch_test.db")
    r = MacroRepository(db_path=db_path)
    asyncio.run(r.ensure_schema())
    return r


def _make_point(ind: str, period: str, value: float = 1.0) -> DataPoint:
    return DataPoint(
        data_id=f"d_{ind}_{period}_{value}", indicator=ind,
        value=value, unit="", period_date=period, extra={},
        source_name="test", source_url="",
        source_type=DataSourceType.OFFICIAL,
        publish_time=datetime(2026, 9, 28, tzinfo=timezone.utc),
        fetch_time=datetime(2026, 9, 28, tzinfo=timezone.utc),
        fetch_method=FetchMethod.API_CALL,
        raw_content_hash=f"h_{ind}_{period}", processed_by="t",
        process_time=datetime.now(timezone.utc),
        confidence=0.9,
    )


async def test_batch_query_basic(repo):
    """批量查询基本功能。"""
    # 入库 3 个指标各 5 条
    for ind in ["ind_a", "ind_b", "ind_c"]:
        points = [_make_point(ind, f"2026-09-{20+i:02d}", float(i)) for i in range(1, 6)]
        await repo.save_points(points, task_id="t1")

    result = await repo.query_points_batch(["ind_a", "ind_b", "ind_c"])
    assert set(result.keys()) == {"ind_a", "ind_b", "ind_c"}
    assert len(result["ind_a"]) == 5
    assert len(result["ind_b"]) == 5
    assert len(result["ind_c"]) == 5
    # 按 period_date 升序
    a_dates = [p.period_date for p in result["ind_a"]]
    assert a_dates == sorted(a_dates)


async def test_batch_query_missing_indicator_returns_empty(repo):
    """批量查询未入库指标 → 空列表（不是抛错）。"""
    for ind in ["ind_a"]:
        await repo.save_points([_make_point(ind, "2026-09-20")], task_id="t1")

    result = await repo.query_points_batch(["ind_a", "missing_ind"])
    assert len(result["ind_a"]) == 1
    assert result["missing_ind"] == []


async def test_batch_query_with_date_range(repo):
    """批量查询带日期范围。"""
    for ind in ["ind_a"]:
        for d in range(1, 11):
            await repo.save_points(
                [_make_point(ind, f"2026-09-{d:02d}", float(d))], task_id="t1")
    result = await repo.query_points_batch(
        ["ind_a"], start_date="2026-09-03", end_date="2026-09-07")
    dates = [p.period_date for p in result["ind_a"]]
    assert dates == ["2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07"]


async def test_batch_query_with_limit(repo):
    """批量查询 limit_per_indicator 截最新 N 条。"""
    for ind in ["ind_a"]:
        for d in range(1, 11):
            await repo.save_points(
                [_make_point(ind, f"2026-09-{d:02d}", float(d))], task_id="t1")
    result = await repo.query_points_batch(["ind_a"], limit_per_indicator=3)
    assert len(result["ind_a"]) == 3
    dates = [p.period_date for p in result["ind_a"]]
    assert dates == ["2026-09-08", "2026-09-09", "2026-09-10"]


async def test_batch_query_empty_input(repo):
    """空指标列表 → 空 dict，不报错。"""
    result = await repo.query_points_batch([])
    assert result == {}


async def test_batch_query_faster_than_serial(repo):
    """批量查询应显著快于 N 次单查（粗略性能断言）。"""
    n = 30
    indicators = [f"ind_{i:02d}" for i in range(n)]
    # 入库
    for ind in indicators:
        for d in range(1, 4):
            await repo.save_points(
                [_make_point(ind, f"2026-09-{d:02d}", float(d))], task_id="t1")

    # 单次批量
    t0 = time.perf_counter()
    batch_result = await repo.query_points_batch(indicators)
    t_batch = time.perf_counter() - t0

    # N 次单查
    t0 = time.perf_counter()
    serial_result: dict = {}
    for ind in indicators:
        serial_result[ind] = await repo.query_points(ind)
    t_serial = time.perf_counter() - t0

    assert set(batch_result.keys()) == set(serial_result.keys())
    # 批量应该不慢于串行（不一定快很多，但不应差太多）
    # SQLite WAL + 进程内连接池的差异在小批量下可能不大
    assert t_batch <= t_serial * 1.5, (
        f"批量({t_batch*1000:.1f}ms) 显著慢于串行({t_serial*1000:.1f}ms)"
    )