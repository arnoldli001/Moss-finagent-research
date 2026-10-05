"""A01 采集侧的**缺口台账**（进程内、有界）—— 让"豁免"留痕而不是消失。

## 为什么必须存在（否则本轮的三态分流在真实链路上是死代码）

三态分流要回答的是「这条**没有数据**，是豁免还是缺陷」。判据（`gap_kind`）
在采集侧已经算得出来（`logic.classify_gap_kind`），但**载体少了一条**：

    豁免类缺口是**抛异常**传达的（`NoApplicableData`）—— A01 抛出后
    由 supervisor 分流成"非缺陷、不联网硬试"，**不会**产出 `AgentOutput`；
    而 A18 审计读的恰恰是 `agent_outputs`（`_summary(output)` 才有 `result`）。

于是：**豁免的那两条在审计的输入里根本不存在**，审计既不会误报、也**看不到**
"有几条被豁免了"。只把 `gap_kind` 放进 `AgentOutput.result` 的话，
那份代码要等上游（`supervisor._live_fetch_one`）改成"失败也留一条产出"才会被走到
—— 那正是本项目反复吃亏的形状：「判据接在没人走的路上 = 没接」。

所以本模块补上**另一条载本**：A01 在**得出结论的那一刻**把缺口记进来（纯内存），
A18 按 `task_id` 读出来，放进自己的返回值（`exemptions` / `defects`）。
两条载本（产出里的 `gap_kind` + 这里的台账）在审计侧**按指标合并去重**，
所以上游以后真加了产出也不会重复计数。

## 三条纪律

1. **纯内存**：不写盘、不发网络请求 —— 采集是主链路，观测不许给它加往返。
2. **有界**：按 task 与总条数双重上限（长跑进程不许被它撑大），
   被挤掉的条数记在 `snapshot()["dropped"]` 里（**不许静默丢** ——
   丢了却不说，台账就会以"少了但看起来正常"的方式骗人）。
3. **绝不抛**：`record()` 内部吞掉一切异常（观测坏掉不该影响采集），
   读侧 `exemptions()` 同样返回空列表而不是炸。

## 为什么按 `task_id` 而不按指标全局记

一次任务里的豁免只对**这次审计**有意义（客户看的是这份报告）；
按指标全局记会把上一轮的结论混进这一轮 —— 而"上一轮不适用、这一轮取到了"
是完全可能的（专题表更新了）。键取 `(task_id, indicator)`，同一对重复记
只保留**最后一次**（重试路径会把同一条指标取两次）。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 单个 task 最多留多少条缺口记录（超了丢最旧的）。
MAX_PER_TASK: Final[int] = 200

#: 全部 task 合计的上限（长跑进程里 task 只增不减，必须有个总闸）。
MAX_TASKS: Final[int] = 500


@dataclass
class GapRecord:
    """一条采集缺口（豁免或缺陷）—— 审计侧逐条可见、可定位。"""

    task_id: str
    indicator: str
    #: `None` = 没有豁免依据（真失败/未知）⇒ 审计侧仍按缺陷处理。
    gap_kind: str | None = None
    reason: str = ""
    #: 记这条的 Agent（审计侧据此拼出**可定位**的 `A01_data_collector[指标名]`，
    #: 而不是让审计自己写死一个 agent_id —— 写死就是第四份字面量）。
    source: str = ""
    #: 本次取数的路径计数报告（`path_stats.record()` 的返回值）。
    path_stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "indicator": self.indicator,
            "gap_kind": self.gap_kind,
            "reason": self.reason,
            "source": self.source,
            "path_stats": dict(self.path_stats),
        }


#: 进程级状态（单一事实源）。`task_id -> {indicator: GapRecord}`（dict 保序 ⇒ 天然"丢最旧"）。
_BY_TASK: dict[str, dict[str, GapRecord]] = {}
_LOCK: Final[threading.Lock] = threading.Lock()
_DROPPED: list[int] = [0]        # 用 list 装一个可变的 int（闭包/模块级都可写）


def record(
    *, task_id: str, indicator: str, gap_kind: str | None,
    reason: Any = "", path_stats: dict[str, Any] | None = None,
    source: str = "",
) -> None:
    """记一条缺口。**绝不抛**（观测失败不影响采集）。

    `task_id` 或 `indicator` 为空时**不记**：没有键的记录在审计侧无法归属到
    任何一次任务 —— 留着只会变成"看得见但认不出是谁"的噪音。
    """
    try:
        tid = str(task_id or "").strip()
        ind = str(indicator or "").strip()
        if not tid or not ind:
            return
        record_obj = GapRecord(
            task_id=tid, indicator=ind,
            gap_kind=(str(gap_kind) if gap_kind else None),
            reason=" ".join(str(reason or "").split())[:300],
            source=str(source or ""),
            path_stats=dict(path_stats or {}),
        )
        with _LOCK:
            bucket = _BY_TASK.setdefault(tid, {})
            bucket.pop(ind, None)          # 重试/重复取数 ⇒ 只留最后一次
            bucket[ind] = record_obj
            while len(bucket) > MAX_PER_TASK:
                bucket.pop(next(iter(bucket)))
                _DROPPED[0] += 1
            while len(_BY_TASK) > MAX_TASKS:
                _BY_TASK.pop(next(iter(_BY_TASK)))
                _DROPPED[0] += 1
    except Exception as exc:  # noqa: BLE001 观测失败绝不影响采集
        logger.debug("缺口台账记录失败（忽略）: %s", exc)


def _read(task_id: str) -> list[GapRecord]:
    """取某次任务的全部缺口记录（**副本**，调用方改它不影响台账）。"""
    try:
        tid = str(task_id or "").strip()
        if not tid:
            return []
        with _LOCK:
            bucket = _BY_TASK.get(tid) or {}
            return [GapRecord(**r.to_dict()) for r in bucket.values()]
    except Exception as exc:  # noqa: BLE001 读侧同样不抛
        logger.debug("缺口台账读取失败（按空处理）: %s", exc)
        return []


def exemptions(task_id: str, *, exempt_kinds: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """某次任务里**被豁免**的缺口（`gap_kind` 落在 `exempt_kinds` 里）。

    ⚠️ `exempt_kinds` 由**调用方**传入（审计侧传 `EXEMPT_GAP_KINDS`）——
    本模块**不自己定义**哪些 kind 算豁免：那是 `NoApplicableData.KINDS` 的职责，
    在这里再写一份就等于给同一件事留了第二个事实源。
    """
    allowed = {str(k) for k in exempt_kinds}
    return [r.to_dict() for r in _read(task_id)
            if r.gap_kind and r.gap_kind in allowed]


def defects(task_id: str, *, exempt_kinds: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """某次任务里**不豁免**的缺口（真失败/没有依据）—— 计数用，不是判据本体。"""
    allowed = {str(k) for k in exempt_kinds}
    return [r.to_dict() for r in _read(task_id)
            if not (r.gap_kind and r.gap_kind in allowed)]


def snapshot() -> dict[str, Any]:
    """只读快照：任务数 / 条数 / **被挤掉的条数**（有界不许静默）。"""
    try:
        with _LOCK:
            per_task = {tid: len(items) for tid, items in _BY_TASK.items()}
            return {
                "tasks": len(_BY_TASK),
                "records": sum(per_task.values()),
                "dropped": int(_DROPPED[0]),
                "max_per_task": MAX_PER_TASK,
                "max_tasks": MAX_TASKS,
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("缺口台账快照失败: %s", exc)
        return {"tasks": 0, "records": 0, "dropped": 0,
                "max_per_task": MAX_PER_TASK, "max_tasks": MAX_TASKS}


def reset_for_test() -> None:
    """清空台账（**只给测试用**；生产代码里不许出现调用）。"""
    with _LOCK:
        _BY_TASK.clear()
        _DROPPED[0] = 0


__all__ = [
    "MAX_PER_TASK",
    "MAX_TASKS",
    "GapRecord",
    "defects",
    "exemptions",
    "record",
    "reset_for_test",
    "snapshot",
]
