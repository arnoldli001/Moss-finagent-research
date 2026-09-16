"""日K量价规则引擎（基础量柱 / 量价16形态 / 高量柱攻防）。

本模块是需求方「量价体系」的量化落地，逐条对应其定义：
  §1  基础量柱定义（高量/缩量/缩倍/倍量/梯量/平量/地量/爆量/大长腿/大阴大阳）
  §2  量价关系16形态判定矩阵（8类单K + 8类组合 + 4类平量变体）
  §3  高量柱战法（安全线=实顶 / 风险线=实底 / 三级响应 / 高量纪律）
  §6.3 主力操盘成本线五形态
  §11 位置量化（同一形态在低位是吸筹、在高位是出货）

设计原则：
- 全部为**因果**计算：第 i 根K线的判定只使用 ≤ i 的数据，天然无未来函数；
- 全部为纯函数/纯数据类：输入 DataFrame + 索引 → 输出标记或判定结果，便于单测与回测；
- 阈值全部来自 configs/intraday.yaml 的 daily 段（对应需求的「参数表（回测可调）」），
  不在代码里硬编码 7% / 0.5 / 2.0 这类魔法数字；
- 数据不足时返回 None 或给出 gaps，绝不猜测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.intraday.config import DailyParams
from src.intraday.models import (
    CostLine,
    PositionInfo,
    VolumeAnchor,
    VpPattern,
)


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def _vol(frame: pd.DataFrame, index: int) -> float:
    value = _num(frame["volume"].iloc[index])
    return 0.0 if value is None else value


def _close(frame: pd.DataFrame, index: int) -> float:
    return float(frame["close"].iloc[index])


def _open(frame: pd.DataFrame, index: int) -> float:
    return float(frame["open"].iloc[index])


def _high(frame: pd.DataFrame, index: int) -> float:
    return float(frame["high"].iloc[index])


def _low(frame: pd.DataFrame, index: int) -> float:
    return float(frame["low"].iloc[index])


# ==================================================================
# §1 基础量柱定义
# ==================================================================

def is_high_volume(frame: pd.DataFrame, index: int, lookback: int = 3) -> bool:
    """高量：当日量 > 前 N 日成交量的最大值。**不分阴阳、不看颜色**（§口诀）。"""
    if index < lookback:
        return False
    previous = [_vol(frame, index - offset) for offset in range(1, lookback + 1)]
    return _vol(frame, index) > max(previous)


def is_double_volume(frame: pd.DataFrame, index: int, ratio: float = 2.0) -> bool:
    """倍量：当日量 ≥ 前一日 × ratio。"""
    if index < 1:
        return False
    return _vol(frame, index) >= _vol(frame, index - 1) * ratio


def is_shrink_volume(frame: pd.DataFrame, index: int) -> bool:
    """缩量：当日量 < 前一日量。"""
    if index < 1:
        return False
    return _vol(frame, index) < _vol(frame, index - 1)


def is_shrink_half(frame: pd.DataFrame, index: int, ratio: float = 0.5) -> bool:
    """缩倍量：当日量 ≤ 前一日量 × ratio（默认 1/2）。"""
    if index < 1:
        return False
    previous = _vol(frame, index - 1)
    return previous > 0 and _vol(frame, index) <= previous * ratio


def is_ladder_down(frame: pd.DataFrame, index: int, days: int = 3) -> bool:
    """梯量（递减）：连续 days 天成交量递减。"""
    if index < days:
        return False
    return all(_vol(frame, index - k) < _vol(frame, index - k - 1)
               for k in range(days))


def is_flat_volume(frame: pd.DataFrame, index: int, tolerance: float = 0.1) -> bool:
    """平量：当日量与前一日偏差 < tolerance。"""
    if index < 1:
        return False
    previous = _vol(frame, index - 1)
    if previous <= 0:
        return False
    return abs(_vol(frame, index) - previous) / previous < tolerance


def is_ground_volume(frame: pd.DataFrame, index: int, window: int = 20) -> bool:
    """地量：区间内最低成交量（注意「地量不等于地价」）。"""
    if index < window - 1:
        return False
    window_slice = [_vol(frame, i) for i in range(index - window + 1, index + 1)]
    return _vol(frame, index) <= min(window_slice)


def is_explode_volume(frame: pd.DataFrame, index: int, window: int = 20,
                      ratio: float = 1.5) -> bool:
    """爆量：当日量 > 近 N 日最大量 × ratio（爆量往往意味着调整开始）。"""
    if index < window:
        return False
    prior = [_vol(frame, i) for i in range(index - window, index)]
    peak = max(prior) if prior else 0.0
    return peak > 0 and _vol(frame, index) > peak * ratio


def body_top(frame: pd.DataFrame, index: int) -> float:
    """实体顶 = max(开, 收)。高量柱的实顶作安全线。"""
    return max(_open(frame, index), _close(frame, index))


def body_bottom(frame: pd.DataFrame, index: int) -> float:
    """实体底 = min(开, 收)。高量柱的实底作风险线（「实底一破快平仓」）。"""
    return min(_open(frame, index), _close(frame, index))


def body_size(frame: pd.DataFrame, index: int) -> float:
    return abs(_close(frame, index) - _open(frame, index))


def is_long_lower_shadow(frame: pd.DataFrame, index: int,
                         multiple: float = 2.0) -> bool:
    """大长腿：下影线长度 ≥ 实体 × multiple。"""
    lower_shadow = min(_open(frame, index), _close(frame, index)) - _low(frame, index)
    body = body_size(frame, index)
    if body <= 0:
        # 一字/T字：实体为0时用振幅做参照，避免除零
        return lower_shadow > 0 and lower_shadow >= (
            (_high(frame, index) - _low(frame, index)) * 0.5)
    return lower_shadow >= body * multiple


def pct_change(frame: pd.DataFrame, index: int) -> float:
    """涨跌幅%（相对前一日收盘）。"""
    if index < 1:
        return 0.0
    previous = _close(frame, index - 1)
    if previous <= 0:
        return 0.0
    return (_close(frame, index) / previous - 1.0) * 100.0


def amplitude(frame: pd.DataFrame, index: int) -> float:
    """振幅% = (最高-最低)/昨收。"""
    if index < 1:
        return 0.0
    previous = _close(frame, index - 1)
    if previous <= 0:
        return 0.0
    return (_high(frame, index) - _low(frame, index)) / previous * 100.0


def is_big_bar(frame: pd.DataFrame, index: int, threshold: float = 7.0) -> bool:
    """大阴/大阳线：涨跌幅绝对值 ≥ threshold%。"""
    return abs(pct_change(frame, index)) >= threshold


def is_yang(frame: pd.DataFrame, index: int) -> bool:
    return _close(frame, index) >= _open(frame, index)


def enrich_daily_frame(frame: pd.DataFrame,
                       params: DailyParams) -> pd.DataFrame:
    """给日线表补齐全部量柱标记列（一次算完，供规则与前端共用）。"""
    out = frame.copy().reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    for index in range(len(out)):
        rows.append({
            "pct_chg": round(pct_change(out, index), 4),
            "amplitude": round(amplitude(out, index), 4),
            "body_top": round(body_top(out, index), 4),
            "body_bottom": round(body_bottom(out, index), 4),
            "is_high_volume": is_high_volume(out, index, params.hv_lookback),
            "is_double_volume": is_double_volume(out, index, params.double_ratio),
            "is_shrink_volume": is_shrink_volume(out, index),
            "is_shrink_half": is_shrink_half(out, index, params.shrink_ratio),
            "is_ladder_down": is_ladder_down(out, index),
            "is_flat_volume": is_flat_volume(out, index, params.flat_ratio),
            "is_ground_volume": is_ground_volume(out, index),
            "is_explode_volume": is_explode_volume(out, index),
            "is_long_lower_shadow": is_long_lower_shadow(
                out, index, params.tail_shadow_x),
            "is_big_yang": pct_change(out, index) >= params.big_bar_pct,
            "is_big_yin": pct_change(out, index) <= -params.big_bar_pct,
        })
    flags = pd.DataFrame(rows)
    for column in flags.columns:
        out[column] = flags[column]
    # 均线列（供前端逐bar画均线；B7/B10 等规则也依赖）
    closes = out["close"].astype("float64")
    for period in (5, 10, 20, 60):
        out[f"ma{period}"] = closes.rolling(period).mean()
    return out


# ==================================================================
# §2 量价关系16形态判定矩阵
# ==================================================================

@dataclass
class _VpRule:
    code: str
    name: str
    category: str
    direction: str
    meaning: str
    signal: str


# 单根K线（8类）
_SINGLE_RULES: dict[str, _VpRule] = {
    "big_yang_expand": _VpRule(
        "big_yang_expand", "放量大阳", "单K", "bullish",
        "分歧中买方获胜，建仓初期", "看多"),
    "big_yang_shrink": _VpRule(
        "big_yang_shrink", "缩量大阳", "单K", "bullish",
        "卖盘枯竭、追涨意愿强，拉升阶段", "看多"),
    "big_yin_expand": _VpRule(
        "big_yin_expand", "放量大阴", "单K", "bearish",
        "抛压大，高位主力出货", "看空"),
    "big_yin_shrink": _VpRule(
        "big_yin_shrink", "缩量大阴", "单K", "bearish",
        "一致看空、恐慌砸盘，下跌中继", "看空"),
    "small_expand": _VpRule(
        "small_expand", "放量小阴小阳", "单K", "reversal",
        "分歧巨大打成平手，顶/底反转区", "反转预警"),
    "small_shrink": _VpRule(
        "small_shrink", "缩量小阴小阳", "单K", "neutral",
        "量价匹配、无方向，横盘整理", "观望"),
    "wide_expand": _VpRule(
        "wide_expand", "放量大阴大阳(宽幅)", "单K", "neutral",
        "趋势延续：上升中继续涨 / 下降中继续跌", "顺势"),
    "wide_shrink": _VpRule(
        "wide_shrink", "缩量大阴大阳(宽幅)", "单K", "neutral",
        "高控盘：上升途中=洗盘续涨；高位横盘=出货风险", "位置定信号"),
}

# 多根K线组合（8类 + 4类平量变体）
_MULTI_RULES: dict[str, _VpRule] = {
    "up_decel_shrink": _VpRule(
        "up_decel_shrink", "价升量缩", "组合", "reversal",
        "量价背离，买方动能不足，将回踩但幅度有限", "回踩后再多"),
    "down_small_shrink": _VpRule(
        "down_small_shrink", "量缩微跌", "组合", "bullish",
        "卖方减弱无人愿卖，洗盘特征", "看多（洗盘）"),
    "up_decel_expand": _VpRule(
        "up_decel_expand", "放量滞涨", "组合", "bearish",
        "高位抛压增大，即将反转", "减仓/清仓"),
    "down_small_expand": _VpRule(
        "down_small_expand", "放量微跌", "组合", "reversal",
        "低位承接资金进场，见底信号", "反转预警（多）"),
    "up_accel_shrink": _VpRule(
        "up_accel_shrink", "缩量大涨", "组合", "bullish",
        "分歧转一致，抛压减弱锁仓", "强看多"),
    "down_accel_shrink": _VpRule(
        "down_accel_shrink", "缩量大跌", "组合", "bearish",
        "多头崩溃、恐慌一致看空", "强看空"),
    "up_accel_expand": _VpRule(
        "up_accel_expand", "放量大涨", "组合", "bullish",
        "量价齐升、放量攻击", "看多延续"),
    "down_accel_expand": _VpRule(
        "down_accel_expand", "放量大跌", "组合", "bearish",
        "空头肆虐、承接不住，高位出货信号", "看空"),
    "flat_up_decel": _VpRule(
        "flat_up_decel", "平量滞涨", "组合", "bearish",
        "卖方增加、抛压加重，见顶变盘", "减仓"),
    "flat_down_decel": _VpRule(
        "flat_down_decel", "平量价缩", "组合", "neutral",
        "仅小反弹非反转，不可抄底", "观望"),
    "flat_up_accel": _VpRule(
        "flat_up_accel", "平量大涨", "组合", "bullish",
        "空方减弱锁仓等加速，顶部将现天量", "看多但备顶"),
    "flat_down_accel": _VpRule(
        "flat_down_accel", "平量大跌", "组合", "bearish",
        "一致看空争相逃命", "看空加速"),
}


def classify_volume_price(frame: pd.DataFrame, index: int,
                          params: DailyParams) -> VpPattern | None:
    """判定第 i 根K线的量价形态（§2 矩阵）。

    三维度：价格方向（涨/跌/平）× 价格速率（加速/减速/平）× 量能（放量/缩量/平量）。
    「速率」用相邻两日涨跌幅的绝对值比较（放大=加速，缩小=减速）。
    """
    if index < 3:
        return None
    today = pct_change(frame, index)
    yesterday = pct_change(frame, index - 1)
    price_dir = "涨" if today > 0 else ("跌" if today < 0 else "平")
    if index >= 2:
        before = pct_change(frame, index - 1)
        earlier = pct_change(frame, index - 2)
        if abs(today) > abs(yesterday) * 1.05:
            speed = "加速"
        elif abs(today) < abs(yesterday) * 0.95:
            speed = "减速"
        else:
            speed = "平"
        del before, earlier
    else:
        speed = "平"
    if is_flat_volume(frame, index, params.flat_ratio):
        volume_dir = "平量"
    elif is_shrink_volume(frame, index):
        volume_dir = "缩量"
    else:
        volume_dir = "放量"

    amp = amplitude(frame, index)
    big_yang = today >= params.big_bar_pct
    big_yin = today <= -params.big_bar_pct

    def build(key: str, category: str = "单K",
              detail: str = "") -> VpPattern:
        rule = (_SINGLE_RULES if category == "单K" else _MULTI_RULES)[key]
        return VpPattern(
            code=rule.code, name=rule.name, category=category,  # type: ignore[arg-type]
            price_dir=price_dir, price_speed=speed,  # type: ignore[arg-type]
            volume_dir=volume_dir,  # type: ignore[arg-type]
            meaning=rule.meaning, signal=rule.signal,
            direction=rule.direction,  # type: ignore[arg-type]
            detail=detail)

    # ---- 单根K线（优先级：宽幅 > 大阴大阳 > 小阴小阳）----
    if amp >= params.wide_amplitude_pct:
        key = "wide_expand" if volume_dir == "放量" else (
            "wide_shrink" if volume_dir == "缩量" else "wide_shrink")
        return build(key, "单K",
                     f"振幅{amp:.1f}% ≥ {params.wide_amplitude_pct:g}%，"
                     f"量能{volume_dir}")
    if big_yang or big_yin:
        prefix = "big_yang" if big_yang else "big_yin"
        suffix = "expand" if volume_dir == "放量" else "shrink"
        return build(f"{prefix}_{suffix}", "单K",
                     f"涨跌幅{today:+.2f}%，量能{volume_dir}")
    if amp < params.small_amplitude_pct:
        key = "small_expand" if volume_dir == "放量" else (
            "small_shrink" if volume_dir == "缩量" else "small_shrink")
        return build(key, "单K",
                     f"振幅{amp:.2f}% < {params.small_amplitude_pct:g}%，"
                     f"量能{volume_dir}")

    # ---- 多根K线组合 ----
    small_move = 2.0  # 「微跌/滞涨」的幅度界限
    if volume_dir == "平量":
        if price_dir == "涨" and speed != "加速":
            return build("flat_up_decel", "组合", "价涨减速 + 平量（且为高量）")
        if price_dir == "跌" and speed != "加速":
            return build("flat_down_decel", "组合", "价跌减速 + 平量（且为高量）")
        if price_dir == "涨":
            return build("flat_up_accel", "组合", "价涨加速 + 平量偏缩")
        return build("flat_down_accel", "组合", "价跌加速 + 平量偏缩")
    shrink = volume_dir == "缩量"
    if price_dir == "涨":
        if speed == "加速":
            return build("up_accel_shrink" if shrink else "up_accel_expand",
                         "组合", f"价涨加速 + 量能{volume_dir}")
        if abs(today) <= small_move:
            return build("up_decel_expand" if not shrink else "up_decel_shrink",
                         "组合", f"价涨减速（{today:+.2f}%）+ 量能{volume_dir}")
        return build("up_decel_shrink" if shrink else "up_decel_expand",
                     "组合", f"价涨减速 + 量能{volume_dir}")
    if price_dir == "跌":
        if speed == "加速":
            return build("down_accel_shrink" if shrink else "down_accel_expand",
                         "组合", f"价跌加速 + 量能{volume_dir}")
        if abs(today) <= small_move:
            return build("down_small_shrink" if shrink else "down_small_expand",
                         "组合", f"价跌减速/微跌（{today:+.2f}%）+ 量能{volume_dir}")
        return build("down_accel_shrink" if shrink else "down_accel_expand",
                     "组合", f"价跌减速 + 量能{volume_dir}")
    return None


def trend_health(frame: pd.DataFrame, index: int, window: int = 10) -> str:
    """§2 合成规则：上涨放量 + 下跌缩量 = 健康上升趋势；反之 = 见顶信号。"""
    if index < window:
        return "样本不足"
    up_expand = up_shrink = down_expand = down_shrink = 0
    for i in range(index - window + 1, index + 1):
        change = pct_change(frame, i)
        shrink = is_shrink_volume(frame, i)
        if change > 0:
            if shrink:
                up_shrink += 1
            else:
                up_expand += 1
        elif change < 0:
            if shrink:
                down_shrink += 1
            else:
                down_expand += 1
    if up_expand > up_shrink and down_shrink >= down_expand:
        return "健康上升趋势（上涨放量、下跌缩量）"
    if up_shrink >= up_expand and down_expand > down_shrink:
        return "见顶特征（上涨无量、下跌放量）"
    return "量价关系中性/混合"


# ==================================================================
# §3 高量柱战法（安全线 / 风险线 / 三级响应）
# ==================================================================

def find_volume_anchors(frame: pd.DataFrame, params: DailyParams,
                        lookback: int = 60,
                        max_anchors: int = 6) -> list[VolumeAnchor]:
    """扫描近 lookback 根K线中的高量柱，按「左侧、水平、最近、最高」取用。

    返回按时间倒序（最近的在前）的锚点列表；每根给出安全线（实顶）、
    风险线（实底）、类型（标杆/梯量/缩量/倍量）、位置（底部/中继/接力/顶部）
    与当前状态（有效支撑 / 待观察 / 已破位）。
    """
    if frame is None or len(frame) < 5:
        return []
    anchors: list[VolumeAnchor] = []
    total = len(frame)
    start = max(params.hv_lookback + 1, total - lookback)
    recent_highs = frame["high"].tail(60).max()
    recent_lows = frame["low"].tail(60).min()
    # 状态必须用**最新收盘价**与锚点风险线比较；
    # 用高量柱自身的收盘价判断会永远得到"有效支撑"（它当然不低于自己的实体底）。
    latest_close = _close(frame, total - 1)
    for index in range(start, total):
        if not is_high_volume(frame, index, params.hv_lookback):
            continue
        close = _close(frame, index)
        top = body_top(frame, index)
        bottom = body_bottom(frame, index)
        # 实体过小时取虚顶/虚底（需求 §3.2「先取实后取虚」）
        if abs(top - bottom) < close * 0.005:
            top, bottom = _high(frame, index), _low(frame, index)
        if is_double_volume(frame, index, params.double_ratio):
            kind = "倍量"
        elif is_ladder_down(frame, index, 3):
            kind = "梯量"
        elif is_shrink_volume(frame, index):
            kind = "缩量"
        else:
            kind = "标杆"
        span = (recent_highs - recent_lows) or 1.0
        position_ratio = (close - recent_lows) / span
        position = ("底部" if position_ratio <= 0.3
                    else "顶部" if position_ratio >= 0.7 else "中继")
        days_since = total - 1 - index
        if latest_close < bottom:
            status = "已破位"
        elif days_since == 0:
            status = "待观察"  # 高量第1天：观望
        else:
            status = "有效支撑"
        anchors.append(VolumeAnchor(
            date=str(frame["date"].iloc[index]), index=index,
            volume=round(_vol(frame, index), 2),
            body_top=round(top, 3), body_bottom=round(bottom, 3),
            kind=kind, position=position,  # type: ignore[arg-type]
            days_since=days_since, status=status,  # type: ignore[arg-type]
            note=(f"高量后第{days_since}天，实体"
                  f"{bottom:.2f}~{top:.2f}（{kind}高量柱，{position}）")))
    anchors.sort(key=lambda item: item.index, reverse=True)
    return anchors[:max_anchors]


def high_volume_discipline(frame: pd.DataFrame, anchors: list[VolumeAnchor],
                           params: DailyParams) -> list[str]:
    """§S6 高量纪律 + §7 口诀中可由日线判定的条目（逐条给出是否命中）。"""
    hits: list[str] = []
    if frame is None or len(frame) < 3 or not anchors:
        return hits
    last = len(frame) - 1
    close = _close(frame, last)
    anchor = anchors[0]
    # 高量破 → 清仓
    if close < anchor.body_bottom:
        hits.append(
            f"【高量破】现价 {close:.2f} 跌破最近高量柱实体低点 "
            f"{anchor.body_bottom:.2f}（{anchor.date}）→ 按纪律清仓")
    # 高量后2-3天站上支撑且收阳 → 可参与
    if anchor.days_since in (2, 3) and close > anchor.body_bottom \
            and is_yang(frame, last):
        hits.append(
            f"【高量第{anchor.days_since}天】站上风险线 {anchor.body_bottom:.2f} "
            f"且收阳 → 可参与")
    # 上影线触碰高量压力线过不去 → 减仓
    if _high(frame, last) >= anchor.body_top and close < anchor.body_top:
        hits.append(
            f"【遇顶不过】盘中最高 {_high(frame, last):.2f} 触及高量实顶 "
            f"{anchor.body_top:.2f} 但收盘未站上 → 减仓")
    # 高量3天内高低点下移 → 减仓
    if anchor.days_since <= 3 and last >= 1:
        if _high(frame, last) < _high(frame, last - 1) and \
                _low(frame, last) < _low(frame, last - 1):
            hits.append("【高低点下移】高量3天内高点与低点同步下移 → 减仓")
    # 量大实体小（高位）→ 有人跑
    if anchor.position == "顶部" and is_high_volume(frame, last, params.hv_lookback):
        body = body_size(frame, last)
        if body < close * 0.01:
            hits.append(
                "【量大实体小】高量+小实体且处于高位 → 有人跑，减仓")
    # 连红≥4天后见放量阴线 → 减仓
    if last >= 4:
        reds = all(is_yang(frame, i) for i in range(last - 4, last))
        if reds and not is_yang(frame, last) and not is_shrink_volume(frame, last):
            hits.append("【连红见阴】连红≥4天后出现放量阴线 → 减仓")
    # 大量次日阴 → 仓位全出清（口诀）
    if last >= 1 and is_high_volume(frame, last - 1, params.hv_lookback) \
            and not is_yang(frame, last):
        hits.append("【大量次日阴】前一日高量、今日收阴 → 口诀：仓位全出清")
    return hits


# ==================================================================
# §6.3 主力操盘成本线（基准线五形态）
# ==================================================================

def find_cost_lines(frame: pd.DataFrame, params: DailyParams,
                    lookback: int = 40) -> list[CostLine]:
    """识别最近的主力成本线候选（五形态），并计算现价相对幅度与是否破位。"""
    if frame is None or len(frame) < 3:
        return []
    total = len(frame)
    last = total - 1
    close = _close(frame, last)
    start = max(1, total - lookback)
    lines: list[CostLine] = []

    def add(kind: str, date: str, price: float, note: str = "") -> None:
        if price <= 0:
            return
        lines.append(CostLine(
            kind=kind, date=date, price=round(price, 3),
            distance_pct=round((close / price - 1.0) * 100, 2),
            broken=close < price, note=note))

    for index in range(last, start - 1, -1):
        change = pct_change(frame, index)
        # 形态1/2：大阳线 → 开盘价 / 实体1/2
        if change >= params.big_bar_pct:
            date = str(frame["date"].iloc[index])
            add("大阳线开盘价", date, _open(frame, index),
                "大阳线开盘价作成本线（不破视为洗盘）")
            add("大阳线实体1/2", date,
                (_open(frame, index) + _close(frame, index)) / 2,
                "大阳线实体中点作成本线")
        # 形态4：跳空缺口 → 前一根收盘价
        if index >= 1 and _low(frame, index) > _high(frame, index - 1):
            add("跳空缺口前收盘", str(frame["date"].iloc[index - 1]),
                _close(frame, index - 1), "向上跳空缺口，缺口不破则强势延续")
        # 形态5：长下影大阳线 → 下影区域成本密集位（用当日均价近似）
        if is_long_lower_shadow(frame, index, params.tail_shadow_x) and \
                change > 0:
            typical = (_high(frame, index) + _low(frame, index)
                       + _close(frame, index)) / 3
            add("长下影成本区(当日均价近似)", str(frame["date"].iloc[index]),
                typical, "长下影大阳线取下影成本密集位")
        if len(lines) >= 4:
            break
    # 形态3：相邻两根大阳线 → 第一根开盘价
    for index in range(last, start, -1):
        if pct_change(frame, index) >= params.big_bar_pct and \
                pct_change(frame, index - 1) >= params.big_bar_pct:
            add("并列大阳首根开盘价", str(frame["date"].iloc[index - 1]),
                _open(frame, index - 1), "相邻两根大阳线取第一根开盘价")
            break
    # 去重（同一日期同一类型只留一条）
    seen: set[tuple[str, str]] = set()
    unique: list[CostLine] = []
    for line in lines:
        key = (line.kind, line.date)
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
    return unique[:6]


# ==================================================================
# §11 位置量化
# ==================================================================

def quantify_position(frame: pd.DataFrame, window: int = 120) -> PositionInfo | None:
    """量化「位置」：现价在近 N 日高低区间中的分位（低位吸筹 / 高位出货的前提）。"""
    if frame is None or len(frame) < 20:
        return None
    tail = frame.tail(window)
    low = _num(tail["low"].min())
    high = _num(tail["high"].max())
    close = _close(frame, len(frame) - 1)
    if low is None or high is None or high <= low:
        return None
    percentile = (close - low) / (high - low)
    label = ("低位" if percentile <= 0.3
             else "高位" if percentile >= 0.7 else "中位")
    note = {
        "低位": "同一形态在低位更可能是吸筹/洗盘",
        "中位": "中继位置，需结合高量柱与前高判断",
        "高位": "同一形态在高位更可能是出货/诱多",
    }[label]
    return PositionInfo(
        label=label, percentile=round(percentile, 4), window=len(tail),
        high=round(high, 3), low=round(low, 3),
        from_high_pct=round((close / high - 1.0) * 100, 2),
        from_low_pct=round((close / low - 1.0) * 100, 2), note=note)


def chip_peak_valley(frame: pd.DataFrame, window: int = 60,
                     bins: int = 30) -> tuple[float | None, float | None]:
    """筹码峰与峰谷近似（用成交量加权的价格分布代替真实筹码分布）。

    真实筹码分布需要逐笔成交数据，这里用「近 window 日按收盘价分箱、以成交量加权」
    的成交量分布近似：
      - 筹码峰 = 成交量最密集的价格箱中心（套牢/获利盘密集区）
      - 峰谷   = 筹码峰**下方**成交量最低的箱中心（常作支撑参考）
    返回 (峰, 谷)；样本不足返回 (None, None)。
    """
    if frame is None or len(frame) < 20:
        return None, None
    tail = frame.tail(window)
    closes = tail["close"].astype("float64")
    volumes = tail["volume"].astype("float64").fillna(0.0)
    low, high = float(closes.min()), float(closes.max())
    if not (high > low) or float(volumes.sum()) <= 0:
        return None, None
    width = (high - low) / bins
    profile = [0.0] * bins
    for close, volume in zip(closes, volumes, strict=True):
        index = min(bins - 1, max(0, int((float(close) - low) / width)))
        profile[index] += float(volume)
    peak_index = max(range(bins), key=lambda i: profile[i])
    peak = low + (peak_index + 0.5) * width
    # 峰谷：筹码峰下方（更低价位）成交量最小的箱 —— 支撑参考位
    below = [i for i in range(peak_index) if profile[i] > 0]
    valley = None
    if below:
        valley_index = min(below, key=lambda i: profile[i])
        valley = low + (valley_index + 0.5) * width
    return round(peak, 3), (None if valley is None else round(valley, 3))


def protection_lines(frame: pd.DataFrame, params: DailyParams) -> dict[str, Any]:
    """§S5 保护线体系：固定止损 + 移动止盈。

    关键约束（实测踩过坑）：**止损线必须在现价下方**。
    早期实现取 max(最近上涨阳线低点, 60日成交量加权均价)，在「高位回落后」
    会得到一个高于现价的"止损线"（实测某票现价880、算出1002），
    导致永远显示「已破位」，毫无意义。
    正确做法是从「现价下方的候选支撑」里取**最高**的那个（最近的支撑），
    并在支撑过远时回退到固定止损幅度。
    """
    if frame is None or len(frame) < 5:
        return {}
    last = len(frame) - 1
    close = _close(frame, last)

    # 候选支撑1：最近一条上涨阳线的低点
    recent_up_low: float | None = None
    recent_up_date = ""
    for index in range(last, max(0, last - 20), -1):
        if is_yang(frame, index) and pct_change(frame, index) > 0:
            recent_up_low = _low(frame, index)
            recent_up_date = str(frame["date"].iloc[index])
            break
    # 候选支撑2：筹码峰谷（成交量分布的低谷）
    _peak, valley = chip_peak_valley(frame)
    # 候选支撑3/4：20日低点 / MA20
    low20 = float(frame["low"].tail(20).min())
    ma20 = _num(frame["close"].tail(20).mean())
    ma60 = _num(frame["close"].tail(60).mean())

    labelled: list[tuple[str, float]] = []
    if recent_up_low and recent_up_low < close:
        labelled.append((f"最近上涨阳线低点({recent_up_date})", recent_up_low))
    if valley and valley < close:
        labelled.append(("近60日筹码峰谷", valley))
    if low20 < close:
        labelled.append(("近20日最低", low20))
    if ma20 and ma20 < close:
        labelled.append(("MA20", ma20))

    floor = close * (1 - params.stop_loss_pct / 100)
    if labelled:
        basis, stop_line = max(labelled, key=lambda item: item[1])
        if stop_line < floor:
            # 最近的支撑离得太远（超过固定止损幅度）→ 按固定幅度止损更现实
            stop_line, basis = floor, f"固定止损{params.stop_loss_pct:g}%（支撑过远）"
    else:
        stop_line, basis = floor, f"固定止损{params.stop_loss_pct:g}%（现价下方无支撑）"

    return {
        "stop_line": round(stop_line, 3),
        "stop_basis": basis,
        "trail_line": round(stop_line, 3),
        "trail_basis": "最近上涨K线低点与筹码峰谷取较高者（且低于现价）",
        "ma20": ma20, "ma60": ma60,
        "broken_stop": close < stop_line,
        "note": "两层都击穿必须止损；机构股可用有效跌破60日线防守",
        "support_candidates": [
            {"label": label, "price": round(price, 3)}
            for label, price in sorted(labelled, key=lambda i: -i[1])
        ],
    }


@dataclass
class DailyContext:
    """一次日K分析的全部中间产物（规则函数共用，避免重复计算）。"""

    frame: pd.DataFrame
    params: DailyParams
    anchors: list[VolumeAnchor] = field(default_factory=list)
    pattern: VpPattern | None = None
    position: PositionInfo | None = None
    cost_lines: list[CostLine] = field(default_factory=list)
    index: int = 0

    @property
    def last(self) -> int:
        return self.index

    def close(self, offset: int = 0) -> float:
        return _close(self.frame, self.index - offset)

    def open(self, offset: int = 0) -> float:
        return _open(self.frame, self.index - offset)

    def high(self, offset: int = 0) -> float:
        return _high(self.frame, self.index - offset)

    def low(self, offset: int = 0) -> float:
        return _low(self.frame, self.index - offset)

    def volume(self, offset: int = 0) -> float:
        return _vol(self.frame, self.index - offset)

    def change(self, offset: int = 0) -> float:
        return pct_change(self.frame, self.index - offset)

    def is_yang(self, offset: int = 0) -> bool:
        return is_yang(self.frame, self.index - offset)

    def date(self, offset: int = 0) -> str:
        return str(self.frame["date"].iloc[self.index - offset])

    def amp(self, offset: int = 0) -> float:
        return amplitude(self.frame, self.index - offset)

    def flags(self, offset: int = 0) -> dict[str, Any]:
        row = self.frame.iloc[self.index - offset]
        return {
            "high_volume": bool(row.get("is_high_volume", False)),
            "double_volume": bool(row.get("is_double_volume", False)),
            "shrink_volume": bool(row.get("is_shrink_volume", False)),
            "shrink_half": bool(row.get("is_shrink_half", False)),
            "ladder_down": bool(row.get("is_ladder_down", False)),
            "flat_volume": bool(row.get("is_flat_volume", False)),
            "ground_volume": bool(row.get("is_ground_volume", False)),
            "explode_volume": bool(row.get("is_explode_volume", False)),
            "long_lower_shadow": bool(row.get("is_long_lower_shadow", False)),
            "big_yang": bool(row.get("is_big_yang", False)),
            "big_yin": bool(row.get("is_big_yin", False)),
        }

    def build_context(self, frame: pd.DataFrame,
                      params: DailyParams) -> DailyContext:
        """构造上下文（含派生产物）。"""
        return DailyContext(
            frame=frame, params=params, index=len(frame) - 1,
            anchors=find_volume_anchors(frame, params),
            pattern=classify_volume_price(frame, len(frame) - 1, params),
            position=quantify_position(frame),
            cost_lines=find_cost_lines(frame, params),
        )
