"""异步任务存储（进程内TaskStore，Demo版单实例）。

支持：
- 任务创建/查询/更新
- asyncio Task句柄管理（lifespan关停时统一取消）
- CancellationToken管理（用户主动停止时端到端终止任务链+LLM调用）
- running时实时读取state中的agent_messages（前端轮询展示协作过程）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from src.core.cancel import CancellationToken


class TaskRecord(BaseModel):
    """一次投研任务的状态与产出。"""

    task_id: str
    trace_id: str
    tenant_id: str
    query: str
    analysis_type: str
    target: str
    status: str = "queued"  # queued/running/completed/failed/cancelled
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    errors: list[str] = Field(default_factory=list)
    agent_outputs: list[dict[str, Any]] = Field(default_factory=list)
    agent_messages: list[dict[str, Any]] = Field(default_factory=list)
    final_report: str | None = None
    error: str | None = None
    # 实时进度（running时前端轮询展示）
    progress: str = ""


class TaskStore:
    """内存任务表 + asyncio任务句柄管理 + 取消令牌管理。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._handles: dict[str, asyncio.Task] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._live_state: dict[str, dict[str, Any]] = {}

    def create(self, task_id: str, **fields: Any) -> TaskRecord:
        record = TaskRecord(task_id=task_id, **fields)
        self._tasks[task_id] = record
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def update(self, task_id: str, **fields: Any) -> TaskRecord | None:
        record = self._tasks.get(task_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        return record

    def register_handle(self, task_id: str, handle: asyncio.Task) -> None:
        self._handles[task_id] = handle

    def register_token(self, task_id: str, token: CancellationToken) -> None:
        self._tokens[task_id] = token

    def get_token(self, task_id: str) -> CancellationToken | None:
        return self._tokens.get(task_id)

    def set_live_state(self, task_id: str, state: dict[str, Any]) -> None:
        """running时暴露state引用，前端可轮询agent_messages。"""
        self._live_state[task_id] = state

    def get_live_state(self, task_id: str) -> dict[str, Any] | None:
        return self._live_state.get(task_id)

    def clear_live_state(self, task_id: str) -> None:
        self._live_state.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        """用户主动取消任务：触发CancellationToken + 取消asyncio Task。"""
        token = self._tokens.get(task_id)
        if token:
            token.cancel("user_requested")
        handle = self._handles.get(task_id)
        if handle and not handle.done():
            handle.cancel()
            try:
                await asyncio.wait_for(handle, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass  # 任务已终止或超时
        self.update(task_id, status="cancelled", error="用户主动停止任务")
        self.clear_live_state(task_id)
        return True

    async def cancel_all(self) -> int:
        """lifespan关停：取消全部在飞任务，返回取消数量。"""
        running = [h for h in self._handles.values() if not h.done()]
        for handle in running:
            handle.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._handles.clear()
        self._tokens.clear()
        self._live_state.clear()
        return len(running)


def new_task_id() -> str:
    from uuid import uuid4

    return f"task_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid4().hex[:8]}"
