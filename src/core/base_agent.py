"""BaseAgent统一接口（agent-interface-spec.md）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.core.models import AgentInput, AgentOutput


class BaseAgent(ABC):
    """所有Agent的抽象基类。

    子类必须实现 execute / get_capabilities / health_check 三个方法；
    agent_id 遵循 {编号}_{角色} 命名，如 "A08_macro"。
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
