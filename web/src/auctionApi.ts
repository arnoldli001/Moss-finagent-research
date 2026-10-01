import { notifyUnauthorized } from "./unauthorized";

/**
 * 集合竞价选股 API 客户端。
 *
 * 路径前缀 `/api/v1/auction_select`（后端 `APIRouter(prefix="/api/v1/...")`，
 * 与项目其它模块一致 —— `api_router` 本身不加前缀）。
 */

const PREFIX = "/api/v1/auction_select";

export interface AuctionDimNotes {
  [key: string]: string;
}

/** 单个模式的拟合明细（为什么命中 / 为什么否决）。 */
export interface ModeFit {
  mode: string;
  name: string;
  branch: string;
  eligible: boolean;
  score: number;
  checks: { rule: string; passed: boolean; detail: string }[];
  blockers: string[];
  entry_hint: string;
  exit_hint: string;
  source: string[];
}

export interface AuctionFeature {
  code?: string;
  name?: string;
  auction_volume_ratio?: number | null;
  auction_amount?: number | null;
  auction_amount_ratio?: number | null;
  auction_volume_hand?: number | null;
  auction_volume_vs_yesterday?: number | null;
  open_gap_pct?: number | null;
  open_price?: number | null;
  pre_close?: number | null;
  takeover_score?: number | null;
  price_slope?: number | null;
  unmatched_buy_hand?: number | null;
  unmatched_sell_hand?: number | null;
  unmatched_ratio?: number | null;
  pattern?: string | null;
  /** 竞价形态判定明细：振幅/首末价/是否因量不足被挡下（JSON） */
  pattern_meta?: Record<string, unknown> | null;
  /** 跳空值 = 09:25 正式撮合价 ÷（09:24:40~09:24:55 撮合均价） */
  jump_gap?: number | null;
  jump_gap_points?: number | null;
  jump_window_mean?: number | null;
  jump_base_price?: number | null;
  jump_fallback?: boolean | null;
  /** 抢筹 / 抢跑主标签（空串表示未打标） */
  rush_tag?: string | null;
  /** 全部抢筹族标签：抢筹 / 大量抢筹 / 无量抢筹 / 大单抢筹 / 抢跑 */
  rush_labels?: string[] | null;
  /** 是否属于正向抢筹族（豁免量比/低开/主板高开三条否决） */
  is_rush_positive?: boolean | null;
  rush_note?: string | null;
  is_one_word_board?: boolean | null;
  prev_limit_up_streak?: number | null;
  prev_board_text?: string | null;
  prev_seal_amount?: number | null;
  prev_amount?: number | null;
  prev_seal_to_float_ratio?: number | null;
  theme_heat?: number | null;
  theme_name?: string | null;
  theme_limit_up_count?: number | null;
  theme_reason?: string | null;
  theme_all?: string[];
  circulating_market_value?: number | null;
  is_weak_to_strong?: boolean | null;
  is_strong_to_weak?: boolean | null;
  market_stage?: string | null;
  market_temperature?: number | null;
  market_limit_up_count?: number | null;
  market_broken_rate?: number | null;
  degraded?: string[];
  // ---- 新增维度相关 ----
  turnover_rate?: number | null;
  theme_highest_ladder?: number | null;
  theme_is_leader?: boolean | null;
  // ---- 自定义战术标签（追击式/破冰式/无极式/龙回式/反核/趋龙）----
  // 语料 344 个文件里逐词检索 0 命中，定义来自本模块，`tactics_unverified`
  // 恒为 true，前端据此加"需你确认"的提示。
  tactics?: string[];
  tactic_notes?: Record<string, string>;
  tactics_unverified?: boolean;
  amplitude?: number | null;
  // ---- 新龙头 / 有梯队（市场级标签下发到龙头那一只）----
  is_new_leader?: boolean;
  has_tier?: boolean;
  tier_count?: number | null;
  leader_detail?: string;
  // ---- 模式（**互斥分类，不是加权求和**）----
  // 由择时（情绪周期阶段）裁定该用哪一套，单一模式 + 该模式机会分。
  mode?: string;
  mode_name?: string;
  mode_branch?: string;
  mode_score?: number;
  mode_reason?: string;
  mode_fits?: ModeFit[];
  mode_context?: Record<string, unknown>;
  is_market_top_board?: boolean;
  market_max_streak?: number | null;
}

export interface AuctionPick {
  trade_date: string;
  code: string;
  name: string;
  rank: number;
  total_score: number;
  decision: string;
  buy_reason: string;
  dim_scores: Record<string, number>;
  dim_notes: AuctionDimNotes;
  features: AuctionFeature;
  vetoed: boolean;
  veto_reasons: string[];
}

export interface AuctionRun {
  trade_date: string;
  status: string;
  preheat_at: string;
  started_at: string;
  finished_at: string;
  on_time: boolean;
  candidates: number;
  scored: number;
  picked: number;
  seconds: number;
  market_stage: string;
  market_temperature: number | null;
  gaps: string[];
  error: string;
  /**
   * 当前情绪周期在**发酵哪个题材**（用户口径 2026-09-22）。
   *
   * ⚠️ 只在发酵/加速/分歧/主升这些阶段才有值；退潮/冰点/高位震荡期
   * 后端**不给结论**（那时谈"发酵题材"与阶段自相矛盾），所以这里是空对象。
   *
   * ⚠️ 样本只有「昨日涨停 + 今日 9:25 涨停」那几十只（不是全市场题材榜），
   * 所以展示时**必须带上家数/最高板这些数字**，不能只给一个题材名。
   */
  fermenting_theme?: FermentingTheme;
}

export interface FermentingThemeCandidate {
  /** 涨停原因（题材名，来自 f10 的 `limit_reason`）。 */
  theme: string;
  /** 排序分 = 最高板 × 2 + 家数 + 今日9:25涨停 × 0.5（后端标定）。 */
  score: number;
  /** 家数：昨日涨停 + 今日 9:25 涨停里属于该题材的只数。 */
  count: number;
  /** 其中**今日 9:25 就封板**的只数（最新鲜的资金信号）。 */
  today_count: number;
  /** 该题材内最高板。 */
  highest: number;
  leader: string;
  leader_name: string;
  /** 昨日涨停池里属于该题材的代码（选股对象；今日涨停的不在这里面）。 */
  members: string[];
  /** 题材热度 0~1（与个股 `theme_heat` 同一刻度）。 */
  heat: number;
}

export interface FermentingTheme {
  available: boolean;
  candidates: FermentingThemeCandidate[];
  /** 口径说明：样本大小 + 排序公式（跟着结论一起展示，别让人只信一个名次）。 */
  note?: string;
}

/**
 * 手动关注列表的一只票（用户口径 2026-09-22）。
 *
 * 用户需求："在竞价选股池下方，要支持用户手动添个股功能，在重跑一次触发时，
 * 同步给这些股打分和输出操作建议到前端（决策要不要卖，是冲高卖还是回踩冲高、
 * 还是加仓。要求文字简洁控制字数在 50 字内）。"
 *
 * ⚠️ `score` 为 `null` = **还没打过分**（刚加进来、还没重跑）。前端要显示
 * "待重跑一次后打分"，**不能显示 0 分** —— 那会被读成"这只票很差"。
 */
export interface WatchItem {
  code: string;
  name: string;
  note: string;
  added_at: string;
  score: WatchScore | null;
}

export interface WatchScore {
  trade_date: string;
  code: string;
  name: string;
  total_score: number;
  /** 打分链路的决策（buy / watch / reject）；建议档位看 `advice` 的文字。 */
  decision: string;
  /** 操作建议，≤50 字（后端已按字符截断）。 */
  advice: string;
  /** 建议依据（分项信号）：用来解释"为什么是这句话"。 */
  advice_basis: string[];
  dim_scores: Record<string, number>;
  dim_notes: Record<string, string>;
  degraded: string[];
}

export interface AuctionToday {
  trade_date: string;
  picked: AuctionPick[];
  /** 够门槛、也没被否决，但排在 `pool_size` 之外（分数从高到低）。 */
  overflow?: AuctionPick[];
  /** 前置筛选剔除（无分数、无特征）：昨收价/市值/ST 等四条件。 */
  prefiltered?: AuctionPick[];
  near_miss: AuctionPick[];
  vetoed?: AuctionPick[];
  counts?: {
    picked: number;
    near_miss: number;
    overflow?: number;
    prefiltered?: number;
    vetoed: number;
  };
  run: AuctionRun | null;
  threshold?: number;
  message?: string;
}

export interface AuctionSeriesPoint {
  time: string;
  time_seconds: number;
  price: number | null;
  matched: number | null;
  unmatched: number | null;
  direction: number | null;
}

export interface AuctionDetail {
  trade_date: string;
  code: string;
  snapshot?: {
    name?: string;
    open_price?: number | null;
    open_change_pct?: number | null;
    open_volume_hand?: number | null;
    open_amount?: number | null;
    pre_close?: number | null;
    circulating_market_value?: number | null;
    source?: string;
    series: AuctionSeriesPoint[];
    series_meta: Record<string, unknown>;
  };
  feature?: AuctionFeature;
  pick?: AuctionPick;
}

export interface AuctionState {
  started: boolean;
  threads: string[];
  running: boolean;
  runs: number;
  last_run_date: string;
  last_run_at: string;
  last_status: string;
  last_error: string;
  preheat_ready: boolean;
  preheat_trade_date: string;
  preheat_summary: Record<string, unknown> | null;
}

export interface AuctionInfo {
  module: string;
  tables: Record<string, number>;
  latest_trade_date: string;
  latest_run: AuctionRun | null;
  scheduler: AuctionState;
  schedule: {
    enabled: boolean;
    preheat_at: string;
    trigger: string;
    deadline: string;
  };
  universe: {
    min_market_cap: number;
    max_market_cap: number;
    /** 前置筛选（用户口径 2026-09-19 规则 1）：昨日收盘价上限。 */
    max_prev_close_price?: number;
    /** 前置筛选第 5 条：昨收必须 > 该窗口的均线（默认 20 日）。 */
    require_above_ma?: boolean;
    ma_window?: number;
    /** 市值门槛的口径说明（前置筛选用昨收，展示/打分用 9:25）。 */
    cap_prefilter_basis?: string;
    exclude_st: boolean;
    require_yesterday_limit_up: boolean;
  };
  score: {
    min_total_score: number;
    pool_size: number;
    /** 抢筹（常规档）加分；大量抢筹/大单抢筹走 `rush_bonus_heavy`。 */
    rush_bonus?: number;
    rush_bonus_heavy?: number;
    weights: Record<string, number>;
  };
  veto: Record<string, number>;
}

/** 该阶段的判据（来自 skill `cycle.json` 的 `cycle_stages[].indicators`）。 */
export interface StageIndicator {
  name: string;
  /** 判据的原文描述（含阈值口径）。 */
  rule: string;
  metric?: string;
  op?: string | null;
  value?: number | null;
  /** 附加口径（如绝对冰点阈值、金叉公式）。 */
  extra?: Record<string, unknown> | null;
  /** 出处（资料文件:行号）。 */
  source?: string;
}

/**
 * 情绪周期阶段的 skill 操作指南（仓位 / 择股方向 / 风险）。
 *
 * 来自 `skills/jianmen-shortterm/references/`：
 * - `cycle.json` → `action`（该干什么，含仓位口径）+ `indicators`（阶段判据）；
 * - `modes.json` → `position_hint`（仓位管理）+ `best_modes` / `secondary_modes`
 *   （择股方向）+ `avoid`（该避开的 → 风险）。
 *
 * 后端**原样透传**这两份资料，不做合并改写。
 */
export interface StageGuide {
  available: boolean;
  /** 原始阶段名（`market_cycle` 的做T口径）。 */
  stage: string;
  /** 归一后的择时链条阶段名；与 `stage` 不同说明做过口径映射。 */
  phase: string;
  definition: string;
  action: string;
  indicators: StageIndicator[];
  position_hint: string;
  best_modes: string[];
  secondary_modes: string[];
  avoid: string[];
  /** 命中到的择时矩阵行名（带括号说明的长串）。 */
  timing_stage: string;
  source: { cycle: string; timing: string };
  /** 非空表示只命中了一半资料或缺失原因。 */
  gap: string;
}

/** 情绪周期五线（`/sentiment_cycle`）：**主板口径**，日频（最后一个点通常为上一交易日）。 */
export type AuctionSentimentCycle = {
  available: boolean;
  source: string;
  /** 统计范围说明，如「主板（10CM）」 */
  scope: string;
  seconds?: number;
  /** 交易日列表（`YYYYMMDD`，升序） */
  days: string[];
  series: {
    /** 大肉数：涨停 或 (现价−最低价)/最低价 ≥ 10% */
    win: number[];
    /** 大面数：跌停 或 (最高价−现价)/最高价 ≥ 10%，且近 7 天涨停 */
    loss: number[];
    /** 连板数：≥2 连板家数（剔新股与 ST） */
    boards: number[];
    /** 小周期：当日最高板身位 */
    height: number[];
    /** 大周期：连板压力高度 */
    pressure: number[];
    limit_up: number[];
    limit_down: number[];
  };
  /** 每个交易日「最高板」的那几只（最多 3 个名字，供 tooltip 显示） */
  leaders: Record<string, string[]>;
  stage: {
    date?: string; height?: number; pressure?: number; prev_pressure?: number;
    win?: number;
    win_ma5?: number; breakout?: boolean; win_rising?: boolean; label?: string;
  };
  gaps: string[];
  disclaimer?: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(PREFIX + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized(PREFIX + path);  // 会话失效 → 退回登录页
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload?.detail) detail = String(payload.detail);
    } catch {
      // 响应不是 JSON：保留状态码文案
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const auctionApi = {
  info: () => request<AuctionInfo>(""),
  today: (tradeDate = "") =>
    request<AuctionToday>(`/today${tradeDate ? `?trade_date=${tradeDate}` : ""}`),
  detail: (code: string, tradeDate = "") =>
    request<AuctionDetail>(
      `/detail/${code}${tradeDate ? `?trade_date=${tradeDate}` : ""}`),
  runs: (limit = 20) => request<{ runs: AuctionRun[]; count: number }>(
    `/runs?limit=${limit}`),
  state: () => request<AuctionState>("/state"),
  /**
   * 该情绪周期阶段的 skill 操作指南（仓位/方向/风险）。
   *
   * 传 `stage` 用当前那一轮的阶段；留空则后端回落到最近一轮运行记录的阶段。
   */
  stageGuide: (stage = "") =>
    request<StageGuide>(
      `/stage_guide${stage ? `?stage=${encodeURIComponent(stage)}` : ""}`),
  preheat: (force = false) =>
    request<{ summary: Record<string, unknown> }>(
      `/preheat?force=${force ? "true" : "false"}`),
  run: (tradeDate = "", force = false) =>
    request<Record<string, unknown>>("/run", {
      method: "POST",
      body: JSON.stringify({ trade_date: tradeDate, force }),
    }),
  /**
   * 手动关注列表 + 最近一轮的打分与建议（用户口径 2026-09-22）。
   *
   * 列表跨日保留，分数是"最近一轮重跑"的产物 —— 所以加完票要先重跑一次
   * 才有分数（前端对 `score === null` 显示"待重跑一次后打分"）。
   */
  watch: () => request<{ trade_date: string; items: WatchItem[]; count: number }>(
    "/watch"),
  watchAdd: (code: string, name = "", note = "") =>
    request<{ ok: boolean; code: string; name: string; created: boolean; message: string }>(
      "/watch", { method: "POST", body: JSON.stringify({ code, name, note }) }),
  watchRemove: (code: string) =>
    request<{ ok: boolean; code: string; message: string }>(
      `/watch/${encodeURIComponent(code)}`, { method: "DELETE" }),
  /**
   * 情绪周期五线（大肉 / 大面 / 连板数 / 小周期 / 大周期）—— **主板口径**。
   *
   * 口径出自《情绪周期表的用法》，按用户口径只统计主板（北证/创业/科创不计入）。
   * 数据是本地行情仓的**日频**数据（收盘后落库），所以最后一个点通常落在上一交易日。
   */
  sentimentCycle: (days = 30) =>
    request<AuctionSentimentCycle>(`/sentiment_cycle?days=${days}`),
};

/** 维度中文名（前端展示用；与后端 `scoring.score_candidate` 的键一致）。 */
export const DIM_LABELS: Record<string, string> = {
  auction_strength: "竞价量能",
  price_position: "开盘位置",
  takeover: "承接强度",
  theme_heat: "题材热度",
  sentiment: "情绪周期",
  auction_sentiment: "竞价情绪",
  ladder: "连板梯队",
  previous_day: "昨日质量",
  capital_fit: "盘口适配",
  seal_flow_ratio: "封流比",
  turnover: "换手率",
  theme_leader: "题材龙头",
};

/**
 * 各维度满分（分项条按它归一）。
 *
 * ⚠️ 必须与后端 `auction_select/config.yaml` 的 `score.weights` 保持一致
 * （和 = 100）。改了一边忘了另一边，分项条的长度就会说谎。
 */
export const DIM_MAX: Record<string, number> = {
  auction_strength: 19,
  price_position: 14,
  takeover: 12,
  theme_heat: 12,
  sentiment: 7,
  auction_sentiment: 5,
  ladder: 10,
  previous_day: 5,
  capital_fit: 4,
  seal_flow_ratio: 6,
  turnover: 3,
  theme_leader: 3,
};
