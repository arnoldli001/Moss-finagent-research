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
  alert_key: string;
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
  source_name: string;
  source_url: string;
  event_publish_time: string;
  trigger_time: string;
  expire_time: string;
  status: "active" | "read" | "expired";
  tenant_id: string;
  disclaimer: string;
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
  schedule: { job: string; cron: string | null };
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

/** 逐bar档位：低吸/高抛/止损随时间的真实取值（档位是时刻量，不是全天恒定）。 */
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
  boll_upper: number | null; boll_mid: number | null; boll_lower: number | null;
  pct_b: number | null; bandwidth: number | null;
  vwap: number | null; atr: number | null;
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
  health: IntradayHealth;
  config_snapshot: Record<string, unknown>;
  notifier: Record<string, unknown>;
  disclaimer: string;
};

export type IntradayWatchItem = {
  code: string; name: string; boards: string[];
  total_score: number | null;
  signal_strength: "solid" | "hollow" | "forced_exit" | "none";
  signal_kind: "low_buy" | "high_sell" | "stop_loss" | "none";
  price: number | null; change_pct: number | null;
};

/** 自选池自动刷新的运行状态（服务端每分钟重算一次，前端据此展示刷新时间/暂停原因）。 */
export type IntradayWatchRefresh = {
  enabled: boolean;
  interval_seconds: number;
  window_open: boolean;
  window_reason: string;
  running: boolean;
  generation: number;
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

/** 个股股性画像：做T友好度 + 推荐权重模板 + 推荐档位。 */
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
  /** 推荐模板 key。 */
  template: string;
  /** 推荐权重（已按股性微调并归一化到 100）。 */
  weights: Record<string, number>;
  /** 推荐档位（min_band_pct/max_band_pct/stop_loss_pct/atr_stop_mult/
   *  touch_band_pct/dip_fallback_atr）。 */
  levels: Record<string, number>;
  /** 中文人话说明，直接展示。 */
  notes: string[];
  template_label: string;
  template_description: string;
  templates: WeightTemplate[];
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
  signal: Record<string, unknown> | null;
};

// ==================== 日K级别做T（量价体系） ====================

export type IntradayDailyBar = {
  date: string;
  open: number; high: number; low: number; close: number;
  volume: number; amount: number;
  pct_chg: number | null; amplitude: number | null; turnover: number | null;
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
  /** buy=买点；sell=卖点(S1–S3/S6)；risk=风控/止损类 */
  side: "buy" | "sell" | "risk";
  code: string;
  name: string;
  price: number | null;
  entry: number | null;
  stop_loss: number | null;
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
  health: IntradayHealth;
  config_snapshot: Record<string, unknown>;
  disclaimer: string;
};


async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`请求失败(${resp.status}): ${detail.slice(0, 200)}`);
  }
  return resp.json() as Promise<T>;
}

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

  // ---- 做T辅助 ----
  intradaySnapshot: (code: string, refresh = false) =>
    request<IntradaySnapshot>(
      `/api/v1/intraday/snapshot?code=${encodeURIComponent(code)}` +
      (refresh ? "&refresh=true" : "")),
  intradayWatchlist: (force = false) =>
    request<IntradayWatchPayload>(
      `/api/v1/intraday/watchlist${force ? "?force=true" : ""}`),
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
  /** 个股股性画像（推荐权重/档位的来源）。 */
  intradayCharacter: (code: string, mode: IntradayMode = "intraday",
                      refresh = false) =>
    request<CharacterProfile>(
      `/api/v1/intraday/character?code=${encodeURIComponent(code)}`
      + `&mode=${mode}` + (refresh ? "&refresh=true" : "")),
  /** 市场情绪周期（涨停家数/炸板率/连板高度 → 阶段与做T环境温度）。 */
  intradayMarketCycle: (force = false) =>
    request<MarketCycle>(
      `/api/v1/intraday/market-cycle${force ? "?force=true" : ""}`),
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

/** 同源WebSocket地址（vite代理已开启ws升级；生产同源直连）。 */
export function alertsWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/v1/ws/alerts?tenant_id=tenant_001`;
}
