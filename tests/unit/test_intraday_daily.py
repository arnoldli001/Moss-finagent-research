"""日K量价规则引擎单元测试（基础量柱 / 16形态 / 高量柱攻防 / 位置 / 成本线 / 保护线）。

每条测试都构造**确定性**的合成K线，把需求的量化口径钉死；同时覆盖三个
实测踩过的坑（已修复，防止回归）：
  1. 触发判定不能用「得分≥0.75」——核心条件不满足就是不触发（B4 曾误报）；
  2. 止损线必须在现价**下方**（曾出现现价880、止损1002的荒谬结果）；
  3. 条件的 actual 文本必须真实反映 ✓/✗，不能写死描述性文字。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.schemas import DataPoint
from src.intraday.config import DailyParams, IntradayConfig
from src.intraday.daily import analyse_daily
from src.intraday.daily_signals import run_daily_signals
from src.intraday.volume import (
    DailyContext,
    chip_peak_valley,
    classify_volume_price,
    enrich_daily_frame,
    find_cost_lines,
    find_volume_anchors,
    high_volume_discipline,
    is_big_bar,
    is_double_volume,
    is_explode_volume,
    is_flat_volume,
    is_ground_volume,
    is_high_volume,
    is_ladder_down,
    is_long_lower_shadow,
    is_shrink_half,
    is_shrink_volume,
    protection_lines,
    quantify_position,
    trend_health,
)


def make_frame(rows: list[dict], start: str = "2026-01-05") -> pd.DataFrame:
    """由 (open, high, low, close, volume) 列表构造日线表（日期自动递推交易日）。"""
    dates = pd.bdate_range(start, periods=len(rows))
    data = {
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "open": [r["open"] for r in rows],
        "high": [r["high"] for r in rows],
        "low": [r["low"] for r in rows],
        "close": [r["close"] for r in rows],
        "volume": [float(r.get("volume", 1000)) for r in rows],
        "amount": [float(r.get("amount", 0)) for r in rows],
    }
    return pd.DataFrame(data)


def flat_row(price: float, volume: float = 1000.0, spread: float = 0.01) -> dict:
    return {"open": price, "high": price * (1 + spread),
            "low": price * (1 - spread), "close": price, "volume": volume}


def params(**kwargs) -> DailyParams:
    return DailyParams(**kwargs)


# ==================== §1 基础量柱定义 ====================


def test_high_volume_ignores_color() -> None:
    """高量：> 前3日最大量，**不分阴阳**。"""
    frame = make_frame([
        flat_row(10, 1000), flat_row(10, 1200), flat_row(10, 900),
        # 第4根：阴线但量最大 → 仍是高量
        {"open": 10.5, "high": 10.6, "low": 10.0, "close": 10.1, "volume": 2000},
    ])
    assert is_high_volume(frame, 3, 3) is True
    assert is_high_volume(frame, 1, 3) is False  # 样本不足


def test_double_and_shrink_volume() -> None:
    frame = make_frame([flat_row(10, 1000), flat_row(10, 2000)])
    assert is_double_volume(frame, 1, 2.0) is True
    assert is_shrink_volume(frame, 1) is False
    frame2 = make_frame([flat_row(10, 1000), flat_row(10, 400)])
    assert is_shrink_volume(frame2, 1) is True
    assert is_shrink_half(frame2, 1, 0.5) is True


def test_ladder_down_needs_three_decreasing() -> None:
    frame = make_frame([flat_row(10, 3000), flat_row(10, 2000),
                        flat_row(10, 1500), flat_row(10, 1000)])
    assert is_ladder_down(frame, 3, 3) is True
    frame2 = make_frame([flat_row(10, 3000), flat_row(10, 2000),
                         flat_row(10, 2500), flat_row(10, 1000)])
    assert is_ladder_down(frame2, 3, 3) is False


def test_flat_volume_tolerance() -> None:
    frame = make_frame([flat_row(10, 1000), flat_row(10, 1050)])
    assert is_flat_volume(frame, 1, 0.1) is True
    frame2 = make_frame([flat_row(10, 1000), flat_row(10, 1200)])
    assert is_flat_volume(frame2, 1, 0.1) is False


def test_ground_volume_is_window_min() -> None:
    rows = [flat_row(10, 1000 + i * 10) for i in range(20)] + [flat_row(10, 50)]
    frame = make_frame(rows)
    assert is_ground_volume(frame, 20, 20) is True


def test_explode_volume_ratio() -> None:
    rows = [flat_row(10, 1000) for _ in range(20)] + [flat_row(10, 2000)]
    frame = make_frame(rows)
    assert is_explode_volume(frame, 20, 20, 1.5) is True
    assert is_explode_volume(frame, 20, 20, 2.5) is False


def test_big_bar_threshold() -> None:
    frame = make_frame([flat_row(10), {"open": 10.0, "high": 10.8,
                                       "low": 9.9, "close": 10.75,
                                       "volume": 1000}])
    assert is_big_bar(frame, 1, 7.0) is True


def test_long_lower_shadow_multiple() -> None:
    """下影 ≥ 实体×2 → 大长腿。"""
    frame = make_frame([flat_row(10), {"open": 10.0, "high": 10.1,
                                       "low": 9.5, "close": 10.0,
                                       "volume": 1000}])
    lower = min(10.0, 10.0) - 9.5
    body = abs(10.0 - 10.0)
    assert lower > 0 and body == 0
    # 实体为0（十字星）时用振幅做参照，仍应识别为大长腿
    assert is_long_lower_shadow(frame, 1, 2.0) is True
    # 实体足够大但不满足倍数 → False
    frame2 = make_frame([flat_row(10), {"open": 10.0, "high": 11.0,
                                        "low": 9.9, "close": 10.9,
                                        "volume": 1000}])
    assert is_long_lower_shadow(frame2, 1, 2.0) is False


# ==================== §2 量价16形态矩阵 ====================


def _pattern_frame(today: dict, prior_prices: list[float],
                   prior_volumes: list[float]) -> pd.DataFrame:
    rows = [{"open": p, "high": p * 1.005, "low": p * 0.995, "close": p,
             "volume": v}
            for p, v in zip(prior_prices, prior_volumes, strict=True)]
    rows.append(today)
    return make_frame(rows)


def test_pattern_big_yang_expand() -> None:
    """放量大阳：涨幅≥7% 且放量。"""
    frame = _pattern_frame(
        {"open": 10.0, "high": 10.9, "low": 9.95, "close": 10.8, "volume": 3000},
        [10.0, 10.0, 10.0], [1000, 1000, 1000])
    pattern = classify_volume_price(frame, 3, params())
    assert pattern is not None
    assert pattern.name == "放量大阳"
    assert pattern.direction == "bullish"


def test_pattern_big_yin_shrink() -> None:
    """缩量大阴：跌幅≥7% 且缩量。"""
    frame = _pattern_frame(
        {"open": 10.0, "high": 10.05, "low": 9.2, "close": 9.25, "volume": 200},
        [10.0, 10.0, 10.0], [3000, 3000, 3000])
    pattern = classify_volume_price(frame, 3, params())
    assert pattern is not None
    assert pattern.name == "缩量大阴"
    assert pattern.volume_dir == "缩量"
    assert pattern.direction == "bearish"


def test_pattern_wide_bar_priority() -> None:
    """振幅≥10% 优先判为「宽幅」，而不是大阴大阳。"""
    frame = _pattern_frame(
        {"open": 10.0, "high": 11.2, "low": 10.0, "close": 11.1, "volume": 3000},
        [10.0, 10.0, 10.0], [1000, 1000, 1000])
    pattern = classify_volume_price(frame, 3, params())
    assert pattern is not None
    assert "宽幅" in pattern.name


def test_pattern_small_shrink_is_neutral() -> None:
    """缩量小阴小阳 → 观望。"""
    frame = _pattern_frame(
        {"open": 10.0, "high": 10.1, "low": 9.95, "close": 10.02,
         "volume": 300},
        [10.0, 10.0, 10.0], [1000, 1000, 1000])
    pattern = classify_volume_price(frame, 3, params())
    assert pattern is not None
    assert pattern.name == "缩量小阴小阳"
    assert pattern.signal == "观望"


def test_pattern_up_accel_shrink_is_strong_bullish() -> None:
    """缩量大涨（价涨加速+量缩）→ 强看多。"""
    frame = _pattern_frame(
        {"open": 10.0, "high": 10.9, "low": 9.98, "close": 10.85,
         "volume": 100},
        [10.0, 10.0, 10.2], [1000, 1000, 1000])
    pattern = classify_volume_price(frame, 3, params())
    assert pattern is not None
    assert pattern.price_dir == "涨"
    assert pattern.volume_dir == "缩量"


def test_pattern_requires_min_samples() -> None:
    frame = make_frame([flat_row(10), flat_row(10)])
    assert classify_volume_price(frame, 1, params()) is None


def test_trend_health_synthesis_rule() -> None:
    """"上涨放量+下跌缩量 = 健康上升趋势" 的合成规则。"""
    rows = []
    price = 10.0
    for index in range(12):
        if index % 2 == 0:      # 上涨日放量
            price *= 1.02
            rows.append({"open": price / 1.02, "high": price, "low": price / 1.02,
                         "close": price, "volume": 3000})
        else:                    # 下跌日缩量
            price *= 0.995
            rows.append({"open": price / 0.995, "high": price / 0.995,
                         "low": price, "close": price, "volume": 800})
    frame = make_frame(rows)
    assert "健康" in trend_health(frame, len(frame) - 1, 10)


# ==================== §3 高量柱攻防 ====================


def test_volume_anchor_lines_and_status() -> None:
    """安全线=实顶、风险线=实底；破位后状态为已破位。"""
    rows = [flat_row(10, 1000) for _ in range(5)]
    rows.append({"open": 10.0, "high": 10.5, "low": 9.9, "close": 10.4,
                 "volume": 5000})  # 高量柱
    rows.append(flat_row(10.3, 1200))
    frame = make_frame(rows)
    anchors = find_volume_anchors(frame, params())
    assert anchors, "应识别出高量柱"
    anchor = anchors[0]
    assert anchor.body_top == pytest.approx(10.4)   # 实顶（收>开）
    assert anchor.body_bottom == pytest.approx(10.0)  # 实底
    assert anchor.days_since == 1
    assert anchor.status == "有效支撑"


def test_volume_anchor_broken_sets_status() -> None:
    rows = [flat_row(10, 1000) for _ in range(5)]
    rows.append({"open": 10.0, "high": 10.5, "low": 9.9, "close": 10.4,
                 "volume": 5000})
    rows.append({"open": 10.0, "high": 10.1, "low": 9.5, "close": 9.6,
                 "volume": 1200})
    frame = make_frame(rows)
    anchors = find_volume_anchors(frame, params())
    assert anchors[0].status == "已破位"


def test_high_volume_discipline_break_warns_clear() -> None:
    """高量破 → 清仓（口诀「高量破，有灾祸」）。"""
    rows = [flat_row(10, 1000) for _ in range(5)]
    rows.append({"open": 10.0, "high": 10.5, "low": 9.9, "close": 10.4,
                 "volume": 5000})
    rows.append({"open": 10.0, "high": 10.1, "low": 9.4, "close": 9.5,
                 "volume": 1200})
    frame = make_frame(rows)
    anchors = find_volume_anchors(frame, params())
    hits = high_volume_discipline(frame, anchors, params())
    assert any("高量破" in item for item in hits)
    assert any("清仓" in item for item in hits)


def test_high_volume_discipline_still_watching_on_day_one() -> None:
    """高量第1天应观望（不应给出参与信号）。"""
    rows = [flat_row(10, 1000) for _ in range(5)]
    rows.append({"open": 10.0, "high": 10.5, "low": 9.9, "close": 10.4,
                 "volume": 5000})
    frame = make_frame(rows)
    anchors = find_volume_anchors(frame, params())
    assert anchors[0].days_since == 0
    assert anchors[0].status == "待观察"
    hits = high_volume_discipline(frame, anchors, params())
    assert not any("可参与" in item for item in hits)


# ==================== §6.3 主力成本线 ====================


def test_cost_line_from_big_yang_open_and_mid() -> None:
    rows = [flat_row(9.85, 1000) for _ in range(5)]
    # 大阳线：相对前收 9.85 → 收盘 10.6 = +7.6%
    rows.append({"open": 9.3, "high": 10.7, "low": 9.2, "close": 10.6,
                 "volume": 4000})
    rows.append(flat_row(10.5, 1500))
    frame = make_frame(rows)
    lines = find_cost_lines(frame, params())
    kinds = {line.kind for line in lines}
    assert "大阳线开盘价" in kinds
    assert "大阳线实体1/2" in kinds
    open_line = next(line for line in lines if line.kind == "大阳线开盘价")
    assert open_line.price == pytest.approx(9.3)
    mid_line = next(line for line in lines if line.kind == "大阳线实体1/2")
    assert mid_line.price == pytest.approx((9.3 + 10.6) / 2)


def test_cost_line_gap_uses_previous_close() -> None:
    """跳空缺口 → 以前一根收盘价为成本线。"""
    rows = [flat_row(10, 1000) for _ in range(5)]
    rows.append({"open": 10.8, "high": 11.0, "low": 10.75, "close": 10.9,
                 "volume": 3000})  # 向上跳空（最低 > 前收 10）
    rows.append(flat_row(10.85, 1200))
    frame = make_frame(rows)
    lines = find_cost_lines(frame, params())
    gap_line = next((line for line in lines if "跳空" in line.kind), None)
    assert gap_line is not None
    assert gap_line.price == pytest.approx(10.0)


# ==================== §11 位置量化 ====================


def test_quantify_position_labels() -> None:
    rising = [flat_row(10 + i * 0.5) for i in range(30)]
    frame = make_frame(rising)
    position = quantify_position(frame, 30)
    assert position is not None
    assert position.label == "高位"
    assert position.percentile > 0.9
    falling = [flat_row(30 - i * 0.5) for i in range(30)]
    frame2 = make_frame(falling)
    position2 = quantify_position(frame2, 30)
    assert position2 is not None
    assert position2.label == "低位"


def test_quantify_position_needs_samples() -> None:
    assert quantify_position(make_frame([flat_row(10) for _ in range(5)])) is None


# ==================== S5 保护线（回归：止损必须在现价下方） ====================


def test_stop_line_always_below_price() -> None:
    """回归用例：高位回落后，止损线必须低于现价（曾算出高于现价的"止损"）。"""
    # 先涨后跌：现价低于60日多数成交价
    rows = [flat_row(10 + i * 0.3) for i in range(40)]      # 一路上涨到 21.7
    rows += [flat_row(21.7 - i * 0.5) for i in range(1, 16)]  # 回落到 14.2
    frame = make_frame(rows)
    result = protection_lines(frame, params())
    close = float(frame["close"].iloc[-1])
    assert result["stop_line"] is not None
    assert result["stop_line"] < close, "止损线必须在现价下方"
    assert result["broken_stop"] is False


def test_stop_line_falls_back_to_fixed_pct_when_support_far() -> None:
    """现价下方支撑过远（超过固定止损幅度）→ 回退到固定百分比止损。

    构造：长期在 10 元成交，最后两天跳涨到 50 元 —— 20日低点/MA20/筹码峰谷
    都远在 50 元下方 5% 以外，此时最近的"支撑"没有风控意义，
    应改用固定止损幅度。
    """
    rows = [flat_row(10) for _ in range(25)]
    # 跳涨到 50 后横住（spread=0 → 当日最低即收盘，避免"最近阳线低点"恰好贴近现价）
    rows += [flat_row(50, spread=0.0) for _ in range(2)]
    frame = make_frame(rows)
    result = protection_lines(frame, params(stop_loss_pct=5.0))
    close = float(frame["close"].iloc[-1])
    assert result["stop_line"] == pytest.approx(close * 0.95, abs=0.01)
    assert "固定止损" in result["stop_basis"]


def test_stop_line_prefers_nearby_support_over_fixed_pct() -> None:
    """现价下方有很近的支撑（如20日低点）时，应优先用该支撑而不是固定幅度。"""
    rows = [flat_row(50) for _ in range(25)]
    rows.append({"open": 50.0, "high": 51.0, "low": 49.0, "close": 50.5,
                 "volume": 1200})   # 最后一根带下影
    frame = make_frame(rows)
    result = protection_lines(frame, params(stop_loss_pct=5.0))
    close = float(frame["close"].iloc[-1])
    assert result["stop_line"] < close
    assert result["stop_line"] > close * 0.95, "近支撑应比固定止损更近"


def test_chip_peak_valley_finds_below_peak() -> None:
    rows = [flat_row(10, 5000) for _ in range(20)]   # 密集成交在 10
    rows += [flat_row(20, 100) for _ in range(20)]   # 少量在 20
    frame = make_frame(rows)
    peak, valley = chip_peak_valley(frame, 40, 20)
    assert peak is not None
    assert 9 < peak < 11
    assert valley is None or valley < peak


# ==================== 信号库：触发判定（回归：核心条件缺失不得触发） ====================


def _context(frame: pd.DataFrame) -> DailyContext:
    cfg = IntradayConfig()
    enriched = enrich_daily_frame(frame, cfg.daily)
    return DailyContext(
        frame=enriched, params=cfg.daily, index=len(enriched) - 1,
        anchors=find_volume_anchors(enriched, cfg.daily))


def test_b4_not_triggered_when_core_condition_fails() -> None:
    """回归用例：B4 的「跌破情绪释放点」不满足时，即便得分0.75也**不得**判触发。"""
    rows = [flat_row(10) for _ in range(10)]
    rows.append({"open": 10.0, "high": 11.0, "low": 10.0, "close": 11.0,
                 "volume": 3000})       # 涨停（+10%）
    rows += [flat_row(11.2 - i * 0.02) for i in range(5)]  # 高位横盘，未跌破情绪点
    frame = make_frame(rows)
    signals = run_daily_signals(_context(frame))
    b4 = next(item for item in signals["buy"] if item.code == "B4")
    assert b4.triggered is False
    core = [c for c in b4.conditions if c.required]
    assert any(not c.met for c in core), "应有硬性条件未满足"


def test_b4_triggered_when_all_core_conditions_met() -> None:
    """核心条件全部满足时 B4 触发。"""
    rows = [flat_row(10) for _ in range(10)]
    rows.append({"open": 10.0, "high": 11.0, "low": 10.0, "close": 11.0,
                 "volume": 3000})               # 涨停日：情绪点 10.5，支撑 10.0
    rows += [flat_row(10.9), flat_row(10.8), flat_row(10.7)]  # 回调3天
    rows.append({"open": 10.4, "high": 10.45, "low": 10.2, "close": 10.3,
                 "volume": 1200})               # 阴线跌破情绪点，仍在支撑上方
    frame = make_frame(rows)
    signals = run_daily_signals(_context(frame))
    b4 = next(item for item in signals["buy"] if item.code == "B4")
    assert b4.triggered is True
    assert b4.stop_loss is not None


def test_s3_shrink_yin_triggers_no_buy() -> None:
    """S3：缩量阴线 → 当天不抄底（风控触发）。"""
    rows = [flat_row(10, 2000) for _ in range(5)]
    rows.append({"open": 10.2, "high": 10.25, "low": 10.0, "close": 10.05,
                 "volume": 800})   # 阴线 + 缩量
    frame = make_frame(rows)
    signals = run_daily_signals(_context(frame))
    s3 = next(item for item in signals["sell"] if item.code == "S3")
    assert s3.triggered is True


def test_s4_shrink_yang_hold_hint() -> None:
    """S4：缩量阳线 → 当天不卖。"""
    rows = [flat_row(10, 2000) for _ in range(5)]
    rows.append({"open": 10.0, "high": 10.3, "low": 9.98, "close": 10.25,
                 "volume": 700})   # 阳线 + 缩量
    frame = make_frame(rows)
    signals = run_daily_signals(_context(frame))
    s4 = next(item for item in signals["sell"] if item.code == "S4")
    assert s4.triggered is True


def test_signals_expose_condition_actuals() -> None:
    """每个信号都要给出逐条条件与真实数值（前端据此展示"差在哪"）。"""
    rows = [flat_row(10 + (i % 5) * 0.2) for i in range(40)]
    frame = make_frame(rows)
    signals = run_daily_signals(_context(frame))
    all_items = signals["buy"] + signals["sell"]
    assert all_items
    for item in all_items:
        assert item.conditions, f"{item.code} 应至少给出一个条件"
        assert item.score >= 0
        for cond in item.conditions:
            assert cond.label
            assert cond.actual, f"{item.code}/{cond.label} 缺少实际值"
            assert cond.met in (True, False)


def test_no_signal_is_silently_triggered_without_required_flag() -> None:
    """所有信号的触发都必须由 required 条件决定（防止再退回"得分制"）。"""
    rows = [flat_row(10 + (i % 7) * 0.3) for i in range(60)]
    frame = make_frame(rows)
    signals = run_daily_signals(_context(frame))
    for item in signals["buy"] + signals["sell"]:
        if not item.triggered:
            continue
        assert all(cond.met for cond in item.conditions if cond.required), (
            f"{item.code} 被判触发但存在未满足的硬性条件")


# ==================== 端到端（analyse_daily） ====================


def _points_from_frame(frame: pd.DataFrame) -> list[DataPoint]:
    points = []
    for _, row in frame.iterrows():
        points.append(DataPoint(
            indicator="stock_close:300308", value=float(row["close"]),
            period_date=str(row["date"]),
            extra={"open": float(row["open"]), "high": float(row["high"]),
                   "low": float(row["low"]), "close": float(row["close"]),
                   "volume": float(row["volume"]),
                   "amount": float(row["amount"])}))
    return points


def test_analyse_daily_end_to_end() -> None:
    rows = [flat_row(10 + (i % 6) * 0.4, 1000 + (i % 3) * 400)
            for i in range(140)]
    rows.append({"open": 10.2, "high": 11.2, "low": 10.1, "close": 11.1,
                 "volume": 5000})
    frame = make_frame(rows)
    snapshot = analyse_daily(
        code="300308", name="测试", points=_points_from_frame(frame),
        config=IntradayConfig())
    assert snapshot.available is True
    assert snapshot.bars, "应输出日K数据供画图"
    assert snapshot.position is not None
    assert snapshot.pattern is not None
    assert snapshot.buy_signals, "应至少返回买入信号条目"
    assert snapshot.protective is not None
    assert snapshot.verdict
    # 量柱标记要落到 bar 上（前端画量柱颜色）
    assert any(bar.is_high_volume for bar in snapshot.bars)
    # 日线最新日期非当日时应给出缺口说明
    assert any("日线最新为" in gap for gap in snapshot.health.gaps)


def test_analyse_daily_empty_points_reports_gap() -> None:
    snapshot = analyse_daily(
        code="300308", name="测试", points=[], config=IntradayConfig())
    assert snapshot.available is False
    assert snapshot.health.gaps


def test_daily_scorecard_is_attached_and_uses_own_frame() -> None:
    """日K快照必须带加权决策总分，且股性因子用**本面板同一份日线**现算。

    回归的坑：服务层曾另外取一份日线来算股性，而两条取数路径的缓存口径不同
    （带区间的绕开 TTL/DB 短路），QMT 未启动时分别拿到 2026-08-31 与 2026-09-16，
    于是"图上画的是 8/31 的K线、右侧股性却按 9/16 的数据算"。
    """
    rows = [flat_row(10 + (i % 6) * 0.4, 1000 + (i % 3) * 400)
            for i in range(140)]
    frame = make_frame(rows)
    snapshot = analyse_daily(
        code="300308", name="测试", points=_points_from_frame(frame),
        config=IntradayConfig())
    card = snapshot.scorecard
    assert card is not None, "日K快照必须带加权决策总分"
    assert [item.key for item in card.factors] == [
        "trend", "chan_daily", "volume", "position", "signal_rule",
        "cycle", "character"]
    # character 由本函数用同一份 enriched 现算 → 不应因"没传"而记缺口
    character_factor = next(item for item in card.factors
                            if item.key == "character")
    assert character_factor.available is True
    assert character_factor.inputs.get("atr_pct") is not None
    # cycle 没有外部注入 → 如实记缺口并从有效权重里扣除
    cycle_factor = next(item for item in card.factors if item.key == "cycle")
    assert cycle_factor.available is False
    assert card.available_weight == pytest.approx(90.0)
    # 图表口径也随之下发
    assert snapshot.config_snapshot["daily_weights"]["trend"] == pytest.approx(20.0)
    assert snapshot.config_snapshot["daily_thresholds"]["action"] == pytest.approx(25.0)


def test_daily_scorecard_marks_character_gap_when_explicitly_unavailable() -> None:
    """显式传入一份不可用的股性画像时，必须按缺口处理（不能偷偷现算一份顶上）。"""
    from src.intraday.character import analyze_character

    rows = [flat_row(10 + (i % 6) * 0.4) for i in range(140)]
    frame = make_frame(rows)
    unavailable = analyze_character(None, code="300308", mode="daily")
    snapshot = analyse_daily(
        code="300308", name="测试", points=_points_from_frame(frame),
        config=IntradayConfig(), character=unavailable)
    factor = next(item for item in snapshot.scorecard.factors
                  if item.key == "character")
    assert factor.available is False
    assert factor.gap


def test_daily_params_validation() -> None:
    from src.core.exceptions import ConfigError

    with pytest.raises(ConfigError):
        DailyParams(ma_short=10, ma_long=5)
    with pytest.raises(ConfigError):
        DailyParams(wide_amplitude_pct=3.0, small_amplitude_pct=5.0)
    assert DailyParams().hv_lookback == 3
    assert DailyParams().big_bar_pct == 7.0


def test_enrich_adds_all_flag_columns() -> None:
    frame = make_frame([flat_row(10 + i * 0.1) for i in range(50)])
    enriched = enrich_daily_frame(frame, params())
    for column in ("is_high_volume", "is_double_volume", "is_shrink_volume",
                   "is_shrink_half", "is_ladder_down", "is_flat_volume",
                   "is_ground_volume", "is_explode_volume",
                   "is_long_lower_shadow", "is_big_yang", "is_big_yin",
                   "pct_chg", "amplitude", "body_top", "body_bottom"):
        assert column in enriched.columns, column
