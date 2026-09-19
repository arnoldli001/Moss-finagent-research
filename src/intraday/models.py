"""做T辅助模块数据契约（Pydantic）。

设计口径与项目 DATA_CONTRACT 一致：
- 每个对外数值都能追溯到具体数据源与原始输入（FactorScore.inputs / DataHealth）；
- 数据源全部失败即报「缺口」（gaps），绝不用模拟数据填充；
- 所有结论随附免责声明。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ==================== 行情 ====================

class Bar(BaseModel):
    """单根K线（5分钟基准周期）。"""

    ts: str = Field(description="K线结束时间，如 2026-09-15 10:35")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    vwap: float | None = Field(
        default=None, description="该bar收盘时的当日累计VWAP（成交量加权均价）")


class TrendPoint(BaseModel):
    """分时点（1分钟粒度，用于分时图连线）。"""

    ts: str
    price: float
    avg_price: float | None = Field(default=None, description="当日累计均价")
    volume: float = 0.0


class Quote(BaseModel):
    """个股实时快照。"""

    code: str
    name: str = ""
    price: float
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    change: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    limit_up: float | None = None
    ts: str = Field(default_factory=_now_iso)


# ==================== 关键价位 ====================

class LevelSet(BaseModel):
    """动态支撑/压力位（做T档位）。"""

    price: float
    box_high: float
    box_low: float
    box_position: float | None = Field(default=None, description="现价在箱体中的位置 0~1")
    box_span_days: int = 0
    low_buy: float = Field(description="低吸线（箱体下沿，可融合布林下轨）")
    high_sell: float = Field(description="高抛线（箱体上沿，可融合布林上轨）")
    stop_loss: float = Field(description="止损位；跌破禁止一切低吸信号")
    stop_loss_pct: float
    stop_basis: str = Field(
        default="",
        description="止损位是怎么定出来的（供面板解释，避免用户以为它一定可靠）")
    boll_upper: float | None = None
    boll_mid: float | None = None
    boll_lower: float | None = None
    pct_b: float | None = Field(default=None, description="布林%B位置")
    bandwidth: float | None = None
    vwap: float | None = None
    atr: float | None = Field(default=None, description="日线ATR（波动幅度参考）")

    # ---- 组装口径（由 `impact.annotate_level_basis` 只补到**当前**档位对象上）----
    # 为什么要有这几项：面板只显示「低吸 862.10」时，用户无法判断这条线是谁定的
    # —— 是箱体下沿、布林下轨，还是"箱体/布林都跑到现价上方"后的 ATR 兜底？
    # 三者的调整方式完全不同（前两者改不了，后者要动 dip_fallback_atr），
    # 说不清来源就只能靠猜。
    #
    # 刻意**不在 `compute_levels` 里填**：该函数还被 `replay_levels` 逐bar调用
    # （一次 240 根），给它挂这一组解释字段会让快照载荷无谓膨胀。
    low_source: str = Field(default="", description="低吸线由谁决定（箱体下沿/布林下轨/ATR兜底）")
    high_source: str = Field(default="", description="高抛线由谁决定（箱体上沿/布林上轨/高抛缓冲）")
    band_width_pct: float | None = Field(default=None, description="实际档位差占现价%")
    band_clamped: str = Field(default="", description="档位差是否被 min/max 护栏夹过")
    # 护栏前的原始位置：低吸线/高抛线在被 min/max_band_pct 夹过之前是多少。
    # 没有它就会出现「先说这条线是布林下轨 902.35、线上却写着 898.63」的错位 ——
    # 用户拿这两个数一比就会怀疑面板在乱算。
    pre_clamp_low: float | None = Field(default=None, description="档位差护栏前的低吸线")
    pre_clamp_high: float | None = Field(default=None, description="档位差护栏前的高抛线")
    low_trigger_price: float | None = Field(
        default=None, description="低吸触发的最高价 = 低吸线×(1+贴线带宽)")
    high_trigger_price: float | None = Field(
        default=None, description="高抛触发的最低价 = 高抛线×(1-贴线带宽)")
    take_profit_buffer_pct: float | None = None
    dip_fallback_atr: float | None = None
    atr_stop_mult: float | None = None
    level_fit_note: str = Field(
        default="",
        description="档位是否来自神经网络拟合（含留一日成功率与调整项乘数）")


# ==================== 多因子打分 ====================

class TriggerLevelRow(BaseModel):
    """「这条线现在是多少 + 离现价多远 + 谁定的」一行（权重编辑面板直接渲染）。"""

    key: str
    label: str
    price: float
    distance_pct: float = Field(description="(线 - 现价)/现价×100，负=在现价下方")
    source: str = ""
    trigger_price: float | None = None
    trigger_note: str = ""
    note: str = ""


class TriggerGateRow(BaseModel):
    """「差多少分/差多少价才出信号」一行。"""

    key: str
    label: str
    ready: bool
    score_need: float = Field(default=0.0, description="总分还差多少（正=还差这么多）")
    price_need_pct: float | None = Field(
        default=None, description="价格还要走多少%（负=还要往下跌）")
    blocked_by: str = ""
    note: str = ""


class TriggerLevelDelta(BaseModel):
    """「改动前 → 改动后」一条价格线的变动（权重编辑面板的对照表直接用）。

    「改动前」取的是**当前表单口径被保存在的档案/全局参数**下这条线的位置。
    需要说明的是：低吸/高抛/止损是**时刻量**（随 VWAP/布林/现价每分钟重算），
    两份档位都基于同一份行情算出来，因此这里比的是「参数与阈值改动」的效果，
    不是"上一分钟那条线在哪"。
    """

    key: str
    label: str
    current: float | None = None
    preview: float
    delta: float | None = Field(default=None, description="preview - current（无基准时为空）")
    delta_pct: float | None = None


class FactorImpactRow(BaseModel):
    """单因子对总分的**影响度**（权重口径，不含档位）。

    两个口径刻意都给出，因为它们回答的是不同问题：
      - `unit_impact = 得分`：权重每加 1 分，总分多多少（负因子加权是**拉低**总分）；
      - `zero_impact`：把这一项权重**置 0** 后总分变多少 —— 「关掉它」的净影响。
    """

    key: str
    label: str
    weight: float
    score: float
    contribution: float
    unit_impact: float
    zero_impact: float
    weight_share_pct: float = Field(description="该权重占**有效权重**的比例%")
    available: bool = True
    gap: str | None = None
    note: str = ""


class TriggerImpact(BaseModel):
    """把「参数 → 价格线 / 总分 → 是否出信号」摊开给用户看的一次性推导。

    为什么需要它：档位（低吸/高抛/止损）与权重走的是**两条互不相干**的链路 ——
    权重决定总分（够不够格动手），档位决定价格线（价格到没到）。用户在权重编辑
    面板里拖滑杆时，最想知道的恰恰是这两件事的合成结果：**现在差多少才动手**。
    """

    available: bool
    reason: str = ""
    price: float = 0.0
    low_trigger_price: float | None = None
    high_trigger_price: float | None = None
    stop_price: float | None = None
    total: float = 0.0
    threshold_action: float = 30.0
    threshold_hint: float = 20.0
    available_weight: float = 100.0
    coverage_blocked: bool = False
    cycle_blocked: bool = False
    cycle_stage: str = ""
    level_rows: list[TriggerLevelRow] = Field(default_factory=list)
    level_deltas: list[TriggerLevelDelta] = Field(
        default_factory=list,
        description="「改动前 → 改动后」对照（传了 current_levels 才有）")
    gates: list[TriggerGateRow] = Field(default_factory=list)
    factor_impact: list[FactorImpactRow] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class FactorScore(BaseModel):
    """单因子打分明细（分值∈[-1,1]，贡献=分值×权重）。"""

    key: str
    label: str
    weight: float
    score: float
    contribution: float
    detail: str = Field(description="人类可读的解释，直接用于前端表格")
    inputs: dict[str, Any] = Field(
        default_factory=dict, description="关键中间量（可溯源）")
    available: bool = True
    gap: str | None = Field(default=None, description="该因子数据缺口的说明")


ScoreZone = Literal[
    "strong_buy_zone", "buy_zone", "neutral", "sell_zone", "strong_sell_zone"]


class ScoreCard(BaseModel):
    """多指标合成打分卡（核心引擎输出）。"""

    total: float = Field(description="总分 = Σ(因子得分×权重) ∈ [-100,100]")
    threshold_action: float
    threshold_hint: float
    zone: ScoreZone
    verdict: str = Field(description="一句话结论（震荡区间/偏多低吸/偏空高抛…）")
    factors: list[FactorScore]
    weights_sum: float
    available_weight: float = Field(
        default=100.0, description="有效权重合计（缺失因子的权重已按比例重分配）")
    gaps: list[str] = Field(default_factory=list)


# ==================== 信号 ====================

SignalKind = Literal["low_buy", "high_sell", "stop_loss", "none"]
SignalStrength = Literal["solid", "hollow", "forced_exit", "none"]


class TradeSignal(BaseModel):
    """做T信号（三角标记点）。"""

    kind: SignalKind
    strength: SignalStrength
    triggered: bool
    price: float
    ts: str
    total_score: float
    reason: str
    blocked_by_stop_loss: bool = Field(
        default=False, description="是否被止损硬约束拦截（阻止低吸）")
    target_level: float | None = None
    pushed: bool = False


class SignalMarker(BaseModel):
    """前端三角标记（分时图打点）。"""

    ts: str
    price: float
    kind: SignalKind
    strength: SignalStrength
    label: str


# ==================== 估值空间 ====================

class ValuationPeer(BaseModel):
    """同业个股估值行。"""

    code: str
    name: str = ""
    pe_ttm: float | None = None
    pb: float | None = None
    price: float | None = None
    change_pct: float | None = None


class ValuationSpace(BaseModel):
    """估值空间模块：判断「上涨空间」还是「估值透支」。"""

    available: bool
    code: str
    name: str = ""
    pe_ttm: float | None = None
    pb: float | None = None
    # 个股自身历史分位（近3年日频序列）
    pe_percentile: float | None = Field(default=None, description="PE三年分位 0~100")
    pb_percentile: float | None = None
    pe_series_days: int = 0
    pb_series_days: int = 0
    pe_min: float | None = None
    pe_max: float | None = None
    pe_median: float | None = None
    pb_min: float | None = None
    pb_max: float | None = None
    pb_median: float | None = None
    # 同业对比
    peer_source: Literal["configured", "cninfo_industry", "unavailable"] = "unavailable"
    peer_label: str = ""
    peers: list[ValuationPeer] = Field(default_factory=list)
    peer_pe_median: float | None = None
    peer_pb_median: float | None = None
    peer_count: int = 0
    pe_vs_peer_pct: float | None = Field(
        default=None, description="个股PE相对同业中位数的溢价率（%），负=折价")
    pb_vs_peer_pct: float | None = None
    industry_pe_median: float | None = Field(
        default=None, description="巨潮行业中位数PE（权威对标）")
    industry_name: str = ""
    industry_company_count: int | None = None
    # 结论
    verdict: str = ""
    headroom: Literal["ample", "moderate", "stretched", "expensive", "unknown"] = "unknown"
    score: float | None = Field(
        default=None, description="估值空间打分 ∈[-1,1]，正=有空间，负=透支")
    gap: str | None = None
    source_name: str = ""


# ==================== 市场情绪 ====================

class BoardSnapshot(BaseModel):
    """关联板块实时快照（同花顺概念板块）。"""

    name: str
    kind: str = "concept"
    available: bool = False
    change_pct: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    breadth: float | None = Field(
        default=None, description="上涨家数占比 0~1")
    amount: float | None = Field(default=None, description="成交额（亿元）")
    net_inflow: float | None = Field(default=None, description="资金净流入（亿元）")
    rank: str | None = None
    open_price: float | None = None
    prev_close: float | None = None
    high: float | None = None
    low: float | None = None
    source_name: str = ""
    gap: str | None = None


class BoardSeries(BaseModel):
    """板块分时序列（用于分时图叠加）。"""

    name: str
    kind: str = "concept"
    available: bool = False
    points: list[TrendPoint] = Field(default_factory=list)
    source_name: str = ""
    gap: str | None = None


class SentimentPanel(BaseModel):
    """消息面与市场情绪模块。"""

    # 板块内涨跌家数
    board_name: str = ""
    board_change_pct: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    breadth: float | None = None
    breadth_score: float | None = None
    boards: list[BoardSnapshot] = Field(default_factory=list)
    boards_bound: bool = Field(
        default=False,
        description="关联板块是否已绑定：false 时 boards 仅作参考展示，"
                    "板块情绪/板块排行维度不计入总分")
    board_series: list[BoardSeries] = Field(default_factory=list)
    # 个股相对板块强度
    stock_change_pct: float | None = None
    relative_strength_pct: float | None = None
    rs_score: float | None = None
    # 大盘状态
    index_name: str = ""
    index_code: str = ""
    index_price: float | None = None
    index_change_pct: float | None = None
    index_state: str = ""
    # 合成
    score: float | None = None
    verdict: str = ""
    gaps: list[str] = Field(default_factory=list)
    source_name: str = ""


class NewsItem(BaseModel):
    title: str
    source_name: str = ""
    source_url: str = ""
    publish_time: str = ""
    polarity: Literal["positive", "negative", "neutral"] = "neutral"


class NewsSentiment(BaseModel):
    """消息面NLP情绪打分（DeepSeek）。"""

    available: bool = False
    score: float = Field(default=0.0, description="情绪分 ∈[-1,1]")
    positive_count: int = 0
    negative_count: int = 0
    neutral_count: int = 0
    count_score: float = 0.0
    llm_score: float | None = None
    llm_used: bool = False
    model: str = ""
    summary: str = ""
    items: list[NewsItem] = Field(default_factory=list)
    news_count: int = 0
    gap: str | None = None
    source_name: str = ""


# ==================== 数据健康度 ====================

class SourceAttempt(BaseModel):
    """单次数据源尝试记录（failover链透明化）。"""

    source: str
    ok: bool
    detail: str = ""
    rows: int = 0
    latency_ms: int | None = None


class DataHealth(BaseModel):
    """数据健康度：命中源 + 缺口清单（绝不静默使用模拟数据）。"""

    chosen_intraday_source: str | None = None
    chosen_daily_source: str | None = None
    chosen_quote_source: str | None = None
    attempts: list[SourceAttempt] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    stale: bool = Field(default=False, description="行情是否非当日（盘前/休市取上一交易日）")
    trade_date: str = ""


# ==================== 顶层快照 ====================

class IntradaySnapshot(BaseModel):
    """做T辅助完整快照（前端四个面板的唯一数据来源）。"""

    code: str
    name: str = ""
    trade_date: str = ""
    generated_at: str = Field(default_factory=_now_iso)
    session_state: Literal[
        "pre_open", "call_auction", "trading", "lunch_break", "closed"] = "closed"
    session_label: str = ""
    quote: Quote | None = None
    trend: list[TrendPoint] = Field(default_factory=list)
    bars: list[Bar] = Field(default_factory=list)
    levels: LevelSet | None = None
    scorecard: ScoreCard | None = None
    signal: TradeSignal | None = None
    markers: list[SignalMarker] = Field(default_factory=list)
    # 逐bar总分：用于回答"价格摸到了档位为什么没信号"（触发需要价格与总分同时达标）
    score_series: list[ScorePoint] = Field(default_factory=list)
    # 逐bar档位：用于把档位画成随时间变化的曲线、并让回放按"当时那一刻"判定
    level_series: list[LevelPoint] = Field(default_factory=list)
    valuation: ValuationSpace | None = None
    sentiment: SentimentPanel | None = None
    news: NewsSentiment | None = None
    # 市场环境（新增三因子的原始数据，供前端「市场环境」条展示）
    index_volume: dict[str, Any] | None = Field(
        default=None, description="所属指数量能（含全天预测量与量能比）")
    overseas: dict[str, Any] | None = Field(
        default=None, description="海外映射（美股隔夜 + 韩股盘中同步）")
    level_fit: dict[str, Any] | None = Field(
        default=None,
        description=("关键价位的神经网络拟合结果摘要（含 in-sample / 留一日两个成功率、"
                     "是否过闸门、拟合线与调整项乘数）——档位是规则口径还是拟合口径，"
                     "面板必须说得出来"))
    sell_points: dict[str, Any] | None = Field(
        default=None,
        description=("**分时卖点**判定结果（量价关系）：冲高回落无承接 / 尾盘放天量见顶 / "
                     "零轴长影见顶。与日线 S 系列是**不同层次** —— S 系列判"
                     "「这一段该不该持有」，本字段判「此刻盘中该不该卖」。"
                     "含每条信号的逐条判据与缺口，前端可展开核对"))
    health: DataHealth = Field(default_factory=DataHealth)
    config_snapshot: dict[str, Any] = Field(
        default_factory=dict, description="本次打分所用权重/阈值（前端展示口径）")
    notifier: dict[str, Any] = Field(
        default_factory=dict, description="推送通道配置状态（不泄露Webhook密钥）")
    disclaimer: str = ""


class WatchItem(BaseModel):
    """自选标的列表项。"""

    code: str
    name: str = ""
    boards: list[str] = Field(default_factory=list)
    total_score: float | None = None
    signal_strength: SignalStrength = "none"
    signal_kind: SignalKind = "none"
    price: float | None = None
    change_pct: float | None = None
    # 价格的取数时刻（ISO）：价格走「报价快车道」（每几秒），而总分/信号走每分钟
    # 重算 —— 前端据此能把两者的新鲜度分开显示，不会让人把 60 秒前的信号当此刻的。
    quote_ts: str = ""
    #: 是否置顶（置顶项永远排在最前；状态存在配置里，跨浏览器一致）
    pinned: bool = False


class NotifyResult(BaseModel):
    """推送结果。"""

    channel: str
    status: Literal["sent", "suppressed", "unconfigured", "failed"]
    detail: str = ""


class BacktestTrade(BaseModel):
    """做T回测单笔交易。"""

    entry_ts: str
    exit_ts: str
    direction: Literal["long_t", "short_t"]
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str
    total_score_at_entry: float
    strength: SignalStrength


class ThresholdStat(BaseModel):
    """单阈值档位的信号后验统计。"""

    threshold: float
    direction: Literal["long", "short", "all"] = Field(
        default="long",
        description="long=低吸侧(总分≥阈值) / short=高抛侧(总分≤-阈值) / all=全样基准")
    signals: int
    hit_rate: float | None = None
    avg_forward_return_pct: float | None = None
    median_forward_return_pct: float | None = None
    excess_vs_baseline_pct: float | None = None


class BacktestResult(BaseModel):
    """阈值回测结果（验证 ±20/±30 是否真能带来正收益）。"""

    available: bool
    code: str
    name: str = ""
    days: int = 0
    bars: int = 0
    range_start: str = ""
    range_end: str = ""
    horizon_bars: int = 0
    trades: list[BacktestTrade] = Field(default_factory=list)
    action_line: ThresholdStat | None = None
    hint_line: ThresholdStat | None = None
    baseline: ThresholdStat | None = None
    by_threshold: list[ThresholdStat] = Field(default_factory=list)
    total_return_pct: float | None = None
    win_rate: float | None = None
    max_drawdown_pct: float | None = None
    gaps: list[str] = Field(default_factory=list)
    verdict: str = ""
    disclaimer: str = ""


# ==================================================================
# 日K级别做T（量价体系）
# ==================================================================

class DailyBar(BaseModel):
    """日K线（含画图与规则计算所需的全部字段）。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    pct_chg: float | None = None
    amplitude: float | None = Field(default=None, description="振幅% = (高-低)/昨收")
    turnover: float | None = None
    # ---- 量柱标记（口径见 docs/INTRADAY_T_DESIGN.md 第11节）----
    is_high_volume: bool = False
    is_double_volume: bool = False
    is_shrink_volume: bool = False
    is_shrink_half: bool = False
    is_ladder_down: bool = False
    is_flat_volume: bool = False
    is_ground_volume: bool = False
    is_explode_volume: bool = False
    is_long_lower_shadow: bool = False
    is_big_yang: bool = False
    is_big_yin: bool = False
    body_top: float = 0.0
    body_bottom: float = 0.0
    # 均线（逐bar值，供前端画均线；当日值同时出现在 DailySnapshot.ma）
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None


class VolumeAnchor(BaseModel):
    """高量柱锚点（安全线=实顶，风险线=实底）。"""

    date: str
    index: int
    volume: float
    body_top: float = Field(description="实顶=安全线")
    body_bottom: float = Field(description="实底=风险线")
    kind: Literal["标杆", "梯量", "缩量", "倍量"] = "标杆"
    position: Literal["底部", "中继", "接力", "顶部"] = "中继"
    days_since: int = 0
    status: Literal["有效支撑", "待观察", "已破位"] = "待观察"
    note: str = ""


class VpPattern(BaseModel):
    """量价关系形态判定结果（16形态矩阵 + 平量变体）。"""

    code: str = Field(description="如 single_yang_vol / multi_up_shrink")
    name: str
    category: Literal["单K", "组合"] = "单K"
    price_dir: Literal["涨", "跌", "平"] = "平"
    price_speed: Literal["加速", "减速", "平"] = "平"
    volume_dir: Literal["放量", "缩量", "平量"] = "平量"
    meaning: str = ""
    signal: str = ""
    direction: Literal["bullish", "bearish", "reversal", "neutral"] = "neutral"
    detail: str = ""


class PositionInfo(BaseModel):
    """位置量化（同一形态在低位是吸筹、在高位是出货）。"""

    label: Literal["低位", "中位", "高位"] = "中位"
    percentile: float = Field(description="现价在近N日高低区间中的分位 0~1")
    window: int = 120
    high: float = 0.0
    low: float = 0.0
    from_high_pct: float = Field(description="距区间高点幅度%（负=低于高点）")
    from_low_pct: float = Field(description="距区间低点幅度%")
    note: str = ""


class CostLine(BaseModel):
    """主力操盘成本线（基准线五形态）。"""

    kind: str = Field(
        description="大阳开盘价 / 大阳实体1-2 / 并列大阳首根开盘 / "
                    "跳空缺口前收 / 长下影成本区")
    date: str
    price: float
    distance_pct: float = Field(description="现价相对该成本线幅度%")
    broken: bool = False
    note: str = ""


class SignalCondition(BaseModel):
    """单条规则条件（前端逐条展示「为什么触发/差在哪」）。"""

    label: str
    met: bool
    actual: str = ""
    expected: str = ""
    note: str = ""
    required: bool = Field(
        default=True,
        description="是否为该信号的**硬性条件**：全部硬性条件满足才判触发；"
                    "上下文类条件（如位置、待确认项）置 False 只参与打分展示")


class DailySignalItem(BaseModel):
    """日K级别买卖信号（B1–B15 / S1–S6）。"""

    code: str
    name: str
    kind: Literal["buy", "sell", "risk", "watch"] = "watch"
    triggered: bool = False
    score: float = Field(default=0.0, description="条件满足度 0~1")
    conditions: list[SignalCondition] = Field(default_factory=list)
    reason: str = ""
    entry: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    gaps: list[str] = Field(default_factory=list)


class DailySignalMark(BaseModel):
    """日K图上的一个买卖标记（历史bar的因果回放结果）。

    与 `DailySignalItem` 的区别：后者是**当前bar**的条件明细（含未触发的），
    而这个是"某一根历史bar当时触发了哪个买卖点"，只保留触发项，用于在K线图上
    标出最近 N 个交易日的实际操作点。
    """

    date: str
    side: Literal["buy", "sell", "risk"] = Field(
        description="buy=买点；sell=卖点(S1–S3/S6)；risk=风控/止损类")
    code: str = Field(description="信号编号，如 B1 / S2")
    name: str = ""
    price: float | None = Field(default=None, description="该bar收盘价（标记位置参考）")
    entry: float | None = None
    stop_loss: float | None = None


class ScorePoint(BaseModel):
    """逐bar总分（与实时打分同一批打分核重放，无未来函数）。

    为什么要有它：用户最常问的一类问题是"价格明明摸到/跌破了那条线，为什么没有信号"。
    触发需要「价格触及档位」**且**「总分达标」两条同时满足，只给当前总分无法回答
    "触点那一刻是多少分"。有了这条序列，面板可以直接说：
    「今日触及低吸线 56 次，触点处最高总分仅 +13（< 提示线 20）→ 未触发」。
    """

    ts: str
    price: float | None = None
    total: float


class LevelPoint(BaseModel):
    """逐bar档位（低吸/高抛/止损随时间的真实取值）。

    为什么必须逐bar给：档位是**时刻量**（随 VWAP/布林/现价每分钟重算）。图上只画
    一条横线（当前值）会产生两种误读，2026-09-16 用户实测都踩到了：

    - "开盘就在低吸线以下，为什么没信号" —— 其实当刻低吸线低得多，价格一直在线**上方**；
    - "该低吸的位置却触发止损" —— 其实是把后来的高位止损拿去判早盘的低点。
    """

    ts: str
    low_buy: float
    high_sell: float
    stop_loss: float


class ProtectiveLines(BaseModel):
    """S5 保护线体系（固定止损 + 移动止盈）。"""

    stop_line: float | None = None
    stop_basis: str = ""
    trail_line: float | None = None
    trail_basis: str = ""
    ma20: float | None = None
    ma60: float | None = None
    broken_stop: bool = False
    broken_trail: bool = False
    note: str = ""


class NiuLinePoint(BaseModel):
    """擒牛线在某一根日线上的五个档位值（None = 该线尚未暖机完成）。"""

    date: str = ""
    nml: float | None = None
    qrl: float | None = None
    cbx20: float | None = None
    cbx60: float | None = None
    smx: float | None = None


class NiuLineSet(BaseModel):
    """擒牛线档位线体系（日K做T主图）。

    两套同花顺公式按标的类别自动选（见 `src/intraday/niuline.py`）：

    - `variant="stock"`：个股版，CBX = `SUM(AMOUNT,N)/SUM(V,N)`（真实成交额均价）；
    - `variant="index"`：指数/ETF/板块版，CBX = `SUM(C*V,N)/SUM(V,N)`（收盘价加权）。

    NML/QRL/SMX 两套完全相同，**只有 CBX 分叉** —— 混用会让成本线系统性偏移。
    """

    available: bool = True
    #: stock=个股口径 / index=指数·ETF·板块口径
    variant: Literal["stock", "index"] = "stock"
    #: 为什么选了这个变体（可追溯，不猜）
    reason: str = ""
    #: 实际用的均价口径：amount=真实成交额 / close_volume=收盘价加权
    price_basis: Literal["amount", "close_volume"] = "amount"
    #: CBX 换算系数：个股口径下把"每手价"换成"每股"。
    #: 本项目 volume 单位是**手**，故实测为 ~100；不换算 CBX 会比股价高两个
    #: 数量级，"站稳/跌破"判据会整体反过来。指数口径恒为 1.0。
    cbx_scale: float = 1.0
    n: int = 20
    m: int = 14
    #: 最后一根的五个值（前端状态条直接显示）
    latest: dict[str, float | None] = Field(default_factory=dict)
    #: 线的展示元数据（label/note），由后端给出，前端不硬编码线名
    lines: list[dict[str, str]] = Field(default_factory=list)
    points: list[NiuLinePoint] = Field(default_factory=list)
    #: 口径说明与降级原因（如"成交额缺失 → 退回指数口径"）
    notes: list[str] = Field(default_factory=list)


class DailySnapshot(BaseModel):
    """日K级别做T完整快照。"""

    available: bool = False
    code: str = ""
    name: str = ""
    trade_date: str = ""
    generated_at: str = Field(default_factory=_now_iso)
    bars: list[DailyBar] = Field(default_factory=list)
    anchors: list[VolumeAnchor] = Field(default_factory=list)
    active_anchor: VolumeAnchor | None = Field(
        default=None, description="当前生效（最近）的高量柱锚点")
    pattern: VpPattern | None = None
    position: PositionInfo | None = None
    cost_lines: list[CostLine] = Field(default_factory=list)
    buy_signals: list[DailySignalItem] = Field(default_factory=list)
    sell_signals: list[DailySignalItem] = Field(default_factory=list)
    signal_history: list[DailySignalMark] = Field(
        default_factory=list,
        description="最近 N 个交易日的买卖标记（逐bar因果回放，供K线图打点）")
    protective: ProtectiveLines | None = None
    ma: dict[str, float | None] = Field(default_factory=dict)
    discipline: list[str] = Field(
        default_factory=list, description="S6 高量纪律 + 口诀命中的条目")
    # 日线做T的**加权决策总分**（七因子，与分时 ScoreCard 同一套算术）。
    # 与 buy_signals/sell_signals 是**并列**关系而非替代：
    # 规则信号回答「满不满足某套战法形态」，加权总分回答「综合偏向低吸还是高抛」。
    scorecard: ScoreCard | None = None
    verdict: str = ""
    #: 擒牛线档位线（日K做T**主图**；原蜡烛K线已按用户要求下线）
    niuline: NiuLineSet | None = None
    health: DataHealth = Field(default_factory=DataHealth)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    disclaimer: str = ""
