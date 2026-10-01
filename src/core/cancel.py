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


class TaskCancelledError(BaseException):
    """任务被用户主动取消时抛出，区别于普通异常。

    ## ★ 为什么继承 `BaseException` 而不是 `Exception`（`CHG-0132`）

    原来是 `Exception`。而这张图里**到处都是** `except Exception` 的兜底
    （`supervisor.py` 一个文件就有 18+ 处，注释写着"单节点失败不拖垮整图"）——
    于是只要 `token.check()` 落在某个 `try` 里，**"用户取消"就会被降级成
    "这个节点失败了"，图继续往下走**，取消变成"慢一点停"而不是"停"。

    实测（`CHG-0132` 现场）：照搬 `supervisor.py:2901-2906` 的形状，
    `token.check()` 被 `except Exception` 吞掉后，节点返回的是
    `{'errors': ['意外异常 user_requested']}` —— 一次取消被记成一次节点故障。

    Python 自己就是这个口径：`asyncio.CancelledError` 从 3.8 起**故意**改成
    `BaseException`，理由完全一样（`except Exception` 不该吞掉"取消"）。
    这里跟随它，**不是**为了少写代码，是为了让"取消"在所有兜底面前都拦不住。

    代价：任何 `except Exception` 都不再吞它（这正是目的）；
    需要收拾现场的地方用 `finally`（照常执行）。
    """


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
