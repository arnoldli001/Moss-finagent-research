"""联网兜底**接线**（call site）的测试 —— `network_fallback.py` → 请求链路。

## 为什么这个文件必须存在（本轮缺陷的**本体**）

`tests/unit/test_network_fallback.py` 的 30 条判据**全绿**，而
`src/infrastructure/catalog/network_fallback.py` 在生产代码里
**一个调用方都没有** —— 全仓库 `grep` 只命中它自己、它自己的测试、
和一个 `docs/` 证据脚本。

按《护栏保真与效果验证》的四道门：
**① 判据认不认得出 → ② 有没有人调它 → ③ 有没有被层叠吃掉 → ④ 打到真实产物**，
它卡在**第 2 道门**，而且**不报错**。所以本文件的第 ① 组判据不是
"护栏判得对不对"（那是上一个文件的事），而是 **"到底有没有人调它"**：

  · 拿掉 `supervisor.data_fallback_hop()` 里那一次 `get_network_fallback()`
    → `test_local_three_hops_empty_with_no_data_really_calls_the_fallback` **变红**；
  · 把 `recommend_node._query_data` 改回自己实现（不再委托）
    → `test_query_data_closure_delegates_to_the_module_level_implementation`
    **变红**（`ast` 判据，不是正则 —— 本仓库实测正则会静默少算）。

## 判据清单（缺一条就等于护栏缺一条）

1. ★ **call site 存在**：本地三跳全空 + `NO_DATA` → `NetworkFallback` 真的被调用
   （spy 计数 + 账本里真的落了 `ALLOWLIST_EMPTY` 拒绝记录 + routes 与 A01 同一份）
2. **不该联网的码一个都不联网**：`NOT_APPLICABLE_FOR_ENTITY` + 5 个环境维度码
   （参数化逐个断言；判据沿用 `FALLBACK_TRIGGER_CODES` / `_NO_FALLBACK_REASONS`，
   **不另起一套**）
3. **护栏拒绝 → 不联网 + 人话理由**：白名单空 / 预算耗尽 / 冷却中 三种各一条，
   理由必须含**剩余额度或剩余时间**，且不是枚举值裸输出
4. **「没量到」≠「量到 0」**：联网拿到 → 结果里有该数据点且来源可追溯；
   联网也拿不到 → 结果是 `未量到`，**一行数据都没有**（`DATA_LINE_RE` 零命中），
   且 `points == []`（不是 `[0]`、也不是占位对象）
5. **输出形状没变**：`query_data` 仍是 `str`（`ToolRegistry.execute` 用
   `str(result)` 喂 LLM）；走一遍**真实消费者** `ToolRegistry.execute` 验证

## 绝不发网络请求

所有取数实现都是注入的假实现；`A01._backend` 是替身；
`MOSS_NETWORK_FALLBACK_PATH` 指到临时目录（**绝不能落到 `data/run/`** ——
那里是 dev/pilot/生产共用的账本，测试里花的钱会真的吃掉生产额度）。
"""
from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.schemas import DataPoint  # noqa: E402
from src.infrastructure.catalog import local_data as ld  # noqa: E402
from src.infrastructure.catalog import network_fallback as nf  # noqa: E402
from src.infrastructure.catalog.local_data import (  # noqa: E402
    Diag,
    DiagCode,
    MetricSeries,
)
from src.infrastructure.llm.circuit_breaker import (  # noqa: E402
    CircuitBreakerRegistry,
)
from src.orchestration import supervisor  # noqa: E402

SUPERVISOR_PY = ROOT / "src" / "orchestration" / "supervisor.py"


# ============================================================
# 测试替身（**全部进程内，不发任何网络请求**）
# ============================================================


class _SpyFactory:
    """`get_network_fallback()` 的替身：**记录被调用过**（本轮的核心判据）。

    为什么替换的是**工厂函数**而不是 `NetworkFallback` 的方法：
    "有没有人调它"问的是**链路上有没有那个 call site**。
    换方法只能证明"某个实例被用过"，换工厂才能证明
    "链路真的去取了那个生效中的单例"（`/health` 与链路必须读同一个）。
    """

    def __init__(self, instance: nf.NetworkFallback) -> None:
        self._instance = instance
        self.calls: list[object] = []

    def __call__(self, routes=None) -> nf.NetworkFallback:
        self.calls.append(routes)
        return self._instance


class _Fetcher:
    """假取数实现：记录 `(indicator, source)`，按脚本返回点 / 空 / 抛错。"""

    def __init__(self, *, points=None, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._points = list(points or [])
        self._error = error

    async def __call__(self, indicator: str, source: str) -> list:
        self.calls.append((indicator, source))
        if self._error is not None:
            raise self._error
        return list(self._points)


class _ConnA:
    """白名单里的源替身（`source_key()` = 类名 `_ConnA`）。"""

    async def fetch(self, indicator: str) -> list:
        return []


class _StubBackend:
    """A01 `_backend` 替身：`fetch` 恒空（第三跳空）+ `_routes` 给第四跳用。"""

    def __init__(self, *, points=None, with_routes: bool = True) -> None:
        self.calls: list[str] = []
        self._points = list(points or [])
        self._routes = [(_ConnA(), lambda _ind: True)] if with_routes else []

    async def fetch(self, indicator: str, *args, **kwargs) -> list:
        self.calls.append(indicator)
        return list(self._points)


class _StubCollector:
    def __init__(self, backend) -> None:
        self._backend = backend


class _StubLocalExecutor:
    """`LocalDataExecutor` 替身：`metric_series` 按脚本返回（默认 `NO_DATA` 空）。"""

    def __init__(self, *, code: str = DiagCode.NO_DATA, rows=None) -> None:
        self.code = code
        self.rows = list(rows or [])
        self.calls: list[str] = []

    def metric_series(self, metric: str, *, entity: str = "", limit: int = 10):
        self.calls.append(metric)
        if self.rows:
            return MetricSeries(metric=metric, entity=entity, points=self.rows,
                                source="local", dataset_id="ds", diag=None)
        return MetricSeries(metric=metric, entity=entity, points=[],
                            diag=Diag(self.code, "测试替身：本地空结果"))


class _Point:
    """假数据点（不是 `DataPoint`）：验证链路对鸭子类型的容忍度。"""

    def __init__(self, value, period_date="2026-09-01", source_name="AkShare"):
        self.value = value
        self.period_date = period_date
        self.source_name = source_name


def _state(**kw):
    base = {"validated_points": [], "focus_stock_code": ""}
    base.update(kw)
    return base


def _agents(backend=None):
    return {"A01_data_collector": _StubCollector(backend or _StubBackend())}


async def _all_hops_empty(key: str, *, code: str = DiagCode.NO_DATA,
                          backend=None, limit: int = 10):
    """跑**生产实现** `query_data_for_agent`，并返回 `(文本, 本地替身, backend)`。"""
    ex = _StubLocalExecutor(code=code)
    backend = backend or _StubBackend()
    original = ld.LocalDataExecutor
    ld.LocalDataExecutor = lambda *a, **k: ex          # 第二跳：本地库空
    try:
        text = await supervisor.query_data_for_agent(
            key, limit, state=_state(), agents=_agents(backend))
    finally:
        ld.LocalDataExecutor = original
    return text, ex, backend


# ============================================================
# 隔离（autouse）：账本 / 白名单环境变量 / 熔断器 / 单例
# ============================================================


@pytest.fixture(autouse=True)
def _isolate(tmp_dir, monkeypatch):
    """四件会串用例的东西一起处理（照抄 `test_network_fallback.py` 的纪律）。

    ★ 账本路径必须指到临时目录：`data/run/` 是 dev/pilot/生产**共用**的，
    测试里"花掉"的额度会真的吃掉生产的日预算（不报错，只表现为"今天怎么不兜底"）。
    ★ 白名单环境变量必须**清掉**：否则开发机 `.env` 里开了兜底时，
    "默认 fail-closed"这条判据会变成"看谁的环境"。
    ★ 熔断注册表是进程级单例，必须换成本用例专用的一份。
    """
    monkeypatch.setenv("MOSS_NETWORK_FALLBACK_PATH",
                       str(Path(tmp_dir) / "nf_wiring.json"))
    monkeypatch.delenv(nf.ALLOWLIST_ENV, raising=False)
    registry = CircuitBreakerRegistry()
    monkeypatch.setattr(nf, "get_circuit_registry", lambda: registry)
    nf.reset_network_fallback()
    yield
    nf.reset_network_fallback()


def _disabled_instance(tmp_dir) -> nf.NetworkFallback:
    """默认态实例（白名单空 = fail-closed），账本指到临时目录。"""
    return nf.NetworkFallback(state_path=str(Path(tmp_dir) / "nf.json"))


def _enabled_instance(tmp_dir, fetcher, *, allowlist=("_ConnA",), **kw):
    """开着的实例（白名单非空 + 注入假取数实现），账本指到临时目录。"""
    kw.setdefault("daily_budget_cny", nf.DEFAULT_DAILY_BUDGET_CNY)
    kw.setdefault("max_calls_per_hour", nf.DEFAULT_MAX_CALLS_PER_HOUR)
    kw.setdefault("call_cost_cny", nf.DEFAULT_CALL_COST_CNY)
    return nf.NetworkFallback(allowlist=allowlist, fetcher=fetcher,
                              state_path=str(Path(tmp_dir) / "nf.json"), **kw)


# ============================================================
# ① ★ call site 存在（**本任务的核心判据**）
# ============================================================


def test_local_three_hops_empty_with_no_data_really_calls_the_fallback(
        tmp_dir, monkeypatch):
    """★★ **「有没有人调它」的机器复现** —— 拿掉 call site 这条就红。

    ## 实测证据（2026-09-29，不是推演）

    把 `data_fallback_hop` 里那一行 `fb = get_network_fallback(routes=routes)`
    注释掉、改成本地 `NetworkFallback()`，然后跑本文件：

        uv run python -m pytest tests/unit/test_fallback_wiring.py -q
        → 11 failed, 16 passed        （测于本文件 27 条判据的版本）

    其中本条报的原文是：

      「本地三跳全空 + NO_DATA，却**一次都没有**去取联网兜底实例 ——
        护栏不在请求链路上（这正是本任务要修的缺陷）」

    改回那一行 → 27 passed。**这就是"我改了 ≠ 它生效了"的第三道门**。

    ⚠️ 那两个数字是**测出来的**，对应 27 条判据的版本；此后又加了 1 条
    （`test_fallback_output_has_a_hard_row_cap_written_in_code`，它同样依赖
    这个 call site）—— 所以现在的数字只会 ≥ 12 failed，**不要照抄旧数**，
    要引用就重跑一次（改一行、跑一次，约 1 分钟）。

    场景：本地**三跳全空** + 诊断码 = `NO_DATA`
      · 第 1 跳 `validated_points` → 空（state 里没有该指标）
      · 第 2 跳 `LocalDataExecutor.metric_series` → `NO_DATA`（替身）
      · 第 3 跳 A01 backend → 返回 `[]`（替身，且**断言它真的被问过**）

    三条断言分别在证明不同的事（缺一条都会漏掉一种"没人调"）：

      A. **工厂被调用过**（`spy.calls == 1`）——
         这是"链路上有那个 call site"。
      B. **routes 与 `ConnectorRouter` 同一份**（`spy.calls[0]` 非空）——
         证明"调了，而且调的是能真取数的那个实例"；只判 A 的话，
         一个永远取不到数的实例也能让测试变绿。
      C. **护栏真的跑了一遍并落了账**（账本 `rejected_by[ALLOWLIST_EMPTY] == 1`）——
         证明 `fetch_outcome()` 被真正执行；只判 A、B 的话，
         "调了工厂但没调护栏"照样绿。
    """
    backend = _StubBackend(with_routes=True)
    instance = _disabled_instance(tmp_dir)
    spy = _SpyFactory(instance)
    monkeypatch.setattr(nf, "get_network_fallback", spy)

    text, ex, backend = asyncio.run(_all_hops_empty("dv_ratio:600036",
                                                    backend=backend))

    # 前提：三跳真的都走空了（否则这条测试测的是别的东西）
    assert backend.calls, "第三跳（连接器）没有被问过 —— 场景没构造出来"
    assert ex.calls, "第二跳（本地库）没有被问过 —— 场景没构造出来"

    # A. 工厂被调用过
    assert len(spy.calls) == 1, (
        "本地三跳全空 + NO_DATA，却**一次都没有**去取联网兜底实例 —— "
        "护栏不在请求链路上（这正是本任务要修的缺陷）")
    # B. 与 ConnectorRouter 同一份 routes（否则兜底拿不到任何源）
    assert spy.calls[0], "取兜底实例时没有把 ConnectorRouter 的 routes 传进去"
    assert len(spy.calls[0]) == 1
    # C. 护栏真的判定并记账了（默认 fail-closed → 一条 ALLOWLIST_EMPTY 拒绝）
    counters = instance.snapshot()["counters"]
    assert counters is not None, "账本没有建立 —— 护栏没有被真正执行"
    assert counters["rejected_by"].get(nf.ReasonCode.ALLOWLIST_EMPTY) == 1, (
        f"护栏没有被执行（rejected_by={counters['rejected_by']}）")
    assert counters["totals"]["attempts"] == 0, "默认态下不许有任何真实尝试"

    # 结果里必须说清"没量到"，且能看出是**护栏拒绝**（不是"源上没有"）
    assert nf.UNMEASURED in text
    assert "未放行" in text and "白名单为空" in text


def test_trigger_judgement_has_exactly_one_implementation(tmp_dir, monkeypatch):
    """触发判据**只有一份实现**：`_fallback_trigger` 必须等于 `should_fallback`。

    「同一判断只允许一份实现」的机器复现：把同一个本地结果分别喂给
    两条路径，**结果必须逐字相等**（本项目实测过"在线人数 3≠1"就是这么来的）。
    """
    for code in (DiagCode.NO_DATA, DiagCode.NO_TABLE, DiagCode.NO_COLUMN,
                 DiagCode.CONN_FAIL, DiagCode.STALE_BEYOND_TOLERANCE,
                 DiagCode.NOT_APPLICABLE_FOR_ENTITY, DiagCode.PROD_ONLY,
                 DiagCode.ENV_NOT_COVERED, "FUTURE_UNKNOWN_CODE"):
        res = MetricSeries(metric="x", points=[], diag=Diag(code, "d"))
        assert supervisor._fallback_trigger(res, None) == nf.should_fallback(res), (
            f"{code}：两条路径的结论不一致 —— 判据被实现了两遍")


def test_stale_plan_also_routes_through_the_same_judgement():
    """没有诊断码、只有 `plan.stale_days` 的本地结果，同样走那一份判据。"""
    fresh = MetricSeries(metric="x", points=[{"period": "2026-09-01", "value": 1}],
                         plan={"stale_days": 1})
    stale = MetricSeries(metric="x", points=[{"period": "2020-01-01", "value": 1}],
                         plan={"stale_days": nf.STALE_TRIGGER_DAYS + 1})
    # 注意：`MetricSeries.ok` 需要 points 非空且 diag 为 None —— 这里正是"有数据"态
    assert supervisor._fallback_trigger(fresh, None)[0] is False
    assert supervisor._fallback_trigger(stale, None)[0] is True
    assert (supervisor._fallback_trigger(stale, None)
            == nf.should_fallback(stale))


def test_local_executor_crash_is_treated_as_conn_fail_not_no_data():
    """★ 本地执行器**抛异常**（`res is None`）时，按 `CONN_FAIL` 登记。

    为什么这条要单独钉住：原实现的 `except` 只 `logger.debug` ——
    "本地这一跳根本没量到"**看起来与"本地有数据且新鲜"一模一样**
    （都落到 `should_fallback(None)` 的同一分支）。不区分的话，
    本地库一挂，联网兜底反而**不会触发**（因为读出来的是"本地已有数据"）。
    """
    triggered, why = supervisor._fallback_trigger(None, RuntimeError("库挂了"))
    assert triggered is True, "本地库抛异常时必须允许联网兜底（最差兜底）"
    assert DiagCode.CONN_FAIL in why
    # 必须**不是** NO_DATA：两者的下一步动作不同（查连接 vs 补采）
    assert DiagCode.NO_DATA not in why

    # 没有异常、也没有结果对象 → 未知即不许花钱（fail-closed）
    triggered, why = supervisor._fallback_trigger(None, None)
    assert triggered is False and "未量到" in why


def test_query_data_closure_delegates_to_the_module_level_implementation():
    """★ `recommend_node._query_data` 必须**委托**给模块级实现（`ast` 判据）。

    为什么用 `ast` 而不是正则：本项目实测正则版踩过三个坑（捕获组编号、
    `\\s` 吞并跨行、docstring 里的示例被当成真引用），全部是静默少算或假告警。
    这条判据挡住的是**"call site 被搬到一个没人走的副本里"** ——
    行为测试（上面那条）测的是模块级函数，本条约束闭包必须走同一条路。
    """
    tree = ast.parse(SUPERVISOR_PY.read_text(encoding="utf-8"))
    node = _find_nested_async(tree, "recommend_node", "_query_data")
    called = {c.func.id for c in ast.walk(node)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "query_data_for_agent" in called, (
        "`recommend_node._query_data` 不再委托给 `query_data_for_agent()` —— "
        "于是第四跳（联网兜底）被绕开了，而**什么都不会报错**")
    kwargs = {k.arg for c in ast.walk(node) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Name)
              and c.func.id == "query_data_for_agent"
              for k in c.keywords}
    assert {"state", "agents"} <= kwargs, "委托时没有把 state/agents 传全"


def test_the_singleton_is_read_from_exactly_one_place_in_supervisor():
    """`get_network_fallback()` 在 supervisor 里**只有一个** call site。

    护栏的判据必须过同一条路：多一个 call site 就多一处"忘了带 routes /
    忘了判诊断码"的机会（而它同样不报错）。新增时必须显式改这条断言。
    """
    tree = ast.parse(SUPERVISOR_PY.read_text(encoding="utf-8"))
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "get_network_fallback"]
    assert len(hits) == 1, (
        f"supervisor 里有 {len(hits)} 处 get_network_fallback 调用"
        "（期望恰好 1 处：data_fallback_hop）")


def test_no_fallback_trigger_exemptions_are_left_behind():
    """★ 「豁免必须带过期语义」—— 登记表必须**是空的**。

    `_FALLBACK_TRIGGER_EXEMPTIONS` 只可能把"该联网"改成"不联网"
    （fail-closed 方向），所以它本身是安全的；但**任何**豁免都会腐烂成
    永久豁免（本项目实测过 `_KNOWN_UNGUARDED` 那条路）。
    这条断言就是它的过期语义：谁加一条，谁就必须同时改这条测试并说明
    "什么时候删掉"。
    """
    assert supervisor._FALLBACK_TRIGGER_EXEMPTIONS == {}, (
        "第四跳出现了豁免条目 —— 请说明过期条件，或把它加进 "
        "network_fallback._NO_FALLBACK_REASONS（那才是唯一的事实源）")


def _find_nested_async(tree: ast.AST, outer: str, inner: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == outer:
            for sub in ast.walk(node):
                if isinstance(sub, ast.AsyncFunctionDef) and sub.name == inner:
                    return sub
    raise AssertionError(f"没有找到 {outer}.{inner}（测试的前提本身不成立）")


# ============================================================
# ② 不该联网的诊断码：一个都不许联网（参数化）
# ============================================================


#: 5 个**环境维度**码 + 1 个**语义不适用**码（用户点名的那 6 个）。
_NEVER_ONLINE_CODES = (
    DiagCode.NOT_APPLICABLE_FOR_ENTITY,
    DiagCode.ENV_NOT_COVERED,
    DiagCode.PROD_ONLY,
    DiagCode.DEV_ONLY,
    DiagCode.DEV_SYNC_DELAY,
    DiagCode.PROD_PERMISSION_DENIED,
)


@pytest.mark.parametrize("code", _NEVER_ONLINE_CODES)
def test_non_trigger_codes_never_touch_the_network(tmp_dir, monkeypatch, code):
    """这 6 个码**各自**都不许触发联网（逐个断言，不是"抽样一个"）。

    理由（沿用 `_NO_FALLBACK_REASONS`，**不另起一套**）：
      · `NOT_APPLICABLE_FOR_ENTITY`：银行没有"流动比率"——**口径问题**，
        不是数据缺失，联网拿不到同一口径；
      · 5 个环境维度码：`本环境没有 ≠ 没有数据`，要的是**同步或换环境**，
        联网只会白花预算，还可能把另一个环境的数当成这个环境的。
    """
    spy = _SpyFactory(_disabled_instance(tmp_dir))
    monkeypatch.setattr(nf, "get_network_fallback", spy)

    text, _ex, _backend = asyncio.run(_all_hops_empty("流动比率:600036",
                                                      code=code))
    assert spy.calls == [], (
        f"{code} 触发了联网兜底 —— 联网拿不到同一口径，只会白花预算")
    assert nf.UNMEASURED in text, "不联网也必须说清『没量到』"
    assert "未触发" in text and code in text, "结果里要说清为什么没联网"


def test_trigger_set_and_no_trigger_set_stay_partitioned():
    """两张表必须**不重叠**且**并集 == DiagCode 全集**（沿用既有判据）。

    这条是 `test_network_fallback.py::test_trigger_codes_cover_every_diag_code_without_silent_gaps`
    在**接线侧**的影子：接线侧不许自己再维护一份"哪些码该联网"的清单。
    """
    real = {v for k, v in vars(DiagCode).items()
            if not k.startswith("_") and isinstance(v, str)}
    no_fb = set(nf._NO_FALLBACK_REASONS)                      # noqa: SLF001
    assert nf.FALLBACK_TRIGGER_CODES | no_fb == real
    assert not (nf.FALLBACK_TRIGGER_CODES & no_fb)
    assert set(_NEVER_ONLINE_CODES) <= no_fb, (
        "本文件点名的 6 个码必须落在『不触发』表里（接线侧不许另立标准）")


# ============================================================
# ③ 护栏拒绝：不联网 + 人话理由（含剩余额度 / 剩余时间）
# ============================================================


def _assert_human_reason(reason: str, code: str) -> None:
    """拒绝理由必须是**人话 + 剩余量**，不能是枚举值裸输出。

    判据：① 不是码本身；② 有中文；③ 有"还剩多少"的说法 ——
    **剩余时间**（还剩 N 分钟 / 再过 N 分钟）或**剩余额度**（已用尽 X/Y 元、
    `X/Y 元` 这种已用/总量写法）。白名单为空时没有冷却，所以它的"还剩多少"
    是额度而不是时间；两种情况都必须说得出"还差多少才轮到我"。
    """
    assert code and reason and reason != code, f"理由就是枚举值：{reason!r}"
    assert len(reason) > 20, f"理由太短，说不清：{reason!r}"
    assert re.search(r"[\u4e00-\u9fff]", reason), f"理由不是中文：{reason!r}"
    assert re.search(r"还剩|剩余|已用尽|再过|\d+\.\d+/\d+\.\d+\s*元", reason), (
        f"理由里没有『还剩多少』：{reason!r}")


def test_empty_allowlist_refuses_without_any_network_call(tmp_dir, monkeypatch):
    """默认态（白名单空）：拒绝、**一次网络都不发**、理由说清怎么开。"""
    fetcher = _Fetcher(points=[_Point(1.0)])
    instance = nf.NetworkFallback(state_path=str(Path(tmp_dir) / "nf.json"),
                                 fetcher=fetcher)      # allowlist 默认 = 空
    nf.set_network_fallback(instance)

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))

    assert outcome.triggered is True and outcome.allowed is False
    assert outcome.measured is False and outcome.points == []
    assert outcome.code == nf.ReasonCode.ALLOWLIST_EMPTY
    _assert_human_reason(outcome.reason, outcome.code)
    assert "白名单为空" in outcome.reason and "fail-closed" in outcome.reason
    assert "今日预算" in outcome.reason, "拒绝理由里要给出剩余额度"
    assert fetcher.calls == [], "白名单为空却调用了源 —— fail-closed 被破坏"


def test_exhausted_daily_budget_refuses_without_network_call(tmp_dir, monkeypatch):
    """预算耗尽：拒绝、不联网、理由含「已用尽 X/Y 元」。"""
    fetcher = _Fetcher(points=[_Point(1.0)])
    instance = _enabled_instance(tmp_dir, fetcher,
                                 daily_budget_cny=nf.DEFAULT_CALL_COST_CNY,
                                 call_cost_cny=nf.DEFAULT_CALL_COST_CNY)
    nf.set_network_fallback(instance)
    # 先把今日额度用掉（一次成功的兜底调用）
    first = asyncio.run(instance.fetch_outcome("别的指标"))
    assert first.measured is True
    calls_after_first = len(fetcher.calls)

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))

    assert outcome.allowed is False and outcome.measured is False
    assert outcome.code == nf.ReasonCode.DAILY_BUDGET_EXHAUSTED
    _assert_human_reason(outcome.reason, outcome.code)
    assert "已用尽" in outcome.reason and "元" in outcome.reason
    assert len(fetcher.calls) == calls_after_first, (
        "预算已用尽却还在调用源 —— 上限没有真正生效")


def test_cooldown_refuses_with_remaining_time(tmp_dir, monkeypatch):
    """冷却中：拒绝、不联网、理由含「冷却中，还剩 N 分钟」。"""
    fetcher = _Fetcher(error=RuntimeError("源挂了"))
    instance = _enabled_instance(tmp_dir, fetcher)
    nf.set_network_fallback(instance)
    # 第一次：真的去试，失败 → 挂上 FAILURE_COOLDOWN_SECONDS 冷却
    first = asyncio.run(instance.fetch_outcome("dv_ratio:600036"))
    assert first.measured is False and first.allowed is True
    calls_after_first = len(fetcher.calls)
    assert calls_after_first == 1

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))

    assert outcome.allowed is False and outcome.measured is False
    assert outcome.code == nf.ReasonCode.COOLDOWN
    _assert_human_reason(outcome.reason, outcome.code)
    assert "冷却中" in outcome.reason and "还剩" in outcome.reason
    assert re.search(r"分钟|秒", outcome.reason), "要给出**剩余时间**"
    assert len(fetcher.calls) == calls_after_first, "冷却中却还在调用源"


def test_circuit_breaker_open_refuses(tmp_dir, monkeypatch):
    """熔断：源被打成 OPEN 后，拒绝理由含熔断与恢复时间。"""
    fetcher = _Fetcher(error=RuntimeError("源挂了"))
    instance = _enabled_instance(tmp_dir, fetcher,
                                 allowlist=("_ConnA",))
    nf.set_network_fallback(instance)
    # 打满熔断阈值（与既有实现同一个注册表；判据不另写一套）
    cb = nf.get_circuit_registry().get_or_create("fallback:_ConnA")
    for _ in range(200):
        cb.record_failure()
    assert cb.allow_request() is False, "熔断器没有打开 —— 测试前提不成立"

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))
    assert outcome.allowed is False
    assert outcome.code in (nf.ReasonCode.CIRCUIT_OPEN,
                            nf.ReasonCode.NO_SOURCE_AVAILABLE)
    _assert_human_reason(outcome.reason, outcome.code)
    assert "熔断" in outcome.reason


def _local_no_data() -> MetricSeries:
    return MetricSeries(metric="dv_ratio", points=[],
                        diag=Diag(DiagCode.NO_DATA, "真缺口"))


# ============================================================
# ④ 拿到真实数据 vs 没量到（**绝不用 0 糊**）
# ============================================================


def test_real_points_are_rendered_with_traceable_source(tmp_dir, monkeypatch):
    """联网**拿到**数据 → 结果里出现该数据点，且来源可追溯。"""
    point = DataPoint(indicator="dv_ratio:600036", value=5.42,
                      period_date="2026-09-01", source_name="AkShare",
                      source_url="https://akshare.akfamily.xyz/")
    fetcher = _Fetcher(points=[point])
    nf.set_network_fallback(_enabled_instance(tmp_dir, fetcher))

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))

    assert outcome.measured is True and outcome.allowed is True
    assert outcome.source == "_ConnA", "要能指认是哪个源给的（可追溯）"
    assert outcome.points and outcome.points[0] is point
    assert str(point.source_url), "溯源字段必须随数据点一起回来"
    assert "5.42" in outcome.text and "2026-09-01" in outcome.text
    assert "AkShare" in outcome.text, "每一行都要带来源"
    assert supervisor.DATA_LINE_RE.search(outcome.text), "数据行格式没渲染出来"

    # 走完整链路（第四跳的结果要真的进 `query_data` 的返回值）
    text, _ex, _backend = asyncio.run(_all_hops_empty("dv_ratio:600036"))
    # 注：`_all_hops_empty` 没装实例 → 用的是本用例已装入的那个
    assert "5.42" in text


def test_no_data_from_network_is_unmeasured_and_has_no_value(tmp_dir, monkeypatch):
    """联网**也拿不到** → 结果是「未量到」：**一行数据都没有**（没有 0、没有占位）。"""
    fetcher = _Fetcher(points=[])                      # 源返回空
    instance = _enabled_instance(tmp_dir, fetcher)
    nf.set_network_fallback(instance)

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))

    assert outcome.allowed is True, "护栏放行了（区别于被拒绝）"
    assert outcome.measured is False
    assert outcome.points == [], "没量到就必须是空 —— 不许 [0] / 占位对象"
    assert outcome.code == nf.ReasonCode.ALL_SOURCES_FAILED
    assert "未量到" in outcome.reason and "已放行" in outcome.reason
    assert not supervisor.DATA_LINE_RE.search(outcome.text), (
        f"没量到的结果里出现了数据行：{outcome.text!r}")
    # 护栏放行 = 真的去试了（与"被拒绝、一次都没试"必须可分）
    assert fetcher.calls == [("dv_ratio:600036", "_ConnA")]
    assert instance.snapshot()["counters"]["totals"]["attempts"] == 1


def test_full_chain_text_explains_release_without_data(tmp_dir, monkeypatch):
    """走完整链路（`query_data_for_agent`）：说清"联网了但源上也没有"+ 禁止用 0。"""
    fetcher = _Fetcher(points=[])
    nf.set_network_fallback(_enabled_instance(tmp_dir, fetcher))

    text, _ex, _backend = asyncio.run(_all_hops_empty("dv_ratio:600036"))

    assert nf.UNMEASURED in text
    assert "已放行、但没取到" in text, "要说清是『联网了但源上也没有』"
    assert not supervisor.DATA_LINE_RE.search(text), (
        "给 LLM 的文本里出现了数据行 —— 等于编了一个数")
    assert "不要" in text and "0" in text, "必须明确禁止用 0 替代"


def test_points_without_values_are_unmeasured_not_zero(tmp_dir, monkeypatch):
    """★ 源返回了 N 个点但**每一个都没有值** → 仍是未量到，且如实说出来。

    这是「没量到」与「量到 0」的边界：`DataPoint.value is None` 的点
    **不是 0**。把它渲染成 0 就是本项目最贵的那类缺陷
    （"宁可不显示，也不显示假绿"）。
    """
    fetcher = _Fetcher(points=[_Point(None), _Point(None)])
    nf.set_network_fallback(_enabled_instance(tmp_dir, fetcher))

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))

    assert outcome.measured is False, "全是空值 → 未量到"
    assert outcome.points == []
    assert not supervisor.DATA_LINE_RE.search(outcome.text)
    assert "没有一个带数值" in outcome.text and nf.UNMEASURED in outcome.text


def test_mixed_points_report_measured_and_unmeasured_separately(tmp_dir, monkeypatch):
    """混合：有值的进结果，没值的**单独计数**并说明"不是 0"。"""
    fetcher = _Fetcher(points=[_Point(5.42), _Point(None)])
    nf.set_network_fallback(_enabled_instance(tmp_dir, fetcher))

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10, state=_state(),
        agents=_agents()))

    assert outcome.measured is True
    assert "5.42" in outcome.text
    assert nf.UNMEASURED in outcome.text and "不是 0" in outcome.text


# ============================================================
# ⑤ 输出形状没变（`query_data` 仍然是 `str`）
# ============================================================


def test_query_data_still_returns_str_on_every_path(tmp_dir, monkeypatch):
    """★ 四条路径的返回值**都必须是 `str`**（形状没变）。

    为什么必须钉住：唯一的消费者是
    `ToolRegistry.execute()`（`src/domain/agents/analysis/react.py:101`）——
    它做的是 `return str(result)`，喂给 LLM 的是一段**文本**。
    改成 dict/对象不会报错，只会让 LLM 收到 `{'points': [...]}` 的 repr。
    """
    # ① 第一跳（已采集点）
    text = asyncio.run(supervisor.query_data_for_agent(
        "CPI", 10, state=_state(validated_points=[
            {"indicator": "CPI", "period_date": "2026-08-01", "value": 0.5,
             "source_name": "stats"}]),
        agents=_agents()))
    assert isinstance(text, str) and "0.5" in text

    # ② 第二跳（本地库命中）
    ex = _StubLocalExecutor(rows=[{"period": "2026-08-01", "value": 1.0}])
    original = ld.LocalDataExecutor
    ld.LocalDataExecutor = lambda *a, **k: ex
    try:
        text = asyncio.run(supervisor.query_data_for_agent(
            "CPI", 10, state=_state(), agents=_agents()))
    finally:
        ld.LocalDataExecutor = original
    assert isinstance(text, str) and "本地库" in text

    # ③ 第三跳（连接器命中）
    backend = _StubBackend(points=[_Point(3.3, "2026-08-01", "Tencent")])
    text, _ex, _b = asyncio.run(_all_hops_empty("CPI", backend=backend))
    assert isinstance(text, str) and "连接器兜底" in text

    # ④ 四跳全空（第四跳 = 联网兜底）
    text, _ex, _b = asyncio.run(_all_hops_empty("CPI"))
    assert isinstance(text, str) and nf.UNMEASURED in text


def test_the_real_consumer_tool_registry_still_gets_a_string(tmp_dir, monkeypatch):
    """★ 走一遍**真实消费者** `ToolRegistry.execute`（形状断言的端到端版）。

    `ToolRegistry` 是 `query_data` 唯一的消费方（`supervisor.py` 里
    `tools.register("query_data", _query_data, ...)`），它内部 `str(result)`。
    这条比"返回类型是 str"更强：它证明**经过真实消费者之后**仍然拿到文本。
    """
    from src.domain.agents.analysis.react import ToolRegistry

    spy = _SpyFactory(_disabled_instance(tmp_dir))
    monkeypatch.setattr(nf, "get_network_fallback", spy)

    async def _run() -> str:
        tools = ToolRegistry()
        ex = _StubLocalExecutor()

        async def _q(indicator: str, limit: int = 10) -> str:
            original = ld.LocalDataExecutor
            ld.LocalDataExecutor = lambda *a, **k: ex
            try:
                return await supervisor.query_data_for_agent(
                    indicator, limit, state=_state(), agents=_agents())
            finally:
                ld.LocalDataExecutor = original

        tools.register("query_data", _q, "测试用")
        return await tools.execute("query_data", {"indicator": "dv_ratio:600036"})

    out = asyncio.run(_run())
    assert isinstance(out, str)
    assert nf.UNMEASURED in out
    assert len(spy.calls) == 1, "经过真实消费者时 call site 也必须走到"


def test_structured_outcome_carries_the_machine_readable_half(tmp_dir, monkeypatch):
    """结构化的那一半必须存在（判据不许只活在给 LLM 看的散文里）。"""
    outcome = supervisor.DataFallbackOutcome(triggered=False, trigger="x")
    for name in ("triggered", "trigger", "allowed", "measured", "code",
                 "reason", "source", "text", "points"):
        assert hasattr(outcome, name), f"DataFallbackOutcome 缺字段 {name}"
    assert outcome.measured is False and outcome.points == []


def test_fallback_output_has_a_hard_row_cap_written_in_code(tmp_dir, monkeypatch):
    """★ 第四跳的**输出上限**必须写进代码，且超限行为可见。

    AGENTS.md《AI 首轮编码硬约束》：「上限必须写进代码，不能留在注释里。
    任何『一般不会超过…』都是缺陷」。这条链路的 `limit` 来自 **LLM 的工具调用
    参数**，而文本会原样回到 LLM 上下文 —— 没有上限就是让模型一次把上下文顶满。

    判据写成**数字**（行数）而不是"看着不多"：
      · 渲染行数 ≤ `MAX_FALLBACK_ROWS`，即使 `limit` 传得更大；
      · 表头**如实写明**源返回了多少条 —— 截断必须可见。
    """
    assert supervisor.MAX_FALLBACK_ROWS > 0
    points = [_Point(float(i), f"2026-08-{i:02d}") for i in range(1, 41)]
    nf.set_network_fallback(_enabled_instance(tmp_dir, _Fetcher(points=points)))

    outcome = asyncio.run(supervisor.data_fallback_hop(
        "dv_ratio:600036", _local_no_data(), limit=10_000, state=_state(),
        agents=_agents()))

    lines = [ln for ln in outcome.text.splitlines()
             if supervisor.DATA_LINE_RE.search(ln)]
    assert len(lines) == supervisor.MAX_FALLBACK_ROWS, (
        f"渲染了 {len(lines)} 行 —— 上限 {supervisor.MAX_FALLBACK_ROWS} 没生效")
    assert "40 条" in outcome.text, "表头要如实写明源返回了多少条（截断可见）"
    assert f"最多列 {supervisor.MAX_FALLBACK_ROWS} 行" in outcome.text
    # 上限只管渲染：**数据点一个都不许少**（截断属于展示层，不许吃掉数据）
    assert len(outcome.points) == len(points)


def test_bad_points_are_not_rendered_as_data_lines():
    """★ `_render_fallback_points` 的**纯函数**判据：空值行绝不渲染。

    单测到函数一级是为了让"编数"这件事**在最里面一层就被挡住** ——
    等到结果文本那层才发现，就已经晚了。
    """
    body, measured, unmeasured = supervisor._render_fallback_points(
        [_Point(None), _Point(0.0), _Point(None)], source="ConnA", limit=10)
    # ⚠️ `0.0` 是**量到的 0**（源明确给了 0）—— 它必须出现；
    #    `None` 是**没量到** —— 它一行都不许有。
    assert measured == 1 and unmeasured == 2
    assert body.count("\n") == 0, f"只该有 1 行数据：{body!r}"
    assert "0.0" in body
    assert "None" not in body and "nan" not in body.lower()
