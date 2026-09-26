import {
  ApiError,
  apiErrorFromResponse,
  networkError as makeNetworkError,
} from "./errors";
import { notifyUnauthorized } from "./unauthorized";

export type AgentOutputSummary = {
  agent_id: string;
  agent_name?: string;
  conclusion: string;
  confidence: string;
  confidence_zh?: string;
  data_refs: string[];
  result: Record<string, unknown>;
};

export type LlmAuditEntry = {
  ts: string;
  model: string;
  provider: string;
  prompt_hash: string;
  response_hash: string;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  cache_hit: boolean;
  cache_kind: string;
  fallback_used: boolean;
  provider_chain: string[];
  error: string | null;
};

export type AgentMessage = {
  message_id: string;
  sender: string;
  receiver: string;
  message_type: "question" | "answer";
  content: string;
  timestamp: number;
  reply_to: string;
};

export type TaskDetail = {
  task_id: string;
  trace_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  conclusion: string | null;
  confidence: string | null;
  report: string | null;
  agent_messages: AgentMessage[];
  progress: string;
  errors: string[];
  error: string | null;
  created_at: string;
};

export type TraceDetail = {
  trace_id: string;
  status: string;
  query: string;
  agent_outputs: AgentOutputSummary[];
  errors: string[];
  llm_calls: LlmAuditEntry[];
  audit_chain: { valid: boolean; head: string; records: number };
};

export type Health = {
  status: string;
  agents: Record<string, string>;
  data_sources: {
    connectors: {
      name: string;
      status: string;
      simulated: boolean;
      indicators: string[];
    }[];
    storage: { backend: string; status: string; indicators?: number; points?: number };
    redis_cache: string;
    health?: DataHealth;
  };
  model_gateway: Record<string, string>;
  audit_chain: { valid: boolean; records: number };
};

// ---- 数据源健康度（能力矩阵 + 实测延迟 + Tushare 覆盖） ----

export type DataSourceCapability = {
  source: string; label: string; kind: string;
  realtime: boolean; fields: string; note: string;
};

export type DataSourceHealthRow = {
  source: string; label: string; kind: string; realtime: boolean;
  method: string; calls: number; failures: number;
  // 本段口径（熔断恢复后清零）用于判断"现在能不能信它"；
  // total_* 是终身累计，保留故障历史。
  total_calls?: number; total_failures?: number;
  success_rate: number | null;
  lifetime_success_rate?: number | null;
  ewma_ms: number | null; median_ms: number | null; samples: number;
  last_ms: number | null; last_error: string;
  cooling_down: boolean; cooldown_seconds: number; note: string;
};

export type TushareDatasetHealth = {
  dataset: string; partitions: number; rows: number;
  first: string; last: string; lag_days: number | null; stale: boolean;
};

export type WarehouseTableHealth = {
  dataset: string; table: string; rows: number; first: string; last: string;
};

export type WarehouseHealth = {
  available: boolean;
  dialect?: string;
  description?: string;
  error?: string;
  hint?: string;
  tables?: WarehouseTableHealth[];
  total_rows?: number;
  dataset_count?: number;
  earliest_date?: string;
  latest_date?: string;
};

export type DataHealth = {
  generated_at: string;
  capability_matrix: DataSourceCapability[];
  intraday_sources: {
    available: boolean;
    note?: string;
    sources?: DataSourceHealthRow[];
    ranking?: Record<string, string[]>;
    capabilities?: Record<string, DataSourceCapability>;
    measured_at?: string;
  };
  tushare: {
    label: string; kind: string; realtime: boolean;
    token: { configured: boolean; hint?: string; error?: string };
    datasets: TushareDatasetHealth[];
    partitions: number; rows: number; latest_date: string;
    note: string; intraday_usable: boolean;
  };
  warehouse?: WarehouseHealth;
  notes: string[];
};

export type SchedulerJob = {
  name: string;
  cron: string;
  kind: string;
  description: string;
  params: Record<string, unknown>;
  paused: boolean;
  last_run: {
    status: string;
    start_time: string;
    duration_ms: number;
    records_processed: number;
    error_message: string | null;
  } | null;
};

export type RunRecord = {
  run_id: string;
  job_name: string;
  trigger: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  status: "running" | "success" | "failed" | "skipped";
  records_processed: number;
  error_message: string | null;
  retries: number;
};

export type DailySummary = {
  date: string;
  total: number;
  success: number;
  failed: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
  failure_reasons: Record<string, number>;
};

export type LlmMetrics = {
  window_calls: number;
  errors: number;
  error_rate: number | null;
  cache_hit_rate: number | null;
  fallback_rate: number | null;
  latency_ms: { p50: number | null; p95: number | null; p99: number | null;
                 avg: number | null; max: number | null };
  slow_calls_over_3s: number;
  tokens: { in: number; out: number; total: number };
  by_provider: {
    provider: string; calls: number; error_rate: number | null;
    cache_hit_rate: number | null; p95_latency_ms: number | null;
  }[];
  by_agent: { agent_id: string; calls: number }[];
};

export type BacktestRequest = {
  indicator: string;
  code: string;
  asset_type?: "stock" | "index" | "etf";
  eps_pct: number;
  pe_watermark?: number | null;
  start_date?: string | null;
  end_date?: string | null;
  initial_capital?: number;
  commission_rate?: number;
  stamp_tax_rate?: number | null;
  slippage_rate?: number;
  cash_annual_yield?: number;
};

export type BacktestResponse = {
  asset: string;
  indicator: string;
  simulated: boolean;
  range: { start: string; end: string };
  pe_gate: { enabled: boolean; watermark: number | null;
              coverage: number; months_covered: number };
  cache: { indicator_hit: boolean; price_hit: boolean; pe_hit: boolean;
           ttl_seconds: number };
  rule: { trend_indicator: string; eps_pct: number;
          pe_watermark: number | null; kind: string;
          cost: { commission_rate: number; stamp_tax_rate: number;
                  slippage_rate: number; cash_annual_yield: number } };
  periods: number;
  signals: { long: number; neutral: number; avoid: number };
  directional: Record<string, {
    long: { n: number; hit_rate: number | null; avg_forward_return: number | null };
    avoid: { n: number; hit_rate: number | null; avg_forward_return: number | null };
    always_long_baseline: number | null;
    baseline_n: number;
  }>;
  strategy: {
    cumulative_return: number;
    cagr: number | null;
    annualized_volatility: number;
    max_drawdown: number;
    sharpe_rf0: number | null;
    invested_months: number;
    total_months: number;
    trades: number;
    total_transaction_cost: number;
    initial_capital: number;
    final_equity: number;
    buy_and_hold: {
      cumulative_return: number; cagr: number | null;
      annualized_volatility: number; max_drawdown: number;
      sharpe_rf0: number | null;
    };
    excess_cumulative_return: number;
  };
  equity_curve: { period: string; strategy: number;
                   buy_and_hold: number; signal: number }[];
  disclaimer: string;
};

export type AffectedStock = {
  code: string;
  name: string;
  impact: "positive" | "negative" | "mixed" | string;
  reason: string;
};

export type Alert = {
  alert_id: string;
  /**
   * ⚠️ 列表响应里**没有** `alert_key` / `content_key` / `tenant_id`。
   *
   * 它们是服务端去重键与多租户内部标识，前端零引用，却在 95 条里占了
   * 好几 KB —— 而隧道实测只有 ~51 KB/s，每个没人读的字段都是从用户
   * 等待时间里扣的（见 `alerts.py` 的 `_ALERT_LIST_DROP`）。
   */
  event_id: string;
  alert_type: "risk" | "opportunity";
  alert_level: "high" | "medium" | "low";
  title: string;
  description: string;
  risk_score: number;
  opportunity_score: number;
  confidence: number;
  affected_stocks: AffectedStock[];
  affected_industries: string[];
  impact_path: string;
  /**
   * 来源**假名**（`src-xxxxxxxx`），由后端下发。
   *
   * ## 为什么不是真名（2026-09-25 改）
   *
   * 原来是 `source_name: "东方财富-全球财经"` + `source_url: "https://..."`，
   * 任何登录用户按 F12 就能看到**我们用了哪几个免费渠道** ——
   * 而"渠道组合 + 采集节奏"正是这套系统的壁垒。
   *
   * ## 显示口径
   *
   * 它是**稳定假名**（同一来源恒定），只用来分组/去重。
   * 界面上按"来源已隐藏"呈现即可 —— **不要把假名印出来**，
   * 那会让人以为是个内部编号。
   *
   * 管理员请求时后端会额外带上 `source_name` / `source_url`（排障用）。
   */
  source_alias: string;
  /** ⚠️ 仅管理员响应里有；普通用户响应中**不存在这个键**。 */
  source_name?: string;
  /** ⚠️ 仅管理员响应里有；普通用户响应中**不存在这个键**。 */
  source_url?: string;
  event_publish_time: string;
  trigger_time: string;
  expire_time: string;
  status: "active" | "read" | "expired";
  /**
   * ⚠️ 列表响应里**不存在**（服务端为省带宽去掉了每条重复的 91 字节文案）。
   * 详情接口 `/alerts/{id}` 仍有。UI 用的是设置接口的
   * `settings.disclaimer`，见 `AlertDetail` 的 `disclaimer` prop。
   */
  disclaimer?: string;
};

export type MarketEvent = {
  event_id: string;
  event_key: string;
  event_type: "policy" | "sector" | "stock" | "calendar";
  title: string;
  content: string;
  source_name: string;
  source_url: string;
  publish_time: string;
  fetch_time: string;
};

export type ScanResult = {
  trigger: string;
  status: "success" | "partial" | "failed";
  scanned: number;
  new_events: number;
  alerts_created: number;
  by_level: Record<string, number>;
  by_type: Record<string, number>;
  per_source: Record<string, number>;
  email_results: { alert_id: string; status: string; detail: string }[];
  data_gaps: string[];
  errors: string[];
  started_at: string;
  finished_at: string;
};

export type AlertSettings = {
  available: boolean;
  confidence_min: number;
  min_level: string;
  cooldown_hours: number;
  candidate_limit: number;
  thresholds: Record<string, Record<string, number>>;
  email: { configured: boolean; smtp_host: string;
           risk_min_score: number; opp_min_score: number; to: string };
  schedule: {
    job: string; cron: string | null;
    // 定时班次时刻表：后端从调度注册表现算（改了 cron 不用改前端文案）
    jobs?: { job: string; cron: string; description: string }[];
    slots?: string[];
    weekday_only?: boolean;
    startup_scan?: boolean;
  };
  disclaimer: string;
};

export type ScanState = {
  running: boolean;
  started_at: string;
  result: ScanResult | null;
};

// ==================== 做T辅助模块 ====================

export type IntradayFactor = {
  key: string;
  label: string;
  weight: number;
  score: number;
  contribution: number;
  detail: string;
  inputs: Record<string, unknown>;
  available: boolean;
  gap: string | null;
};

export type IntradayScoreCard = {
  total: number;
  threshold_action: number;
  threshold_hint: number;
  zone: "strong_buy_zone" | "buy_zone" | "neutral" | "sell_zone" | "strong_sell_zone";
  verdict: string;
  factors: IntradayFactor[];
  weights_sum: number;
  available_weight: number;
  gaps: string[];
};

export type IntradayQuote = {
  code: string;
  name: string;
  price: number;
  prev_close: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  change: number | null;
  change_pct: number | null;
  volume: number | null;
  amount: number | null;
  turnover_rate: number | null;
  pe_ttm: number | null;
  pb: number | null;
  limit_up: number | null;
  ts: string;
};

export type IntradayBar = {
  ts: string;
  open: number; high: number; low: number; close: number;
  volume: number; amount: number; vwap: number | null;
};

export type IntradayTrendPoint = {
  ts: string;
  price: number;
  avg_price: number | null;
  volume: number;
};

/** 逐bar总分（与实时打分同一批打分核重放）：用于回答"摸到档位为什么没信号"。 */
export type IntradayScorePoint = {
  ts: string; price: number | null; total: number;
};

/** 逐bar档位：回踩/冲高/止损随时间的真实取值（档位是时刻量，不是全天恒定）。 */
export type IntradayLevelPoint = {
  ts: string; low_buy: number; high_sell: number; stop_loss: number;
};

export type IntradayLevels = {
  price: number;
  box_high: number; box_low: number;
  box_position: number | null;
  box_span_days: number;
  low_buy: number; high_sell: number; stop_loss: number;
  stop_loss_pct: number;
  /** 止损位是怎么定出来的（百分比口径 / ATR 口径 / 当日最低）。 */
  stop_basis?: string;
  boll_upper: number | null; boll_mid: number | null; boll_lower: number | null;
  pct_b: number | null; bandwidth: number | null;
  vwap: number | null; atr: number | null;
  // ---- 解释字段（服务端 annotate_level_basis 补，仅当前档位对象上有）----
  /** 回踩线由谁决定（箱体下沿/布林下轨/ATR 兜底）。 */
  low_source?: string;
  /** 冲高线由谁决定（箱体上沿/布林上轨/冲高缓冲）。 */
  high_source?: string;
  /** 实际档位差占现价%。 */
  band_width_pct?: number | null;
  /** 档位差是否被 min/max 护栏夹过（空串=没夹）。 */
  band_clamped?: string;
  /** 档位差护栏前的回踩线/冲高线候选位置。 */
  pre_clamp_low?: number | null;
  pre_clamp_high?: number | null;
  /** 回踩触发的最高价 = 回踩线×(1+贴线带宽)。 */
  low_trigger_price?: number | null;
  /** 冲高触发的最低价 = 冲高线×(1−贴线带宽)。 */
  high_trigger_price?: number | null;
  take_profit_buffer_pct?: number | null;
  dip_fallback_atr?: number | null;
  atr_stop_mult?: number | null;
};

export type IntradaySignal = {
  kind: "low_buy" | "high_sell" | "stop_loss" | "none";
  strength: "solid" | "hollow" | "forced_exit" | "none";
  triggered: boolean;
  price: number;
  ts: string;
  total_score: number;
  reason: string;
  blocked_by_stop_loss: boolean;
  target_level: number | null;
  pushed: boolean;
};

export type IntradayMarker = {
  ts: string;
  price: number;
  kind: "low_buy" | "high_sell" | "stop_loss";
  strength: "solid" | "hollow" | "forced_exit";
  label: string;
};

export type IntradayValuationPeer = {
  code: string; name: string;
  pe_ttm: number | null; pb: number | null;
  price: number | null; change_pct: number | null;
};

export type IntradayValuation = {
  available: boolean;
  code: string; name: string;
  pe_ttm: number | null; pb: number | null;
  pe_percentile: number | null; pb_percentile: number | null;
  pe_series_days: number; pb_series_days: number;
  pe_min: number | null; pe_max: number | null; pe_median: number | null;
  pb_min: number | null; pb_max: number | null; pb_median: number | null;
  peer_source: "configured" | "cninfo_industry" | "unavailable";
  peer_label: string;
  peers: IntradayValuationPeer[];
  peer_pe_median: number | null; peer_pb_median: number | null;
  peer_count: number;
  pe_vs_peer_pct: number | null; pb_vs_peer_pct: number | null;
  industry_pe_median: number | null; industry_name: string;
  industry_company_count: number | null;
  verdict: string;
  headroom: "ample" | "moderate" | "stretched" | "expensive" | "unknown";
  score: number | null;
  gap: string | null;
  source_name: string;
};

export type IntradayBoard = {
  name: string; kind: string; available: boolean;
  change_pct: number | null;
  up_count: number | null; down_count: number | null;
  breadth: number | null;
  amount: number | null; net_inflow: number | null;
  rank: string | null;
  open_price: number | null; prev_close: number | null;
  high: number | null; low: number | null;
  source_name: string; gap: string | null;
};

export type IntradayBoardSeries = {
  name: string; kind: string; available: boolean;
  points: IntradayTrendPoint[];
  source_name: string; gap: string | null;
};

export type IntradaySentiment = {
  board_name: string;
  board_change_pct: number | null;
  up_count: number | null; down_count: number | null; breadth: number | null;
  breadth_score: number | null;
  boards: IntradayBoard[];
  boards_bound: boolean;
  board_series: IntradayBoardSeries[];
  stock_change_pct: number | null;
  relative_strength_pct: number | null;
  rs_score: number | null;
  index_name: string; index_code: string;
  index_price: number | null; index_change_pct: number | null; index_state: string;
  score: number | null; verdict: string;
  gaps: string[]; source_name: string;
};

export type IntradayNewsItem = {
  title: string; source_name: string; source_url: string;
  publish_time: string; polarity: "positive" | "negative" | "neutral";
};

export type IntradayNews = {
  available: boolean;
  score: number;
  positive_count: number; negative_count: number; neutral_count: number;
  count_score: number;
  llm_score: number | null; llm_used: boolean;
  model: string; summary: string;
  items: IntradayNewsItem[];
  news_count: number;
  gap: string | null; source_name: string;
};

export type IntradaySourceAttempt = {
  source: string; ok: boolean; detail: string; rows: number;
  latency_ms: number | null;
};

export type IntradayHealth = {
  chosen_intraday_source: string | null;
  chosen_daily_source: string | null;
  chosen_quote_source: string | null;
  attempts: IntradaySourceAttempt[];
  gaps: string[];
  stale: boolean;
  trade_date: string;
};

/** 所属指数量能（指数量能因子）。 */
export type IntradayIndexVolume = {
  code: string; name: string; available: boolean;
  change_pct: number | null;
  volume: number | null; amount: number | null;
  yesterday_volume: number | null; projected_volume: number | null;
  volume_ratio: number | null; elapsed_ratio: number;
  verdict: string; source_name: string; gap: string | null;
};

/** 海外映射单条行情。 */
export type IntradayOverseasQuote = {
  symbol: string; name: string; market: string;
  change_pct: number | null; price: number | null;
  prev_close: number | null; quote_time: string;
  available: boolean; gap: string | null;
};

/** 海外映射汇总（美股隔夜 + 韩股盘中同步）。 */
export type IntradayOverseas = {
  available: boolean;
  score: number | null;
  overnight_score: number | null; intraday_score: number | null;
  overnight_avg_pct: number | null; intraday_avg_pct: number | null;
  verdict: string; gaps: string[];
  quotes: IntradayOverseasQuote[];
};

export type IntradaySnapshot = {  code: string; name: string; trade_date: string; generated_at: string;
  session_state: "pre_open" | "call_auction" | "trading" | "lunch_break" | "closed";
  session_label: string;
  quote: IntradayQuote | null;
  trend: IntradayTrendPoint[];
  bars: IntradayBar[];
  levels: IntradayLevels | null;
  scorecard: IntradayScoreCard | null;
  signal: IntradaySignal | null;
  markers: IntradayMarker[];
  score_series: IntradayScorePoint[];
  level_series: IntradayLevelPoint[];
  valuation: IntradayValuation | null;
  sentiment: IntradaySentiment | null;
  news: IntradayNews | null;
  index_volume: IntradayIndexVolume | null;
  overseas: IntradayOverseas | null;
  /**
   * **今日做T决策**（情绪周期结论）：周期阶段 +（不）做T降本 + 是否禁止追高。
   *
   * 用户口径 2026-09-23：原来这三条以"数据缺口"形式堆在底部数据健康度里，
   * 现在提到顶部「市场环境」行右侧显示成一句决策，逐条依据放在 tooltip。
   * 周期不可用时为 `null`（不显示徽标，也不编造结论）。
   */
  cycle_decision: IntradayCycleDecision | null;
  /** 档位拟合摘要（规则口径 / 拟合口径、两个成功率、调整项乘数）。 */
  level_fit: IntradayLevelFit | null;
  health: IntradayHealth;
  config_snapshot: Record<string, unknown>;
  notifier: Record<string, unknown>;
  disclaimer: string;
};

/** 情绪周期 → 今日做T决策（顶部「市场环境」行右侧的徽标）。 */
export type IntradayCycleDecision = {
  available: boolean;
  /** 周期阶段：主升期 / 试错期 / 退潮期 / 冰点 … */
  stage: string;
  /** 做T环境温度 0~100 */
  temperature: number | null;
  /** 是否允许做T降本（false = 退潮/冰点或触发一票否决） */
  t_allowed: boolean;
  /** 是否禁止追高（一票否决或禁止做T时为 true） */
  no_chase: boolean;
  /** 配置里是否开启了"周期否决正式信号" */
  veto_signals_on: boolean;
  /** 一句话结论，如「退潮期 · 缩量 1,180 亿 · 不做T降本 · 禁止追高」 */
  summary: string;
  /** 触发一票否决的逐条依据（大面/跌停家数超标等），供 tooltip 核对 */
  reasons: string[];
  trade_date: string;
  /**
   * 全市场量能预测（沪深京三市，**同一时刻同比昨日**）。
   *
   * 用户口径 2026-09-23：「如果预测量能低于 2 万亿或相比昨日缩量 1000 亿以上，
   * 提示缩量XX亿不追高；放量XX亿可做T」。与情绪周期**并列**进入同一枚徽标。
   * 取不到时为 `null`（徽标少这一段，不编造结论）。
   */
  turnover: IntradayMarketTurnover | null;
  /** 量能那一段的短句，如「缩量 1,180 亿 不追高」 */
  turnover_text: string;
  /** "缩量" / "放量" / "平量" / ""（空=没有量能数据） */
  turnover_verdict: string;
};

/**
 * 全市场成交额预测量能（顶部决策徽标里的量能那一段）。
 *
 * 口径 = 昨日全天成交额 × (今日累计额 ÷ 昨日**同一时刻**累计额)。
 * 为什么不用「累计 ÷ 已交易时间占比」：A股日内量能是 U 型，早盘按时间外推会
 * 系统性高估（详见后端 `src/intraday/market_amount.py` 的说明与实测表）。
 */
export type IntradayMarketTurnover = {
  available: boolean;
  /** 数据日（YYYYMMDD） */
  trade_date: string;
  /** 对比日（上一交易日） */
  prev_date: string;
  fetched_at: string;
  source: string;
  /** 对比时刻（HHMM，如 "1030"） */
  moment: string;
  /** 今日当前累计成交额（元；**不含京市**，它没有日内成交额曲线） */
  today_amount: number | null;
  /** 昨日同一时刻累计成交额（元） */
  prev_same_time_amount: number | null;
  /** 昨日全天成交额（元，三市口径） */
  prev_total_amount: number | null;
  /** 全天预测成交额（元，三市口径） */
  projected_amount: number | null;
  /** 预测量 − 昨日全天（正=放量，负=缩量） */
  delta_amount: number | null;
  /** 预测量 ÷ 昨日全天 */
  ratio: number | null;
  verdict: string;
  verdict_text: string;
  /** 放量/持平 → true；缩量（含地板或缩量超 1000 亿）→ false */
  chase_allowed: boolean;
  t_allowed: boolean;
  /** 命中了哪条阈值判定 */
  reasons: string[];
  /** 数据缺口/口径说明（如京市按量比折算） */
  notes: string[];
  gap: string | null;
  /** 各市场明细 `{market: {name, today, prev_same, prev_total, projected}}` */
  markets: Record<string, Record<string, unknown>>;
  /** 参与合计的市场名（缺哪个市场一眼可见） */
  markets_used: string[];
};

export type IntradayWatchItem = {  code: string; name: string; boards: string[];
  total_score: number | null;
  signal_strength: "solid" | "hollow" | "forced_exit" | "none";
  signal_kind: "low_buy" | "high_sell" | "stop_loss" | "none";
  price: number | null; change_pct: number | null;
  /** 价格的取数时刻（报价快车道，每几秒更新） */
  quote_ts?: string;
  /** 是否置顶：置顶项永远排最前（状态存在配置文件里） */
  pinned?: boolean;
  /**
   * 估值结论短标签（用户口径 2026-09-23）：显示在「观望」信号右侧。
   * 原来是主区域整块「① 估值空间」面板，已按用户要求收成列表里的一个标签。
   * 取值为 `上涨空间充足 / 估值合理 / 估值合理偏贵 / 估值透支`，空串=没算出来。
   */
  valuation_label?: string;
  /** 结论档位 `ample|moderate|stretched|expensive`（前端据此上色；空串=未计算） */
  valuation_bucket?: string;
};

/** 自选池自动刷新的运行状态（服务端每分钟重算一次，前端据此展示刷新时间/暂停原因）。 */
export type IntradayWatchRefresh = {
  enabled: boolean;
  interval_seconds: number;
  window_open: boolean;
  window_reason: string;
  running: boolean;
  generation: number;
  /**
   * true = 服务端此刻**没有任何缓存**（首次部署 / 热缓存超过 24 小时 /
   * 自选池改动较大），返回的是只有代码·名称·板块的**占位列表**：
   * 现价由报价快车道几秒内贴上，分数/信号要等后台整表重算（冷启动实测约 200 秒）。
   * 前端据此显示"首次重算中"，而不是把 "—" 当成数据坏了。
   */
  warming?: boolean;
  cache_age_seconds: number | null;
  cached_count: number;
  last_run_at: string;
  last_seconds: number;
  last_count: number;
  last_error: string;
  /** 报价快车道（现价/涨跌幅）：与打分重算是两条独立节奏。 */
  quote_enabled: boolean;
  quote_interval_seconds: number;
  quote_running: boolean;
  quote_generation: number;
  quote_covered: number;
  quote_last_run_at: string;
  quote_last_seconds: number;
  quote_last_count: number;
  quote_last_error: string;
};

export type IntradayWatchPayload = {
  items: IntradayWatchItem[];
  count: number;
  auto_refresh: IntradayWatchRefresh;
};

/** 配置里去重后的自选标的（新增/删除接口返回的口径）。 */
export type IntradayWatchConfigItem = {
  code: string; name: string; boards: string[];
  industry: string; peers: string[]; overseas: string[];
};

export type IntradayConfigPayload = {
  weights: { key: string; label: string; weight: number }[];
  weights_sum: number;
  /** YAML 解析失败时的原因（后端返回，非空即说明口径回落默认值）。 */
  load_error: string | null;
  thresholds: { action: number; hint: number };
  levels: Record<string, number | boolean>;
  /** 个股微调（configs/intraday.yaml 的 overrides）：这只票改了哪些参数。 */
  overrides: Record<string, {
    weights: Record<string, number>;
    thresholds: Record<string, number>;
    levels: Record<string, number>;
    describe: string;
  }>;
  data: Record<string, unknown>;
  session: Record<string, string>;
  boards: { name: string; kind: string; aliases: string[] }[];
  watchlist: { code: string; name: string; boards: string[]; peers: string[] }[];
  notifier: Record<string, unknown>;
  snapshot: Record<string, unknown>;
  disclaimer: string;
};

export type IntradayThresholdStat = {
  threshold: number;
  direction: "long" | "short" | "all";
  signals: number;
  hit_rate: number | null;
  avg_forward_return_pct: number | null;
  median_forward_return_pct: number | null;
  excess_vs_baseline_pct: number | null;
};

export type IntradayBacktestTrade = {
  entry_ts: string; exit_ts: string;
  direction: "long_t" | "short_t";
  entry_price: number; exit_price: number; return_pct: number;
  exit_reason: string; total_score_at_entry: number;
  strength: "solid" | "hollow" | "forced_exit";
};

export type IntradayBacktest = {
  available: boolean;
  code: string; name: string;
  days: number; bars: number;
  range_start: string; range_end: string; horizon_bars: number;
  trades: IntradayBacktestTrade[];
  action_line: IntradayThresholdStat | null;
  hint_line: IntradayThresholdStat | null;
  baseline: IntradayThresholdStat | null;
  by_threshold: IntradayThresholdStat[];
  total_return_pct: number | null;
  win_rate: number | null;
  max_drawdown_pct: number | null;
  gaps: string[];
  verdict: string;
  disclaimer: string;
};

/** 做T实时快照WebSocket地址（服务端按周期主动推）。 */
export function intradayWsUrl(code: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/v1/ws/intraday?code=${encodeURIComponent(code)}`;
}

// ==================== 做T因子目录 / 股性 / 权重档案 ====================

/** 做T模式：intraday=日内分时做T，daily=日K级别做T。 */
export type IntradayMode = "intraday" | "daily";

/**
 * 单个因子的展示与契约信息（**目录由服务端给，前端不写死任何中文名或顺序**）。
 *
 * 为什么坚持不写死：因子清单与中文标签是会变的产品口径（本次就新增了 4 个技能库
 * 因子），前端硬编码一份必然与后端漂移 —— 而漂移的权重表会让用户保存出
 * 一套后端不认识的口径。
 */
export type FactorMetaItem = {
  key: string;
  label: string;
  group: string;
  group_label: string;
  default_weight: number;
  /** 一句话公式（前端折叠展示"这个分数是怎么算出来的"）。 */
  formula: string;
  /** 数据来源（技能库来源因子的 title 提示用它）。 */
  source: string;
  /** 是否来自交易技能库（前端打「技能」角标）。 */
  from_skill: boolean;
};

/** 一套命名权重配方（已归一化到合计 100）。 */
export type WeightTemplate = {
  key: string;
  label: string;
  description: string;
  weights: Record<string, number>;
};

/** 权重档案（数据库里的个股口径覆盖）。 */
export type WeightProfile = {
  code: string; name: string;
  weights: Record<string, number>;
  daily_weights: Record<string, number>;
  thresholds: Record<string, number>;
  daily_thresholds: Record<string, number>;
  levels: Record<string, number>;
  character_profile: Record<string, unknown>;
  template: string;
  source: "manual" | "auto_character" | "import";
  note: string;
  created_at: string;
  updated_at: string;
  describe: string;
  weights_sum: number;
  daily_weights_sum: number;
};

/** 因子目录 + 权重模板（权重编辑器唯一的数据来源）。 */
export type IntradayFactorCatalog = {
  mode: IntradayMode;
  factors: FactorMetaItem[];
  groups: { key: string; label: string }[];
  templates: WeightTemplate[];
  /** 当前全局生效权重（保存时与它比较，只提交差集）。 */
  current_weights: Record<string, number>;
  thresholds: { action: number; hint: number };
  /** 分时才有意义（档位）；daily 时后端返回 {}。 */
  levels: Record<string, number | boolean>;
  weights_sum: number;
  /** 归一化后允许的最小可见权重（低于它的项在滑杆上会抖）。 */
  min_visible_weight: number;
  // ---- 仅当传了 code ----
  code?: string;
  effective_weights?: Record<string, number>;
  /** 例「权重档案（manual，更新于 …）」；无覆盖时为空串。 */
  override_source?: string;
  override?: {
    weights: Record<string, number>;
    daily_weights: Record<string, number>;
    thresholds: Record<string, number>;
    levels: Record<string, number>;
    describe: string;
  } | null;
};

/** 个股股性画像：做T友好度 + 预填权重模板 + 预填档位。 */
export type CharacterProfile = {
  code: string; name: string; available: boolean; mode: string;
  sampled_days: number; source: string; gap: string | null;
  atr_pct: number | null; avg_amplitude_pct: number | null;
  avg_turnover: number | null;
  volume_activity: number | null; trend_efficiency: number | null;
  gap_frequency: number | null;
  limit_up_count: number; limit_up_freq: number | null;
  volatility_percentile: number | null;
  regime: "swing" | "mixed" | "trend";
  grade: "活跃" | "温和" | "钝化";
  /** 0-100 做T友好度。 */
  t_friendly: number;
  /** 预填模板 key。 */
  template: string;
  /** 预填权重（已按股性微调并归一化到 100）。 */
  weights: Record<string, number>;
  /** 预填档位（min_band_pct/max_band_pct/stop_loss_pct/atr_stop_mult/
   *  touch_band_pct/dip_fallback_atr）。 */
  levels: Record<string, number>;
  /** 中文人话说明，直接展示。 */
  notes: string[];
  template_label: string;
  template_description: string;
  templates: WeightTemplate[];
};

/** 档位神经网络拟合：成功率、可达上限、闸门与拟合线（服务端口径，见 level_fit.py）。 */
export type IntradayLevelFitMetrics = {
  available: boolean;
  reason: string;
  sessions: number;
  bars: number;
  horizon_bars: number;
  touch_samples: number;
  /** 训练窗口（过去 N 个交易日）内的成功率 —— 用户口径的那个数。 */
  in_sample_rate: number | null;
  in_sample_touches: number;
  /** 留一日交叉验证成功率 —— 决定能不能启用拟合档位。 */
  walk_forward_rate: number | null;
  walk_forward_touches: number;
  avg_round_trip_pct: number | null;
  /** 放宽搜索能达到的上限：用来区分"没搜到"与"到不了"。 */
  best_achievable_rate: number | null;
  best_achievable_lines: number[];
  target_hit_rate: number;
  gate_passed: boolean;
  gate_reason: string;
  elapsed_ms: number;
};

export type IntradayLevelFit = {
  code: string;
  trade_date: string;
  fitted_at: string;
  metrics: IntradayLevelFitMetrics;
  low_mix: number[];
  high_mix: number[];
  stop_mix: number[];
  low_anchors: number[];
  high_anchors: number[];
  stop_anchors: number[];
  feature_means: number[];
  feature_stds: number[];
  features: { key: string; label: string }[];
  notes: string[];
  /** 快照里额外带的：调整项乘数（其余 7 个维度）与"是否已用于档位"。 */
  adjustment?: Record<string, number>;
  applied?: boolean;
  notice?: string;
};

/** 资金流监控：一天的一条资金流（单位统一为元）。 */
export type FlowPoint = {
  date: string;
  net: number | null;
  buy_lg: number | null;
  sell_lg: number | null;
  buy_elg: number | null;
  sell_elg: number | null;
  /**
   * 日线 OHLCV：走势图叠加 K 线与成交量柱用（**只有个股有**，板块这几项为 null）。
   * 单位：价格=元，volume=股，amount=元，pct_chg=%。
   */
  close: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  volume?: number | null;
  amount?: number | null;
  pct_chg?: number | null;
};

/** 资金流监控：一个被监控实体（板块或个股）及其近 N 日资金流。 */
export type FlowEntity = {
  kind: "sector" | "stock";
  code: string;
  name: string;
  source: "manual" | "default" | string;
  available: boolean;
  unit: string;
  data_source: string;
  series: FlowPoint[];
  /** 近 N 日净额均值（元）。 */
  net_avg: number | null;
  /** 个股：净额均值 / 流通市值（越小越"轻"，用于跨大小盘排序）。 */
  net_to_mv: number | null;
  circ_mv: number | null;
  /** 板块：当日盘中净额（元，同花顺即时口径）。 */
  today_net: number | null;
  change_pct: number | null;
  latest_net: number | null;
  latest_date: string;
  gap: string | null;
  notes: string[];
  /**
   * 个股榜的类别（2026-09-17 口径）：昨日涨停 / 净流入前10 / 净流出前10 / 自选 / 其他。
   * 板块实体没有这个字段。
   */
  rank_group?: string;
  /** 涨停原因（东财涨停池的行业/题材归类，含连板数）；非涨停股为空串 */
  limitup_reason?: string;
  /** 涨停原因对应的交易日（避免把昨天的池子标成今天） */
  limitup_date?: string;
  /** 涨幅来源（腾讯盘中快照 / 本地仓库日频），用于判断数据新旧 */
  change_source?: string;
};

export type FlowBoard = {
  generated_at: string;
  window_days: number;
  trade_date: string;
  session_state: string;
  session_label: string;
  sector_rank: FlowEntity[];
  stock_rank: FlowEntity[];
  /**
   * 用户自选个股，**单列一节、不占 `stock_rank` 的名额**。
   *
   * 2026-09-22 之前自选混在 `stock_rank` 里（`rank_group="自选"`），实测把
   * `top=10` 的净流入榜挤到只剩 2 只 —— 手动加了几只票，排行榜就不成其为榜。
   * 旧后端没有这个字段，故可选：缺失时按空数组处理。
   */
  stock_watch?: FlowEntity[];
  sectors: FlowEntity[];
  stocks: FlowEntity[];
  source_notes: string[];
  gaps: string[];
  refresh_hint: string;
  notice?: string;
};

export type FlowWatchItem = {
  kind: "sector" | "stock";
  code: string;
  name: string;
  source: string;
  added_at: string;
};

// ---------------- 量化选股（3 档模型 + 自定义板块） ----------------

/** 量化选股模块状态。 */
export type QuantSelectStatus = {
  available: boolean;
  reason: string;
  /** 模型文件名（如 core_v1_20-150亿_20260917-160502.joblib）。 */
  model_version: string;
  /** 三档模型名（20-150亿 / 150-500亿 / 500亿+）。 */
  model_buckets: string[];
  model_trained_at: string;
  last_run_at: string;
  last_trade_date: string;
  last_selected: number;
  last_seconds: number;
  last_error: string;
  running: boolean;
  /** 模型缺失时是否正在**自动训练**（后端后台跑，前端据此提示"训练中"）。 */
  training: boolean;
  sectors: number;
};

/** 选股结果里的单只票。 */
export type QuantSelectionItem = {
  code: string;
  name: string;
  score: number;
  rank: number;
  /** 该票打分时实际使用的市值档（三档模型里的哪一档）。 */
  cap_bucket: string;
  circ_mv: number | null;
  /**
   * 当日涨跌幅（%）：**已实现的收盘涨跌**，不是预测值。
   * 算不出来时为 null（老记录没这列 / 新股首日无前收）—— 界面显示 `—`。
   */
  pct_chg: number | null;
  /** 命中的自定义板块名（既是归类标签，也是选股范围的回执）。 */
  sectors: string[];
  factors: Record<string, number | null>;
  /** 是否已加进做T自选（后端在加入时统一打标）。 */
  added: boolean;
};

/** 选股结果用的个股消息面（新闻/公告）单条。 */
export type QuantStockNewsItem = {
  title: string;
  /** 发布时间（形如 `2026-09-18 10:31:30`）。 */
  publish_time: string;
  /** 媒体名（财联社 / 证券时报网 …）。 */
  media: string;
  url: string;
  summary: string;
};

/** 一只票的消息面；取不到时 `items` 为空且 `reason` 说明原因（不编造新闻）。 */
export type QuantStockNews = {
  code: string;
  items: QuantStockNewsItem[];
  source: string;
  reason: string;
};

/** 批量加入做T自选池的结果（「一键全部加自选」）。 */
export type QuantBatchWatchResult = {
  /** 是否真的写了配置（全部都已在池中且无需补名时为 false）。 */
  saved: boolean;
  requested: number;
  /** 本次新增进自选池的代码。 */
  added: string[];
  /** 本来就在池中、但名字是空的，本次顺手补上了名字（其余字段未动）。 */
  repaired: string[];
  /** 本来就在池中、本次**未改动**的（避免覆盖手配的板块/海外映射）。 */
  existing: string[];
  /** 代码格式不合法、没写进去的。 */
  failed: { code: string; reason: string }[];
  /** 写进去了但本地字典查不到中文名（配置里名字为空，不拿代码冒充）。 */
  missing_name: string[];
  /** 写入后自选池总只数。 */
  total: number;
  /** 被回标成「已加」的历史选股明细行数。 */
  marked_runs: number;
};

/** 一次选股运行。 */
export type QuantSelectionRun = {
  id: number;
  trade_date: string;
  /** open=开盘窗口(9:25-9:45) / close=尾盘(14:45) / manual=手动。 */
  window: "open" | "close" | "manual";
  ran_at: string;
  model_version: string;
  model_detail: string;
  threshold: number;
  scored: number;
  top_n: number;
  selected: number;
  sector_filter: string[];
  seconds: number;
  gaps: string[];
  error: string;
  triggered_by: string;
  /**
   * 数据新鲜度（服务端**读时计算**，不落库：老记录也能被正确重判）。
   *
   * `data_stale=true` 表示这一轮用的行情不是最近一个已收盘交易日的 ——
   * 分数、阈值、排名全都算得出来、界面上看不出异常，但它描述的是旧市场。
   * 起因是本地行情仓库没同步（`quant_data_sync` 作业负责推进）。
   */
  data_stale?: boolean;
  expected_trade_date?: string;
  /** 滞后时给人看的说明（含"实际用的"与"应该用的"两个日期）。 */
  note?: string;
  /** false = 交易日历不可用、**没做判断**（不能读成"确认新鲜"）。 */
  checked?: boolean;
  items?: QuantSelectionItem[];
};

/** 自定义板块（既是选股范围也是归类标签）。 */
export type QuantSector = {
  id: number;
  name: string;
  /** manual=手工成分股；dynamic=按规则由服务端求值。 */
  kind: "manual" | "dynamic";
  note: string;
  rule: Record<string, unknown>;
  color: string;
  sort_order: number;
  members: { code: string; name: string }[];
  member_count: number;
  created_at: string;
  updated_at: string;
};

/** 市场情绪周期（涨停家数 / 炸板率 / 最高连板 → 阶段与做T环境温度）。 */
export type MarketCycle = {
  available: boolean;
  trade_date: string;
  fetched_at: string;
  source: string;
  gap: string | null;
  limit_up_count: number;
  limit_down_count: number;
  broken_count: number;
  strong_count: number;
  first_board: number;
  streak2plus: number;
  max_streak: number;
  big_loss_count: number;
  broken_rate: number | null;
  promotion_rate: number | null;
  stage: "冰点" | "试错期" | "发酵期" | "主升期" | "高位震荡期" | "退潮期";
  temperature: number;
  t_allowed: boolean;
  /** 退潮/冰点期的闸门说明（红字展示）。 */
  gates: string[];
  notes: string[];
  gaps: string[];
  thresholds: Record<string, number>;
  verdict: string;
};

export type IntradayWeightProfileList = {
  count: number;
  profiles: WeightProfile[];
  source: string;
};

export type IntradayWeightProfileDetail = {
  code: string;
  /** 该票的档案；null 表示没有个股档案。 */
  profile: WeightProfile | null;
  /** 当前实际生效的口径（档案 > YAML overrides > 全局）。 */
  effective: {
    weights: Record<string, number>;
    daily_weights: Record<string, number>;
    thresholds: { action: number; hint: number };
    levels: Record<string, number>;
    source: string;
  };
};

/**
 * 保存档案入参（**稀疏差分**：只提交被改过的项）。
 *
 * 为什么必须是差集：档案是"这只票相对全局口径的覆盖"。如果每次都提交全量，
 * 全局默认值就被固化进这只票的档案里 —— 以后调全局权重，这只票不跟着变，
 * 而用户完全看不出为什么只有它不动。
 */
export type IntradayWeightProfileRequest = {
  name?: string;
  weights?: Record<string, number>;
  daily_weights?: Record<string, number>;
  thresholds?: Record<string, number>;
  levels?: Record<string, number>;
  daily_thresholds?: Record<string, number>;
  template?: string;
  source?: "manual" | "auto_character" | "import";
  note?: string;
  character_profile?: Record<string, unknown>;
  /** true=服务端用该票股性画像补全未给出的权重/档位。 */
  apply_character?: boolean;
};

export type IntradayWeightProfileSaved = {
  ok: true;
  code: string;
  profile: WeightProfile;
  describe: string;
  notice: string;
};

/** 权重预览入参（不落库、不动缓存；至少给一项改动）。 */
export type IntradayWeightPreviewRequest = {
  code: string;
  mode: IntradayMode;
  weights?: Record<string, number>;
  thresholds?: Record<string, number>;
  levels?: Record<string, number>;
  /**
   * 可选：「面板此刻」的档位（low_buy/high_sell/stop_loss/vwap）。
   * 服务端用它当「改动前」那一列 —— 传了它，"变动"就只反映参数改动，
   * 不会把这几秒内 VWAP 的自然漂移算成你改出来的。
   */
  current_levels?: Record<string, number>;
};

/** 一条档位线：现在是多少 + 离现价多远 + 谁定的 + 触发价。 */
export type IntradayTriggerLevel = {
  key: string;
  label: string;
  price: number;
  /** (线 - 现价)/现价×100，负=在现价下方。 */
  distance_pct: number;
  source: string;
  trigger_price: number | null;
  trigger_note: string;
  note: string;
};

/** 「差多少分 / 差多少价才出信号」一行。 */
export type IntradayTriggerGate = {
  key: string;
  label: string;
  ready: boolean;
  /** 总分还差多少（正=还差这么多）。 */
  score_need: number;
  /** 价格还要走多少%（负=还要往下跌）。 */
  price_need_pct: number | null;
  blocked_by: string;
  note: string;
};

/** 单因子对总分的**影响度**（权重口径，与档位线无关）。 */
export type IntradayFactorImpact = {
  key: string;
  label: string;
  weight: number;
  score: number;
  contribution: number;
  /** = 得分：权重每 +1 分对总分的推动（负=拉低总分）。 */
  unit_impact: number;
  /** 把这一项权重置 0 时总分的变化。 */
  zero_impact: number;
  /** 该权重占**有效权重**的比例%。 */
  weight_share_pct: number;
  available: boolean;
  gap: string | null;
  note: string;
};

/** 「改动前 → 改动后」一条价格线的变动（服务端算，前端只做展示）。 */
export type IntradayLevelDelta = {
  key: string;
  label: string;
  /** 该票**当前生效口径**下这条线的位置（无基准时为空）。 */
  current: number | null;
  preview: number;
  delta: number | null;
  delta_pct: number | null;
};

/** 「参数 → 价格线 / 触发门槛」的可解释推导（服务端与图上那条线同源）。 */
export type IntradayTriggerImpact = {
  available: boolean;
  reason: string;
  price: number;
  low_trigger_price: number | null;
  high_trigger_price: number | null;
  stop_price: number | null;
  total: number;
  threshold_action: number;
  threshold_hint: number;
  available_weight: number;
  coverage_blocked: boolean;
  cycle_blocked: boolean;
  cycle_stage: string;
  level_rows: IntradayTriggerLevel[];
  /** 「改动前 → 改动后」逐线对照（服务端按当前口径算好）。 */
  level_deltas: IntradayLevelDelta[];
  gates: IntradayTriggerGate[];
  factor_impact: IntradayFactorImpact[];
  examples: string[];
  notes: string[];
};

/**
 * 权重预览结果。预览走的是服务端**同一条打分链路**（完整快照），
 * 因此预览分数与保存后的真实分数必然同源同口径 —— 后端耗时 1~3 秒。
 */
export type IntradayWeightPreview = {
  code: string;
  mode: string;
  weights_sum: number;
  scorecard: IntradayScoreCard;
  notice: string;
  /** 预览下的实际生效权重（未给的项沿用全局）。 */
  weights: Record<string, number>;
  thresholds: { action: number; hint: number };
  levels: Record<string, number>;
  /** 预览口径下的**真实档位对象**（含各线的来源与触发价）。 */
  level_set?: IntradayLevels | null;
  /** 「价格线 / 触发门槛 / 因子影响度」的展开（保存前就能看到会变成多少）。 */
  impact?: IntradayTriggerImpact | null;
  signal: Record<string, unknown> | null;
};

// ==================== 日K级别做T（量价体系） ====================

export type IntradayDailyBar = {
  date: string;
  open: number; high: number; low: number; close: number;
  volume: number; amount: number;
  pct_chg: number | null; amplitude: number | null; turnover: number | null;
  /**
   * 主力资金净流入额（元；负值=净流出）。来自本地行情仓 `quant_moneyflow`，
   * 由后端在日K快照上补齐（`src/intraday/day_extras.py`）：
   * **只供读数与区间统计，不参与打分与信号**。当日那根形成中bar没有该值（日频 T-1 定稿）。
   */
  net_mf: number | null;
  is_high_volume: boolean; is_double_volume: boolean;
  is_shrink_volume: boolean; is_shrink_half: boolean;
  is_ladder_down: boolean; is_flat_volume: boolean;
  is_ground_volume: boolean; is_explode_volume: boolean;
  is_long_lower_shadow: boolean; is_big_yang: boolean; is_big_yin: boolean;
  body_top: number; body_bottom: number;
  ma5: number | null; ma10: number | null;
  ma20: number | null; ma60: number | null;
};

export type IntradayVolumeAnchor = {
  date: string; index: number; volume: number;
  body_top: number; body_bottom: number;
  kind: "标杆" | "梯量" | "缩量" | "倍量";
  position: "底部" | "中继" | "接力" | "顶部";
  days_since: number;
  status: "有效支撑" | "待观察" | "已破位";
  note: string;
};

export type IntradayVpPattern = {
  code: string; name: string; category: "单K" | "组合";
  price_dir: "涨" | "跌" | "平";
  price_speed: "加速" | "减速" | "平";
  volume_dir: "放量" | "缩量" | "平量";
  meaning: string; signal: string;
  direction: "bullish" | "bearish" | "reversal" | "neutral";
  detail: string;
};

export type IntradayPosition = {
  label: "低位" | "中位" | "高位";
  percentile: number; window: number;
  high: number; low: number;
  from_high_pct: number; from_low_pct: number;
  note: string;
};

export type IntradayCostLine = {
  kind: string; date: string; price: number;
  distance_pct: number; broken: boolean; note: string;
};

export type IntradaySignalCondition = {
  label: string; met: boolean;
  actual: string; expected: string; note: string; required: boolean;
};

export type IntradayDailySignal = {
  code: string; name: string;
  kind: "buy" | "sell" | "risk" | "watch";
  triggered: boolean; score: number;
  conditions: IntradaySignalCondition[];
  reason: string;
  entry: number | null; stop_loss: number | null; target: number | null;
  gaps: string[];
};

export type IntradayProtective = {
  stop_line: number | null; stop_basis: string;
  trail_line: number | null; trail_basis: string;
  ma20: number | null; ma60: number | null;
  broken_stop: boolean; broken_trail: boolean; note: string;
};

export type IntradayDailySignalMark = {
  date: string;
  /** buy=多方触发；sell=空方触发(S1–S3/S6)；risk=风控/止损类 */
  side: "buy" | "sell" | "risk";
  code: string;
  name: string;
  price: number | null;
  entry: number | null;
  stop_loss: number | null;
};

/** 擒牛线在某一根日线上的五个档位值（null = 该线暖机未完成）。 */
export type NiuLinePoint = {
  date: string;
  nml: number | null;
  qrl: number | null;
  cbx20: number | null;
  cbx60: number | null;
  smx: number | null;
};

/**
 * 擒牛线档位线体系（日K做T 主图）—— **下发给前端的部分**。
 *
 * ⚠️ 用户口径 2026-09-23：**计算口径属于核心机密，不下发**。
 * 这里刻意只有"画图与判断需要的东西"：
 *   - `latest`：五条线的当期数值（状态条显示"站上/跌破"）；
 *   - `lines`：`key` + `label`（线名），用于图例与读数条；
 *   - `points`：逐 bar 序列（画线用）。
 *
 * 曾经存在的 `variant` / `reason` / `price_basis` / `cbx_scale` / `n` / `m` /
 * `notes` / `lines[].note` **已从后端 payload 中移除** —— 它们足以还原整套公式，
 * 所以不是"前端不渲染"，而是根本不下发。需要看口径请读
 * `src/intraday/niuline.py` 或服务端日志，不要加回这里。
 */
export type NiuLineSet = {
  available: boolean;
  latest: Record<string, number | null>;
  /** 线的展示元数据：**只有 key/label**（后端给出，前端不硬编码线名） */
  lines: { key: string; label: string }[];
  points: NiuLinePoint[];
};

export type IntradayDailySnapshot = {
  available: boolean;
  code: string; name: string;
  trade_date: string; generated_at: string;
  bars: IntradayDailyBar[];
  anchors: IntradayVolumeAnchor[];
  active_anchor: IntradayVolumeAnchor | null;
  pattern: IntradayVpPattern | null;
  position: IntradayPosition | null;
  cost_lines: IntradayCostLine[];
  buy_signals: IntradayDailySignal[];
  sell_signals: IntradayDailySignal[];
  /** 最近 30 个交易日的买卖标记（逐bar因果回放，无未来函数） */
  signal_history: IntradayDailySignalMark[];
  protective: IntradayProtective | null;
  ma: Record<string, number | null>;
  discipline: string[];
  verdict: string;
  /** 擒牛线档位线（日K做T 主图；原蜡烛K线已按用户要求下线） */
  niuline: NiuLineSet | null;
  health: IntradayHealth;
  config_snapshot: Record<string, unknown>;
  disclaimer: string;
};


/** `request` 的扩展 init：`retrySafe` 表示"这次请求重复执行一次也无副作用"。
 *
 * 只有**调用方知道**这件事，所以必须显式声明，不能猜：
 * "创建自选池"重放一次会多一个池，"把套餐改成 vip"重放一次毫无变化。
 */
type RetryInit = RequestInit & { retrySafe?: boolean };

/** 幂等键（`X-Idempotency-Key`）。
 *
 * 服务端认这个键的接口会做去重（`src/core/idempotency.py`），
 * 不认的接口只是忽略这个头 —— 所以可以无条件带上。
 */
function newIdempotencyKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const buf = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") c.getRandomValues(buf);
  else for (let i = 0; i < buf.length; i += 1) buf[i] = Math.floor(Math.random() * 256);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

/** 极轻存活探针：0 I/O，专用于判断"后端进程还在吗"。
 *
 * ⚠️ **不要改成 `/api/v1/health`**。那个聚合健康检查会连 Ollama（2s 超时）、
 * 校验审计链、聚合数据源健康度，正常也要 0.3~2.5 秒。拿它当探针，
 * 服务只是"忙"就会被判成"死"，于是对着一个好好的后端弹"无法连接服务器"。
 *
 * ## 为什么要合并并发 + 极短缓存（2026-09-26 用户报障）
 *
 * 这个探针**默认要等满 3 秒才返回 false**。而网络一抖动时它会被同时
 * 触发很多次：每个失败的请求都来问一次、`ServerStatusBanner` 的 15 秒
 * 轮询也在问。实测日志里能看到 `health/live` 连发七八次 —— 每一次都是
 * 一个可能要等 3 秒的请求，叠起来就是用户看到的"正在检查登录状态"停十秒。
 *
 * 探针问的是一个**全局、单一**的事实（"后端进程还在吗"），所以：
 *   - 并发调用**共享同一个 in-flight 请求**（不是发 N 个）；
 *   - 结果**缓存 2.5 秒** —— 同一秒内问十遍和问一遍是同一个问题。
 *
 * 另外：浏览器已经明确说断网（`navigator.onLine === false`）时直接返回
 * false，**不发请求**。这是唯一在离线时 100% 正确的短路。
 */
const PING_TTL_MS = 2500;
let pingInFlight: Promise<boolean> | null = null;
let pingCached: { at: number; ok: boolean } | null = null;

export async function pingServer(timeoutMs = 3000): Promise<boolean> {
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    return false;
  }
  if (pingCached && performance.now() - pingCached.at < PING_TTL_MS) {
    return pingCached.ok;
  }
  if (pingInFlight) return pingInFlight;

  pingInFlight = (async () => {
    let ok = false;
    try {
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(), timeoutMs);
      const r = await fetch("/api/v1/health/live",
                            { signal: ac.signal, cache: "no-store" });
      clearTimeout(timer);
      ok = r.ok;
    } catch {
      ok = false;
    } finally {
      pingInFlight = null;
      pingCached = { at: performance.now(), ok };
    }
    return ok;
  })();
  return pingInFlight;
}

/** 网络层失败 → 用户可读错误。内部细节（运维指令）只在 localhost 出现，
 * 公网用户只见"服务暂时不可用/请联系管理员"（见 errors.ts）。 */
async function networkError(url: string): Promise<ApiError> {
  // 浏览器已知离线：不必花 3 秒去证实一件已经确定的事。
  // （`pingServer` 内部也会短路，这里只是把话说得更直白。）
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    return makeNetworkError(url, false);
  }
  const alive = await pingServer();
  return makeNetworkError(url, alive);
}

async function request<T>(url: string, init?: RetryInit): Promise<T> {
  // `retrySafe` 是本模块自己的标记，不能传给 `fetch`（会变成未知字段）。
  const { retrySafe, ...rest } = init ?? {};

  const method = (rest.method ?? "GET").toUpperCase();
  const idempotent = method === "GET" || method === "HEAD" || method === "OPTIONS";
  // 幂等方法重试天然安全；写操作只有在调用方声明 retrySafe（服务端有唯一
  // 约束或幂等键兜底）时才重试 —— 否则"响应丢了"会被重试成"多建了一个"。
  const mayRetry = retrySafe === true || (idempotent && retrySafe !== false);

  // ★★ `headers` 必须**合并**，不能整体覆盖 —— 这是实测踩到的 422 bug。
  //
  // `fetch` 的 `headers` 是一个整体对象：后写的会把先写的**整个替换掉**，
  // 而不是逐键合并。原来写成"先铺 Content-Type，再展开 init"，于是只要
  // 调用方传了任何 headers（哪怕只是 `X-CSRF-Token`、甚至空对象 `{}`），
  // `Content-Type: application/json` 就被抹掉 —— 浏览器于是按**纯文本**
  // 发送 body，FastAPI 收到的 `body` 是字符串而不是对象，直接 422：
  //   Input should be a valid dictionary or object to extract fields from
  //   且 input 里显示的是 `{"account":"admin",...` 这个**字符串本身**。
  //
  // 症状之所以绕：**只有未登录时第一个请求就炸**（登录前没有 csrf，
  // `mutate` 传的是 `{}`），看起来像后端参数校验问题，会一直往后端方向查。
  // 实际是前端没声明内容类型。
  //
  // 正确写法：先展开调用方的 headers，再让默认头**兜底**
  // （调用方显式指定的优先）。
  const given = (rest.headers ?? {}) as Record<string, string>;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...given,
  };
  if (!idempotent && !headers["X-Idempotency-Key"]) {
    headers["X-Idempotency-Key"] = newIdempotencyKey();
  }

  let resp: Response | undefined;
  // 最多两次：第一次失败后立刻重试一次。
  // 为什么值得重试：后端重启/休眠唤醒后，浏览器**仍然持有一条到旧进程的
  // keep-alive 连接**，写请求打上去会立刻拿到 `ERR_EMPTY_RESPONSE`。
  // 换一条新连接就好了 —— 这正是"再点一次就成功"的原因，那就自动做掉。
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      resp = await fetch(url, {
        // `credentials: "include"` 是认证能在前端生效的前提：会话令牌
        // （`moss_sid`）与"记住我"（`moss_rt`）都是 HttpOnly Cookie，
        // 浏览器只在请求声明带上凭据时才会回发。默认值 `"same-origin"`
        // 在同源下也带，但本应用有两条路径不是"同源直连"：
        //   ① `vite dev`（:5173）经 proxy 转发；② 将来的独立前端域名。
        credentials: "include",
        ...rest,
        headers,
      });
      break;
    } catch (e) {
      if (!(e instanceof TypeError)) throw e;
      if (mayRetry && attempt === 0) {
        await sleep(400);
        continue;
      }
      throw await networkError(url);
    }
  }
  if (!resp) throw await networkError(url);

  if (!resp.ok) {
    if (resp.status === 401) notifyUnauthorized(url);
    // 统一走报错码契约：后端 envelope → ApiError；**不再透传响应体原文**
    const bodyText = await resp.text();
    throw apiErrorFromResponse(resp.status, bodyText);
  }
  return resp.json() as Promise<T>;
}


/** 读 CSRF Cookie（**非 HttpOnly**，由服务端下发、前端回填到请求头）。
 *
 * 为什么需要它：`moss_sid` 是 HttpOnly，跨站请求会**自动带上**它 ——
 * 这正是 CSRF 的成因。服务端因此同时下发一个前端可读的随机串，
 * 要求写操作把它放进请求头：攻击者的站点读不到这个 Cookie（同源策略），
 * 也就伪造不出这个头。
 */
export function readCsrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)moss_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

/** 带 CSRF 头的 POST（需要改状态时用这个）。
 *
 * `retrySafe` 只在"重复执行没有副作用"时传 true：接口是**设置型**
 * （把套餐设成 vip、把状态设成 disabled），或服务端已按幂等键去重。
 * 创建型接口（开会话、建自选池）不要传 —— 重试会多建一个。
 */
async function mutate<T>(url: string, body?: unknown,
                        retrySafe = false): Promise<T> {
  const csrf = readCsrfToken();
  // ⚠️ 只传 init（不要重复传 headers）：`request` 内部先铺 headers 再展开 init，
  // 若这里也传 headers，`Content-Type` 会被覆盖掉。
  return request<T>(url, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: csrf ? { "X-CSRF-Token": csrf } : {},
    retrySafe,
  });
}

/** 带 CSRF 头的 DELETE（删除天然幂等 → 允许自动重试一次）。 */
async function remove<T>(url: string): Promise<T> {
  const csrf = readCsrfToken();
  return request<T>(url, {
    method: "DELETE",
    headers: csrf ? { "X-CSRF-Token": csrf } : {},
    retrySafe: true,
  });
}

// ======================================================================
// 认证 API（对应后端 `src/api/routes/auth.py` 的 12 个端点）
// ======================================================================

/** 后端外发的用户字段（**不含**哈希/令牌/明文联系方式）。 */
export type AuthUser = {
  user_id: string;
  username: string;
  display_name: string;
  /** active | pending | disabled | expired */
  status: string;
  applied_tier: string;
  valid_until: string;
};

/** 登录结果。令牌**不在**响应体里 —— 它们只走 HttpOnly Cookie。 */
export type LoginResult = {
  ok: boolean;
  message: string;
  user: AuthUser;
  must_change_password: boolean;
};

/** `/auth/me` 的返回：身份 + **脱敏**联系方式。
 *
 * 联系方式是后端 `AuthService.contacts()` 的**分组**形状
 * （按 email/phone 分桶，值只有掩码），不是一个扁平数组 ——
 * 早先这里写成了 `{channel,value,verified}[]`，与接口实际返回不符。
 */
export type MeContacts = {
  email: Array<{ masked: string; verified: boolean; primary: boolean }>;
  phone: Array<{ masked: string; verified: boolean; primary: boolean }>;
};

export type MeResult = {
  user_id: string;
  username: string;
  display_name: string;
  status: string;
  applied_tier: string;
  valid_until: string;
  session_id: string;
  contacts: MeContacts;
};

export type SessionInfo = {
  session_id: string;
  current: boolean;
  device_label: string;
  ip: string;
  created_at: string;
  last_seen_at: string;
  valid: boolean;
};

/** `/auth/login-mode` 的返回：本 IP 登录要不要图形码。 */
export type LoginMode = {
  require_captcha: boolean;
  allowed: boolean;
  reason: string;
  retry_after: number;
};

/** `/auth/bootstrap` 的返回：**一次往返**答完开机探测需要的全部信息。
 *
 * `me` / `user` 由同一份服务端载荷派生（`user` 只是 `me` 的子集字段），
 * 所以两者不会出现"头像有名字、资料页没名字"这种不一致。
 */
export type BootstrapResult = MeResult & {
  authenticated: boolean;
  /** 本次是否由"记住我"换来了新会话（用于埋点/诊断）。 */
  renewed: boolean;
  user: AuthUser | Record<string, never>;
  login_mode: LoginMode;
};

/** 图形验证码挑战（图片 + 一次性令牌）。 */
export type CaptchaChallenge = {
  captcha_token: string;
  /** `data:image/png;base64,...` —— 直接给 `<img src>`，不额外开接口取图。 */
  image_png: string;
  expires_in: number;
  meta: {
    length: number;
    width: number;
    height: number;
    hint: string;
    alphabet_note: string;
  };
  /** 仅 dev/test 返回（生产恒为空）：本地调试不必费劲看图。 */
  debug_answer: string;
};

export const authApi = {
  /** 领一个图形验证码（图片 + 一次性令牌；3 分钟有效、用完即废）。 */
  captcha: () => request<CaptchaChallenge>("/api/v1/auth/captcha"),

  /** 当前 IP 登录**是否需要**图形码（据此决定是否渲染，避免白填一次表单）。 */
  loginMode: () =>
    request<{ require_captcha: boolean; allowed: boolean;
              reason: string; retry_after: number }>(
      "/api/v1/auth/login-mode"),

  /** 发邮箱验证码。`scene`：register | reset_password。 */
  verifyCode: (body: {
    scene: string; email: string;
    captcha_token: string; captcha_answer: string;
  }) => mutate<{ ok: boolean; message: string; code?: string }>(
    "/api/v1/auth/verify-code", body),

  /** 注册（成功后状态为 pending，**需管理员审批才能登录**）。 */
  register: (body: {
    email: string; username: string; password: string;
    code: string; captcha_token: string; captcha_answer: string;
  }) => mutate<{ ok: boolean; message: string; code?: string }>(
    "/api/v1/auth/register", body),

  /** 登录。成功时服务端 Set-Cookie 下发三层 Cookie。
   *
   * `captcha_*` 只在**该 IP 已被要求图形码**时才需要填 ——
   * 正常用户第一次登录不需要（见后端 `login` 的分层防护说明）。
   */
  login: (body: {
    account: string; password: string; remember_me: boolean;
    captcha_token?: string; captcha_answer?: string;
  }) => mutate<LoginResult>("/api/v1/auth/login", body),

  /** 用"记住我"Cookie 静默换新会话（关掉浏览器再打开免密）。 */
  refresh: () => mutate<{ ok: boolean; user: AuthUser }>(
    "/api/v1/auth/refresh"),

  /**
   * **开机探测（首选）**：一次往返回答"我是谁 + 要不要图形码"。
   *
   * 为什么不用 `/me` + `/refresh` + `/me` 那条老链：每一次往返在公网
   * 都要 0.4~2 秒（实测，还会偶发 502），三次串行就是十秒级的
   * "正在检查登录状态"。服务端在这里已经做完"会话优先、否则静默续期"，
   * 客户端只需要读一个布尔值。
   *
   * `authenticated:false` 是**正常结论**（该登录了），不是错误 ——
   * 所以它不抛异常，调用方别用 try/catch 判登录态。
   */
  bootstrap: () => request<BootstrapResult>(
    "/api/v1/auth/bootstrap", { retrySafe: true }),

  logout: (allDevices = false) => mutate<{ ok: boolean; message: string }>(
    `/api/v1/auth/logout?all_devices=${allDevices ? "true" : "false"}`),

  me: () => request<MeResult>("/api/v1/auth/me"),

  forgot: (body: { email: string; captcha_token: string }) =>
    mutate<{ ok: boolean; message: string }>(
      "/api/v1/auth/password/forgot", body),

  reset: (body: {
    email: string; code: string; token: string; new_password: string;
  }) => mutate<{ ok: boolean; message: string }>(
    "/api/v1/auth/password/reset", body),

  changePassword: (body: {
    old_password: string; new_password: string;
  }) => mutate<{ ok: boolean; message: string }>(
    "/api/v1/auth/password/change", body),

  sessions: () => request<{ sessions: SessionInfo[] }>("/api/v1/auth/sessions"),

  killSession: (sessionId: string) =>
    remove<{ ok: boolean; message: string }>(
      `/api/v1/auth/sessions/${encodeURIComponent(sessionId)}`),
};

// ======================================================================
// 管理员控制台 API（对应后端 `src/api/routes/admin.py`）
// ======================================================================

/** 管理台视角的用户（比 `AuthUser` 多出到期判定、在线设备数等）。 */
export type AdminUser = {
  user_id: string;
  username: string;
  display_name: string;
  status: string;
  tier: string;
  valid_until: string;
  valid_from: string;
  created_at: string;
  reviewed_by: string;
  reviewed_at: string;
  review_note: string;
  expired: boolean;
  can_login: boolean;
  login_block_reason: string;
  active_sessions: number;
};

export type AdminOverview = {
  counts: Record<string, number>;
  pending_count: number;
  pending: AdminUser[];
  tiers: string[];
  statuses: string[];
  /** **这个管理台管的是哪个实例的账号库**（env + 库路径）。
   *
   * 为什么必须下发：2026-09-23 实测踩到 —— 客户在**试点实例**（8110）注册并
   * 提示"等待管理员审批"，而管理员打开的是**调试实例**（8100）的用户管理。
   * 两边账号库是分开的，于是"看不到申请记录"，看起来像注册没落库，
   * 排查方向被完全带偏。把 env + 库路径摆在标题旁边，看错实例当场可见。 */
  instance?: { env: string; db: string };
};

export type AdminUserDetail = {
  user: AdminUser;
  contacts: Array<{ kind: string; masked: string; verified: boolean;
                    is_primary: boolean }>;
  sessions: Array<{ session_id: string; device_label: string; ip: string;
                    created_at: string; last_seen_at: string; valid: boolean }>;
  reviews: Array<{ action: string; from_status: string; to_status: string;
                   reviewer_id: string; note: string; tier_code: string;
                   valid_until: string; created_at: string }>;
};
export type StockProfile = {
  code: string;
  mode: string;
  name: string;
  weights: Record<string, number>;
  thresholds: Record<string, number>;
  levels: Record<string, number>;
  boards: string[];
  overseas: string[];
  from_user: boolean;
  source: string;
  /** 口径指纹：只由 mode/权重/阈值/档位决定（相同则计算可共享） */
  caliber_key: string;
  updated_at: string;
  /** 后端给出的"这条口径从哪来"说明，前端直接显示 */
  explain?: string;
};

export const profileApi = {
  list: (mode = "") =>
    request<{ profiles: StockProfile[]; total: number }>(
      `/api/v1/me/profiles${mode ? `?mode=${mode}` : ""}`),

  get: (code: string, mode = "intraday") =>
    request<StockProfile>(
      `/api/v1/me/profiles/${encodeURIComponent(code)}?mode=${mode}`),

  save: (code: string, body: {
    mode: string;
    weights: Record<string, number>;
    thresholds: Record<string, number>;
    levels: Record<string, number>;
    boards: string[];
    overseas: string[];
  }) => mutate<{ ok: boolean; message: string; profile: StockProfile }>(
    `/api/v1/me/profiles/${encodeURIComponent(code)}`, body),

  /** 还原系统默认（= 删掉我自己的那份，回退到模板/内置默认）。 */
  reset: (code: string, mode = "intraday") =>
    remove<{ ok: boolean; removed: boolean; message: string;
             profile?: StockProfile }>(
      `/api/v1/me/profiles/${encodeURIComponent(code)}?mode=${mode}`),
};

/** 一个等级的完整套餐（资源上限 + 功能权限 + 每项定价）。
 *
 * ⚠️ **没有 `monthly_price`**（套餐月费）：该字段已下线（用户口径 2026-09-23，
 * 定价不由本系统维护）。服务端 `TierPlan` 与 `configs/platform_tiers.json`
 * 里也已一并移除 —— 前端类型保留一个后端不再返回的字段，会让"这个值到底
 * 谁在用"永远说不清。
 */
export type TierPlan = {
  key: string;
  label: string;
  sellable: boolean;
  resources: Record<string, number>;
  features: Record<string, boolean>;
  pricing: Record<string, number>;
  note: string;
};

export type TierConfigPayload = {
  tiers: TierPlan[];
  /** 功能键 → 中文名（前端渲染表单用，不硬编码） */
  features: Record<string, string>;
  /** 资源键 → 中文名 */
  resources: Record<string, string>;
  config_path: string;
};

/** 我的可见页签与额度（前端照着 `visible_views` 渲染页签）。 */
export type MyFeatures = {
  user_id: string;
  tier: string;
  tier_label: string;
  features: Record<string, boolean>;
  feature_labels: Record<string, string>;
  visible_views: string[];
  /**
   * 管理员专属页签（目前只有 `scheduler`）：非管理员**永远不在**
   * `visible_views` 里。前端拿它做说明文案，而不是自己再判一遍权限。
   */
  admin_only_views?: string[];
  /** 当前会话是不是管理员（服务端判据，不用前端自己比对 tier 字面量）。 */
  is_admin?: boolean;
  quant_views: Record<string, boolean>;
  resources: Record<string, number>;
  pricing: Record<string, number>;
  note: string;
};

/** 一个租户的"用掉多少 / 上限多少 / 还剩多少"（目标口径：展示剩余配额）。 */
export type TenantUsage = {
  calls_today: number;
  calls_today_limit: number;
  calls_today_remaining: number;
  calls_today_used_pct: number | null;
  tokens_month: number;
  tokens_month_limit: number;
  tokens_month_remaining: number;
  tokens_month_used_pct: number | null;
  /** false = **还没量到**（不是"用掉 0"）。两者对决策的含义相反，界面必须区分。 */
  tokens_measured: boolean;
};

export type MonitorTenant = {
  tenant_id: string;
  label: string;
  /** 该租户是否对应一个套餐等级（false = 匿名/平台自身流量，没有额度）。 */
  known_tier: boolean;
  calls: number;
  errors: number;
  error_rate: number;
  users: number;
  latency_ms: { avg: number; p50: number; p95: number; max: number };
  top_paths: Array<[string, number]>;
  limits: Record<string, number>;
  llm_tokens: number;
  /** 本月 LLM 费用（元）。与 `usage.tokens_month` 同一份审计、同一次读取。 */
  llm_cost: {
    cny: number;
    calls: number;
    tokens: number;
    /** 占本月总费用的比例（%） */
    share_pct: number;
    /** false = **还没量到**（不是"花了 0 元"）。 */
    measured: boolean;
  };
  usage: TenantUsage;
};

/** 一个功能的 LLM 费用（前端页面 / 后台作业 / 命令行脚本）。 */
export type MonitorCostFeature = {
  key: string;
  label: string;
  cny: number;
  calls: number;
  tokens: number;
  /** 占本月总费用的比例（%） */
  share_pct: number;
  /** 是不是**前端页面**（false = 平台自身开销：定时作业/脚本/管理台）。 */
  page_feature: boolean;
  /**
   * 归属依据：`page` 按请求路径（精确）/ `agent` 按 Agent 名（推断）/
   * `mixed` 两者都有 / `unknown` 判不出来。
   * 界面必须显示它 —— "精确"与"推断"对账目的可信度完全不同。
   */
  basis: "page" | "agent" | "mixed" | "unknown";
};

export type MonitorCostTenant = {
  tenant_id: string;
  cny: number;
  calls: number;
  tokens: number;
  users: number;
  /** 归属来源：session（会话）/ principal（令牌）/ unknown（老行）。 */
  sources: string[];
  share_pct: number;
};

/** LLM 费用汇总（运维页「花费」区的全部数据）。 */
export type MonitorCost = {
  /** 本窗口（本月）总费用，元。 */
  total_cny: number;
  today_cny: number;
  calls: number;
  calls_today: number;
  tokens_in: number;
  tokens_out: number;
  paid_calls: number;
  free_calls: number;
  unknown_provider_calls: number;
  /** 用了**未登记价格**的模型名的调用次数（金额按兜底价估算）。 */
  unpriced_calls: number;
  unpriced_models: Array<[string, number]>;
  prices_loaded: string[];
  fallback_price: { input_cache_miss: number; output: number };
  by_feature: MonitorCostFeature[];
  by_tenant: MonitorCostTenant[];
  by_day: Array<{ day: string; cny: number; calls: number }>;
  window_label: string;
  since: string;
  /** false = 读不到审计：**不能**显示成"0 元"。 */
  readable?: boolean;
  basis_notes: string[];
};

export type MonitorPayload = {
  window_minutes: number;
  sampled_calls: number;
  audit_file: string;
  overall: { calls: number; errors: number; avg_ms: number; p95_ms: number };
  by_tenant: MonitorTenant[];
  by_path: Array<{ path: string; calls: number; errors: number;
                  avg_ms: number; p95_ms: number }>;
  /** LLM 费用：汇总 + 按功能 + 按租户 + 按天趋势。 */
  llm_cost: MonitorCost;
  /** 配额口径与局限（直接渲染，不让人猜数字怎么来的）。 */
  quota_basis: {
    day_start: string;
    month_start: string;
    audit_file: string;
    audit_lines: number;
    audit_truncated: boolean;
    tokens_truncated: boolean;
    notes: string[];
  };
  data_sources: Array<{ name: string; kind: string; available: boolean;
                        detail: string }>;
  data_sources_note: string;
};

export const platformApi = {
  myFeatures: () => request<MyFeatures>("/api/v1/me/features"),

  tiers: () => request<TierConfigPayload>("/api/v1/admin/platform/tiers"),

  /** 改某个等级的资源/功能/标签。**不含价格**（见 `TierPlan` 的说明）。 */
  updateTier: (tier: string, patch: {
    label?: string; sellable?: boolean; note?: string;
    resources?: Record<string, number>;
    features?: Record<string, boolean>;
    pricing?: Record<string, number>;
  }) => request<{ ok: boolean; message: string; tier: TierPlan }>(
    `/api/v1/admin/platform/tiers/${encodeURIComponent(tier)}`, {
      method: "PUT", body: JSON.stringify(patch), retrySafe: true,
    }),

  matrix: () => request<{
    rows: Array<{ feature: string; label: string;
                  tiers: Record<string, { enabled: boolean; price: number }> }>;
    tiers: Array<{ key: string; label: string; sellable: boolean }>;
  }>("/api/v1/admin/platform/permissions-matrix"),

  monitor: (minutes = 60) =>
    request<MonitorPayload>(
      `/api/v1/admin/platform/monitor?minutes=${minutes}`),
};

export const adminApi = {
  overview: () => request<AdminOverview>("/api/v1/admin/overview"),

  users: (params: { status?: string; keyword?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.keyword) q.set("keyword", params.keyword);
    const suffix = q.toString() ? `?${q}` : "";
    return request<{ users: AdminUser[]; total: number }>(
      `/api/v1/admin/users${suffix}`);
  },

  user: (userId: string) =>
    request<AdminUserDetail>(`/api/v1/admin/users/${encodeURIComponent(userId)}`),

  approve: (userId: string, body: {
    tier: string; days: number; display_name?: string; note?: string;
  }) => mutate<{ ok: boolean; message: string; user: AdminUser }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/approve`, body, true),

  reject: (userId: string, note: string) =>
    mutate<{ ok: boolean; message: string; user: AdminUser }>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}/reject`
      + `?note=${encodeURIComponent(note)}`, undefined, true),

  /** 改状态 / 套餐 / 有效期（只传要改的字段）。设置型 → 可安全重试。 */
  updateUser: (userId: string, body: {
    status?: string; tier?: string; days?: number;
    valid_until?: string; display_name?: string; note?: string;
  }) => request<{ ok: boolean; message: string; user: AdminUser }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
      retrySafe: true,
    }),

  /** 直接开号。
   *
   * `retrySafe: true` 的依据**不在前端**，而在服务端：这个接口实现
   * `X-Idempotency-Key`（`src/core/idempotency.py`），重试同一个键只会
   * 回放上次结果，不会开出第二个账号。所以这里敢让 `request` 自动重试。
   */
  createUser: (body: {
    username: string; email?: string; password: string;
    tier: string; days: number; display_name?: string;
    must_change_password?: boolean;
  }) => mutate<{ ok: boolean; message: string; user: AdminUser;
                initial_password: string }>("/api/v1/admin/users", body, true),

  deleteUser: (userId: string, note = "") =>
    remove<{ ok: boolean; message: string; revoked_sessions: number }>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}`
      + `?note=${encodeURIComponent(note)}`),

  resetPassword: (userId: string, newPassword: string,
                  mustChange = true) =>
    mutate<{ ok: boolean; message: string }>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}/reset-password`,
      { new_password: newPassword, must_change_password: mustChange }, true),

  kickAll: (userId: string) =>
    remove<{ ok: boolean; message: string; revoked_sessions: number }>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}/sessions`),

  reviews: (limit = 100) =>
    request<{ reviews: Array<{
      user_id: string; action: string; from_status: string; to_status: string;
      reviewer_id: string; note: string; tier_code: string;
      valid_until: string; created_at: string;
    }> }>(`/api/v1/admin/reviews?limit=${limit}`),
};

export type BacktestJobStarted = {
  job_id: string;
  status: "running";
  stage: string;
  stage_label: string;
  estimated_wait_seconds: number;
  poll_interval_seconds: number;
  message: string;
};

export type BacktestJobStatus = {
  job_id: string;
  status: "running" | "done" | "error";
  stage: string;
  stage_label: string;
  elapsed_seconds: number;
  estimated_wait_seconds: number;
  poll_interval_seconds: number;
  message?: string;
  result?: BacktestResponse;
  error?: string;
  error_code?: number;
};

export const api = {
  submit: (body: {
    query: string;
    analysis_type: string;
    target: string;
  }) =>
    request<{ task_id: string; status: string; plan: string[] }>(
      "/api/v1/research/analyze",
      { method: "POST", body: JSON.stringify(body) }
    ),
  task: (taskId: string) =>
    request<TaskDetail>(`/api/v1/research/${taskId}`),
  cancelTask: (taskId: string) =>
    request<{ task_id: string; status: string; message: string }>(
      `/api/v1/research/${encodeURIComponent(taskId)}/cancel`,
      { method: "POST" }
    ),
  trace: (taskId: string) => request<TraceDetail>(`/api/v1/trace/${taskId}`),
  report: (taskId: string) =>
    request<{ report: string; disclaimer: string }>(
      "/api/v1/report/generate",
      { method: "POST", body: JSON.stringify({ task_id: taskId }) }
    ),
  health: () => request<Health>("/api/v1/health"),
  /** 数据源健康度（能力矩阵 + 实测延迟 + Tushare 覆盖）。 */
  dataHealth: async (): Promise<DataHealth> => {
    const payload = await request<Health>("/api/v1/health");
    if (!payload.data_sources.health) {
      throw new Error("服务端未返回数据源健康度");
    }
    return payload.data_sources.health;
  },
  /** 大盘量能（**独立小接口**）：前端可单独轮询，不依赖个股快照。
   *  取不到时返回 `available:false` + `reason`（如"数据源熔断冷却"）。 */
  marketTurnover: () => request<{
    available: boolean; text: string; reason: string;
    t_allowed?: boolean; chase_allowed?: boolean; reasons?: string[];
    turnover: Record<string, unknown>;
  }>("/api/v1/intraday/market-turnover"),
  schedulerJobs: () =>
    request<{ jobs: SchedulerJob[] }>("/api/v1/scheduler/jobs"),
  schedulerTrigger: (name: string) =>
    request<{ run: RunRecord }>(
      `/api/v1/scheduler/jobs/${encodeURIComponent(name)}/run`,
      { method: "POST" }
    ),
  schedulerRuns: (limit = 50) =>
    request<{ runs: RunRecord[] }>(
      `/api/v1/scheduler/runs?limit=${limit}`
    ),
  schedulerSummary: () =>
    request<DailySummary>("/api/v1/scheduler/runs/summary"),
  llmMetrics: (limit = 1000) =>
    request<{ limit: number; slow_call_threshold_ms: number;
              metrics: LlmMetrics }>(`/api/v1/metrics?limit=${limit}`),
  backtestRun: (body: BacktestRequest) =>
    request<BacktestJobStarted>("/api/v1/backtest/run", {
      method: "POST", body: JSON.stringify(body),
    }),
  backtestJob: (jobId: string) =>
    request<BacktestJobStatus>(
      `/api/v1/backtest/jobs/${encodeURIComponent(jobId)}`),
  alerts: (params: Record<string, string | number | undefined | boolean> = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "") qs.set(k, String(v));
    });
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<{ alerts: Alert[]; total: number; unread: number }>(
      `/api/v1/alerts${suffix}`);
  },
  alertDetail: (alertId: string) =>
    request<{ alert: Alert }>(`/api/v1/alerts/${encodeURIComponent(alertId)}`),
  markAlertRead: (alertId: string) =>
    request<{ alert_id: string; status: string }>(
      `/api/v1/alerts/${encodeURIComponent(alertId)}/read`, { method: "POST" }),
  markAllAlertsRead: () =>
    request<{ updated: number }>("/api/v1/alerts/read-all", { method: "POST" }),
  unreadCount: () =>
    request<{ unread: number }>("/api/v1/alerts/unread-count"),
  events: (params: { type?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.type) qs.set("type", params.type);
    if (params.limit) qs.set("limit", String(params.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<{ events: MarketEvent[]; total: number }>(
      `/api/v1/events${suffix}`);
  },
  importEvents: (events: { title: string; content?: string;
                           event_type?: string; source_name?: string;
                           publish_time?: string }[]) =>
    request<{ received: number; inserted: number; skipped: number;
              event_ids: string[] }>("/api/v1/events/import", {
      method: "POST", body: JSON.stringify({ events }),
    }),
  triggerScan: () =>
    request<{ status: string; started_at: string; poll: string }>(
      "/api/v1/alerts/scan", { method: "POST" }),
  scanLatest: () => request<ScanState>("/api/v1/alerts/scan/latest"),
  alertSettings: () => request<AlertSettings>("/api/v1/alerts/settings"),

  /**
   * **一次往返**取齐事件告警首屏要的全部东西（列表 + 设置）。
   *
   * 原来首屏发两条（`/alerts` + `/alerts/settings`）。本机无所谓，
   * 但隧道实测只有 ~51 KB/s、单次往返 0.4~2 秒 —— 每少一条就少一次
   * 往返与队头等待。用于"登录成功就预加载"（见 `alertsCache.preloadAlerts`）。
   */
  alertsBootstrap: (params: { limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<{ alerts: Alert[]; total: number; unread: number;
                     settings: AlertSettings }>(
      `/api/v1/alerts/bootstrap${suffix}`, { retrySafe: true });
  },

  // ---- 做T辅助 ----
  /**
   * 单标的快照。`light=true` 只取「分时图 + 行情 + 关键价位 + 总分/信号」，
   * 跳过消息面 LLM/估值/板块分时/大盘/海外映射 —— 用于两段式首屏：
   * 冷标的一次完整快照实测 4.5~7 秒，而用户点开一只票第一眼看的是分时图。
   */
  intradaySnapshot: (code: string, refresh = false, light = false) =>
    request<IntradaySnapshot>(
      `/api/v1/intraday/snapshot?code=${encodeURIComponent(code)}` +
      (refresh ? "&refresh=true" : "") +
      (light ? "&light=true" : "")),
  /**
   * 自选标的概览。
   *
   * 必须显式传 `limit`：后端默认值是 **20**，省略时自选超过 20 只就会出现
   * "新加的股票不在列表里"（实测：自选已 39 只，界面只显示前 20 只，
   * 用户以为没写入成功）。
   *
   * `active`（当前查看的标的）**只在 force=true 时有意义**：后端会把整表重算
   * 收敛成"只重算这一只 + 其余走缓存 + 后台补齐"。自选 30+ 只时这是
   * "强制刷新要等 6~7 秒"的根治手段（实测每只票的板块概念快照要 3.2~6.5s，
   * 39 只就是几十个子进程抢 CPU）。
   */
  intradayWatchlist: (force = false, limit = 50, active = "") =>
    request<IntradayWatchPayload>(
      `/api/v1/intraday/watchlist?limit=${limit}`
      + (force ? "&force=true" : "")
      + (force && active ? `&active=${encodeURIComponent(active)}` : "")),

  /** **按当前用户**解析的自选概览（多用户路径）。
   *
   * 数据来源：`configs/intraday.yaml`（**全系统一份**）。
   *
   * ⚠️ 自选池功能已删除（用户口径 2026-09-23）：原先还有一条
   * `intradayWatchlistMine`（取"当前用户自己的池"），随功能一起移除。
   *
   * - 本方法**没有 `force`/`active` 参数**：后端那条路径是"按用户短 TTL 缓存 +
   *   按需计算"，没有共享那份的后台刷新循环，传 force 也没有对应语义
   *   （假装支持会让人以为"强制刷新生效了"，而实际上没有）。
   * - 它**不参与 WS 推送**：推送仍走共享那份（见设计 §7.2 的共享计算层，
   *   那是让"100 人"成立的下一步）。
   */
  intradayPinWatch: (code: string, pinned = true) =>
    request<{ ok: boolean; code: string; pinned: boolean;
              watchlist: Record<string, unknown>[] }>(
      `/api/v1/intraday/watchlist/pin?code=${encodeURIComponent(code)}`
      + `&pinned=${pinned ? "true" : "false"}`, { method: "POST" }),
  intradayAddWatch: (body: {
    code: string; name?: string; boards?: string[];
    peers?: string[]; industry?: string; overseas?: string[];
  }) =>
    request<{ ok: boolean; code: string; name: string;
              boards: string[]; boards_bound: boolean;
              overseas: string[];
              watchlist: IntradayWatchConfigItem[] }>(
      "/api/v1/intraday/watchlist", {
        method: "POST", body: JSON.stringify(body),
      }),
  intradayRemoveWatch: (code: string) =>
    request<{ ok: boolean; code: string;
              watchlist: IntradayWatchConfigItem[] }>(
      `/api/v1/intraday/watchlist/${encodeURIComponent(code)}`,
      { method: "DELETE" }),
  intradayConfig: () => request<IntradayConfigPayload>("/api/v1/intraday/config"),
  /** 因子目录 + 权重模板（可选带 code 拿到该票当前生效权重与覆盖来源）。 */
  intradayFactors: (mode: IntradayMode = "intraday", code?: string) => {
    const query = new URLSearchParams({ mode });
    if (code) query.set("code", code);
    return request<IntradayFactorCatalog>(
      `/api/v1/intraday/factors?${query.toString()}`);
  },
  /** 个股股性画像（预填权重/档位的来源）。 */
  intradayCharacter: (code: string, mode: IntradayMode = "intraday",
                      refresh = false) =>
    request<CharacterProfile>(
      `/api/v1/intraday/character?code=${encodeURIComponent(code)}`
      + `&mode=${mode}` + (refresh ? "&refresh=true" : "")),
  /** 市场情绪周期（涨停家数/炸板率/连板高度 → 阶段与做T环境温度）。 */
  intradayMarketCycle: (force = false) =>
    request<MarketCycle>(
      `/api/v1/intraday/market-cycle${force ? "?force=true" : ""}`),
  /** 档位神经网络拟合（refresh=true 忽略缓存重训一次，约 1~5 秒）。 */
  intradayLevelFit: (code: string, refresh = false) =>
    request<IntradayLevelFit>(
      `/api/v1/intraday/level-fit?code=${encodeURIComponent(code)}`
      + (refresh ? "&refresh=true" : "")),
  /**
   * 某只票**已保存**的「关联板块 / 海外映射」绑定（切股/加自选时预填输入框）。
   *
   * 用户口径 2026-09-23：配置存在数据库里，删了自选再加回来要能自动加载。
   * 服务端在保存时也会自己回落到这份绑定，所以**不预填也不会丢** ——
   * 这个接口是为了让用户**看得见、改得动**已存的值。
   */
  intradayStockBinding: (code: string) =>
    request<{ ok: boolean; code: string; boards: string[]; overseas: string[];
              saved: boolean }>(
      `/api/v1/intraday/stock-bindings/${encodeURIComponent(code)}`),
  /** 全部已保存的绑定 —— 给输入框做**联想下拉**（提示"以前给哪些票配过什么"）。 */
  intradayStockBindings: () =>
    request<{ items: { code: string; name: string; boards: string[];
                       overseas: string[] }[]; count: number }>(
      `/api/v1/intraday/stock-bindings`),
  /**
   * **批量**实时快照（只读）：给自定义板块的成分行显示涨跌幅用。
   *
   * 与 `/intraday/watchlist` 的区别是**覆盖面**：那个只来自选池；自定义板块的
   * 成分里没进自选的票，只能靠这个接口拿价格。调用方应按 code 缓存 ——
   * 同一只票可能同时属于多个板块（用户口径 2026-09-23）。
   */
  intradayQuotes: (codes: string[]) =>
    request<{ items: { code: string; name: string; price: number | null;
                       change_pct: number | null }[]; count: number }>(
      `/api/v1/intraday/quotes?codes=${encodeURIComponent(codes.join(","))}`),
  /** 资金流监控快照（榜单 + 已选实体走势；盘中 60 秒缓存，refresh 穿透）。 */
  fundflowSnapshot: (refresh = false, window = 10, top = 20) =>
    request<FlowBoard>(
      `/api/v1/fundflow/snapshot?refresh=${refresh ? "true" : "false"}`
      + `&window=${window}&top=${top}`),
  /** 资金流监控：已加入监控的板块/个股。 */
  fundflowWatch: (kind: "sector" | "stock") =>
    request<{ kind: string; count: number; items: FlowWatchItem[] }>(
      `/api/v1/fundflow/watch?kind=${kind}`),
  /** 加入监控（幂等）。 */
  fundflowAddWatch: (kind: "sector" | "stock", code: string, name = "") =>
    request<{ ok: true; kind: string; code: string; count: number;
              items: FlowWatchItem[]; notice: string }>(
      "/api/v1/fundflow/watch",
      { method: "POST", body: JSON.stringify({ kind, code, name }) }),
  /** 移除监控（幂等）。 */
  fundflowRemoveWatch: (kind: "sector" | "stock", code: string) =>
    request<{ ok: true; removed: boolean; count: number;
              items: FlowWatchItem[]; notice: string }>(
      `/api/v1/fundflow/watch/${kind}/${encodeURIComponent(code)}`,
      { method: "DELETE" }),
  /** 搜索可加入监控的板块（数据源官方名）或个股（仓库名录）。 */
  fundflowSearch: (kind: "sector" | "stock", q = "", limit = 20) =>
    request<{ kind: string; query: string; items: Record<string, unknown>[] }>(
      `/api/v1/fundflow/search?kind=${kind}&q=${encodeURIComponent(q)}`
      + `&limit=${limit}`),

  // ---------------- 量化选股（3 档模型 + 自定义板块） ----------------
  // 选股结果**不自动写自选池**：只落在模块里，用户点「加自选」才写
  // configs/intraday.yaml（`quantSelectAddToWatchlist`）。
  /** 模块状态：模型档位/训练时间、上一轮选股、是否正在跑、板块数。 */
  quantSelectStatus: () =>
    request<QuantSelectStatus>("/api/v1/quant/select/status"),
  /**
   * 手动补训模型（前端「立即训练模型」）。
   *
   * 正常路径不需要它：模型缺失时 `/status` 会**自动**起一次训练；
   * 这个接口是"自动那次失败/等不及"时的重试入口。
   */
  quantSelectTrain: () =>
    request<{ started: boolean; training: boolean; message: string }>(
      "/api/v1/quant/select/train", { method: "POST" }),
  /**
   * 手动跑一轮选股（同步等待；一轮几十秒到几分钟）。
   *
   * `sectorFilter` 是**选股范围**（自定义板块名）：给了就把候选池换成这些板块的
   * 成分，阈值也在板块内部取 —— 而不是"全市场选完再筛"（后者板块一小就选不出票）。
   */
  quantSelectRun: (body: {
    sector_filter?: string[]; top_n?: number | null;
    trade_date?: string | null; max_stocks?: number | null;
  }) =>
    request<QuantSelectionRun>("/api/v1/quant/select/run", {
      method: "POST", body: JSON.stringify(body),
    }),
  /**
   * 选股结果用的**个股消息面**（新闻/公告）。
   *
   * 增强信息：取不到时后端返回空 items + reason，**不会**让选股结果接口失败。
   * 服务端有 TTL 缓存，前端可以放心在结果刷新后调用。
   */
  quantSelectNews: (codes: string[], limit = 3) =>
    request<{ news: Record<string, QuantStockNews>; count: number; reason: string }>(
      `/api/v1/quant/select/news?codes=${encodeURIComponent(codes.join(","))}`
      + `&limit=${limit}`),
  /** 历史选股记录（含明细）。 */
  quantSelectRuns: (limit = 20, window = "") =>
    request<{ runs: QuantSelectionRun[]; count: number }>(
      `/api/v1/quant/select/runs?limit=${limit}`
      + (window ? `&window=${encodeURIComponent(window)}` : "")),
  /** 最新一轮选股结果（没有记录时 `available=false`）。 */
  quantSelectLatest: (window = "") =>
    request<QuantSelectionRun & { available?: boolean; reason?: string }>(
      `/api/v1/quant/select/latest`
      + (window ? `?window=${encodeURIComponent(window)}` : "")),
  /** 把选出的票加进做T自选（人工确认）。 */
  quantSelectAddToWatchlist: (code: string, name = "") =>
    request<{ code: string; added: boolean; marked_runs: number }>(
      "/api/v1/quant/select/add-to-watchlist", {
        method: "POST", body: JSON.stringify({ code, name }),
      }),
  /**
   * 整批加进做T自选（「＋ 全部加自选」）。
   *
   * 与逐只调用 `quantSelectAddToWatchlist` 的区别只在**落盘次数**：
   * 服务端一次写完配置、一次重建做T子组件（逐只循环会让自选概览缓存
   * 被反复作废，随后那次整表重算是整个动作里最贵的一步）。
   */
  quantSelectAddManyToWatchlist: (items: { code: string; name?: string }[]) =>
    request<QuantBatchWatchResult>(
      "/api/v1/quant/select/add-to-watchlist-batch", {
        method: "POST", body: JSON.stringify({ items }),
      }),
  /** 自定义板块列表（含成分股）。 */
  quantSectors: (withMembers = true) =>
    request<{ sectors: QuantSector[]; count: number }>(
      `/api/v1/quant/sectors?with_members=${withMembers ? "true" : "false"}`),
  /** 新建/更新自定义板块（同名即更新，幂等）。 */
  quantSaveSector: (body: {
    name: string; kind?: "manual" | "dynamic"; note?: string;
    rule?: Record<string, unknown>; color?: string; sort_order?: number;
    members?: { code: string; name?: string }[];
  }) =>
    request<QuantSector>("/api/v1/quant/sectors", {
      method: "POST", body: JSON.stringify(body),
    }),
  quantDeleteSector: (sectorId: number) =>
    request<{ deleted: boolean; sector_id: number }>(
      `/api/v1/quant/sectors/${sectorId}`, { method: "DELETE" }),
  /** 增量加成分股（幂等：已在板块里的不算新增，返回实际新增数）。 */
  quantAddSectorMembers: (sectorId: number,
                          members: { code: string; name?: string }[]) =>
    request<{ sector_id: number; added: number }>(
      `/api/v1/quant/sectors/${sectorId}/members`, {
        method: "POST", body: JSON.stringify({ members }),
      }),
  quantRemoveSectorMember: (sectorId: number, code: string) =>
    request<{ removed: boolean; sector_id: number; code: string }>(
      `/api/v1/quant/sectors/${sectorId}/members/${encodeURIComponent(code)}`,
      { method: "DELETE" }),
  /** 权重档案列表（按更新时间倒序）。 */
  intradayWeightProfiles: (limit = 200) =>
    request<IntradayWeightProfileList>(
      `/api/v1/intraday/weight-profiles?limit=${limit}`),
  /** 单只票的档案 + 当前实际生效口径。 */
  intradayWeightProfile: (code: string) =>
    request<IntradayWeightProfileDetail>(
      `/api/v1/intraday/weight-profiles/${encodeURIComponent(code)}`),
  /** 保存/更新权重档案（稀疏差分，写数据库并立即生效）。 */
  intradaySaveWeightProfile: (code: string,
                              body: IntradayWeightProfileRequest) =>
    request<IntradayWeightProfileSaved>(
      `/api/v1/intraday/weight-profiles/${encodeURIComponent(code)}`,
      { method: "PUT", body: JSON.stringify(body) }),
  /** 删除权重档案（幂等；删除后回落到 YAML overrides / 全局口径）。 */
  intradayDeleteWeightProfile: (code: string) =>
    request<{ ok: true; code: string; removed: boolean; notice: string }>(
      `/api/v1/intraday/weight-profiles/${encodeURIComponent(code)}`,
      { method: "DELETE" }),
  /** 用给定权重重算一次总分（不落库、不动缓存；服务端耗时 1~3 秒）。 */
  intradayPreviewWeights: (body: IntradayWeightPreviewRequest) =>
    request<IntradayWeightPreview>(
      "/api/v1/intraday/weight-profiles/preview",
      { method: "POST", body: JSON.stringify(body) }),
  intradayBacktest: (body: { code: string; horizon: number; days: number }) =>
    request<IntradayBacktest>("/api/v1/intraday/backtest", {
      method: "POST", body: JSON.stringify(body),
    }),
  intradayScan: () =>
    request<{ scanned: number; triggered: number;
              items: IntradayWatchItem[]; all: IntradayWatchItem[] }>(
      "/api/v1/intraday/scan", { method: "POST" }),
  intradayDaily: (code: string, refresh = false) =>
    request<IntradayDailySnapshot>(
      `/api/v1/intraday/daily?code=${encodeURIComponent(code)}`
      + (refresh ? "&refresh=true" : "")),

  // ---- 多因子（策略回测 · 多因子模式）----
  quantFactors: () => request<QuantFactorList>("/api/v1/quant/factors"),
  quantDataStatus: () =>
    request<QuantDataStatus>("/api/v1/quant/data-status"),
  quantScreen: (body: QuantScreenRequest) =>
    request<{ job_id: string; status: string }>("/api/v1/quant/screen", {
      method: "POST", body: JSON.stringify(body),
    }),
  quantScreenStatus: (jobId: string) =>
    request<QuantScreenJob>(
      `/api/v1/quant/screen/${encodeURIComponent(jobId)}`),

  // ---- 单股票多因子条件策略回测 ----
  quantSingleBacktest: (body: QuantSingleRequest) =>
    request<{ job_id: string; status: string }>(
      "/api/v1/quant/backtest/single", {
        method: "POST", body: JSON.stringify(body),
      }),
  quantSingleStatus: (jobId: string) =>
    request<QuantSingleJob>(
      `/api/v1/quant/backtest/single/${encodeURIComponent(jobId)}`),
  quantStrategies: (params?: {
    minExcess?: number; onlyWinners?: boolean; code?: string;
  }) => {
    const query = new URLSearchParams();
    if (params?.minExcess) query.set("min_excess", String(params.minExcess));
    if (params?.onlyWinners) query.set("only_winners", "true");
    if (params?.code) query.set("code", params.code);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<QuantStrategyList>(`/api/v1/quant/strategies${suffix}`);
  },
  quantStrategyPresets: () =>
    request<{ count: number; presets: QuantStrategyPreset[]; note: string }>(
      "/api/v1/quant/strategies/presets"),

  /** 操作说明书（内容由后端从代码生成，避免文档与实现漂移） */
  quantHelp: (topic: "single" | "factors") =>
    request<QuantHelp>(`/api/v1/quant/help?topic=${topic}`),

  /** 股票联动联想（代码 / 中文名 / 拼音首字母 / 全拼） */
  stockSearch: (q: string, limit = 20) =>
    request<StockSearchResult>(
      `/api/v1/quant/stocks/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  /**
   * 某只票的**关联概念板块**（按相关性降序）。
   *
   * `auto_default` 是相关性最高的概念名，用于"选中个股后自动关联最相关概念"。
   * 相关性 = 窄度(55%) + 人均主力净额(28%) + 板块涨幅(17%)，
   * 已剔除"同花顺全A""百元股"这类市场级/量化标签概念。
   */
  stockBoards: (code: string, limit = 12) =>
    request<StockBoardsResult>(
      `/api/v1/quant/stocks/${encodeURIComponent(code)}/boards?limit=${limit}`),

  /** 概念板块名联想（「关联板块」输入框用）。 */
  conceptSuggest: (q: string, limit = 12) =>
    request<{ query: string; count: number; items: ConceptSuggestion[]; hint: string }>(
      `/api/v1/quant/concepts/suggest?q=${encodeURIComponent(q)}&limit=${limit}`),

  /** 单个代码的名称与拼音（输入框旁的名称补全） */
  stockDetail: (code: string, autoEnrich = true) =>
    request<{ stock: StockEntry }>(
      `/api/v1/quant/stocks/${encodeURIComponent(code)}` +
      `?auto_enrich=${autoEnrich}`),

  /** 开源策略案例（网络公开分享，未经验证） */
  quantCases: (params?: { theme?: string; source?: string; kind?: string;
                          limit?: number; excludeKinds?: string }) => {
    const query = new URLSearchParams();
    if (params?.theme) query.set("theme", params.theme);
    if (params?.source) query.set("source", params.source);
    if (params?.kind) query.set("kind", params.kind);
    if (params?.limit) query.set("limit", String(params.limit));
    if (params?.excludeKinds !== undefined) {
      query.set("exclude_kinds", params.excludeKinds);
    }
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<QuantCaseList>(`/api/v1/quant/cases${suffix}`);
  },
  quantSaveStrategy: (body: QuantStrategySaveRequest) =>
    request<{ saved: boolean; strategy: QuantStrategy; directory: string }>(
      "/api/v1/quant/strategies", {
        method: "POST", body: JSON.stringify(body),
      }),
  quantDeleteStrategy: (id: string) =>
    request<{ deleted: boolean; id: string }>(
      `/api/v1/quant/strategies/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
};

// ==================== 多因子（策略回测 · 多因子模式） ====================

export type QuantFactor = {
  key: string; label: string; category: string;
  direction: number; formula: string;
};

export type QuantFactorList = {
  count: number;
  categories: { key: string; label: string; count: number; factors: QuantFactor[] }[];
  factors: QuantFactor[];
  registry_size: number;
  disclaimer: string;
};

export type QuantDataStatus = {
  universe: string;
  datasets: { dataset: string; partitions: number; rows: number;
              first: string; last: string }[];
  akshare_periods: number;
  qmt_price_codes: number;
  total_rows: number;
  warehouse?: {
    available: boolean;
    dialect: string;
    description: string;
    tables: WarehouseTableHealth[];
    total_rows: number;
    error: string;
  };
  ready: boolean;
  hint: string;
};

export type QuantIcRow = {
  factor: string; label: string; category: string;
  IC: number | null; ICIR: number | null; t: number | null;
  "IC>0": number | null; periods: number;
};

export type QuantGroupStat = {
  index: number | string;
  annual_return: number | null; vol: number | null;
  sharpe: number | null; max_dd: number | null; avg_turnover: number | null;
};

export type QuantScreenResult = {
  ic_table: QuantIcRow[];
  oos_table: QuantIcRow[];
  clusters: { representative: string; size: number;
              members: { factor: string; icir: number; kept: boolean }[] }[];
  selected: string[];
  dropped: { factor: string; kept_by: string; reason: string }[];
  quantile: {
    n_groups?: number; hl_return?: number; hl_sharpe?: number;
    hl_max_dd?: number; turnover?: Record<string, number>;
    group_stats?: QuantGroupStat[]; composite_factors?: string[];
  };
  notes: string[];
  start: string; end: string; trading_days: number; universe_size: number;
  panels_gaps: string[];
  disclaimer: string;
};

export type QuantScreenRequest = {
  start: string; end: string; factors: string[];
  horizon: number; min_ic: number; min_icir: number;
  corr_threshold: number; train_ratio: number; target_count: number;
  neutralize: boolean;
  /** 剔除 ST（历史名称口径）。默认关闭：它会改变截面构成。 */
  exclude_st?: boolean;
  /** 股票池过滤：按 20 日均成交额每日剔除最差的 30%。默认关闭。 */
  liquidity_filter?: boolean;
  liquidity_drop_pct?: number;
};

export type QuantScreenJob = {
  job_id: string; status: "running" | "done" | "failed";
  stage: string; elapsed: number;
  result?: QuantScreenResult; error?: string;
};

// ==================== 单股票多因子条件策略 ====================

export type QuantSingleRequest = {
  code: string; entry: string; exit?: string;
  start?: string; end?: string; name?: string;
  initial_cash?: number; position_pct?: number;
  stop_loss_pct?: number; take_profit_pct?: number;
  max_hold_days?: number; min_hold_days?: number;
  train_ratio?: number;
  t_plus_1?: boolean; respect_price_limits?: boolean;
  respect_suspension?: boolean;
  /** ST 期间不买入（历史名称口径）。 */
  respect_st?: boolean;
  commission_rate?: number; min_commission?: number;
  stamp_tax_rate?: number; transfer_fee_rate?: number; slippage_bps?: number;
  auto_save?: boolean;
};

export type QuantTrade = {
  entry_date: string; entry_price: number; shares: number;
  exit_date: string; exit_price: number; hold_days: number;
  pnl: number; return_pct: number; exit_reason: string; cost: number;
};

export type QuantSegment = {
  start: string; end: string; trading_days: number;
  return_pct: number; annual_return_pct: number; max_drawdown_pct: number;
  sharpe: number | null; trades: number; win_rate_pct: number | null;
  benchmark_return_pct?: number | null;
  index_return_pct?: number | null;
  excess_vs_benchmark_pct?: number | null;
  excess_vs_index_pct?: number | null;
};

export type QuantVerdict = {
  has_value: boolean; all_dimensions?: boolean;
  passed_count: number; total_count: number; summary: string;
  checks: { key: string; label: string; passed: boolean | null;
            detail: string }[];
};

export type QuantSingleResult = {
  code: string; name: string;
  index_code: string;
  dates: string[]; equity: number[]; benchmark: number[];
  benchmark_index: number[]; positions: number[];
  trades: QuantTrade[];
  metrics: {
    initial_cash: number; final_equity: number;
    total_return_pct: number; annual_return_pct: number;
    volatility_pct: number; sharpe: number | null; sortino: number | null;
    max_drawdown_pct: number; trade_count: number;
    win_rate_pct: number | null; avg_win_pct: number | null;
    avg_loss_pct: number | null; profit_factor: number | null;
    avg_hold_days: number | null; exposure_pct: number;
    total_cost: number; trading_days: number;
    exit_reasons: Record<string, number>;
    // 基准对比
    benchmark_final?: number; index_final?: number;
    benchmark_return_pct?: number; index_return_pct?: number;
    excess_vs_benchmark_pct?: number; excess_vs_index_pct?: number;
    benchmark_max_drawdown_pct?: number; index_max_drawdown_pct?: number;
    benchmark_sharpe?: number | null; index_sharpe?: number | null;
    calmar?: number | null;
  };
  segments: Record<string, QuantSegment>;
  verdict: QuantVerdict;
  entry_condition: { text: string; factors: string[] };
  exit_condition?: { text: string; factors: string[] };
  warnings: string[];
  notes: string[];
  config: Record<string, unknown>;
  start: string; end: string; trading_days: number;
  panels_gaps: string[];
  panel_origins: string[];
  elapsed_seconds: number; peak_memory_mb: number;
  saved_strategy?: { id?: string; name?: string; auto_saved: boolean;
                     reason?: string };
  disclaimer?: string;
};

export type QuantSingleJob = {
  job_id: string; status: "running" | "done" | "failed";
  stage: string; elapsed: number;
  result?: QuantSingleResult; error?: string;
};

export type QuantStrategy = {
  id: string; name: string; code: string; entry: string; exit: string;
  config: Record<string, unknown>;
  metrics: Record<string, number | null>;
  segments: Record<string, QuantSegment>;
  data_range: { start?: string; end?: string; trading_days?: number };
  warnings: string[];
  content_hash: string; source: string; auto_saved: boolean;
  created_at: string; updated_at: string; saved_count: number;
};

export type QuantStrategySummary = {
  id: string; name: string; code: string;
  entry?: string; entry_condition?: string;
  exit?: string; exit_condition?: string;
  preset?: string;
  created_at: string; updated_at: string;
  auto_saved: boolean; saved_count?: number;
  total_return_pct: number | null; max_drawdown_pct: number | null;
  sharpe: number | null; trade_count: number | null;
  win_rate_pct: number | null;
  oos_return_pct: number | null; oos_trades?: number | null;
  /** 近一年口径（"跑赢基准"的实际计量窗口） */
  recent_return_pct?: number | null;
  recent_excess_pct?: number | null;
  recent_benchmark_pct?: number | null;
  recent_trades?: number | null;
  /** 交易笔数 <5 → 单笔运气就能主导结论，必须显式提示 */
  low_sample?: number | null;
  /** 搜索上下文：这条是从多少组合里选出来的、全体中位超额多少 */
  search_space?: number | null;
  scan_median_excess_pct?: number | null;
  scan_beat_ratio?: number | null;
  beats_benchmark?: number | null;
  data_range?: { start?: string; end?: string; trading_days?: number };
  recent_window?: string;
  verdict_passed?: number | null; verdict_total?: number | null;
  warning_count?: number;
};

export type QuantStrategyList = {
  count: number;
  /** db:sqlite.quant_strategy / file —— 如实说明这批数据从哪来 */
  source: string;
  stats: {
    available?: boolean; dialect?: string; table?: string;
    total?: number; winners?: number;
    best_recent_excess_pct?: number | null;
    directory?: string; hint?: string; error?: string;
  };
  database: { dialect: string; description: string; available: boolean };
  filters: { min_excess: number; only_winners: boolean; code: string };
  strategies: QuantStrategySummary[];
  disclaimer: string;
};

export type QuantStrategyPreset = {
  key: string; label: string; entry: string; exit: string;
  note: string; params: Record<string, number>;
};

// ==================== 说明书与开源策略案例 ====================

export type QuantHelpFunction = {
  name: string; signature: string; example: string;
  kind: "时序" | "截面" | "逐元素";
};

export type QuantHelpFactorGroup = {
  category: string; count: number;
  factors: { key: string; label: string; direction: number;
             formula: string; note: string }[];
};

export type QuantHelpFieldGroup = {
  group: string; columns: string[]; note: string;
};

export type QuantHelpParam = {
  name: string; default: unknown; type: string; meaning: string;
};

export type QuantHelp = {
  topic: string; title: string;
  sections: { heading: string; items: string[] }[];
  dsl?: {
    functions: QuantHelpFunction[];
    forbidden: { functions: string[]; why: string };
    operators: Record<string, string[]>;
    pit: string;
  };
  panel_fields?: QuantHelpFieldGroup[];
  factors?: QuantHelpFactorGroup[];
  params?: QuantHelpParam[];
  pit_rule?: string;
  limitations?: string[];
  troubleshooting?: { symptom: string; cause: string; fix: string }[];
  disclaimer: string;
};

export type QuantStrategyCase = {
  id: string; theme: string; title: string; summary: string;
  source_name: string; source_url: string;
  published_at: string; fetched_at?: string;
  tags?: string[];
  kind?: string;
  /** 恒为 0：抓取的内容未经本项目复现验证 */
  verified?: number;
  disclaimer?: string;
};

export type QuantCaseList = {
  count: number;
  themes: { theme: string; count: number }[];
  kinds: { kind: string; count: number }[];
  filters: Record<string, unknown>;
  cases: QuantStrategyCase[];
  verified: boolean;
  note: string;
  disclaimer: string;
};

export type QuantStrategySaveRequest = {
  code: string; entry: string; exit?: string; name?: string;
  config?: Record<string, unknown>;
  result?: QuantSingleResult | Record<string, unknown>;
  thresholds?: Record<string, number>;
};

/** ==================== 股票字典（联动联想） ==================== */

export type StockEntry = {
  code: string; name: string;
  pinyin_initials: string; pinyin_full: string;
  instrument_type: string;
  exchange?: string; market?: string;
  industry?: string; area?: string; list_date?: string;
  source?: string;
  /** 预算好的 `代码 名称`，列表里直接显示 */
  label?: string;
};

export type StockSearchResult = {
  query: string; count: number;
  total_in_directory: number;
  stocks: StockEntry[];
  hint: string;
};

/** 一个概念板块与某只个股的相关性（`/quant/stocks/{code}/boards`）。 */
export type ConceptBoard = {
  code: string;
  name: string;
  /** 概念内成员数：越少说明概念越"窄"、越能刻画这只票 */
  members: number;
  relevance: number;
  /** 板块当日主力净额（亿元） */
  net_yi: number | null;
  change_pct: number | null;
  reasons: string[];
};

export type StockBoardsResult = {
  code: string; name: string; count: number;
  boards: ConceptBoard[];
  /** 相关性最高的概念名（前端"自动关联最相关概念"用它） */
  auto_default: string;
  /** true=在线取数失败，用的是落库结果 */
  stale: boolean;
  note: string;
};

/** 概念名联想项（`/quant/concepts/suggest`）。 */
export type ConceptSuggestion = {
  code: string; name: string; members: number;
};

/** 同源WebSocket地址（vite代理已开启ws升级；生产同源直连）。 */
export function alertsWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/v1/ws/alerts?tenant_id=tenant_001`;
}
