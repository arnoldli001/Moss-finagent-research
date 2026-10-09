"""关停收口：`Runtime.aclose()` 必须覆盖 **Agent**，且逐条隔离（判据）。

## 这条判据防的是什么

关停清单原先手写在 `api/main.py` 的 lifespan 里，只有 4 个仓储：

```python
for label, closer in (
    ("事件告警仓储", ...), ("做T权重档案仓储", ...),
    ("资金流选择列表仓储", ...), ("主数据点仓储", ...),
): ...
```

而**真正持有资源的是 Agent** —— `A19_code_engineer`（自学习数据补充）会建连接、
`DataGapResolverAgent` 会持句柄、将来的插件更会。清单写在别处，
**新增 Agent 时没人会想起来改** ⇒ 表现为"进程退出前资源没释放"，且没有报错。

修法：`BaseAgent.close()`（默认空实现）+ `Runtime.aclose()` 逐条尽力关闭。

## 判据强度

- `test_agent_close_is_called_even_if_it_is_not_a_repository` —— **行为判据**：
  一个既不是仓储、也没有被旧清单点名的 Agent，它的 `close()` 必须被调用。
  把 `Runtime.aclose()` 里的 `self.agents` 那一段删掉 ⇒ 立刻红。
- 其余三条钉"逐条隔离 / 不适用≠失败 / exclude 生效"这三条纪律。
"""

from __future__ import annotations

from src.api.runtime import Runtime
from src.core.base_agent import BaseAgent
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence


class _Recorder:
    """记录自己有没有被关闭。"""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.closed = 0
        self.fail = fail

    async def close(self) -> None:
        self.closed += 1
        if self.fail:
            raise RuntimeError(f"{self.name} 关闭失败（故意的）")


class _ClosingAgent(BaseAgent):
    """一个**持有资源**的 Agent（旧清单完全覆盖不到它）。"""

    def __init__(self) -> None:
        super().__init__("A19_code_engineer")
        self.resource = _Recorder("agent-resource")

    async def execute(self, input: AgentInput) -> AgentOutput:  # pragma: no cover
        return AgentOutput(task_id=input.task_id, agent_id=self.agent_id,
                           conclusion="ok", confidence=Confidence.HIGH)

    def get_capabilities(self) -> dict:
        return {"name": "closing"}

    def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        await self.resource.close()


class _NoCloseAgent(BaseAgent):
    """不覆盖 `close()` 的 Agent —— 走 `BaseAgent` 的默认空实现。"""

    async def execute(self, input: AgentInput) -> AgentOutput:  # pragma: no cover
        return AgentOutput(task_id=input.task_id, agent_id=self.agent_id,
                           conclusion="ok", confidence=Confidence.HIGH)

    def get_capabilities(self) -> dict:
        return {"name": "plain"}

    def health_check(self) -> bool:
        return True


def _runtime(agents: dict, repo=None, **kw) -> Runtime:
    return Runtime(gateway=None, repo=repo, agents=agents, graph=None,
                   backend=None, **kw)


def test_base_agent_has_a_close_hook_with_default_noop():
    """`close()` 必须是**非抽象**的默认空实现（否则每个子类都要写一遍空的）。"""
    assert "close" in dir(BaseAgent)
    assert not getattr(BaseAgent.close, "__isabstractmethod__", False), (
        "close 若变成抽象方法，19 个 Agent 全要改 —— 那是设计错误"
    )
    # 不覆盖 close 的子类必须能正常构造（说明它不是抽象方法）
    assert _NoCloseAgent("A00_plain").agent_id == "A00_plain"


async def test_agent_close_is_called_even_if_it_is_not_a_repository():
    """★ 核心判据：Agent 的 `close()` 必须被调 —— 旧清单覆盖不到它。

    删掉 `Runtime.aclose()` 里 `self.agents` 那一段 ⇒ 本用例红。
    """
    agent = _ClosingAgent()
    rt = _runtime({"A19_code_engineer": agent})
    failed = await rt.aclose()
    assert agent.resource.closed == 1, "Agent 的资源没被释放（旧清单覆盖不到）"
    assert failed == []


async def test_no_close_attribute_is_not_a_failure():
    """没有 `close()` 的对象**静默跳过** —— 那是"不适用"，不是"失败"。"""
    rt = _runtime({"A00_plain": _NoCloseAgent("A00_plain")}, repo=object())
    assert await rt.aclose() == []


async def test_one_failure_does_not_stop_the_rest():
    """★ 逐条隔离：前面失败，后面的对象**仍然要关**。

    这是 lifespan 里那场实测事故的判据化（一句 AttributeError 让整个关停段中断，
    导致 WAL checkpoint 全没执行）。`repo` 排在最后，所以它必须仍被关到。
    """
    bad = _Recorder("bad", fail=True)
    good_agent = _ClosingAgent()
    repo = _Recorder("repo")
    rt = _runtime({"A19_code_engineer": good_agent}, repo=repo,
                  fundflow_repo=bad)
    failed = await rt.aclose()
    assert failed == ["fundflow_repo"], f"失败清单不对：{failed}"
    assert repo.closed == 1, "前一个失败把后面的关停拖累了（正是那场事故）"
    assert good_agent.resource.closed == 1


async def test_exclude_skips_named_targets():
    """`exclude` 生效：lifespan 用它在关停顺序不变的前提下跳过做T。"""
    intraday = _Recorder("intraday")
    repo = _Recorder("repo")
    rt = _runtime({}, repo=repo, intraday=intraday)
    await rt.aclose(exclude=("intraday",))
    assert intraday.closed == 0, "exclude 没生效 ⇒ 会被关两次（幂等性未验证）"
    assert repo.closed == 1


async def test_aclose_prefers_aclose_over_close():
    """两个名字都在时优先 `aclose`（与仓储层的既有约定一致）。"""
    class Both:
        def __init__(self) -> None:
            self.a = 0
            self.s = 0

        async def aclose(self) -> None:
            self.a += 1

        async def close(self) -> None:  # pragma: no cover 不该被调到
            self.s += 1

    obj = Both()
    rt = _runtime({}, repo=obj)
    assert await rt.aclose() == []
    assert (obj.a, obj.s) == (1, 0)
