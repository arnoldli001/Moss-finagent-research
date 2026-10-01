"""A01 分析逻辑（纯函数）：推理步骤与结论摘要构造，不依赖外部服务。

日志文本也在这里生成（**格式只有一处**）：采集成功/缺口/本轮汇总都由
`format_*` 产出，`agent.py` 与 `supervisor.py` 只负责把它交给 `logger`。
"格式写两处"的后果是**日志没法 grep** —— 而用户要的正是"方便查看采集效果"。
"""

from __future__ import annotations

from collections.abc import Iterable

from src.core.schemas import DataPoint, TraceStep

#: 采集缺口的日志标签（**grep 用**：`grep '\[采集缺口\]' data/run/backend*.log`）。
GAP_TAG = "[采集缺口]"
#: 采集成功的日志标签。
OK_TAG = "[采集成功]"
#: 本轮采集汇总的日志标签。
SUMMARY_TAG = "[采集汇总]"


def build_reasoning_steps(
    indicator: str, count: int, data_refs: list[str], duration_ms: int
) -> list[TraceStep]:
    """构造采集步骤Trace。"""
    return [
        TraceStep(
            step=1,
            step_type="data_retrieval",
            description=f"从数据源获取 {indicator} 共 {count} 条数据",
            data_refs=data_refs,
            duration_ms=duration_ms,
        )
    ]


def build_collection_summary(indicator: str, points: list[DataPoint]) -> str:
    """生成采集结论摘要（含最新期间与来源，便于上层聚合）。"""
    if not points:
        return f"未获取到 {indicator} 数据"
    periods = [p.period_date for p in points if p.period_date]
    latest = max(periods) if periods else "未知期间"
    source = points[0].source_name or "未知来源"
    return f"成功采集 {indicator} 共 {len(points)} 条，最新期间 {latest}，来源 {source}"


def format_gap_log(
    indicator: str, reason: str, *, kind: str = "empty",
    seconds: float | None = None,
) -> str:
    """一条**采集缺口**的日志行（结构固定，便于 grep / 统计）。

    字段：`indicator` / `kind`（empty|error）/ `reason`（截断 200 字）/ `sec`。
    `kind=error` 表示取数抛异常；`kind=empty` 表示连接器们都没给出点
    （**"没量到"，不是"量到 0"** —— 两者在日志里也必须分得开）。
    """
    parts = [GAP_TAG, f"indicator={indicator}", f"kind={kind}"]
    text = " ".join(str(reason or "").split())
    parts.append(f"reason={text[:200] or '未给出原因'}")
    if seconds is not None:
        parts.append(f"sec={seconds:.1f}")
    return " ".join(parts)


def format_ok_log(
    indicator: str, points: list[DataPoint], *, seconds: float | None = None,
) -> str:
    """一条**采集成功**的日志行（含点数/最新期间/来源 —— 判断"采集效果"要用）。"""
    periods = [p.period_date for p in points if p.period_date]
    latest = max(periods) if periods else "?"
    source = points[0].source_name or "?"
    line = (f"{OK_TAG} indicator={indicator} points={len(points)} "
            f"latest={latest} source={source}")
    if seconds is not None:
        line += f" sec={seconds:.1f}"
    return line


def format_round_summary(
    task_id: str, planned: Iterable[str], ok: Iterable[str],
    empty: Iterable[str], failed: Iterable[str],
) -> str:
    """**本轮采集汇总**一行（用户要的"方便查看采集效果"）。

    例：`[采集汇总] task=task_x 计划=53 成功=45 空=6 失败=2 缺口=[…]`
    """
    planned_l = list(planned)
    ok_l, empty_l, failed_l = list(ok), list(empty), list(failed)
    gaps = list(dict.fromkeys(empty_l + failed_l))
    return (f"{SUMMARY_TAG} task={task_id} 计划={len(planned_l)} "
            f"成功={len(ok_l)} 空={len(empty_l)} 失败={len(failed_l)} "
            f"缺口={gaps if gaps else '无'}")
