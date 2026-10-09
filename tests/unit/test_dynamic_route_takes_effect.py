"""动态连接器「最后一公里」判据：**加载 ≠ 生效**。

## 这条判据防的是什么（`CHG-0192` 修的真实缺陷）

A19 生成连接器后的链路是：

```
写文件 → DynamicConnectorLoader.reload() → ??? → A19 结论说"已热加载到数据路由"
```

`reload()` 只是**返回了一个新列表**，而活 `ConnectorRouter` 的 `self._routes`
在 `__init__` 里赋值一次、类内**没有任何增补方法**。于是：

- 文件在盘上 ✓、加载器认它 ✓、A19 的 `_test_fetch` 直接用手上的 connector 也能取到数 ✓；
- 但**这个进程的取数链永远路由不到它** ✗ —— 而结论文案写着"下次采集即可使用"。

**危害的形状**：外表全绿（数据取到了、结论说得很好听），
而真正的消费路径（`ConnectorRouter.fetch`）根本不认识它。要重启才生效。

## 判据强度

- `test_match_and_fetch_see_the_new_connector` —— **行为判据**：直接调
  `router.supports()` 与 `router.fetch()`，断言新连接器**被路由到**。
  把 `refresh_routes` 从 `_attach_to_live_router` 里删掉 ⇒ 立刻红。
- `test_no_router_handle_reports_not_attached` —— 没有 router 句柄时**必须如实说没生效**
  （禁止复用"已热加载"的话术）。
- `test_refresh_is_idempotent_and_appends` —— 顺序即优先级，且同名不重复挂。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.infrastructure.connectors.router import ConnectorRouter


class _FakeConnector:
    """最小可用连接器：认一个指标，返回一条数据点。

    ⚠️ 两个坑（第一版替身都踩了）：

    1. **不能用 `@staticmethod supports`** —— 动态加载器挂的是**实例的**
       `connector.supports`；`staticmethod` 恒返回同一个值，于是
       "挂上去了但 supports 看不见"（那是**替身的错**，不是被测代码的错）。
       这里把 `supports` 设成**实例属性**（闭包），与真实连接器的调用形式一致。
    2. **必须有 `source_name`** —— `router._fetch_uncached` 用它做失败冷却的键
       （`fkey = f"{connector.source_name}:{indicator}"`），真实连接器都有。
    """

    source_url = "https://example.invalid"

    def __init__(self, indicator: str, *, source_name: str | None = None) -> None:
        self._indicator = indicator
        self.source_name = source_name or "dyn_test_source"
        #: 与 `BaseConnector` 的实例方法同形（调用方写 `connector.supports`）
        self.supports = lambda ind: ind == indicator

    def get_capabilities(self) -> dict[str, Any]:
        return {"indicators": [self._indicator]}

    async def fetch(self, indicator: str, start_date=None, end_date=None):
        return [f"{indicator}@dyn"]


def _route(connector: _FakeConnector):
    return (connector, connector.supports)


class _ExistingConnector(_FakeConnector):
    def __init__(self, indicator: str) -> None:
        super().__init__(indicator, source_name="existing_source")


def _router(*routes) -> ConnectorRouter:
    """不接 repo、不接缓存：判据只关心"路由能不能看见它"。"""
    return ConnectorRouter(list(routes), disable_cache=True, disable_db=True)


def test_match_and_fetch_see_the_new_connector():
    """★ 核心判据：新挂上去的连接器必须被 `supports` 与 `fetch` **看见**。

    删掉 `_attach_to_live_router` 里的 `refresh_routes` 调用 ⇒ 本用例红。
    """
    ind = "ind:dyn_demo"
    router = _router()
    assert router.supports(ind) is False, "前提：初始路由表里没有它"

    added = router.refresh_routes([_route(_FakeConnector(ind))])
    assert added == 1
    assert router.supports(ind) is True, (
        "★ 挂上去了但 supports 仍看不见 ⇒ 又是「加载了但没路由」"
    )
    # 真正走一次取数链（不只是问"认不认"）
    points = asyncio.run(router.fetch(ind))
    assert points, "★ 路由看得见但 fetch 取不到 ⇒ 最后一公里没接上"


def test_no_router_handle_reports_not_attached():
    """★ 没有 router 句柄时必须**如实说没生效**，不许复用"已热加载"话术。"""
    from src.domain.agents.engineering.code_engineer.agent import CodeEngineerAgent

    agent = CodeEngineerAgent(gateway=None)          # 不注入 router
    connector = _FakeConnector("ind:x")
    assert agent._attach_to_live_router([], connector) is False, (
        "没有活路由表却报「已挂上」⇒ 声称与行为不一致"
    )


def test_with_router_handle_reports_attached():
    """有句柄时返回 True，且**真的挂上了**（与上一条构成对偶）。"""
    from src.domain.agents.engineering.code_engineer.agent import CodeEngineerAgent

    ind = "ind:y"
    router = _router()
    agent = CodeEngineerAgent(gateway=None, router=router)
    connector = _FakeConnector(ind)
    assert agent._attach_to_live_router([], connector) is True
    assert router.supports(ind) is True, "报了已挂上但路由看不见 ⇒ 谎报"


def test_refresh_appends_so_existing_priority_is_unchanged():
    """★ 新源挂**链尾**：存量指标的取数路径不许被改掉。"""
    old_ind, new_ind = "ind:old", "ind:new"
    old = _ExistingConnector(old_ind)

    router = _router(_route(old))
    router.refresh_routes([_route(_FakeConnector(new_ind))])

    # 老指标仍由老源命中（顺序即优先级，新源在链尾）
    assert router._resolve(old_ind) is old
    assert router._resolve(new_ind).source_name == "dyn_test_source"


def test_refresh_is_idempotent_by_source_name():
    """同名重挂不重复（自修复可能重跑）；返回值为**新增**条数。"""
    ind = "ind:z"
    router = _router()
    assert router.refresh_routes([_route(_FakeConnector(ind))]) == 1
    again = router.refresh_routes([_route(_FakeConnector(ind))])
    assert again == 0, "同名连接器被重复挂上 ⇒ 链上出现两份同名源"


def test_replace_mode_prunes_tombstones():
    """整体替换时要清掉**已不在链上**的失败冷却/陈旧记忆（否则成墓碑）。"""
    ind = "ind:t"
    router = _router(_route(_FakeConnector(ind)))
    router._failure_cache["dyn_test_source|" + ind] = (0.0, "boom")
    router._stale_floor["dyn_test_source"] = (0.0, [], "2000-01-01")

    router.refresh_routes([_route(_ExistingConnector(ind))], keep_existing=False)
    assert not router._failure_cache, "替换后仍留着已移除源的冷却（墓碑）"
    assert not router._stale_floor, "替换后仍留着已移除源的陈旧记忆（墓碑）"


def test_runtime_wires_the_router_into_a19():
    """★ 装配期必须真的把活路由表交给 A19（否则上面那条 `is False` 分支就是常态）。

    强度边界：这里读 `build_runtime` 的源码形态（装配是过程式的，
    没有可单独调用的出口）。真正的行为由前几条判据覆盖。
    """
    import inspect

    from src.api import runtime as rt

    src = inspect.getsource(rt.build_runtime)
    assert "CodeEngineerAgent(gateway, router=backend)" in src, (
        "A19 没有拿到活路由表 ⇒ 生成的连接器永远不会被路由到"
    )


def test_refresh_survives_a_broken_route() -> None:
    """挂路由失败不能把整条自修复搞崩（文件已落盘，只是没生效）。"""
    class _Broken:
        source_name = "broken_source"

        def get_capabilities(self) -> dict[str, Any]:  # pragma: no cover
            return {}

    from src.domain.agents.engineering.code_engineer.agent import CodeEngineerAgent

    router = _router()
    agent = CodeEngineerAgent(gateway=None, router=router)
    # `_Broken` 没有 supports ⇒ 属性访问抛错 ⇒ 必须被吞掉并返回 False
    assert agent._attach_to_live_router([], _Broken()) is False
