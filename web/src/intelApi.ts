/**
 * 舆情情报中心 API（对接 `/api/v1/intel/*`）。
 *
 * ## 为什么单独一个文件而不是塞进 `api.ts`
 *
 * `api.ts` 已经 2700+ 行。情报中心是一组**边界清晰**的只读接口
 * （+ 一个处置动作），而且它的响应契约里有若干"刻意不存在"的字段
 * （见下），值得把这份契约单独写清楚，而不是混进大文件里。
 *
 * ## ⚠️ 响应里**不存在**的字段（这是设计，不是遗漏）
 *
 * `source_url` / `report_url` / `group_id` / `author_id` / `topic_id`
 * 以及任何真名 —— 后端在**契约层**就不构造它们（白名单式 `to_public()`）。
 *
 * 所以前端也**不要**去猜、不要显示、不要从别的字段拼出来：
 *
 *   · `source_alias` 是**稳定假名**（`src-3f9a2c1b`），只用于"这条与那条
 *     是否同源"的分组与去重，**显示时一律换成中文类型名**
 *     （`kind_label`，如"财经快讯"），绝不把假名印在界面上。
 *   · 假名**不是打码成 `***`** —— 那样所有来源会塌成同一个值，
 *     按源分组/去重直接失效。所以它保留了区分度，也正因如此
 *     它**有信息量**，不能随手展示。
 */

import { readCsrfToken } from "./api";
import { apiErrorFromResponse } from "./errors";
import { notifyUnauthorized } from "./unauthorized";

const BASE = "/api/v1/intel";

/**
 * 可信度（**规则层打分，可复算**）。
 *
 * 它只回答一个问题：**这条信息有多可核实**。不是"会不会涨"。
 * 两个轴都是查表/形态匹配算出来的，**不经过任何模型**，所以没有幻觉风险。
 *
 * ⚠️ 界面必须**可展开看构成**（`explain`），不做黑盒分数 ——
 * 一个说不出理由的分数，用户只能选择信或不信，而两者都不合适。
 */
export interface IntelCredibility {
  /** 0–100 综合分。低权威来源**封顶**在它的来源档附近（内容写得再好也抬不动）。 */
  score: number;
  /** 来源轴（权威性）：监管公告 94 … 未证实传闻 28 */
  source_base: number;
  /** 内容轴（可核实程度）：附公告编号 100 … 纯推测 12 */
  content_base: number;
  /** 来源档的中文名（如"财经自媒体"）—— **不含任何渠道标识** */
  source_reason: string;
  /** 内容档的中文名（如"有数据支撑"） */
  content_reason: string;
  /**
   * 独立佐证数。**第一步恒为 `null`** —— 需要按事件轴聚类（第二步）。
   * 界面对 `null` 要显示"暂未统计"，**不能显示 0**
   * （0 的意思是"查过了没有第二条来源"）。
   */
  corroboration: number | null;
  /** 是否允许进入倾向统计（低于 50 分不做倾向分析） */
  tone_allowed: boolean;
  /** 一句话解释这个分是怎么来的 */
  explain: string;
}

/**
 * 一条**关联新闻**（同一件事的另一个来源）。
 *
 * ⚠️ 刻意**不含** `source_alias` —— 那是假名，不上屏。
 * 只带判断"是不是同一件事、分别多可核实"所需的最小字段。
 */
export interface RelatedNews {
  title: string;
  kind_label: string;
  published_at: string;
  /** 该条自己的可信度分（与主条可能不同 —— 同一件事、不同来源） */
  credibility_score: number | null;
}

/**
 * **原文倾向**（第三步：语义抽取）。
 *
 * ## ⚠️ 字段名 `tone` 的含义被严格限定

 * 它是「**这条第三方原文自己**是什么语气」，**不是**平台判断、不是预测。
 * 界面上必须显示为「原文倾向」，且**永远**与 `phrases`（原文词组）一起
 * 出现 —— 用户能拿它去原文核对。说不出理由的倾向标签，用户只能选择
 * 信或不信，而两者都不合适。
 *
 * ## 为什么 `confidence` 可能是 `null`

 * 用户口径（2026-09-25）："原则上有幻觉风险的可以不显示数值。"
 * 有幻觉风险的情形就是**规则层与模型层判定不一致** —— 那时给
 * `tone="未定"` 且 `confidence=null`，界面上不显示倾向标签，
 * 也不显示任何数值。只有两层一致时才给数值（且取两者较小值）。
 */
export interface IntelTone {
  /** `偏多` | `偏空` | `中性` | `未定` */
  tone: string;
  /** 界面靠它决定"显示标签还是显示未定"（服务端算好，前端不重复判断） */
  has_tone: boolean;
  /** 原文词组（**逐字来自原文**，已过服务端子串校验） */
  phrases: string[];
  /** 原文中真实出现的 A 股代码（已过幻觉拦截） */
  codes: string[];
  /** 0~1，**两层一致时才有值**；不一致时为 `null` */
  confidence: number | null;
  /** `rules` | `rules+llm` | `skipped` */
  source: string;
  /** 一句话解释（不含来源标识） */
  explain: string;
}

/** 单条情报（`IntelItem.to_public()` 的白名单输出）。 */
export interface IntelItem {
  kind: string;
  kind_label: string;
  title: string;
  /** 已由后端按类型截断（快讯 160 / 研报 200 / 笔记 260 / 政策 300 字） */
  summary: string;
  published_at: string;
  /**
   * 来源**假名**（`src-xxxxxxxx`）。
   * 只用于同源判断；**不要渲染到界面上**。
   */
  source_alias: string;
  codes: string[];
  industry: string;
  rating_origin: string;
  /** 仅研报有署名；其它类型的后端已置空 */
  agency: string;
  content_hash: string;
  extra: Record<string, unknown>;
  credibility?: IntelCredibility;
  /**
   * 相似新闻聚合：**同一件事的其他来源条数**。
   *
   * 后端已把同一簇里的非代表条**收起**（不再单独占位），所以这个数字
   * 就是"被折叠了几条"。点它才展开 `related` —— 用户口径：
   * "相似相同观点的可以聚合成一条的，把信息源在关联数字里……
   * 点开数字可以展开看"。
   */
  related_count?: number;
  /** 关联新闻明细（最多 4 条）。**只在展开时渲染**。 */
  related?: RelatedNews[];
  /** 独立佐证数（= 不同来源数 − 1），由聚类算出 */
  corroboration?: number;
  /** 这一簇的代表条（非代表条已被后端收起，前端一般看不到） */
  is_cluster_lead?: boolean;
  cluster_id?: string;
  /**
   * 原文倾向。**可能不存在** —— 抽取是定时任务（2 小时一次），
   * 新条目在下一批抽到之前没有这个字段。界面要说尚未抽取，
   * **不能编一个默认值**。
   */
  tone?: IntelTone;
}

/** 数据缺口。`message` 面向普通用户，**不含源名与错误原文**。 */
export interface IntelGap {
  kind: string;
  kind_label: string;
  message: string;
}

export interface IntelFeed {
  items: IntelItem[];
  gaps: IntelGap[];
  /** 各类型条数（注意是**去重后全量**的计数，可能大于 `items.length`） */
  counts: Record<string, number>;
  fetched_at: string;
  /** 有缺口即为 true —— 界面要显示"数据不完整"，但**不说是哪个源坏了** */
  degraded: boolean;
  /** 仅管理员可见的运维提示（`applied_tier === 'admin'` 时才渲染） */
  admin_hints?: string[];
  /** 当前生效的筛选档（服务端下发，前端不自己猜） */
  filter?: string;
  /** 当前生效的取样顺序 */
  sort?: string;
  /**
   * 分层计数：`{high, upper, mid, low, doubt}` → 条数。
   *
   * ⚠️ **基于全量条目**，不随当前 `filter` 变化 —— 否则切一次 tab
   * 角标就全变了，用户会以为数据在动。所以角标要用它，不要用 `items`。
   */
  credibility_dist?: Record<string, number>;
  /** 可用筛选档：`key → 中文名`（定义在服务端，避免前后端两套口径） */
  filters?: Record<string, string>;
  /**
   * 相似新闻聚合统计：`{clusters, clustered_items, folded, pairs}`。
   *
   * 聚合是**悄悄减少条数**的操作 —— 没有这个统计就没人能发现阈值配错了
   * （比如把不相关的事合在一起）。界面把它显示在页脚，当"可见的账"。
   */
  cluster_stats?: {
    clusters?: number; clustered_items?: number;
    folded?: number; pairs?: number;
  };
  /**
   * 原文倾向分布 {偏多: n, 偏空: n, 中性: n}。
   *
   * ⚠️ 服务端**只统计 has_tone 的条目** —— 把未定算进多空比，
   * 等于替用户做了一个我们并不确定的判断。所以这里的三个数之和
   * **小于**条目总数，那是正常的。
   */
  tone_dist?: Record<string, number>;
}

/** 日历事件。四类：`earnings` 预约披露 / `unlock` 解禁 / `macro` 宏观 / `trade` 交易日。 */
export interface CalendarEvent {
  event_id: string;
  kind: string;
  date: string;
  title: string;
  scope: {
    kind?: string;
    company_count?: number;
    industries?: string[];
    codes?: string[];
  };
  /** `rule` = 规则确定（交易所规则，不会变）｜`scheduled` = 预约（可改期） */
  certainty: string;
  changes: Array<Record<string, string>>;
  /**
   * 补充指标。宏观事件用这四个状态字段表达**预期差**：
   *
   *   state      `pending` 待公布｜`prior_only` 仅有前值｜
   *              `released` 已公布｜`surprise` 有预期也有公布
   *   expected   一致预期（多半没有 —— 实测 439 条前值只有 40 条预期）
   *   previous   上期值
   *   published  公布值
   *   surprise   公布 − 预期（**有正负号**，是"预期差"本体）
   *   src_date   数据来源的日期。与 `date` 不同时要显式标注 ——
   *              它说明这个前值其实是**哪一期**的，不标就是误导。
   */
  metrics: {
    state?: string;
    expected?: number | null;
    previous?: number | null;
    published?: number | null;
    surprise?: number | null;
    note?: string;
    importance?: number;
    src_date?: string;
    [k: string]: unknown;
  };
}

export interface CalendarResult {
  events: CalendarEvent[];
  gaps: Array<{ kind?: string; message?: string }>;
  fetched_at: string;
  degraded: boolean;
  disclaimer?: string;
}

export interface SourceHealth {
  /** `healthy` | `degraded`（聚合口径，不细分到源） */
  state: string;
  sources_total: number;
  sources_ok: number;
  last_ok_ms: number;
}

export interface PremarketBrief {
  fetched_at: string;
  degraded: boolean;
  sections: {
    premarket_news: IntelItem[];
    broker_heat: IntelItem[];
    policy: IntelItem[];
    research_notes: IntelItem[];
  };
  counts: Record<string, number>;
  disclaimer?: string;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    // 会话令牌 `moss_sid` 是 HttpOnly Cookie，必须显式声明带凭据才会回发
    // （`sectorCrowdingApi.ts` 那套靠同源默认值也成立，这里写明更稳）。
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    signal,
  });
  if (!resp.ok) {
    // 401 → 广播"会话失效"，由 useAuth 退回登录页。不这么做的话，
    // 登录过期会被这条接口误报成"情报功能坏了"。
    if (resp.status === 401) notifyUnauthorized(`${BASE}${path}`);
    // 统一走报错码契约（`errors.ts`）：后端 envelope → ApiError，
    // **不透传响应体原文** —— 那里面可能有上游 URL（源保密的一部分）。
    throw apiErrorFromResponse(resp.status, await resp.text());
  }
  return resp.json() as Promise<T>;
}

/** 情报流。`codes` 是关注标的（用于额外拉取对应研报）。 */
export function fetchIntelFeed(
  opts: { limit?: number; codes?: string[]; sort?: string; filter?: string } = {},
  signal?: AbortSignal,
): Promise<IntelFeed> {
  const q = new URLSearchParams();
  if (opts.limit) q.set("limit", String(opts.limit));
  if (opts.codes?.length) q.set("codes", opts.codes.join(","));
  if (opts.sort) q.set("sort", opts.sort);
  if (opts.filter && opts.filter !== "all") q.set("filter", opts.filter);
  const qs = q.toString();
  return getJson<IntelFeed>(`/feed${qs ? `?${qs}` : ""}`, signal);
}

/** 投资日历（预约披露 / 解禁 / 宏观 / 交易日）。 */
export function fetchIntelCalendar(
  horizonDays = 30,
  signal?: AbortSignal,
): Promise<CalendarResult> {
  return getJson<CalendarResult>(`/calendar?horizon_days=${horizonDays}`, signal);
}

/** 采集健康度（聚合口径，不暴露有几个源、分别叫什么）。 */
export function fetchIntelHealth(signal?: AbortSignal): Promise<SourceHealth> {
  return getJson<SourceHealth>("/sources/health", signal);
}

/** 盘前简报（盘前新闻 / 研报热度 / 政策 / 研究笔记 四段）。 */
export function fetchPremarketBrief(signal?: AbortSignal): Promise<PremarketBrief> {
  return getJson<PremarketBrief>("/brief", signal);
}

/**
 * 处置一个事件（确认 / 忽略 / 重新打开）。
 *
 * 只改状态与备注，**不删数据** —— 审计链要能回溯谁在什么时候处置了什么。
 */
export async function setAlertState(
  alertId: string,
  state: "ack" | "ignore" | "open",
  note = "",
): Promise<{ ok: boolean; alert_id: string; state: string }> {
  // 写操作要带 CSRF 头：`moss_sid` 是 HttpOnly，跨站请求会自动带上它
  // （这正是 CSRF 的成因），所以服务端额外下发一个前端可读的随机串，
  // 要求写操作放进请求头 —— 攻击者的站点读不到它。
  const csrf = readCsrfToken();
  const url = `${BASE}/alerts/${encodeURIComponent(alertId)}/state`;
  const resp = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
    },
    body: JSON.stringify({ state, note }),
  });
  if (!resp.ok) {
    if (resp.status === 401) notifyUnauthorized(url);
    throw apiErrorFromResponse(resp.status, await resp.text());
  }
  return resp.json();
}

// ======================================================================
// 展示工具（纯函数，无副作用）
// ======================================================================

/**
 * `published_at` → 可读时间。
 *
 * 源的时间戳格式**不统一**，实测有三种：
 *   `20260924`                     （快讯，只有日期）
 *   `2026-09-25 04:26:03`          （快讯，带时间）
 *   `2026-09-25T13:15:41.340+0800` （知识星球，带毫秒与无冒号时区）
 *
 * `new Date()` 对第一种与第三种都会得到 `Invalid Date` 或错值，
 * 所以这里**自己解析**，不依赖 `Date` 的宽容行为。
 */
export function formatTime(raw: string, opts: { withDate?: boolean } = {}): string {
  const s = (raw || "").trim();
  if (!s) return "—";
  // 紧凑日期 `YYYYMMDD`
  const compact = /^(\d{4})(\d{2})(\d{2})$/.exec(s);
  if (compact) {
    const [, y, m, d] = compact;
    return opts.withDate ? `${y}-${m}-${d}` : `${m}-${d}`;
  }
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(s);
  if (m) {
    const [, y, mo, d, hh, mi] = m;
    return opts.withDate ? `${y}-${mo}-${d} ${hh}:${mi}` : `${hh}:${mi}`;
  }
  return s.slice(0, opts.withDate ? 16 : 5);
}

/** `published_at` → 只取日期部分 `YYYY-MM-DD`（用于按天分组）。 */
export function dayKey(raw: string): string {
  const s = (raw || "").trim();
  const compact = /^(\d{4})(\d{2})(\d{2})$/.exec(s);
  if (compact) return `${compact[1]}-${compact[2]}-${compact[3]}`;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : s.slice(0, 10);
}

/** 今天（本地时区）的 `YYYY-MM-DD`。 */
export function todayKey(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 两个 `YYYY-MM-DD` 相差几天（`b - a`）。解析失败返回 `null`（不猜）。 */
export function daysBetween(a: string, b: string): number | null {
  const pa = Date.parse(`${a}T00:00:00`);
  const pb = Date.parse(`${b}T00:00:00`);
  if (Number.isNaN(pa) || Number.isNaN(pb)) return null;
  return Math.round((pb - pa) / 86400000);
}

/**
 * 日历事件类型 → 中文名与配色 key。
 *
 * ⚠️ key 必须与**后端实际产出**的常量逐一对应，少一个就会出现
 * "英文机器名直接上屏"。实测踩过：后端写的是 `trade_day`，
 * 这里写成 `trade`，于是交易日显示成 `trade_day`（`?? ` 兜底把
 * 原值透出去了）。后端全集（`src/domain/intel/calendar.py`）：
 *
 *     earnings  预约披露（`stock_yysj_em` / `stock_report_disclosure`）
 *     unlock    限售解禁
 *     macro     宏观发布（含 FOMC —— 后端把 fed 段落也标成 macro）
 *     trade_day 交易日（交易所规则，**刻意无备源**）
 */
export const CALENDAR_KINDS: Record<string, { label: string; tone: string }> = {
  earnings: { label: "预约披露", tone: "blue" },
  unlock: { label: "限售解禁", tone: "orange" },
  macro: { label: "宏观发布", tone: "purple" },
  trade_day: { label: "交易日", tone: "gray" },
};

/**
 * 预期差四态的**文案与配色**。
 *
 * ⚠️ 合规：这里只描述"数据处于什么状态"，**不含方向判断**。
 * `surprise` 那两态刻意用"高于/低于一致预期"这种**纯算术**表述，
 * 不用"超预期利好"之类 —— 后者是投资建议（见 `docs/INTEL_CENTER_REDESIGN.md` §0）。
 */
export const EXPECTATION_STATES: Record<
  string, { label: string; tone: string; hint: string }
> = {
  pending: {
    label: "待公布",
    tone: "gray",
    hint: "该数据尚未发布，暂无公布值",
  },
  prior_only: {
    label: "仅前值",
    tone: "blue",
    hint: "暂无一致预期值，只有上一期可比数据",
  },
  released: {
    label: "已公布",
    tone: "green",
    hint: "已发布，但无一致预期值可比对（不计算预期差）",
  },
  surprise: {
    label: "预期差",
    tone: "purple",
    hint: "公布值与一致预期值均可得，展示两者之差",
  },
};

/** 情报类型 → 中文标签（前端只认这个，不认内部源名） */
export const KIND_FALLBACK_LABELS: Record<string, string> = {
  newswire: "财经快讯",
  broker_report: "券商研报",
  policy: "政策信号",
  research_note: "研究笔记",
  other: "其他",
};

/**
 * 可信度分层（与后端 `credibility.LEVELS` **逐项对应**）。
 *
 * 阈值写在前端是为了给分数环上色（后端只下发 `credibility_dist` 的
 * 分层计数，不下发每条的分层 key —— 那会让响应多一个冗余字段）。
 * 改阈值必须**同时改两处**，`tests/unit/test_intel_credibility.py`
 * 的 `test_level_boundaries` 锁住后端那一侧。
 */
export const CRED_LEVELS: Array<{
  lower: number; key: string; label: string;
}> = [
  { lower: 80, key: "high", label: "高" },
  { lower: 65, key: "upper", label: "较高" },
  { lower: 50, key: "mid", label: "中" },
  { lower: 35, key: "low", label: "低" },
  { lower: 0, key: "doubt", label: "存疑" },
];

/** 分数 → 分层 key（与后端 `level_of` 同口径）。 */
export function credLevel(score: number): string {
  for (const lv of CRED_LEVELS) {
    if (score >= lv.lower) return lv.key;
  }
  return "doubt";
}

/** 分层 key → 中文名。 */
export function credLevelLabel(score: number): string {
  const key = credLevel(score);
  return CRED_LEVELS.find((l) => l.key === key)?.label ?? "存疑";
}

/**
 * 数值格式化：保留合适位数，`null`/`undefined` → `—`（**不填 0**）。
 *
 * ⚠️ 这条规则在本项目是硬约束：`—` 表示"没有这个数"，
 * `0` 表示"这个数是零"，两者不能混。
 */
export function fmtNum(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  // 整数不补小数位（`49` 比 `49.00` 好读），小数按 digits 截断
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
}

/** 带正负号的数值（预期差用）——正数显式带 `+`。
 *
 * ⚠️ 符号要取**原值**的符号，不能取四舍五入后的结果：
 * `-0.004` 保留两位会变成 `0`，若按四舍五入值判符号就会输出 `0`
 * （看着像"没有预期差"，其实是负的），必须输出 `-0`。
 */
export function fmtSigned(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const body = fmtNum(Math.abs(n), digits);
  if (n > 0) return `+${body}`;
  if (n < 0) return `-${body}`;
  return "0";
}
