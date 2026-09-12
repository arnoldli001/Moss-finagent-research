"""异步任务存储（进程内TaskStore，Demo版单实例）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class TaskRecord(BaseModel):
    """一次投研任务的状态与产出。"""

    task_id: str
    trace_id: str
    tenant_id: str
    query: str
    analysis_type: str
    target: str
    status: str = "queued"  # queued/running/completed/failed
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    errors: list[str] = Field(default_factory=list)
    agent_outputs: list[dict[str, Any]] = Field(default_factory=list)
    final_report: str | None = None
    error: str | None = None


class TaskStore:
    """内存任务表 + asyncio任务句柄管理（lifespan关停时统一取消）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._handles: dict[str, asyncio.Task] = {}

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

    async def cancel_all(self) -> int:
        """lifespan关停：取消全部在飞任务，返回取消数量。"""
        running = [h for h in self._handles.values() if not h.done()]
        for handle in running:
            handle.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._handles.clear()
        return len(running)


def new_task_id() -> str:
    from uuid import uuid4

    return f"task_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid4().hex[:8]}"
