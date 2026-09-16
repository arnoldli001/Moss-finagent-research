"""LocalCsvConnector测试：tmp_path造迅投CSV，不依赖D:\\quantTrader。"""

import pytest

from src.core.exceptions import DataFetchError
from src.core.schemas import DataSourceType, FetchMethod
from src.infrastructure.connectors.local_csv_connector import (
    LocalCsvConnector,
    csv_rows_to_points,
)

CSV_HEADER = "timetag,open,high,low,close,volumn,amount"


def _write_csv(base, market, code, rows: list[str]):
    d = base / market
    d.mkdir(parents=True, exist_ok=True)
    (d / f"price_{code}.csv").write_text(
        CSV_HEADER + "\n" + "\n".join(rows), encoding="utf-8")


async def test_fetch_sh_stock(tmp_path):
    _write_csv(tmp_path, "SH", "600000", [
        "20260910,10.0,10.2,9.9,10.1,1000,10100",
        "20260911,10.1,10.3,10.0,10.2,2000,20400",
    ])
    connector = LocalCsvConnector(str(tmp_path))
    points = await connector.fetch("stock_close:600000")
    assert len(points) == 2
    p = points[-1]
    assert p.indicator == "stock_close:600000"
    assert p.value == 10.2
    assert p.period_date == "2026-09-11"
    assert p.source_type == DataSourceType.FILE
    assert p.fetch_method == FetchMethod.FILE_READ
    assert p.extra["volume"] == 2000


async def test_sz_market_directory(tmp_path):
    _write_csv(tmp_path, "SZ", "000001", ["20260911,6.8,6.9,6.7,6.75,840,836014"])
    points = await LocalCsvConnector(str(tmp_path)).fetch("stock_close:000001")
    assert len(points) == 1
    assert points[0].value == 6.75


async def test_date_range_filter(tmp_path):
    _write_csv(tmp_path, "SH", "600000", [
        "20260801,10,10,10,10,1,10",
        "20260901,11,11,11,11,1,11",
        "20260911,12,12,12,12,1,12",
    ])
    connector = LocalCsvConnector(str(tmp_path))
    points = await connector.fetch(
        "stock_close:600000", start_date="2026-09-01", end_date="20260930")
    assert [p.period_date for p in points] == ["2026-09-01", "2026-09-11"]


async def test_missing_file_raises(tmp_path):
    connector = LocalCsvConnector(str(tmp_path))
    with pytest.raises(DataFetchError):
        await connector.fetch("stock_close:600000")


async def test_unconfigured_dir_raises():
    connector = LocalCsvConnector("")
    with pytest.raises(DataFetchError):
        await connector.fetch("stock_close:600000")


def test_csv_rows_to_points_skips_bad_rows():
    rows = [
        {"timetag": "bad", "close": "1"},
        {"timetag": "20260911", "open": "1", "high": "1", "low": "1",
         "close": "x", "volumn": "1", "amount": "1"},
        {"timetag": "20260911", "open": "1", "high": "1", "low": "1",
         "close": "10.5", "volumn": "1", "amount": "1"},
    ]
    points = csv_rows_to_points(rows, "stock_close:600000", "file://x")
    assert len(points) == 1
    assert points[0].value == 10.5
