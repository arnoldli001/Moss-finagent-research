"""数据采集异常记录：**给管理员看，不给用户看**。

## 触发它的用户口径（2026-09-30）

> 「前端显示"部分节点异常、查询超过 10 秒防撞钟"，这类信息异常信息，
>  **不要显示在用户界面**。要记录并显示到管理员界面的"运行指标"里，
>  可以加一块**数据采集异常展示区**，方便我后续维护。」

## 设计要点

* **落盘**：`<登记的 run_dir>/collection_anomalies.jsonl`（复用既有 `run_dir`
  存储，不新登记路径 —— `test_no_store_path_literals_in_src` 会拦住源码里的路径字面量）；
* **有界**：文件超过 `MAX_LINES` 行就重写成"最近 `KEEP_LINES` 行" ⇒
  长期运行不会无限长大（本项目纪律：**上限必须写进代码**）；
* **去重**：同一 `(kind, indicator, reason[:60])` 在 `DEDUP_WINDOW_S` 内只记一次 ——
  一条坏源在重试循环里会刷屏，不去重等于把"可维护的信号"淹掉；
* **绝不影响主链路**：任何异常都被吞掉（这是**观测**，不是功能）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 落盘文件名（放在**登记的** `run_dir` 下，与维护报告同目录）。
FILE_NAME = "collection_anomalies.jsonl"

#: 文件行数上限；超过就裁剪到 `KEEP_LINES`（有界，长跑不涨）。
MAX_LINES = 4000
KEEP_LINES = 2000

#: 同一异常的静默窗口（秒）：窗口内重复出现只记一次。
DEDUP_WINDOW_S = 1800.0

#: 异常种类（**枚举，不是自由文本** —— 判据只认机器可读的标识）。
KIND_GAP = "gap"            # 采集缺口（源为空/无连接器）
KIND_TIMEOUT = "timeout"    # 触发防撞钟（交互路径超预算）
KIND_ERROR = "error"        # 取数异常
KIND_NOT_APPLICABLE = "not_applicable"   # 该口径不适用（**不是故障**）
#: ★ 外部搜索源的**运维状态**（`CHG-0120`）：额度用尽 / 未配凭据 / 配置读失败 /
#: 被服务端拒 / 连不上 / 返回体坏了。这些**不是"某个指标取不到"**，
#: 而是"**找数据源这条路本身出了问题**" —— 混进 `gap` 会让排查方向跑偏。
KIND_SEARCH_SOURCE = "search_source"
#: ★ 换源线索（`CHG-0120`）：某个指标没有连接器支持，联网搜到了候选源网址。
#: **它不是故障**，是"待人工复核的候选" —— 所以单独一类，别与 gap 混。
KIND_SOURCE_LEAD = "source_lead"
#: ★ 作业**墙钟预算被截断**（`CHG-0137`）：一轮作业没跑完就到了预算上限。
#: **不是故障**（源慢/源挂才是），但它是"作业与在线服务争资源"的**直接证据**，
#: 且在实测里正是"前端显示后端不可达"的根因（日K预热 316s > 90s 预算 > 120s cron）。
KIND_JOB_BUDGET = "job_budget"
#: ★ API **事件循环被阻塞**（`CHG-0137`）：1 Hz 采样到的调度延迟超过阈值。
#: 这一条是"服务不可达"的**唯一直接仪器** —— 在此之前我们只能靠请求延迟反推。
KIND_LOOP_LAG = "loop_lag"
#: ★ **跨通道一致性**（`CHG-0157`）：本地库与在线源两条通道都拿得到这个指标时，
#: 出现的"本地陈旧 / 同期次数值不一致 / 在线不可得"三种情形。
#: 为什么必须单独一类：`hop_stats` 只回答"哪一跳答出来的"，
#: **"命中的是旧数据"在命中率里看不见** —— 本地命中率很高可能正是问题本身。
KIND_CROSS_CHANNEL = "cross_channel"

KINDS = (KIND_GAP, KIND_TIMEOUT, KIND_ERROR, KIND_NOT_APPLICABLE,
         KIND_SEARCH_SOURCE, KIND_SOURCE_LEAD, KIND_JOB_BUDGET, KIND_LOOP_LAG,
         KIND_CROSS_CHANNEL)

#: 是否记录 `not_applicable`：它**不是故障**，默认不记（记了会让"异常区"变噪音）。
RECORD_NOT_APPLICABLE = False


@dataclass
class Anomaly:
    """一条采集异常。"""

    ts: float
    kind: str
    indicator: str
    reason: str
    task_id: str = ""
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _path(root: Path | None = None) -> Path:
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    base = Path(root) if root is not None else PROJECT_ROOT
    return base / store_rel("run_dir") / FILE_NAME


#: 进程内去重表：`(kind, indicator, reason[:60]) -> 上次记录时刻`
_SEEN: dict[tuple[str, str, str], float] = {}

#: 已经就"未知 kind"告警过的值（**每个错值只报一次**，避免刷日志）。
#: 为什么需要它：见 `record()` 里那段说明 —— 未知 kind 会被**静默改写成 gap**。
_WARNED_UNKNOWN_KINDS: set[str] = set()


def _dedup_hit(kind: str, indicator: str, reason: str, now: float) -> bool:
    key = (kind, indicator, reason[:60])
    last = _SEEN.get(key)
    if last is not None and (now - last) < DEDUP_WINDOW_S:
        return True
    _SEEN[key] = now
    # 表本身也要有界：超过 2000 条键就清掉最旧的一半
    if len(_SEEN) > 2000:
        for k, _ in sorted(_SEEN.items(), key=lambda kv: kv[1])[:1000]:
            _SEEN.pop(k, None)
    return False


def record(kind: str, indicator: str, reason: str, *, task_id: str = "",
           source: str = "", extra: dict[str, Any] | None = None,
           root: Path | None = None) -> bool:
    """记一条采集异常。返回 True = 真的写进去了（False = 去重/被忽略/失败）。

    ⚠️ **本函数绝不抛异常**：观测坏掉不该影响采集（调用点都在主链路上）。
    """
    try:
        if kind not in KINDS:
            #: ★★ 未知 kind 会被**静默改写成 `gap`** —— 这个兜底本身保住了
            #: "异常区不会因为一个错值就崩"，但它有一个**很深的副作用**：
            #: **拼错一个字母与"真的采集缺口"在界面上长得一模一样**
            #: （本项目同类先例：`MOSS_SCHEDULER_DENY` 拼错作业名 ⇒
            #: "写错一个字母"与"本来就不需要禁"完全不可区分）。
            #: 所以这里**保留兜底，但必须吭声**，且每个错值只吭一次。
            if kind not in _WARNED_UNKNOWN_KINDS:
                _WARNED_UNKNOWN_KINDS.add(str(kind))
                logger.warning(
                    "采集异常记了未知 kind=%r（不在 %s 里）⇒ **已按 %r 记录**。"
                    "界面会把它显示成『采集缺口』，而它其实是别的东西 —— "
                    "补进 `KINDS` 或在调用点改用已登记的 kind。",
                    kind, list(KINDS), KIND_GAP)
            kind = KIND_GAP
        if kind == KIND_NOT_APPLICABLE and not RECORD_NOT_APPLICABLE:
            return False
        ind = str(indicator or "").strip()
        if not ind:
            return False
        now = time.time()
        if _dedup_hit(kind, ind, str(reason or ""), now):
            return False
        path = _path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = Anomaly(ts=now, kind=kind, indicator=ind,
                       reason=" ".join(str(reason or "").split())[:300],
                       task_id=task_id, source=source,
                       extra=dict(extra or {})).to_json()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _trim(path)
        return True
    except Exception as exc:  # noqa: BLE001 观测失败绝不影响采集
        logger.debug("采集异常记录失败（忽略）: %s", exc)
        return False


def _trim(path: Path) -> None:
    """超过 `MAX_LINES` 就裁剪到 `KEEP_LINES`（有界）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES:
        return
    path.write_text("\n".join(lines[-KEEP_LINES:]) + "\n", encoding="utf-8")


def recent(*, limit: int = 100, since_hours: float = 72.0,
           kinds: tuple[str, ...] | None = None, root: Path | None = None,
           ) -> dict[str, Any]:
    """读最近的异常（给管理员界面用）：**按种类汇总 + 逐条明细**。

    判据（口径随数据下发）：`by_kind` 是计数，`total` 是本次读到的条数，
    `window_hours` 是时间窗 —— 前端要能一眼看出"这是哪个窗口里的多少条"。
    """
    path = _path(root)
    cutoff = time.time() - max(0.0, float(since_hours)) * 3600.0
    items: list[dict[str, Any]] = []
    bad_lines = 0
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue
                if float(obj.get("ts") or 0) < cutoff:
                    continue
                if kinds and str(obj.get("kind")) not in kinds:
                    continue
                items.append(obj)
    except OSError as exc:
        logger.debug("采集异常读取失败: %s", exc)
    items.sort(key=lambda o: float(o.get("ts") or 0), reverse=True)
    by_kind: dict[str, int] = {}
    for obj in items:
        k = str(obj.get("kind") or "?")
        by_kind[k] = by_kind.get(k, 0) + 1
    return {
        "total": len(items),
        "by_kind": by_kind,
        "window_hours": float(since_hours),
        "file": str(path),
        "bad_lines": bad_lines,
        "items": items[: max(1, int(limit))],
    }


__all__ = [
    "Anomaly",
    "DEDUP_WINDOW_S",
    "FILE_NAME",
    "KEEP_LINES",
    "KINDS",
    "KIND_ERROR",
    "KIND_GAP",
    "KIND_JOB_BUDGET",
    "KIND_LOOP_LAG",
    "KIND_NOT_APPLICABLE",
    "KIND_SEARCH_SOURCE",
    "KIND_SOURCE_LEAD",
    "KIND_TIMEOUT",
    "MAX_LINES",
    "recent",
    "record",
]
