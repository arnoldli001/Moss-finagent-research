"""个股股性画像：把「这只票的脾气」量化成可用的权重与档位。

## 为什么需要它

同一套做T参数不可能适配所有票，这不是理论问题而是实测问题：

- 300308（中际旭创）日线 ATR 占价约 5.8%，箱体下沿到上沿动辄 10% ——
  1% 的止损距离只有 9 元，任何噪声都能打穿；VWAP 偏 0.3% 就报警毫无意义。
- 600036（招商银行）ATR 只占 1.7%，档位差若给到 3% 则一年也等不到一次。

项目原本用 `config.CodeOverride`（`configs/intraday.yaml` 的 `overrides:` 段）手工填参数
来解决，但「该填多少」全靠试错。本模块把这个试错换成**从该股自己的历史反推**。

## 更细的一层：均值回归 vs 趋势

同样是「VWAP 负偏离 5%」：

- 在**震荡票**上是低吸机会（历史上这种偏离平均 2 小时内回归）；
- 在**单边下跌的趋势票**上是接飞刀（历史上偏离后继续偏离）。

所以股性不只决定「档位给多宽」，还决定**哪些因子的权重该高**：
震荡票加重箱体/VWAP/布林（均值回归族），趋势票加重缠论结构与 MACD（趋势族）。
这正是前端「按个股股性自定义权重」的默认推荐值来源。

## 数据来源与降级

全部来自日线历史（默认 250 根，最少 60 根），不依赖任何付费数据。
任何一项取不到时**如实置 None 并记 `gap`**，绝不填 0 冒充「已经算过了」——
0 会静默参与 `t_friendly` 加权，把一只活跃票算成钝化票。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

from src.intraday import indicators as ind
from src.intraday.weight_profiles import (
    Mode,
    normalize_weights,
)
from src.intraday.weight_profiles import (
    template as get_template,
)

# 股性画像最少需要的日线样本：少于这个根数，ATR20/60日涨停频率都没意义。
MIN_SAMPLE_DAYS = 60
# 默认取样长度：覆盖一轮完整的中级周期（约1年），又不会被三年前的旧股性污染。
DEFAULT_SAMPLE_DAYS = 250

Regime = Literal["swing", "mixed", "trend"]
Grade = Literal["活跃", "温和", "钝化"]


def _clip01(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    return min(1.0, max(0.0, float(value)))


def limit_up_pct_for(code: str) -> float:
    """该代码的涨停幅度（%）：创业板/科创板 20cm，其余 10cm。

    北交所（8xx/4xx/920）本模块数据源未覆盖，按 30cm 口径给出但不会进入做T链路
    （路由层 `_validate_code` 已拦截）。
    """
    target = str(code or "").strip()
    if target.startswith(("300", "301", "688", "689")):
        return 20.0
    if target.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


@dataclass
class CharacterProfile:
    """个股股性画像（可 JSON 序列化，直接进 API 与前端）。"""

    code: str = ""
    name: str = ""
    available: bool = False
    mode: Mode = "intraday"
    sampled_days: int = 0
    source: str = "daily_bars"
    gap: str | None = None

    # ---- 波动与活跃度 ----
    atr_pct: float | None = None
    avg_amplitude_pct: float | None = None
    avg_turnover: float | None = None
    volume_activity: float | None = None

    # ---- 结构特征 ----
    trend_efficiency: float | None = None
    gap_frequency: float | None = None
    limit_up_count: int = 0
    limit_up_freq: float | None = None
    volatility_percentile: float | None = None

    # ---- 结论 ----
    regime: Regime = "mixed"
    grade: Grade = "温和"
    t_friendly: int = 0
    template: str = "balanced"
    weights: dict[str, float] = field(default_factory=dict)
    levels: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "available": self.available,
            "mode": self.mode, "sampled_days": self.sampled_days,
            "source": self.source, "gap": self.gap,
            "atr_pct": self.atr_pct, "avg_amplitude_pct": self.avg_amplitude_pct,
            "avg_turnover": self.avg_turnover, "volume_activity": self.volume_activity,
            "trend_efficiency": self.trend_efficiency,
            "gap_frequency": self.gap_frequency,
            "limit_up_count": self.limit_up_count,
            "limit_up_freq": self.limit_up_freq,
            "volatility_percentile": self.volatility_percentile,
            "regime": self.regime, "grade": self.grade,
            "t_friendly": self.t_friendly, "template": self.template,
            "weights": dict(self.weights), "levels": dict(self.levels),
            "notes": list(self.notes),
        }


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series | None:
    if frame is None or column not in frame.columns:
        return None
    series = pd.to_numeric(frame[column], errors="coerce")
    if series is None or len(series) == 0:
        return None
    return series


def _prepare(bars: pd.DataFrame | None) -> tuple[pd.DataFrame | None, bool]:
    """清洗日线 → (清洗后的表, 开盘价是否不可信)。

    第二个返回值是实测踩出来的坑：日线链路在部分数据源下**没有开盘价**
    （`daily_bars_from_points` 会把缺失的 open 用 close 填上）。
    此时「跳空频率」就退化成「当日涨跌幅超过1%的频率」——
    实测 300308 因此报出 **67% 的跳空频率**，而它的真实跳空远没这么频繁。
    宁可把这个指标判为不可得（并写进 notes），也不能拿一个语义完全不同的数冒充它。
    """
    if bars is None or len(bars) == 0:
        return None, False
    frame = bars.copy()
    close = _numeric(frame, "close")
    if close is None:
        return None, False
    frame["close"] = close
    open_series = _numeric(frame, "open")
    open_missing = open_series is None
    for column in ("open", "high", "low"):
        series = _numeric(frame, column)
        frame[column] = close if series is None else series.fillna(close)
    frame = frame[frame["close"].notna() & (frame["close"] > 0)]
    if len(frame) == 0:
        return None, False
    frame = frame.reset_index(drop=True).tail(DEFAULT_SAMPLE_DAYS).reset_index(drop=True)
    # 即便有 open 列，若它与 close 几乎完全相等，说明同样是被填充出来的
    same_ratio = float((frame["open"] == frame["close"]).mean())
    return frame, open_missing or same_ratio > 0.99


def _limit_up_count(frame: pd.DataFrame, code: str, tolerance: float = 0.3) -> int:
    """样本内涨停天数（按该代码的涨停幅度判定，留 tolerance 容差）。"""
    close = frame["close"]
    prev = close.shift(1)
    pct = (close / prev - 1.0) * 100.0
    threshold = limit_up_pct_for(code) - tolerance
    return int((pct >= threshold).sum())


def _trend_efficiency(close: pd.Series, window: int) -> float | None:
    """趋势效率 = |区间净涨跌| / Σ|每日涨跌| ∈ [0,1]。

    这是区分「震荡票」与「趋势票」最稳的一个量：
    - 来回震荡：每天涨跌都很大，但净位移接近 0 → 效率 → 0
    - 单边趋势：每天的涨跌都在同一方向累积 → 效率 → 1
    比「用均线角度」「用ADX」更不容易被一两个跳空带偏。
    """
    if len(close) < window + 1:
        return None
    segment = close.tail(window + 1)
    net = abs(float(segment.iloc[-1]) - float(segment.iloc[0]))
    path = float(segment.diff().abs().sum())
    if path <= 0:
        return None
    return round(min(1.0, net / path), 4)


def _grade_for(atr_pct: float) -> Grade:
    if atr_pct < 1.5:
        return "钝化"
    if atr_pct < 3.5:
        return "温和"
    return "活跃"


def _regime_for(efficiency: float) -> Regime:
    if efficiency < 0.35:
        return "swing"
    if efficiency > 0.55:
        return "trend"
    return "mixed"


def _t_friendly_score(*, avg_amplitude_pct: float, trend_efficiency: float,
                      volume_activity: float, limit_up_freq: float) -> int:
    """做T友好度 0~100：空间35 + 回归性30 + 活跃度20 + 涨停基因15。

    权重来源是「做T这件事本身要什么」，不是拟合出来的：
    - **空间**（35）：没有振幅就没有价差，档位差覆盖不了双边摩擦成本（约0.2%）。
    - **回归性**（30）：最能决定成败的一项。趋势效率 1.0 的票做T必卖飞。
    - **活跃度**（20）：量能萎缩的票挂单滑点大，触发价拿不到。
    - **涨停基因**（15）：有过涨停的票日内弹性更好，反抽更快。
    """
    space = _clip01(avg_amplitude_pct / 6.0) * 35.0
    mean_revert = (1.0 - _clip01(trend_efficiency)) * 30.0
    activity = _clip01((volume_activity - 0.7) / 1.0) * 20.0
    gene = _clip01(limit_up_freq / 0.08) * 15.0
    return int(round(min(100.0, max(0.0, space + mean_revert + activity + gene))))


def _pick_template(*, grade: Grade, regime: Regime, limit_up_freq: float,
                   volume_activity: float) -> str:
    """按股性选最贴近的权重配方。

    优先级刻意是「题材 > 钝化 > 震荡 > 趋势」：
    一只连板妖股的涨停基因与情绪周期权重比它的箱体位置重要得多；
    而钝化票（银行/公用事业）无论趋势效率如何，都只能靠箱体与布林吃饭。
    """
    if limit_up_freq >= 0.06 and grade == "活跃" and volume_activity >= 1.1:
        return "dragon"
    if grade == "钝化":
        return "low_vol"
    if regime == "swing":
        return "swing"
    if regime == "trend":
        return "trend"
    return "balanced"


def _tilt(weights: dict[str, float], *, regime: Regime, grade: Grade,
          limit_up_freq: float, gap_frequency: float | None) -> dict[str, float]:
    """在模板基础上按股性做小幅倾斜（乘数而非重排，保证可解释）。

    为什么用乘数而不是重新分配：用户在前端看到的是「模板 + 这只票的微调」，
    乘数能直接讲成"箱体权重 ×1.15"，比"箱体从 17 变成 19.6"更好追责。
    """
    factors = {key: 1.0 for key in weights}

    def bump(key: str, multiplier: float) -> None:
        if key in factors:
            factors[key] *= multiplier

    if regime == "swing":
        for key in ("box", "vwap", "boll"):
            bump(key, 1.15)
        bump("macd", 0.8)
        bump("chan", 0.9)
    elif regime == "trend":
        for key in ("box", "boll"):
            bump(key, 0.85)
        bump("macd", 1.2)
        bump("chan", 1.25)
    if grade == "钝化":
        bump("character", 1.4)
        bump("cycle", 1.2)
        bump("chip", 0.8)
    if limit_up_freq >= 0.06:
        bump("chip", 1.25)
        bump("cycle", 1.2)
        bump("board_rank", 1.15)
    if gap_frequency is not None and gap_frequency > 0.25:
        # 频繁跳空的票，当日 VWAP 起点不可靠（大缺口后均价线长时间失真）
        bump("vwap", 0.9)
        bump("box", 1.05)
    return {key: value * factors.get(key, 1.0) for key, value in weights.items()}


def _suggest_levels(*, atr_pct: float, grade: Grade) -> dict[str, float]:
    """按股性给做T档位建议（字段名与 `config.LevelParams` 一致，可直接覆盖）。

    `min_band_pct` 是这里最关键的一项：它必须大于做T一轮的双边成本
    （佣金0.05 + 印花税0.05 + 滑点0.1 ≈ 0.2%），否则信号再准也被摩擦吃光；
    同时它要跟着该股的真实日波动走 —— 拿 1.5% 去要求一只 ATR 5.8% 的票，
    等于每分钟都在报警。
    """
    band = min(6.0, max(1.0, round(atr_pct * 0.45, 1)))
    stop = min(5.0, max(0.8, round(atr_pct * 0.55, 1)))
    ceiling = min(20.0, max(3.0, round(atr_pct * 3.0, 1)))
    touch = {"钝化": 0.2, "温和": 0.3, "活跃": 0.5}[grade]
    return {
        "min_band_pct": band,
        "max_band_pct": ceiling,
        "stop_loss_pct": stop,
        "atr_stop_mult": 0.4 if grade == "钝化" else 0.6,
        "touch_band_pct": touch,
        "dip_fallback_atr": 0.3,
    }


def _empty(code: str, name: str, mode: Mode, gap: str) -> CharacterProfile:
    return CharacterProfile(
        code=code, name=name, available=False, mode=mode, gap=gap,
        template="balanced",
        weights=normalize_weights(
            dict(get_template("balanced", mode).weights), mode),
        levels={}, notes=[gap])


def analyze_character(
    bars: pd.DataFrame | None, *, code: str = "", name: str = "",
    mode: Mode = "intraday", template_override: str | None = None,
) -> CharacterProfile:
    """日线 → 个股股性画像（含推荐权重与档位）。

    任何一项算不出来都走「不可用 + gap」而不是编一个数，
    理由见模块 docstring：股性直接决定权重，编出来的数会静默改变买卖信号。
    """
    frame, open_unreliable = _prepare(bars)
    if frame is None or len(frame) < MIN_SAMPLE_DAYS:
        have = 0 if frame is None else len(frame)
        return _empty(code, name, mode,
                      f"日线样本不足（仅{have}根，股性画像至少需要{MIN_SAMPLE_DAYS}根）")

    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    open_ = frame["open"]
    price = float(close.iloc[-1])
    if not math.isfinite(price) or price <= 0:
        return _empty(code, name, mode, "最新收盘价非法，无法计算股性")

    notes: list[str] = []

    # ---- 波动 ----
    atr_series = ind.atr(frame, 20)
    atr_value = ind.last_value(atr_series)
    atr_pct = None if atr_value is None else round(atr_value / price * 100.0, 3)
    prev_close = close.shift(1)
    amplitude = ((high - low) / prev_close * 100.0).dropna()
    avg_amplitude = (
        round(float(amplitude.tail(20).mean()), 3) if len(amplitude) >= 5 else None)
    if atr_pct is None or avg_amplitude is None:
        return _empty(code, name, mode, "日线缺少高低价，ATR/振幅不可得")

    # ---- 量能活跃度 ----
    volume = _numeric(frame, "volume")
    volume_activity = None
    if volume is not None and float(volume.tail(60).mean() or 0) > 0:
        base = float(volume.tail(60).mean())
        volume_activity = round(float(volume.tail(20).mean()) / base, 3)

    # ---- 换手率（日线链路多数没有这一列 → 如实置 None）----
    turnover_series = _numeric(frame, "turnover")
    avg_turnover = None
    if turnover_series is not None and turnover_series.notna().sum() >= 5:
        avg_turnover = round(float(turnover_series.dropna().tail(20).mean()), 3)
    else:
        notes.append("日线未提供换手率列，换手活跃度用「近20日均量/近60日均量」替代")

    # ---- 结构 ----
    efficiency = _trend_efficiency(close, min(60, len(close) - 1))
    if efficiency is None:
        return _empty(code, name, mode, "日线长度不足以估计趋势效率")
    # 跳空频率只在**开盘价可信**时才算 —— 否则它退化成"当日涨跌幅>1%的频率"
    # （实测 300308 因此报出 67% 的假跳空率，见 `_prepare` 的说明）
    gap_frequency: float | None = None
    if not open_unreliable:
        gaps_ratio = (open_ / prev_close - 1.0).abs()
        gap_frequency = round(float((gaps_ratio.dropna().tail(60) > 0.01).mean()), 4)
    limit_ups = _limit_up_count(frame, code)
    limit_up_freq = round(limit_ups / max(1, len(frame)), 4)
    # 波动率分位：当前 ATR% 在该股自身近一年的 ATR% 序列中的位置。
    # 用「自身历史」而不是全市场，才能回答「这只票现在算它自己活跃还是安静」。
    atr_pct_series = (atr_series / close * 100.0).dropna()
    volatility_pctl = ind.percentile_rank(atr_pct_series, atr_pct)
    if volatility_pctl is None and len(atr_pct_series) >= 20:
        volatility_pctl = round(
            float((atr_pct_series < atr_pct).mean()) if atr_pct is not None else 0.0, 4)

    grade = _grade_for(atr_pct)
    regime = _regime_for(efficiency)
    friendly = _t_friendly_score(
        avg_amplitude_pct=avg_amplitude, trend_efficiency=efficiency,
        volume_activity=volume_activity if volume_activity is not None else 1.0,
        limit_up_freq=limit_up_freq)

    # ---- 推荐模板与权重 ----
    template_key = template_override or _pick_template(
        grade=grade, regime=regime, limit_up_freq=limit_up_freq,
        volume_activity=volume_activity if volume_activity is not None else 1.0)
    chosen = get_template(template_key, mode)
    if chosen is None:
        template_key = "balanced"
        chosen = get_template("balanced", mode)
    assert chosen is not None
    blended = _tilt(dict(chosen.weights), regime=regime, grade=grade,
                    limit_up_freq=limit_up_freq, gap_frequency=gap_frequency)
    weights = normalize_weights(blended, mode)

    levels = _suggest_levels(atr_pct=atr_pct, grade=grade) if mode == "intraday" else {}

    # ---- 人话说明 ----
    regime_text = {"swing": "震荡型（均值回归占优，适合高抛低吸）",
                   "mixed": "混合型（震荡与趋势交替）",
                   "trend": "趋势型（单边推进，做T极易卖飞）"}[regime]
    notes.append(
        f"日线 ATR20 占价 {atr_pct:.2f}% → 波动等级「{grade}」；"
        f"趋势效率 {efficiency:.2f} → {regime_text}")
    notes.append(
        f"近20日平均振幅 {avg_amplitude:.2f}%，"
        f"量能活跃度 {volume_activity if volume_activity is not None else float('nan'):.2f}"
        f"（>1 表示近期比两个月前活跃）")
    gap_text = ("跳空频率不可得（日线源未提供开盘价）"
                if gap_frequency is None else f"跳空频率 {gap_frequency * 100:.0f}%")
    notes.append(
        f"近{len(frame)}日涨停 {limit_ups} 次（频率 {limit_up_freq * 100:.1f}%），{gap_text}")
    notes.append(
        f"做T友好度 {friendly}/100："
        + ("值得滚动做T" if friendly >= 60 else
           "可以做但别指望高胜率" if friendly >= 40 else
           "不建议做T（空间或回归性不足）"))
    if mode == "intraday" and levels:
        notes.append(
            f"建议档位差 ≥ {levels['min_band_pct']:.1f}%、止损 {levels['stop_loss_pct']:.1f}%"
            f"（该股 ATR 口径），低于此值的低吸/高抛会被双边摩擦成本吃光")
    notes.append(f"推荐权重配方：{chosen.label}（股性微调后已归一化到100）")

    return CharacterProfile(
        code=code, name=name, available=True, mode=mode,
        sampled_days=len(frame), source="daily_bars", gap=None,
        atr_pct=atr_pct, avg_amplitude_pct=avg_amplitude,
        avg_turnover=avg_turnover, volume_activity=volume_activity,
        trend_efficiency=efficiency, gap_frequency=gap_frequency,
        limit_up_count=limit_ups, limit_up_freq=limit_up_freq,
        volatility_percentile=(
            None if volatility_pctl is None else round(float(volatility_pctl), 4)),
        regime=regime, grade=grade, t_friendly=friendly,
        template=template_key, weights=weights, levels=levels, notes=notes,
    )


def character_score_from(*, dev_pct: float | None, atr_pct: float | None,
                         trend_efficiency: float | None,
                         scale: float = 0.35) -> float | None:
    """股性适配打分核：把「偏离」用**该股自身日波动**归一化，再按震荡性缩放。

    为什么不能只用 vwap 因子：那个 z 是「相对**当日**分时波动」的标准化值，
    早盘前半小时样本极少，z 会失真到 ±3 以外，同一只票在不同日期不可比。
    本因子的分母是该股自己的**日 ATR**，跨日、跨票都稳定：

        ratio = (dev_pct / 100) / (atr_pct / 100 × scale)
              = dev_pct / (atr_pct × scale)     # 偏了几个"该股的日常波动"
        score = -clip(ratio) × (0.5 + 0.5 × 震荡性)

    其中 震荡性 = 1 - trend_efficiency：
    趋势票上同样的偏离要打折（偏离常是趋势本身，不是超买超卖），
    震荡票上全额生效（偏离大概率回归）。

    默认 `scale=0.35`：一只日 ATR 5.8% 的票，VWAP 偏 2% 即给满分 ——
    这是「半日波动」量级的偏离，再大就属于趋势日而不该指望回归。

    入参用 `dev_pct`（现价相对 VWAP 的偏离%，因子上下文里本来就有）而不是
    price/vwap，是为了让「逐bar重放」能用同一支打分核：
    重放表里没有现成的 vwap 列，但有 `dev_pct`，口径完全一致。

    符号与全模块一致：>0 对低吸有利。
    """
    if dev_pct is None or atr_pct is None or atr_pct <= 0:
        return None
    denominator = atr_pct * max(0.05, scale)
    ratio = dev_pct / denominator
    swing_ratio = 1.0 if trend_efficiency is None else (1.0 - _clip01(trend_efficiency))
    return ind.clip(-ratio * (0.5 + 0.5 * swing_ratio))

