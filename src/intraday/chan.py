"""极简缠论结构识别（A股做T辅助专用，零第三方依赖）。

相对 `chanlun-quant` 的全量实现，这里只保留「分型 → 笔 → 中枢 → 背驰」四步：

- **不做线段**：特征序列算法对参数与噪声高度敏感，把它的不确定性传导到中枢上
  会显著降低分钟级做T的可用性；中枢直接用笔构造（笔中枢）。
- **不做级别递归 / 九段升级 / 三类买卖点**：只回答「现在的结构在哪、有没有背驰」。
- **单文件自包含**：只用 pandas / numpy / 标准库，不 import 本项目其他模块。
  MACD 与 ``src.intraday.indicators.macd`` 口径完全一致（EMA(adjust=False)、
  柱 = 2×(DIF-DEA)，见 tests/unit/test_intraday_chan.py 的一致性用例），
  但在这里独立实现，避免模块间循环 import。

硬约束
------
- 所有公开函数**不抛异常**：数据不可用时返回 ``available=False`` + ``gap`` 说明原因。
- 无未来函数：任何输出只依赖输入 bars 中已经出现的数据；最后一根（合并）K线
  永远不能成为分型的中间K线（它缺少右侧确认K线）。

输入约定（尽量宽容）
--------------------
- 必需列 ``high`` / ``low``（列名大小写、首尾空白不敏感）；``close`` 仅背驰口径需要。
- ``ts`` 列可选：缺失时退化为 ``str(df.index)``，因此 RangeIndex 与 DatetimeIndex 都可。
- 行序必须**按时间升序**：本模块刻意不排序，以免打乱 ``bar_index`` 与调用方表下标的
  一一对应关系；``bar_index`` 始终是输入 DataFrame 的**行位置**。
- 含 NaN/Inf 的行会被剔除（其余行照常参与结构计算），全为 NaN 则判为不可用。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

__all__ = [
    "ChanDivergence",
    "ChanFractal",
    "ChanPivot",
    "ChanStroke",
    "ChanStructure",
    "build_structure",
    "detect_divergence",
    "pivot_position",
]

# ==================== 常量 ====================

_TINY = 1e-12          # 浮点零阈值（防止除零）
_MIN_MERGED_BARS = 5   # 分型需要3根合并K线，一笔至少跨越5根（新笔口径的最小值）
_MIN_STROKES = 3       # 中枢至少由3笔重叠构成
_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_AREA_WEIGHT = 0.7     # strength 中「面积衰减」的权重，其余留给「DIF 衰减」


# ==================== 对外数据结构 ====================


@dataclass(frozen=True)
class ChanFractal:
    """分型（在包含关系处理后的K线上识别）。

    Attributes
    ----------
    index : int
        分型中间K线在**合并后**K线序列中的下标。
    bar_index : int
        分型极值对应的**原始 bars 行位置**（顶分型取最高价所在行，底分型取最低价所在行）。
    ts : str
        上述原始K线的时间戳。
    kind : {"top", "bottom"}
        顶分型 / 底分型。
    price : float
        分型极值价格（顶=最高价，底=最低价）。
    """

    index: int
    bar_index: int
    ts: str
    kind: Literal["top", "bottom"]
    price: float


@dataclass(frozen=True)
class ChanStroke:
    """笔（一个顶分型与一个底分型之间的连线）。

    Attributes
    ----------
    start_index, end_index : int
        起止分型极值所在的**原始 bars 行位置**。
    start_ts, end_ts : str
        起止时间戳。
    start_price, end_price : float
        起止分型价格。
    direction : {"up", "down"}
        up = 底分型 → 顶分型；down = 顶分型 → 底分型。
    high, low : float
        笔的价格区间（max/min of start_price、end_price）。
    amplitude_pct : float
        笔振幅百分比 ``|end - start| / |start| * 100``。
    """

    start_index: int
    end_index: int
    start_ts: str
    end_ts: str
    start_price: float
    end_price: float
    direction: Literal["up", "down"]
    high: float
    low: float
    amplitude_pct: float


@dataclass(frozen=True)
class ChanPivot:
    """缠中说禅走势中枢（由**笔**重叠构成）。

    Attributes
    ----------
    start_index, end_index : int
        中枢覆盖的首笔起点 / 末笔终点（原始 bars 行位置）。
    start_ts, end_ts : str
        起止时间戳。
    zg : float
        中枢上沿 = min(构成笔的高点)。
    zd : float
        中枢下沿 = max(构成笔的低点)。
    gg : float
        中枢最高 = max(构成笔的高点)。
    dd : float
        中枢最低 = min(构成笔的低点)。
    stroke_count : int
        构成中枢的笔数（>= 3）。
    """

    start_index: int
    end_index: int
    start_ts: str
    end_ts: str
    zg: float
    zd: float
    gg: float
    dd: float
    stroke_count: int


@dataclass(frozen=True)
class ChanDivergence:
    """背驰判定结果（只用截至最后一根bar的数据，无未来函数）。

    Attributes
    ----------
    kind : {"top", "bottom"}
        顶背驰（看跌）/ 底背驰（看涨）。
    ts, price : str, float
        发生背驰的那一笔的终点时间与价格。
    strength : float
        背驰强度 0~1，由 MACD 面积衰减（主）与 DIF 衰减（辅）融合而成。
    inputs : dict
        判定明细（面积比、DIF 极值比等），只含可 JSON 序列化的叶子值。
    """

    kind: Literal["top", "bottom"]
    ts: str
    price: float
    strength: float
    inputs: dict = field(default_factory=dict)


@dataclass
class ChanStructure:
    """一次结构识别的完整结果。

    ``available=False`` 时 ``gap`` 说明原因，且 ``fractals`` / ``strokes`` / ``pivots``
    一律为空、``last_stroke`` 为 None（调用方只需判 available 就能安全降级）。
    """

    available: bool
    gap: str | None
    bars_used: int
    merged_bars: int
    fractals: list[ChanFractal] = field(default_factory=list)
    strokes: list[ChanStroke] = field(default_factory=list)
    pivots: list[ChanPivot] = field(default_factory=list)
    last_stroke: ChanStroke | None = None

    def to_dict(self) -> dict:
        """转成只含叶子字段的纯字典（可直接 json.dumps，不含 DataFrame）。"""
        return {
            "available": self.available,
            "gap": self.gap,
            "bars_used": self.bars_used,
            "merged_bars": self.merged_bars,
            "fractals": [asdict(item) for item in self.fractals],
            "strokes": [asdict(item) for item in self.strokes],
            "pivots": [asdict(item) for item in self.pivots],
            "last_stroke": None if self.last_stroke is None else asdict(self.last_stroke),
        }


# ==================== 内部结构 ====================


@dataclass
class _MergedBar:
    """包含关系处理后的合并K线（内部结构，不对外暴露）。

    ``high_pos`` / ``low_pos`` 记录最高价、最低价分别来自哪一根原始K线（清洗后的**位置下标**），
    这样分型才能报到真实产生极值的那根K线上。
    """

    index: int
    high: float
    low: float
    high_pos: int
    low_pos: int
    ts_high: str
    ts_low: str


@dataclass
class _CleanBars:
    """清洗后的K线视图：位置下标与输入 bars 的行位置一一对应。"""

    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray | None
    ts: list[str]
    positions: list[int]


# ==================== 输入归一化 ====================


def _is_missing(value: object) -> bool:
    """判断标量是否缺失（None / NaN / NaT）；异常类型一律按缺失处理。"""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _ts_text(value: object, fallback: object) -> str:
    """取时间戳文本；缺失或空串时退回 fallback（通常是 df.index 的对应值）。"""
    if _is_missing(value):
        return str(fallback)
    text = str(value).strip()
    return text if text else str(fallback)


def _column_map(frame: pd.DataFrame) -> dict[str, Any]:
    """列名映射：``小写去空白 → 实际列``；重复列名保留第一个。"""
    mapping: dict[str, Any] = {}
    for column in frame.columns:
        mapping.setdefault(str(column).strip().lower(), column)
    return mapping


def _numeric(bars: pd.DataFrame, column: Any) -> np.ndarray:
    """取一列并转成 float64 数组；无法解析的值变 NaN，绝不抛异常。"""
    try:
        values = pd.to_numeric(bars[column], errors="coerce")
        return np.asarray(values, dtype="float64")
    except (TypeError, ValueError):
        return np.full(len(bars), np.nan, dtype="float64")


def _clean_bars(bars: pd.DataFrame | None) -> tuple[_CleanBars | None, str]:
    """归一化输入K线，返回 ``(清洗结果, 失败原因)``；失败时结果为空、原因非空。"""
    if not isinstance(bars, pd.DataFrame) or len(bars) == 0:
        return None, "输入K线为空"
    columns = _column_map(bars)
    high_column = columns.get("high")
    low_column = columns.get("low")
    if high_column is None or low_column is None:
        return None, f"缺少 high/low 列（实际列：{list(bars.columns)[:8]}）"
    high = _numeric(bars, high_column)
    low = _numeric(bars, low_column)
    # 数据源偶发 high<low，按大小关系纠正；NaN 会被 np.maximum 传播，随后统一剔除
    upper = np.maximum(high, low)
    lower = np.minimum(high, low)
    keep = np.isfinite(upper) & np.isfinite(lower)
    if not keep.any():
        return None, "剔除 NaN/Inf 后没有可用K线"

    close_column = columns.get("close")
    closes = _numeric(bars, close_column) if close_column is not None else None

    index_labels = list(bars.index)
    ts_column = columns.get("ts")
    if ts_column is None:
        texts = [str(label) for label in index_labels]
    else:
        texts = [
            _ts_text(value, index_labels[pos])
            for pos, value in enumerate(bars[ts_column].tolist())
        ]

    positions = np.nonzero(keep)[0]
    return _CleanBars(
        highs=upper[keep],
        lows=lower[keep],
        closes=None if closes is None else closes[keep],
        ts=[texts[int(pos)] for pos in positions],
        positions=[int(pos) for pos in positions],
    ), ""


# ==================== 包含关系处理 ====================


def _is_contained(high1: float, low1: float, high2: float, low2: float) -> bool:
    """**严格**包含关系判定：只有一根K线真正包住另一根时才算包含。

    成立的情况只有三种：

    1. ``h1 > h2 且 l1 < l2`` —— 第2根严格落在第1根内部；
    2. ``h2 > h1 且 l2 < l1`` —— 第1根严格落在第2根内部；
    3. ``h1 == h2 且 l1 == l2`` —— 两根完全重合（两个方向同时成立）。

    **不算**包含的情况（参考实现踩过的坑）：用 ``h1>=h2 and l1<=l2`` 这类宽松比较，
    会把「平顶但低点不同」「平底但高点不同」的相邻K线也并掉，凭空抹掉一个真实的高低点，
    进而吃掉分型、改变笔的方向。因此端点只是**相等**（``h1==h2`` 或 ``l1==l2``）
    而另一端不等价时，一律不合并。
    """
    if high1 == high2 and low1 == low2:
        return True
    return (high1 > high2 and low1 < low2) or (high2 > high1 and low2 < low1)


def _fallback_direction(merged: list[_MergedBar], last: _MergedBar) -> Literal["up", "down"]:
    """方向未知时（首次遇到包含），用前两根已合并K线的高低关系定方向。"""
    if len(merged) < 2:
        # 只有一根已合并K线，无从判断走势方向，按向上处理（与参考实现一致）
        return "up"
    prev = merged[-2]
    if last.high > prev.high:
        return "up"
    if last.high < prev.high:
        return "down"
    return "up" if last.low >= prev.low else "down"


def _merge_inclusion(clean: _CleanBars) -> list[_MergedBar]:
    """顺序递推处理包含关系，返回无包含关系的合并K线序列。

    缠论原文（第65课）强调包含关系**不符合传递律**，必须「先用第1、2根K线的包含关系
    确认新的K线，然后用新的K线去和第三根比」，因此这里只能顺序递推，
    不能对原始序列两两独立判断。

    合并方向：向上取「高高」（``max(h)`` / ``max(l)``），向下取「低低」
    （``min(h)`` / ``min(l)``）；方向由已合并K线之间的高低关系递推得到。
    """
    merged: list[_MergedBar] = []
    direction: Literal["up", "down"] | None = None

    for pos in range(len(clean.highs)):
        high = float(clean.highs[pos])
        low = float(clean.lows[pos])
        text = clean.ts[pos]

        if not merged:
            merged.append(_MergedBar(index=0, high=high, low=low, high_pos=pos,
                                     low_pos=pos, ts_high=text, ts_low=text))
            continue

        last = merged[-1]
        if _is_contained(last.high, last.low, high, low):
            if direction is None:
                direction = _fallback_direction(merged, last)
            if direction == "up":
                # 向上：最高点取高，低点也取两者中较高的那个
                if high >= last.high:
                    last.high, last.high_pos, last.ts_high = high, pos, text
                if low >= last.low:
                    last.low, last.low_pos, last.ts_low = low, pos, text
            else:
                # 向下：最低点取低，高点也取两者中较低的那个
                if high <= last.high:
                    last.high, last.high_pos, last.ts_high = high, pos, text
                if low <= last.low:
                    last.low, last.low_pos, last.ts_low = low, pos, text
            continue

        # 非包含：用当前K线与上一根已合并K线的高低关系更新走势方向（供下次合并使用）
        if high > last.high:
            direction = "up"
        elif high < last.high:
            direction = "down"
        elif low > last.low:
            direction = "up"
        else:
            direction = "down"
        merged.append(_MergedBar(index=len(merged), high=high, low=low, high_pos=pos,
                                 low_pos=pos, ts_high=text, ts_low=text))

    return merged


# ==================== 分型 ====================


def _find_fractals(merged: list[_MergedBar], positions: list[int],
                   texts: list[str]) -> list[ChanFractal]:
    """在合并K线上识别分型：**从左向右（向前）扫描，取第一个满足条件的三K组合**。

    顶分型 = 中间那根的高点最高**且**低点最高；底分型 = 中间那根的低点最低**且**高点最低。
    比较一律用严格不等号，平顶/平底（高点相等）不构成分型。

    为什么强调「向前扫描」：参考实现曾用从右向左的回扫，导致相邻分型的先后顺序被颠倒、
    笔的方向整体反向，且最后一根K线会被当成已确认的分型（隐含未来函数）。这里严格
    按时间升序扫描，且不把最后一根合并K线作为分型中心（它缺少右侧确认K线）。
    """
    out: list[ChanFractal] = []
    for i in range(1, len(merged) - 1):
        left, mid, right = merged[i - 1], merged[i], merged[i + 1]
        is_top = (
            mid.high > left.high and mid.high > right.high
            and mid.low > left.low and mid.low > right.low
        )
        is_bottom = (
            mid.low < left.low and mid.low < right.low
            and mid.high < left.high and mid.high < right.high
        )
        if is_top and not is_bottom:
            out.append(ChanFractal(index=mid.index, bar_index=positions[mid.high_pos],
                                   ts=texts[mid.high_pos], kind="top", price=mid.high))
        elif is_bottom and not is_top:
            out.append(ChanFractal(index=mid.index, bar_index=positions[mid.low_pos],
                                   ts=texts[mid.low_pos], kind="bottom", price=mid.low))
    return out


def _merge_same_kind(fractals: list[ChanFractal]) -> list[ChanFractal]:
    """相邻同类型分型只保留**更极端**的一个（顶取更高、底取更低）。

    这是「笔的唯一划分」的前提：分型序列必须严格顶底交替。
    """
    out: list[ChanFractal] = []
    for fractal in fractals:
        if not out:
            out.append(fractal)
            continue
        last = out[-1]
        if fractal.kind != last.kind:
            out.append(fractal)
            continue
        better = (fractal.price > last.price if fractal.kind == "top"
                  else fractal.price < last.price)
        if better:
            out[-1] = fractal
    return out


# ==================== 笔 ====================


def _stroke_amplitude_pct(start_price: float, end_price: float) -> float:
    """笔振幅百分比；起点价退化（0）时返回 0，不产生 inf。"""
    scale = abs(start_price)
    if not math.isfinite(scale) or scale <= _TINY:
        return 0.0
    return abs(end_price - start_price) / scale * 100.0


def _build_strokes(fractals: list[ChanFractal], min_gap: int) -> list[ChanStroke]:
    """由交替分型构造笔（新笔口径：两个分型之间原始K线间隔 >= ``min_gap``）。

    规则：
    1. 相邻分型必须类型交替（顶→底→顶…），同类型连续出现时取更极端者作为锚点；
    2. 异类型分型之间的**原始K线间隔** ``>= min_gap``（默认 4，即新笔定义下
       顶底之间至少还有 1 根独立K线）；间隔不足的分型直接忽略，锚点保持不变；
    3. 价格有效性：顶→底要求底确实更低，底→顶要求顶确实更高。
    """
    if len(fractals) < 2:
        return []

    strokes: list[ChanStroke] = []
    anchor = fractals[0]
    for fractal in fractals[1:]:
        if fractal.kind == anchor.kind:
            better = (fractal.price > anchor.price if anchor.kind == "top"
                      else fractal.price < anchor.price)
            if better:
                anchor = fractal
            continue

        if fractal.bar_index - anchor.bar_index < min_gap:
            # 间隔不足（顶底之间没有独立K线）→ 该分型不成立，锚点不动
            continue

        if anchor.kind == "top":
            if fractal.price >= anchor.price:   # 顶→底：底必须更低
                continue
            direction: Literal["up", "down"] = "down"
        else:
            if fractal.price <= anchor.price:   # 底→顶：顶必须更高
                continue
            direction = "up"

        strokes.append(ChanStroke(
            start_index=anchor.bar_index,
            end_index=fractal.bar_index,
            start_ts=anchor.ts,
            end_ts=fractal.ts,
            start_price=anchor.price,
            end_price=fractal.price,
            direction=direction,
            high=max(anchor.price, fractal.price),
            low=min(anchor.price, fractal.price),
            amplitude_pct=_stroke_amplitude_pct(anchor.price, fractal.price),
        ))
        anchor = fractal

    return strokes


# ==================== 中枢 ====================


def _build_pivots(strokes: list[ChanStroke]) -> list[ChanPivot]:
    """由笔构造中枢：连续三笔重叠即成立，随后向后延伸至出现完全不重叠的笔。

    ``zg = min(三笔高点)``、``zd = max(三笔低点)``，要求 ``zg > zd`` 才成立；
    延伸时只要 ``stroke.low <= zg and stroke.high >= zd`` 就并入并同步更新 GG/DD。
    中枢之间**不共享笔**：上一个中枢结束后的下一笔开始找下一个中枢。
    """
    pivots: list[ChanPivot] = []
    total = len(strokes)
    if total < _MIN_STROKES:
        return pivots

    start = 0
    while start + _MIN_STROKES - 1 < total:
        window = strokes[start:start + _MIN_STROKES]
        zg = min(stroke.high for stroke in window)
        zd = max(stroke.low for stroke in window)
        if zg <= zd:
            start += 1          # 三笔无重叠 → 窗口右移一格，重新找中枢
            continue

        end = start + _MIN_STROKES
        gg = max(stroke.high for stroke in window)
        dd = min(stroke.low for stroke in window)
        while end < total:
            stroke = strokes[end]
            if stroke.low > zg or stroke.high < zd:
                break           # 完全脱离 [zd, zg] → 中枢结束
            gg = max(gg, stroke.high)
            dd = min(dd, stroke.low)
            end += 1

        pivots.append(ChanPivot(
            start_index=strokes[start].start_index,
            end_index=strokes[end - 1].end_index,
            start_ts=strokes[start].start_ts,
            end_ts=strokes[end - 1].end_ts,
            zg=zg,
            zd=zd,
            gg=gg,
            dd=dd,
            stroke_count=end - start,
        ))
        start = end

    return pivots


# ==================== 公开接口 ====================


def build_structure(bars: pd.DataFrame | None, *, min_bars: int = 30,
                    min_gap: int = 4) -> ChanStructure:
    """从K线构造缠论结构（分型 → 笔 → 中枢）。

    Parameters
    ----------
    bars : DataFrame | None
        含 ``high`` / ``low``（必需）与 ``ts`` / ``close``（可选）的K线，按时间升序。
    min_bars : int
        有效K线数量下限，默认 30（少于该数量直接判为不可用，避免用极少样本编结构）。
    min_gap : int
        相邻顶底分型之间的**原始K线间隔**下限，默认 4（新笔口径）。

    Returns
    -------
    ChanStructure
        数据不可用/样本不足时不抛异常，返回 ``available=False`` + ``gap`` 说明原因，
        且 ``fractals`` / ``strokes`` / ``pivots`` 均为空。
    """
    clean, reason = _clean_bars(bars)
    if clean is None:
        return ChanStructure(available=False, gap=reason, bars_used=0, merged_bars=0)

    bars_used = len(clean.highs)
    floor = max(int(min_bars), 1)
    if bars_used < floor:
        return ChanStructure(available=False, gap=f"有效K线不足：{bars_used} < {floor}",
                             bars_used=bars_used, merged_bars=0)

    merged = _merge_inclusion(clean)
    merged_count = len(merged)
    if merged_count < _MIN_MERGED_BARS:
        return ChanStructure(
            available=False, gap=f"包含合并后K线不足：{merged_count} < {_MIN_MERGED_BARS}",
            bars_used=bars_used, merged_bars=merged_count)

    fractals = _merge_same_kind(_find_fractals(merged, clean.positions, clean.ts))
    if len(fractals) < 2:
        return ChanStructure(
            available=False, gap=f"分型不足：{len(fractals)} < 2（走势过于平滑或方向单一）",
            bars_used=bars_used, merged_bars=merged_count)

    gap_floor = max(int(min_gap), 1)
    strokes = _build_strokes(fractals, gap_floor)
    if not strokes:
        return ChanStructure(
            available=False,
            gap=f"未形成有效笔（相邻分型原始K线间隔不足 {gap_floor}，或价格未创新高/低）",
            bars_used=bars_used, merged_bars=merged_count)

    pivots = _build_pivots(strokes)
    return ChanStructure(
        available=True, gap=None, bars_used=bars_used, merged_bars=merged_count,
        fractals=fractals, strokes=strokes, pivots=pivots, last_stroke=strokes[-1])


def pivot_position(structure: ChanStructure, price: float) -> tuple[float | None, str]:
    """现价在**最后一个中枢**中的相对位置。

    位置定义 ``p = (price - zd) / (zg - zd)``：``p < 0`` 中枢下方，
    ``0 <= p <= 1`` 中枢内，``p > 1`` 中枢上方。

    Returns
    -------
    tuple[float | None, str]
        ``(p, 中文区域说明)``；没有中枢 / 价格无效 / 中枢区间退化时 ``p`` 为 None，
        说明分别为 ``"无中枢"`` / ``"价格无效"`` / ``"中枢区间退化"``。
    """
    if structure is None or not structure.pivots:
        return None, "无中枢"

    pivot = structure.pivots[-1]
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None, "价格无效"
    if not math.isfinite(value):
        return None, "价格无效"

    span = pivot.zg - pivot.zd
    if not math.isfinite(span) or span <= _TINY:
        return None, "中枢区间退化"      # zg <= zd，无法定义区间位置

    position = (value - pivot.zd) / span
    if position < 0.0:
        return position, "中枢下方"
    if position > 1.0:
        return position, "中枢上方"
    return position, "中枢内"


# ==================== 背驰 ====================


def _macd_from_frame(macd: pd.DataFrame | None,
                     n_rows: int) -> tuple[np.ndarray | None, np.ndarray | None]:
    """从调用方传入的 macd DataFrame 取 (dif, hist)；不可用返回 (None, None)。

    要求与 bars **行数一致**（逐行对齐）；长度不符一律视为不可用，交由本地计算兜底，
    避免错位对齐静默算出错误的背驰。列缺失时可互相反推（``hist = 2*(dif-dea)``）。
    """
    if not isinstance(macd, pd.DataFrame) or n_rows <= 0 or len(macd) != n_rows:
        return None, None
    columns = _column_map(macd)
    dif_column = columns.get("dif")
    dea_column = columns.get("dea")
    hist_column = columns.get("hist", columns.get("macd"))
    dif = _numeric(macd, dif_column) if dif_column is not None else None
    dea = _numeric(macd, dea_column) if dea_column is not None else None
    hist = _numeric(macd, hist_column) if hist_column is not None else None
    if dif is None and dea is not None and hist is not None:
        dif = dea + hist / 2.0
    if hist is None and dif is not None and dea is not None:
        hist = (dif - dea) * 2.0
    if dif is None or hist is None:
        return None, None
    return dif, hist


def _macd_from_close(closes: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """用收盘价现算 MACD(12,26,9)，口径与 ``indicators.macd`` 完全一致。

    DIF = EMA(close,12) - EMA(close,26)，DEA = EMA(DIF,9)，HIST = 2×(DIF-DEA)。
    """
    series = pd.Series(closes, dtype="float64").replace([np.inf, -np.inf], np.nan)
    series = series.ffill().bfill()
    if not series.notna().any():
        return None
    ema_fast = series.ewm(span=_MACD_FAST, adjust=False).mean()
    ema_slow = series.ewm(span=_MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=_MACD_SIGNAL, adjust=False).mean()
    return (dif.to_numpy(dtype="float64"),
            (2.0 * (dif - dea)).to_numpy(dtype="float64"))


def _resolve_macd(bars: pd.DataFrame | None, macd: pd.DataFrame | None,
                  n_rows: int) -> tuple[np.ndarray, np.ndarray] | None:
    """取得与 bars 行位置对齐的 (dif, hist)：优先用传入的 macd，否则用 close 现算。"""
    dif, hist = _macd_from_frame(macd, n_rows)
    if dif is not None and hist is not None:
        return (np.nan_to_num(dif, nan=0.0, posinf=0.0, neginf=0.0),
                np.nan_to_num(hist, nan=0.0, posinf=0.0, neginf=0.0))

    if not isinstance(bars, pd.DataFrame) or len(bars) == 0:
        return None
    close_column = _column_map(bars).get("close")
    if close_column is None:
        return None            # 无 close 且 macd 不可用 → 无法判定背驰
    closes = _numeric(bars, close_column)
    return _macd_from_close(closes)


def _range_slice(values: np.ndarray, start: int, end: int) -> np.ndarray:
    """取 ``[start, end]``（含端点、自动排序并裁剪到数组范围内）的切片。"""
    if values.size == 0:
        return values
    low = max(0, min(int(start), int(end)))
    high = min(values.size - 1, max(int(start), int(end)))
    if high < low:
        return values[0:0]
    return values[low:high + 1]


def _stroke_area(hist: np.ndarray, stroke: ChanStroke) -> float:
    """一笔对应的 MACD 柱面积（向上笔累加正柱、向下笔累加负柱的绝对值）。"""
    segment = _range_slice(hist, stroke.start_index, stroke.end_index)
    if segment.size == 0:
        return 0.0
    if stroke.direction == "up":
        area = float(segment[segment > 0].sum())
    else:
        area = float(-segment[segment < 0].sum())
    if area <= _TINY:
        # 该笔区间内没有同向柱（例如整段都在0轴另一侧）→ 退化为绝对面积，避免 0/0
        area = float(np.abs(segment).sum())
    return area


def _stroke_dif_peak(dif: np.ndarray, stroke: ChanStroke) -> float:
    """一笔区间内的 DIF 极值：向上笔取最大值（红柱峰），向下笔取最小值（绿柱谷）。"""
    segment = _range_slice(dif, stroke.start_index, stroke.end_index)
    if segment.size == 0:
        return 0.0
    return float(segment.max()) if stroke.direction == "up" else float(segment.min())


def _clip01(value: float) -> float:
    """裁剪到 [0, 1]。"""
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _safe_float(value: float, digits: int = 6) -> float | None:
    """JSON 友好的浮点：非有限值返回 None（避免 json.dumps 产出 NaN）。"""
    if not math.isfinite(value):
        return None
    return round(float(value), digits)


def detect_divergence(bars: pd.DataFrame | None, structure: ChanStructure,
                      macd: pd.DataFrame | None = None,
                      *, area_ratio_max: float = 0.85) -> ChanDivergence | None:
    """笔级别背驰判定（**只用截至最后一根bar的数据**，无未来函数）。

    判定口径
    --------
    取最后两个**同向**笔（中间夹着一笔反向笔）比较：

    1. 价格创新极值：最后一笔向上且 ``end_price`` 高于前一个向上笔的 ``end_price``
       （向下笔对称，要求创新低）；
    2. MACD 面积衰减：本笔区间内的同向柱面积 / 前一同向笔面积 ``< area_ratio_max``；
    3. DIF 峰值衰减：本笔区间的 DIF 极值未能同步创新高（向下笔为未能创新低）。

    三条同时满足才判为背驰（顶背驰 / 底背驰）。``strength = clip(1 - 面积比)`` 与
    ``clip(1 - DIF比)`` 的加权融合（面积权重 0.7），落在 0~1。

    Parameters
    ----------
    bars : DataFrame | None
        原始K线（需要 ``close`` 列才能现算 MACD）。
    structure : ChanStructure
        :func:`build_structure` 的结果；不可用或笔数不足时直接返回 None。
    macd : DataFrame | None
        含 ``dif`` / ``dea`` / ``hist``（或 ``macd``）列的 MACD，必须与 bars **行数一致**；
        为 None 或不可用时用 bars 的 close 现算 MACD(12,26,9)。
    area_ratio_max : float
        面积比阈值，默认 0.85。

    Returns
    -------
    ChanDivergence | None
        无背驰（或数据不足、无法计算）返回 None。
    """
    if structure is None or not structure.available:
        return None
    strokes = structure.strokes
    if len(strokes) < _MIN_STROKES:
        return None

    current = strokes[-1]
    previous: ChanStroke | None = None
    for offset, stroke in enumerate(reversed(strokes[:-1]), start=1):
        if stroke.direction == current.direction:
            # 同向笔之间必须夹着至少一笔反向笔（笔序列本应严格交替）
            if offset >= 2:
                previous = stroke
            break
    if previous is None:
        return None

    n_rows = len(bars) if isinstance(bars, pd.DataFrame) else 0
    series = _resolve_macd(bars, macd, n_rows)
    if series is None:
        return None
    dif, hist = series

    area_prev = _stroke_area(hist, previous)
    area_cur = _stroke_area(hist, current)
    if area_prev <= _TINY:
        return None
    area_ratio = area_cur / area_prev

    dif_prev = _stroke_dif_peak(dif, previous)
    dif_cur = _stroke_dif_peak(dif, current)

    if current.direction == "up":
        made_extreme = current.end_price > previous.end_price      # 价格创新高
        dif_decayed = dif_cur < dif_prev                          # DIF 峰值下降
        kind: Literal["top", "bottom"] = "top"
    else:
        made_extreme = current.end_price < previous.end_price      # 价格创新低
        dif_decayed = dif_cur > dif_prev                          # DIF 谷值抬升
        kind = "bottom"

    if not made_extreme or not dif_decayed or area_ratio >= area_ratio_max:
        return None

    dif_scale = abs(dif_prev)
    dif_ratio = abs(dif_cur) / dif_scale if dif_scale > _TINY else 1.0
    strength = _clip01(
        _AREA_WEIGHT * _clip01(1.0 - area_ratio) + (1.0 - _AREA_WEIGHT) * _clip01(1.0 - dif_ratio)
    )

    return ChanDivergence(
        kind=kind,
        ts=current.end_ts,
        price=current.end_price,
        strength=strength,
        inputs={
            "area_ratio": _safe_float(area_ratio),
            "area_ratio_max": _safe_float(area_ratio_max),
            "area_prev": _safe_float(area_prev),
            "area_cur": _safe_float(area_cur),
            "dif_ratio": _safe_float(dif_ratio),
            "dif_peak_prev": _safe_float(dif_prev),
            "dif_peak_cur": _safe_float(dif_cur),
            "prev_end_ts": previous.end_ts,
            "prev_end_price": _safe_float(previous.end_price),
            "cur_end_price": _safe_float(current.end_price),
        },
    )
