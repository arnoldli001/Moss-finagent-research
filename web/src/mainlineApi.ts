import {
  apiErrorFromResponse,
  networkError as makeNetworkError,
} from "./errors";
import { notifyUnauthorized } from "./unauthorized";

/**
 * 主线挖掘（Mainline Mining）API 封装 —— 与后端 `/api/v1/mainline` 一一对应。
 *
 * ## 为什么把类型写得这么"松"
 *
 * 这个模块的数据是**全历史回填 + 逐日打分**算出来的：某天的板块可能一条都算不出来、
 * 龙头股可能空、`resonance_ratio` 在样本不足时就是 `null`、`flow_5d/flow_20d` 在
 * 资金流表还没同步到那天时也是 `null`。后端明确约定"缺数据给 `null` 或空数组"，
 * 所以这里**如实声明可空**（`number | null`）而不是给默认值 0 —— 前端如果按 0 显示，
 * 用户会把"没算出来"读成"真的是 0"，这是两件完全不同的事。
 *
 * 金额字段（`net_today` / `net_5d` / `flow_5d` / `flow_20d`）后端统一用**元**，
 * 展示一律走 `formatYi()` 换算成亿元，不要在组件里各写一份 `/1e8`。
 *
 * ## 接口清单
 *
 * | 用途 | 接口 |
 * | --- | --- |
 * | 快照（漏斗/评分/告警/跟踪，一个请求拿全） | `GET /snapshot` |
 * | 单板块下钻（三层明细 + 历史） | `GET /board/{code}` |
 * | 告警流水（可筛等级/日期） | `GET /alerts` |
 * | 期货先行信号 | `GET /futures` |
 * | 回测报告（单次 / 历史列表） | `GET /backtest`、`/backtest/list` |
 * | 数据体量与同步状态 | `GET /data/status` |
 * | 手动刷新（异步任务 + 轮询） | `POST /refresh`、`GET /refresh_status` |
 */

const PREFIX = "/api/v1/mainline";

// ---------------------------------------------------------------- 通用枚举与文案

/** 信号等级：后端只给三档（强/中/弱），前端不做第四档。 */
export type MainlineLevel = "strong" | "medium" | "weak" | "none";

/** 评分层级链路：板块种类（申万一级/二级/概念…），仅作展示标签。 */
export type MainlineLayerKey = string;

/** 期货信号类型：价格动量 / 期股相关性跃升 / 持仓量异动 / 期限结构变化。 */
export type FuturesKind =
  | "momentum" | "correlation_jump" | "open_interest" | "term_structure";

/** 期货与板块的方向：正向 / 反向 / 辅助参考。 */
export type FuturesDirection = "positive" | "negative" | "auxiliary";

/** 期货品种分类：内盘 / 外盘 / 非期货。 */
export type FuturesScope = "domestic" | "foreign" | "non_futures";

/** 等级 → 中文短标签（后端也返回 `level_label`，但筛选项/图例要自己拼）。 */
export const LEVEL_TEXT: Record<string, string> = {
  strong: "🔴 强",
  medium: "🟡 中",
  weak: "🟢 弱",
  none: "未触发",
};

/** 等级排序权重：强 → 中 → 弱，用于"按等级看流水"的稳定排序。 */
export const LEVEL_ORDER: Record<string, number> = {
  strong: 3, medium: 2, weak: 1, none: 0,
};

/** 期货信号类型 → 中文标签。 */
export const FUTURES_KIND_TEXT: Record<string, string> = {
  momentum: "价格动量",
  correlation_jump: "期股相关性跃升",
  open_interest: "持仓量异动",
  term_structure: "期限结构变化",
};

/** 期货方向 → 中文标签。 */
export const DIRECTION_TEXT: Record<string, string> = {
  positive: "正向（同向）",
  negative: "反向（背离）",
  auxiliary: "辅助参考",
};

/** 期货品种分类 → 中文标签。 */
export const SCOPE_TEXT: Record<string, string> = {
  domestic: "内盘",
  foreign: "外盘",
  non_futures: "非期货",
};

// ---------------------------------------------------------------- 单个板块的评分

/** 龙头股（评分里"龙头集中度"维度用的成分股摘要）。 */
export type MainlineLeader = {
  code: string;
  name: string;
  /** 近 5 日主力净流入（元） */
  net_5d: number | null;
  /** 近 10 日涨幅（%） */
  ret_10d: number | null;
  /** 命中几个强势条件（资金/动量/量能） */
  hits: number | null;
  capital_rank: number | null;
  momentum_rank: number | null;
  volume_rank: number | null;
  /** 成交额占板块比例 */
  amount_ratio: number | null;
  /** 数据溯源标签（后端记录的原始来源） */
  source: string;
  /**
   * LLM 对「(这只股票, 这个题材)」的主营相关度（0-100）；null = 没判过。
   *
   * ⚠️ 「龙头」是**资金/动量意义上的**：`identify_leaders()` 只看
   * 资金/动量/量能三维，业务相关性从不参与。所以这个字段的作用是让
   * 「资金龙头」与「业务龙头」**分得开** —— 2026-09-22 用户就是看到
   * 氟化工概念的龙头里有雅克科技（主营半导体材料）才提出质疑的。
   */
  business_score?: number | null;
  /** 业务分来源：`llm` / `llm_theme`（判过并给分）/ `corr`（按相关性入池）/ `""` */
  business_source?: string;
  /** 一句话依据，如「LLM 判定「氟化工」主营分 95」「corr 0.610 ≥ 0.55」 */
  business_reason?: string;
  /**
   * 直接可显示的标签，如「业务相关 95 分」「仅股价相关（LLM 未判过该题材）」
   * 「业务仅 65 分（未达门槛，凭股价相关入池）」。
   */
  business_label?: string;
  /**
   * 入池通道（**只用于展示**）：`corr` = 相关性直通 / `business` = 主营分达标 /
   * `corr_business_failed` = 靠相关性入池、但 LLM 判过且未达 70 分门槛。
   *
   * ⚠️ 与 `business_source` 不是同一件事：后者答"业务分从哪来"，
   * 前者答"这只票凭什么进的池"。东华科技 002140 @ 钛白粉概念就是
   * `corr` + 业务分 65（<70）——旧代码只按 `source` 显示成"LLM 未判过"，
   * 与数据相反。
   */
  admission?: string;
  /** 业务口径是否被绕过（前端用警示色标出）。 */
  business_warn?: boolean;
};

/**
 * 单板块当日评分（`snapshot.scores/candidates/selected` 与 `board.score` 共用）。
 *
 * `total` 已经含门控加分；`base_total` 是不含门控的原始分 —— 两者并列展示是为了
 * 让人一眼看出"这分是被门控抬上去的"。
 */
export type MainlineScore = {
  code: string;
  name: string;
  trade_date: string;
  /** 板块种类，如 sw_l1 / sw_l2 / concept */
  kind: string;
  total: number;
  base_total: number | null;
  six_dim_score: number | null;
  accumulation_score: number | null;
  leader_score: number | null;
  gate_bonus: number | null;
  /**
   * 第二层的 **ETF 异动加分**（0~20，不占权重）。
   *
   * 与 `gate_bonus` 并列，但兑现时机不同：门控加分要先入选精选才给，
   * ETF 加分**无条件兑现**（历史级放量是稀有事件，且它正要把"还没进精选"
   * 的板块顶上来）。两者相加构成 `total - base_total`。
   */
  etf_bonus: number | null;
  /** ETF 异动级别：0=无，1=历史级放量，2=放量+净申购 */
  etf_level: number | null;
  /**
   * 各层**参与加权的维度占比**（<1 说明有维度缺数据）。
   *
   * 纯展示、不参与合成：`weighted_score` 会把缺数据的维度从分母剔除并
   * 重新归一化，所以"只有杠杆有数据"的板块与"四维齐全"的板块会得到
   * 同等强度的第二层分。面板必须能看见覆盖率，否则这两种分会被误当作可比。
   */
  six_dim_coverage: number | null;
  accumulation_coverage: number | null;
  weights: Record<string, number> | null;
  weight_mode: string;
  rank: number | null;
  /** 是否进了候选池 */
  candidate: boolean;
  /** 是否进了精选名单 */
  selected: boolean;
  /** 是否触发共振（资金 + 动量同时达标） */
  resonance: boolean;
  /** 共振比例，样本不足时为 null */
  resonance_ratio: number | null;
  leaders: MainlineLeader[];
  /** 席位/龙虎榜备注，可能为空串 */
  seat_note: string;
  level: string;
  level_label: string;
  /** 触发原因（人类可读） */
  reasons: string[];
  /** 缺失数据说明（**必须展示**：否则用户不知道分数为什么低） */
  gaps: string[];
  /** 当日涨跌幅（%） */
  change_pct: number | null;
  /** 当日主力净流入（元） */
  net_today: number | null;
  /** 权重档位标识 */
  profile_key: string;
  /** 衰减周期（交易日） */
  decay_days: number | null;
};

/** 告警信号（`snapshot.alerts`、`/alerts`、回测的 signals/false_positives 共用）。 */
export type MainlineAlert = {
  alert_id: string;
  board_code: string;
  board_name: string;
  /** 触发日 */
  trade_date: string;
  kind: string;
  level: string;
  level_label: string;
  score: number | null;
  six_dim_score: number | null;
  accumulation_score: number | null;
  leader_score: number | null;
  gate_bonus: number | null;
  /** 触发的维度 key 列表（trading/moneyflow/leverage…） */
  triggered_dims: string[];
  resonance: boolean;
  reasons: string[];
  /** 触发日板块指数收盘 */
  entry_close: number | null;
  /** 触发日涨跌幅（%） */
  change_pct: number | null;
  /** 告警后最大涨幅（%）与出现日期 */
  max_gain_pct: number | null;
  max_gain_date: string;
  ret_5d: number | null;
  ret_10d: number | null;
  ret_20d: number | null;
  ret_60d: number | null;
  /** 是否被后续走势确认 */
  confirmed: boolean;
  confirmed_date: string;
  /** 是否已推送 */
  pushed: boolean;
  push_note: string;
};

/** 重点跟踪板块（比评分多"资金累计净流入"与"共振比例"）。 */
export type MainlineTracked = {
  code: string;
  name: string;
  kind: string;
  total: number;
  six_dim: number | null;
  accumulation: number | null;
  gate_bonus: number | null;
  /** ETF 异动加分（不占权重，无条件兑现；见 `MainlineScore.etf_bonus`） */
  etf_bonus: number | null;
  etf_level: number | null;
  level: string;
  profile: string;
  decay_days: number | null;
  resonance: boolean;
  resonance_ratio: number | null;
  leaders: {
    code: string;
    name: string;
    hits: number | null;
    net_5d: number | null;
    ret_10d: number | null;
  }[];
  seat_note: string;
  /** 近 5 日累计净流入（元） */
  flow_5d: number | null;
  /** 近 20 日累计净流入（元） */
  flow_20d: number | null;
};

/** 快照接口的漏斗/权重元信息。 */
export type MainlineSnapshotMeta = {
  generated_at: string;
  trade_date: string;
  /** open / closed / pre_open … 由后端给出 */
  session_state: string;
  session_label: string;
  board_count: number;
  scored_count: number;
  candidate_count: number;
  selected_count: number;
  alert_count: number;
  weight_mode: string;
  weights: Record<string, number>;
  /** 各资金维度的可用条数与分布（"这个维度今天有多少板块算得出来"） */
  ic_summary: Record<string, MainlineIcBucket>;
  gap_count?: number;
};

/** `ic_summary` 里单个维度的统计桶。 */
export type MainlineIcBucket = {
  label: string;
  available: number;
  mean: number | null;
  min: number | null;
  max: number | null;
};

/** `GET /snapshot` 的完整返回。 */
export type MainlineSnapshot = MainlineSnapshotMeta & {
  /** 全部评分（已按 total 降序、截断到 top） */
  scores: MainlineScore[];
  candidates: MainlineScore[];
  selected: MainlineScore[];
  alerts: MainlineAlert[];
  tracked: MainlineTracked[];
  source_notes: string[];
  gaps: string[];
  /** 后端建议的刷新提示（非空时前端要显眼地提示"该刷新了"） */
  refresh_hint: string;
  disclaimer: string;
  /**
   * 后端附带的本地仓状态（`/snapshot` 与刷新任务结果里都有）。
   * 有它就不必再单独请求 `/data/status` —— 面板脚注直接用它报"缓存里有多少行"。
   */
  data_status?: MainlineDataStatus | null;
  /** 刷新任务附带的同步说明（每条数据集一行），只在 refresh 结果里有 */
  sync_notes?: string[];
  /**
   * `/refresh_status` 的任务结果字段名是共用的：刷新任务回的是 snapshot（没有
   * `run_id`），回测任务回的是回测报告（有 `run_id` 而没有评分数组）。
   * 所以这里把 `run_id` 声明为可选 —— 用它可以判断"这是哪类任务的结果"，
   * 而不是去强转类型。
   */
  run_id?: string;
};

// ---------------------------------------------------------------- 下钻（三层明细）

/** 一层里的单个维度明细。 */
export type MainlineDimension = {
  key: string;
  label: string;
  score: number | null;
  weight: number | null;
  /** 该维度对总分的贡献（= score × weight / 100） */
  contribution: number | null;
  /** 原始因子值（无固定 schema，逐个读取并做有限性判断） */
  raw: Record<string, number | string | null>;
  note: string;
  available: boolean;
};

/** 一层（六维基座 / 建仓资金 / 门控）的完整明细。 */
export type MainlineLayer = {
  key: string;
  label: string;
  score: number | null;
  weight: number | null;
  /** 覆盖率 0~1，低于 1 说明有维度缺数据 */
  coverage: number | null;
  available: boolean;
  notes: string[];
  dimensions: MainlineDimension[];
};

/** 历史评分点（升序）。 */
export type MainlineHistoryPoint = {
  trade_date: string;
  total: number | null;
  six_dim: number | null;
  accumulation: number | null;
  level: string;
};

/** `GET /board/{code}` 的返回。 */
export type MainlineBoardDetail = {
  code: string;
  name: string;
  trade_date: string;
  /** 单板块评分（含 layers） */
  score: MainlineScore | null;
  layers: MainlineLayer[];
  history: MainlineHistoryPoint[];
  /** 该板块的跟踪对象，可能为 null */
  tracked: MainlineTracked | null;
  /** 后端补充说明（如"本地还没有该板块的评分历史"） */
  note?: string;
};

/** `GET /alerts` 的返回。 */
export type MainlineAlertList = {
  alerts: MainlineAlert[];
  count: number;
  /** 仓储不可用等情况下后端会带一条说明 */
  gaps?: string[];
};

// ---------------------------------------------------------------- 期货先行信号

/** 单个期货品种的先行信号。 */
export type FuturesSignal = {
  code: string;
  name: string;
  kind: string;
  trade_date: string;
  close: number | null;
  change_pct: number | null;
  /** 以下 ret_* 是**小数**（0.021 = 2.1%），展示时要 ×100 */
  ret_5d: number | null;
  ret_10d: number | null;
  ret_20d: number | null;
  /** 动量 z 值 */
  z_5d: number | null;
  /** 持仓量 5 日变化（小数） */
  oi_change_5d: number | null;
  /** 期限结构（近远月价差比率） */
  term_structure: number | null;
  /** 异动强度 0~100，仪表盘按它排序 */
  intensity: number | null;
  kinds: string[];
  level: string;
  level_label: string;
  reasons: string[];
  /** 产业链标识，如 coal_steel */
  chain: string;
  /** 关联板块名列表 */
  boards: string[];
  gaps: string[];
};

/** 期货告警（与板块告警结构不同：没有 score/level 之外的行情字段）。 */
export type FuturesAlert = {
  alert_id: string;
  date: string;
  code: string;
  name: string;
  level: string;
  level_label: string;
  title: string;
  detail: string;
  boards: string[];
  chain: string;
  kinds: string[];
};

/** 期股映射关系（`mappings` 列表里的一行）。 */
export type FuturesMapping = {
  future_code: string;
  future_name: string;
  board_code: string;
  board_name: string;
  direction: string;
  /** 1~5 星 */
  strength: number | null;
  /** 领先天数（正 = 期货领先板块） */
  lead_days: number | null;
  /** 传导逻辑说明（后端给的中文推导） */
  logic: string;
  chain: string;
  kind: string;
  calibrated_strength: number | null;
  calibrated_at: string;
};

/** `GET /futures` 的返回。 */
export type MainlineFuturesSnapshot = {
  generated_at: string;
  trade_date: string;
  /** 品种计数：内盘/外盘/非期货/告警 */
  counts: Record<string, number>;
  signals: FuturesSignal[];
  alerts: FuturesAlert[];
  /** 相关系数矩阵：期货代码 → { 板块名: 相关系数 } */
  correlation: Record<string, Record<string, number>>;
  mappings: FuturesMapping[];
  source_notes: string[];
  gaps: string[];
  disclaimer: string;
};

// ---------------------------------------------------------------- 回测

/** 单个指标的达标情况（`metrics.targets`）。 */
export type BacktestTarget = {
  value: number | null;
  target: number | null;
  passed: boolean;
};

/** 单维度回测表现（`metrics.by_dim`）。 */
export type BacktestDimMetric = {
  ic_mean: number | null;
  icir: number | null;
  ic_win_rate: number | null;
  samples: number | null;
};

/** 分持有期表现（`metrics.by_holding`，键是持有天数）。 */
export type BacktestHoldingMetric = {
  long_annual: number | null;
  long_excess: number | null;
  win_rate: number | null;
};

/** 回测核心指标。字段全部可空 —— 样本不足时后端会留空而不是编一个数。 */
export type BacktestMetrics = {
  ic_mean: number | null;
  ic_std: number | null;
  icir: number | null;
  ic_win_rate: number | null;
  ic_samples: number | null;
  long_short_annual: number | null;
  long_excess: number | null;
  long_annual: number | null;
  benchmark_annual: number | null;
  sharpe: number | null;
  max_drawdown: number | null;
  signal_count: number | null;
  signal_hit_rate: number | null;
  false_positive_rate: number | null;
  avg_max_gain: number | null;
  median_max_gain: number | null;
  by_dim: Record<string, BacktestDimMetric>;
  by_holding: Record<string, BacktestHoldingMetric>;
  targets: Record<string, BacktestTarget>;
};

/** 一次 walk-forward 折（训练段 / purge 段 / 验证段）。 */
export type BacktestFold = {
  index: number;
  train_start: string;
  train_end: string;
  purge_end: string;
  validate_start: string;
  validate_end: string;
  weights: Record<string, number>;
  weight_mode: string;
  ic_six: number | null;
  ic_accumulation: number | null;
  ic_leader: number | null;
  ic_total: number | null;
  ic_equal: number | null;
  boards: number | null;
  alerts: number | null;
  note: string;
};

/** 历史场景回归验证（如 CRO 行情是否会提前告警）。 */
export type BacktestScene = {
  key: string;
  label: string;
  start_date: string;
  /** 检查窗口 [起, 止] */
  check_window: string[];
  min_lead_days: number | null;
  keywords: string[];
  triggered: boolean;
  first_alert_date: string;
  first_alert_board: string;
  lead_days: number | null;
  max_gain_pct: number | null;
  ret_60d: number | null;
  passed: boolean;
  note: string;
};

/** 因子相关性验证。 */
export type BacktestCorrelation = {
  factors: string[];
  /** 下三角矩阵：a → { b: corr } */
  matrix: Record<string, Record<string, number>>;
  /** 超过阈值的因子对（要显眼提示"共线性风险"） */
  high_pairs: { a: string; b: string; corr: number }[];
  cross_layer_mean_abs: number | null;
  cross_layer_max_abs: number | null;
  limit: number | null;
  samples: number | null;
  passed: boolean;
  note: string;
};

/** `GET /backtest?run_id=` 的完整返回（含超长 markdown 报告正文）。 */
export type MainlineBacktestReport = {
  run_id: string;
  started_at: string;
  finished_at: string;
  seconds: number | null;
  range_start: string;
  range_end: string;
  config_note: string;
  metrics: BacktestMetrics | null;
  folds: BacktestFold[];
  scenes: BacktestScene[];
  signals: MainlineAlert[];
  false_positives: MainlineAlert[];
  correlation: BacktestCorrelation | null;
  /** 监控胜率报告正文（Markdown，可能很长；前端只做轻量解析） */
  markdown: string;
  gaps: string[];
  error: string;
  disclaimer: string;
};

/** 回测历史列表的一行。 */
export type BacktestRunSummary = {
  run_id: string;
  range_start: string;
  range_end: string;
  started_at: string;
  finished_at: string;
  seconds: number | null;
  metrics: BacktestMetrics | null;
  error: string;
  gaps: string[];
};

// ---------------------------------------------------------------- 信号收益追踪

/**
 * 一个持有周期的追踪结果。
 *
 * ⚠️ `max_gain_pct` / `ret_pct` 在窗口**没走满**时是 `null`（后端"暂时不填"），
 * 这时要看 `current_gain_pct` / `current_ret_pct`（截至最新数据的进度）与
 * `remaining_days`。**不要把 null 当成 0** —— 那是"还没走完"，不是"没涨"。
 */
export type AlertReturnWindow = {
  days: number;
  /** done=窗口走满 / partial=还没走满 / pending=信号日之后还没有K线 */
  status: string;
  /** 窗口内最高价相对信号日收盘（理论可捕获空间）；未走满为 null */
  max_gain_pct: number | null;
  /** 已走部分的最大涨幅（至今进度） */
  current_gain_pct: number | null;
  /** 窗口末收盘涨跌幅（"持有到底"）；未走满为 null */
  ret_pct: number | null;
  current_ret_pct: number | null;
  /** 窗口内最低价相对信号日收盘（负数 = 中途最多浮亏） */
  worst_pct: number | null;
  max_gain_date: string;
  bars_used: number;
  remaining_days: number;
};

/** 区间领涨成分股。 */
export type AlertReturnLeader = {
  code: string;
  name: string;
  gain_pct: number | null;
  entry_close: number | null;
  high: number | null;
};

/**
 * 板块排行里的领涨股：比逐条表少了 `entry_close` / `high`
 * （那里只需要"哪几只票"，不需要成本价与最高价）。
 */
export type AlertReturnBoardLeader = {
  code: string;
  name: string;
  gain_pct: number | null;
  signal_date: string;
};

/** 信号收益追踪表的一行 = 一个信号周期（同概念重复触发折叠成 `alert_count`）。 */
export type AlertReturnRow = {
  signal_date: string;
  board_code: string;
  board_name: string;
  level: string;
  level_label: string;
  score: number | null;
  /** 1 = 单次；2 = X2；3 = X3 …（`dedup_days` 个交易日内的重复触发） */
  alert_count: number;
  alert_dates: string[];
  /** history = 分界日之前；recent = 近一个月与未来 */
  segment: string;
  entry_close: number | null;
  days_elapsed: number;
  latest_date: string;
  latest_close: number | null;
  current_pct: number | null;
  /** 键是周期天数的字符串形式（"7"/"20"/"60"） */
  windows: Record<string, AlertReturnWindow>;
  leaders: AlertReturnLeader[];
  /** pure=提纯股池 / member=原始成员表 / ""=没算 */
  leaders_source: string;
  /** 告警当时记录在 payload 里的收益（用于和重算口径互校） */
  recorded: Record<string, unknown>;
};

/** 概念板块汇总（"不怎么亏钱或收益较好"的排行）。 */
export type AlertReturnBoard = {
  board_code: string;
  board_name: string;
  signals: number;
  alert_total: number;
  first_date: string;
  last_date: string;
  strong: number;
  medium: number;
  avg_gain_7d: number | null;
  avg_gain_20d: number | null;
  avg_gain_60d: number | null;
  avg_ret_7d: number | null;
  avg_ret_20d: number | null;
  avg_ret_60d: number | null;
  avg_ret_20d_now: number | null;
  win_rate_20d: number | null;
  hit_rate_20d: number | null;
  worst_20d: number | null;
  done_20d: number;
  done_60d: number;
  avg_current: number | null;
  quality: string;
  /** 判定用了哪个口径：ret_20d=已走满的平均实际收益 / ret_20d_now=至今 */
  basis: string;
  /**
   * 筛选三态（口径见后端 `summarize_boards`）：
   * `pass` 胜率达标 → 显示；`hidden` 胜率低于门槛 → **不显示**；
   * `pending` 一条 20 日窗口都没走满 → 无从判定，**显示**。
   */
  gate: string;
  /** `gate !== "hidden"`（`pending` 也算显示，否则最近一个月的板块会全被剔除） */
  passed: boolean;
  leaders: AlertReturnBoardLeader[];
};

/** `GET /alert-returns` 的返回。 */
export type AlertReturnPayload = {
  as_of: string;
  generated_at: string;
  horizons: number[];
  dedup_days: number;
  split: string;
  stock_window: number;
  min_win_rate: number;
  history_only: boolean;
  leaders_computed: boolean;
  cached?: boolean;
  /**
   * **唯一事实来源**：全部信号周期（新的在前），每行带 `segment` 区分历史 / 近期。
   *
   * 后端**不再**另发 `history` / `recent` 两个数组：那是同一批行的重复副本
   * （1305 行会膨胀成 2436 个对象、3.1 MB），而且两者的板块筛选口径还不一致。
   * 分视图请在本地按 `segment` 过滤，板块筛选按 `boards[].passed`。
   */
  rows: AlertReturnRow[];
  boards: AlertReturnBoard[];
  stats: Record<string, number | string | null>;
  gaps: string[];
  note: string;
};

/** 板块级 20 日胜率门槛（`GET /board-win-rates` 的一项）。 */
export type BoardWinRate = {
  board_code: string;
  board_name: string;
  signals: number;
  /** 已走满的 20 日窗口数 —— 胜率的分母；0 表示这条胜率判不出来 */
  done_20d: number;
  /** 已走满的 20 日窗口里实际收益 > 0 的比例（0~1）；判不出来时为 null */
  win_rate_20d: number | null;
  /** pass / hidden / pending（口径同 `AlertReturnBoard.gate`） */
  gate: string;
  /** `gate === "hidden"` —— 该板块的强/中告警要隐藏 */
  hidden: boolean;
};

/** `GET /board-win-rates` 的返回（刻意只带板块级判定，不带行）。 */
export type BoardWinRatePayload = {
  as_of: string;
  generated_at: string;
  min_win_rate: number;
  levels: string[];
  boards: BoardWinRate[];
  /** 被判为 hidden 的板块数 */
  hidden: number;
  /** 一条 20 日窗口都没走满、无从判定的板块数（这些**不**隐藏） */
  pending: number;
  cached: boolean;
  gaps: string[];
};

// ---------------------------------------------------------------- 数据状态 / 刷新

/** 单张表的行数统计（`data/status.tables`）。 */
export type MainlineTableCount = Record<string, number>;

/** 单个数据集的同步区间。 */
export type MainlineSyncItem = {
  dataset: string;
  span_start: string;
  span_end: string;
  rows: number | null;
  /** ok / empty / failed … */
  status: string;
  message: string;
  seconds: number | null;
  updated_at: string;
};

/** `GET /data/status` 的返回。 */
export type MainlineDataStatus = {
  cache_path: string;
  tables: MainlineTableCount;
  sync: MainlineSyncItem[];
  gaps: string[];
};

/**
 * 刷新任务状态。
 *
 * ⚠️ 契约里同时出现过 `state`（refresh_status 返回）与 `status` 两种命名，
 * 这里两个字段都声明为可选，取值统一走 `resolveTaskState()`——
 * 前端不应该因为后端改名就整块白屏。
 */
export type MainlineRefreshStatus = {
  task_id: string;
  state?: string;
  status?: string;
  message: string;
  progress: string;
  /** done 时直接就是 snapshot 结构 */
  result: MainlineSnapshot | null;
  /** 任务失败时的原因（后端只给非空字符串，不问 null） */
  error?: string;
};

/** 从刷新状态里取"运行态"，兼容 `state` / `status` 两种字段名。 */
export function resolveTaskState(payload: MainlineRefreshStatus | null): string {
  if (!payload) return "";
  const raw = payload.state ?? payload.status ?? "";
  return String(raw).toLowerCase();
}

// ---------------------------------------------------------------- 契约归一

/** 数组兜底：后端给的不是数组（undefined/字符串/对象）就换成 `[]`。 */
function asArray<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

/** 相关性验证：**空内容一律归一成 `null`**，有内容才补齐成完整对象。 */
function asCorrelation(
  value: BacktestCorrelation | null | undefined,
): BacktestCorrelation | null {
  if (!value || typeof value !== "object") return null;
  const factors = asArray(value.factors);
  const matrix = value.matrix && typeof value.matrix === "object"
    ? value.matrix : {};
  const highPairs = asArray(value.high_pairs);
  if (factors.length === 0 && highPairs.length === 0
      && Object.keys(matrix).length === 0) return null;
  return {
    ...value,
    factors, matrix, high_pairs: highPairs,
  };
}

/**
 * 回测报告归一：**所有数组键保证是数组，`correlation` 保证是完整对象或 `null`**。
 *
 * 为什么必须在 API 层做：后端"还没有回测记录"的空壳曾经返回
 * `"correlation": {}` —— `{}` 在 JS 里是**真值**，所以 `report.correlation ?? null`
 * 兜不住它，紧接着的 `correlation.factors.length` 抛
 * `TypeError: Cannot read properties of undefined (reading 'length')`，
 * 整个「回测报告」页签渲染失败。报错在 `.length`，根因在"空对象冒充有内容"。
 *
 * 在这里（而不是在每个组件里）归一：契约的**形状**只有一处定义，
 * 组件才能放心直接读 `report.gaps.length`。
 */
export function normalizeBacktestReport(
  raw: MainlineBacktestReport | null | undefined,
): MainlineBacktestReport | null {
  if (!raw || typeof raw !== "object") return null;
  return {
    ...raw,
    metrics: raw.metrics && typeof raw.metrics === "object"
      ? { ...raw.metrics, by_dim: raw.metrics.by_dim ?? {},
          by_holding: raw.metrics.by_holding ?? {},
          targets: raw.metrics.targets ?? {} }
      : null,
    folds: asArray(raw.folds),
    scenes: asArray(raw.scenes),
    signals: asArray(raw.signals),
    false_positives: asArray(raw.false_positives),
    correlation: asCorrelation(raw.correlation),
    gaps: asArray(raw.gaps),
  };
}

/** 回测历史列表的一行归一（`gaps` 是列表页唯一会读 `.length` 的字段）。 */
function normalizeRunSummary(raw: BacktestRunSummary): BacktestRunSummary {
  return { ...raw, gaps: asArray(raw.gaps) };
}

// ---------------------------------------------------------------- 展示工具

/**
 * 元 → 亿元（保留 2 位、带正负号）。
 *
 * 放在 API 层而不是各组件里：金额的换算口径（除以 1e8、几位小数、负号怎么摆）
 * 必须**只有一处**，否则同一个数在不同面板里显示成不同值，用户会以为数据不一致。
 * `null` / NaN / Infinity 一律返回 `"—"`——**不要返回 "0.00亿"**，
 * "没数据"和"净流入为零"在资金流里是两件完全不同的事。
 */
export function formatYi(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const yi = value / 1e8;
  const text = yi.toFixed(2);
  // 负数自带 "-"，只有正数需要补 "+"
  return yi > 0 ? `+${text}亿` : `${text}亿`;
}

/** 百分比（输入已经是百分数，如 3.1 → "3.10%"），带正负号。 */
export function formatPct(value: number | null | undefined,
                          digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const text = value.toFixed(digits);
  return value > 0 ? `+${text}%` : `${text}%`;
}

/** 小数比例 → 百分比（0.021 → "+2.10%"），期货的 ret_5d / oi_change_5d 用。 */
export function formatRatioPct(value: number | null | undefined,
                               digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return formatPct(value * 100, digits);
}

/** 分数（1 位小数），保留原值不带正负号。 */
export function formatScore(value: number | null | undefined,
                            digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

/** 涨跌方向 → CSS class（**涨红跌绿**，A 股习惯；与 .crowding-metric-value 同口径）。 */
export function toneOf(value: number | null | undefined): "up" | "down" | "flat" {
  if (value === null || value === undefined || !Number.isFinite(value)) return "flat";
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

/** `20260917` → `2026-09-17`（后端日期一律紧凑格式，展示要可读）。 */
export function formatDate(value: string | null | undefined): string {
  const text = (value ?? "").trim();
  if (text.length !== 8 || !/^\d{8}$/.test(text)) return text || "—";
  return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
}

// ---------------------------------------------------------------- fetch 封装

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch (exc) {
    // `fetch` 只在**网络层**失败时 reject（后端重启/连接被拒/被代理掐断），
    // 浏览器给的原文是 `TypeError: Failed to fetch` —— 对用户零信息量。
    // 网络层文案与报错码统一在 errors.ts（运维提示仅 localhost 可见）。
    void exc;
    throw makeNetworkError(url, false);
  }
  if (!resp.ok) {
    if (resp.status === 401) notifyUnauthorized(url);  // 会话失效 → 退回登录页
    throw apiErrorFromResponse(resp.status, await resp.text());
  }
  return resp.json() as Promise<T>;
}

/** 拼查询串：空值一律不发，避免后端把 `trade_date=` 当成"空字符串"解析出错。 */
function query(params: Record<string, string | number | undefined | null>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    parts.push(`${key}=${encodeURIComponent(String(value))}`);
  }
  return parts.length > 0 ? `?${parts.join("&")}` : "";
}

export const mainlineApi = {
  /** 快照：漏斗统计 + 评分 + 候选 + 精选 + 告警 + 跟踪，一次拿全（主轮询接口）。 */
  snapshot: (params: { tradeDate?: string; top?: number; alertLimit?: number } = {}) =>
    request<MainlineSnapshot>(`${PREFIX}/snapshot${query({
      trade_date: params.tradeDate, top: params.top, alert_limit: params.alertLimit,
    })}`),

  /** 单板块下钻：三层明细 + 历史走势 + 跟踪对象。 */
  board: (code: string, params: { tradeDate?: string; days?: number } = {}) =>
    request<MainlineBoardDetail>(
      `${PREFIX}/board/${encodeURIComponent(code)}${query({
        trade_date: params.tradeDate, days: params.days,
      })}`),

  /** 告警流水（`level` 留空 = 全部；`start`/`end` 是紧凑日期）。 */
  alerts: (params: { level?: string; start?: string; end?: string;
                     limit?: number } = {}) =>
    request<MainlineAlertList>(`${PREFIX}/alerts${query({
      level: params.level, start: params.start, end: params.end,
      limit: params.limit,
    })}`),

  /** 期货先行信号（仪表盘 + 联动矩阵 + 映射 + 期货告警）。 */
  futures: (params: { tradeDate?: string; top?: number } = {}) =>
    request<MainlineFuturesSnapshot>(`${PREFIX}/futures${query({
      trade_date: params.tradeDate, top: params.top,
    })}`),

  /** 回测报告；`runId` 留空 → 最近一次。返回前归一契约（见 `normalizeBacktestReport`）。 */
  backtest: async (runId = "") =>
    normalizeBacktestReport(
      await request<MainlineBacktestReport>(
        `${PREFIX}/backtest${query({ run_id: runId })}`)),

  /** 回测历史列表（只带摘要指标，正文要单独拉）。 */
  backtestList: async (limit = 20) => {
    const payload = await request<{ runs: BacktestRunSummary[] }>(
      `${PREFIX}/backtest/list${query({ limit })}`);
    return { ...payload, runs: asArray(payload.runs).map(normalizeRunSummary) };
  },

  /**
   * 信号收益追踪面板（回测报告页签里的「回测收益展示」）。
   *
   * ⚠️ **分两次调用**：`leaders=false` 只算板块级（约 0.5 秒，先把表格画出来），
   * `leaders=true` 才算区间领涨成分股（约 6~10 秒，结果后端有 10 分钟缓存）。
   * 一次拿全会让首屏为一个增强列白等 6 秒。
   */
  alertReturns: (params: { levels?: string; split?: string; dedupDays?: number;
                           horizons?: string; leaders?: boolean;
                           stockWindow?: number; topLeaders?: number;
                           minWinRate?: number; historyOnly?: boolean;
                           limit?: number; refresh?: boolean } = {}) =>
    request<AlertReturnPayload>(`${PREFIX}/alert-returns${query({
      levels: params.levels, split: params.split, dedup_days: params.dedupDays,
      horizons: params.horizons,
      // false 也要发出去：后端默认值是 false，但显式传更不容易被后续改动带偏
      leaders: params.leaders === undefined ? undefined : String(params.leaders),
      stock_window: params.stockWindow, top_leaders: params.topLeaders,
      min_win_rate: params.minWinRate,
      history_only: params.historyOnly === undefined
        ? undefined : String(params.historyOnly),
      limit: params.limit, refresh: params.refresh === true ? "true" : undefined,
    })}`),

  /**
   * 板块级「20 日胜率过没过门槛」—— 告警流水页签用它隐藏不达标板块的强/中告警。
   *
   * 与 `alertReturns` **共用后端同一份缓存**（两个接口的默认参数逐项一致），
   * 所以先开「回测报告」再开「告警流水」时这一次调用是毫秒级的。
   */
  boardWinRates: (params: { minWinRate?: number; refresh?: boolean } = {}) =>
    request<BoardWinRatePayload>(`${PREFIX}/board-win-rates${query({
      min_win_rate: params.minWinRate,
      refresh: params.refresh === true ? "true" : undefined,
    })}`),

  /** 数据体量与各数据集同步状态（首屏判断"这个模块有没有数据"）。 */
  dataStatus: () => request<MainlineDataStatus>(`${PREFIX}/data/status`),

  /**
   * 手动刷新（立即返回 task_id，后台线程跑）。
   *
   * `tradeDate` 留空 = 后端自己定最新交易日；`syncDays=0` = 增量。
   */
  refresh: (tradeDate = "", syncDays = 0) =>
    request<{ ok: boolean; task_id: string }>(`${PREFIX}/refresh`, {
      method: "POST",
      body: JSON.stringify({ trade_date: tradeDate, sync_days: syncDays }),
    }),

  /** 查询刷新进度（轮询用；`taskId` 留空 → 最近一次任务）。 */
  refreshStatus: (taskId = "") =>
    request<MainlineRefreshStatus>(
      `${PREFIX}/refresh_status${query({ task_id: taskId })}`),

  /**
   * 触发一次 Walk-Forward 回测（后台任务，与刷新共用 `/refresh_status` 轮询）。
   *
   * ⚠️ 这一条**超出任务书给的 9 个接口**：任务书只要求"读回测报告"，但没有这条接口
   * 时前端永远只能读到一个空报告（首次回测没有任何入口），这个视图就是死的。
   * 后端 `mainline.py` 已经实现 `POST /backtest/run`，所以按它的契约补上，
   * 参数全部可选（留空 = 用配置里的默认区间与步长）。
   */
  runBacktest: (params: { start?: string; end?: string; stepDays?: number;
                          save?: boolean } = {}) =>
    request<{ ok: boolean; task_id: string }>(`${PREFIX}/backtest/run`, {
      method: "POST",
      body: JSON.stringify({
        start: params.start ?? "", end: params.end ?? "",
        step_days: params.stepDays ?? 0, save: params.save ?? true,
      }),
    }),
};
