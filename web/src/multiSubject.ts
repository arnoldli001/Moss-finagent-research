/**
 * 投研分析 · 「多标的（逐只）」结果的**读取 + 展示模型**（纯函数，不依赖 React）。
 *
 * ## 为什么单独一个模块
 *
 * 后端 2026-10-08 给 5 个 Agent 补了"多标的"支持，新增 5 个**逐只**字段
 * （`valuation_calc_by_code` / `per_subject` / `compliance_per_code` /
 * `generic_industry_sections` / `sentiment_metrics_by_group`），
 * 界面此前**一个都没读** —— 用户问"宁波银行和中国神华能不能持有"，
 * 只能看到被合并成一句话的结论。
 *
 * 读字段的规则全部收在这里，渲染只做"照抄展示模型"（`components/MultiSubjectPanel`），
 * 于是"单标的不许多渲染"这条硬要求可以被**逐条自检**
 * （`multiSubjectRenderCheck.tsx`，跑法见该文件头部）。
 *
 * ## 三条不许破的规矩
 *
 * 1. ★★ **单标的逐字一致**：字段缺席、或只有 1 条 ⇒ 本节返回 `null`，
 *    界面上**连小节标题都不会多一个**（单标的就是修复前那条路径）。
 * 2. ★ **不静默丢数据**：字段在场却读不出/一条都没有 ⇒ 如实给
 *    "（未提供）"＋原因，既不抛异常，也不整块消失。
 * 3. **没量到 ≠ 量到 0**：`measured=false`、`weighted_sentiment=0`、
 *    `severe_flag_count=0` 都是**有效读数**；读不到时保留 `null`，
 *    由展示层写"（未提供）"，绝不用 0 顶替（本项目在 A12 上为这条付过代价）。
 */

import { agentLabel, confidenceLabel } from "./agentMeta";
import type {
  AgentOutputSummary,
  CompliancePerCode,
  GenericIndustrySection,
  PerSubjectRecommendation,
  SentimentMetrics,
  ValuationCalcByCode,
} from "./api";

/** 缺值/读不出的统一显示口径（不猜，也不留空）。 */
export const NOT_PROVIDED = "（未提供）";

type Raw = Record<string, unknown>;

/**
 * 字段**三态**。
 *
 * `absent` 与 `unreadable` 必须分开：前者是"这次应答本来就没有这个字段"
 * （单标的的正常形态），后者是"字段在，但形状不对"（要如实报出来）。
 */
export type FieldState<T> =
  | { kind: "absent" }
  | { kind: "ok"; value: T }
  | { kind: "unreadable"; reason: string };

export type MultiSubjectTone = "info" | "ok" | "warn" | "bad" | "muted";

export type MultiSubjectBadge = { text: string; tone: MultiSubjectTone };
export type MultiSubjectField = { label: string; value: string };
export type MultiSubjectList = { label: string; items: string[] };

export type MultiSubjectRow = {
  key: string;
  title: string;
  badges: MultiSubjectBadge[];
  fields: MultiSubjectField[];
  lists: MultiSubjectList[];
  body: { label: string; text: string } | null;
};

export type MultiSubjectSection = {
  key: string;
  title: string;
  /** 出处：Agent 中文名 + 字段名（投研系统必须能对回源头） */
  source: string;
  rows: MultiSubjectRow[];
  /** 非 null ⇒ 这一节没有可渲染的行，如实显示"（未提供）"＋原因 */
  unreadable: string | null;
  /** 非 null ⇒ 这一节成立，但有一句"必须先说的话"（如行业被截断、个别条目读不出） */
  note: string | null;
};

// ── 解析后的"读"模型 ──────────────────────────────────────────────────
//
// 与 `api.ts` 的线上契约同形（用 `Omit` 派生，形状漂移会编译不过），
// 只把**可能读不到的标量**放宽成 `| null`：`null` 在展示层显示"（未提供）"，
// 绝不折算成 0 / false（那正是"没量到"与"量到 0"混淆的来源）。

/** `compliance_per_code[i]`：形状同 `CompliancePerCode`，读不到的标量留 `null`。 */
export type CompliancePerCodeRead = Omit<
  CompliancePerCode,
  "measured" | "severe_flag_count" | "event_flags"
> & {
  measured: boolean | null;
  severe_flag_count: number | null;
  event_flags: number | null;
};

/** `sentiment_metrics_by_group[key]`：形状同 `SentimentMetrics`，读不到的标量留 `null`。 */
export type SentimentMetricsRead = Omit<
  SentimentMetrics,
  "weighted_sentiment" | "event_count"
> & {
  weighted_sentiment: number | null;
  event_count: number | null;
};

// ── 底层取值helper（全部对 unknown 设防）────────────────────────────────

function isRaw(v: unknown): v is Raw {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function typeName(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "数组";
  switch (typeof v) {
    case "object": return "对象";
    case "string": return "字符串";
    case "number": return "数字";
    case "boolean": return "布尔";
    default: return typeof v;
  }
}

/** 宽松转文字：数字/布尔也认；其余（对象、数组、null）→ 空串。 */
function text(v: unknown): string {
  if (typeof v === "string") return v.trim();
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
  if (typeof v === "boolean") return v ? "是" : "否";
  return "";
}

/** 标量槽位：读不出就写"（未提供）"—— 格子还在，"这里没值"看得见。 */
function show(v: unknown): string {
  return text(v) || NOT_PROVIDED;
}

function numberOrNull(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function boolOrNull(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}

/** 字符串数组；**不是数组 ⇒ `null`**（与"空数组"区分开）。 */
function textList(v: unknown): string[] | null {
  if (!Array.isArray(v)) return null;
  const out: string[] = [];
  for (const item of v as unknown[]) {
    const t = text(item);
    if (t !== "") out.push(t);
  }
  return out;
}

/**
 * 列表槽位 → 一行展示。
 * · 键缺席 / 空数组 ⇒ 不渲染这一行（没东西可说）
 * · 在场但不是数组 ⇒ 如实写"（未提供）"（**不许静默吞掉**）
 */
function listLine(label: string, v: unknown): MultiSubjectList | null {
  if (v === null || v === undefined) return null;
  const items = textList(v);
  if (items === null) return { label, items: [NOT_PROVIDED] };
  if (items.length === 0) return null;
  return { label, items };
}

/** 某个 Agent 的 `result`（找不到 → null）；同时替非对象 result 兜底。 */
function agentResult(outputs: AgentOutputSummary[], agentId: string): Raw | null {
  for (const o of outputs) {
    if (o && o.agent_id === agentId && isRaw(o.result)) return o.result;
  }
  return null;
}

/**
 * 取一个"多标的"字段 → 三态。
 *
 * ⚠️ `null` 与"键不存在"都算 `absent`：后端多标的字段是**加法**字段，
 * 单标的时要么没有这个键，要么显式为 null —— 两种都必须走原路径。
 */
function fieldOf(
  outputs: AgentOutputSummary[], agentId: string, key: string,
): FieldState<unknown> {
  const result = agentResult(outputs, agentId);
  if (!result || !(key in result)) return { kind: "absent" };
  const value = result[key];
  if (value === null || value === undefined) return { kind: "absent" };
  return { kind: "ok", value };
}

// ── 5 个字段各自的解析（导出以便自检直接打）──────────────────────────────

/** A10_micro `result.valuation_calc_by_code`：键 = 6 位代码。 */
export function readValuationByCode(
  outputs: AgentOutputSummary[],
): FieldState<ValuationCalcByCode> {
  const f = fieldOf(outputs, "A10_micro", "valuation_calc_by_code");
  if (f.kind !== "ok") return f;
  if (!isRaw(f.value)) {
    return { kind: "unreadable", reason: `是${typeName(f.value)}，不是"代码 → 估值"的对象` };
  }
  const out: ValuationCalcByCode = {};
  for (const [code, calc] of Object.entries(f.value)) {
    if (!isRaw(calc)) {
      return {
        kind: "unreadable",
        reason: `代码 ${code} 的取值是${typeName(calc)}，不是估值对象`,
      };
    }
    out[code] = {
      valuation: text(calc.valuation),
      basis: text(calc.basis),
      detail: text(calc.detail),
      pe_percentile: numberOrNull(calc.pe_percentile),
      pb_percentile: numberOrNull(calc.pb_percentile),
    };
  }
  return { kind: "ok", value: out };
}

/** A17_recommend `result.per_subject`：每只票一条立场/仓位/三档情景。 */
export function readPerSubject(
  outputs: AgentOutputSummary[],
): FieldState<PerSubjectRecommendation[]> {
  const f = fieldOf(outputs, "A17_recommend", "per_subject");
  if (f.kind !== "ok") return f;
  if (!Array.isArray(f.value)) {
    return { kind: "unreadable", reason: `是${typeName(f.value)}，不是数组` };
  }
  const list = f.value as unknown[];
  const rows: PerSubjectRecommendation[] = [];
  for (const [i, item] of list.entries()) {
    if (!isRaw(item)) {
      return { kind: "unreadable", reason: `第 ${i + 1} 条是${typeName(item)}，不是对象` };
    }
    const ret = isRaw(item.expected_return_3_6m) ? item.expected_return_3_6m : null;
    rows.push({
      code: text(item.code),
      name: text(item.name) || null,
      stance: text(item.stance) || null,
      position_advice: text(item.position_advice) || null,
      expected_return_3_6m: ret
        ? {
          bull: text(ret.bull) || null,
          base: text(ret.base) || null,
          bear: text(ret.bear) || null,
          assumptions: textList(ret.assumptions),
          invalid_signals: textList(ret.invalid_signals),
        }
        : null,
      key_logic: textList(item.key_logic),
      risks: textList(item.risks),
    });
  }
  return { kind: "ok", value: rows };
}

/** A12_compliance `result.compliance_per_code`：**单标的时也有 1 条**。 */
export function readCompliancePerCode(
  outputs: AgentOutputSummary[],
): FieldState<CompliancePerCodeRead[]> {
  const f = fieldOf(outputs, "A12_compliance", "compliance_per_code");
  if (f.kind !== "ok") return f;
  if (!Array.isArray(f.value)) {
    return { kind: "unreadable", reason: `是${typeName(f.value)}，不是数组` };
  }
  const list = f.value as unknown[];
  const rows: CompliancePerCodeRead[] = [];
  for (const [i, item] of list.entries()) {
    if (!isRaw(item)) {
      return { kind: "unreadable", reason: `第 ${i + 1} 条是${typeName(item)}，不是对象` };
    }
    rows.push({
      code: text(item.code),
      label: text(item.label),
      level: text(item.level),
      flags: textList(item.flags) ?? [],
      risk_flags: textList(item.risk_flags) ?? [],
      families_measured: textList(item.families_measured) ?? [],
      families_unmeasured: textList(item.families_unmeasured) ?? [],
      measured: boolOrNull(item.measured),
      severe_flag_count: numberOrNull(item.severe_flag_count),
      event_flags: numberOrNull(item.event_flags),
    });
  }
  return { kind: "ok", value: rows };
}

/** A20_generic_industry `result.generic_industry_sections`：一个"没人管"的行业一节。 */
export function readGenericIndustrySections(
  outputs: AgentOutputSummary[],
): FieldState<GenericIndustrySection[]> {
  const f = fieldOf(outputs, "A20_generic_industry", "generic_industry_sections");
  if (f.kind !== "ok") return f;
  if (!Array.isArray(f.value)) {
    return { kind: "unreadable", reason: `是${typeName(f.value)}，不是数组` };
  }
  const list = f.value as unknown[];
  const rows: GenericIndustrySection[] = [];
  for (const [i, item] of list.entries()) {
    if (!isRaw(item)) {
      return { kind: "unreadable", reason: `第 ${i + 1} 条是${typeName(item)}，不是对象` };
    }
    rows.push({
      industry: text(item.industry),
      basis: text(item.basis),
      confidence: text(item.confidence),
      conclusion: text(item.conclusion),
      skipped: item.skipped === true,
      skip_kind: text(item.skip_kind),
      model_used: text(item.model_used),
      industry_signal_calc: item.industry_signal_calc ?? null,
    });
  }
  return { kind: "ok", value: rows };
}

/** A07_sentiment `result.sentiment_metrics_by_group`：键 = 代码或 subject。 */
export function readSentimentMetricsByGroup(
  outputs: AgentOutputSummary[],
): FieldState<Record<string, SentimentMetricsRead>> {
  const f = fieldOf(outputs, "A07_sentiment", "sentiment_metrics_by_group");
  if (f.kind !== "ok") return f;
  if (!isRaw(f.value)) {
    return { kind: "unreadable", reason: `是${typeName(f.value)}，不是"标的 → 情绪指标"的对象` };
  }
  const out: Record<string, SentimentMetricsRead> = {};
  for (const [key, metrics] of Object.entries(f.value)) {
    if (!isRaw(metrics)) {
      return {
        kind: "unreadable",
        reason: `分组 ${key} 的取值是${typeName(metrics)}，不是指标对象`,
      };
    }
    const dist: Record<string, number> = {};
    if (isRaw(metrics.distribution)) {
      for (const [d, n] of Object.entries(metrics.distribution)) {
        const count = numberOrNull(n);
        if (count !== null) dist[d] = count;
      }
    }
    const tops: SentimentMetrics["top_subjects"] = [];
    if (Array.isArray(metrics.top_subjects)) {
      for (const t of metrics.top_subjects as unknown[]) {
        if (!isRaw(t)) continue;
        const score = numberOrNull(t.net_score);
        const subject = text(t.subject);
        if (subject !== "" && score !== null) {
          tops.push({ subject, net_score: score });
        }
      }
    }
    out[key] = {
      weighted_sentiment: numberOrNull(metrics.weighted_sentiment),
      event_count: numberOrNull(metrics.event_count),
      distribution: dist,
      top_subjects: tops,
    };
  }
  return { kind: "ok", value: out };
}

/**
 * A07 的**伴随**字段 `sentiment_phase_by_group`（逐只周期定位）。
 *
 * 用户口径里的"每个标的各自的情绪分/**phase**"两半分别落在两个键上，
 * 所以这里一并读；它不在这次要接的 5 个字段里，读不到就当没有（不报"未提供"）。
 */
export function readSentimentPhaseByGroup(
  outputs: AgentOutputSummary[],
): Record<string, string> {
  const f = fieldOf(outputs, "A07_sentiment", "sentiment_phase_by_group");
  if (f.kind !== "ok" || !isRaw(f.value)) return {};
  const out: Record<string, string> = {};
  for (const [key, phase] of Object.entries(f.value)) {
    const t = text(phase);
    if (t !== "") out[key] = t;
  }
  return out;
}

// ── 展示模型 ──────────────────────────────────────────────────────────

function badge(label: string, tone: MultiSubjectTone = "muted"): MultiSubjectBadge {
  return { text: label, tone };
}

function unreadableSection(
  key: string, title: string, source: string, reason: string,
): MultiSubjectSection {
  return { key, title, source, rows: [], unreadable: reason, note: null };
}

function problemsNote(problems: string[]): string | null {
  if (problems.length === 0) return null;
  return `另有 ${problems.length} 处读不出：${problems.join("；")}`;
}

function stanceTone(stance: string | null | undefined): MultiSubjectTone {
  const s = stance ?? "";
  if (/买入|增持|推荐|看多|强烈/.test(s)) return "ok";
  if (/卖出|减持|回避|看空|减仓/.test(s)) return "bad";
  if (/数据不足|不明确|观望|中性|持有/.test(s)) return "muted";
  return "info";
}

function valuationTone(valuation: string): MultiSubjectTone {
  if (/低估|历史低位|洼地/.test(valuation)) return "ok";
  if (/高估|历史偏高/.test(valuation)) return "warn";
  if (/数据不足|无/.test(valuation)) return "muted";
  return "info";
}

function levelTone(level: string): MultiSubjectTone {
  if (level === "高") return "bad";
  if (level === "中") return "warn";
  if (level === "无") return "ok";
  if (level.includes("未量到")) return "muted";
  return "info";
}

function sentimentTone(score: number | null): MultiSubjectTone {
  if (score === null) return "muted";
  if (score >= 0.15) return "ok";
  if (score <= -0.15) return "bad";
  return "info";
}

function distributionText(dist: Record<string, number>): string {
  const parts = Object.entries(dist).map(([k, v]) => `${k} ${v}`);
  return parts.length > 0 ? parts.join(" · ") : NOT_PROVIDED;
}

/**
 * ★★ **多标的小节的唯一门槛**（单标的"逐字一致"就靠这个函数）。
 *
 * | 情形 | 结果 |
 * |---|---|
 * | 字段缺席 | `absent` ⇒ 调用方返回 `null`（连小节都不存在） |
 * | 读得出、≥2 条 | `render` ⇒ 正常渲染 |
 * | 读得出、**恰好 1 条** | `single` ⇒ 调用方返回 `null`（= 单标的，走原路径） |
 * | 读得出、0 条 | `empty` ⇒ 如实写"（未提供）"（**不许静默消失**） |
 */
function gate<T>(
  rows: T[],
): { kind: "render"; rows: T[] } | { kind: "single" } | { kind: "empty" } {
  if (rows.length === 0) return { kind: "empty" };
  if (rows.length < 2) return { kind: "single" };
  return { kind: "render", rows };
}

const EMPTY_LIST_REASON = "字段在场，但一条逐只结论都没有（本次应答没有逐只给出）";

/** ① A17_recommend：**逐只立场 / 仓位 / 三档情景**（用户问"能不能持有"的直接答案）。 */
export function recommendSection(outputs: AgentOutputSummary[]): MultiSubjectSection | null {
  const field = readPerSubject(outputs);
  const source = `${agentLabel("A17_recommend")} · per_subject`;
  if (field.kind === "absent") return null;
  if (field.kind === "unreadable") {
    return unreadableSection("recommend", "逐只建议", source, field.reason);
  }
  const g = gate(field.value);
  if (g.kind === "single") return null;
  if (g.kind === "empty") {
    return unreadableSection("recommend", "逐只建议", source, EMPTY_LIST_REASON);
  }
  const problems: string[] = [];
  const rows: MultiSubjectRow[] = g.rows.map((r, i) => {
    const code = r.code;
    const name = r.name ?? "";
    if (code === "" && name === "") {
      problems.push(`第 ${i + 1} 条既没有代码也没有名字`);
    }
    const title = name !== ""
      ? (code !== "" ? `${name}（${code}）` : name)
      : (code !== "" ? code : NOT_PROVIDED);
    const fields: MultiSubjectField[] = [
      { label: "仓位建议", value: show(r.position_advice) },
    ];
    const ret = r.expected_return_3_6m;
    if (ret) {
      fields.push(
        { label: "乐观情景（3–6 个月）", value: show(ret.bull) },
        { label: "中性情景（3–6 个月）", value: show(ret.base) },
        { label: "悲观情景（3–6 个月）", value: show(ret.bear) },
      );
    } else {
      fields.push({ label: "3–6 个月三档情景", value: NOT_PROVIDED });
    }
    const lists = [
      listLine("核心逻辑", r.key_logic),
      listLine("风险", r.risks),
      ...(ret ? [listLine("情景假设", ret.assumptions),
                 listLine("证伪信号", ret.invalid_signals)] : []),
    ].filter((l): l is MultiSubjectList => l !== null);
    return {
      key: code !== "" ? code : `row-${i}`,
      title,
      badges: [badge(`立场 ${show(r.stance)}`, stanceTone(r.stance))],
      fields,
      lists,
      body: null,
    };
  });
  return {
    key: "recommend", title: "逐只建议", source, rows,
    unreadable: null, note: problemsNote(problems),
  };
}

/** ② A10_micro：**逐只估值判定**（单标的时该键不存在，走 `valuation_calc`）。 */
export function valuationSection(outputs: AgentOutputSummary[]): MultiSubjectSection | null {
  const field = readValuationByCode(outputs);
  const source = `${agentLabel("A10_micro")} · valuation_calc_by_code`;
  if (field.kind === "absent") return null;
  if (field.kind === "unreadable") {
    return unreadableSection("valuation", "逐只估值", source, field.reason);
  }
  const codes = Object.keys(field.value);
  if (codes.length === 0) {
    return unreadableSection(
      "valuation", "逐只估值", source,
      "字段在场，但一个代码的估值判定都没有（本次应答没有逐只给出）",
    );
  }
  if (codes.length < 2) return null;   // ★ 单标的 ⇒ 不加任何东西
  const rows: MultiSubjectRow[] = codes.map((code) => {
    const calc = field.value[code];
    const fields: MultiSubjectField[] = [
      { label: "判定依据", value: show(calc.basis) },
    ];
    if (calc.pe_percentile !== null && calc.pe_percentile !== undefined) {
      fields.push({
        label: "PE 历史分位",
        value: `${calc.pe_percentile}%（0% = 历史最便宜）`,
      });
    }
    if (calc.pb_percentile !== null && calc.pb_percentile !== undefined) {
      fields.push({
        label: "PB 历史分位",
        value: `${calc.pb_percentile}%（0% = 历史最便宜）`,
      });
    }
    return {
      key: code,
      title: code,
      badges: [badge(`估值 ${show(calc.valuation)}`, valuationTone(calc.valuation))],
      fields,
      lists: [],
      body: calc.detail !== "" ? { label: "判定明细", text: calc.detail } : null,
    };
  });
  return { key: "valuation", title: "逐只估值", source, rows, unreadable: null, note: null };
}

/** ③ A07_sentiment：**逐只情绪分 / 周期定位**（总体分与逐只分是同一个函数算的）。 */
export function sentimentSection(outputs: AgentOutputSummary[]): MultiSubjectSection | null {
  const field = readSentimentMetricsByGroup(outputs);
  const source = `${agentLabel("A07_sentiment")} · sentiment_metrics_by_group`;
  if (field.kind === "absent") return null;
  if (field.kind === "unreadable") {
    return unreadableSection("sentiment", "逐只舆情", source, field.reason);
  }
  const keys = Object.keys(field.value);
  if (keys.length === 0) {
    return unreadableSection(
      "sentiment", "逐只舆情", source,
      "字段在场，但一个标的的情绪指标都没有（本次应答没有逐只给出）",
    );
  }
  if (keys.length < 2) return null;   // ★ 单标的
  const phases = readSentimentPhaseByGroup(outputs);
  const problems: string[] = [];
  const rows: MultiSubjectRow[] = keys.map((key) => {
    const m = field.value[key];
    const phase = phases[key];
    const fields: MultiSubjectField[] = [];
    if (m.weighted_sentiment === null) {
      problems.push(`分组 ${key} 没有情绪分`);
    }
    fields.push({
      label: "事件数",
      value: m.event_count === null ? NOT_PROVIDED : String(m.event_count),
    });
    fields.push({ label: "方向分布", value: distributionText(m.distribution) });
    const lists = [
      m.top_subjects.length > 0
        ? {
          label: "热点主体",
          items: m.top_subjects.map((t) => `${t.subject}（净分 ${t.net_score}）`),
        }
        : null,
    ].filter((l): l is MultiSubjectList => l !== null);
    return {
      key,
      title: key,
      badges: [
        badge(
          m.weighted_sentiment === null
            ? `情绪分 ${NOT_PROVIDED}`
            : `情绪分 ${m.weighted_sentiment}`,
          sentimentTone(m.weighted_sentiment),
        ),
        ...(phase ? [badge(`周期定位 ${phase}`, "info")] : []),
      ],
      fields,
      lists,
      body: null,
    };
  });
  return {
    key: "sentiment", title: "逐只舆情", source, rows,
    unreadable: null, note: problemsNote(problems),
  };
}

/** ④ A12_compliance：**逐只合规等级与旗标**（单标的时也有 1 条 ⇒ 门槛仍是 ≥2）。 */
export function complianceSection(outputs: AgentOutputSummary[]): MultiSubjectSection | null {
  const field = readCompliancePerCode(outputs);
  const source = `${agentLabel("A12_compliance")} · compliance_per_code`;
  if (field.kind === "absent") return null;
  if (field.kind === "unreadable") {
    return unreadableSection("compliance", "逐只合规", source, field.reason);
  }
  const g = gate(field.value);
  if (g.kind === "single") return null;   // ★ 单标的：这一条仍在 A12 的结构化输出里
  if (g.kind === "empty") {
    return unreadableSection("compliance", "逐只合规", source, EMPTY_LIST_REASON);
  }
  const rows: MultiSubjectRow[] = g.rows.map((r, i) => {
    const label = r.label !== "" ? r.label : (r.code !== "" ? r.code : NOT_PROVIDED);
    const title = r.code !== "" && r.code !== r.label
      ? `${label}（${r.code}）`
      : label;
    // ⚠️ `flags` 含占位旗标（"未量到"/"未见异常"），`risk_flags` 才是真实风险。
    //    两者都渲染，但只在没有真实风险旗标时才拿 `flags` 顶位（避免同一句话出现两遍）。
    const lists = [
      r.risk_flags.length > 0
        ? { label: "风险旗标", items: r.risk_flags }
        : (r.flags.length > 0 ? { label: "旗标（含占位）", items: r.flags } : null),
      r.families_measured.length > 0
        ? { label: "已量到的族", items: r.families_measured }
        : null,
      r.families_unmeasured.length > 0
        ? { label: "未量到的族", items: r.families_unmeasured }
        : null,
    ].filter((l): l is MultiSubjectList => l !== null);
    return {
      key: r.code !== "" ? r.code : `row-${i}`,
      title,
      badges: [badge(`合规等级 ${show(r.level)}`, levelTone(r.level))],
      fields: [
        {
          // "没量到"与"量到 0"必须分开表达：读不到时写"（未提供）"，不写"未量到"
          label: "是否量到",
          value: r.measured === null ? NOT_PROVIDED
            : (r.measured ? "已量到" : "未量到"),
        },
        {
          label: "严重旗标数",
          value: r.severe_flag_count === null ? NOT_PROVIDED : String(r.severe_flag_count),
        },
        {
          label: "涉诉/监管事件",
          value: r.event_flags === null ? NOT_PROVIDED : String(r.event_flags),
        },
      ],
      lists,
      body: null,
    };
  });
  return { key: "compliance", title: "逐只合规", source, rows, unreadable: null, note: null };
}

/** A20 的**伴随**字段 `industries_truncated`：截断了就必须说出来（不许静默少写）。 */
function industryTruncationNote(outputs: AgentOutputSummary[]): string | null {
  const result = agentResult(outputs, "A20_generic_industry");
  if (!result) return null;
  const truncated = textList(result.industries_truncated);
  if (!truncated || truncated.length === 0) return null;
  return `另有 ${truncated.length} 个行业本次未分析（${truncated.join("、")}）——`
    + "一节一个行业 = 一次推理调用，超过上限时显式截断。";
}

/** ⑤ A20_generic_industry：**每个"没有专属行业 Agent"的行业各一节**。 */
export function industrySection(outputs: AgentOutputSummary[]): MultiSubjectSection | null {
  const field = readGenericIndustrySections(outputs);
  const source = `${agentLabel("A20_generic_industry")} · generic_industry_sections`;
  if (field.kind === "absent") return null;
  if (field.kind === "unreadable") {
    return unreadableSection("industry", "逐行业结论", source, field.reason);
  }
  const g = gate(field.value);
  if (g.kind === "single") return null;
  if (g.kind === "empty") {
    return unreadableSection("industry", "逐行业结论", source, EMPTY_LIST_REASON);
  }
  const problems: string[] = [];
  const rows: MultiSubjectRow[] = g.rows.map((s, i) => {
    if (s.industry === "") problems.push(`第 ${i + 1} 节没有行业名`);
    const fields: MultiSubjectField[] = [
      { label: "行业判定依据", value: show(s.basis) },
    ];
    if (s.industry_signal_calc !== null && s.industry_signal_calc !== undefined) {
      fields.push({
        label: "本地量化信号",
        value: "有（原始值见该 Agent 的结构化输出）",
      });
    }
    return {
      key: s.industry !== "" ? s.industry : `row-${i}`,
      title: s.industry !== "" ? s.industry : NOT_PROVIDED,
      badges: [
        badge(`置信度 ${confidenceLabel(s.confidence)}`, "info"),
        ...(s.skipped
          ? [badge(s.skip_kind !== "" ? `已跳过：${s.skip_kind}` : "已跳过", "warn")]
          : []),
      ],
      fields,
      lists: [],
      body: s.conclusion !== "" ? { label: "结论", text: s.conclusion } : null,
    };
  });
  return {
    key: "industry", title: "逐行业结论", source, rows,
    // ⚠️ 两条说明**都要留**：读不出的条目与"被截断的行业"是两件不能互相顶替的事
    //    （用 `??` 会让后者在前者出现时静默消失）。
    unreadable: null,
    note: [problemsNote(problems), industryTruncationNote(outputs)]
      .filter((n): n is string => n !== null).join(" ") || null,
  };
}

/**
 * 全部"多标的"小节。**空数组 ⇒ 界面一个节点都不加**（单标的路径）。
 *
 * 顺序按"用户先想知道什么"排：建议（能不能持有）→ 估值 → 舆情 → 合规 → 行业。
 */
export function buildMultiSubjectView(
  outputs: AgentOutputSummary[] | null | undefined,
): MultiSubjectSection[] {
  if (!outputs || outputs.length === 0) return [];
  return [
    recommendSection(outputs),
    valuationSection(outputs),
    sentimentSection(outputs),
    complianceSection(outputs),
    industrySection(outputs),
  ].filter((s): s is MultiSubjectSection => s !== null);
}
