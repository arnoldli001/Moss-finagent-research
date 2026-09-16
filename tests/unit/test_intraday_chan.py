"""做T模块 · 极简缠论结构（分型/笔/中枢/背驰）单元测试。

夹具构造思路：用 `_path_bars` 把一条折线路径展开成K线，且每根K线振幅固定
（``high = 折线价 + _W``、``low = 折线价 - _W``）。等宽K线之间永远不构成包含关系
（包含要求 ``h1 > h2 且 l1 < l2``，等价于同时要求 ``价1 > 价2`` 与 ``价1 < 价2``），
因此「合并K线数 == 原始K线数」，分型必然落在折线拐点上，数值可以精确断言。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.intraday import indicators as ind
from src.intraday.chan import (
    ChanPivot,
    ChanStructure,
    build_structure,
    detect_divergence,
    pivot_position,
)

_W = 0.05        # K线半宽：high = 折线价 + _W，low = 折线价 - _W
_LEG = 5         # 默认每段折线的K线数：相邻拐点的原始K线间隔 = 5 >= min_gap(4)
_MIN_BARS = 10   # 小样本夹具显式下调 min_bars，避免被默认的30根门槛挡住


# ==================== 夹具 ====================


def _path_bars(points: list[float], bars_per_leg: int | list[int] = _LEG,
               width: float = _W) -> pd.DataFrame:
    """把折线路径展开成K线序列（每段 bars_per_leg 根，段内价格线性变化）。

    每段只生成「不含起点、含终点」的K线，因此拐点恰好落在段末那根K线上，
    相邻拐点的原始K线间隔 == bars_per_leg。
    """
    if isinstance(bars_per_leg, int):
        counts = [bars_per_leg] * (len(points) - 1)
    else:
        counts = list(bars_per_leg)
    if len(counts) != len(points) - 1:
        raise ValueError("bars_per_leg 的长度必须等于折线段数")

    prices: list[float] = []
    for index, count in enumerate(counts):
        start, end = points[index], points[index + 1]
        for step in range(1, count + 1):
            prices.append(start + (end - start) * step / count)

    stamps = pd.date_range("2026-09-15 09:35", periods=len(prices), freq="5min")
    return pd.DataFrame({
        "ts": [s.strftime("%Y-%m-%d %H:%M") for s in stamps],
        "open": [p - width for p in prices],
        "high": [p + width for p in prices],
        "low": [p - width for p in prices],
        "close": prices,
        "volume": [1000.0] * len(prices),
    })


def _bars_from_hl(highs: list[float], lows: list[float]) -> pd.DataFrame:
    """按给定高低价构造最小K线表（开收盘取中间值，列名与真实数据源一致）。"""
    stamps = pd.date_range("2026-09-15 09:35", periods=len(highs), freq="5min")
    middles = [(high + low) / 2 for high, low in zip(highs, lows, strict=True)]
    return pd.DataFrame({
        "ts": [s.strftime("%Y-%m-%d %H:%M") for s in stamps],
        "open": middles,
        "high": highs,
        "low": lows,
        "close": middles,
        "volume": [1000.0] * len(highs),
    })


def _pivot_path_bars() -> pd.DataFrame:
    """一段能形成标准中枢的走势：10.2→10.5→10.0→10.4→9.5→9.8→9.3→9.9。

    拐点即分型（间隔5根K线）：顶10.5+_W、底10.0-_W、顶10.4+_W、底9.5-_W…
    前三笔（下/上/下）重叠成中枢，第四笔（9.5→9.8）整段位于中枢下沿之下 → 中枢就此结束。
    """
    return _path_bars([10.2, 10.5, 10.0, 10.4, 9.5, 9.8, 9.3, 9.9])


# 「上下上」走势：底 9.6、顶 10.2、底 9.4、顶 9.9（末根K线不能成分型，故后面还有一段）
_UPDOWNUP_PATH = [10.0, 9.6, 10.2, 9.4, 9.9, 9.3]

# 背驰夹具：急跌 → 强反弹 → 回落 → **弱反弹创新高** → 小幅回落
_DIVERGENCE_PATH = [10.0, 8.0, 11.0, 10.3, 11.05, 10.7]
_DIVERGENCE_LEGS = [14, 14, 14, 14, 6]   # 两段反弹等长，只有力度（斜率）不同


def _divergence_bars(mirrored: bool = False) -> pd.DataFrame:
    """背驰夹具；``mirrored=True`` 时上下镜像，用于构造对称的底背驰。"""
    points = (_DIVERGENCE_PATH if not mirrored
              else [20.0 - price for price in _DIVERGENCE_PATH])
    return _path_bars(points, _DIVERGENCE_LEGS)


# ==================== 空表 / 不足样本 / NaN ====================


def test_build_structure_empty_inputs_are_unavailable() -> None:
    """空表 / None / 只有列没有行 → available=False 且 gap 说明原因，绝不抛异常。"""
    for bars in (None, pd.DataFrame(), pd.DataFrame({"high": [], "low": []})):
        structure = build_structure(bars)
        assert structure.available is False
        assert structure.gap                       # 原因必须非空
        assert structure.fractals == []
        assert structure.strokes == []
        assert structure.pivots == []
        assert structure.last_stroke is None


def test_build_structure_three_bars_is_unavailable() -> None:
    """只有3根K线：样本不足，返回不可用而不是硬造结构。"""
    structure = build_structure(_bars_from_hl([10.0, 10.2, 10.4], [9.8, 10.0, 10.2]))
    assert structure.available is False
    assert structure.gap is not None and "不足" in structure.gap
    assert structure.bars_used == 3
    assert structure.merged_bars == 0


def test_build_structure_all_nan_is_unavailable() -> None:
    """全 NaN：没有任何可用K线 → available=False。"""
    bars = _path_bars([10.0, 10.4, 9.8, 10.6, 10.1, 10.7])
    bars[["high", "low", "close"]] = np.nan
    structure = build_structure(bars)
    assert structure.available is False
    assert structure.gap
    assert structure.bars_used == 0


def test_build_structure_missing_columns_is_unavailable() -> None:
    """缺少 high/low 列 → available=False，不抛 KeyError。"""
    structure = build_structure(pd.DataFrame({"ts": ["a", "b"], "close": [1.0, 2.0]}))
    assert structure.available is False
    assert structure.gap is not None and "high" in structure.gap


def test_build_structure_tolerates_partial_nan_rows() -> None:
    """中间偶发 NaN 行只被剔除（bar 计数变化），不影响结构可用性。"""
    bars = _path_bars(_UPDOWNUP_PATH)
    bars.loc[7, ["high", "low", "close"]] = np.nan
    structure = build_structure(bars, min_bars=_MIN_BARS)
    assert structure.available is True
    assert structure.bars_used == len(bars) - 1
    assert [f.bar_index for f in structure.fractals] == [4, 9, 14, 19]


def test_missing_ts_column_falls_back_to_index() -> None:
    """没有 ts 列时用 df.index 转字符串：RangeIndex 与 DatetimeIndex 都要能跑。"""
    bars = _path_bars(_UPDOWNUP_PATH).drop(columns=["ts"])

    range_indexed = build_structure(bars, min_bars=_MIN_BARS)
    assert range_indexed.available is True
    assert range_indexed.fractals[0].ts == "4"          # RangeIndex → 行号字符串
    assert range_indexed.fractals[0].bar_index == 4

    datetime_indexed = bars.set_index(pd.to_datetime(_path_bars(_UPDOWNUP_PATH)["ts"]))
    structure = build_structure(datetime_indexed, min_bars=_MIN_BARS)
    assert structure.available is True
    assert structure.fractals[0].bar_index == 4
    assert structure.fractals[0].ts == "2026-09-15 09:55:00"


# ==================== 包含关系 ====================


def test_inclusion_merges_strictly_contained_bars() -> None:
    """3根互相包含的K线 → 顺序递推合并为1根（合并后仍是完整区间）。"""
    structure = build_structure(_bars_from_hl([10.0, 9.8, 9.6], [8.0, 8.2, 8.4]), min_bars=1)
    assert structure.bars_used == 3
    assert structure.merged_bars == 1


def test_equal_high_only_is_not_inclusion() -> None:
    """仅高点相等（低点不同）不算包含：宽松比较会凭空抹掉真实低点。"""
    structure = build_structure(_bars_from_hl([10.0, 10.0, 10.0], [8.0, 8.2, 8.4]), min_bars=1)
    assert structure.bars_used == 3
    assert structure.merged_bars == 3


def test_equal_low_only_is_not_inclusion() -> None:
    """仅低点相等（高点不同）同样不算包含。"""
    structure = build_structure(_bars_from_hl([10.0, 10.2, 10.4], [8.0, 8.0, 8.0]), min_bars=1)
    assert structure.merged_bars == 3


def test_identical_bars_are_merged() -> None:
    """高低点完全重合时两个方向都成立 → 算包含并合并（一字板场景）。"""
    structure = build_structure(_bars_from_hl([10.0, 10.0, 10.0], [8.0, 8.0, 8.0]), min_bars=1)
    assert structure.merged_bars == 1


def test_touching_bars_are_not_merged() -> None:
    """端点相接（前高 == 后低）是相邻而非包含。"""
    structure = build_structure(_bars_from_hl([10.0, 10.5], [9.0, 10.0]), min_bars=1)
    assert structure.merged_bars == 2


def test_equal_width_path_bars_never_merge() -> None:
    """夹具自校验：等宽K线互不包含，分型才必然落在折线拐点上。"""
    structure = build_structure(_path_bars(_UPDOWNUP_PATH), min_bars=_MIN_BARS)
    assert structure.bars_used == 25
    assert structure.merged_bars == structure.bars_used


# ==================== 分型与笔 ====================


def test_fractals_and_strokes_on_up_down_up_path() -> None:
    """「上下上」走势：分型数量、笔的方向交替、笔的原始K线间隔约束。"""
    structure = build_structure(_path_bars(_UPDOWNUP_PATH), min_bars=_MIN_BARS)
    assert structure.available is True
    assert structure.gap is None

    # 分型：底 → 顶 → 底 → 顶（末根K线不能成为分型中心，故顶9.9不作为分型）
    assert [f.kind for f in structure.fractals] == ["bottom", "top", "bottom", "top"]
    assert [f.bar_index for f in structure.fractals] == [4, 9, 14, 19]
    assert [f.price for f in structure.fractals] == pytest.approx(
        [9.6 - _W, 10.2 + _W, 9.4 - _W, 9.9 + _W])

    # 笔：「上下上」，方向严格交替
    assert [s.direction for s in structure.strokes] == ["up", "down", "up"]
    assert [s.start_index for s in structure.strokes] == [4, 9, 14]
    assert [s.end_index for s in structure.strokes] == [9, 14, 19]
    for stroke in structure.strokes:
        assert stroke.end_index - stroke.start_index >= 4      # 新笔口径：原始K线间隔 >= 4
    assert structure.last_stroke == structure.strokes[-1]


def test_stroke_bounds_and_amplitude() -> None:
    """笔的 high/low/amplitude_pct 与起止价自洽，方向与价格涨跌一致。"""
    structure = build_structure(_pivot_path_bars(), min_bars=_MIN_BARS)
    for stroke in structure.strokes:
        assert stroke.high == pytest.approx(max(stroke.start_price, stroke.end_price))
        assert stroke.low == pytest.approx(min(stroke.start_price, stroke.end_price))
        assert (stroke.end_price > stroke.start_price) is (stroke.direction == "up")
        expected = abs(stroke.end_price - stroke.start_price) / stroke.start_price * 100.0
        assert stroke.amplitude_pct == pytest.approx(expected)


def test_min_gap_filters_too_close_fractals() -> None:
    """顶底之间原始K线间隔不足 min_gap 时不成笔（3根一段 → 间隔3）。"""
    bars = _path_bars(_UPDOWNUP_PATH, bars_per_leg=3)
    rejected = build_structure(bars, min_bars=_MIN_BARS, min_gap=4)
    assert rejected.available is False
    assert rejected.gap is not None and "笔" in rejected.gap

    accepted = build_structure(bars, min_bars=_MIN_BARS, min_gap=3)
    assert accepted.available is True
    assert len(accepted.strokes) == 3


def test_structure_is_causal_for_earlier_fractals() -> None:
    """因果性：截断尾部数据后，之前已确认的分型/笔不发生变化（无未来函数）。"""
    bars = _divergence_bars()
    full = build_structure(bars)
    truncated = build_structure(bars.iloc[:-12].reset_index(drop=True))
    assert truncated.available is True
    assert truncated.fractals == full.fractals[:len(truncated.fractals)]
    assert truncated.strokes == full.strokes[:len(truncated.strokes)]
    assert len(truncated.fractals) < len(full.fractals)


# ==================== 中枢 ====================


def test_pivot_bounds_from_three_overlapping_strokes() -> None:
    """标准三笔重叠中枢：zg/zd/gg/dd 与笔数取值正确，且第四笔脱离后中枢结束。"""
    structure = build_structure(_pivot_path_bars(), min_bars=_MIN_BARS)
    assert structure.available is True
    assert [s.direction for s in structure.strokes[:4]] == ["down", "up", "down", "up"]

    assert len(structure.pivots) == 1
    pivot = structure.pivots[0]
    # 三笔：下(10.5→10.0) / 上(10.0→10.4) / 下(10.4→9.5)，价格都带 ±_W 的K线半宽
    assert pivot.zg == pytest.approx(10.4 + _W)     # min(各笔高点)
    assert pivot.zd == pytest.approx(10.0 - _W)     # max(各笔低点)
    assert pivot.gg == pytest.approx(10.5 + _W)     # 中枢最高
    assert pivot.dd == pytest.approx(9.5 - _W)      # 中枢最低
    assert pivot.stroke_count == 3                  # 第四笔（9.5→9.8）整体在中枢下沿之下
    assert pivot.start_index == structure.strokes[0].start_index
    assert pivot.end_index == structure.strokes[2].end_index
    assert pivot.start_ts == structure.strokes[0].start_ts
    assert pivot.end_ts == structure.strokes[2].end_ts


def test_pivot_position_below_inside_above() -> None:
    """pivot_position：p<0 中枢下方、0~1 中枢内、>1 中枢上方（含边界）。"""
    structure = build_structure(_pivot_path_bars(), min_bars=_MIN_BARS)
    pivot = structure.pivots[-1]
    span = pivot.zg - pivot.zd

    position, zone = pivot_position(structure, (pivot.zg + pivot.zd) / 2)
    assert zone == "中枢内"
    assert position == pytest.approx(0.5)

    position, zone = pivot_position(structure, pivot.zd - span * 0.5)
    assert zone == "中枢下方"
    assert position == pytest.approx(-0.5)

    position, zone = pivot_position(structure, pivot.zg + span * 0.5)
    assert zone == "中枢上方"
    assert position == pytest.approx(1.5)

    # 边界：正好压在上/下沿时算中枢内
    assert pivot_position(structure, pivot.zg) == (pytest.approx(1.0), "中枢内")
    assert pivot_position(structure, pivot.zd) == (pytest.approx(0.0), "中枢内")


def test_pivot_position_without_pivot() -> None:
    """没有中枢（空结构 / 只有一笔）→ (None, "无中枢")。"""
    assert pivot_position(build_structure(None), 10.0) == (None, "无中枢")

    one_stroke = build_structure(_path_bars([10.0, 10.4, 9.8, 10.2]), min_bars=_MIN_BARS)
    assert one_stroke.available is True
    assert len(one_stroke.strokes) == 1
    assert one_stroke.pivots == []
    assert pivot_position(one_stroke, 10.0) == (None, "无中枢")


def test_pivot_position_degenerate_and_invalid_price() -> None:
    """防御：zg<=zd 的退化中枢不除零，NaN 价格返回「价格无效」。"""
    pivot = ChanPivot(start_index=0, end_index=9, start_ts="t0", end_ts="t9",
                      zg=10.0, zd=10.0, gg=10.0, dd=10.0, stroke_count=3)
    degenerate = ChanStructure(available=True, gap=None, bars_used=10, merged_bars=10,
                               pivots=[pivot])
    assert pivot_position(degenerate, 10.0) == (None, "中枢区间退化")

    structure = build_structure(_pivot_path_bars(), min_bars=_MIN_BARS)
    assert pivot_position(structure, float("nan")) == (None, "价格无效")


# ==================== 背驰 ====================


def test_detect_top_divergence_on_weaker_new_high() -> None:
    """价格创新高、但 MACD 面积与 DIF 峰值同步衰减 → 顶背驰。"""
    bars = _divergence_bars()
    structure = build_structure(bars)
    assert structure.available is True
    # 最后两个同向笔是第1笔与第3笔（都是向上笔），中间夹着一笔向下笔
    assert [(s.direction, s.start_index, s.end_index) for s in structure.strokes] == [
        ("up", 13, 27), ("down", 27, 41), ("up", 41, 55)]

    divergence = detect_divergence(bars, structure)
    assert divergence is not None
    assert divergence.kind == "top"
    assert divergence.ts == structure.strokes[-1].end_ts
    assert divergence.price == pytest.approx(structure.strokes[-1].end_price)
    assert 0.0 < divergence.strength <= 1.0
    assert divergence.inputs["area_ratio"] < 0.85
    assert divergence.inputs["dif_peak_cur"] < divergence.inputs["dif_peak_prev"]
    assert divergence.inputs["cur_end_price"] > divergence.inputs["prev_end_price"]


def test_detect_bottom_divergence_on_mirrored_path() -> None:
    """上下镜像后应得到对称的底背驰：价格创新低、面积与 DIF 谷值同步衰减。"""
    bars = _divergence_bars(mirrored=True)
    structure = build_structure(bars)
    assert structure.available is True
    assert [s.direction for s in structure.strokes] == ["down", "up", "down"]

    divergence = detect_divergence(bars, structure)
    assert divergence is not None
    assert divergence.kind == "bottom"
    assert divergence.ts == structure.strokes[-1].end_ts
    assert 0.0 < divergence.strength <= 1.0
    assert divergence.inputs["area_ratio"] < 0.85
    # 向下笔比的是 DIF 谷值：谷值抬升（衰减）才算背驰
    assert divergence.inputs["dif_peak_cur"] > divergence.inputs["dif_peak_prev"]
    assert divergence.inputs["cur_end_price"] < divergence.inputs["prev_end_price"]


def test_no_divergence_when_last_leg_is_stronger() -> None:
    """价格创新高但力度更强（DIF 峰值抬升）→ 不是背驰。"""
    bars = _path_bars([10.0, 8.0, 11.0, 10.3, 12.5, 12.0], _DIVERGENCE_LEGS)
    structure = build_structure(bars)
    assert structure.available is True
    assert structure.strokes[-1].end_price > structure.strokes[0].end_price
    assert detect_divergence(bars, structure) is None


def test_area_ratio_max_gates_divergence() -> None:
    """面积比阈值可调：收得比实际面积比更紧时不再判为背驰。"""
    bars = _divergence_bars()
    structure = build_structure(bars)
    assert detect_divergence(bars, structure) is not None
    assert detect_divergence(bars, structure, area_ratio_max=0.5) is not None
    assert detect_divergence(bars, structure, area_ratio_max=0.01) is None


def test_divergence_matches_project_indicators_macd() -> None:
    """macd=None 时本地现算的 MACD 与 indicators.macd 口径一致（同一判定结果）。"""
    bars = _divergence_bars()
    structure = build_structure(bars)
    local = detect_divergence(bars, structure)
    reused = detect_divergence(bars, structure, ind.macd(bars))
    assert local is not None and reused is not None
    assert reused.kind == local.kind
    assert reused.inputs == local.inputs

    # 长度不一致的 macd 视为不可用，退回本地计算（不静默错位对齐）
    misaligned = detect_divergence(bars, structure, ind.macd(bars).iloc[:-5])
    assert misaligned is not None
    assert misaligned.inputs == local.inputs


def test_detect_divergence_returns_none_without_inputs() -> None:
    """结构不可用 / 无 close 且无 macd → 返回 None，不抛异常。"""
    empty = build_structure(None)
    assert detect_divergence(None, empty) is None

    bars = _divergence_bars()
    structure = build_structure(bars)
    assert detect_divergence(bars.drop(columns=["close"]), structure) is None
    assert detect_divergence(bars, build_structure(_path_bars([10.0, 10.4, 9.8, 10.2]),
                                                  min_bars=_MIN_BARS)) is None


# ==================== 序列化 ====================


def test_to_dict_is_json_serializable() -> None:
    """to_dict 只吐叶子字段，可直接 json.dumps（可用与不可用结构都要能序列化）。"""
    structure = build_structure(_pivot_path_bars(), min_bars=_MIN_BARS)
    payload = json.loads(json.dumps(structure.to_dict()))
    assert payload["available"] is True
    assert payload["bars_used"] == structure.bars_used
    assert payload["merged_bars"] == structure.merged_bars
    assert len(payload["fractals"]) == len(structure.fractals)
    assert len(payload["strokes"]) == len(structure.strokes)
    assert len(payload["pivots"]) == len(structure.pivots)
    assert isinstance(payload["pivots"][0]["zg"], float)
    assert structure.last_stroke is not None
    assert payload["last_stroke"]["end_ts"] == structure.last_stroke.end_ts

    unavailable = json.loads(json.dumps(build_structure(None).to_dict()))
    assert unavailable["available"] is False
    assert unavailable["gap"]
    assert unavailable["last_stroke"] is None

    bars = _divergence_bars()
    divergence = detect_divergence(bars, build_structure(bars))
    assert divergence is not None
    assert json.loads(json.dumps(divergence.inputs))["area_ratio"] < 1.0
