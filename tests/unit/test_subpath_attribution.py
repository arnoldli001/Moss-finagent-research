"""「**这一次**采集走的是哪条近路」—— 逐次子路径归属的契约（本轮新增）。

## 为什么这个文件必须存在

`subpath_stats`（上一轮）给的是**聚合**分布：整个进程里 TTL 缓存 / 本地库 /
真联网 / 各类拒绝各走了多少次。它回答不了「**这一次**走的是哪条」—— 而采集侧
（A01）能记进审计与日志行的，只能是**这一次**的值。缺口原文（`CHG-0157` §33.5）：

    逐次子路径归属未接：`subpath_stats` 给的是**聚合**分布；"这一次采集走的是哪条
    近路"需要 `fetch()` 带回本次 outcome —— 并发下读"最近一次"会把别人的结果算给
    自己，属"看起来完全正常的错数"，所以采集侧继续**不写**"本地命中率"字段。

本轮把这一半接上：`fetch(..., outcome=...)` 把**本次**的子路径 mark 到**调用方
自己持有的那个对象**上；A01 把它记进 `path_stats.record(subpath=...)` 的报告，
于是产出 / 异常属性 / 台账 / 日志行都带着它，"本地命中率 / 联网触发率"第一次
在采集侧算得出来。

## 判据清单（每条都对应一种会静默变绿 / 变假的失效）

| 判据 | 防的是 |
|---|---|
| ① **交错 await** 下两次采集各报各的子路径 | 读"最近一次"冒充本次（**自证靶子**） |
| ② 路由层：两个持有者各拿各的，且聚合的"最近一次"是**别人**的 | 同上（机制层证据） |
| ③ 不传 outcome ⇒ 返回值与聚合计数**逐字一致** | 新出参偷偷改了老路径的行为 |
| ④ 采集侧记的是**本次**的值，不是聚合分布 | 记成"最近一次"或第二份计数 |
| ⑤ 空态：「还没量到」≠ 任何一条近路，且与 `network_fallback` 同源 | 拿不到归属时编一条近路 |
| ⑥ 失败（决定之前就抛）⇒ **未定**，不是 `miss` | "没定"污染缺口率 |
| ⑦ 取消（决定之前）⇒ 未定且聚合一次都不记；已定再取消 ⇒ 保留那个键 | 取消冒充近路 / 抹掉真实归属 |
| ⑧ 复用同一个对象 ⇒ 上一次的结论不许算给这一次 | 复用退化成"上一次" |
| ⑨ 该值进审计与日志行（产出 / 异常属性 / 台账 / `subpath=`） | 算了但没人读得到 |
| ⑩ 分布仍只有**一个**家（本轮不加第二段读取面） | 双份计数 ⇒ 比例算错 |

## 自证（真改源码跑红，见 `scripts/_prove_subpath_attribution.py`）

把「传 outcome」那条路改成「读全局最近一次」（router 把本次持有者挂在实例上、
采集侧取完数去读它）：**判据①必红**，而它在**顺序**调用下完全正常 ——
这正是这个缺陷的形状（本轮实测：其余判据全绿，只有①变红）。

## 为什么用 `Event` 停车而不是 `sleep`

交错必须**确定性**地构造：`sleep` 猜时序的判据会变成"偶尔红"，
而偶尔红的判据下场一定是被关掉。`Event` 让"谁停在哪儿"由测试自己决定。

## 绝不联网、绝不写盘

连接器 / 仓库全是**进程内替身**；台账与计数都是进程级状态 ⇒ 每条判据前后
autouse 清空（不清就会变成"看上一个用例跑了几次"）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time

import pytest

from src.core import hop_stats
from src.core.exceptions import AgentExecutionError, DataFetchError
from src.core.models import AgentInput
from src.core.schemas import DataPoint
from src.domain.agents.audit.verifier.agent import AuditAgent
from src.domain.agents.data.collector import gap_ledger, path_stats
from src.domain.agents.data.collector.agent import DataCollectorAgent
from src.infrastructure.catalog import network_fallback as nf
from src.infrastructure.connectors import subpath_stats
from src.infrastructure.connectors.router import ConnectorRouter

# ============================================================
# 测试替身（全部进程内；不打桩任何"计数"本身）
# ============================================================


class _FakeConnector:
    """替身连接器：`calls` 用来断言"这次有没有联网"。

    `park=True` 时停在 `entered` 事件上（**由测试决定什么时候放行 / 取消**）——
    取消语义与交错都必须确定性构造，不许靠 `sleep` 猜。
    """

    def __init__(self, name: str, *, points: list[DataPoint] | None = None,
                 fail: bool = False) -> None:
        self.source_name = name
        self.source_url = f"test://{name}"
        self._points = list(points or [])
        self._fail = fail
        self.calls = 0
        self.park = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def get_capabilities(self) -> dict:
        return {"name": self.source_name, "simulated": False,
                "indicators": ["CPI"]}

    async def fetch(self, indicator, start_date=None, end_date=None):
        self.calls += 1
        if self.park:
            self.entered.set()
            await self.release.wait()          # 只有被取消 / 被放行才继续
        if self._fail:
            raise DataFetchError(f"{self.source_name}不可用")
        return list(self._points)


class _ParkingRepo:
    """最小 `DataPointRepository` 替身；`save_points` 可停在事件上。

    「联网已成功（子路径已定）但整个 fetch 还没结束」这个窗口只存在于
    `_cached_fetch` 回填库的那一次 await 上 —— 判据②⑦要的正是它。
    """

    def __init__(self, *, park_save: bool = False) -> None:
        self.park_save = park_save
        self.saved: list[DataPoint] = []
        self.save_entered = asyncio.Event()
        self.release_save = asyncio.Event()

    async def query_points(self, indicator, start_date=None, end_date=None):
        return []                              # 库里没有 ⇒ 走网络链

    async def save_points(self, points, task_id="") -> dict:
        if self.park_save:
            self.save_entered.set()
            await self.release_save.wait()
        self.saved.extend(points)
        return {"inserted": len(points), "skipped": 0, "total": len(points)}


class _LegacyBackend:
    """**不认** `outcome` 的后端（老实现 / 测试替身）—— 签名里没有它。"""

    def __init__(self, *, points=None, exc=None) -> None:
        self._points = list(points or [])
        self._exc = exc

    async def fetch(self, indicator, start_date=None, end_date=None):
        if self._exc is not None:
            raise self._exc
        return list(self._points)

    def get_capabilities(self) -> dict:
        return {"name": "legacy", "indicators": []}


class _SwallowingBackend(_LegacyBackend):
    """签名认 `outcome`（`**kwargs`）但**不往里写**的后端。

    现实里这就是"接了但没接上"的形状：A01 必须如实记「未量到」，
    而不许编一条近路出来。
    """

    async def fetch(self, indicator, start_date=None, end_date=None, **kwargs):
        return list(self._points)


def _point(period: str, indicator: str = "CPI") -> DataPoint:
    return DataPoint(indicator=indicator, value=1.0, period_date=period,
                     source_name="替身源")


def _ainput(indicator: str, task_id: str = "t_subpath") -> AgentInput:
    return AgentInput(task_id=task_id, tenant_id="tenant_001",
                      payload={"indicator": indicator})


async def _collect(agent: DataCollectorAgent, indicator: str,
                   task_id: str = "t_subpath"):
    """跑**生产实现** A01（异常按原样抛出，判据自己决定要不要接）。"""
    return await agent.execute(_ainput(indicator, task_id=task_id))


def _counters() -> dict:
    return dict(subpath_stats.snapshot()["counters"])


@pytest.fixture(autouse=True)
def _clean_stats():
    """计数/台账都是**进程级**的：每条判据都从冷启动态开始。"""
    subpath_stats.reset_for_test()
    path_stats.reset_for_test()
    gap_ledger.reset_for_test()
    assert subpath_stats.snapshot()["latest_subpath"] == subpath_stats.UNMEASURED_SUBPATH
    yield
    subpath_stats.reset_for_test()
    path_stats.reset_for_test()
    gap_ledger.reset_for_test()


# ============================================================
# ① ★ 交错 await：两次采集各报各的子路径（**自证靶子**）
# ============================================================


async def test_interleaved_collections_each_report_their_own_subpath():
    """★★ 两次**交错**的采集必须各报各的子路径 —— 不许靠"最近一次"。

    ## 交错的形状（确定性构造，不猜时序）

    * 采集 Y（`CPI`）：联网成功 ⇒ 子路径已经定下 `connector_network`，
      但它还停在"回填本地库"那一次 await 上（窗口就这一个，见 `_ParkingRepo`）；
    * 采集 X（`PPI`）：在 Y 停着的时候**跑完**，由 `cooldown_refused`
      （全源在失败冷却中、一次网都没发）服务。

    于是"最近一次"在 Y 回来之前已经变成 X 的了 —— 任何"取完数去读最近一次"的
    实现都会把 **X 的结果算给 Y**（或反过来），而它**读起来完全正常**：
    两个字段都是合法的子路径键，比例也只是微微错一点。

    ## 自证让这条变红的做法

    把"传 outcome"那条路改成"读全局最近一次"：router 每次 fetch 把本次持有者
    挂在实例上、采集侧取完数去读它（`scripts/_prove_subpath_attribution.py`
    的变异 M1）⇒ 下面 Y 那一条会拿到 `cooldown_refused`。实测输出见交付说明。
    """
    ok = _FakeConnector("好源", points=[_point("2026-08-01")])
    bad = _FakeConnector("坏源", points=[_point("2026-08-01")])
    repo = _ParkingRepo(park_save=True)
    router = ConnectorRouter(
        [(ok, lambda i: i == "CPI"), (bad, lambda i: i == "PPI")], repo=repo)
    #: X 这一次由**冷却拒绝**服务（`[]`，不抛异常），与 Y 的 `connector_network`
    #: 是两条不同的近路 —— 这样"各拿各的"才有区分度。
    router._failure_cache["坏源:PPI"] = (time.monotonic() + 300, "上游 503")
    agent = DataCollectorAgent(router)

    # Y：联网成功，停在回填库那一步（此时它的子路径已经定下）
    task_y = asyncio.create_task(_collect(agent, "CPI", task_id="t_y"))
    await asyncio.wait_for(repo.save_entered.wait(), timeout=5)
    assert ok.calls == 1, "前提：Y 真的联网了（否则这条判据什么也证明不了）"

    # X：在 Y 停着的时候整条跑完 —— 它记下的 `cooldown_refused` 成为"最近一次"
    out_x = await _collect(agent, "PPI", task_id="t_x")
    assert bad.calls == 0, "冷却拒绝必须一次网络请求都不发"
    assert out_x.result["path_stats"]["hit_path"] == path_stats.PATH_EMPTY

    repo.release_save.set()
    out_y = await asyncio.wait_for(task_y, timeout=5)

    # ★ 各报各的：两条都是"本次"，谁也不许拿对方的值
    assert out_y.result["path_stats"]["subpath"] == subpath_stats.SUBPATH_CONNECTOR_NETWORK
    assert out_x.result["path_stats"]["subpath"] == subpath_stats.SUBPATH_COOLDOWN_REFUSED
    assert (out_y.result["path_stats"]["subpath"]
            != out_x.result["path_stats"]["subpath"]), "两次走了不同的近路，取值必须不同"


# ============================================================
# ② 路由层：持有者各拿各的，而聚合的"最近一次"是别人的
# ============================================================


async def test_two_outcomes_never_share_one_latest():
    """★ 同一交错在**机制层**再证一次：每个持有者拿到的都是**本次**的值，
    而聚合里的 `latest_subpath` 在交错结束后是**另一个调用**的 ——
    这就是"读最近一次"为什么答不了这个问题（它答的是"谁最后记的"）。

    与判据①的分工：①证"采集侧读到的是本次"；②证"这份归属确实来自调用方
    自己持有的对象，而不是某个全局槽"。
    """
    ok = _FakeConnector("好源", points=[_point("2026-08-01")])
    bad = _FakeConnector("坏源", points=[_point("2026-08-01")])
    repo = _ParkingRepo(park_save=True)
    router = ConnectorRouter(
        [(ok, lambda i: i == "CPI"), (bad, lambda i: i == "PPI")], repo=repo)
    router._failure_cache["坏源:PPI"] = (time.monotonic() + 300, "上游 503")

    out_y, out_x = subpath_stats.Outcome(), subpath_stats.Outcome()
    task_y = asyncio.create_task(router.fetch("CPI", outcome=out_y))
    await asyncio.wait_for(repo.save_entered.wait(), timeout=5)

    await router.fetch("PPI", outcome=out_x)          # 在 Y 的 await 窗口里跑完
    repo.release_save.set()
    await asyncio.wait_for(task_y, timeout=5)

    assert out_y.subpath == subpath_stats.SUBPATH_CONNECTOR_NETWORK
    assert out_x.subpath == subpath_stats.SUBPATH_COOLDOWN_REFUSED
    assert out_y.resolved is True and out_x.resolved is True

    snap = subpath_stats.snapshot()
    assert snap["latest_subpath"] == subpath_stats.SUBPATH_CONNECTOR_NETWORK
    assert out_x.subpath != snap["latest_subpath"], (
        "聚合的「最近一次」是**别人**的值 —— 拿它冒充本次就是那个错数")
    assert snap["total"] == 2 and snap["unexpected"] == 0


# ============================================================
# ③ 不传 outcome ⇒ 行为与改动前**逐字一致**
# ============================================================


async def _three_fetches(pass_outcome: bool) -> tuple[list, dict]:
    """同一个场景跑三跳（1 次联网 + 2 次 TTL），返回（返回值, 聚合快照）。"""
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])
    returns: list = []
    for _ in range(3):
        if pass_outcome:
            returns.append(await router.fetch("CPI", outcome=subpath_stats.Outcome()))
        else:
            returns.append(await router.fetch("CPI"))       # 不传（老调用方）
    return returns, subpath_stats.snapshot()


async def test_without_outcome_nothing_changes():
    """★ 不传 `outcome` ⇒ 返回值与**聚合计数逐字一致**（含分母与键集合）。

    防的是"加了个可选参数，顺手把老路径也改了"：这种改动**不报错**，
    只让老调用方拿到的数悄悄变了。

    同时钉住两个形状契约：`outcome` 是**关键字可选**参数（默认 `None`）——
    老调用方一句都不用改；传了它也只是**多带回一个值**，聚合仍是一次一条。
    """
    sig = inspect.signature(ConnectorRouter.fetch)
    param = sig.parameters["outcome"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None, "可选出参的默认值必须是 None（不传 = 老行为）"

    subpath_stats.reset_for_test()
    ret_old, snap_old = await _three_fetches(pass_outcome=False)
    subpath_stats.reset_for_test()
    ret_new, snap_new = await _three_fetches(pass_outcome=True)

    assert [p.period_date for pts in ret_old for p in pts] == \
        [p.period_date for pts in ret_new for p in pts], "返回值必须逐字一致"
    assert snap_old == snap_new, (
        f"传 outcome 改变了聚合计数：{snap_old} != {snap_new}")
    counts = snap_old["counters"]
    assert counts[subpath_stats.SUBPATH_CONNECTOR_NETWORK] == 1
    assert counts[subpath_stats.SUBPATH_TTL_CACHE] == 2
    assert snap_old["total"] == 3 and snap_old["unexpected"] == 0
    assert set(counts) == set(subpath_stats.SUBPATH_KINDS)


async def test_one_fetch_still_records_exactly_one_key_with_outcome():
    """传 outcome **不是**第二个计数点：一次 fetch 仍然只记一条、分母只 +1。

    防的是"出参顺手又记了一次"：那样 `total` 会翻倍，而所有比率会一起变小
    —— 却没有任何东西会报错。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])

    await router.fetch("CPI", outcome=subpath_stats.Outcome())

    counts = _counters()
    snap = subpath_stats.snapshot()
    assert counts[subpath_stats.SUBPATH_CONNECTOR_NETWORK] == 1
    assert [k for k, v in counts.items() if v] == [
        subpath_stats.SUBPATH_CONNECTOR_NETWORK], f"记了不止一条：{counts}"
    assert snap["total"] == 1, f"分母必须恰好 +1，实际 {snap['total']}"
    assert snap["unexpected"] == 0


# ============================================================
# ④ 采集侧记的是**本次**的值（不是聚合分布）
# ============================================================


async def test_collector_records_this_calls_subpath_in_path_stats(caplog):
    """★ A01 的 `path_stats` 报告里带**这一次**的子路径，两次不同的近路各记各的。

    防的是"记成最近一次"或"记成分布"：报告是**按次**的，它只能装一个值；
    把分布塞进来会让下游（审计/运维）以为那是本次的归属。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "CPI")])
    agent = DataCollectorAgent(router)

    with caplog.at_level(logging.INFO,
                         logger="src.domain.agents.data.collector.agent"):
        first = await _collect(agent, "CPI", task_id="t_first")    # 联网
        second = await _collect(agent, "CPI", task_id="t_second")  # TTL 命中
        ok_lines = [r.message for r in caplog.records if "采集成功" in r.message]

    first_stats = first.result["path_stats"]
    second_stats = second.result["path_stats"]

    assert first_stats["subpath"] == subpath_stats.SUBPATH_CONNECTOR_NETWORK, first_stats
    assert second_stats["subpath"] == subpath_stats.SUBPATH_TTL_CACHE, second_stats
    #: 值本身**不是**分布（分布是映射，本次归属是一个键名）
    assert isinstance(second_stats["subpath"], str)
    assert second_stats["subpath"] in subpath_stats.SUBPATH_KINDS
    #: 两次的**结论**都是命中（`path1_backend`），只有子路径不同 ——
    #: 这正是"两维正交"的直接证据（同一维上的值不许被子路径替换掉）
    assert (first_stats["hit_path"] == second_stats["hit_path"]
            == path_stats.PATH_BACKEND)
    #: 成功行也要带（TTL 命中这类正常返回占比最大，不给它带就永远拼不齐比率）
    assert ok_lines, "成功行必须真的打出来（否则 caplog 断言的靶子不存在）"
    assert any(f"subpath={subpath_stats.SUBPATH_TTL_CACHE}" in ln for ln in ok_lines)
    #: 聚合**不受**子路径影响：两个结论键各 1，子路径不是计数器
    counts = path_stats.snapshot()["counters"]
    assert counts[path_stats.PATH_BACKEND] == 2
    assert set(counts) == set(path_stats.PATH_KINDS)
    assert path_stats.snapshot()["total"] == 2


# ============================================================
# ⑤ 空态：「还没量到」≠ 任何一条近路
# ============================================================


async def test_unmeasured_is_not_any_subpath_and_shares_the_one_constant():
    """★ 后端给不出归属 ⇒ `UNMEASURED`（**同一个常量**），不是 0、也不是某条近路。

    两种"给不出"都要如实降级、**都不许影响取数**：

    * 后端根本**不认** `outcome`（老实现/替身）⇒ A01 一个参数都不多传；
    * 后端认（`**kwargs`）但**不往里写** ⇒ 持有者停在「还没定」。

    两者的下一步动作是同一件事（这一维没有依据、别拿它算比率），所以合成
    「未量到」；但它们与"量到了一条近路"必须**分得开** —— 后者才让比率有意义。
    """
    legacy = DataCollectorAgent(_LegacyBackend(points=[_point("2026-08-01")]))
    out = await _collect(legacy, "CPI", task_id="t_legacy")
    assert out.result["path_stats"]["subpath"] == path_stats.UNMEASURED, (
        "后端不认这个出参时必须是「未量到」——写任何一条近路都是编")
    assert out.result["data_points"], "降级不许把取数带下去"

    swallowing = DataCollectorAgent(
        _SwallowingBackend(points=[_point("2026-08-01")]))
    out2 = await _collect(swallowing, "CPI", task_id="t_swallow")
    assert out2.result["path_stats"]["subpath"] == path_stats.UNMEASURED

    #: 常量同源（与 `network_fallback` / `hop_stats` / `subpath_stats` **同一个**）
    assert path_stats.UNMEASURED is nf.UNMEASURED
    assert path_stats.UNMEASURED == subpath_stats.UNMEASURED_SUBPATH == hop_stats.UNMEASURED

    #: 对照：量到一条近路时，字段**不再**是「未量到」（两者分得开）
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    measured = await _collect(DataCollectorAgent(
        ConnectorRouter([(stub, lambda i: i == "CPI")])), "CPI", task_id="t_measured")
    report = measured.result["path_stats"]
    assert report["subpath"] == subpath_stats.SUBPATH_CONNECTOR_NETWORK
    assert report["subpath"] != report["unmeasured"], "「量到」与「未量到」必须分得开"


def test_cold_start_subpath_is_unmeasured_while_counters_are_meaningful_zero():
    """冷启动：计数器里的 0 是**有意义的 0**（比率的分母要它），
    而"最近一次"与报告里的 `subpath` 都是「未量到」—— 两者不许混。"""
    snap = subpath_stats.snapshot()
    assert snap["total"] == 0 and all(v == 0 for v in snap["counters"].values())
    assert snap["latest_subpath"] == subpath_stats.UNMEASURED_SUBPATH == "未量到"
    assert snap["unmeasured"] == nf.UNMEASURED
    #: `record()` 不传 subpath ⇒ 报告里是「未量到」（不是 0、不是任何键）
    report = path_stats.record(path_stats.PATH_EMPTY)
    assert report["subpath"] == nf.UNMEASURED
    assert report["subpath"] not in subpath_stats.SUBPATH_KINDS


# ============================================================
# ⑥ 失败（决定之前就抛）⇒ 未定，不是 `miss`
# ============================================================


async def test_failure_before_any_decision_is_unresolved_not_miss():
    """★ 在"哪条近路"被定下**之前**就抛了 ⇒ 调用方拿到的是**未定**（`UNRECORDED`），
    绝不是 `miss`（后者是"链上真的全失败"这个**结论**）。

    用"路由自己的匹配谓词抛异常"构造这条出口（连接器声明的能力读不出来，
    是程序缺陷而非源故障）：链上一条近路都没走完 ⇒ 既不该记成缺口，
    也不该静默 —— 聚合侧把它记进 `unexpected`（漏埋点自鸣），`total` 不动。
    """
    def _boom(indicator: str) -> bool:
        raise RuntimeError("路由谓词炸了（程序缺陷，不是源故障）")

    router = ConnectorRouter([(_FakeConnector("替身源"), _boom)])
    outcome = subpath_stats.Outcome()

    with pytest.raises(RuntimeError):
        await router.fetch("CPI", outcome=outcome)

    assert outcome.subpath == subpath_stats.UNRECORDED
    assert outcome.resolved is False, "没定下子路径时不许说已定"
    assert outcome.subpath != subpath_stats.SUBPATH_MISS, "未定不许冒充 `miss`"
    snap = subpath_stats.snapshot()
    assert snap["counters"][subpath_stats.SUBPATH_MISS] == 0, "缺口率不许被'没定'污染"
    assert snap["total"] == 0, "没有一条近路服务过这次 fetch ⇒ 分母不动"
    assert snap["unexpected"] == 1, "走完却一处都没登记 ⇒ 必须留下痕迹（不静默）"

    #: 采集侧同一条出口：报告里是「未量到」，而结论仍是 `path1_error`（真故障）
    agent = DataCollectorAgent(router)
    with pytest.raises(AgentExecutionError) as ei:
        await _collect(agent, "CPI", task_id="t_boom")
    assert ei.value.path_stats["hit_path"] == path_stats.PATH_ERROR
    assert ei.value.path_stats["subpath"] == path_stats.UNMEASURED, (
        "没定下近路 ⇒ 未量到；用 `miss` 冒充会同时谎报缺口与归属")


# ============================================================
# ⑦ 取消：未定 ⇒ 一次都不记；已定 ⇒ 保留那个事实
# ============================================================


async def test_cancel_before_any_decision_is_unresolved_and_uncounted():
    """★ 取消发生在**决定之前** ⇒ 未定，且聚合**一次都不记**。

    取消是**上层**放弃等待，不是第三跳内部的任何一条近路 —— 硬记一条就是编。
    这一条同时守住"取消 ≠ 漏埋点"：`unexpected` 也不许涨（它不是代码缺陷）。
    """
    stub = _FakeConnector("慢源", points=[_point("2026-08-01")])
    stub.park = True
    router = ConnectorRouter([(stub, lambda i: i == "X:1")])
    outcome = subpath_stats.Outcome()

    task = asyncio.create_task(router.fetch("X:1", outcome=outcome))
    await asyncio.wait_for(stub.entered.wait(), timeout=5)
    assert stub.calls == 1, "前提：请求真的发出去了（否则这条判据什么也证明不了）"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert outcome.resolved is False
    assert outcome.subpath == subpath_stats.UNRECORDED
    assert outcome.subpath != subpath_stats.SUBPATH_MISS
    snap = subpath_stats.snapshot()
    assert snap["total"] == 0 and snap["unexpected"] == 0, (
        f"取消既不是一条近路、也不是漏埋点：{snap}")
    assert all(v == 0 for v in snap["counters"].values())


async def test_cancel_after_the_decision_keeps_that_decision():
    """★ 子路径**已经定下**之后才被取消 ⇒ 那个键留在调用方的对象上，聚合照记。

    理由：取消不改变"数据确实由这条近路给出"这个**事实**（联网已经打过、
    数据已经拿到），抹掉它会让联网触发率少算一次真实成本。
    """
    stub = _FakeConnector("好源", points=[_point("2026-08-01")])
    repo = _ParkingRepo(park_save=True)
    router = ConnectorRouter([(stub, lambda i: i == "CPI")], repo=repo)
    outcome = subpath_stats.Outcome()

    task = asyncio.create_task(router.fetch("CPI", outcome=outcome))
    await asyncio.wait_for(repo.save_entered.wait(), timeout=5)
    assert stub.calls == 1, "前提：这次 fetch 已经真的联网并拿到数据"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert outcome.subpath == subpath_stats.SUBPATH_CONNECTOR_NETWORK
    assert outcome.resolved is True
    snap = subpath_stats.snapshot()
    assert snap["counters"][subpath_stats.SUBPATH_CONNECTOR_NETWORK] == 1
    assert snap["total"] == 1 and snap["unexpected"] == 0


# ============================================================
# ⑧ 复用同一个对象 ⇒ 上一次的结论不许算给这一次
# ============================================================


async def test_reusing_one_outcome_never_keeps_the_previous_verdict():
    """★ 调用方复用同一个持有者时，`fetch()` 入口会先把它清回「还没定」。

    没有这一步，第二次（在决定之前被取消）会**留着上一次的键** ——
    调用方读到的就是"上一次"冒充"这一次"，正是本模块要消灭的那类错数。
    """
    stub = _FakeConnector("替身源", points=[_point("2026-08-01")])
    router = ConnectorRouter([(stub, lambda i: i == "X:1")])
    outcome = subpath_stats.Outcome()

    await router.fetch("X:1", outcome=outcome)          # 第一次：联网
    assert outcome.subpath == subpath_stats.SUBPATH_CONNECTOR_NETWORK

    stub.park = True                                    # 第二次：停在决定之前被取消
    task = asyncio.create_task(router.fetch("X:1", outcome=outcome))
    await asyncio.wait_for(stub.entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert outcome.resolved is False, (
        "复用同一个对象时，上一次的结论被算给了这一次")
    assert outcome.subpath == subpath_stats.UNRECORDED


# ============================================================
# ⑨ 该值进审计与日志行（产出 / 异常属性 / 台账 / `subpath=`）
# ============================================================


def test_subpath_reaches_audit_and_log_lines(caplog, tmp_dir):
    """★ 采集侧算出来的子路径必须**出现在审计与日志里**，不是只在内存里。

    "算了但没人读得到"是本项目登记过的形状（判据接在没人走的路上）。
    四条载本各查一次：产出（成功）、异常属性（失败）、日志行、A18 审计。
    """
    async def _run():
        ok = _FakeConnector("好源", points=[_point("2026-08-01")])
        bad = _FakeConnector("坏源", fail=True)
        router = ConnectorRouter([
            (ok, lambda i: i == "CPI"),
            (bad, lambda i: i.startswith("主线告警")),
        ])
        agent = DataCollectorAgent(router)
        with caplog.at_level(logging.INFO,
                             logger="src.domain.agents.data.collector.agent"):
            good = await _collect(agent, "CPI", task_id="t_carry_sub")
            try:
                await _collect(agent, "主线告警:600036", task_id="t_carry_sub")
            except AgentExecutionError as exc:
                failure = exc
            else:                                        # pragma: no cover
                raise AssertionError("这条出口本应抛 AgentExecutionError")
        return good, failure

    good, failure = asyncio.run(_run())
    lines = [r.message for r in caplog.records]

    #: ① 产出（成功）：本次归属随 `path_stats` 一起交出去
    assert good.result["path_stats"]["subpath"] == subpath_stats.SUBPATH_CONNECTOR_NETWORK
    #: ② 异常属性（失败）：链上全失败 ⇒ `miss`（真缺口），且带在同一个报告里
    assert failure.path_stats["subpath"] == subpath_stats.SUBPATH_MISS
    assert failure.path_stats["hit_path"] == path_stats.PATH_ERROR
    #: ③ 日志行：成功行与缺口行都带 `subpath=`（可 grep）
    assert any(f"subpath={subpath_stats.SUBPATH_CONNECTOR_NETWORK}" in ln
               for ln in lines if "采集成功" in ln)
    assert any(f"subpath={subpath_stats.SUBPATH_MISS}" in ln
               for ln in lines if "采集缺口" in ln)

    #: ④ A18 审计：缺口台账里的报告带着它（审计读的就是这份台账）
    audit = asyncio.run(AuditAgent().execute(_ainput({
        "trace_id": "t_carry_sub", "agent_outputs": [],
        "chain_path": f"{tmp_dir}/chain.jsonl",
        "llm_audit_path": f"{tmp_dir}/none.jsonl",
        "seal_report": False,
    }, task_id="t_carry_sub")))
    gaps = audit.result["collection_gaps"]
    assert gaps and gaps[0]["indicator"] == "主线告警:600036", gaps
    assert gaps[0]["path_stats"]["subpath"] == subpath_stats.SUBPATH_MISS
    #: 分布仍然是聚合的（审计读到的这一条是**逐次**值，两者不许混）
    assert path_stats.snapshot()["total"] == 2


# ============================================================
# ⑩ 分布仍只有**一个**家（本轮不加第二段读取面）
# ============================================================


def test_the_subpath_distribution_still_has_exactly_one_home():
    """★ 「分布」的唯一事实源仍是 `subpath_stats`，读出口仍是 `/health` 既有段。

    本轮**没有**再给采集侧加一份子路径分布（见 `path_stats` 模块头的理由）：
    两份计数会让同一个问题有两个答案，而两个答案都"看起来正常"。
    另外两维必须**正交**：`PATH_KINDS`（结论）与 `SUBPATH_KINDS`（哪条近路）
    不许有交集 —— 一旦有，`counts[kind] / total` 就会把同一次取数算两次。
    """
    assert not (set(path_stats.PATH_KINDS) & set(subpath_stats.SUBPATH_KINDS))
    assert set(path_stats.snapshot()["counters"]) == set(path_stats.PATH_KINDS)
    assert path_stats.snapshot()["kinds"] == list(path_stats.PATH_KINDS)

    subs = hop_stats.snapshot()["connector_subpaths"]
    assert subs["available"] is True, subs
    assert subs["kinds"] == list(subpath_stats.SUBPATH_KINDS)
    assert set(subs["counters"]) == set(subpath_stats.SUBPATH_KINDS)
    assert subs["unmeasured"] == nf.UNMEASURED
    #: 「未定」不是第九条近路（它是一条自鸣报警，不许混进可枚举的键集合）
    assert subpath_stats.UNRECORDED not in subpath_stats.SUBPATH_KINDS
