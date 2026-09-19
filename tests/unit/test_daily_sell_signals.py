"""日K卖出信号单测（S2/S3/S5/S7 + 注册表契约）。

## 这个测试为什么存在

2026-09-17 实测发现：卖出侧原本只有 S2（唯一真卖点，要求"爆量 1.5 倍"或
"跌破 20 日平台"），10 只票 × 30 个交易日的 213 条信号里**真卖点 0 条** ——
用户看到的现象是"最近 30 个交易日只有买没有卖"。

修法是补两条在常规行情也能触发的真卖点（S5 拉升达标止盈、S7 量价背离），
并用这里的用例把"卖出侧至少有一条能在震荡市触发"这件事钉死，
避免以后有人删掉它们又回到"只有买没有卖"。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.intraday.config import DailyParams
from src.intraday.daily_signals import (
    ALL_BUY_SIGNALS,
    ALL_SELL_SIGNALS,
    signal_s5,
    signal_s7,
)
from src.intraday.volume import (
    DailyContext,
    classify_volume_price,
    find_cost_lines,
    find_volume_anchors,
    quantify_position,
)


def _frame(closes: list[float], volumes: list[float] | None = None,
           *, highs: list[float] | None = None,
           lows: list[float] | None = None,
           opens: list[float] | None = None) -> pd.DataFrame:
    """造一份最小可用日线（列名与项目采集链一致）。"""
    count = len(closes)
    volumes = volumes or [1000.0] * count
    opens = opens or list(closes)
    highs = highs or [max(o, c) * 1.001 for o, c in zip(opens, closes, strict=True)]
    lows = lows or [min(o, c) * 0.999 for o, c in zip(opens, closes, strict=True)]
    frame = pd.DataFrame({
        "date": [f"2026{(index // 28) + 1:02d}{(index % 28) + 1:02d}"
                 for index in range(count)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes,
    })
    # 项目里的 flags 依赖这些预计算列，回放路径由 volume 模块填充；单测里补最小集
    frame["is_explode_volume"] = False
    frame["is_high_volume"] = False
    frame["is_double_volume"] = False
    frame["is_shrink_volume"] = False
    frame["is_shrink_half"] = False
    frame["is_ladder_down"] = False
    frame["is_flat_volume"] = False
    frame["is_ground_volume"] = False
    frame["is_long_lower_shadow"] = False
    frame["is_big_yang"] = False
    frame["is_big_yin"] = False
    return frame


def _context(frame: pd.DataFrame) -> DailyContext:
    """按生产同一路径构造上下文（保证规则看到的东西与线上一致）。"""
    params = DailyParams()
    ctx = DailyContext(
        frame=frame, params=params, index=len(frame) - 1,
        anchors=find_volume_anchors(frame, params),
        pattern=None, position=quantify_position(frame),
        cost_lines=find_cost_lines(frame, params))
    ctx.pattern = classify_volume_price(frame, ctx.index, params)
    return ctx


# ==================== 注册表契约 ====================


def test_sell_library_has_more_than_one_kind():
    """卖出侧必须同时有**真卖点**与风控：只有风控就等于"永远不提示卖"。"""
    kinds = set()
    for function in ALL_SELL_SIGNALS:
        # 用一份平稳数据跑一遍，只取 kind 声明（触发与否不重要）
        frame = _frame([10.0 + index * 0.01 for index in range(40)])
        item = function(_context(frame))
        if item is not None:
            kinds.add(item.kind)
    assert "sell" in kinds, f"卖出侧缺少真卖点（当前 kinds={kinds}）"
    assert "risk" in kinds, f"卖出侧缺少风控信号（当前 kinds={kinds}）"


def test_buy_library_unchanged_size():
    """买入库规模不能被误改（B1–B15）。"""
    assert len(ALL_BUY_SIGNALS) == 15


def test_signal_ids_are_unique():
    """信号编号不能重复（前端按 code 展示，重复会串味）。"""
    codes: list[str] = []
    frame = _frame([10.0 + index * 0.02 for index in range(40)])
    for function in [*ALL_BUY_SIGNALS, *ALL_SELL_SIGNALS]:
        item = function(_context(frame))
        if item is not None:
            codes.append(item.code)
    # 用真实生产快照复核会看到全部编号；这里至少保证已产出的不重复
    assert len(codes) == len(set(codes)), f"信号编号重复：{codes}"


# ==================== S5 拉升达标止盈 ====================


def test_s5_triggers_on_big_up_day():
    """当日大涨 ≥5% → 触发卖点（做T兑现），并给出离场参考价。"""
    closes = [10.0] * 30
    closes[-1] = 10.6                       # 当日 +6%
    frame = _frame(closes, opens=[10.0] * 29 + [10.0])
    item = signal_s5(_context(frame))
    assert item is not None
    assert item.kind == "sell"
    assert item.triggered is True
    assert item.entry == pytest.approx(10.6)
    assert item.stop_loss is not None and item.stop_loss < item.entry


def test_s5_triggers_on_high_position_stall():
    """区间高位收阴（滞涨）→ 同样触发卖点。"""
    closes = [10.0 + index * 0.5 for index in range(39)] + [28.0]
    opens = [10.0 + index * 0.5 for index in range(39)] + [29.6]   # 高开低走收阴
    frame = _frame(closes, opens=opens)
    item = signal_s5(_context(frame))
    assert item is not None and item.triggered is True
    assert item.kind == "sell"


def test_s5_silent_in_quiet_range():
    """横盘小幅波动时不该乱报卖点（否则又变成噪音源）。"""
    closes = [10.0 + (0.02 if index % 2 else -0.02) for index in range(40)]
    item = signal_s5(_context(_frame(closes)))
    assert item is not None
    assert item.triggered is False


def test_s5_requires_enough_history():
    """样本不足时返回 None（规则不该在数据不够时给结论）。"""
    assert signal_s5(_context(_frame([10.0, 10.1]))) is None


# ==================== S7 量价背离 ====================


def test_s7_triggers_on_new_high_with_shrinking_volume():
    """创 20 日新高 + 量能未跟 + 冲高回落 → 卖点。"""
    closes = [10.0 + index * 0.1 for index in range(39)] + [14.1]
    volumes = [5000.0] * 39 + [2000.0]                        # 峰量 5000，当日 2000
    opens = [10.0 + index * 0.1 for index in range(39)] + [14.6]   # 收阴/冲高回落
    frame = _frame(closes, volumes, opens=opens)
    item = signal_s7(_context(frame))
    assert item is not None
    assert item.kind == "sell"
    assert item.triggered is True
    assert item.stop_loss is not None


def test_s7_silent_when_price_not_new_high():
    closes = [10.0 + index * 0.1 for index in range(39)] + [12.0]   # 未创新高
    frame = _frame(closes, [5000.0] * 39 + [2000.0])
    item = signal_s7(_context(frame))
    assert item is not None and item.triggered is False


def test_s7_silent_when_volume_follows():
    """价新高且放量 → 不是背离，不该报卖。"""
    closes = [10.0 + index * 0.1 for index in range(39)] + [14.1]
    frame = _frame(closes, [5000.0] * 39 + [6000.0])
    item = signal_s7(_context(frame))
    assert item is not None and item.triggered is False
