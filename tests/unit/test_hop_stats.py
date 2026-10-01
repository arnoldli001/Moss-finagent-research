"""四跳取数的**跳级命中计数**契约 —— 「这次是第几跳答出来的」必须量得出来。

## 为什么这个文件必须存在

`query_data_for_agent`（`src/orchestration/supervisor.py`）是一条**四跳优先级链**：

    ① 本次已采集的 `validated_points`（内存）→ ② 本地库 → ③ 连接器 → ④ 联网兜底

设计原则是「每一跳都只在**上一跳真的没给出数据**时才发生；顺序即策略」。
而在此之前**没有任何埋点记录"这次是第几跳答出来的"** ⇒ 「本地命中率」
「联网兜底触发率」两个指标**给不出来**。一个只讲"我们有四跳"却答不出
"每跳命中多少"的系统，等于没有量化依据：**顺序改对了还是改坏了，没有数据能证明**。

## 判据清单（每条都对应一种会静默变绿/变假的失效）

| 判据 | 防的是 |
|---|---|
| ① 第 1 跳命中 ⇒ 只 `hop1_validated` +1 | 打点多处 ⇒ 一次取数记成两次命中（命中率虚高） |
| ② 第 1 跳空、第 2 跳有 ⇒ 只有 `hop2_local` +1 | 「上一跳没给数据才往下走」这条纪律只剩注释 |
| ③ 四跳全空 ⇒ `misses` +1，且不算任何一跳命中 | 缺口混进某一跳 ⇒ 兜底触发率与成功率分不开 |
| ④ ★ 自证：绕过打点的路径调用后计数不变 | 计数挂在随便什么辅助函数上 ⇒ 判据恒绿而链路没量到 |
| ⑤ `/health` 响应体里字段真的出现 | 只 import 常量 ⇒ 常量在、接线不在（实测过） |

★ 判据 ④ 是本文件的**自证**：`_query_data_via_connectors()` 是**真实**的第三跳
取数实现（拿到数据、返回同样的文本），但它**不是**四跳链决定返回的那一处 ——
所以直接调它拿数据时计数必须纹丝不动；随后**同一个替身**经完整链路时
`hop3_connector` 必须 +1。两条断言合起来才证明"计数挂在真实路径上"。

## 绝不发网络请求、绝不写共享文件

所有取数实现都是注入的替身；第四跳用**默认白名单为空**的实例（fail-closed，
一次都不会真联网）；`MOSS_NETWORK_FALLBACK_PATH` 指向临时目录
（`data/run/` 是 dev/pilot/生产**共用**的账本，测试里"花掉"的额度会真的吃掉
生产日预算，而且不报错）。

跑法：
    uv run python -m pytest tests/unit/test_hop_stats.py -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import hop_stats  # noqa: E402
from src.infrastructure.catalog import local_data as ld  # noqa: E402
from src.infrastructure.catalog import network_fallback as nf  # noqa: E402
from src.infrastructure.catalog.local_data import (  # noqa: E402
    Diag,
    DiagCode,
    MetricSeries,
)
from src.orchestration import supervisor  # noqa: E402

# ============================================================
# 测试替身（全部进程内；不打桩任何"计数"本身）
# ============================================================


class _StubLocalExecutor:
    """第二跳的替身：有 `rows` 就命中本地库，否则返回带诊断码的空结果。"""

    def __init__(self, *, rows=None, code: str = DiagCode.NO_DATA) -> None:
        self.rows = list(rows or [])
        self.code = code

    def metric_series(self, metric: str, *, entity: str = "", limit: int = 10):
        if self.rows:
            return MetricSeries(metric=metric, entity=entity, points=self.rows,
                                source="local", dataset_id="ds", diag=None)
        return MetricSeries(metric=metric, entity=entity, points=[],
                            diag=Diag(self.code, "测试替身：本地空结果"))


class _Point:
    """假数据点（鸭子类型）：`_query_data_via_connectors` 只按属性读。"""

    def __init__(self, value, period_date="2026-09-01", source_name="AkShare"):
        self.value = value
        self.period_date = period_date
        self.source_name = source_name


class _StubBackend:
    """A01 `_backend` 替身：`fetch` 返回脚本给定的点（第三跳的取数实现）。"""

    def __init__(self, *, points=None) -> None:
        self._points = list(points or [])
        self.calls: list[str] = []

    async def fetch(self, indicator: str, *args, **kwargs) -> list:
        self.calls.append(indicator)
        return list(self._points)


class _StubCollector:
    def __init__(self, backend) -> None:
        self._backend = backend


def _state(**kw) -> dict:
    base = {"validated_points": [], "focus_stock_code": ""}
    base.update(kw)
    return base


def _agents(backend=None) -> dict:
    return {"A01_data_collector": _StubCollector(backend or _StubBackend())}


async def _run(indicator: str, *, state=None, rows=None, points=None,
               limit: int = 10):
    """跑**生产实现** `query_data_for_agent`（第二跳替身用完即还原）。"""
    executor = _StubLocalExecutor(rows=rows)
    backend = _StubBackend(points=points)
    original = ld.LocalDataExecutor
    ld.LocalDataExecutor = lambda *a, **k: executor
    try:
        return await supervisor.query_data_for_agent(
            indicator, limit, state=state or _state(), agents=_agents(backend))
    finally:
        ld.LocalDataExecutor = original


def _counters() -> dict:
    return dict(hop_stats.snapshot()["counters"])


# ============================================================
# 隔离（autouse）：计数归零 + 第四跳账本指到临时目录 + 白名单清空
# ============================================================


@pytest.fixture(autouse=True)
def _clean_hop_stats():
    """每条判据都从冷启动态开始（计数是**进程级**的，不清就会串用例）。

    顺带钉住 `reset_for_test()`：清完必须**恰好**是冷启动态
    （全 0 + `latest_hop` 回到「未量到」）—— 清不干净时，
    后面每条"只 +1"的断言都会变成"看上一个用例跑了几次"。
    """
    hop_stats.reset_for_test()
    snap = hop_stats.snapshot()
    assert all(v == 0 for v in snap["counters"].values()), (
        f"reset_for_test() 没清干净：{snap['counters']}")
    assert snap["latest_hop"] == hop_stats.UNMEASURED_HOPS, (
        "冷启动态必须记「未量到」，不能留上一轮的那一跳")
    assert snap["total"] == 0
    yield
    hop_stats.reset_for_test()


@pytest.fixture(autouse=True)
def _no_network_writes(tmp_dir, monkeypatch):
    """第四跳的账本/白名单/熔断注册表全部隔离（照抄 `test_fallback_wiring.py`）。

    ★ 账本路径必须指到临时目录：`data/run/` 是 dev/pilot/生产**共用**的。
    ★ 白名单必须为空：否则开发机 `.env` 开了兜底时，"默认 fail-closed
      且一次都不联网"这条前提就变成"看谁的环境"。
    """
    monkeypatch.setenv("MOSS_NETWORK_FALLBACK_PATH",
                       str(Path(tmp_dir) / "hop_stats_nf.json"))
    monkeypatch.delenv(nf.ALLOWLIST_ENV, raising=False)
    nf.reset_network_fallback()
    yield
    nf.reset_network_fallback()


def test_hop_stats_is_the_only_counter_and_owns_the_stable_names():
    """单一事实源：计数键只有这一份，且**不重名成第五跳**。

    为什么单独一条：计数键一旦有人另起一套（在自己的模块里再写一个 dict），
    两边就会漂移，而漂移的表现是"两个界面各说各的命中率" —— 不报错。
    """
    snap = hop_stats.snapshot()
    assert tuple(snap["counters"]) == hop_stats.HOP_KINDS, (
        "计数的键集合与 HOP_KINDS 不一致 —— 有人在别处另写了一份")
    assert set(hop_stats.HOP_KINDS) == {
        "hop1_validated", "hop2_local", "hop3_connector", "hop4_network",
        "misses",
    }
    assert snap["unmeasured"] == hop_stats.UNMEASURED, (
        "空态口径必须与 network_fallback.UNMEASURED 同一个常量（不是另写的字面量）")
    assert hop_stats.UNMEASURED == nf.UNMEASURED
    assert not any(k.startswith("hop") for k in (hop_stats.HOP_NONE,)), (
        "缺口那一项不许带 hop 前缀 —— 会被读成「第五跳」")


# ============================================================
# ① 第 1 跳命中
# ============================================================


async def test_hop1_hit_counts_only_hop1():
    """本次已采集的数据点命中 ⇒ `hop1_validated` +1，**其余一律不动**。

    防的是"一次取数记成多次命中"：只要有人在多个分支各打一次点，
    或把打点放在函数入口（还没决定谁返回），命中率就会虚高且看不出来。
    """
    text = await _run("CPI", state=_state(validated_points=[
        {"indicator": "CPI", "period_date": "2026-08-01", "value": 0.5,
         "source_name": "stats"}]))

    assert "0.5" in text and "stats" in text, f"第一跳没命中？文本={text!r}"
    assert _counters() == {
        hop_stats.HOP1_VALIDATED: 1,
        hop_stats.HOP2_LOCAL: 0,
        hop_stats.HOP3_CONNECTOR: 0,
        hop_stats.HOP4_NETWORK: 0,
        hop_stats.HOP_NONE: 0,
    }
    snap = hop_stats.snapshot()
    assert snap["total"] == 1 and snap["latest_hop"] == hop_stats.HOP1_VALIDATED


# ============================================================
# ② 第 1 跳空 → 第 2 跳命中
# ============================================================


async def test_hop2_counts_only_when_hop1_really_gave_nothing():
    """`validated_points` 空、本地库有 ⇒ **只** `hop2_local` +1。

    这条是「上一跳真的没给出数据才发生」那条**纪律**的计数版本：
    如果第 1 跳有数据却继续往下走（或反过来，第 2 跳没走却记了一笔），
    这里的分布就会变形 —— 而"顺序即策略"这句话此前无法被任何判据验证。
    """
    text = await _run("CPI", rows=[{"period": "2026-08-01", "value": 1.0}])

    assert "本地库" in text, f"第二跳没命中？文本={text!r}"
    assert _counters() == {
        hop_stats.HOP1_VALIDATED: 0,
        hop_stats.HOP2_LOCAL: 1,
        hop_stats.HOP3_CONNECTOR: 0,
        hop_stats.HOP4_NETWORK: 0,
        hop_stats.HOP_NONE: 0,
    }
    assert hop_stats.snapshot()["latest_hop"] == hop_stats.HOP2_LOCAL


async def test_hop1_hit_never_reaches_the_later_hops():
    """第 1 跳命中时，**后面三跳的计数一个都不许动**（顺序不是"都试一遍"）。"""
    await _run("CPI", state=_state(validated_points=[
        {"indicator": "CPI", "period_date": "2026-08-01", "value": 0.5,
         "source_name": "stats"}]),
        rows=[{"period": "2026-08-01", "value": 1.0}],
        points=[_Point(9.9)])

    assert _counters()[hop_stats.HOP1_VALIDATED] == 1
    assert sum(v for k, v in _counters().items()
               if k != hop_stats.HOP1_VALIDATED) == 0, (
        "第 1 跳已经答出来了，后面的跳却也被记了命中 —— 四跳变成了「都试一遍」")


# ============================================================
# ③ 四跳全空 = 缺口（不是任何一跳的命中）
# ============================================================


async def test_all_four_hops_empty_counts_miss_not_a_hop_hit():
    """四跳全空 ⇒ `misses` +1，且**不算**成任何一跳命中。

    防的是"缺口被算成第四跳命中"：`hop4_network` 计的是**兜底真的给出了数据**，
    与"兜底被走到、但什么也没拿到"是两件事 —— 混在一起，
    「联网兜底触发率」与「兜底成功率」就再也分不开了。
    """
    text = await _run("dv_ratio:600036")           # 四跳全空（白名单空 ⇒ 联网被拒）

    assert nf.UNMEASURED in text, f"四跳全空的结果应如实说未量到：{text!r}"
    counts = _counters()
    assert counts[hop_stats.HOP_NONE] == 1
    # ★ 「四跳都没给数据」不等于「某一跳命中了」—— 后面这四项必须一个都不动
    assert counts[hop_stats.HOP1_VALIDATED] == 0
    assert counts[hop_stats.HOP2_LOCAL] == 0
    assert counts[hop_stats.HOP3_CONNECTOR] == 0
    assert counts[hop_stats.HOP4_NETWORK] == 0, (
        "第四跳被**走到**了但它没给出数据 ⇒ 只能记缺口，不许记成联网命中")
    assert hop_stats.snapshot()["latest_hop"] == hop_stats.HOP_NONE


# ============================================================
# ③b 第三跳命中（打断言 ④ 的正对照）
# ============================================================


async def test_hop3_hit_counts_the_connector_hop():
    """连接器给出数据 ⇒ `hop3_connector` +1（前三跳都没给，第四跳根本没走）。"""
    text = await _run("CPI", points=[_Point(3.3)])

    assert "连接器兜底" in text, f"第三跳没命中？文本={text!r}"
    counts = _counters()
    assert counts[hop_stats.HOP3_CONNECTOR] == 1
    assert counts[hop_stats.HOP4_NETWORK] == 0 and counts[hop_stats.HOP_NONE] == 0


# ============================================================
# ④ ★ 自证：绕过打点的调用路径**不**动计数
# ============================================================


async def test_bypassing_the_instrumented_path_does_not_move_the_counters():
    """★★ **自证**：计数挂在真实路径上，不是挂在随便什么辅助函数上。

    ## 这条防的是什么（本项目实测过的形状）

    「判据接在没人走的路上 = 没接」：`describe()` 曾经零个生产调用方而全绿。
    如果把打点做成一个 `_record_hop()` 之类的**新函数**（只有测试调它），
    那么"命中时会 +1"这类判据**全都会过**，而真实链路一个数都量不到。

    ## 怎么证明（两段，缺一不可）

    1. **绕过**：直接调第三跳的**真实取数实现** `_query_data_via_connectors()`
       —— 它拿到数据、返回与链路里同样的文本，但它不是"决定由哪一跳返回"的
       那一处 ⇒ 计数必须**纹丝不动**（全 0）。
    2. **对照**：**同一个替身**经完整链路 `query_data_for_agent()` ⇒
       `hop3_connector` 必须 +1。

    只有 ① 时判据可能因为"这个函数永远取不到数"而假绿；加上 ② 才排除了这种可能。
    """
    backend = _StubBackend(points=[_Point(3.3)])

    # ① 绕过打点：真实取数实现被调用，且**真的拿到了数据**
    fetched = await supervisor._query_data_via_connectors(
        "CPI", 10, state=_state(), agents=_agents(backend))
    assert fetched and "连接器兜底" in fetched, (
        f"这条路本应取到数据（否则①的断言恒真、证明不了任何事）：{fetched!r}")
    assert backend.calls, "取数实现根本没被调用"
    assert _counters() == {k: 0 for k in hop_stats.HOP_KINDS}, (
        f"绕过四跳链的调用动了计数 ⇒ 计数没挂在真实路径上：{_counters()}")
    assert hop_stats.snapshot()["total"] == 0

    # ② 对照：同一个替身经完整链路 ⇒ 只有第三跳被记
    text = await _run("CPI", points=[_Point(3.3)])
    assert "连接器兜底" in text
    assert _counters()[hop_stats.HOP3_CONNECTOR] == 1, (
        "经完整链路走第三跳却没记上 ⇒ 打点不在决定返回的那一处")


def test_lower_level_fallback_hop_is_not_counted_by_itself():
    """自证（同步版，更强）：**单独调** `data_fallback_hop()` 一次都不许记。

    为什么这条比"绕过第三跳"更硬：第四跳的打点**故意**没有放进
    `data_fallback_hop()` 里。那个函数在测试里被直接调用的地方很多
    （`tests/unit/test_fallback_wiring.py` 有 6 处），若把打点放进它，
    "联网兜底触发率"就会变成"**看谁调过这个函数**"——指标失去意义。
    """
    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", None, limit=10, state=_state(), agents=_agents()))
    assert outcome.triggered is False and outcome.measured is False
    assert _counters() == {k: 0 for k in hop_stats.HOP_KINDS}, (
        f"直接调 data_fallback_hop 就动了计数 ⇒ 指标成了「看谁调过」：{_counters()}")


# ============================================================
# ⑤ `/health` 里字段真的出现（打响应体，不是 import 常量）
# ============================================================


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """与既有 `/health` 契约测试同一套隔离方式（`MOSS_ENV=test` + 临时库）。"""
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "hop_stats.db"))
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
        "`/health` 里没有 `query_data_hops` 段 —— 跳级命中计数又变成只有代码里有，"
        f"运维读不到。顶层现有键：{sorted(body)}")
    return body["query_data_hops"]


def test_health_exposes_query_data_hops_contract(client):
    """★ `/health` 必须给出**逐跳命中数 + 分母 + 缺口 + 空态口径**（字段与类型）。

    这条打的是**真实响应体**：只 import 一个常量证明不了"接线在"
    （本项目实测过「常量在、调用方零个」）。冷启动/空态的判断也在这里 ——
    本项目已登记过「同一形状」的事故：字段名两边各改各的，**不报错，只显示错**。
    """
    hop_stats.reset_for_test()          # 冷启动态：**还没量到**
    sec = _hops_section(client)

    assert sec.get("available") is True, sec
    counters = sec.get("counters")
    assert isinstance(counters, dict), "counters 必须是对象（前端要按键读）"
    assert set(counters) == set(hop_stats.HOP_KINDS), (
        f"counters 的键与 HOP_KINDS 不一致：{sorted(counters)}")
    for key, value in counters.items():
        assert isinstance(value, int) and value >= 0, f"{key}={value!r} 不是非负整数"
    # 空态（**还没量到**）：逐项 0 是**有意义的 0**，而 latest_hop 必须是「未量到」——
    # 用 0 假装"最近一跳是第 0 跳"会让冷启动看起来像故障。
    assert sec["latest_hop"] == hop_stats.UNMEASURED == "未量到", (
        f"冷启动时 latest_hop 必须是「未量到」：{sec['latest_hop']!r}")
    assert isinstance(sec["total"], int) and sec["total"] >= 0
    assert sec["unmeasured"] == "未量到", "空态口径要随响应一起给（否则前端自己写死）"
    assert sec["kinds"] == list(hop_stats.HOP_KINDS), (
        "kinds 是渲染顺序的事实源，不许与计数器脱节")


def test_health_reflects_a_real_hop_hit(client):
    """★ 真的有命中之后，`/health` 的读数**必须跟着动**（不是恒 0 的假字段）。

    用**真实链路**跑一次第一跳命中（只有一个内存过滤，0 I/O），
    再打 `/health` 断言 `hop1_validated == 1` —— 把"埋点 → 暴露"这条线端到端钉住：
    只断字段在、不断数值会漏掉"字段恒 0"这种假绿。
    """
    hop_stats.reset_for_test()
    text = asyncio.run(supervisor.query_data_for_agent(
        "CPI", 10, state=_state(validated_points=[
            {"indicator": "CPI", "period_date": "2026-08-01", "value": 0.5,
             "source_name": "stats"}]),
        agents=_agents()))
    assert "0.5" in text

    sec = _hops_section(client)
    assert sec["counters"][hop_stats.HOP1_VALIDATED] >= 1, (
        f"走过一次第一跳命中，/health 却仍是 {sec['counters']} —— 埋点没接到暴露口")
    assert sec["total"] >= 1
    assert sec["latest_hop"] == hop_stats.HOP1_VALIDATED, (
        f"最近一跳应如实上报：{sec['latest_hop']!r}")


def test_health_hop_section_degrades_instead_of_500(client, monkeypatch):
    """段内异常 ⇒ 降级成 `available: False`，**不许** 500（同 `_search_sources` 的纪律）。

    计数器是纯内存读，理论上不会坏；但 `/health` 是前端 20 秒轮询的面，
    任何一段抛异常都会让整张健康检查变红 —— 那会把"观测坏了"误报成"服务坏了"。
    """
    def _boom() -> dict:
        raise RuntimeError("跳级计数炸了")

    monkeypatch.setattr(hop_stats, "snapshot", _boom)

    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, "跳级计数段坏了却把整张 /health 打挂"
    sec = resp.json()["query_data_hops"]
    assert sec["available"] is False and "跳级计数炸了" in sec["error"]
    assert "counters" not in sec, (
        "读不到时不许给 counters —— 全 0 会被读成「一次都没命中」（假绿）")
