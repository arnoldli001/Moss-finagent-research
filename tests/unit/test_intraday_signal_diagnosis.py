"""触发诊断：为什么"价格摸到档位却没有信号"。

## 用户诉求（2026-09-16）

> 剑桥科技今日开盘在回踩虚线以下，为什么没触发回踩信号？股价涨到冲高线以上
> 为什么没冲高信号？

实测该标的当日：触及回踩线 **56** 次、触及冲高线 **60** 次，但**当日总分最高只有 +8.2**
（提示线 ±20、动手线 ±30）→ 按规则不出信号。

触发条件是 **「价格触及档位」AND「总分达标」**（`engine.decide_signal` /
`engine.build_markers`）。问题在于：只给"当前总分"，用户无法知道"触点那一刻是多少分"，
于是只能猜。修法是把逐bar总分（`replay_totals` 的同一条序列）随快照返回，
前端据此直接写出结论：触及几次、触点处最高/最低总分、差多少。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.intraday.config import IntradayConfig
from src.intraday.engine import build_markers
from src.intraday.models import ScoreCard


def _scorecard(total: float = 0.0, config: IntradayConfig | None = None) -> ScoreCard:
    cfg = config or IntradayConfig()
    return ScoreCard(
        total=total, threshold_action=cfg.thresholds.action,
        threshold_hint=cfg.thresholds.hint, zone="neutral", verdict="测试",
        factors=[], weights_sum=100.0, available_weight=100.0)


def _levels():
    from src.intraday.engine import compute_levels

    return compute_levels(
        price=100.0, box_high=108.0, box_low=96.0, box_span_days=20,
        boll_lower=97.0, boll_upper=107.0, config=IntradayConfig())


def test_touch_without_score_does_not_trigger() -> None:
    """价格跌破回踩线，但总分只有 +5（< 提示线 20）→ 不出信号。

    这正是 603083 当天的情形：线被摸了 56 次，总分却始终在 +8 附近。
    """
    levels = _levels()
    replay = pd.DataFrame({
        "ts": ["2026-09-16 09:31", "2026-09-16 09:32"],
        "close": [95.0, 95.5],           # 双双在回踩线(96)下方
        "low": [94.8, 95.2],
        "total": [5.0, -3.0],            # 都远未达标
    })
    markers = build_markers(
        replay=replay, levels=levels, scorecard=_scorecard(),
        config=IntradayConfig())
    assert markers == []


def test_touch_with_score_triggers() -> None:
    """同样的价格，总分达标（≥ 提示线）→ 空心提示；≥ 动手线 → 实心。"""
    levels = _levels()
    config = IntradayConfig()
    replay = pd.DataFrame({
        "ts": ["2026-09-16 09:31", "2026-09-16 09:32"],
        "close": [95.0, 95.5],
        "low": [94.8, 95.2],
        "total": [25.0, 35.0],
    })
    markers = build_markers(
        replay=replay, levels=levels, scorecard=_scorecard(), config=config)
    kinds = [(m.kind, m.strength) for m in markers]
    assert kinds == [("low_buy", "hollow"), ("low_buy", "solid")]


def test_high_sell_needs_negative_score() -> None:
    """冲高同样要"价格 + 总分"两条：涨到冲高线上方但总分是正的 → 不出冲高。"""
    levels = _levels()
    config = IntradayConfig()
    replay = pd.DataFrame({
        "ts": ["2026-09-16 10:31", "2026-09-16 10:32"],
        "close": [109.0, 109.5],         # 都在冲高线(108)上方
        "low": [108.6, 109.0],
        "total": [8.0, -35.0],           # 第一条不达标（+8），第二条达标
    })
    markers = build_markers(
        replay=replay, levels=levels, scorecard=_scorecard(), config=config)
    assert [(m.kind, m.strength) for m in markers] == [("high_sell", "solid")]


def test_snapshot_exposes_score_series_for_diagnosis() -> None:
    """快照必须带逐bar总分 —— 否则前端无法回答"触点那一刻是多少分"。"""
    import asyncio

    from src.intraday.service import IntradayService

    service = IntradayService(backend=None)
    snapshot = asyncio.run(service.snapshot("603083", light=True))
    # 数据源在此环境不可用时也要给出结构（空序列），不能抛错
    assert isinstance(snapshot.score_series, list)
    for point in snapshot.score_series:
        assert point.ts
        assert -100.0 <= point.total <= 100.0


def test_score_series_contract_matches_frontend_expectations() -> None:
    """前端按 (ts, price, total) 做触点统计：字段名与语义要写死在这里。"""
    from src.intraday.models import ScorePoint

    point = ScorePoint(ts="2026-09-16 09:31", price=95.0, total=5.0)
    assert point.model_dump() == {"ts": "2026-09-16 09:31", "price": 95.0, "total": 5.0}


@pytest.mark.parametrize("total,expected", [
    (19.9, 0), (-19.9, 0), (20.0, 1), (30.0, 1),
])
def test_threshold_boundary_is_inclusive(total: float, expected: int) -> None:
    """提示线是**闭区间**：总分恰好等于阈值也算达标（与后端 `>= hint` 一致）。"""
    levels = _levels()
    config = IntradayConfig()
    replay = pd.DataFrame({
        "ts": ["2026-09-16 09:31"], "close": [95.0], "low": [94.8],
        "total": [total],
    })
    markers = build_markers(
        replay=replay, levels=levels, scorecard=_scorecard(), config=config)
    assert len(markers) == expected
