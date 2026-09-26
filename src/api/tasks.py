"""异步任务存储（进程内TaskStore，Demo版单实例）。

支持：
- 任务创建/查询/更新
- asyncio Task句柄管理（lifespan关停时统一取消）
- CancellationToken管理（用户主动停止时端到端终止任务链+LLM调用）
- running时实时读取state中的agent_messages（前端轮询展示协作过程）
- **容量/TTL 淘汰**（见下）

## 为什么必须有淘汰（2026-09-25 并发改造）

原实现的任务/令牌/句柄三个字典**只增不减**：``TaskRecord`` 里存着完整的
``agent_outputs`` 与 ``final_report`` 字符串，而 ``_tokens``/``_handles``
只在 lifespan 关停的 ``cancel_all()`` 里才 clear。实测 200 个任务跑完后：

    任务残留=200  live残留=0  token残留=200

即**每个请求留下三份常驻对象**。按每次分析产出几十 KB 报告估算，
1 万个任务就是几百 MB 常驻内存且进程永不释放 —— 长跑必然出问题。

现在：完成任务按「TTL（默认 1 小时）+ 容量（默认 200 条）」双阈值淘汰，
且 ``_tasks`` / ``_tokens`` / ``_handles`` **三者同步清理**。

⚠️ 淘汰只针对**已终结**状态（completed/failed/cancelled），
在飞任务永不淘汰；TTL 也刻意留得很长（默认 3600s），
避免前端还在轮询结果时任务已被清掉而拿到 404。
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from src.core.cancel import CancellationToken

#: 已完成任务保留条数上限（超出按最旧优先淘汰）
_MAX_TASKS = max(16, int(os.environ.get("MOSS_TASK_KEEP", "200")))
#: 已完成任务保留时长（秒）；留足前端轮询读取结果的时间
_TASK_TTL_S = max(60.0, float(os.environ.get("MOSS_TASK_TTL", "3600")))
#: 兜底：每次淘汰检查间隔（避免每个请求都全量扫一遍）
_EVICT_EVERY_N = 16

_TERMINAL = frozenset({"completed", "failed", "cancelled"})


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
    #: 终结时刻（monotonic 秒）；0 表示仍在飞。用于 TTL 淘汰。
    finished_at: float = 0.0
    #: 同问合流：本次请求是复用他人计算的（见 routes/research.py 的 in-flight 合流）
    deduplicated: bool = False


class TaskStore:
    """内存任务表 + asyncio任务句柄管理 + 取消令牌管理（含淘汰）。"""

    def __init__(self) -> None:
        #: OrderedDict 保证"最旧在前"，便于按容量淘汰
        self._tasks: OrderedDict[str, TaskRecord] = OrderedDict()
        self._handles: dict[str, asyncio.Task] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._live_state: dict[str, dict[str, Any]] = {}
        self._since_evict = 0

    # ---------- 淘汰 ----------

    def _drop(self, task_id: str) -> None:
        """三表同步删除：只清任务而留下令牌/句柄就是原来的泄漏。"""
        self._tasks.pop(task_id, None)
        self._tokens.pop(task_id, None)
        self._handles.pop(task_id, None)
        self._live_state.pop(task_id, None)

    def evict(self, *, force: bool = False) -> int:
        """淘汰已终结任务。返回清理条数。

        两道闸门：
        - **TTL**：终结超过 ``_TASK_TTL_S`` 的任务
        - **容量**：总任务数超 ``_MAX_TASKS`` 时，从最旧的已终结任务开始删
          （在飞任务永不被容量淘汰 —— 宁可短暂超限，也不能删掉正在跑的任务）
        """
        import time

        self._since_evict += 1
        if not force and self._since_evict < _EVICT_EVERY_N:
            return 0
        self._since_evict = 0

        now = time.monotonic()
        removed = 0
        for task_id, rec in list(self._tasks.items()):
            if rec.status in _TERMINAL and rec.finished_at > 0:
                if now - rec.finished_at > _TASK_TTL_S:
                    self._drop(task_id)
                    removed += 1

        if len(self._tasks) > _MAX_TASKS:
            for task_id in list(self._tasks.keys()):
                if len(self._tasks) <= _MAX_TASKS:
                    break
                rec = self._tasks.get(task_id)
                if rec is not None and rec.status in _TERMINAL:
                    self._drop(task_id)
                    removed += 1
        return removed

    def stats(self) -> dict[str, int]:
        """任务表规模（供容量评估与 /health 观测）。"""
        return {
            "tasks": len(self._tasks),
            "live_state": len(self._live_state),
            "tokens": len(self._tokens),
            "handles": len(self._handles),
            "max_tasks": _MAX_TASKS,
        }

    # ---------- 基础操作 ----------

    def create(self, task_id: str, **fields: Any) -> TaskRecord:
        self.evict()
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
        # 状态转入终结态时打上时刻戳，TTL 淘汰据此计时
        if record.status in _TERMINAL and record.finished_at <= 0:
            import time

            record.finished_at = time.monotonic()
        self._tasks.move_to_end(task_id)  # 活跃任务移到队尾，避免被容量淘汰误伤
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
