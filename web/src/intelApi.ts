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
  opts: { limit?: number; codes?: string[] } = {},
  signal?: AbortSignal,
): Promise<IntelFeed> {
  const q = new URLSearchParams();
  if (opts.limit) q.set("limit", String(opts.limit));
  if (opts.codes?.length) q.set("codes", opts.codes.join(","));
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

/** 数值格式化：保留合适位数，`null`/`undefined` → `—`（**不填 0**）。 */
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
