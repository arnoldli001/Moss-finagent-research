"""从 Indicator Catalog 自动生成调度作业（第十二轮）。

## 为什么需要它

原状：`JOB_REGISTRY`（`registry.py`）里 40+ 个作业**全部手写 cron**。
代价是两处必然漂移：

1. **加了指标忘了加作业** —— 新指标只在线路里被 `SmartFetcher` 现拉，
   永远不进 DB，于是**永远不 fresh**，每次请求都联网（等于没做索引）。
2. **改了频率忘了改 cron** —— `configs/indicators.yaml` 写 `monthly`，
   而 registry 里的 cron 还是旧的日频 → 白跑 30 倍。

本模块把 `frequency` 作为**单一事实源**：cron 从它推导，不再手写。

## 频率 → cron 映射（本模块唯一权威）

| frequency | cron | 理由 |
|---|---|---|
| `realtime` | **不生成作业** | 盘中实时源，靠请求时现拉；落库由 A04 顺带完成 |
| `intraday` | `*/30 9-15 * * 1-5` | 半小时一次，只覆盖交易时段 |
| `daily` | `30 16 * * 1-5` | 收盘后 16:30（等交易所结算/数据源更新） |
| `weekly` | `40 16 * * 5` | 周五收盘后 |
| `monthly` | `0 9 3 * *` | 每月 3 日 09:00（等统计局/央行发布） |
| `quarterly` | `0 9 5 1,4,7,10 *` | 每季首月 5 日（等财报季） |
| `yearly` | `0 9 20 1 *` | 每年 1 月 20 日 |

**为什么 daily 是 16:30 而不是收盘 15:00**：多数免费源（东财/腾讯/交易所）
在 15:00 后仍需 1~2 小时结算；16:30 是实测较稳的时点（见
`docs/DATA_SOURCE_ROUTING.md`）。

## 与手写作业的关系（**不冲突，也不覆盖**）

生成出来的作业名统一带 `catalog_` 前缀：
    catalog_daily / catalog_weekly / catalog_monthly / ...

它们是**按频率聚合的批量作业**（一个作业拉该频率下的全部指标），
而不是"一个指标一个作业" —— 后者会让调度器被 50+ 作业淹没，而且
同一频率的指标完全可以一次并发拉完（复用 `SmartFetcher` 的并发能力）。

已有手写作业（如 `industry_valuation_snapshot`）保持不变：
它们覆盖的是"有特殊时序/口径"的场景，本模块不碰。

## 用法

    # 打印会生成哪些作业（不注册）
    uv run python -m src.scheduler.catalog_jobs --dry-run

    # 打印完整 cron 表
    uv run python -m src.scheduler.catalog_jobs --table

进程启动时由 `install_catalog_jobs()` 自动注册（见 `api/main.py` 的 lifespan）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from src.infrastructure.catalog.catalog_repo import CatalogRepository
from src.infrastructure.catalog.registry import IndicatorMeta, get_registry

logger = logging.getLogger(__name__)

#: 作业名前缀 —— 与手写作业区分开，便于"一键卸载/重建"
CATALOG_JOB_PREFIX = "catalog_"

#: 月频作业名（月频兜底也并入它 —— 见 `plan_jobs` 的 `floored`）。
MONTHLY_JOB_NAME = f"{CATALOG_JOB_PREFIX}monthly"

#: 月频兜底的目标频率（`plan_jobs` 用）。
MONTHLY_FALLBACK = "monthly"


@dataclass(frozen=True)
class CatalogJobPlan:
    """一个待注册的批量采集作业。"""

    name: str
    frequency: str
    cron: str
    indicators: tuple[str, ...]

    def describe(self) -> str:
        preview = ", ".join(self.indicators[:4])
        more = f" …共{len(self.indicators)}个" if len(self.indicators) > 4 else ""
        return f"{self.name:<22} {self.cron:<22} {preview}{more}"


#: ★ 频率 → cron 的**唯一权威映射**（改这里就是改全部）
#:
#: `None` = 不生成定时作业（realtime 靠请求时现拉 + A04 顺带落库）
FREQUENCY_TO_CRON: dict[str, str | None] = {
    "realtime": None,
    "intraday": "*/30 9-15 * * 1-5",
    "daily": "30 16 * * 1-5",
    "weekly": "40 16 * * 5",
    "monthly": "0 9 3 * *",
    "quarterly": "0 9 5 1,4,7,10 *",
    "yearly": "0 9 20 1 *",
}


#: ★ 2026-09-28：日历类指标**不走 SmartFetcher 批量链**，单独一个作业。
#:
#: 为什么单独（用户指出「避免下次取解禁数据取不到」）：
#:   日历数据来自 `fetch_unlock_schedule()` 这类**结构化 API**，
#:   不是 `A01 采集器` 能按 indicator 直接 fetch 的（它要调东财+巨潮双源）。
#:   所以走**专用同步函数** `sync_calendar_to_store()`，
#:   落库后由 `indicator_catalog` 索引接管，投研链路就能走 DB 命中。
CALENDAR_JOB_NAME = "catalog_calendar"
CALENDAR_JOB_CRON = "20 7 * * 1-5"     # 工作日 07:20（盘前，早于一切分析）

#: ★ 2026-09-28：数据缺口补取作业（A17 报缺口 → A19 生成连接器）。
#:
#: 为什么放**盘后 22:00**：
#:   · A17 在链路末端报缺口，当场补会让用户白等（补到的数据本轮也用不上）
#:   · A19 要跑 LLM 生成代码 + 沙箱验证，是**重活**，不该和盘中请求抢资源
#:   · 次日盘前（07:20 的日历作业、16:30 的批量作业）之前补完即可
GAP_DRAIN_JOB_NAME = "gap_drain"
GAP_DRAIN_JOB_CRON = "0 22 * * 1-5"


def plan_jobs(
    metas: list[IndicatorMeta] | None = None,
    *,
    max_per_job: int = 40,
) -> list[CatalogJobPlan]:
    """把 catalog 元数据聚合成批量作业计划（**纯函数**，便于测试）。

    Args:
        metas: 指标元数据；None 时从 registry 加载
        max_per_job: 单个作业最多带多少个指标（超出拆成 `_1`/`_2` 后缀）。
            为什么要拆：一个作业里塞 200 个指标会拉长执行时间、
            撞上"作业超时/重叠"问题；而且失败时整批重跑代价大。
    """
    if metas is None:
        metas = get_registry().all()

    by_freq: dict[str, list[str]] = {}
    floored: list[str] = []
    for meta in metas:
        if not meta.enabled:
            continue
        if meta.is_template():
            # 模板（`stock_close:{code}`）没有具体的 code，无法作为作业指标。
            # 全市场行情由 `scripts/download_market_data.py` 单独负责。
            continue
        if meta.category == "calendar":
            # ★ 日历类走专用作业（见 CALENDAR_JOB_NAME 的说明）
            continue
        cron = FREQUENCY_TO_CRON.get(meta.frequency)
        if cron is None:
            # ★★ 2026-09-30 月频兜底（用户口径：「**所有过时数据或没登记更新周期的
            #    数据都触发月频更新一次**」）。
            #
            # 原先这里直接 `continue` —— 于是这些指标**一个作业都没有**，
            # 只靠交互按需取数；一旦没人问，它们就静默停在旧值上
            # （实测 9 条：`fed:target_upper/lower/effr`、`fed:rate_prob:next`、
            #  `mkt:turnover:total`、`mkt:turnover_rate:all_a`、`mkt:cybkcb:*`）。
            # 兜底不能是 daily（那是"每天该更新"的断言，会让审计拿 3 天宽限去判
            # 一堆假 stale），也不能没有（那就是"永远不更新"）。
            # **monthly 是诚实的下限**：不知道它该多久更新，至少每月试一次。
            floored.append(meta.indicator)
            continue
        by_freq.setdefault(meta.frequency, []).append(meta.indicator)

    if floored:
        # 并入月频作业（不新建作业种类 —— 新建 kind 就得配执行器分支，
        # 本项目实测过"声明了但没有分发分支 ⇒ 每次记未知作业类型失败"）。
        #
        # ⚠️ 兜底目标**必须自己有 cron**：实测（`test_data_index_audit.py` 的 R6）
        #   在"monthly 映射被抹掉"的注入场景里，原先这里会直接 KeyError ⇒
        #   整个 `install_catalog_jobs()` 抛异常 ⇒ R6 的判据文案从
        #   「频率缺 cron 映射：['monthly']」退化成一句异常文本（**判据还在，
        #   但人看不懂了**）。所以这里退回"如实不兜底 + 出声"，让 R6 点名。
        if FREQUENCY_TO_CRON.get(MONTHLY_FALLBACK):
            by_freq.setdefault(MONTHLY_FALLBACK, []).extend(floored)
            logger.info("月频兜底：%d 个指标没有对应频率的作业（%s）⇒ 并入 %s",
                        len(floored), ", ".join(sorted(floored)[:4])
                        + ("…" if len(floored) > 4 else ""), MONTHLY_JOB_NAME)
        else:
            logger.warning(
                "月频兜底不可用：`FREQUENCY_TO_CRON` 里没有 `%s` 的 cron ⇒ "
                "%d 个指标仍无作业覆盖（审计 R6 会点名）",
                MONTHLY_FALLBACK, len(floored))

    plans: list[CatalogJobPlan] = []
    for freq, indicators in sorted(by_freq.items()):
        cron = FREQUENCY_TO_CRON.get(freq)
        if cron is None:
            # 该频率没有 cron 决策 ⇒ 不生成作业（**不许 KeyError**：
            # 那会让上层审计只看到一句异常，而不是"哪个频率缺映射"）。
            continue
        # 稳定排序：保证同一输入产出同一作业名（可 review、可断言）
        indicators = sorted(set(indicators))
        if len(indicators) <= max_per_job:
            plans.append(CatalogJobPlan(
                name=f"{CATALOG_JOB_PREFIX}{freq}",
                frequency=freq, cron=cron,
                indicators=tuple(indicators),
            ))
            continue
        for i in range(0, len(indicators), max_per_job):
            chunk = indicators[i:i + max_per_job]
            plans.append(CatalogJobPlan(
                name=f"{CATALOG_JOB_PREFIX}{freq}_{i // max_per_job + 1}",
                frequency=freq, cron=cron, indicators=tuple(chunk),
            ))
    return plans


def install_catalog_jobs(*, dry_run: bool = False) -> list[CatalogJobPlan]:
    """把 plan 注册进 `JOB_REGISTRY`（幂等：同名覆盖）。

    Args:
        dry_run: True 时只返回计划、不写注册表（给 `--dry-run` 用）
    Returns:
        已注册（或计划注册）的作业列表
    """
    from src.scheduler.registry import JOB_REGISTRY, JobSpec

    plans = plan_jobs()
    if dry_run:
        return plans

    # ★ 日历专用作业（解禁/财报/宏观日程 → 落库 → 索引接管）
    JOB_REGISTRY[CALENDAR_JOB_NAME] = JobSpec(
        name=CALENDAR_JOB_NAME,
        cron=CALENDAR_JOB_CRON,
        kind="calendar_sync",
        description="投资日历同步：限售解禁/财报预约/宏观日程 → "
                    "fact_data_points（东财+巨潮双源）",
        params={"horizon_days": 45},
    )

    # ★ 缺口补取作业（A17 报缺口 → A19 生成连接器 → 回填索引）
    JOB_REGISTRY[GAP_DRAIN_JOB_NAME] = JobSpec(
        name=GAP_DRAIN_JOB_NAME,
        cron=GAP_DRAIN_JOB_CRON,
        kind="gap_drain",
        description="消费数据缺口队列：A19 为缺口生成连接器 → "
                    "落库 → 回填索引（次日分析即可 DB 命中）",
        params={"max_items": 5},
    )

    for plan in plans:
        JOB_REGISTRY[plan.name] = JobSpec(
            name=plan.name,
            cron=plan.cron,
            kind="catalog_collection",
            description=(
                f"catalog 批量采集（{plan.frequency}，{len(plan.indicators)} 个指标）"
                f"：{', '.join(plan.indicators[:5])}"
                f"{' …' if len(plan.indicators) > 5 else ''}"
            ),
            params={"indicators": list(plan.indicators),
                    "frequency": plan.frequency},
        )
    logger.info("catalog 作业已注册：%d 个（指标 %d 个）",
                len(plans), sum(len(p.indicators) for p in plans))
    return plans


def catalog_coverage() -> dict[str, object]:
    """覆盖率自检：catalog 里有多少指标**没有**被任何作业覆盖。

    给运维页用：如果 `uncovered` 很大，说明有一批指标只能靠请求时现拉，
    索引表永远不 fresh —— 这正是本模块要消灭的状态。

    ★ 2026-09-28：日历类指标由 `CALENDAR_JOB_NAME` 专用作业覆盖，
    不能算"未覆盖"（它们走结构化 API 而不是 A01 批量链）。
    """
    metas = [m for m in get_registry().all()
             if m.enabled and not m.is_template()]
    plans = plan_jobs(metas)
    covered = {ind for p in plans for ind in p.indicators}
    # ★ 2026-09-30：原先这里是"排除在覆盖之外"的一类（`frequency` 没有 cron ⇒
    #   设计上不建作业）。用户口径改成「**没登记更新周期的数据都触发月频更新一次**」
    #   之后，它们**已经进月频作业**（见 `plan_jobs` 的 `floored`）⇒ 现在是
    #   "靠月频兜底覆盖"的**子集**，不再是与 covered 并列的一类。
    #   口径不闭合会很危险（`covered + realtime == total` 会因为重复计数而假绿），
    #   所以下面同时给出 `floored` 与新的闭合式。
    floored = [m.indicator for m in metas
               if FREQUENCY_TO_CRON.get(m.frequency) is None]
    # 日历类：由专用作业覆盖
    calendar = [m.indicator for m in metas if m.category == "calendar"]
    uncovered = [m.indicator for m in metas
                 if m.indicator not in covered
                 and m.indicator not in calendar]
    jobs = [{"name": p.name, "cron": p.cron,
             "frequency": p.frequency,
             "indicators": len(p.indicators)} for p in plans]
    jobs.append({"name": CALENDAR_JOB_NAME, "cron": CALENDAR_JOB_CRON,
                 "frequency": "daily", "indicators": len(calendar)})
    return {
        "total": len(metas),
        "covered": len(covered) + len(calendar),
        #: 其中**靠月频兜底**才被覆盖的（频率无 cron ⇒ 并入 catalog_monthly）
        "floored": sorted(floored),
        #: 旧字段名保留（前端/审计在用）：语义已从"设计上不覆盖"变成
        #: "只靠月频兜底覆盖"。**别删**，删了调用方会静默读到 KeyError。
        "realtime_by_design": len(floored),
        "calendar_by_design": len(calendar),
        "uncovered": sorted(uncovered),
        "jobs": jobs,
    }


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="catalog 驱动的调度作业生成")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    ap.add_argument("--table", action="store_true", help="打印完整 cron 表")
    ap.add_argument("--coverage", action="store_true", help="打印覆盖率自检")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    plans = install_catalog_jobs(dry_run=True)
    print("=" * 90)
    print(f"catalog 作业计划（{len(plans)} 个作业，"
          f"{sum(len(p.indicators) for p in plans)} 个指标）")
    print("=" * 90)
    for p in plans:
        print("  " + p.describe())

    if args.table:
        print()
        print("频率 → cron 映射（唯一权威，改这里即改全部）：")
        for freq, cron in FREQUENCY_TO_CRON.items():
            print(f"  {freq:<12} {cron or '（不生成作业，请求时现拉）'}")

    if args.coverage:
        cov = catalog_coverage()
        print()
        print("覆盖率自检：")
        print(f"  catalog 指标总数 : {cov['total']}")
        print(f"  被作业覆盖       : {cov['covered']}")
        print(f"  realtime（设计上不建作业）: {cov['realtime_by_design']}")
        unc = cov["uncovered"]
        print(f"  未覆盖           : {len(unc)}"
              + (f"  {unc[:5]}" if unc else "  ✅ 全部覆盖"))
        if unc:
            print("  ⚠️ 未覆盖指标只能靠请求时现拉，索引表永远不 fresh")

    if args.dry_run:
        print()
        print("（--dry-run：未写入 JOB_REGISTRY）")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
