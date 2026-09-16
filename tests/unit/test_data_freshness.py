"""DataFreshnessEvaluator 单测——覆盖文档 7.1 所有发布频率 + 行业周期倍数。

今天锚定 2026-09-15（硬编码），所有 period_date 相对于此计算。
"""
from __future__ import annotations

from datetime import date

from src.core.data_freshness import DataFreshnessEvaluator

TODAY = date(2026, 9, 15)
eval = DataFreshnessEvaluator()


# ========== 文档示例一：CPI 45 天 ==========

def test_doc_example_cpi_45_days():
    """文档 3.3 示例一：CPI 45 天前 → lagging，conf ≈ 0.47。"""
    fe = eval.evaluate("CPI", "2026-07-31", today=TODAY)
    # 硬截止 filter_days=365 天 → 45 天 < 365，不过期
    assert fe.should_display is True
    # 45 天 / 30 天周期 / multiplier=1.0
    assert fe.days_since == 46  # 2026-07-31 ~ 2026-09-15
    assert fe.publish_cycle_days == 30
    assert fe.multiplier == 1.0
    # exp(-0.5 × 46 / 30) ≈ exp(-0.767) ≈ 0.464 → lagging
    assert fe.status == "lagging"
    assert 0.4 < fe.confidence < 0.7
    assert fe.weight_multiplier == 0.7


# ========== 文档示例三：PE 15 天 ==========

def test_doc_example_pe_15_days_stale():
    """文档示例三：PE 日频 15 天前 → 硬截止内（30 天），conf 极低但 stale 不是 expired。"""
    fe = eval.evaluate("PE(TTM):300308", "2026-08-31", today=TODAY)
    assert fe.days_since == 15
    assert fe.publish_cycle_days == 1
    assert fe.should_display is True  # filter_days=30, 15 < 30
    # exp(-0.5×15/1) = exp(-7.5) ≈ 0.00055 → 被下界 0.001 夹住
    assert fe.confidence <= 0.01
    # stale（status 由 conf 分级，硬截止只决定 should_display）
    assert fe.status in ("stale", "lagging")


# ========== 硬截止线测试 ==========

def test_daily_pe_expired_after_31_days():
    """PE 日频 filter_days=30 → 超过 30 天硬截止 expired。"""
    # 31 天前
    fe = eval.evaluate("PE(TTM):300308", "2026-08-15", today=TODAY)
    assert fe.days_since == 31
    assert fe.should_display is False
    assert fe.status == "expired"
    assert fe.weight_multiplier == 0.0


def test_monthly_cpi_expired_after_366_days():
    """CPI 月频 filter_days=365 → 超过 365 天硬截止 expired。"""
    # 366 天前 ≈ 2025-09-14
    fe = eval.evaluate("CPI", "2025-09-14", today=TODAY)
    assert fe.days_since == 366
    assert fe.should_display is False
    assert fe.status == "expired"

    # 365 天前 ≈ 2025-09-15 → 刚好不过期
    fe2 = eval.evaluate("CPI", "2025-09-15", today=TODAY)
    assert fe2.days_since == 365
    assert fe2.should_display is True  # filter_days=365, 刚好不过线


def test_weekly_inventory_windows():
    """周频库存 publish_cycle=7, filter_days=90。"""
    # 匹配周频前缀 "weekly_inventory"
    # 3 天前 → conf=exp(-0.5*3/7)=exp(-0.214)=0.81 → normal
    fe = eval.evaluate("weekly_inventory:dummy", "2026-09-12", today=TODAY)
    assert fe.publish_cycle_days == 7
    assert fe.days_since == 3
    assert fe.confidence >= 0.7  # normal 或 fresh

    # 91 天前 → expired（filter_days=90）
    fe2 = eval.evaluate("weekly_inventory:dummy", "2026-06-15", today=TODAY)
    assert fe2.days_since == 92
    assert fe2.should_display is False
    assert fe2.status == "expired"


# ========== 行业周期倍数 ==========

def test_semiconductor_1_5x_multiplier():
    """半导体（中周期 multiplier=1.5）衰减更慢。"""
    # 找个匹配的指标 + 行业 hint
    fe_short = eval.evaluate(
        "PE(TTM):300308", "2026-09-01", industry_hint="消费电子", today=TODAY
    )
    fe_mid = eval.evaluate(
        "PE(TTM):300308", "2026-09-01", industry_hint="半导体", today=TODAY
    )
    assert fe_short.multiplier == 1.0
    assert fe_mid.multiplier == 1.5
    # mid multiplier 大 → 衰减慢 → conf 更高
    assert fe_mid.confidence >= fe_short.confidence


def test_shipbuilding_2_0x_multiplier():
    """船舶（长周期 multiplier=2.0）。"""
    fe = eval.evaluate("船舶订单", "2026-01-01", industry_hint="船舶", today=TODAY)
    assert fe.multiplier == 2.0


# ========== db_expired_months ==========

def test_db_expired_months_matches_filter_days():
    """db_expired_months(indicator) 应该等于 filter_days / 30。"""
    assert eval.db_expired_months("CPI") == 365 // 30  # 12
    assert eval.db_expired_months("PE(TTM):300308") == 30 // 30  # 1
    assert eval.db_expired_months("PE(TTM):300308") == 1  # 日频 PE 只有 1 个月


# ========== status icon 分级 ==========

def test_status_classification_boundaries():
    """测试 confidence 边界的分级正确性。"""
    def _classify(conf):
        from src.core.data_freshness import _STATUS_THRESHOLDS
        # 直接用 evaluator 的 classify 逻辑
        for thr, status, icon, weight, _note in _STATUS_THRESHOLDS:
            if conf >= thr:
                return status, icon, weight
        return "expired", "⛔", 0.0

    assert _classify(0.95) == ("fresh", "✅", 1.0)
    assert _classify(0.80) == ("normal", "✅", 1.0)
    assert _classify(0.55) == ("lagging", "⚠️", 0.7)
    assert _classify(0.20) == ("stale", "🔶", 0.3)
    assert _classify(0.05) == ("expired", "⛔", 0.0)
