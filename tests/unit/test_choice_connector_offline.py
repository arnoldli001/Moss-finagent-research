"""Choice 连接器的**离线护栏 + 「断开主用」演练**（`CHG-0130`）。

`.trae/skills/backup-path-availability` 的 **R1** 要求：接一个新源之前，
**先建"断开主用"的离线演练**。Choice 今天登录都过不去（账号没权限），
所以本文件是**唯一**能在开通之前把这条链验掉的地方 —— 全部离线，不碰真 SDK。

## 本文件守的四件事

| # | 守什么 | 为什么它值得一条判据 |
|---|---|---|
| ① | 今天 `INDICATORS` **必须为空** | 这是目标里那句「**在授权到位前不得把它们声明为可用源**」的机器形态。写在文档里的纪律会被忽略，写成判据的不会 |
| ② | 闸门没开 ⇒ **一条路由都不接** | 接上去 = 自己造一条必然失败的路径 |
| ③ | `fetch()` 非 `ok` 时**抛错**，**绝不返回 `[]`** | `[]` 会被链上读成"这个源没这个指标的数据"（正常空结果，甚至进缺口队列让 A19 去补）；真相是**权限**，补一万次也补不出来 |
| ④ | **开通之后无需改逻辑**：假闸门 `ok` + 假取数 ⇒ 主源失败时真的会切到 Choice，且数据带溯源 | 否则"开通即可用"只是句口号 —— 我没验过它 |

## 演练为什么用**真的** `ConnectorRouter`

③④ 都可以拿连接器对象自己测，但那样证明不了"**链上真的会切过去**" ——
路由顺序、`supports()` 的命中、失败后的下一次尝试，都是 `ConnectorRouter` 的行为。
所以第 ④ 条走**真路由器**（`routes=[(必挂的主源), (Choice)]`），只把
SDK 与口径注入成假的。
"""
from __future__ import annotations

import asyncio

import pytest

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors import choice_connector as C
from src.infrastructure.connectors import choice_gate as G

# ─────────────────────────── 假件（全部离线） ───────────────────────────


def _status(state: str) -> G.ChoiceStatus:
    return G.ChoiceStatus(state, f"fake:{state}", raw_code="10001003")


def _probe_ok() -> G.ChoiceStatus:
    return _status(G.STATE_OK)


def _point(indicator: str, value: float = 1.23) -> DataPoint:
    return DataPoint(
        indicator=indicator, value=value, unit="元", period_date="2026-09-30",
        extra={"source": "fake-choice"}, source_name="fake",
        fetch_method=FetchMethod.API_CALL, confidence=0.9,
    )


class _PrimaryDown:
    """一定会失败的主源（演练"断开主用"）。

    `supports` 收窄成"只支持日线"：这样反演练才能问出
    "闸门关着时，一个只有 Choice 能供的指标会怎样"。
    """

    source_name = "必挂的主源"
    source_url = "http://127.0.0.1:1/"

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith("stock:close")

    def get_capabilities(self) -> dict:
        return {"name": self.source_name, "indicators": ["stock:close:*"]}

    async def fetch(self, indicator, start_date=None, end_date=None):  # noqa: ANN001
        raise DataFetchError("主源断了（演练）")


NON_OK_STATES = [s for s in G.STATES if s != G.STATE_OK]


# ─────────────────── ① 「不得声明为可用源」的机器形态 ───────────────────


def test_indicators_stay_empty_until_the_gate_is_open() -> None:
    """★ 目标原话的判据：**授权到位前不得声明为可用源**。

    这条红了**不要删**：它红了说明有人往 `INDICATORS` 里填了东西 ——
    那时该做的是**同时**完成三件事（改判断 + 改本条 + 登台账）：
      ① `uv run python scripts/check_external_sources.py` 退出码 **0**；
      ② 那个指标**真的取到过数**（SDK 函数 + 参数 + 单位都记下来）；
      ③ 在 `docs/PRD.md` §19.33.5 把"待办"改成"已接"并写清口径。
    """
    assert C.INDICATORS == (), (
        "Choice 的指标口径被填上了，但本轮没有任何『真登录成功 + 真取到数』的证据。"
        f"当前值：{C.INDICATORS!r} —— 见本判据 docstring 里的三步。"
    )


def test_connector_cannot_hijack_any_indicator() -> None:
    """口径为空 ⇒ `supports()` 对**任何**指标都是 False（含别人家的指标）。"""
    for ind in ("choice:test", "stock:close:600036", "macro:gdp", "", "*"):
        assert C.ChoiceConnector.supports(ind) is False, ind


def test_capabilities_do_not_advertise_availability() -> None:
    """能力描述必须**如实**说不可用（面板若展示它，不能看起来像一个可用源）。"""
    cap = C.ChoiceConnector().get_capabilities()
    assert cap["wireable"] is False
    assert cap["indicators"] == []
    assert cap["source_type"] == DataSourceType.API.value
    assert cap["pending"], "必须写清还差什么，否则看面板的人只能猜"


# ─────────────────── ② 闸门没开 ⇒ 不接线（含"填了口径也不行"） ───────────────────


def test_no_routes_while_indicators_empty() -> None:
    """今天（口径为空）⇒ 零路由；且**不应该**去打网络（用会抛异常的探针证明）。"""

    def _boom() -> G.ChoiceStatus:
        raise AssertionError("口径为空时不该探测闸门（启动期不许有外部等待）")

    assert C.build_choice_routes(probe_fn=_boom) == []


@pytest.mark.parametrize("state", NON_OK_STATES)
def test_filled_indicators_still_refuse_when_gate_closed(state, monkeypatch) -> None:
    """★ 关键的一条：**即使有人填了口径**，闸门不是 `ok` 也一条路由都不接。

    这是"两个条件同时满足"的判据 —— 只查 `INDICATORS` 非空是不够的：
    权限没开通时接了线，指标再对也是每次都失败。
    """
    monkeypatch.setattr(C, "INDICATORS", ("choice:test",))
    routes = C.build_choice_routes(probe_fn=lambda: _status(state))
    assert routes == [], f"state={state} 时不该接线"


def test_wires_only_when_both_conditions_hold(monkeypatch) -> None:
    """两个条件都满足才接（这里不验证取数，只验证**接线**这个动作）。"""
    monkeypatch.setattr(C, "INDICATORS", ("choice:test",))
    routes = C.build_choice_routes(probe_fn=_probe_ok, fetch_fn=lambda *a: [])
    assert len(routes) == 1
    connector, supports = routes[0]
    assert supports("choice:test") is True
    assert supports("stock:close:600036") is False


def test_wire_entry_point_is_the_only_one() -> None:
    """收尾判据：`build_runtime()` 里**只有** `build_choice_routes()` 这一处接线。

    防的是"有人图省事直接 `routes.append((ChoiceConnector(), ...))`" ——
    那会绕过闸门，而闸门正是这条纪律的全部。
    """
    import ast
    import inspect
    import textwrap

    from src.api import runtime

    tree = ast.parse(textwrap.dedent(inspect.getsource(runtime.build_runtime)))
    names = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "build_choice_routes" in names, (
        "`build_runtime()` 里没有调用 `build_choice_routes()` —— "
        "接线入口不在链上，那段代码就永远不会生效"
    )
    src = inspect.getsource(runtime.build_runtime)
    assert "ChoiceConnector(" not in src, (
        "`build_runtime()` 直接构造了 `ChoiceConnector` —— 绕过了闸门；"
        "必须走 `build_choice_routes()`"
    )


# ─────────────────── ③ 非 ok ⇒ 抛错，绝不返回空列表 ───────────────────


@pytest.mark.parametrize("state", NON_OK_STATES)
def test_fetch_raises_instead_of_returning_empty(state) -> None:
    """★ 五种非 `ok` 状态一律**抛错**，并且是 `DataFetchError`（链上认这个类型）。

    断言 `not []` 是重点：返回空列表在链上是一个**合法的正常结果**，
    会被当成"这个源没有这条数据"，然后静默继续 —— 那正是要防的静默失败。
    """
    conn = C.ChoiceConnector(probe_fn=lambda: _status(state),
                             fetch_fn=lambda *a: [_point("choice:test")])
    with pytest.raises(DataFetchError) as ei:
        asyncio.run(conn.fetch("choice:test"))
    msg = str(ei.value)
    assert state in msg, f"错误里必须带机器可读的状态码，实际：{msg}"
    assert "Choice" in msg


def test_no_access_error_tells_you_what_to_do() -> None:
    """`AGENTS.md`：**拒绝要给出路**。没权限时要指到"找客户经理开通"。"""
    conn = C.ChoiceConnector(probe_fn=lambda: _status(G.STATE_NO_ACCESS))
    with pytest.raises(DataFetchError) as ei:
        asyncio.run(conn.fetch("choice:test"))
    assert "客户经理" in str(ei.value), str(ei.value)


def test_wireable_but_unmeasured_still_refuses(monkeypatch) -> None:
    """闸门开了但口径没探明 ⇒ **仍然拒绝**，且错误要说清"差的是口径不是权限"。"""
    conn = C.ChoiceConnector(probe_fn=_probe_ok, fetch_fn=None)
    with pytest.raises(DataFetchError) as ei:
        asyncio.run(conn.fetch("choice:test"))
    assert "口径" in str(ei.value), str(ei.value)
    assert "没权限" not in str(ei.value) or "尚未探明" in str(ei.value)


# ─────────────────── ④ 「断开主用」演练（真路由器） ───────────────────


def test_drill_primary_down_fails_over_to_choice(monkeypatch) -> None:
    """★ R1 演练：主源断了 ⇒ **真的**切到 Choice，且数据带溯源。

    走**真的** `ConnectorRouter`（不是直接调连接器）：要验的正是
    "路由会切过去"这件事 —— 它由 `supports()` 命中与失败后重试共同决定。
    """
    from src.infrastructure.connectors.router import ConnectorRouter

    ind = "choice:test"
    monkeypatch.setattr(C, "INDICATORS", (ind,))

    def _fetch(indicator, start, end):  # noqa: ANN001
        #: **同步**函数：真 SDK（EmQuantAPI）是阻塞的 C 扩展，
        #: 连接器用 `asyncio.to_thread` 包它。第一版我在这里写成 `async def`，
        #: 于是演练报 `TypeError: 'coroutine' object is not iterable` ——
        #: 那正是"注入点的形状"没对齐，而不是连接器的问题。
        return [_point(indicator, 7.77)]

    routes = [(_PrimaryDown(), _PrimaryDown.supports)]
    routes += C.build_choice_routes(probe_fn=_probe_ok, fetch_fn=_fetch)
    assert len(routes) == 2, "演练前提：Choice 必须已被接上"

    router = ConnectorRouter(routes, disable_cache=True, disable_db=True)
    points = asyncio.run(router.fetch(ind))

    assert points, "主源断了、Choice 已接上，却一条都没拿到 ⇒ 不会兜底"
    assert points[0].value == 7.77
    assert C.ChoiceConnector.source_name in (points[0].source_name or ""), (
        f"溯源丢了：拿到 {points[0].source_name!r}，无法证明这条数是 Choice 供的"
    )


def test_drill_with_gate_closed_the_failure_is_actionable(monkeypatch, caplog) -> None:
    """★ 反向演练：闸门关着时，整条链必须说清"**没人支持这个指标**"，而不是空结果。

    这是本演练真正的价值：主源也不支持它、Choice 又没权限 ——
    此时若报出的是一个**空列表**（合法结果），上游会当成"这条数据本来就没有"，
    排查方向整个跑偏（本项目为此花过两轮）。所以：
      ① Choice **不进路由**；② 拒绝时**日志里留痕**（含机器可读的状态码）；
      ③ 取数时明确报"无连接器支持"。
    """
    from src.infrastructure.connectors.router import ConnectorRouter

    ind = "choice:test"
    monkeypatch.setattr(C, "INDICATORS", (ind,))
    routes = [(_PrimaryDown(), _PrimaryDown.supports)]
    with caplog.at_level("WARNING"):
        routes += C.build_choice_routes(probe_fn=lambda: _status(G.STATE_NO_ACCESS))

    assert len(routes) == 1, (
        "闸门关着时 Choice 不该进路由 ⇒ 路由表里应当只有主源"
    )
    assert any(G.STATE_NO_ACCESS in r.getMessage() for r in caplog.records), (
        "拒绝接线必须留一条带状态码的警告（否则它是静默的）"
    )

    router = ConnectorRouter(routes, disable_cache=True, disable_db=True)
    assert not router.supports(ind), (
        "只有 Choice 能供的指标，在闸门关着时不该被声称支持"
    )
    with pytest.raises(Exception) as ei:  # noqa: B017 路由器抛自己的 DataFetchError
        asyncio.run(router.fetch(ind))
    assert "无连接器支持" in str(ei.value), (
        f"应当明确报『没人支持』（而不是伪装成一次空结果）：{ei.value}"
    )
