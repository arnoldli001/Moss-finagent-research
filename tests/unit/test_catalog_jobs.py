"""catalog 驱动的调度作业生成测试（第十二轮）。

守两件事：
1. **频率 → cron 映射的单一权威**（改一处即改全部，不能有人在别处手写 cron）
2. **覆盖率**（catalog 里每个非 realtime 指标都必须被某个作业覆盖 ——
   否则它只能靠请求时现拉，索引表永远不 fresh，等于没做索引）
"""

from __future__ import annotations

import pytest

from src.infrastructure.catalog.registry import IndicatorMeta
from src.scheduler.catalog_jobs import (
    CATALOG_JOB_PREFIX,
    FREQUENCY_TO_CRON,
    catalog_coverage,
    plan_jobs,
)


def _meta(ind: str, freq: str, *, enabled: bool = True,
          template: bool = False) -> IndicatorMeta:
    return IndicatorMeta(
        indicator=ind, category="x", frequency=freq,
        freshness_hours=24, primary_source="", source_url="",
        enabled=enabled, ttl_days=365,
    )


# ============================================================
# 频率 → cron 映射
# ============================================================


def test_realtime_is_floored_into_monthly():
    """★ 口径变更（2026-09-30，用户原话）：

    > 「所有**过时数据**或**没登记更新周期**的数据都触发**月频**更新一次」

    `realtime` 指标在 `FREQUENCY_TO_CRON` 里是显式 `None`（不建小时级作业），
    于是**原先它们一个作业都没有** —— 实测 9 条（`fed:target_upper/lower/effr`、
    `fed:rate_prob:next`、`mkt:turnover:total`、`mkt:turnover_rate:all_a`、
    `mkt:cybkcb:*`）只靠交互按需取数，没人问就静默停在旧值上。

    > ~~`realtime` 指标**不建**定时作业（靠请求时现拉 + A04 顺带落库）。
    > 依据：realtime 的 freshness_hours=0.5，定时作业最快也只能小时级，
    > 建了也是永远 stale，纯浪费调度器槽位与网络调用。~~
    > 废止理由：**"永远 stale"说的是新鲜度判据，不是"该不该定期拉"**。
    > 月频兜底不是把它当实时数据用，而是给它一个**诚实的下限**
    > （不知道它该多久更新，至少每月试一次）。

    判据（两条，都要）：
      ① `FREQUENCY_TO_CRON["realtime"] is None` **仍然成立**（不许改这条声明）；
      ② 但 realtime 指标必须**出现在月频作业里**，且被单独计数（不是混进 covered）。
    """
    assert FREQUENCY_TO_CRON["realtime"] is None, (
        "realtime 仍然不该有小时级 cron —— 变化的是它**并入月频兜底**，"
        "不是给它一个 realtime 作业")
    plans = plan_jobs([_meta("mkt:turnover:total", "realtime"),
                       _meta("fed:policy_range", "realtime")])
    assert len(plans) == 1, "realtime 指标应并入**一个月频**作业"
    assert plans[0].name == f"{CATALOG_JOB_PREFIX}monthly"
    assert set(plans[0].indicators) == {"mkt:turnover:total", "fed:policy_range"}


def test_every_frequency_has_a_mapping():
    """白名单里的每个 frequency 都必须有 cron 决策（含显式 None）。"""
    from src.infrastructure.catalog.registry import FREQUENCIES

    for freq in FREQUENCIES:
        assert freq in FREQUENCY_TO_CRON, (
            f"frequency `{freq}` 没有 cron 映射 —— 新增频率时必须同时"
            "更新 FREQUENCY_TO_CRON，否则该频率的指标会静默不被采集"
        )


def test_daily_cron_is_after_market_close():
    """daily 必须排在收盘之后（15:00 后留结算时间），不能盘中跑。"""
    cron = FREQUENCY_TO_CRON["daily"]
    assert cron is not None
    minute, hour, _dom, _mon, dow = cron.split()
    assert int(hour) >= 16, f"daily cron 定在 {hour} 点，早于数据源结算时间"
    assert dow == "1-5", "daily 应只在工作日跑"


def test_monthly_cron_waits_for_official_release():
    """monthly 必须留出官方发布延迟（统计局/央行通常次月上旬发布）。"""
    cron = FREQUENCY_TO_CRON["monthly"]
    assert cron is not None
    _m, _h, dom, _mon, _dow = cron.split()
    assert int(dom) >= 3, "monthly 定在月初，官方数据往往还没发布"


# ============================================================
# 作业计划聚合
# ============================================================


def test_plans_group_by_frequency():
    """同频率指标聚合成**一个**作业（不是一个指标一个作业）。"""
    plans = plan_jobs([
        _meta("a", "daily"), _meta("b", "daily"), _meta("c", "monthly"),
    ])
    names = {p.name for p in plans}
    assert names == {f"{CATALOG_JOB_PREFIX}daily", f"{CATALOG_JOB_PREFIX}monthly"}
    daily = next(p for p in plans if p.name == f"{CATALOG_JOB_PREFIX}daily")
    assert set(daily.indicators) == {"a", "b"}


def test_plans_skip_disabled():
    """disabled 指标不参与作业生成。"""
    plans = plan_jobs([_meta("on", "daily"), _meta("off", "daily", enabled=False)])
    assert len(plans) == 1
    assert plans[0].indicators == ("on",)


def test_plans_skip_templates():
    """模板指标（`stock_close:{code}`）不参与 —— 它没有具体 code。

    全市场行情由 `scripts/download_market_data.py` 单独负责。
    """
    plans = plan_jobs([
        _meta("stock_close:{code}", "daily", template=True),
        _meta("CPI", "daily"),
    ])
    all_inds = [i for p in plans for i in p.indicators]
    assert "stock_close:{code}" not in all_inds
    assert "CPI" in all_inds


def test_plans_deterministic_order():
    """同一输入必须产出同一作业名与同一指标顺序（可 review、可断言）。"""
    metas = [_meta(f"ind_{i:02d}", "daily") for i in range(10)]
    p1 = plan_jobs(metas)
    p2 = plan_jobs(list(reversed(metas)))
    assert [p.name for p in p1] == [p.name for p in p2]
    assert p1[0].indicators == p2[0].indicators  # 排序去重后一致


def test_plans_split_large_batches():
    """单作业指标数超上限时拆分成 `_1`/`_2` 后缀。"""
    metas = [_meta(f"ind_{i:03d}", "daily") for i in range(95)]
    plans = plan_jobs(metas, max_per_job=40)
    names = sorted(p.name for p in plans)
    assert names == [
        f"{CATALOG_JOB_PREFIX}daily_1",
        f"{CATALOG_JOB_PREFIX}daily_2",
        f"{CATALOG_JOB_PREFIX}daily_3",
    ]
    # 不丢指标
    total = sum(len(p.indicators) for p in plans)
    assert total == 95


# ============================================================
# 覆盖率（本文件最重要的断言）
# ============================================================


def test_real_catalog_is_fully_covered():
    """★ 真实 catalog 里每个非 realtime 指标都必须被作业覆盖。

    这条红了意味着：有指标只能靠请求时现拉 → 索引表永远不 fresh →
    SmartFetcher 的 DB 命中路径对它永远不生效。这正是第十二轮要消灭的状态。
    """
    cov = catalog_coverage()
    unc = cov["uncovered"]
    assert not unc, (
        f"{len(unc)} 个指标没有被任何调度作业覆盖：{unc[:10]}。"
        "要么在 configs/indicators.yaml 给它一个非 realtime 的 frequency，"
        "要么在 FREQUENCY_TO_CRON 里为它的频率提供 cron。"
    )
    assert cov["total"] > 0, "catalog 是空的 —— indicators.yaml 没被加载"


def test_coverage_reports_floored_separately():
    """靠**月频兜底**才被覆盖的指标要**单独计数**，不能混进普通的 covered。

    口径纪律（AGENTS.md）："没量到"与"量到 0"必须分开显示。
    这里的对照组是"靠频率作业覆盖"与"只靠月频兜底覆盖"。

    ★ 2026-09-30 口径变更：原先这条断言是
    `covered + realtime_by_design == total`（当时 realtime 是
    **不被覆盖**的一类，与 covered 并列）。现在它们**已被月频作业覆盖**
    （⊂ covered）⇒ 旧等式会**重复计数**、变成假绿，故改成新的闭合式：

        covered + calendar 已含在 covered 里 ⇒
        `covered(含日历) == total − uncovered`
    """
    cov = catalog_coverage()
    assert cov["floored"], "catalog 里应有靠月频兜底的指标（realtime 那批）"
    assert cov["realtime_by_design"] == len(cov["floored"]), (
        "旧字段名必须与新口径同步（调用方还在读它）")
    assert cov["covered"] == cov["total"] - len(cov["uncovered"]), (
        "覆盖率口径不闭合：covered(含日历) 应等于 total − uncovered")
    assert cov["uncovered"] == [], "★ 兜底之后不该还有未覆盖的指标"


# ============================================================
# 执行器分支存在性（防"声明了但没实现"）
# ============================================================


def test_catalog_collection_kind_has_executor_branch():
    """`catalog_collection` 必须在 jobs.py 里有分发分支。

    本项目实测过这个坑（AGENTS.md）：作业在 JOB_REGISTRY 里声明了，
    但执行器分发链没有分支 → 每次记"未知作业类型"失败，
    且**看起来像"正常跑了但没数据"**。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "scheduler" / "jobs.py"
    text = src.read_text(encoding="utf-8")
    assert 'spec.kind == "catalog_collection"' in text, (
        "jobs.py 里没有 catalog_collection 的分发分支 —— "
        "作业会被记为「未知作业类型」失败"
    )
