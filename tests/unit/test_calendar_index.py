"""投资日历 → 索引体系 测试（第十三轮 · 用户指出「避免下次取解禁数据取不到」）。

## 守的是什么

用户原话：
> 「这个解禁规模在平台的投资日历功能块里有详细的每天解禁数据，
>   不存在缺少数据，可以去对应数据库里取」
> 「加入到索引清单，避免下次取解禁数据取不到」

在此之前解禁数据**只能实时调接口**，不在 `fact_data_points` 里 →
索引表没有它 → SmartFetcher 判"未登记" → 每次分析都现拉网络。

本文件守三段链路：
  ① 转换（CalendarEvent → DataPoint）
  ② 登记（指标在 YAML 里 + 有专用调度作业）
  ③ 索引（SmartFetcher 能走 DB 命中）

以及**三个实测踩过的坑**（每个都有对应测试）：
  · `fetch_unlock_schedule` 返回 `(events, tried)` 元组，当列表用会静默产出 0 条
  · `metrics` 键带 kind 前缀（`unlock_market_cap`），裸取会得到 0
  · `data_id` 是主键，用稳定 ID 会导致值修正**永远写不进去**
"""

from __future__ import annotations

import pytest

from src.domain.intel.calendar_store import (
    CAL_PREFIX,
    _KIND_METRICS,
    _top_stock_cap,
    calendar_events_to_points,
)


class FakeEvent:
    """最小 CalendarEvent 替身（只带转换需要的字段）。"""

    def __init__(self, kind: str, date: str, *, scope=None, metrics=None,
                 certainty: str = "rule") -> None:
        self.kind = kind
        self.date = date
        self.title = f"{kind} {date}"
        self.scope = scope or {}
        self.metrics = metrics or {}
        self.certainty = certainty


# ============================================================
# ① 转换：CalendarEvent → DataPoint
# ============================================================


def test_unlock_event_produces_three_indicators():
    """解禁事件应产出 3 条指标（市值/家数/最大单只）。"""
    ev = FakeEvent(
        "unlock", "2026-10-28",
        scope={"company_count": 9,
               "codes": ["688783"], "names": ["西安奕材"],
               "stocks": [{"code": "688783", "name": "西安奕材",
                           "market_cap": 84850495969.77}]},
        metrics={"unlock_market_cap": 108078427191.96},
    )
    pts = calendar_events_to_points([ev])
    inds = {p.indicator for p in pts}
    assert inds == {
        "cal:unlock:market_cap",
        "cal:unlock:company_count",
        "cal:unlock:top_stock_cap",
    }
    by_ind = {p.indicator: p for p in pts}
    # ★ 关键：market_cap 必须取到真值（踩过取键错误的坑）
    assert by_ind["cal:unlock:market_cap"].value == pytest.approx(
        108078427191.96)
    assert by_ind["cal:unlock:company_count"].value == 9
    assert by_ind["cal:unlock:top_stock_cap"].value == pytest.approx(
        84850495969.77)


def test_metrics_key_with_kind_prefix_is_read():
    """★ 核心坑：`metrics` 的键带 kind 前缀（`unlock_market_cap`）。

    直接 `metrics.get("market_cap")` 会**静默取到 0**
    —— 实测：29 条解禁市值全是 0，而同批的 top_stock_cap 有值，
    一眼看出是取键错了。这条测试红了 = 又退回了裸键。
    """
    ev = FakeEvent("unlock", "2026-09-28",
                   scope={"company_count": 25},
                   metrics={"unlock_market_cap": 53453114600.72})
    pts = calendar_events_to_points([ev])
    cap = next(p for p in pts if p.indicator.endswith("market_cap"))
    assert cap.value == pytest.approx(53453114600.72), (
        "market_cap 取值失败 —— 检查 metrics 键是否带了 kind 前缀"
    )
    assert cap.value != 0


def test_earnings_event_produces_count_only():
    """财报事件只产家数（它没有"市值"这种可比值）。"""
    ev = FakeEvent("earnings", "2026-10-31",
                   scope={"company_count": 12}, certainty="scheduled")
    pts = calendar_events_to_points([ev])
    assert [p.indicator for p in pts] == ["cal:earnings:company_count"]
    assert pts[0].value == 12


def test_macro_and_trade_day_are_skipped():
    """`macro` / `trade_day` 是**日程不是数值**，不该硬造 value。

    AGENTS.md：宁可不显示，也不显示假的。
    """
    for kind in ("macro", "trade_day"):
        pts = calendar_events_to_points([FakeEvent(kind, "2026-10-01")])
        assert pts == [], f"{kind} 不该产出数值型数据点"


def test_event_without_date_is_skipped():
    """缺日期的条目跳过（无法定期间）。"""
    assert calendar_events_to_points([FakeEvent("unlock", "")]) == []


def test_data_id_is_content_addressed():
    """★ 核心坑：`data_id` 必须含内容哈希。

    `data_id` 是**主键**。用稳定 ID（`cal_unlock_x_2026-09-28`）时，
    后续值修正会撞主键被 `INSERT OR IGNORE` 静默忽略 ——
    实测：取键 bug 修好后重跑仍全是 0（inserted=0/skipped=2407）。

    含哈希后：值变了 → 新 data_id → 新版本行（符合本表的版本序列设计）。
    """
    ev1 = FakeEvent("unlock", "2026-09-28",
                    scope={"company_count": 25},
                    metrics={"unlock_market_cap": 100.0})
    ev2 = FakeEvent("unlock", "2026-09-28",
                    scope={"company_count": 25},
                    metrics={"unlock_market_cap": 200.0})
    id1 = next(p for p in calendar_events_to_points([ev1])
               if p.indicator.endswith("market_cap")).data_id
    id2 = next(p for p in calendar_events_to_points([ev2])
               if p.indicator.endswith("market_cap")).data_id
    assert id1 != id2, (
        "值不同但 data_id 相同 —— 后续修正会被主键冲突静默忽略"
    )


def test_same_input_same_data_id():
    """同样输入必须产出同样 data_id（幂等，重复同步不产生垃圾版本）。"""
    ev = FakeEvent("unlock", "2026-09-28",
                   scope={"company_count": 25},
                   metrics={"unlock_market_cap": 100.0})
    a = calendar_events_to_points([ev])[0].data_id
    b = calendar_events_to_points([ev])[0].data_id
    assert a == b


# ============================================================
# 溯源与置信度（AGENTS.md：数据点四件套）
# ============================================================


def test_points_carry_provenance():
    """每个日历数据点必须带溯源四件套。"""
    ev = FakeEvent("unlock", "2026-09-28", scope={"company_count": 3},
                   metrics={"unlock_market_cap": 1.0})
    for p in calendar_events_to_points([ev]):
        assert p.source_name, "缺 source_name"
        assert p.source_url, "缺 source_url"
        assert p.fetch_time, "缺 fetch_time"
        assert p.raw_content_hash, "缺 raw_content_hash"


def test_certainty_maps_to_confidence():
    """`rule`（交易所规则确定）的置信度应高于 `scheduled`（可改期）。"""
    rule_ev = FakeEvent("unlock", "2026-09-28",
                        scope={"company_count": 1},
                        metrics={"unlock_market_cap": 1.0}, certainty="rule")
    sched_ev = FakeEvent("earnings", "2026-10-01",
                         scope={"company_count": 1},
                         certainty="scheduled")
    c_rule = calendar_events_to_points([rule_ev])[0].confidence
    c_sched = calendar_events_to_points([sched_ev])[0].confidence
    assert c_rule > c_sched, "解禁日（规则确定）置信度应高于财报预约"


def test_certainty_recorded_in_extra():
    """certainty 要写进 extra，让下游知道"这个日期能不能信"。"""
    ev = FakeEvent("unlock", "2026-09-28", scope={"company_count": 1},
                   metrics={"unlock_market_cap": 1.0}, certainty="rule")
    extra = calendar_events_to_points([ev])[0].extra
    assert extra["certainty"] == "rule"
    assert extra["certainty_zh"] == "规则确定"
    assert extra["from_calendar"] is True


def test_top_stocks_kept_and_sorted():
    """`extra.top_stocks` 只留最大的几只（省 token），且按市值倒序。"""
    ev = FakeEvent(
        "unlock", "2026-10-28",
        scope={"company_count": 3, "stocks": [
            {"name": "小", "market_cap": 1.0},
            {"name": "大", "market_cap": 100.0},
            {"name": "中", "market_cap": 50.0},
        ]},
        metrics={"unlock_market_cap": 151.0})
    top = calendar_events_to_points([ev])[0].extra["top_stocks"]
    assert [s["name"] for s in top] == ["大", "中", "小"]
    assert _top_stock_cap(ev) == 100.0


# ============================================================
# ② 登记：YAML + 调度作业
# ============================================================


def test_calendar_indicators_registered_in_yaml():
    """★ 日历指标必须登记在 `configs/indicators.yaml`（用户要求的"加入索引清单"）。

    没登记 → SmartFetcher 判"未登记" → 走 daily/24h 兜底
    → 每次分析都现拉网络（就是用户说的"下次取不到"）。
    """
    from src.infrastructure.catalog import get_registry, reset_registry_for_test

    reset_registry_for_test()
    r = get_registry()
    for ind in ("cal:unlock:market_cap", "cal:unlock:company_count",
                "cal:unlock:top_stock_cap", "cal:earnings:company_count"):
        meta = r.get(ind)
        assert meta is not None, f"{ind} 未登记在 indicators.yaml"
        assert meta.category == "calendar", f"{ind} 分类应为 calendar"
        assert meta.frequency == "daily"


def test_calendar_has_dedicated_job():
    """★ 日历必须有**专用调度作业**（不能只靠请求时现拉）。

    日历数据来自结构化 API（东财+巨潮双源），不是 A01 按 indicator
    能直连的 —— 硬塞进批量链会得到"无连接器支持"的静默失败。

    ⚠️ **必须快照并恢复 `JOB_REGISTRY`**：`install_catalog_jobs()` 会写全局表，
    不恢复的话会污染同进程的其它测试
    （实测：`test_scheduler.py::test_beat_schedule_built_from_registry`
     在全量跑时红、单独跑绿 —— 就是被这里污染的）。
    """
    from src.scheduler.catalog_jobs import (
        CALENDAR_JOB_CRON,
        CALENDAR_JOB_NAME,
        FREQUENCY_TO_CRON,
        install_catalog_jobs,
    )
    from src.scheduler.registry import JOB_REGISTRY

    snapshot = dict(JOB_REGISTRY)          # ★ 快照
    try:
        install_catalog_jobs()
        assert CALENDAR_JOB_NAME in JOB_REGISTRY, "日历专用作业未注册"
        spec = JOB_REGISTRY[CALENDAR_JOB_NAME]
        assert spec.kind == "calendar_sync"
        assert spec.cron == CALENDAR_JOB_CRON
        # 盘前跑（早于一切分析），否则当日分析拿不到当日解禁
        hour = int(CALENDAR_JOB_CRON.split()[1])
        assert hour < 9, f"日历作业 {hour} 点才跑，晚于开盘"
        del FREQUENCY_TO_CRON
    finally:
        JOB_REGISTRY.clear()               # ★ 恢复
        JOB_REGISTRY.update(snapshot)


def test_calendar_sync_kind_has_executor_branch():
    """`calendar_sync` 必须在 jobs.py 有分发分支。

    本项目实测过：作业声明了但执行器没分支 → 每次记"未知作业类型"失败，
    且**看起来像"正常跑了但没数据"**。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "scheduler" / "jobs.py"
    text = src.read_text(encoding="utf-8")
    assert 'spec.kind == "calendar_sync"' in text
    assert "_sync_calendar" in text


def test_calendar_covered_by_coverage_check():
    """覆盖率自检不能把日历指标算成"未覆盖"。"""
    from src.scheduler.catalog_jobs import catalog_coverage

    cov = catalog_coverage()
    assert cov["calendar_by_design"] > 0, "应有日历指标"
    assert not cov["uncovered"], (
        f"有未覆盖指标：{cov['uncovered'][:5]} —— "
        "日历类应由 CALENDAR_JOB_NAME 专用作业覆盖"
    )


def test_kind_metrics_mapping():
    """`_KIND_METRICS` 只覆盖 unlock / earnings（其余无值可比）。"""
    assert set(_KIND_METRICS) == {"unlock", "earnings"}
    assert CAL_PREFIX == "cal:"
