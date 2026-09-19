"""ConnectorRouter故障转移测试：QMT→CSV→AkShare顺序回退、模拟源防冒充。

外加一组「数据新鲜度下限」用例：QMT 未启动时，链上过旧的源必须让位给更新的源，
全部源都旧时回退本地 DB —— 见文件末尾的分节说明。
"""

import pytest

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint
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


# ==================== 数据新鲜度下限（QMT 未启动时的自动回退） ====================
#
# 实测场景（2026-09-17）：
#   QMT 未启动 → 带区间查询只走网络链 → 链上第一个成功的是「本地行情CSV」
#   （QMT 的导出文件，停在 2026-08-31）→ 直接返回，后面的 AkShare 根本没被尝试；
#   同一时刻本地 SQL 库里躺着 2026-09-16 的完整数据，却因为 skip_db 被跳过。
#   结果日K面板显示两周前的行情。下面几条用例把这套回退行为钉死。


class _FakeRepo:
    """最小 DataPointRepository 替身（只实现路由用到的两个方法）。"""

    def __init__(self, points: list[DataPoint] | None = None,
                 raise_on_query: bool = False) -> None:
        self.points = points or []
        self.saved: list[DataPoint] = []
        self.raise_on_query = raise_on_query
        self.queries: list[tuple] = []

    async def query_points(self, indicator, start_date=None, end_date=None):
        self.queries.append((indicator, start_date, end_date))
        if self.raise_on_query:
            raise RuntimeError("模拟：DB 打不开")
        return list(self.points)

    async def save_points(self, points, task_id="") -> dict:
        self.saved.extend(points)
        return {"inserted": len(points), "skipped": 0, "total": len(points)}


def _point(period: str, value: float = 1.0) -> DataPoint:
    return DataPoint(indicator="stock_close:300308", value=value,
                     period_date=period, extra={"close": value})


def _daily_routes(csv_points, ak_points, *, csv_fail=False, ak_fail=False):
    """(本地CSV, AkShare) 两个连接器 —— 调用方负责按顺序包成 routes。"""
    return (_FakeConnector("本地行情CSV", points=csv_points, fail=csv_fail),
            _FakeConnector("AkShare", points=ak_points, fail=ak_fail))


async def test_ranged_query_skips_source_older_than_local_db():
    """链上第一个源比本地 DB 旧 → 不采用，继续尝试下一个源。"""
    csv_c, ak = _daily_routes([_point("2026-08-31")], [_point("2026-09-16")])
    repo = _FakeRepo([_point("2026-09-16")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:")),
                              (ak, lambda i: i.startswith("stock_close:"))],
                             repo=repo)
    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-17")
    assert [p.period_date for p in out] == ["2026-09-16"]
    assert csv_c.calls == 1 and ak.calls == 1, "过旧的源必须让位给后面的源"


async def test_ranged_query_falls_back_to_db_when_every_source_is_stale():
    """所有源都比本地 DB 旧 → 用 DB（比用更旧的数据强）。"""
    csv_c, ak = _daily_routes([_point("2026-08-31")], [_point("2026-08-25")])
    repo = _FakeRepo([_point("2026-09-16"), _point("2026-09-15")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:")),
                              (ak, lambda i: i.startswith("stock_close:"))],
                             repo=repo)
    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-17")
    assert [p.period_date for p in out] == ["2026-09-16", "2026-09-15"]


async def test_ranged_query_falls_back_to_db_when_network_chain_fails():
    """网络链整体失败 → 回退本地 DB，而不是把异常抛给面板。"""
    csv_c, ak = _daily_routes([], [], csv_fail=True, ak_fail=True)
    repo = _FakeRepo([_point("2026-09-16")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:")),
                              (ak, lambda i: i.startswith("stock_close:"))],
                             repo=repo)
    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-17")
    assert [p.period_date for p in out] == ["2026-09-16"]


async def test_ranged_query_still_prefers_fresh_network_data():
    """网络源比 DB 新（当天的形成中bar）时必须用网络 —— 这是区间查询的初衷。"""
    csv_c = _FakeConnector("本地行情CSV", points=[_point("2026-09-17")])
    ak = _FakeConnector("AkShare", points=[_point("2026-09-16")])
    repo = _FakeRepo([_point("2026-09-16")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:")),
                              (ak, lambda i: i.startswith("stock_close:"))],
                             repo=repo)
    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-17")
    assert [p.period_date for p in out] == ["2026-09-17"]
    assert ak.calls == 0, "第一个源已经够新，不该多打一次网络"


async def test_stale_source_does_not_enter_failure_cooldown():
    """「数据旧」不是「源坏了」：不能记失败冷却，否则非区间查询也会被跳过。"""
    csv_c = _FakeConnector("本地行情CSV", points=[_point("2026-08-31")])
    ak = _FakeConnector("AkShare", points=[_point("2026-09-16")])
    repo = _FakeRepo([_point("2026-09-16")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:")),
                              (ak, lambda i: i.startswith("stock_close:"))],
                             repo=repo)
    await router.fetch("stock_close:300308",
                       start_date="2025-05-01", end_date="2026-09-17")
    assert not router._failure_cache, "过旧不应被记成失败"  # noqa: SLF001
    # 非区间查询（不带下限）时该源仍应被正常使用
    out = await router.fetch("stock_close:300308")
    assert out and csv_c.calls >= 1


async def test_ranged_query_without_db_keeps_first_success():
    """没有注入 repo 时行为不变（第一个成功的源即答案）。"""
    csv_c = _FakeConnector("本地行情CSV", points=[_point("2026-08-31")])
    ak = _FakeConnector("AkShare", points=[_point("2026-09-16")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:")),
                              (ak, lambda i: i.startswith("stock_close:"))])
    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-17")
    assert [p.period_date for p in out] == ["2026-08-31"]
    assert ak.calls == 0


async def test_db_query_failure_still_reaches_network():
    """DB 打不开时 fail-open：照常走网络链（不能因为 DB 故障就取不到数）。"""
    csv_c = _FakeConnector("本地行情CSV", points=[_point("2026-09-16")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo(raise_on_query=True))
    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-17")
    assert [p.period_date for p in out] == ["2026-09-16"]


async def test_non_ranged_flow_unchanged_uses_fresh_db_without_network():
    """非区间查询的老行为必须保持不变：DB 够新就直接返回，不打网络。"""
    csv_c = _FakeConnector("本地行情CSV", points=[_point("2026-08-31")])
    repo = _FakeRepo([_point("2026-09-16")])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:"))],
                             repo=repo)
    out = await router.fetch("stock_close:300308")
    assert [p.period_date for p in out] == ["2026-09-16"]
    assert csv_c.calls == 0


def test_newest_period_helper():
    from src.infrastructure.connectors.router import _newest_period

    assert _newest_period([]) is None
    assert _newest_period([_point("2026-09-16")]) == "2026-09-16"
    assert _newest_period([_point("2026-08-31"), _point("2026-09-16"),
                           _point("2026-01-05")]) == "2026-09-16"
