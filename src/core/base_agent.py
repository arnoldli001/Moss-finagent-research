"""BaseAgent统一接口（agent-interface-spec.md）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.core.models import AgentInput, AgentOutput


class BaseAgent(ABC):
    """所有Agent的抽象基类。

    子类必须实现 execute / get_capabilities / health_check 三个方法；
    agent_id 遵循 {编号}_{角色} 命名，如 "A08_macro"。

    ## 生命周期（`CHG-0192`）

    构造 → execute（可多次）→ `close()`。**`close()` 不是抽象方法**，
    默认空实现：绝大多数 Agent 是无状态的，强制每个子类写一遍空的关闭逻辑
    只会制造噪音。但**必须存在这个钩子** —— 否则持有资源的 Agent
    （A19 自学习补充、将来的插件）在卸载时**没有入口释放**，
    表现为"进程退出前连接泄漏"，且没有任何报错。

    为什么是 `close()` 而不是 `aclose()`：与仓储层（`DataPointRepository.close()`）
    和 `Runtime.aclose()` 的探测顺序一致（先 `aclose` 再 `close`，见 `Runtime.aclose`），
    两个名字都被支持，子类按自己是否真的需要 await 选一个。
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    @abstractmethod
    async def execute(self, input: AgentInput) -> AgentOutput:
        """执行Agent任务，返回结构化输出。"""

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]:
        """返回Agent能力描述，用于Supervisor路由。"""

    @abstractmethod
    def health_check(self) -> bool:
        """健康检查。"""

    async def close(self) -> None:
        """释放该 Agent 持有的资源（默认无操作）。

        需要释放资源的子类**覆盖**它。调用方是 `Runtime.aclose()`，
        它逐条隔离异常 —— 所以这里抛异常只会记一条 warning，不会中断关停。
        """
        return None
