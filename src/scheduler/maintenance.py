"""定期数据维护：扫「登记指标的事实序列」→ 分类 → 报缺口 → 入补采队列。

## 为什么需要它（用户 2026-09-29 原话）

> 「数据采集 agent 要记录任何未能获取到的信息日志，展示在后端日志里，
>   方便查看采集效果，**定期维护数据**」

本项目实测的代价值得写在这：`us_unemployment` / `us_nonfarm` / `us_pce` /
`us_core_cpi` / `us_fed_rate` **停在 2025-07/08**（陈旧约 14 个月），
`社融` 停在 **2026-04**（陈旧 5 个月）—— 而**没有任何一处会主动说出来**。
它们只在被问到时以「某条数据缺失 / 美林时钟不明确」的形式冒出来，
**看起来像取数失败，实际是没人维护**（源或作业停在那儿了）。

## 判据全部从既有单一真值源读（不写清单）

| 要什么 | 从哪读 |
|---|---|
| 指标名单与频率 | `IndicatorRegistry`（`configs/indicators.yaml`） |
| 新鲜度分级 | `DataFreshnessEvaluator`（与 §15/§19 同一份实现，**不另立阈值**） |
| 运行时态（有没有点、最新期） | `CatalogRepository.all()`（统一数据层，**本模块不写 SQL**） |
| 补救动作 | **入既有缺口队列** `gap_queue` —— 补取由既有 drain 作业做，

本模块**从不自己取数、从不写事实表**（只读 + 入队 + 落报告）。

## 输出（两处，都要能被人看见）

* 日志（`[数据维护]` 标签，可 `grep`）：一行汇总 + 每条陈旧/缺失一行；
* 报告 `data/run/data_freshness_report.json`：人可看、机可读（含每条的判据）。

## 与 `catalog_*` 批采作业的分工

`catalog_daily/weekly/monthly` 负责**按频率拉数**；本模块负责**回答"拉回来的够不够新"**
—— 两者互补：批采可能一直在跑，而某个源自己停更了（本项目正是这种情况）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.core.data_freshness import DataFreshnessEvaluator
from src.infrastructure.catalog.data_stores import store_rel

logger = logging.getLogger(__name__)

#: 日志标签（grep 用）。
TAG = "[数据维护]"

#: 报告文件名（目录走登记的 `run_dir` 存储，**不写字面路径** ——
#: `tests/unit/test_store_registry.py::test_no_store_path_literals_in_src` 守着这条）。
REPORT_NAME = "data_freshness_report.json"


def report_path(root: Path | None = None, *, env: str = "") -> Path:
    """报告落盘位置：`<root>/<登记的 run_dir>/data_freshness_report[_<env>].json`。

    为什么带 env：`run_dir` 是**共享**存储，而 dev/pilot 的事实表是**各自的**
    —— 两个实例写同一个文件会互相覆盖（看到的是别人的结论）。
    """
    base = Path(root) if root is not None else Path.cwd()
    suffix = f"_{env}" if env else ""
    name = REPORT_NAME.replace(".json", f"{suffix}.json")
    return base / store_rel("run_dir") / name

#: 判为「需要维护」的新鲜度状态（报告里的 `state` 取值，见 `_classify`）。
NEEDS_MAINTENANCE = ("lagging", "stale", "expired", "missing", "empty")


@dataclass(frozen=True)
class FreshnessIssue:
    """一条需要维护的指标。"""

    indicator: str
    state: str            # lagging / stale / expired / missing / empty
    status: str           # DataFreshnessEvaluator 的 status（missing/empty 时为 "-"）
    last_period_date: str
    days_since: int
    row_count: int
    frequency: str
    publish_cycle_days: int
    reason: str

    def log_line(self) -> str:
        """一行可 grep 的日志（字段固定顺序，便于 awk/统计）。"""
        return (f"{TAG} indicator={self.indicator} state={self.state} "
                f"days_since={self.days_since} last={self.last_period_date or '-'} "
                f"rows={self.row_count} freq={self.frequency} reason={self.reason}")


#: 频率 → 期望发布周期（天）。**从登记的 `frequency` 读**，不用评估器的猜测。
#:
#: 为什么不能只用 `DataFreshnessEvaluator` 的 `publish_cycle_days`：它对
#: 季频指标给 30 天（粗粒度启发式），于是"6 月 30 日的财报在 9 月 29 日看"
#: （91 天）会被判成停更 —— 实测这一条就贡献了 700+ 条**假告警**。
_FREQ_CYCLE_DAYS: dict[str, int] = {
    "realtime": 1, "intraday": 1, "daily": 1, "weekly": 7,
    "monthly": 30, "quarterly": 90, "yearly": 365,
}

#: 判为「**该维护了**」的宽限期规则：`days_since > K × 期望周期`。
#:
#: ## 为什么不能直接用 `lagging/stale` 分级
#:
#: 第一版就是这么写的，**实测 862 条里报了 835 条**（连"`PB:600036` 昨天刚更新"
#: 都被标 lagging）—— 那是**给上下文加权**用的连续衰减口径，不是"要不要维护"。
#: 一份 97% 都是告警的报告等于没有报告（本项目为"9 条长期红灯被当背景噪音"
#: 付过代价：漂移哨兵被无视两天，最后以"客户抱怨邮件太多"的形式爆出来）。
#:
#: 维护判据要回答的是"**这条序列停了**"：
#:   * `days_since > 3 × 期望周期`（日频 ≥3 天、月频 ≥90 天、季频 ≥270 天）；
#:   * 或已 `expired`（超过该指标的硬截止 `filter_days` ⇒ 已从分析中过滤）；
#:   * 宽限期下限 3 天：跨周末与连续假期不该报（日频序列周一早上看是 3 天前）。
MAINTENANCE_CYCLE_MULTIPLIER = 3

#: 宽限期下限（天）。
MAINTENANCE_MIN_GRACE_DAYS = 3


def expected_cycle_days(frequency: str) -> int:
    """登记的频率 → 期望发布周期（天）；未知频率按 30 天（保守：不轻易报）。"""
    return _FREQ_CYCLE_DAYS.get(str(frequency or "").strip().lower(), 30)


def _grace_days(frequency: str) -> int:
    """该指标的维护宽限期（天）。"""
    return max(MAINTENANCE_MIN_GRACE_DAYS,
               MAINTENANCE_CYCLE_MULTIPLIER * expected_cycle_days(frequency))


def _looks_like_quarter_end(period: str) -> bool:
    """`2026-06-30` 这类**季末**日期（用来识别"季频数据按日频登记"）。"""
    text = str(period or "")
    if len(text) < 10:
        return False
    month, day = text[5:7], text[8:10]
    return month in ("03", "06", "09", "12") and day >= "28"


def _classify(entry: Any, today: date) -> FreshnessIssue | None:
    """一条 `CatalogEntry` → 需要维护的问题（健康返回 None）。

    四种"没数据"必须分开（AGENTS.md：「没量到」≠「量到 0」，也不等于「源停了」）：

      * `missing`        —— 登记了、但事实表**一条都没有**（从没采到）；
      * `empty`          —— 有点，但**没有期间**（`last_period_date` 为空）；
      * `freq_mismatch`  —— 登记成日频/实时，而最新一期落在**季末**且已隔 60 天以上
        ⇒ 数据是**季频**的，频率登记错了（后果：`SmartFetcher` 每天白取一次）；
      * `stale`/`expired` —— 按宽限期规则判"这条序列停了"。
    """
    indicator = str(getattr(entry, "indicator", "") or "")
    rows = int(getattr(entry, "row_count", 0) or 0)
    last = str(getattr(entry, "last_period_date", "") or "")
    freq = str(getattr(entry, "frequency", "") or "")
    if "{" in indicator:
        return None                     # 模板（`PB:{code}`）本身不该有点

    if rows <= 0:
        return FreshnessIssue(
            indicator, "missing", "-", "", 10**6, rows, freq, 0,
            "登记在册但事实表零行（从未采到）")
    if not last:
        return FreshnessIssue(
            indicator, "empty", "-", "", 10**6, rows, freq, 0,
            "有点但没有期间（period_date 全空）⇒ 无法判新鲜度")

    result = DataFreshnessEvaluator.evaluate(indicator, last, today=today)
    days = int(result.days_since)
    cycle = expected_cycle_days(freq)
    grace = _grace_days(freq)
    if result.status == "expired":
        return FreshnessIssue(
            indicator, "expired", result.status, last, days, rows, freq, cycle,
            f"距最新一期 {days} 天，已超过该指标的硬截止（已从分析中过滤）")
    if days > grace:
        state = ("freq_mismatch"
                 if freq in ("daily", "realtime", "intraday")
                 and days > 60 and _looks_like_quarter_end(last)
                 else "stale")
        reason = (
            f"登记频率={freq}（期望 {cycle} 天）但最新一期 {last} 距今天 {days} 天"
            + ("；该期间是**季末** ⇒ 数据其实是季频，频率登记错了"
               "（后果：每天白取一次）" if state == "freq_mismatch" else
               f" > 宽限 {grace} 天 ⇒ 这条序列停了"))
        return FreshnessIssue(indicator, state, result.status, last, days,
                              rows, freq, cycle, reason)
    return None


def _stale_by_index(entry: Any) -> bool:
    """按**索引里的** `last_period_date` 粗判"可能过期了"（用于挑出要对齐的子集）。

    刻意比 `_classify` 宽松（用一半宽限期）：这里只决定"要不要去事实表核一遍"，
    漏掉几个不影响正确性（最终判据仍在 `_classify`），多核几个代价很小。
    """
    last = str(getattr(entry, "last_period_date", "") or "")
    freq = str(getattr(entry, "frequency", "") or "")
    result = DataFreshnessEvaluator.evaluate(
        str(getattr(entry, "indicator", "") or ""), last)
    return int(result.days_since) * 2 > _grace_days(freq)


async def audit(
    *, today: date | None = None, enqueue: bool = True,
    write_report: bool = True, root: Path | None = None,
) -> dict[str, Any]:
    """跑一次数据维护审计（**async**：`CatalogRepository.all()` 是异步的）。

    Returns: `{"scanned": n, "issues": [...], "enqueued": n, "by_state": {...},
               "report_path": str|None}`
    """
    from src.infrastructure.catalog.catalog_repo import CatalogRepository
    from src.infrastructure.catalog.registry import get_registry

    day = today or date.today()
    registry = get_registry()
    repo = CatalogRepository()
    entries = await repo.all(only_enabled=True)

    # ★★ 2026-09-30：「停产」必须以**登记表**为准，不许信索引里的副本。
    #
    # 实测缺陷：把 5 条源已停更的美国宏观改成 `enabled: false` 之后，登记表解析
    # 是对的（`meta.enabled is False`），但 `indicator_catalog` 里那 5 行的
    # `enabled` 列**仍是 1**（两处各存一份 = 必然漂移），而 `repo.all(only_enabled=True)`
    # 过滤的是**索引那一列** ⇒ 停产的 5 条**照旧每月出现在报告里并白取一次**。
    # 判据改为从 `registry` **现读**（单一真值源），索引列只当它自己的运行时态。
    disabled = {str(m.indicator) for m in registry.all() if not m.enabled}

    # ★ 先**把索引对齐事实表**再判（否则会把"索引没刷新"报成"序列停了"——
    #   实测 `stock_close:600036` 索引 2026-09-24 / 事实 2026-09-29，
    #   差 5 天；那是两类完全不同的根因，修法也不同）。
    #   只对齐"看着可能有问题"的那些（有行 + 最后期在宽限期外），保持作业轻。
    suspect = [
        str(e.indicator) for e in entries
        if getattr(e, "row_count", 0)
        and "{" not in str(e.indicator)
        and str(getattr(e, "last_period_date", "") or "")
        and _stale_by_index(e)
    ]
    refreshed = 0
    if suspect:
        try:
            refreshed = await repo.refresh_stats_from_facts(suspect)
            entries = await repo.all(only_enabled=True)
        except Exception as exc:  # noqa: BLE001 索引刷新失败 → 按原索引判，但要出声
            logger.warning("%s 索引对齐失败（按现有索引判，可能多报）：%s",
                           TAG, exc)

    issues: list[FreshnessIssue] = []
    seen: set[str] = set()
    for entry in entries:
        indicator = str(getattr(entry, "indicator", "") or "")
        if not indicator or indicator in seen or indicator in disabled:
            continue
        seen.add(indicator)
        issue = _classify(entry, day)
        if issue is not None:
            issues.append(issue)

    # 登记在册但**索引里根本没有**的指标（索引没建/被清）——也是"没量到"
    for meta in registry.all():
        ind = str(getattr(meta, "indicator", "") or "")
        if not ind or ind in seen or meta.is_template() or not meta.enabled:
            continue
        issues.append(FreshnessIssue(
            ind, "missing", "-", "", 10**6, 0, str(meta.frequency or ""), 0,
            "登记在册但索引/事实表里查不到（未采集或索引未建）"))

    issues.sort(key=lambda i: (-i.days_since, i.indicator))

    enqueued = 0
    if enqueue and issues:
        try:
            from src.domain.agents.decision.gap_queue import get_gap_queue

            queue = get_gap_queue()
            for issue in issues:
                if queue.enqueue(
                    issue.indicator,
                    reason=f"{TAG} {issue.state}: {issue.reason}",
                    source="freshness_audit",
                ):
                    enqueued += 1
        except Exception as exc:  # noqa: BLE001 入队是增值功能，坏了不阻断
            logger.warning("%s 缺口入队失败（不影响报告）：%s", TAG, exc)

    by_state: dict[str, int] = {}
    for issue in issues:
        by_state[issue.state] = by_state.get(issue.state, 0) + 1

    report_file: Path | None = None
    if write_report:
        env = str(os.environ.get("MOSS_ENV") or "")
        report_file = report_path(root, env=env)
        try:
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text(json.dumps({
                "date": day.isoformat(),
                "env": env or "default",
                "scanned": len(seen),
                "issues": len(issues),
                "by_state": by_state,
                "enqueued": enqueued,
                "index_aligned": refreshed,
                "items": [asdict(i) for i in issues],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 报告写不出不该让作业失败
            logger.warning("%s 报告落盘失败：%s", TAG, exc)
            report_file = None

    # ---- 日志：汇总 + **可执行的那些**逐条 + 其余按族聚合 ----
    #
    # 为什么不全逐条打：实测 862 条登记里 700+ 条是"季频数据按日频登记"
    # （`商誉占净资产比:600036` 这种），逐条打会把日志刷成噪音 ——
    # 而日志的价值是**让人一眼看出该修哪件事**。完整清单在 JSON 报告里。
    actionable = [i for i in issues if i.state in ("expired", "missing", "empty")]
    grouped: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if issue.state not in ("stale", "freq_mismatch"):
            continue
        root = issue.indicator.split(":", 1)[0]
        slot = grouped.setdefault(root, {"state": issue.state, "n": 0,
                                         "freq": issue.frequency,
                                         "max_days": 0, "last": ""})
        slot["n"] += 1
        if issue.days_since > slot["max_days"]:
            slot["max_days"] = issue.days_since
            slot["last"] = issue.last_period_date

    logger.info(
        "%s 汇总 date=%s 扫描=%d 需维护=%d 入队=%d 索引对齐=%d 分布=%s",
        TAG, day.isoformat(), len(seen), len(issues), enqueued, refreshed,
        by_state or "无")
    for issue in actionable:
        logger.warning("%s", issue.log_line())
    for root, slot in sorted(grouped.items(),
                             key=lambda kv: -kv[1]["max_days"]):
        logger.warning(
            "%s group=%s state=%s 覆盖=%d条 freq=%s 最旧=%s（%d 天）"
            " ⇒ 根因通常是**频率登记**或**批采作业**，逐条清单见报告",
            TAG, root, slot["state"], slot["n"], slot["freq"],
            slot["last"] or "-", slot["max_days"])

    return {
        "scanned": len(seen), "issues": [asdict(i) for i in issues],
        "enqueued": enqueued, "by_state": by_state,
        "index_aligned": refreshed,
        "report_path": str(report_file) if report_file else None,
        #: 人话结论（前端/日志都直接用这一句）
        "summary": (
            f"扫描 {len(seen)} 条登记指标，需维护 {len(issues)} 条"
            + (f"（{by_state}）" if by_state else "")
            + f"，已入补采队列 {enqueued} 条"),
    }


__all__ = ["TAG", "REPORT_NAME", "NEEDS_MAINTENANCE", "FreshnessIssue",
           "audit", "report_path", "expected_cycle_days"]
