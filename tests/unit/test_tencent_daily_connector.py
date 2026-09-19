"""腾讯日K连接器测试（离线：行解析纯函数 + 假 httpx client）。"""

from __future__ import annotations

import httpx
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.tencent_daily_connector import (
    TencentDailyConnector,
    rows_to_points,
    to_tencent_symbol,
)

# 腾讯行序：日期, 开, 收, 高, 低, 成交量(手)
_ROWS = [
    ["2026-09-15", "873.38", "864.01", "883.73", "858.00", "179554.41"],
    ["2026-09-16", "869.02", "907.80", "918.38", "866.00", "240218.46"],
]


@pytest.mark.parametrize(
    ("code", "expected"),
    [("300308", "sz300308"), ("002463", "sz002463"), ("600036", "sh600036"),
     ("688981", "sh688981"), ("000001", "sz000001")],
)
def test_symbol_mapping(code: str, expected: str) -> None:
    assert to_tencent_symbol(code) == expected


@pytest.mark.parametrize("bad", ["30030", "abcdef", "", "830799", "920001",
                                 "588170", "510300"])
def test_symbol_rejects_bj_and_etf(bad: str) -> None:
    with pytest.raises(DataFetchError):
        to_tencent_symbol(bad)


def test_rows_mapped_with_correct_column_order() -> None:
    """腾讯行是「开、**收**、高、低」—— 把 high/low 读反会让K线形态整体错位。"""
    points = rows_to_points(_ROWS, "stock_close:300308", symbol="sz300308")
    assert [p.period_date for p in points] == ["2026-09-15", "2026-09-16"]
    latest = points[-1]
    assert latest.value == pytest.approx(907.80)          # close
    assert latest.extra["open"] == pytest.approx(869.02)
    assert latest.extra["high"] == pytest.approx(918.38)  # 不是 866.00
    assert latest.extra["low"] == pytest.approx(866.00)   # 不是 918.38
    assert latest.extra["volume"] == pytest.approx(240218.46)  # 手
    assert latest.extra["amount"] is None, "腾讯日K不返回成交额，不能编造"
    assert latest.extra["adjust"] == "qfq"


def test_rows_filtered_by_range_and_bad_rows_skipped() -> None:
    rows = [*_ROWS, ["bad-date", "1", "1", "1", "1", "1"], ["2026-09-14"]]
    points = rows_to_points(rows, "stock_close:300308", symbol="sz300308",
                            start_date="2026-09-16", end_date="2026-09-17")
    assert [p.period_date for p in points] == ["2026-09-16"]


def test_supports_only_stock_close() -> None:
    assert TencentDailyConnector.supports("stock_close:300308")
    assert not TencentDailyConnector.supports("index_close:000001")
    assert not TencentDailyConnector.supports("etf_close:588170")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, *, boom: bool = False) -> None:
        self.payload = payload
        self.boom = boom
        self.urls: list[str] = []
        self.closed = False

    async def get(self, url: str):
        self.urls.append(url)
        if self.boom:
            raise httpx.ConnectError("模拟：连接被重置")
        return _FakeResponse(self.payload)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fetch_parses_qfqday_node() -> None:
    client = _FakeClient({"data": {"sz300308": {"qfqday": _ROWS}}})
    connector = TencentDailyConnector(client=client)
    points = await connector.fetch("stock_close:300308")
    assert [p.period_date for p in points] == ["2026-09-15", "2026-09-16"]
    assert points[-1].source_name == "腾讯财经日K"
    assert "qfq" in client.urls[0], "必须显式请求前复权"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_day_node() -> None:
    client = _FakeClient({"data": {"sz300308": {"day": _ROWS}}})
    points = await TencentDailyConnector(client=client).fetch("stock_close:300308")
    assert len(points) == 2
    assert points[-1].extra["adjust"] == "qfq"


@pytest.mark.asyncio
async def test_fetch_raises_on_network_error() -> None:
    client = _FakeClient(boom=True)
    with pytest.raises(DataFetchError, match="请求失败"):
        await TencentDailyConnector(client=client).fetch("stock_close:300308")


@pytest.mark.asyncio
async def test_fetch_raises_on_empty_payload() -> None:
    client = _FakeClient({"data": {"sz300308": {}}})
    with pytest.raises(DataFetchError, match="无"):
        await TencentDailyConnector(client=client).fetch("stock_close:300308")


@pytest.mark.asyncio
async def test_fetch_rejects_unsupported_indicator() -> None:
    with pytest.raises(DataFetchError, match="不支持"):
        await TencentDailyConnector(client=_FakeClient({})).fetch("index_close:000001")
