"""ConnectorRouter故障转移测试：QMT→CSV→AkShare顺序回退、模拟源防冒充。"""

import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.router import ConnectorRouter


class _FakeConnector:
    def __init__(self, name: str, *, simulated: bool = False,
                 fail: bool = False, boom: bool = False,
                 points: list | None = None):
        self.source_name = name
        self.source_url = f"test://{name}"
        self._simulated = simulated
        self._fail = fail
        self._boom = boom
        self._points = points if points is not None else []
        self.calls = 0

    def get_capabilities(self):
        return {"name": self.source_name, "simulated": self._simulated,
                "indicators": ["X"]}

    async def fetch(self, indicator, start_date=None, end_date=None):
        self.calls += 1
        if self._boom:
            raise RuntimeError("非DataFetchError不应被吞掉")
        if self._fail:
            raise DataFetchError(f"{self.source_name}不可用")
        return self._points


async def test_first_match_success_no_fallback():
    a = _FakeConnector("qmt", points=["p1"])
    b = _FakeConnector("csv")
    router = ConnectorRouter([
        (a, lambda i: i == "X"),
        (b, lambda i: i == "X"),
    ])
    assert await router.fetch("X") == ["p1"]
    assert a.calls == 1 and b.calls == 0


async def test_failover_to_next_real_source():
    qmt = _FakeConnector("迅投QMT", fail=True)
    csv_c = _FakeConnector("本地行情CSV", points=["csv1", "csv2"])
    ak = _FakeConnector("AkShare", points=["ak1"])
    router = ConnectorRouter([
        (qmt, lambda i: i == "stock_close:601088"),
        (csv_c, lambda i: i.startswith("stock_close:")),
        (ak, lambda i: i.startswith("stock_close:")),
    ])
    out = await router.fetch("stock_close:601088")
    assert out == ["csv1", "csv2"]
    assert qmt.calls == 1 and csv_c.calls == 1 and ak.calls == 0


async def test_all_real_sources_fail_raises_chain():
    qmt = _FakeConnector("迅投QMT", fail=True)
    csv_c = _FakeConnector("本地行情CSV", fail=True)
    ak = _FakeConnector("AkShare", fail=True)
    router = ConnectorRouter([
        (qmt, lambda i: True),
        (csv_c, lambda i: True),
        (ak, lambda i: True),
    ])
    with pytest.raises(DataFetchError) as ei:
        await router.fetch("X")
    msg = str(ei.value)
    assert "迅投QMT" in msg and "AkShare" in msg and "均失败" in msg


async def test_never_fallback_to_simulated_after_real_failure():
    """真实源失败后不得静默回退模拟源（红线：假数据不得冒充真实）。"""
    real = _FakeConnector("AkShare", fail=True)
    mock = _FakeConnector("模拟产业数据(Demo)", simulated=True,
                          points=["fake_point"])
    router = ConnectorRouter([
        (real, lambda i: i == "ind:社会消费品零售总额同比"),
        (mock, lambda i: i.startswith("ind:")),
    ])
    with pytest.raises(DataFetchError):
        await router.fetch("ind:社会消费品零售总额同比")
    assert real.calls == 1 and mock.calls == 0


async def test_simulated_only_indicator_still_served_directly():
    """无真实源覆盖的指标：模拟连接器作为首个（唯一）命中者正常服务。"""
    mock = _FakeConnector("模拟产业数据(Demo)", simulated=True,
                          points=["demo1"])
    router = ConnectorRouter([(mock, lambda i: i == "ind:白酒批价(元/瓶)")])
    assert await router.fetch("ind:白酒批价(元/瓶)") == ["demo1"]


async def test_unexpected_error_propagates_not_swallowed():
    """非DataFetchError（程序缺陷）必须立即上抛，不能误触发回退。"""
    broken = _FakeConnector("broken", boom=True)
    backup = _FakeConnector("backup", points=["x"])
    router = ConnectorRouter([
        (broken, lambda i: True),
        (backup, lambda i: True),
    ])
    with pytest.raises(RuntimeError):
        await router.fetch("X")
    assert backup.calls == 0


async def test_no_matching_connector_raises_known_list():
    router = ConnectorRouter([])
    with pytest.raises(DataFetchError, match="无连接器支持指标"):
        await router.fetch("nope")
