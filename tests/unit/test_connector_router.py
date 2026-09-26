"""ConnectorRouter故障转移测试：QMT→CSV→AkShare顺序回退、模拟源防冒充。

外加一组「数据新鲜度下限」用例：QMT 未启动时，链上过旧的源必须让位给更新的源，
全部源都旧时回退本地 DB —— 见文件末尾的分节说明。
"""

from datetime import date, timedelta

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
    """非区间查询的老行为必须保持不变：DB 够新就直接返回，不打网络。

    ⚠️ 这里的 DB 日期必须**相对今天**取，不能写死。

    非区间查询走 `_is_db_fresh()`，它以 `date.today()` 为基准按指数衰减算
    confidence，**< 0.4 就判定为 stale 并继续打网络**（`stock_close` 的
    publish_cycle_days=1，隔 2 天就掉到 0.4 以下）。原先这里写死
    `2026-09-16`，于是这条用例在写入当天通过、两天后自己变红 ——
    看起来像"代码坏了"，其实是测试把日历钉死了。
    """
    today = date.today().isoformat()
    csv_c = _FakeConnector("本地行情CSV", points=[_point("2026-08-31")])
    repo = _FakeRepo([_point(today)])
    router = ConnectorRouter([(csv_c, lambda i: i.startswith("stock_close:"))],
                             repo=repo)
    out = await router.fetch("stock_close:300308")
    assert [p.period_date for p in out] == [today]
    assert csv_c.calls == 0


def test_newest_period_helper():
    from src.infrastructure.connectors.router import _newest_period

    assert _newest_period([]) is None
    assert _newest_period([_point("2026-09-16")]) == "2026-09-16"
    assert _newest_period([_point("2026-08-31"), _point("2026-09-16"),
                           _point("2026-01-05")]) == "2026-09-16"


# ==================== 调用方声明的新鲜度下限（min_date） ====================
#
# 2026-09-23 用户报障：日K面板盘中停在 09-22，而当天是 09-23 且行情是活的。
# 根因之一：区间查询的自动下限是「本地 DB 最新日期」= 09-22，于是链上第一个
# 返回 09-22 的源（AkShare 时好时坏）就被采纳，带**当天形成中bar**的腾讯日K
# 根本没被问到。修法是调用方把"我要的是今天的 bar"用 `min_date` 说清楚。
# 下面几条把"传了会怎样、不传还是老样子"都钉住。


async def test_min_date_skips_yesterday_only_source() -> None:
    """传了 min_date=今天：只到昨天的源必须让位给带当天形成中bar的源。"""
    ak = _FakeConnector("AkShare", points=[_point("2026-09-22")])
    tencent = _FakeConnector("腾讯财经日K", points=[_point("2026-09-23")])
    repo = _FakeRepo([_point("2026-09-22")])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))],
                             repo=repo)

    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-23",
                             min_date="2026-09-23")

    assert [p.period_date for p in out] == ["2026-09-23"]
    assert ak.calls == 1 and tencent.calls == 1


async def test_without_min_date_first_acceptable_source_still_wins() -> None:
    """不传 min_date 时既有语义不变：第一个不早于 DB 的源即答案（不多打网络）。"""
    ak = _FakeConnector("AkShare", points=[_point("2026-09-22")])
    tencent = _FakeConnector("腾讯财经日K", points=[_point("2026-09-23")])
    repo = _FakeRepo([_point("2026-09-22")])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))],
                             repo=repo)

    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-23")

    assert [p.period_date for p in out] == ["2026-09-22"]
    assert tencent.calls == 0


async def test_min_date_bypasses_fresh_db_short_circuit() -> None:
    """DB 里的"昨天"对 `_is_db_fresh` 算新鲜，但调用方点名要"今天"时不能被短路。

    日期一律**相对今天**取：写死日期会让用例在几天后自己变红
    （见 `test_non_ranged_flow_unchanged_uses_fresh_db_without_network` 的教训）。
    """
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tencent = _FakeConnector("腾讯财经日K", points=[_point(today)])
    router = ConnectorRouter([(tencent, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([_point(yesterday)]))

    out = await router.fetch("stock_close:300308", min_date=today)

    assert [p.period_date for p in out] == [today]
    assert tencent.calls == 1


async def test_without_min_date_db_short_circuit_unchanged() -> None:
    """同上一幕但不传下限 → DB 命中直接返回（老行为，一个网络都不打）。"""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tencent = _FakeConnector("腾讯财经日K", points=[_point(today)])
    router = ConnectorRouter([(tencent, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([_point(yesterday)]))

    out = await router.fetch("stock_close:300308")

    assert [p.period_date for p in out] == [yesterday]
    assert tencent.calls == 0


async def test_min_date_bypasses_stale_ttl_cache() -> None:
    """TTL 缓存里是"昨天"时，点名要"今天"的请求不能被它挡住。

    `stock_close` 的 TTL 是 **4 小时**：若不看下限，缓存里那根昨天的 bar
    会让下半场的所有请求都拿不到当天的形成中bar（下限等于白设）。
    """
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    ak = _FakeConnector("AkShare", points=[_point(yesterday)])
    tencent = _FakeConnector("腾讯财经日K", points=[_point(today)])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))])

    first = await router.fetch("stock_close:300308")          # 普通请求 → 缓存昨天
    assert [p.period_date for p in first] == [yesterday]

    second = await router.fetch("stock_close:300308", min_date=today)
    assert [p.period_date for p in second] == [today], "点名要今天时必须穿透 TTL 缓存"
    assert tencent.calls == 1


async def test_min_date_ignores_malformed_value() -> None:
    """脏下限（格式不对）当没传 —— 不能让一个坏参数把取数卡死。

    ⚠️ DB 里的日期必须**相对今天**取。

    这条走的是"非区间查询"，而 `_is_db_fresh()` 以 `date.today()` 为基准
    按指数衰减算 confidence，**< 0.4 就判 stale 并继续打网络**
    （`stock_close` 的 `publish_cycle_days=1`，落后 2 天就掉到 0.4 以下）。
    原先写死 `2026-09-22`，于是"DB 够新 → 短路不发网络"的断言在两天后必然失败。
    """
    fresh = date.today().isoformat()
    ak = _FakeConnector("AkShare", points=[_point(fresh)])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([_point(fresh)]))
    out = await router.fetch("stock_close:300308", min_date="2026/09/23")
    assert [p.period_date for p in out] == [fresh]
    assert ak.calls == 0, "下限非法 → 退回 DB 短路的老行为"


async def test_min_date_falls_back_to_db_when_network_dies() -> None:
    """点名要更新的数据、但网络全挂 → 回退本地 DB，而不是把异常抛给面板。

    这条对**不带区间**的调用尤其重要：分时的日线上下文现在也带 `min_date`
    （见 `IntradayService._fetch_daily_bars`），如果网络挂了就 raise，
    面板会从"用昨天的日线上下文"退化成"完全没有日线上下文"。
    """
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    ak = _FakeConnector("AkShare", fail=True)
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([_point(yesterday)]))

    out = await router.fetch("stock_close:300308", min_date=today)
    assert [p.period_date for p in out] == [yesterday]


async def test_min_date_falls_back_to_newest_when_no_source_meets_it() -> None:
    """所有源都够不到下限（例如休市日）→ 取其中最新的一份，而不是报错。"""
    ak = _FakeConnector("AkShare", points=[_point("2026-09-22")])
    tencent = _FakeConnector("腾讯财经日K", points=[_point("2026-09-21")])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([]))
    out = await router.fetch("stock_close:300308",
                             start_date="2025-05-01", end_date="2026-09-23",
                             min_date="2026-09-23")
    assert [p.period_date for p in out] == ["2026-09-22"]
    assert ak.calls == 1 and tencent.calls == 1


# ==================== 「够不到下限」的短期记忆（省掉白等的一跳） ====================
#
# 实测（2026-09-23 盘中，日K口径）：AkShare 每次要 0.4~0.6s 且只给到昨天（够不到下限），
# 腾讯只要 0.12~0.15s 且带当天形成中bar。而"够不到下限"不记失败冷却（设计如此），
# 于是**每次**请求都要先白等 AkShare 那半秒 —— 日K面板每次切标的、
# 自选池每只票的日线上下文，都在重复付这笔钱（前端"刷出来要十秒"的元凶之一）。
# 下面把"短期记忆"的三条约束钉死：跳过、会过期重试、且不破坏兜底。


async def test_stale_source_is_skipped_within_memo_window() -> None:
    """刚确认"够不到下限"的源，短时间内不再重付它的网络延迟。"""
    ak = _FakeConnector("AkShare", points=[_point("2026-09-22")])
    tencent = _FakeConnector("腾讯财经日K", points=[_point("2026-09-23")])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([]), stale_skip=60)

    first = await router.fetch("stock_close:300308",
                               start_date="2025-05-01", end_date="2026-09-23",
                               min_date="2026-09-23")
    assert [p.period_date for p in first] == ["2026-09-23"]
    assert ak.calls == 1 and tencent.calls == 1

    second = await router.fetch("stock_close:300308",
                                start_date="2025-05-01", end_date="2026-09-23",
                                min_date="2026-09-23")
    assert [p.period_date for p in second] == ["2026-09-23"]
    assert ak.calls == 1, "同一源在记忆窗口内不该再被问一次（这就是省的半秒）"
    assert tencent.calls == 2


async def test_stale_memo_disabled_retries_every_time() -> None:
    """`stale_skip=0` = 关闭这层记忆：每次都照旧实打实试一遍链上的源。"""
    ak = _FakeConnector("AkShare", points=[_point("2026-09-22")])
    tencent = _FakeConnector("腾讯财经日K", points=[_point("2026-09-23")])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([]), stale_skip=0)
    for _ in range(2):
        await router.fetch("stock_close:300308",
                           start_date="2025-05-01", end_date="2026-09-23",
                           min_date="2026-09-23")
    assert ak.calls == 2


async def test_stale_memo_not_used_without_floor() -> None:
    """不带下限的请求**不受**这层记忆影响（老口径一字不改）。

    关掉 TTL 缓存才能看见真正的链路行为：否则第二次请求会命中 4 小时的
    `stock_close` 缓存，根本走不到"要不要跳过某个源"这一步。
    """
    ak = _FakeConnector("AkShare", points=[_point("2026-09-22")])
    tencent = _FakeConnector("腾讯财经日K", points=[_point("2026-09-23")])
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([]), stale_skip=60, disable_cache=True)
    await router.fetch("stock_close:300308",
                       start_date="2025-05-01", end_date="2026-09-23",
                       min_date="2026-09-23")
    assert ak.calls == 1

    # 同一指标、不带下限：链首的 AkShare 必须照常被调用（它昨天那份数据是合格答案）
    out = await router.fetch("stock_close:300308")
    assert ak.calls == 2, "不带下限时不该跳过任何源"
    assert [p.period_date for p in out] == ["2026-09-22"]


async def test_stale_memo_still_falls_back_to_stale_data() -> None:
    """只有"够不到下限"的源可用时，跳过也不能变成硬失败 —— 仍返回那份最新数据。"""
    ak = _FakeConnector("AkShare", points=[_point("2026-09-22")])
    tencent = _FakeConnector("腾讯财经日K", fail=True)
    router = ConnectorRouter([(ak, lambda i: i.startswith("stock_close:")),
                              (tencent, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([]), stale_skip=60)

    first = await router.fetch("stock_close:300308",
                               start_date="2025-05-01", end_date="2026-09-23",
                               min_date="2026-09-23")
    assert [p.period_date for p in first] == ["2026-09-22"]

    second = await router.fetch("stock_close:300308",
                                start_date="2025-05-01", end_date="2026-09-23",
                                min_date="2026-09-23")
    assert [p.period_date for p in second] == ["2026-09-22"], (
        "记忆里的那份数据要能兜底，不能因为跳过而 raise")
    assert ak.calls == 1
