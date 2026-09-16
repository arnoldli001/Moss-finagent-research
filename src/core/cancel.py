"""任务取消令牌（上下文管理模式）。

设计参考moss-finance-assistant的Actor模式：
- 每个任务绑定一个CancellationToken，注入到LangGraph state
- 每个Agent节点入口检查cancellation，被取消时抛TaskCancelledError
- LLM Gateway在调用provider前检查，被取消时直接跳过API调用
- API层通过TaskStore管理handle.cancel()，配合cancellation token实现端到端终止

附带live_state引用：节点内可直接追加progress/agent_messages，
绕过LangGraph的state copy限制，前端轮询立即可见。
"""

from __future__ import annotations

import asyncio
from typing import Any


class TaskCancelledError(Exception):
    """任务被用户主动取消时抛出，区别于普通异常。"""


class CancellationToken:
    """轻量取消令牌：基于asyncio.Event，支持check/is_cancelled。

    注入到LangGraph state["cancellation"]中，各节点和LLM Gateway在
    关键检查点调用check()判断是否应中止。

    live_state: 节点内追加进度的共享通道（指向API层state引用）。
    """

    __slots__ = ("_event", "task_id", "reason", "live_state")

    def __init__(self, task_id: str = "") -> None:
        self._event = asyncio.Event()
        self.task_id = task_id
        self.reason = ""
        self.live_state: dict[str, Any] | None = None

    def cancel(self, reason: str = "user_requested") -> None:
        self.reason = reason
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """在关键检查点调用：被取消时抛TaskCancelledError。"""
        if self._event.is_set():
            raise TaskCancelledError(self.reason or "cancelled")

    def push_progress(self, msg: str) -> None:
        """节点内追加进度到live_state（前端轮询立即可见）。"""
        if self.live_state is not None:
            self.live_state.setdefault("progress", []).append(msg)

    def push_messages(self, msgs: list[dict[str, Any]]) -> None:
        """节点内追加Agent对话到live_state（前端轮询立即可见）。"""
        if self.live_state is not None and msgs:
            self.live_state.setdefault("agent_messages", []).extend(msgs)

    def to_state(self) -> dict[str, Any]:
        """序列化到LangGraph state（只存标记，Event不可序列化但进程内无影响）。"""
        return {"cancelled": self._event.is_set(), "reason": self.reason}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> CancellationToken | None:
        """从state中恢复（跨节点传递时取回）。"""
        raw = state.get("cancellation_token")
        if isinstance(raw, cls):
            return raw
        return None
