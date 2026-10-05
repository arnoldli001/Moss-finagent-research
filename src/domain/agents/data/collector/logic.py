"""A01 分析逻辑（纯函数）：推理步骤与结论摘要构造，不依赖外部服务。

日志文本也在这里生成（**格式只有一处**）：采集成功/缺口/本轮汇总都由
`format_*` 产出，`agent.py` 与 `supervisor.py` 只负责把它交给 `logger`。
"格式写两处"的后果是**日志没法 grep** —— 而用户要的正是"方便查看采集效果"。

## 「为什么没有数据」的机器可读结论（`gap_kind`）

采集侧以前只回答"有没有取到"，而**三种含义完全不同的情形**在审计面板上
长得一模一样（都是「无数据溯源引用」）：

| 情形 | `gap_kind` | 该怎么办 |
|---|---|---|
| 语义上不适用（银行没有"流动比率"） | `not_applicable` | 不用管，**别联网硬试**（烧钱） |
| 专题表未收录该主体（窗口内没有质押公告） | `not_covered` | 承认覆盖不到，**别去补** |
| 真的取数失败 | （无 `gap_kind`） | 去修取数链 |

判据（谁算这一档）**只有一处**：`classify_gap_kind()`。取值范围**不是本模块
定的**：`EXEMPT_GAP_KINDS` 就是 `NoApplicableData.KINDS` **本身**（同一对象、
不是副本），所以这里不可能与 `src/core/exceptions.py` 漂移。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Final

from src.core.exceptions import NoApplicableData
from src.core.schemas import DataPoint, TraceStep

logger = logging.getLogger(__name__)


def _bind_kinds_by_marker() -> tuple[str, str]:
    """把 `(not_applicable, not_covered)` 绑到**语义**上，而不是靠位置。

    `NoApplicableData.KINDS` 是唯一事实源，但"`KINDS[0]` 就是不适用"这句话
    只写在注释里 —— 谁调换一下顺序，`not_applicable` 与 `not_covered` 就会
    **静默互换**（两者处置一致，所以没人会立刻发现；而文案正好读反：
    "表里没有这家公司" 变成 "这个口径不存在"）。

    所以这里**当场验证**：拿每个 kind 构造一次异常，看它带的是哪一个标记，
    对不上就抛（加载期就红，不留到线上）。
    """
    by_kind = {kind: str(NoApplicableData("", kind=kind))
               for kind in NoApplicableData.KINDS}
    not_applicable = [k for k, text in by_kind.items()
                      if NoApplicableData.MARKER_NOT_APPLICABLE in text]
    not_covered = [k for k, text in by_kind.items()
                   if NoApplicableData.MARKER_NOT_COVERED in text]
    if len(not_applicable) != 1 or len(not_covered) != 1:
        raise RuntimeError(
            "NoApplicableData 的 kind/标记对应关系变了："
            f"不适用={not_applicable} 未收录={not_covered}（KINDS={NoApplicableData.KINDS}）")
    return not_applicable[0], not_covered[0]


#: 语义上不适用（银行没有"流动比率"）与专题表未收录该主体（这家公司在窗口内
#: 没有质押公告，≠ 质押 0%）—— 两个取值**语义绑定**在 `NoApplicableData` 上。
_BOUND_GAP_KINDS: Final[tuple[str, str]] = _bind_kinds_by_marker()

GAP_KIND_NOT_APPLICABLE: Final[str] = _BOUND_GAP_KINDS[0]
GAP_KIND_NOT_COVERED: Final[str] = _BOUND_GAP_KINDS[1]

#: 豁免类 `gap_kind` 的**全部**取值：**就是** `NoApplicableData.KINDS` 本身。
#:
#: ⚠️ 不写成 `tuple(...)` 副本、更不抄字面量：判据用 `is` 断言"同一个对象"，
#: 抄一份出来就当场变红（本项目最贵的那类缺陷就是"同一个 key 写在 3 处，
#: 只改一处 ⇒ 静默不一致"）。
EXEMPT_GAP_KINDS: Final[tuple[str, ...]] = NoApplicableData.KINDS


def gap_marker_tables() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(不适用标记, 未收录标记)` —— **现读**既有事实源，不另抄一份字面量。

    两处事实源缺一不可：

    * `NoApplicableData.MARKER_*`（`src/core/exceptions.py`）—— 结构化载体的标记；
    * `supervisor.NOT_APPLICABLE_MARKERS`（`src/orchestration/supervisor.py`）——
      **连接器侧**还有一族措辞（如 AkShare 的「该实体无值」：列在、这只票没值，
      语义正确而非缺陷）。少了它，最典型的那个现场（银行 / 流动比率）就认不出来。

    为什么用**延迟导入 + 读不到就退化**，而不是把字面量抄进 domain：
    抄一份必然漂移（改一边忘记另一边 ⇒ 分类静默失效）；而
    `supervisor` 是"标记的登记处"，延迟导入不构成模块加载期的环。读不到时
    只退化到 core 那一份（**宁可少豁免，也不许把真失败豁免掉**）。
    """
    not_applicable: tuple[str, ...] = (NoApplicableData.MARKER_NOT_APPLICABLE,)
    not_covered: tuple[str, ...] = (NoApplicableData.MARKER_NOT_COVERED,)
    try:
        from src.orchestration.supervisor import (
            NOT_APPLICABLE_MARKERS,
            NOT_COVERED_MARKERS,
        )

        not_applicable = tuple(dict.fromkeys(
            not_applicable + tuple(NOT_APPLICABLE_MARKERS)))
        not_covered = tuple(dict.fromkeys(not_covered + tuple(NOT_COVERED_MARKERS)))
    except Exception as exc:  # noqa: BLE001 事实源读不到 ⇒ 只用 core 那一份
        logger.debug("supervisor 的缺口标记读不到（只用 core 标记分类）: %s", exc)
    return not_applicable, not_covered


def classify_gap_kind(exc: object) -> str | None:
    """这次"没有数据"是哪一种？**返回 `None` 表示"没有豁免依据"**（真失败/未知）。

    ## 为什么先看类型、再看标记（顺序不是随便定的）

    `NoApplicableData` 是**结构化**的（取数侧自己写下的结论），优先信它；
    但异常经 `ConnectorRouter` 聚合后**类型会丢**（它按 `DataFetchError` 故障转移，
    最终抛一条把所有源串起来的新异常）—— 所以标记这条退路是**必需**的，
    这也正是 `NoApplicableData` 的 docstring 写着「标记跨层可读」的原因。

    ⚠️ 返回 `None` 而不是编一个值：拿不到依据时，审计侧必须**继续报缺陷**
    （把真失败豁免掉，比多报几条假阳性严重得多）。
    """
    kind = getattr(exc, "kind", None)
    if isinstance(exc, NoApplicableData) and kind in EXEMPT_GAP_KINDS:
        return str(kind)
    text = f"{exc}"
    not_applicable, not_covered = gap_marker_tables()
    if any(marker and marker in text for marker in not_covered):
        return GAP_KIND_NOT_COVERED
    if any(marker and marker in text for marker in not_applicable):
        return GAP_KIND_NOT_APPLICABLE
    return None


def deadline_cut(indicator: str, seconds: float | None, exc: object) -> bool:
    """这次取数**有没有证据**表明被防撞钟护栏拦下了（结构化优先）。

    两条证据：① 后端直接抛 `TimeoutError`；② 异常里带着**本次防撞钟自己的话术**
    （`intel_limits.deadline_reason` **现算**出来的那句话 —— 不是抄字面量，
    所以改文案时两边一起变，判据不会静默失效）。

    为什么需要②：`ConnectorRouter` 把防撞钟折算成 `DataFetchError`（把各源错误
    串成一条消息），所以①在真实链路上**拿不到**；而"被护栏拦下（=慢/活太重）"
    与"源上就没有（=真缺口）"的下一步动作完全不同，不能混成一句"没取到"。
    """
    if exc is None:
        return False
    if isinstance(exc, TimeoutError):
        return True
    if seconds is None:
        return False
    try:
        from src.core.intel_limits import deadline_reason

        needle = deadline_reason(str(indicator), float(seconds))
    except Exception:  # noqa: BLE001 话术取不到 ⇒ 当作"没有证据"
        return False
    return bool(needle) and needle in f"{exc}"


def gap_note(gap_kind: str | None) -> str:
    """豁免类缺口的一句**可读**说明（留痕用；`None` = 没有豁免依据）。"""
    if gap_kind == GAP_KIND_NOT_APPLICABLE:
        return NoApplicableData.MARKER_NOT_APPLICABLE
    if gap_kind == GAP_KIND_NOT_COVERED:
        return NoApplicableData.MARKER_NOT_COVERED
    return ""

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
    seconds: float | None = None, gap_kind: str | None = None,
    paths: Mapping[str, Any] | None = None,
) -> str:
    """一条**采集缺口**的日志行（结构固定，便于 grep / 统计）。

    字段：`indicator` / `kind`（empty|error）/ `reason`（截断 200 字）/ `sec`。
    `kind=error` 表示取数抛异常；`kind=empty` 表示连接器们都没给出点
    （**"没量到"，不是"量到 0"** —— 两者在日志里也必须分得开）。

    ★ 两个**可选**字段（不传时这一行与加它们之前**逐字一致**）：

    * `gap_kind=` —— "为什么没有"的机器可读结论（`not_applicable` /
      `not_covered`；**没有依据时这个字段整段不出现**，不许写一个 `unknown`
      去冒充结论）；
    * `paths_*=` —— 本次取数走了几条候选路径、命中哪一条、护栏有没有拦
      （渲染归 `path_stats.format_tokens`，格式只那一处）。

    ⚠️ 新字段一律**追加在行尾**：既有契约断言这一行以
    `[采集缺口] indicator=… kind=error ` **开头**（有一条判据守着），
    把字段插到前面会让所有按前缀 grep 的脚本失效。
    """
    parts = [GAP_TAG, f"indicator={indicator}", f"kind={kind}"]
    text = " ".join(str(reason or "").split())
    parts.append(f"reason={text[:200] or '未给出原因'}")
    if seconds is not None:
        parts.append(f"sec={seconds:.1f}")
    if gap_kind:
        parts.append(f"gap_kind={gap_kind}")
    if paths:
        try:
            from src.domain.agents.data.collector.path_stats import format_tokens

            tokens = format_tokens(paths)
        except Exception:  # noqa: BLE001 计数渲染坏了不该把日志（与主链路）带下去
            tokens = ""
        if tokens:
            parts.append(tokens)
    return " ".join(parts)


def format_ok_log(
    indicator: str, points: list[DataPoint], *, seconds: float | None = None,
    paths: Mapping[str, Any] | None = None,
) -> str:
    """一条**采集成功**的日志行（含点数/最新期间/来源 —— 判断"采集效果"要用）。

    ★ `paths`：可选，与 `format_gap_log` 的**同一个**入参（渲染也同一处：
    `path_stats.format_tokens`）。为什么成功行也要带它：`subpath=` 是"这一次
    走的是哪条近路"（TTL 缓存 / 本地库 / 真联网 / 被冷却或预算拒绝），
    而**正常返回**的次数远多于缺口 —— 只给缺口行带，采集侧的
    「本地命中率 / 联网触发率」就永远拼不齐分子分母。

    ⚠️ 新字段一律**追加在行尾**（同 `format_gap_log` 的纪律）：既有契约断言
    这一行以 `[采集成功] indicator=… points=…` **开头**、含 `latest=`/`source=`，
    把字段插到前面会让按前缀 grep 的脚本失效。
    """
    periods = [p.period_date for p in points if p.period_date]
    latest = max(periods) if periods else "?"
    source = points[0].source_name or "?"
    line = (f"{OK_TAG} indicator={indicator} points={len(points)} "
            f"latest={latest} source={source}")
    if seconds is not None:
        line += f" sec={seconds:.1f}"
    if paths:
        try:
            from src.domain.agents.data.collector.path_stats import format_tokens

            tokens = format_tokens(paths)
        except Exception:  # noqa: BLE001 计数渲染坏了不该把日志（与主链路）带下去
            tokens = ""
        if tokens:
            line += f" {tokens}"
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
