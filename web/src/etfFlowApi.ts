import { apiErrorFromResponse } from "./errors";
import { notifyUnauthorized } from "./unauthorized";

/**
 * ETF 份额监控 API 封装 —— 与后端 `/api/v1/mainline/etf-flow/*` 一一对应。
 *
 * ## 为什么另开一个文件而不是塞进 mainlineApi.ts
 *
 * 后端把这两块挂在**同一个 router** 下（`src/api/routes/mainline.py` 里写明了
 * "与主线挖掘共用这个 router / 这个库"），但前端的契约是两套完全不同的东西：
 * 主线挖掘是"板块评分 + 漏斗 + 告警"，这里是"ETF 份额变化 + 市场环境门控 +
 * 多产品共振"。而 `mainlineApi.ts` 已经 760 行，混在一起后两块都读不动。
 * 展示口径（百分比/涨跌色/日期）**不另写一份**，统一从 `mainlineApi` 引 ——
 * 同一个数在两个面板里显示成不同值是最难查的一类 bug。
 *
 * ## 契约里最容易踩的三件事
 *
 * 1. **份额变化率一律是小数**（`-0.0017` = -0.17%），展示要走 `formatRatioPct()`；
 *    `null` 表示"基准缺失/历史不够"，必须显示"—"，**绝不能显示成 0%**
 *    —— 份额持平与份额没数据是两件事，后者拿去做决策会直接看错方向。
 * 2. **份额单位是万份**（`2365968.77` = 236.6 亿份），既不是"份"也不是"亿份"；
 *    单列一个 `formatSharesWan()`，避免面板各处各写一份换算。
 * 3. 快照生成失败时后端会退回一个**最小载荷**（只有 `trade_date` + 空数组 +
 *    一个兜底 `regime` + `gaps`），`generated_at`/`session_label`/`source_notes`/
 *    `disclaimer` 都不在里面。所以这几个字段如实声明为**可选**，前端不该因为
 *    少一个字段就整块白屏（与 mainlineApi 的"松类型"同一取舍）。
 *
 * ## 接口清单
 *
 * | 用途 | 接口 |
 * | --- | --- |
 * | 快照（环境/指标/分位/信号/共振，一个请求拿全） | `GET /snapshot` |
 * | 落库信号历史（可筛类型/等级/ETF/系列/区间/门控） | `GET /signals` |
 * | 把当日信号写入本地库 | `POST /save` |
 * | 回测报告（单次 / 历史列表 / 触发一次） | `GET /backtest`、`/backtest/list`、`POST /backtest/run` |
 */

const PREFIX = "/api/v1/mainline/etf-flow";

// ---------------------------------------------------------------- 枚举与文案

/** 信号类型；后端判定顺序也是这个顺序（机会 → 风险 → 行业反转）。 */
export const ETF_KIND_ORDER: string[] = [
  "opportunity", "risk", "industry_reversal",
];

/**
 * 信号类型 → 中文标签。
 *
 * 单条信号自带 `kind_label`（后端给），这里只为**分组表头**和**回测分组 key**
 * 准备 —— 与 mainlineApi 的 `LEVEL_TEXT` 同一考虑：后端返回的标签优先，
 * 实在没有时才用这份兜底。
 */
export const ETF_KIND_TEXT: Record<string, string> = {
  opportunity: "🟢 机会信号",
  risk: "🔴 风险信号",
  industry_reversal: "🟡 行业反转警示",
};

/** 市场环境 → 中文标签（回测的 `by_regime` 分组只有 key，没有 label）。 */
export const ETF_REGIME_TEXT: Record<string, string> = {
  bull: "牛市", bear: "熊市", range: "震荡市",
};

/** 信号等级 → 中文短标签（筛选用）。 */
export const ETF_LEVEL_TEXT: Record<string, string> = {
  strong: "🔴 强", medium: "🟡 中", weak: "🟢 弱", none: "未触发",
};

/** 等级名（不带图标）：回测分组标签用，表格里已经有一列图标了。 */
const LEVEL_NAME: Record<string, string> = {
  strong: "强", medium: "中", weak: "弱", none: "未触发",
};

/** 观测等级的语义（`configs/etf_flow.yaml` 的 `level`）：只有核心锚点出机会/风险信号。 */
export const ETF_GROUP_LEVEL_TEXT: Record<string, string> = {
  core: "核心锚点", assist: "重要辅助", industry: "行业主题",
};

/** 回测的"门控对照"分桶：放行 vs 被降级，两桶的差异就是门控的价值。 */
const BUCKET_TEXT: Record<string, string> = {
  opportunity_live: "🟢 机会 · 环境放行",
  opportunity_gated: "🟢 机会 · 已门控",
  risk_live: "🔴 风险 · 环境放行",
  risk_gated: "🔴 风险 · 已门控",
};

/**
 * 回测分组 key → 中文标签。
 *
 * key 有三种形态（后端 `_aggregate()` 拼出来的）：`opportunity`（类型）、
 * `level:strong`（等级）、`opportunity@bear`（类型 × 环境）、`opportunity_gated`
 * （门控对照）。不是查表就原样返回 key —— 后端加了新分桶时前端只是标签不好看，
 * 不会漏行。
 */
export function etfGroupText(key: string): string {
  if (BUCKET_TEXT[key]) return BUCKET_TEXT[key];
  if (key.includes("@")) {
    const [kind, regime] = key.split("@");
    return `${etfGroupText(kind)} @ ${ETF_REGIME_TEXT[regime] ?? regime}`;
  }
  if (key.startsWith("level:")) {
    const level = key.slice("level:".length);
    return `等级 · ${LEVEL_NAME[level] ?? level}`;
  }
  return ETF_KIND_TEXT[key] ?? ETF_REGIME_TEXT[key] ?? key;
}

// ---------------------------------------------------------------- 快照

/** 市场环境判定（`snapshot.regime`）。 */
export type EtfFlowRegime = {
  /** bull / bear / range */
  key: string;
  label: string;
  /** 参考指数；最小载荷里没有 */
  index_code?: string;
  index_name?: string;
  trade_date?: string;
  /** 环境窗口（交易日），默认 120 */
  window?: number;
  /** 近 window 日收益（**小数**）；历史不足时 null */
  change?: number | null;
  close?: number | null;
  /** 机会信号在本环境下是否放行（false = 只进观察列表，不构成仓位建议） */
  opportunity_allowed: boolean;
  risk_allowed: boolean;
  /** 最小载荷里的 regime 连 gaps 都没有，所以可空 */
  gaps?: string[];
};

/** 一只 ETF 在某交易日的份额指标（`snapshot.indicators`）。 */
export type EtfFlowIndicator = {
  code: string;
  name: string;
  /** 所属观测系列（hs300 / sse50 / industry…） */
  group: string;
  /** core / assist / industry */
  level: string;
  trade_date: string;
  /** 份额（**万份**）；无数据时 null */
  shares: number | null;
  /**
   * `shares` 实际取自哪个交易日。与 `trade_date` 不同时说明**当天份额还没发布**
   * （交易所次日 8:30 更新），卡片上的是上一交易日的口径 —— 界面必须显式标出，
   * 否则用户会拿昨天的份额配今天的收盘做判断。
   */
  shares_date?: string;
  close: number | null;
  /** 以下 change_* 都是**小数**（0.0218 = +2.18%） */
  change_1d: number | null;
  change_5d: number | null;
  change_10d: number | null;
  change_20d: number | null;
  /** 当日成交额（元） */
  amount: number | null;
  /** 放量倍数（相对近 5 日均值） */
  amount_ratio: number | null;
  gaps: string[];
};

/** 宽基指数的位置分位（`snapshot.positions`）。 */
export type EtfFlowPosition = {
  /** 指数代码，如 000300.SH */
  code: string;
  name: string;
  trade_date: string;
  close: number | null;
  /** 0~1，越大越接近窗口高位；样本不足时 null */
  percentile: number | null;
  /** range / rank */
  mode: string;
  window: number;
  high: number | null;
  low: number | null;
  gaps: string[];
};

/** 一条 ETF 份额信号（`snapshot.signals` 与回测 records 共用）。 */
export type EtfFlowSignal = {
  alert_id: string;
  /** 触发日（注意字段名是 `date` 而不是 `trade_date`，落库表里才叫 trade_date） */
  date: string;
  code: string;
  name: string;
  group: string;
  group_label: string;
  /** opportunity / risk / industry_reversal */
  kind: string;
  kind_label: string;
  /** strong / medium / weak / none */
  level: string;
  level_label: string;
  index_code: string;
  index_name: string;
  index_percentile: number | null;
  change_1d: number | null;
  change_5d: number | null;
  /** 同系列同向的 ETF 数量 / 该系列总数 */
  resonance: boolean;
  resonance_count: number;
  resonance_total: number;
  /** 触发时的市场环境 key */
  regime: string;
  regime_label: string;
  /**
   * true = 因环境不支持被**降级为观察**，不构成仓位建议。
   * 前端必须把它画成与正常告警明显不同的样子 —— 见 EtfFlowSignals 的注释。
   */
  gated: boolean;
  /** 人类可读的触发理由（逐条展示，不要合并成一段） */
  reasons: string[];
  /**
   * 回测回填的后续收益 `{"5": 0.02, ...}`。
   * 泛型写 `number | null` 而不是 `number`：后端 `forward` 的每个观察期在窗口
   * 不够长时就是 None，写死成 number 会让前端在渲染时按 0 处理。
   */
  forward: Record<string, number | null>;
};

/** 多产品共振指示灯的一行（`snapshot.resonance`）。 */
export type EtfFlowResonanceGroup = {
  group: string;
  label: string;
  /** core / assist / industry */
  level: string;
  /** 该组对应的宽基指数（行业组为空串） */
  index: string;
  index_name: string;
  /** 当日份额增加 / 减少的只数，以及该系列总只数 */
  rising: number;
  falling: number;
  total: number;
  resonance: boolean;
  /** in = 净申购方向 / out = 净赎回方向 / flat = 多空相当或无数据 */
  direction: string;
  etfs: {
    code: string;
    name: string;
    change_1d: number | null;
    change_5d: number | null;
    direction: string;
  }[];
};

/** `GET /snapshot` 的完整返回。 */
export type EtfFlowSnapshot = {
  trade_date: string;
  /** 以下 4 个字段在"快照生成失败"的最小载荷里没有，故声明可选 */
  generated_at?: string;
  session_label?: string;
  source_notes?: string[];
  disclaimer?: string;
  regime: EtfFlowRegime;
  indicators: EtfFlowIndicator[];
  positions: EtfFlowPosition[];
  signals: EtfFlowSignal[];
  resonance: EtfFlowResonanceGroup[];
  signal_count: number;
  /** 会真正告警的条数（强/中等级**且未被门控**） */
  alert_count: number;
  gated_count: number;
  /** 必须展示：它解释了"为什么今天没有告警" */
  gaps: string[];
  /** 命中服务端 TTL 缓存时为 true */
  cached?: boolean;
  cache_age?: number;
};

// ---------------------------------------------------------------- 落库信号历史

/** `GET /signals` 的一行（外层是索引字段，完整信号在 `payload`）。 */
export type EtfFlowSignalRow = {
  alert_id: string;
  trade_date: string;
  code: string;
  name: string;
  group: string;
  kind: string;
  level: string;
  regime: string;
  gated: boolean;
  resonance: boolean;
  index_percentile: number | null;
  change_1d: number | null;
  change_5d: number | null;
  updated_at: string;
  /**
   * 完整 `FlowSignal.to_dict()`。后端逐行解析 payload，**不是 JSON 对象的行会被
   * 整行跳过**（见 storage.py 的 `_payload`），所以拿到手的行一定有 payload。
   */
  payload: EtfFlowSignal;
};

/** `GET /signals` 的返回。 */
export type EtfFlowSignalHistory = {
  signals: EtfFlowSignalRow[];
  count: number;
  /** 存储不可用时后端只回 `{signals: [], count: 0, gaps: [...]}` */
  latest_trade_date?: string;
  gaps: string[];
};

/** `POST /save` 的返回。 */
export type EtfFlowSaveResult = {
  trade_date: string;
  /** 实际写入（upsert）的行数 */
  written: number;
  /** 本次快照算出的信号条数（写入前的数量） */
  signal_count: number;
};

// ---------------------------------------------------------------- 回测

/** 一个观察期的统计。字段全部可空 —— 样本为 0 时后端留空而不是编一个数。
 *
 * ⚠️ `independent` / `win_rate_low` / `win_rate_high` 对**旧回测记录**是
 * `undefined`：它们落库时还没有这几个字段。声明成可选而不是 `| null`，
 * 就是为了让"没这个数"和"这个数是空的"在类型上分开 —— 后者会被渲染成
 * `—`（不表态），前者应当整列不显示。
 */
export type EtfFlowHorizonStats = {
  /** 观察期（交易日）：5 / 10 / 20 / 34 */
  horizon: number;
  samples: number;
  /**
   * **前瞻窗口互不重叠**的独立时段个数。
   *
   * 这是整张表的可信度指标：`samples` 是"多少条信号"，它是"几个独立事件"。
   * 判据用"份额 5 日累计"而份额是存量，一次资金流入会让同一事件连续多日超标；
   * 同系列 ETF 又常在同一天一起触发 —— 两者叠加会让 `samples` 虚高数倍。
   * 实测 `opportunity_live` 是 66 条信号 / 3~5 个独立时段。
   */
  independent?: number | null;
  /** T 收盘买入的中位数 / 均值（小数） */
  median: number | null;
  mean: number | null;
  /** 胜率（0~1） */
  win_rate: number | null;
  /** 胜率的 Wilson 95% 置信区间。小样本下必须和点估计一起看 */
  win_rate_low?: number | null;
  win_rate_high?: number | null;
  false_positive_rate: number | null;
  /** T+1 收盘买入口径（更保守） */
  median_t1: number | null;
  win_rate_t1: number | null;
  /** 后端实际用来判达标的门槛（已按信号方向取，见 `kind`） */
  target_median: number | null;
  target_win_rate: number | null;
  /** 该桶的信号类型；混合类型的桶为空串 */
  kind?: string;
  /** null = 没配目标或中位数为空（此时**不表态**，不要当未通过） */
  passed: boolean | null;
};

/** 回测明细的一条记录：`FlowSignal` 的字段**平铺** + 后续表现。 */
export type EtfFlowBacktestRecord = EtfFlowSignal & {
  forward: Record<string, number | null>;
  forward_t1: Record<string, number | null>;
  /** 窗口内最大浮盈 / 最大浮亏（小数） */
  max_gain: number | null;
  max_drawdown: number | null;
  /**
   * 这一条代表的事件**连续报了多少个交易日**（`>1` 说明报表已把后面几天
   * 合并进来）。判据用"份额 5 日累计"，一次流入会让累计值连续多日超标，
   * 所以不合并的话同一事件会占好几行、把独立样本数虚增好几倍。
   */
  repeat_days?: number;
  /** 合并进来的最后一天（`repeat_days > 1` 时有值） */
  repeat_until?: string;
};

/** `by_kind` / `by_etf` / `by_regime` / `by_kind_regime` 的公共结构。 */
export type EtfFlowHorizonTable = Record<string, Record<string, EtfFlowHorizonStats>>;

/** 回测结果主体（落库时被序列化进 `result` 列）。 */
export type EtfFlowBacktestResult = {
  by_kind: EtfFlowHorizonTable;
  by_etf: EtfFlowHorizonTable;
  by_regime: EtfFlowHorizonTable;
  by_kind_regime: EtfFlowHorizonTable;
  /**
   * 样本内 / 样本外对照表，键形如 `"in:opportunity@bear"` / `"out:risk_live"`。
   *
   * 阈值全是看着全区间结果调出来的，所以只有切分点**之后**那段才算真样本外。
   * 旧回测记录没有这张表（`undefined`），此时整张卡不渲染。
   */
  validation?: EtfFlowHorizonTable;
  /** 样本内/外切分点（`YYYYMMDD`）；空 = 本次未切分 */
  split?: string;
  /** 明细保留**最近** 300 条（新的在前）；同一事件连报多日已合并 */
  records: EtfFlowBacktestRecord[];
  record_count: number;
  /** 与 `record_count` 同义（两个名字指同一个数，后端都给了） */
  signals?: number;
  /** 因"同一事件连报多日"被合并掉的行数（0 = 未合并） */
  collapsed?: number;
};

/** `GET /backtest` 的返回。 */
export type EtfFlowBacktestReport = {
  run_id: string;
  /** 从未跑过回测时后端只回 `{run_id: "", gaps: [...]}`，下面这些确实会缺席 */
  range_start?: string;
  range_end?: string;
  started_at?: string;
  finished_at?: string;
  seconds?: number | null;
  days?: number;
  signals?: number;
  error?: string;
  gaps: string[];
  result?: EtfFlowBacktestResult;
  /** 只在 `with_markdown=true` 时返回；面板目前不请求（见 EtfFlowBacktest 的注释） */
  markdown?: string;
};

/** `GET /backtest/list` 的一行（摘要，不含 result / markdown）。 */
export type EtfFlowBacktestRun = {
  run_id: string;
  range_start: string;
  range_end: string;
  started_at: string;
  finished_at: string;
  seconds: number | null;
  days: number;
  signals: number;
  error: string;
  gaps: string[];
};

/** 后台任务（回测/保存共用 `/refresh_status` 轮询）。 */
export type EtfFlowTask = {
  task_id: string;
  /** running / done / failed */
  state: string;
};

// ---------------------------------------------------------------- 展示工具

/**
 * 份额（万份）→ 千分位数字串（不带单位，单位写在表头/标签里）。
 *
 * 小数位按量级自适应：份额从 60 万份到 700 万份都有，固定 2 位小数在百万级是
 * 噪声（0.77 万份 = 7700 份，对判断申赎毫无意义），固定 0 位又会让小 ETF 的
 * 精度变差。`null` 一律 "—"。
 */
export function formatSharesWan(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const digits = abs >= 100000 ? 0 : abs >= 10000 ? 1 : 2;
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/**
 * 指数分位（0~1）→ "8.8%"。
 *
 * 刻意**不加正负号**（`formatPct` 会给正数补 "+"）：分位是"位置"不是"涨跌"，
 * "在近 34 日的 8.8% 位置"与"涨了 8.8%"是两回事。`null` → "—"。
 */
export function formatPercentile(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/**
 * 份额方向 → 涨跌色 class。
 *
 * 沿用项目口径（**涨红跌绿**，与 `.crowding-metric-value` 同）：份额增加 = 申购 =
 * `up`（红），份额减少 = 赎回 = `down`（绿）。认不出的值一律 `flat` 走灰，
 * 不猜方向。
 */
export function directionTone(direction: string): "up" | "down" | "flat" {
  const key = (direction || "").toLowerCase();
  if (key === "in") return "up";
  if (key === "out") return "down";
  return "flat";
}

/**
 * 系列 → 指数代码的映射。
 *
 * ETF 指标本身不带指数，只有共振数组里有 `index`（观测清单里配的），
 * 所以"这只 ETF 对应哪个指数分位"只能这样对上。行业组的 index 是空串，
 * 对不上就是"—"（行业 ETF 的份额变化不做分位判断，这是后端口径）。
 */
export function indexByGroup(
  resonance: EtfFlowResonanceGroup[],
): Record<string, string> {
  const map: Record<string, string> = {};
  for (const item of resonance) {
    if (item.index) map[item.group] = item.index;
  }
  return map;
}

/** 指数代码 → 分位行（给指标卡/明细表补"对应指数分位"）。 */
export function positionByIndex(
  positions: EtfFlowPosition[],
): Record<string, EtfFlowPosition> {
  const map: Record<string, EtfFlowPosition> = {};
  for (const item of positions) map[item.code] = item;
  return map;
}

/**
 * 命中/未命中门控的原因（按信号类型说）。
 *
 * 后端只给一个 `gated` 布尔值，而**机会与风险的门控是两条不同的规则**
 * （机会只在熊市放行；风险默认排除熊市）。前端如果统一写"机会信号只在熊市有效"，
 * 被门控的**风险**信号就会被误读成另一条规则 —— 所以原因按类型给，
 * 且只在 tooltip 里讲，正文只留一个「已门控」标签。
 */
export function etfGateReason(kind: string): string {
  if (kind === "opportunity") {
    return "机会信号只在熊市放行：其余环境回测方向相反，故降级为观察项";
  }
  if (kind === "risk") {
    return "风险信号在熊市不放行：该格回测方向相反（高位赎回后往往反弹）";
  }
  return "该类型信号在当前市场环境下不放行，只进观察列表";
}

// ---------------------------------------------------------------- fetch 封装

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    if (resp.status === 401) notifyUnauthorized(url);  // 会话失效 → 退回登录页
    throw apiErrorFromResponse(resp.status, await resp.text());
  }
  return resp.json() as Promise<T>;
}

/** 拼查询串：undefined 一律不发，避免后端把 `kind=` 当成一个空筛选值。 */
function query(params: Record<string, string | number | undefined | null>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    parts.push(`${key}=${encodeURIComponent(String(value))}`);
  }
  return parts.length > 0 ? `?${parts.join("&")}` : "";
}

/** 布尔参数必须显式转字符串：`false` 是要发给后端的值，不能被当成"空"丢掉。 */
function boolParam(value: boolean | undefined): string | undefined {
  return value === undefined ? undefined : String(value);
}

export const etfFlowApi = {
  /**
   * 快照（面板主接口）。
   *
   * `refresh=true` 跳过服务端 TTL 缓存强制重算 —— 份额数据是次日早间更新的，
   * 盘后手动点一次"立即刷新"就该看到最新值，而不是五分钟前的缓存。
   */
  snapshot: (params: { tradeDate?: string; refresh?: boolean } = {}) =>
    request<EtfFlowSnapshot>(`${PREFIX}/snapshot${query({
      trade_date: params.tradeDate, refresh: boolParam(params.refresh),
    })}`),

  /**
   * 落库信号历史（新的在前）。
   *
   * 默认 `alertsOnly=true`（只返回会真正告警的）；复盘要看"门控挡住了什么"时
   * 传 `false`，`gated` 字段会如实标出哪些不构成仓位建议。
   */
  signals: (params: { kind?: string; level?: string; code?: string;
                      group?: string; start?: string; end?: string;
                      gated?: boolean; alertsOnly?: boolean;
                      limit?: number } = {}) =>
    request<EtfFlowSignalHistory>(`${PREFIX}/signals${query({
      kind: params.kind, level: params.level, code: params.code,
      group: params.group, start: params.start, end: params.end,
      gated: boolParam(params.gated), alerts_only: boolParam(params.alertsOnly),
      limit: params.limit,
    })}`),

  /** 把当日快照的信号写入本地库（幂等 upsert），返回写入行数。 */
  save: (tradeDate = "") =>
    request<EtfFlowSaveResult>(
      `${PREFIX}/save${query({ trade_date: tradeDate })}`, { method: "POST" }),

  /** 回测报告；`runId` 留空 → 最近一次。 */
  backtest: (runId = "") =>
    request<EtfFlowBacktestReport>(
      `${PREFIX}/backtest${query({ run_id: runId })}`),

  /** 回测历史列表（摘要，不含 result）。 */
  backtestList: (limit = 20) =>
    request<{ runs: EtfFlowBacktestRun[]; gaps?: string[] }>(
      `${PREFIX}/backtest/list${query({ limit })}`),

  /**
   * 触发一次回测（后台任务，立即返回 task_id）。
   *
   * start/end 留空 = 用配置里的区间（`configs/etf_flow.yaml` 的 backtest.start
   * 到本地数据终点）；面板只提供"跑一次"，不在这里开日期输入框。
   * 轮询走主线挖掘那一个共用的 `mainlineApi.refreshStatus`（后端三类任务
   * 共用一张任务表，前端也共用一处轮询）。
   */
  runBacktest: (params: { start?: string; end?: string } = {}) =>
    request<EtfFlowTask>(`${PREFIX}/backtest/run`, {
      method: "POST",
      body: JSON.stringify({ start: params.start ?? "", end: params.end ?? "" }),
    }),
};
