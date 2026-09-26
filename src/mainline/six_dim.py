"""主线挖掘：第一层「开源六维基座」（全市场初筛）。

## 这一层干什么

对**全市场**申万一级行业 + 概念板块算六个子模型的分，按最终分排序，
输出**前 `funnel.candidate_ratio`（默认 20%）** 的候选板块给第二层。

六维（权重合计 100，可配）：交易行为 15 / 景气度 20 / 资金流 20 /
筹码结构 15 / 宏观驱动 15 / 技术指标 15。

## 关键设计：横截面分位，不是绝对阈值

每个因子的原始值（如"近 5 日主力净流入 / 流通市值 = 3.2%"）**没有绝对好坏**：
牛市里 3.2% 排不进前 50，熊市里 0.5% 就是第一名。因此每个因子都在**当天的
横截面**上转成 0-100 的分位分（`scoring.percentile_score`），再按子权重合成。

这条选择有三个直接后果：

1. **样本不足必须返回 None**（`percentile_score` 在有效样本 < 5 时返回 None）：
   在 5 个板块里排第一太容易，给出高分等于制造假信号；
2. **跨日不可比**：昨天的 80 分和今天的 80 分不是同一件事。所以分数只用于
   "今天买哪些"，`to_dict()` 里带上 `rank` 就是为了让面板展示排序而不是绝对分；
3. **IC 检验的是排序**：`scoring.spearman` 用的就是秩相关 —— 与打分口径一致。

## 口径事实（写在代码里，避免后人重复踩）

**资金流走「本地行情仓个股聚合」而不是东财板块口径。**
实测 `moneyflow_ind_dc`（东财板块资金流）只有 2024 年起的数据、概念板块更要到
2026 年，覆盖不了 2022 起的回测区间；而且它的"主力"定义与个股口径不同。
本模块固定一套口径（Tushare 个股聚合），东财口径只作展示。见 `datastore.py`。

**景气度只有「已实现盈余动量」，没有分析师预期。**
需求写的是"历史景气度（ROE 变化）+ 预期景气度"。本机没有接入分析师一致预期
数据，因此本维度**只用已实现部分**（ROE 同比 / 净利同比 / 营收同比中位数），
并在 `gaps` 里如实标注。用 0 冒充"预期中性"会让这个维度系统性偏乐观。

**股东户数是季报频率、滞后 1-3 个月。**
`stk_holdernumber` 取不到历史快照（实测 `period` 参数被忽略），只能滚动累积。
回测早期本地表为空时该子因子记为 `None`（不参与加权），而不是当 0。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.mainline.config import MainlineConfig
from src.mainline.macro import (
    MacroSensitivity,
    MacroState,
    blend_macro,
    macro_common_score,
    tilt_score,
)
from src.mainline.models import (
    BoardFlow,
    BoardInfo,
    BoardSeries,
    DimensionScore,
    LayerScore,
)
from src.mainline.scoring import (
    mean,
    median,
    percentile_score,
    safe_ratio,
    to_float,
    weighted_score,
)

logger = logging.getLogger(__name__)

#: 六维各自的**子因子权重**（每个子模型内部合计 1.0）。
#:
#: 为什么不放进 YAML：这些是"子模型内部怎么组合"的实现细节，不是研究口径 ——
#: 调它等于改模型定义（要重跑回测并重写文档），而 YAML 里放的是"今天觉得
#: 合适、明天要对比"的阈值。两者混在一起会让配置文件变成第二个代码库。
SUB_WEIGHTS: dict[str, dict[str, float]] = {
    "trading": {"intraday": 0.60, "overnight": 0.40},
    "prosperity": {"roe_yoy": 0.50, "profit_yoy": 0.30, "revenue_yoy": 0.20},
    "moneyflow": {"net_ratio": 0.50, "elg_ratio": 0.30, "persistence": 0.20},
    "chips": {"profit_ratio": 0.60, "holder_change": 0.40},
    "macro": {"tilt": 1.00},
    "technical": {"ma": 0.40, "rsi": 0.30, "volume": 0.30},
}

#: 景气度维的**低分压制底**（2026-09-22 落地）。
#:
#: ## 做什么
#:
#: 景气度低于 50 分（= 横截面分位 50%）时，把分数按
#:
#:     p' = 50 − (50 − p) · λ,   λ = (50 − FLOOR) / 50 = 0.2
#:
#: 压缩。`FLOOR = 40` 的含义是：**把最低分从 0 抬到 40**，
#: 即 0 分 → 40、25 分 → 45、40 分 → 48、50 分及以上**完全不动**。
#: 分数越高越接近原值，所以它不是"打折"，而是**只削低分的拖累**。
#:
#: ## 为什么做
#:
#: 实测（`scripts/prosperity_rank_report.py`）景气度全市场分位 < 30 的板块有
#: **29 个（占 9%）**，它们的进池比例中位只有 **2%**（分位 > 70 的 41 个板块是
#: 53%）。名单里既有煤炭/煤化工/物业这类成熟价值股，也有硅能源、短剧游戏、
#: 空间计算、DeepSeek、华为盘古、谷子经济这些**新题材** —— 后者的成员公司
#: 小、同比增速为负，于是被同一个维度压住。用户的原话是
#: 「景气度不高时不要影响过多总分」。
#:
#: 离线对照（`docs/MAINLINE_PROSPERITY_VARIANTS.md`，候选层面、只依赖六维排名）
#: 在三个窗口与两种指标上给出的排序是 `floor=40` 最稳：
#:
#:     口径              训练召回  训练FP/TP   留出召回  留出FP/TP
#:     L 水平值（现状）        6%      7.65        6%      7.53
#:     F 低分压制(40)         11%      4.46        8%      6.42   ← 两个窗口两个指标都改善
#:     D 变化率 Δg(60)        13%      3.84        7%      8.44   ← 留出净亏
#:
#: ## 为什么不放进 YAML
#:
#: 与本文件 `SUB_WEIGHTS` 同一条理由：改它等于**改模型定义**（要重跑回测、
#: 重写文档），而 YAML 放的是"今天觉得合适、明天要对比"的阈值。混在一起会
#: 让配置文件变成第二个代码库。
#:
#: ## ⚠️ 已知代价（不要粉饰）
#:
#: 在**告警层面**的离线代理里（保真度仅 47%，见 `docs/MAINLINE_PROSPERITY_RANK.md`），
#: `floor=40` 的留出表现是 P(>+10%) 21.7%（现状 22.1%）、均值 +2.74%（现状 +2.78%）
#: —— **略差**。两个层面结论不一致，以候选层面为准（它只依赖六维排名，
#: 不受"离线不含蓄势层"这个缺口影响）。落地后必须用真实重打分复核。
PROSPERITY_FLOOR = 40.0


#: 景气度子因子名 → 原始值名（两者不同名，是这套代码里最容易接错的一处）
PROSPERITY_RAW_KEYS = {"roe_yoy": "roe_yoy", "profit_yoy": "netprofit_yoy",
                       "revenue_yoy": "or_yoy"}


def compress_low(score: float, floor: float = PROSPERITY_FLOOR) -> float:
    """把 50 分以下的分数按比例压缩到 `floor` 以上（50 分及以上不动）。

    纯函数，便于单测。`floor >= 50` 时等于不压缩（λ=0）。
    """
    if score >= 50.0 or floor >= 50.0:
        return score
    lam = (50.0 - max(floor, 0.0)) / 50.0
    return 50.0 - (50.0 - score) * lam


#: 因子方向：True = 值越大越好（正向分位）；False = 值越小越好（反向分位）。
#:
#: - `overnight` 取**反向**：A 股隔夜跳空与后续收益负相关（隔夜反转效应），
#:   这是"日内动量 + 隔夜反转"这个子模型名字的来源；
#: - `holder_change` 取**反向**：股东户数下降 = 筹码集中 = 好；
#: - `rsi` 不做反向，而是在 `_rsi_score` 里做**单峰映射**（见那里说明）。
POSITIVE: dict[str, bool] = {
    "intraday": True, "overnight": False,
    "roe_yoy": True, "profit_yoy": True, "revenue_yoy": True,
    "net_ratio": True, "elg_ratio": True, "persistence": True,
    "profit_ratio": True, "holder_change": False,
    "ma": True, "rsi": True, "volume": True,
}

#: 维度中文名（面板与报告直接用）
DIM_LABELS = {
    "trading": "交易行为", "prosperity": "景气度", "moneyflow": "资金流",
    "chips": "筹码结构", "macro": "宏观驱动", "technical": "技术指标",
}


@dataclass
class BoardInput:
    """第一层打分需要的一个板块的全部输入（由 service 组装）。"""

    info: BoardInfo
    series: BoardSeries = field(default_factory=lambda: BoardSeries(code=""))
    flow: BoardFlow | None = None
    #: 成分股代码（用于聚合景气度 / 筹码）
    members: list[str] = field(default_factory=list)
    #: 成分股个股指标 `{code: {roe_yoy, netprofit_yoy, or_yoy, circ_mv, holder_change}}`
    member_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 板块流通市值（元）—— 优先用成分股市值合计，其次用资金流表里的聚合值
    circ_mv: float | None = None

    @property
    def code(self) -> str:
        return self.info.code

    @property
    def name(self) -> str:
        return self.info.name


# ==================================================================
# 子因子原始值
# ==================================================================


def _bar_prices(series: BoardSeries) -> tuple[list[float], list[float], list[float],
                                              list[float], list[float]]:
    """把板块日线拆成 (open, close, pre_close, high, low, volume) 六个序列。"""
    opens = [bar.open for bar in series.bars]
    closes = [bar.close for bar in series.bars]
    pres = [bar.pre_close for bar in series.bars]
    highs = [bar.high for bar in series.bars]
    lows = [bar.low for bar in series.bars]
    volumes = [bar.volume for bar in series.bars]
    return opens, closes, pres, highs, lows, volumes


def trading_raw(series: BoardSeries, *, momentum: int, overnight: int
                ) -> dict[str, float | None]:
    """交易行为：日内收益率均值 / 隔夜跳空均值（都取最近 N 日）。

    日内 = (close - open) / open；隔夜 = (open - pre_close) / pre_close。

    ⚠️ `pre_close` 缺失（0）时**跳过该日**而不是当 0 算：把缺失当 0 会让
    隔夜跳空序列被一堆 0 拉平，标准差变小，"隔夜反转"因子整体失效，
    而分数仍然是一个正常的 0-100 —— 不报任何错。
    """
    opens, closes, pres, _, _, _ = _bar_prices(series)
    intraday: list[float] = []
    overnight_gap: list[float] = []
    for index in range(len(closes)):
        open_px, close_px = opens[index], closes[index]
        pre_px = pres[index]
        day = (close_px - open_px) / open_px if open_px else None
        if day is not None:
            intraday.append(day)
        if pre_px and open_px:
            overnight_gap.append((open_px - pre_px) / pre_px)
    return {
        "intraday": mean(intraday[-momentum:]) if momentum > 0 else None,
        "overnight": mean(overnight_gap[-overnight:]) if overnight > 0 else None,
    }


def prosperity_raw(inputs: BoardInput) -> dict[str, float | None]:
    """景气度：成分股 ROE 同比 / 净利同比 / 营收同比的**中位数**。

    用中位数而不是均值：板块里总有一两只重组股或并表股，同比动辄 ±500%，
    均值会被单只票带偏（`scoring.median` 的文档里写了同一条理由）。
    """
    keys = ("roe_yoy", "netprofit_yoy", "or_yoy")
    buckets: dict[str, list[float]] = {key: [] for key in keys}
    for stats in inputs.member_stats.values():
        for key in keys:
            value = to_float(stats.get(key))
            if value is not None:
                buckets[key].append(value)
    return {"roe_yoy": median(buckets["roe_yoy"]),
            "profit_yoy": median(buckets["netprofit_yoy"]),
            "revenue_yoy": median(buckets["or_yoy"])}


def moneyflow_raw(inputs: BoardInput, *, window: int) -> dict[str, float | None]:
    """资金流：净流入/流通市值、超大单净额/流通市值、净流入持续性。

    单位口径：`BoardFlow.points` 的净额已是**元**；`circ_mv` 也是元；
    比率类结果一律是**小数**（不是百分数）。
    """
    flow = inputs.flow
    if flow is None or not flow.points:
        return {"net_ratio": None, "elg_ratio": None, "persistence": None}
    tail = flow.points[-window:] if window > 0 else flow.points
    nets = [net for _, net in tail if net is not None]
    total = sum(nets) if nets else 0.0
    circ = inputs.circ_mv or 0.0
    positive = sum(1 for net in nets if net > 0)
    return {
        "net_ratio": safe_ratio(total, circ) if circ else None,
        # 超大单净额只有截面（单日）才有，因此这里用**最近一日**的口径，
        # 与 net_ratio 的窗口口径不同 —— 面板的 raw 里两者都带原始值，可核对。
        "elg_ratio": (safe_ratio(flow.buy_elg, circ)
                      if circ and flow.buy_elg else None),
        "persistence": (positive / len(nets)) if nets else None,
    }


def chips_raw(inputs: BoardInput, *, window: int) -> dict[str, float | None]:
    """筹码结构：获利盘占比 + 股东户数环比变化中位数。

    **获利盘占比**用"成交量加权的历史价格分布"估算：近 `window` 日里，
    收盘价低于当日收盘的那些交易日的成交量之和，占总成交量的比例。
    这是一个**近似**（用日收盘价代表当日全部成交的价位），
    比真算筹码分布便宜两个数量级，且单调性一致 —— 用于横截面排序足够。
    """
    _, closes, _, _, _, volumes = _bar_prices(inputs.series)
    # 两个序列同源于一个 bars 列表，长度必然相等（strict 守住这个不变量）
    pairs = [(close, volume)
             for close, volume in zip(closes, volumes, strict=True)
             if close > 0 and volume > 0]
    pairs = pairs[-window:] if window > 0 else pairs
    profit_ratio: float | None = None
    if len(pairs) >= 20:
        current = pairs[-1][0]
        total_volume = sum(volume for _, volume in pairs)
        below = sum(volume for close, volume in pairs if close < current)
        if total_volume > 0:
            profit_ratio = below / total_volume
    holder = [to_float(stats.get("holder_change"))
              for stats in inputs.member_stats.values()]
    return {"profit_ratio": profit_ratio,
            "holder_change": median([item for item in holder
                                     if item is not None])}


def technical_raw(series: BoardSeries, *, window: int) -> dict[str, float | None]:
    """技术指标：均线排列分 / RSI / 量能比。

    - 均线排列 `ma`：收盘 > MA5、MA5 > MA20、MA20 > MA60 三条各占一档，
      返回 0 / 33.3 / 66.7 / 100；
    - `rsi`：14 日 Wilder RSI（0-100），**不做分位映射**，见 `_rsi_score`；
    - `volume`：近 5 日均量 / 近 `window` 日均量（放量 = 关注度上升）。
    """
    closes = [bar.close for bar in series.bars]
    volumes = [bar.volume for bar in series.bars]
    out: dict[str, float | None] = {"ma": None, "rsi": None, "volume": None}
    if len(closes) >= 60:
        ma5 = mean(closes[-5:])
        ma20 = mean(closes[-20:])
        ma60 = mean(closes[-60:])
        if None not in (ma5, ma20, ma60):
            hits = sum((closes[-1] > ma5, ma5 > ma20, ma20 > ma60))
            out["ma"] = hits / 3.0 * 100.0
    if len(closes) >= 15:
        out["rsi"] = _rsi(closes, period=14)
    if len(volumes) >= max(window, 5):
        recent = mean(volumes[-5:])
        baseline = mean(volumes[-window:])
        if recent is not None and baseline and baseline > 0:
            out["volume"] = recent / baseline
    return out


def _rsi(closes: Sequence[float], *, period: int = 14) -> float | None:
    """Wilder RSI（0-100）；数据不足返回 None。"""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(closes)):
        delta = closes[index] - closes[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])
    if avg_gain is None or avg_loss is None:
        return None
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _rsi_score(rsi: float | None) -> float | None:
    """RSI → 0-100 分的**单峰映射**：50-70 最健康，>85 超买扣分，<30 弱势。

    为什么不用分位映射：RSI 的横截面分布高度集中在 40-60，分位映射会把
    "RSI 61 vs 58"这种无意义差异放大成 30 分的分差，而真正的极端
    （RSI 90 超买）反而因为右侧样本多而拿不到低分。
    """
    value = to_float(rsi)
    if value is None:
        return None
    if value <= 30:
        return 20.0 + value * 0.5          # 15~35：弱势区，越低越差
    if value <= 70:
        return 50.0 + (value - 30) * 1.25  # 50~100：健康上行区
    if value <= 85:
        return 100.0 - (value - 70) * 2.0  # 70~100：过热回落
    return max(0.0, 30.0 - (value - 85))


# ==================================================================
# 打分主流程
# ==================================================================


def score_six_dim(inputs: Sequence[BoardInput], *, config: MainlineConfig,
                  state: MacroState | None = None,
                  sensitivity: MacroSensitivity | None = None,
                  history_raws: dict[str, dict[str, float | None]] | None = None
                  ) -> dict[str, tuple[LayerScore, dict[str, float | None]]]:
    """对全市场板块算第一层六维得分。

    返回 `{board_code: (LayerScore, 因子原始值字典)}`。

    第二个返回值（因子原始值）是给需求 4.4 的**因子相关性验证**用的：
    相关性必须算在**原始因子**上而不是 0-100 的分位分上 —— 分位分是单调
    变换，但不同因子的分位映射会人为改变线性相关结构，用它验证去重效果
    会得出"相关性都很低"的假结论。

    `history_raws`：`{板块: 景气度原始值}`，取自 `W` 个交易日**之前**那一日
    （由 `service` 从历史评分行读出来）。给了它才会启用**增速变化率**口径：
    景气度低于 `cfg.prosperity_delta_below` 的板块，改用
    `Δg = g(t) − g(t−W)` 的横截面分位来算景气度（见 `PROSPERITY_FLOOR`
    上面的长注释与 `docs/MAINLINE_COAL_SPECIAL_CASE.md`）。

    ⚠️ 为什么不是所有板块都换：实测（同一份文档）全局替换会让留出窗口的
    误报率从 6.30 涨到 **8.44**，而只换低景气组是 **6.20**。候选池每天固定
    64 个名额，作用范围越大、被挤掉的真信号越多。
    """
    cfg = config.six_dim
    weights = cfg.normalized_weights()
    windows = dict(cfg.windows)
    reverse_dims = set(getattr(cfg, "reverse_dims", ()) or ())
    if reverse_dims:
        logger.info("六维基座：以下维度按**反向**参与层合成（取 100-score）：%s",
                    "、".join(sorted(reverse_dims)))
    state = state or MacroState()
    sensitivity = sensitivity or MacroSensitivity()
    common_score = macro_common_score(state.common)

    # ---------- 1) 逐板块算原始因子 ----------
    raws: dict[str, dict[str, float | None]] = {}
    macro_notes: dict[str, list[str]] = {}
    for item in inputs:
        row: dict[str, float | None] = {}
        row.update(trading_raw(item.series,
                               momentum=int(windows.get("momentum", 20)),
                               overnight=int(windows.get("overnight", 20))))
        row.update(prosperity_raw(item))
        row.update(moneyflow_raw(item, window=int(windows.get("moneyflow", 5))))
        row.update(chips_raw(item, window=int(windows.get("chips", 250))))
        row.update(technical_raw(item.series,
                                 window=int(windows.get("technical", 60))))
        vector = sensitivity.for_board(code=item.code, name=item.name)
        tilt, used = sensitivity.tilt(vector, state)
        row["macro_tilt"] = tilt
        macro_notes[item.code] = used
        raws[item.code] = row

    # ---------- 2) 横截面分位 → 0-100 ----------
    # 每个因子一次性取出全市场取值，避免在循环里反复扫（N² → N）
    columns: dict[str, list[float | None]] = {}
    for key in POSITIVE:
        columns[key] = [raws[item.code].get(key) for item in inputs]

    percentiles: dict[str, dict[str, float | None]] = {}
    for item in inputs:
        row: dict[str, float | None] = {}
        for key, positive in POSITIVE.items():
            row[key] = percentile_score(columns[key], raws[item.code].get(key),
                                        reverse=not positive)
        row["rsi"] = _rsi_score(raws[item.code].get("rsi"))
        percentiles[item.code] = row

    # ---------- 2b) 景气度的「增速变化率」列（仅低景气板块启用） ----------
    #
    # `SUB_WEIGHTS["prosperity"]` 的子因子名与原始值名不同名，必须先映射：
    # 子因子叫 `profit_yoy` / `revenue_yoy`，原始值是 `netprofit_yoy` / `or_yoy`。
    delta_percentiles: dict[str, dict[str, float | None]] = {}
    if history_raws:
        for sub_key, raw_key in PROSPERITY_RAW_KEYS.items():
            column: list[float | None] = []
            for item in inputs:
                now = raws[item.code].get(raw_key)
                old = (history_raws.get(item.code) or {}).get(raw_key)
                column.append(None if now is None or old is None
                              else float(now) - float(old))
            delta_percentiles[sub_key] = {
                item.code: percentile_score(column, column[index],
                                            reverse=False)
                for index, item in enumerate(inputs)}
        # ⚠️ 键对不上会**静默失效**：`history_raws` 必须按**板块代码**索引
        # （`service` 就是这么传的）。若传成了股票代码，每一列都会是 None，
        # 于是"一个板块都没换"却没有任何报错 —— 与突破触发那次是同一类坑。
        # 所以这里显式探测一次并告警，而不是让它悄悄过去。
        if not any(value is not None
                   for row in delta_percentiles.values()
                   for value in row.values()):
            logger.warning(
                "景气度变化率：%d 个板块的历史原始值一个都没用上 —— "
                "`history_raws` 的键应为板块代码（如 885914.TI），"
                "疑似键不匹配", len(inputs))

    # ---------- 3) 合成 ----------
    out: dict[str, tuple[LayerScore, dict[str, float | None]]] = {}
    for item in inputs:
        code = item.code
        dims: list[DimensionScore] = []
        for dim_key in cfg.DIMS:
            subs = SUB_WEIGHTS.get(dim_key, {})
            dim_weight = float(weights.get(dim_key, 0.0))
            parts: list[tuple[float, float, bool]] = []
            note_bits: list[str] = []
            detail: dict[str, Any] = {}
            for sub_key, sub_weight in subs.items():
                if dim_key == "macro":
                    score = _macro_score(item, raws[code], state, sensitivity,
                                         common_score, config, macro_notes[code])
                    available = score is not None
                    parts.append((score or 0.0, sub_weight, available))
                    detail = {"tilt": raws[code].get("macro_tilt"),
                              "common": state.common,
                              "z": dict(state.z)}
                    if not available:
                        note_bits.append("宏观状态不可用")
                    continue
                value = percentiles[code].get(sub_key)
                available = value is not None
                parts.append((value or 0.0, sub_weight, available))
                detail[sub_key] = raws[code].get(sub_key)
            score, coverage = weighted_score(parts)
            if dim_key == "prosperity" and coverage > 0:
                # V3：景气度**低**的板块改用「增速变化率」（二阶导）口径。
                # 判定用**压制前**的水平值分，否则压制会把门槛本身也改掉。
                if (delta_percentiles
                        and score < float(getattr(cfg, "prosperity_delta_below",
                                                  0.0) or 0.0)):
                    delta_parts: list[tuple[float, float, bool]] = []
                    for sub_key, sub_weight in subs.items():
                        value = delta_percentiles.get(sub_key, {}).get(code)
                        delta_parts.append((value or 0.0, sub_weight,
                                            value is not None))
                    delta_score, delta_cov = weighted_score(delta_parts)
                    if delta_cov > 0:
                        note_bits.append(
                            f"景气度改用增速变化率（水平值 {score:.1f} "
                            f"< {cfg.prosperity_delta_below:g}）→ {delta_score:.1f}")
                        score = delta_score
                # 低分压制（见 `PROSPERITY_FLOOR` 的说明）。只在**真的压到了**
                # 的时候写 note，否则每一行都会挂一句废话。
                compressed = compress_low(score)
                if abs(compressed - score) > 0.05:
                    note_bits.append(
                        f"景气度低分压制 floor={PROSPERITY_FLOOR:g}"
                        f"（{score:.1f} → {compressed:.1f}）")
                    score = compressed
            dims.append(DimensionScore(
                key=dim_key, label=DIM_LABELS.get(dim_key, dim_key),
                score=score, weight=dim_weight,
                raw={k: (round(v, 6) if isinstance(v, float) else v)
                     for k, v in detail.items()},
                note="；".join(note_bits),
                available=any(ok for _, _, ok in parts) and coverage > 0,
                reversed=dim_key in reverse_dims))

        # ⚠️ 层的合成**不是**简单地拿 `dim.score` 加权 —— 反向维度要取 `100 - score`。
        #
        # 依据：实测（`scripts/factor_ic_report.py`）`trading` 与 `technical`
        # 在 5/10/20/60 四个持有期上的 IC **单调为负**（H=20：−0.077 / −0.082），
        # 即"量能与技术形态越强、后续越差"（A 股概念板块在 20~60 日尺度上
        # 均值回归）。而 `weighted_score` 会把 `weight <= 0` 的项直接丢掉，
        # 所以"给负权重"在这套实现里表达不了 —— 必须在这里显式反转。
        #
        # 为什么不直接砍掉：离线实测
        #     砍动量（权重给别的维度）  H=20 IC +0.0077
        #     反转动量（取 100-score）  H=20 IC +0.0694
        # 反转保留了这个信号的信息量，砍掉等于浪费。
        #
        # `DimensionScore.score` 仍然存**自然分**（"量能 90"就是量能强），
        # 只有 `reversed` 标记 + 层的合成走反转 —— 这样面板上不会出现
        # "量能强但得分 10"这种自相矛盾的显示。
        layer_parts: list[tuple[float, float, bool]] = []
        for dim in dims:
            value = dim.score
            if dim.reversed and dim.available:
                value = 100.0 - value
            layer_parts.append((value, dim.weight, dim.available))
        layer_score, layer_coverage = weighted_score(layer_parts)
        notes = _layer_notes(inputs=item, state=state, sensitivity=sensitivity,
                             dims=dims)
        # `weight` 必须显式写入：面板下钻与回测报告都读 `layer.weight`，
        # 漏掉它（默认 0.0）会让"六维层在合成分里的权重"在界面上显示成 0，
        # 而分数本身完全正常 —— 一种只影响可解释性、不影响计算的静默错误。
        layer = LayerScore(key="six_dim", label="开源六维基座",
                           score=layer_score, dimensions=dims,
                           weight=float(weights.get("six_dim", 0.0)),
                           coverage=layer_coverage, notes=notes)
        out[code] = (layer, raws[code])
    return out


def _macro_score(item: BoardInput, raw: dict[str, float | None],
                 state: MacroState, sensitivity: MacroSensitivity,
                 common_score: float, config: MainlineConfig,
                 used: list[str]) -> float | None:
    """单板块的宏观维度分（行业倾斜 + 共同项加权）。"""
    if not state.available:
        return None
    tilt = raw.get("macro_tilt")
    specific = tilt_score(tilt) if tilt is not None else None
    return blend_macro(specific, common_score,
                       common_weight=config.macro.common_weight)


def _layer_notes(*, inputs: BoardInput, state: MacroState,
                 sensitivity: MacroSensitivity, dims: list[DimensionScore]
                 ) -> list[str]:
    """把这一层的数据缺口写清楚（面板与回测报告都读它）。"""
    notes: list[str] = []
    if not inputs.series.bars:
        notes.append("板块指数缺失（该板块无法参与技术/交易维度）")
    if not inputs.member_stats:
        notes.append("成分股指标缺失（景气度 / 股东户数维度不参与加权）")
    elif all(to_float(stats.get("holder_change")) is None
             for stats in inputs.member_stats.values()):
        notes.append("股东户数缺失（季报频率且需滚动累积，筹码维度只用获利盘）")
    if inputs.flow is None or not inputs.flow.points:
        notes.append("板块资金流缺失（资金流维度不参与加权）")
    if not sensitivity.loaded:
        notes.append(sensitivity.gap or "宏观敏感度表未加载")
    elif not sensitivity.for_board(code=inputs.code, name=inputs.name):
        notes.append("该板块不在申万一级行业内，宏观分只含市场共同项")
    if state.gaps:
        notes.extend(state.gaps[:2])
    for dim in dims:
        if not dim.available:
            notes.append(f"{dim.label}维度无有效数据")
    return notes


def candidate_cutoff(scores: dict[str, Any], *, config: MainlineConfig) -> list[str]:
    """按第一层总分取候选池（前 `funnel.candidate_ratio`），返回板块代码列表。"""
    ordered = sorted(scores.items(),
                     key=lambda item: (-float(item[1][0].score), item[0]))
    size = config.funnel.candidates(len(ordered))
    return [code for code, _ in ordered[:size]]


def dim_series(scores: dict[str, tuple[LayerScore, dict[str, float | None]]],
               key: str) -> list[float | None]:
    """取某个子因子在全市场的横截面取值（相关性验证用）。"""
    return [row.get(key) for _, row in scores.values()]


__all__ = [
    "DIM_LABELS",
    "POSITIVE",
    "PROSPERITY_FLOOR",
    "SUB_WEIGHTS",
    "BoardInput",
    "candidate_cutoff",
    "chips_raw",
    "compress_low",
    "dim_series",
    "moneyflow_raw",
    "prosperity_raw",
    "score_six_dim",
    "technical_raw",
    "trading_raw",
]
