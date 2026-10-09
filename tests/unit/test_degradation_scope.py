"""`degradation` 这个名字的**语义边界**判据（`CHG-0192` / 债 #19）。

## 这条判据防的是什么

仓库里有两个都被叫做"降级"、但**毫无调用关系**的东西：

| | 在哪 | 管什么 | 关键符号 |
|---|---|---|---|
| **模型降级链** | `gateway.py` 的 `complete()` | 主→备→本地地板 | `_routing[tier]` / `LLMResponse.provider_chain` / `fallback_used` |
| **缓存降级状态** | `degradation.py` | 语义缓存/精排器还健不健康 | `describe()` → `state ∈ {ok, degraded, no_traffic}` |

模块名 `degradation` 极易被读成前者。真实风险不是"名字难听"，而是
**有人按名字把两者当成一件事** —— 于是"合并重复实现"或"删掉没用的那个"，
删掉的是**另一条正在生效的护栏**。

## 判据强度

- `test_the_two_degradations_do_not_call_each_other` —— 结构判据：
  两个模块**互不 import**（一旦有人"统一"它们，这条红）。
- `test_fallback_chain_lives_in_gateway_and_still_exists` —— ★ 行为判据：
  降级链的**真实载体**仍在 `gateway`（`_routing` 驱动 `for` 循环 +
  `provider_chain` 落账），不是被挪走了。
- `test_module_docstring_states_the_boundary` —— 文档判据：本模块的 docstring
  必须显式点出"看降级到哪一跳请去 gateway"（避免下一个人再困惑）。
"""

from __future__ import annotations

import inspect

from src.infrastructure.llm import degradation as deg
from src.infrastructure.llm import gateway as gw


def test_the_two_degradations_do_not_call_each_other():
    """结构判据：两个模块**互不 import** —— 它们不是同一条链上的东西。"""
    deg_src = inspect.getsource(deg)
    assert "from src.infrastructure.llm.gateway" not in deg_src, (
        "degradation（缓存降级）import 了 gateway（模型降级链）⇒ "
        "两者被当成一件事了？先问清楚它们是否真的该合并"
    )


def test_fallback_chain_lives_in_gateway_and_still_exists():
    """★ 降级链的真实载体仍在 `gateway`：`_routing` 驱动的降级循环 + 链落账。"""
    gw_src = inspect.getsource(gw)
    assert "self._routing[task_tier]" in gw_src, (
        "找不到降级链的取链点（`chain = self._routing[task_tier]`）—— "
        "它被挪走或改名了吗？"
    )
    assert "provider_chain=" in gw_src, "降级链的结果没落账到 LLMResponse"
    assert "fallback_used" in gw_src


def test_cache_degradation_states_are_the_three_documented_ones():
    """缓存降级的三个状态是 `ok` / `degraded` / `no_traffic`（不是"第几跳"）。"""
    assert (deg._OK, deg._DEGRADED, deg._NO_TRAFFIC) == ("ok", "degraded", "no_traffic")
    assert callable(deg.describe)


def test_module_docstring_states_the_boundary():
    """文档判据：docstring 必须显式点出"看降级到哪一跳请去 gateway"。

    没有这句话时，下一个人（或下一个 AI）只会看到模块名叫 `degradation`，
    然后按名字推断它管模型降级 —— 这正是债 #19 记录的困惑。
    """
    doc = inspect.getdoc(deg) or ""
    assert "gateway.py" in doc, (
        "degradation 的 docstring 没有指向 gateway.py ⇒ 名字仍会误导读者"
    )
    assert "降级链" in doc and "缓存" in doc, (
        "docstring 必须把「模型降级链」与「缓存降级」两个概念并列讲清"
    )


def test_no_public_rename_happened_without_a_compat_alias():
    """如果哪天真的改名了，必须留兼容别名（否则外部 import 会断）。

    这条是"改名也要留痕"的哨兵：`describe` 是本模块唯一的公开出口。
    """
    import src.infrastructure.llm.degradation as mod

    assert hasattr(mod, "describe"), (
        "公开出口 describe 消失了 —— 改名请务必留兼容别名，"
        "否则 /health 与 metrics 的消费方会静默拿不到结论"
    )
