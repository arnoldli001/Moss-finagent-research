"""第三跳**内部**子路径自报的契约 —— 「这次 fetch 走的是哪条近路」必须量得出来。

## 为什么这个文件必须存在

`ConnectorRouter.fetch()` 内部是一条**三级短路**：
TTL 缓存（内存）→ 本地持久化库（DB 快照）→ 连接器链（**真联网**）。
而调用方（A01 采集侧）只看到「返回了 / 没返回」一个结果 ⇒ 三件事**给不出来**：

    · 本地命中率 —— 第三跳内部有多少次**根本没联网**（「本地优先」唯一的证据）
    · 联网触发率 —— 真正打网络的比例（成本的分子）
    · 为什么慢 —— 走了连接器，还是**冷却 / 防撞钟预算**把它挡下了

缺口是**登记过的**：`src/domain/agents/data/collector/path_stats.py` 的
「诚实边界」一节写着「要真正的逐跳细分，得由 `ConnectorRouter` 自报它走过的
子路径」。唯一事实源是 `src/infrastructure/connectors/subpath_stats.py`。

## 判据清单（每条都对应一种会静默变绿 / 变假的失效）

| 判据 | 防的是 |
|---|---|
| ① TTL 命中 ⇒ `ttl_cache`，且连接器**一次没被调用** | 缓存命中记成网络（本地命中率消失） |
| ② DB 命中 ⇒ `db_snapshot`，同样**零联网** | 同上；「本地优先」唯一的证据 |
| ③ 真联网 ⇒ `connector_network` | 联网触发率/成本口径少算 |
| ④ 冷却拒绝 ⇒ `cooldown_refused`（**零请求**） | 「源已知坏、等冷却」混进「联网失败」 |
| ⑤ 预算拒绝 ⇒ `budget_refused`（**不是** `miss`） | 「活太重、挪去定时作业」读成「源坏了」 |
| ⑤b 不支持该指标 ⇒ `not_supported` | 契约不满足被读成源故障（改错东西） |
| ⑤c 联网走过、结果由库给出 ⇒ `db_snapshot_fallback` | 「白联一次网」记成「本地命中」 |
| ⑥ 空态：**还没量到** ≠ **量到 0** | 冷启动读成「一次都没联网」（假故障） |
| ⑦ 「一次 fetch = 一条子路径」由**结构**保证 | 多处各记一次 ⇒ 命中率虚高且看不出来 |
| ⑧ ★ **自证**：绕过真实入口后计数**纹丝不动** | 计数挂在没人走的路上 ⇒ 恒绿而链路没量到 |
| ⑨ `/health` 的 `query_data_hops` 带着它，**坏了只降级自己** | 常量在、接线不在；或拖挂健康检查 |

★ 判据 ⑧ 是本文件的**自证**：`_fetch_uncached` 是**真实**的第三跳网络实现
（拿到数据、替身连接器真的被调用），但它不是公共入口 —— 直接调它时计数必须
**全 0**；随后**同一个替身**经 `fetch()` 必须 +1。两条合起来才证明「计数挂在
真实路径上」，而不是「接了但没人走」（本项目实测过 `describe()` 零个生产调用方）。

## 红灯自证（真摘一次打点，跑出失败再还原）

    把 router.py 里 TTL 分支那一行 `outcome.mark(SUBPATH_TTL_CACHE)` 删掉：
    ① 变红（应记 ttl_cache，实际 0），`unexpected` 同时 +1（漏埋点自鸣）。

## 绝不联网、绝不写盘

所有连接器/仓库都是**进程内替身**；本文件不碰 `network_fallback` 的账本
（那条路要写 `data/run/`，是 dev/pilot/生产共用的）。

跑法：
    uv run python -m pytest tests/unit/test_router_subpath_stats.py -q
"""
from __future__ import annotations

import asyncio
import time
from datetime import date

import pytest

from src.core import hop_stats
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint
from src.infrastructure.catalog import network_fallback as nf
from src.infrastructure.connectors import subpath_stats
from src.infrastructure.connectors.router import ConnectorRouter

# ============================================================
# 测试替身（全部进程内；不打桩任何"计数"本身）
# ============================================================


class _FakeConnector:
    """替身连接器：`calls` 用来断言「这次**没有**联网」。"""

    def __init__(self, name: str, *, points: list[DataPoint] | None = None,
                 fail: bool = False, delay: float = 0.0) -> None:
        self.source_name = name
        self.source_url = f"test://{name}"
        self._points = list(points or [])
        self._fail = fail
        self._delay = delay
        self.calls = 0

    def get_capabilities(self) -> dict:
        return {"name": self.source_name, "simulated": False,
                "indicators": ["CPI"]}

    async def fetch(self, indicator, start_date=None, end_date=None):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise DataFetchError(f"{self.source_name}不可用")
        return list(self._points)


class _FakeRepo:
    """最小 `DataPointRepository` 替身（只实现路由用到的两个方法）。"""

    def __init__(self, points: list[DataPoint] | None = None) -> None:
        self.points = list(points or [])
        self.saved: list[DataPoint] = []
        self.queries: list[tuple] = []

    async def query_points(self, indicator, start_date=None, end_date=None):
        self.queries.append((indicator, start_date, end_date))
        return list(self.points)

    async def save_points(self, points, task_id="") -> dict:
        self.saved.extend(points)
        return {"inserted": len(points), "skipped": 0, "total": len(points)}


def _point(period: str, indicator: str = "CPI") -> DataPoint:
    return DataPoint(indicator=indicator, value=1.0, period_date=period,
                     source_name="替身源")


def _counters() -> dict:
    return dict(subpath_stats.snapshot()["counters"])


def _only(key: str) -> None:
    """断言这次 fetch **只**记 `key` 一条（逐次互斥 + 分母 +1 + 无漏埋点）。"""
    counts = _counters()
    snap = subpath_stats.snapshot()
    assert counts[key] == 1, f"应记 {key}，实际 {counts}"
    assert [k for k, v in counts.items() if v] == [key], (
        f"一次 fetch 记了多条子路径（互斥被破坏）：{counts}")
    assert snap["total"] == 1, f"分母必须恰好 +1，实际 {snap['total']}"
    assert snap["latest_subpath"] == key, (
        f"最近一次子路径应如实上报 {key}：{snap['latest_subpath']}")
    assert snap["unexpected"] == 0, (
        "有 fetch 走完却一处都没登记子路径（漏埋点）—— 打点接在真实路径上了吗？")


# ============================================================
# 隔离（autouse）：每条判据都从冷启动态开始
# ============================================================


@pytest.fixture(autouse=True)
def _clean_stats():
    """计数是**进程级**的，不清就会串用例（同 `test_hop_stats.py` 的纪律）。

    顺带钉住 `reset_for_test()`：清完必须**恰好**是冷启动态
    （全 0 + `latest_subpath` 回到「未量到」+ 缺陷计数归零）。
    """
    subpath_stats.reset_for_test()
    hop_stats.reset_for_test()
    snap = subpath_stats.snapshot()
    assert all(v == 0 for v in snap["counters"].values()), snap["counters"]
    assert snap["latest_subpath"] == subpath_stats.UNMEASURED_SUBPATH
    assert snap["total"] == 0 and snap["unexpected"] == 0
    yield
    subpath_stats.reset_for_test()
    hop_stats.reset_for_test()


# ============================================================
# ① TTL 缓存命中
# ============================================================


async def test_ttl_cache_hit_is_recorded_and_never_touches_the_connector():
    """TTL 命中 ⇒ `ttl_cache`，且连接器**一次都没被调用**。

    防的是"把缓存命中记成联网"：那会让「本地命中率」凭空消失，
    而表现只是比例难看 —— 不报错。CPI 的 TTL 是 24h，所以同进程第二次必命中。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])

    await router.fetch("CPI")               # 第一次：联网并**回填 TTL**
    assert stub.calls == 1
    subpath_stats.reset_for_test()          # 只看第二次（TTL 已经养好）

    out = await router.fetch("CPI")

    assert out, "TTL 命中却返回空 —— 这个替身的前提不成立"
    assert stub.calls == 1, "TTL 命中了却仍然打了连接器"
    _only(subpath_stats.SUBPATH_TTL_CACHE)


# ============================================================
# ② 本地库（DB 快照）命中
# ============================================================


async def test_db_snapshot_hit_is_recorded_and_never_touches_the_connector():
    """DB 命中 ⇒ `db_snapshot`，且**零联网**。

    ⚠️ 库里的日期必须**相对今天**取，不能写死：非区间查询走 `_is_db_fresh()`，
    它以 `date.today()` 为基准算指数衰减，写死日期会让用例过几天自己变红
    （`test_connector_router.py` 里登记过这条教训）。
    """
    today = date.today().isoformat()
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")],
                             repo=_FakeRepo([_point(today)]))

    out = await router.fetch("CPI")

    assert [p.period_date for p in out] == [today]
    assert stub.calls == 0, "本地库命中了却打了连接器"
    _only(subpath_stats.SUBPATH_DB_SNAPSHOT)


# ============================================================
# ③ 真联网
# ============================================================


async def test_real_network_is_recorded_as_connector_network():
    """连接器链真的被走到 ⇒ `connector_network`（联网触发率的分子）。"""
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])

    out = await router.fetch("CPI")

    assert out and stub.calls == 1
    _only(subpath_stats.SUBPATH_CONNECTOR_NETWORK)


async def test_network_returning_empty_still_counts_as_network():
    """链上**返回空**（没抛异常）仍记 `connector_network`，**不是** `miss`。

    为什么把这条单独钉住：它答的问题是「**走没走**网络」（成本/触发率一分都不能
    少算 —— 请求真的发出去了），而「网络**给没给**数据」由 `miss`（链上全失败）
    承载。两个维度混起来，运维会看到"没联网"，而账单与延迟都在涨。
    """
    stub = _FakeConnector("替身源", points=[])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])

    out = await router.fetch("CPI")

    assert out == [] and stub.calls == 1, "前提：请求真的发出去了，只是源上没有"
    _only(subpath_stats.SUBPATH_CONNECTOR_NETWORK)


# ============================================================
# ④ 被冷却拒 / 被预算拒 —— 各自的键，**不许**记成失败或网络
# ============================================================


async def test_cooldown_refusal_is_its_own_key_and_sends_no_request():
    """全源在失败冷却中 ⇒ `cooldown_refused`，且**一次网络请求都没发**。

    与「网络失败」处置相反：前者是"源已知坏、等冷却"，后者是"这次没成功"。
    混成一个键，「联网失败率」会虚高 —— 而它读起来完全正常。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])
    router._failure_cache["替身源:CPI"] = (time.monotonic() + 300, "上游 503")

    out = await router.fetch("CPI")

    assert out == [], "全冷却时按既有语义返回空列表（交给上层兜底）"
    assert stub.calls == 0, "冷却拒绝必须一次网络请求都不发"
    _only(subpath_stats.SUBPATH_COOLDOWN_REFUSED)


async def test_budget_refusal_is_its_own_key_not_a_failure():
    """防撞钟（`deadline_sec` 墙钟预算）用尽 ⇒ `budget_refused`，**不是** `miss`。

    两者的下一步动作完全相反：预算用尽说明"活太重 ⇒ 该挪去定时作业/放宽预算"，
    而 `miss` 说明"链上真拿不到 ⇒ 该去修源"。混在一起，两件事都不可判定。

    ⚠️ 预算 0.05s 相对"进入循环前那几微秒"有三个数量级余量，
    所以"先试了再被掐断"这个前提不会因调度抖动而翻面。
    """
    slow = _FakeConnector("慢源", delay=0.5)
    router = ConnectorRouter([(slow, lambda i: i == "CPI")])

    with pytest.raises(DataFetchError):
        await router.fetch("CPI", deadline_sec=0.05)

    assert slow.calls == 1, "预算用尽是「试过了、被掐断」，不是「没试」"
    _only(subpath_stats.SUBPATH_BUDGET_REFUSED)


async def test_unsupported_indicator_has_its_own_key_not_a_gap():
    """没有连接器认这个指标名 ⇒ `not_supported`（契约不满足，不是源故障）。

    混进 `miss` 的后果：一次拼错/缺后缀的指标名会被读成"数据源坏了"，
    然后有人去修源 —— 而真正要做的是改指标名或补登记。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "OTHER")])

    with pytest.raises(DataFetchError, match="无连接器支持指标"):
        await router.fetch("CPI")

    assert stub.calls == 0
    _only(subpath_stats.SUBPATH_NOT_SUPPORTED)


# ============================================================
# ⑤c 联网走过、最终由库给出（`db_snapshot_fallback`）
# ============================================================


async def test_db_fallback_after_network_failure_is_its_own_key():
    """网络全挂 → 回退库 ⇒ `db_snapshot_fallback`：既**不是** `db_snapshot`
    （那是"根本没联网"）、也**不是** `miss`（那会让缺口率虚高，而这次其实有数据）。

    ★ 这条同时钉死「后写覆盖先写」：链上先记的 `miss` 必须被覆盖掉，
    否则一次 fetch 会同时算进"缺口"与"由库服务"——两个数都错。
    """
    stub = _FakeConnector("替身源", fail=True)
    router = ConnectorRouter([(stub, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([_point("2026-09-16",
                                                   "stock_close:300308")]))

    out = await router.fetch("stock_close:300308", start_date="2025-05-01",
                             end_date="2026-09-17")

    assert [p.period_date for p in out] == ["2026-09-16"], "这条近路的前提是拿到了数据"
    assert stub.calls == 1, "它的前提还有一条：联网真的被走过"
    assert _counters()[subpath_stats.SUBPATH_MISS] == 0, (
        "有数据返回却被算成缺口 —— 缺口率从此不可信")
    _only(subpath_stats.SUBPATH_DB_SNAPSHOT_FALLBACK)


async def test_db_wins_over_older_network_keeps_one_record_only():
    """区间查询里网络回来的比库旧 ⇒ 用库，且**只记一条**（`connector_network` 被覆盖）。

    这是"互斥由结构保证"的第二个证据点：链上已经 `mark(connector_network)` 了，
    后写的 `db_snapshot_fallback` 必须**覆盖**而不是**累加**。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-31",
                                                   "stock_close:300308")])
    router = ConnectorRouter([(stub, lambda i: i.startswith("stock_close:"))],
                             repo=_FakeRepo([_point("2026-09-16",
                                                   "stock_close:300308")]))

    out = await router.fetch("stock_close:300308", start_date="2025-05-01",
                             end_date="2026-09-17")

    assert [p.period_date for p in out] == ["2026-09-16"]
    assert stub.calls == 1
    _only(subpath_stats.SUBPATH_DB_SNAPSHOT_FALLBACK)


# ============================================================
# ⑦ 一次 fetch = 一条子路径（结构性互斥）
# ============================================================


async def test_every_fetch_records_exactly_one_key():
    """三次 fetch（1 次联网 + 2 次 TTL）⇒ 分母 3、各键之和 3、缺陷计数 0。

    为什么单独一条：只要有人在多个分支各记一次，比例就会虚高 —
    而 `sum(counters) != total` 是唯一能自动发现的形状。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])

    for _ in range(3):
        await router.fetch("CPI")

    counts = _counters()
    snap = subpath_stats.snapshot()
    assert snap["total"] == 3, f"三次 fetch 的分母必须恰好 3：{snap['total']}"
    assert sum(counts.values()) == 3, f"有一次 fetch 记了不止一条：{counts}"
    assert counts[subpath_stats.SUBPATH_CONNECTOR_NETWORK] == 1
    assert counts[subpath_stats.SUBPATH_TTL_CACHE] == 2
    assert snap["unexpected"] == 0, "三次 fetch 里有出口没登记子路径"


# ============================================================
# ⑥ 空态：「还没量到」≠「量到 0」
# ============================================================


async def test_cold_state_is_unmeasured_not_zero():
    """冷启动：逐项 0 是**有意义的 0**，而 `latest_subpath` 必须是「未量到」。

    用 0 假装"最近一次走的是第 0 条近路"会让冷启动看起来像故障；
    反过来，读不到时给全 0 会被读成"一次都没联网"（假绿）。
    判据必须让两者分得开 —— 所以量到一次之后，它就不许再是「未量到」。
    """
    cold = subpath_stats.snapshot()
    assert cold["total"] == 0
    assert all(v == 0 for v in cold["counters"].values())
    assert cold["latest_subpath"] == subpath_stats.UNMEASURED_SUBPATH == "未量到"
    assert cold["unmeasured"] == nf.UNMEASURED, (
        "空态口径必须与 network_fallback.UNMEASURED 同一个常量（不是另写的字面量）")
    assert subpath_stats.UNMEASURED_SUBPATH == hop_stats.UNMEASURED

    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    await ConnectorRouter([(stub, lambda i: i == "CPI")]).fetch("CPI")

    warm = subpath_stats.snapshot()
    assert warm["latest_subpath"] == subpath_stats.SUBPATH_CONNECTOR_NETWORK, (
        f"量到之后必须如实上报，不许停在「未量到」：{warm['latest_subpath']}")
    assert warm["latest_subpath"] != cold["latest_subpath"], (
        "「还没量到」与「量到 0」必须分得开")


# ============================================================
# ⑧ ★ 自证：绕过真实入口 —— 计数纹丝不动
# ============================================================


async def test_bypassing_the_real_entry_does_not_move_the_counters():
    """★★ **自证**：计数挂在真实入口 `fetch()` 上，不是挂在"随便什么都被调用"的路上。

    ## 这条防的是什么（本项目实测过的形状）

    「判据接在没人走的路上 = 没接」：`describe()` 曾经零个生产调用方而全绿。
    如果把打点做成一个 `_record_subpath()` 之类只有测试调用的新函数，
    "命中时会 +1"这类判据**全都会过**，而真实链路一个数都量不到。

    ## 怎么证明（两段，缺一不可）

    1. **绕过**：直接调第三跳的**真实网络实现** `_fetch_uncached()`
       —— 它真的取到数据、替身连接器真的被调用（否则这条断言恒真、
       什么也证明不了），但它不是公共入口 ⇒ 计数必须**全 0**。
    2. **对照**：**同一个替身**经真实入口 `fetch()` ⇒ `connector_network` +1。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])

    # ①a 真·绕开路由：直接问连接器要数据
    direct = await stub.fetch("CPI")
    assert direct, "替身本身要能给出数据"
    assert _counters() == {k: 0 for k in subpath_stats.SUBPATH_KINDS}

    # ①b 走路由的真实网络实现，但不是公共入口
    fetched = await router._fetch_uncached("CPI", None, None)
    assert fetched and stub.calls == 2, (
        f"这条路本应取到数据（否则①的断言恒真、证明不了任何事）：{fetched!r}")
    assert _counters() == {k: 0 for k in subpath_stats.SUBPATH_KINDS}, (
        f"绕过真实入口的调用动了计数 ⇒ 计数没挂在真实路径上：{_counters()}")
    assert subpath_stats.snapshot()["total"] == 0

    # ② 对照：同一个替身经真实入口 ⇒ 只有 connector_network 被记
    out = await router.fetch("CPI")
    assert out
    assert _counters()[subpath_stats.SUBPATH_CONNECTOR_NETWORK] == 1, (
        "经真实入口走联网却没记上 ⇒ 打点不在决定返回的那一处")
    assert subpath_stats.snapshot()["total"] == 1


# ============================================================
# 键集合 / 绝不抛（单一事实源的形状契约）
# ============================================================


def test_key_names_are_the_single_source_of_truth():
    """计数的键只有这一份、取值可枚举、且「未量到」与 `network_fallback` 同源。

    为什么单独一条：键一旦有人另起一套（在自己的模块里再写一个 dict），
    两边就会漂移 —— 而漂移的表现是"两个界面各说各的命中率"，不报错。
    """
    snap = subpath_stats.snapshot()
    assert tuple(snap["counters"]) == subpath_stats.SUBPATH_KINDS, (
        "计数的键集合与 SUBPATH_KINDS 不一致 —— 有人在别处另写了一份")
    assert set(subpath_stats.SUBPATH_KINDS) == {
        "ttl_cache", "db_snapshot", "db_snapshot_fallback", "connector_network",
        "cooldown_refused", "budget_refused", "not_supported", "miss",
    }
    assert snap["kinds"] == list(subpath_stats.SUBPATH_KINDS), (
        "kinds 是渲染顺序的事实源，不许与计数器脱节")
    assert snap["unmeasured"] == nf.UNMEASURED
    #: 缺陷计数**不是**一条"第九条近路"（它不许混进可枚举的键集合里）
    assert subpath_stats.UNRECORDED not in subpath_stats.SUBPATH_KINDS
    #: 读出来的是**副本**（调用方改它不许污染真计数）
    snap["counters"][subpath_stats.SUBPATH_MISS] = 999
    assert _counters()[subpath_stats.SUBPATH_MISS] == 0


def test_unknown_value_never_raises_and_is_never_silent():
    """未知取值：**绝不抛**（热路径，且它写在 `fetch()` 的 finally 里 ——
    抛出去会顶掉真正的那个异常），但**也不静默**（记进 `unexpected`）。

    这一处刻意与 `hop_stats.bump()` 的「未知键 KeyError」不同：
    那边不在 finally 里，抛出去只会让人看见；这边抛出去会**掩盖现场**。
    """
    subpath_stats.record("ttl_cache_typo")           # 拼错的取值
    subpath_stats.record(subpath_stats.UNRECORDED)   # 漏埋点（一个都没登记）

    snap = subpath_stats.snapshot()
    assert snap["unexpected"] == 2, "拼错/漏埋点必须留下痕迹（不许静默）"
    assert snap["total"] == 0, "它们不许冒充任何一条真实子路径（分母不动）"
    assert snap["latest_subpath"] == "未量到", (
        "latest_subpath 只装可枚举的键（否则它自己就成了不可判的字段）")


# ============================================================
# ⑨ 接进**既有**观测面：/health 的 query_data_hops
# ============================================================


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """与既有 `/health` 契约测试同一套隔离方式（`MOSS_ENV=test` + 临时库）。"""
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "subpath.db"))
    monkeypatch.setenv("LLM_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("MOSS_ENV", "test")
    from fastapi.testclient import TestClient

    from src.api.main import app

    with TestClient(app) as c:
        yield c


def _hops_section(client) -> dict:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "query_data_hops" in body, (
        f"`/health` 里没有 `query_data_hops` 段。顶层现有键：{sorted(body)}")
    return body["query_data_hops"]


def _one_network_fetch() -> None:
    async def _run() -> None:
        stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
        await ConnectorRouter([(stub, lambda i: i == "CPI")]).fetch("CPI")

    asyncio.run(_run())


def test_health_query_data_hops_carries_the_third_hop_subpaths(client) -> None:
    """★ 第三跳的子路径明细必须出现在**既有**读取点上，且读数**会动**。

    为什么打**真实响应体**：只 import 一个常量证明不了"接线在"
    （本项目实测过「常量在、调用方零个」）。为什么接在这里而不是新开端点：
    `/health` 是前端 20 秒轮询的既有面，`_query_data_hops` 已经在读
    `hop_stats.snapshot()` —— 挂在那个快照里 ⇒ **零新增端点、零新增往返**。
    只断字段在、不断数值会漏掉"字段恒 0"这种假绿，所以下面跑一次真联网。
    """
    subpath_stats.reset_for_test()
    subs = _hops_section(client).get("connector_subpaths")

    assert isinstance(subs, dict), "第三跳的子路径明细没接到既有读取点上"
    assert subs.get("available") is True, subs
    assert set(subs["counters"]) == set(subpath_stats.SUBPATH_KINDS), (
        f"counters 的键与 SUBPATH_KINDS 不一致：{sorted(subs.get('counters') or [])}")
    assert subs["kinds"] == list(subpath_stats.SUBPATH_KINDS)
    assert subs["unmeasured"] == nf.UNMEASURED
    assert subs["latest_subpath"] == "未量到", (
        f"冷启动时必须如实说「未量到」：{subs['latest_subpath']!r}")
    assert subs["unexpected"] == 0

    _one_network_fetch()                    # 真的走一次联网（进程内替身，不联网）

    live = _hops_section(client)["connector_subpaths"]
    assert live["counters"][subpath_stats.SUBPATH_CONNECTOR_NETWORK] >= 1, (
        f"走过一次真联网，/health 却仍是 {live['counters']} —— 埋点没接到暴露口")
    assert live["total"] >= 1
    assert live["latest_subpath"] == subpath_stats.snapshot()["latest_subpath"], (
        "响应体与唯一事实源必须给出同一个「最近一次子路径」")


def test_health_subpath_section_degrades_alone(client, monkeypatch) -> None:
    """★ 子路径段坏了 ⇒ **只降级它自己**，四跳那份没坏的计数照常给，且绝不 500。

    为什么这条单独要：`/health` 是前端 20 秒轮询的面，任何一段抛异常都会让
    整张健康检查变红 —— 那会把"观测坏了"误报成"服务坏了"；而如果为了省事
    把两段写成一个 try，子路径坏了连四跳计数一起消失，运维看到的是"两个指标
    同时归零"，指向完全错误的结论。
    """
    def _boom() -> dict:
        raise RuntimeError("子路径计数炸了")

    monkeypatch.setattr(subpath_stats, "snapshot", _boom)

    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, "子路径计数坏了却把整张 /health 打挂"
    sec = resp.json()["query_data_hops"]
    subs = sec["connector_subpaths"]
    assert subs["available"] is False and "子路径计数炸了" in subs["error"]
    assert "counters" not in subs, (
        "读不到时不许给 counters —— 全 0 会被读成「一次都没联网」（假绿）")
    # ★ 两段各自降级：四跳那份**没问题**的计数必须照常给
    assert set(sec["counters"]) == set(hop_stats.HOP_KINDS), (
        "子路径坏了把四跳计数也带没了 —— 两段必须各自降级")
