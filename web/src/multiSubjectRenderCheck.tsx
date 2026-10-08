/**
 * 投研分析 · **多标的（逐只）渲染自检**。
 *
 * ## 为什么需要它
 *
 * 这次改动有一条**硬要求**：单标的下的渲染必须与改动前**逐字一致**
 * ——新字段在单标的下缺席或只有 1 条，界面**不许**多出任何标题/分组/空容器。
 * 这一类退化 `tsc` 与 `vite build` **都拦不住**：
 *
 *   · 把 `return null` 写成"先渲染容器再判空" ⇒ 单标的下多一个空面板（不报错）；
 *   · 门槛写成 `>= 1` ⇒ 单标的下把 A12 的那 1 条也当成多标的渲染（不报错）；
 *   · 字段在场但形状不对时 `Object.entries(null)` / `.map` ⇒ **整页 ErrorBoundary 红框**；
 *   · 没量到（`measured: null`）被写成"未量到" ⇒ 把"不知道"说成"没有风险"（最严重）。
 *
 * ## 它不联网、不读数据
 *
 * 全部用固定 fixture 走真组件（`renderToStaticMarkup`），能进 CI：
 * 没有后端、没有 cookie、没有浏览器。
 *
 * ## 跑法（与 `intelRenderCheck.tsx` 同款：esbuild 打包 → node 跑）
 *
 * ⚠️ 本次改动被限定在 `web/src/**`，所以**没有**往 `web/package.json` 加
 * `check:multi` 脚本（那个文件不在允许范围内）。直接跑等价的命令即可：
 *
 * ```bash
 * cd web
 * npx esbuild src/multiSubjectRenderCheck.tsx --bundle --platform=node \
 *   --format=cjs --jsx=automatic --log-level=warning \
 *   --outfile=node_modules/.cache/multiSubjectRenderCheck.cjs
 * node node_modules/.cache/multiSubjectRenderCheck.cjs
 * ```
 *
 * 退出码：0 = 全过，1 = 有失败（**绝不静默以 0 退出**，见文件尾）。
 */
import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { AgentOutputSummary } from "./api";
import MultiSubjectPanel from "./components/MultiSubjectPanel";
import {
  buildMultiSubjectView,
  readCompliancePerCode,
  readGenericIndustrySections,
  readPerSubject,
  readSentimentMetricsByGroup,
  readValuationByCode,
} from "./multiSubject";

let failures = 0;
function check(ok: boolean, label: string, extra = "") {
  if (!ok) failures += 1;
  console.log(`  ${ok ? "OK  " : "FAIL"} ${label}${extra ? `  ${extra}` : ""}`);
}

const render = (outputs: AgentOutputSummary[]) =>
  renderToStaticMarkup(h(MultiSubjectPanel, { outputs }));

const count = (haystack: string, needle: string) =>
  haystack.split(needle).length - 1;

function out(agentId: string, result: Record<string, unknown>): AgentOutputSummary {
  return {
    agent_id: agentId,
    conclusion: `${agentId} 的结论`,
    confidence: "medium",
    data_refs: [],
    result,
  };
}

// ── fixture ①：**单标的**（= 修复前的形状）──────────────────────────────
//
// 关键是 A12：**单标的时 `compliance_per_code` 也保留 1 条**（后端口径），
// 所以它不是"键不存在"，而是"只有 1 条"—— 两条路径都必须什么都不渲染。
const SINGLE: AgentOutputSummary[] = [
  out("A07_sentiment", {
    sentiment_metrics: {
      weighted_sentiment: 0.22, event_count: 3,
      distribution: { positive: 2, negative: 1 },
      top_subjects: [{ subject: "宁波银行", net_score: 0.5 }],
    },
    sentiment_phase: "乐观",
    narrative: "息差企稳",
  }),
  out("A10_micro", {
    valuation_calc: {
      valuation: "合理", basis: "行业均值对比",
      detail: "PE 5.2 vs 行业 6.1；PB 0.6 vs 行业 0.7",
    },
    moat_scores: { brand: 6, technology: 4 },
  }),
  out("A12_compliance", {
    compliance_level_calc: "无",
    compliance_measured: true,
    compliance_flags: ["未见异常"],
    compliance_per_code: [{
      code: "002142", label: "002142", level: "无",
      flags: ["未见异常"], risk_flags: [],
      families_measured: ["质押比例"], families_unmeasured: ["商誉占比"],
      measured: true, severe_flag_count: 0, event_flags: 0,
    }],
  }),
  out("A20_generic_industry", {
    generic_industry_resolution: { industry: "银行", basis: "问句点名" },
  }),
  out("A17_recommend", {
    stance: "增持",
    key_logic: ["高股息 + 低估值"],
    position_advice: "底仓 5%~8%",
    expected_return_3_6m: { bull: "+25%", base: "+10%", bear: "-8%" },
  }),
];

// ── fixture ②：**多标的**（宁波银行 + 中国神华，用户报障的那句问法）──────
const MULTI: AgentOutputSummary[] = [
  out("A07_sentiment", {
    sentiment_metrics: {
      weighted_sentiment: 0.1, event_count: 8,
      distribution: { positive: 4, negative: 4 }, top_subjects: [],
    },
    sentiment_phase: "分歧",
    sentiment_metrics_by_group: {
      "002142": {
        weighted_sentiment: 0.42, event_count: 4,
        distribution: { positive: 3, negative: 1 },
        top_subjects: [{ subject: "宁波银行", net_score: 0.8 }],
      },
      "601088": {
        weighted_sentiment: -0.31, event_count: 4,
        distribution: { positive: 1, negative: 3 },
        top_subjects: [{ subject: "中国神华", net_score: -0.6 }],
      },
    },
    sentiment_phase_by_group: { "002142": "乐观", "601088": "谨慎" },
    multi_subject: true,
  }),
  out("A10_micro", {
    valuation_calc: {
      valuation: "多标的（逐票判定，见 valuation_calc_by_code）",
      basis: "per_code", detail: "002142: 低估；601088: 历史区间内",
    },
    valuation_calc_by_code: {
      "002142": {
        valuation: "低估", basis: "行业均值对比",
        detail: "PE 5.2 vs 行业 6.1；PB 0.6 vs 行业 0.7",
      },
      "601088": {
        valuation: "历史区间内", basis: "自身历史分位",
        pe_percentile: 46, pb_percentile: 52,
        detail: "当前PE历史分位46%、PB历史分位52%（0%为历史最便宜）",
      },
    },
  }),
  out("A12_compliance", {
    compliance_level_calc: "中",
    compliance_per_code: [
      {
        code: "002142", label: "002142", level: "无",
        flags: ["未见异常"], risk_flags: [],
        families_measured: ["质押比例", "商誉占比"],
        families_unmeasured: ["诉讼"],
        measured: true, severe_flag_count: 0, event_flags: 0,
      },
      {
        code: "601088", label: "601088", level: "中",
        flags: ["对外担保占净资产 38%（超 30% 阈值）"],
        risk_flags: ["对外担保占净资产 38%（超 30% 阈值）"],
        families_measured: ["质押比例"], families_unmeasured: ["商誉占比"],
        measured: true, severe_flag_count: 0, event_flags: 0,
      },
    ],
    compliance_multi_target: true,
  }),
  out("A20_generic_industry", {
    generic_industry_resolution: { industry: "银行", basis: "问句点名" },
    generic_industry_sections: [
      {
        industry: "银行", basis: "问句点名", confidence: "medium",
        conclusion: "息差企稳、资产质量平稳，高股息属性仍在。",
        skipped: false, skip_kind: "", model_used: "local-1.5b",
        industry_signal_calc: { pe: 5.2, pb: 0.6 },
      },
      {
        industry: "煤炭", basis: "问句点名", confidence: "low",
        conclusion: "长协价托底但需求走弱，价格中枢下移风险未消。",
        skipped: false, skip_kind: "", model_used: "local-1.5b",
        industry_signal_calc: null,
      },
    ],
    multi_industry: true,
    industries: ["银行", "煤炭"],
    industries_truncated: ["券商"],
  }),
  out("A17_recommend", {
    stance: "中性",
    per_subject: [
      {
        code: "002142", name: "宁波银行", stance: "增持",
        position_advice: "底仓 5%~8%，回调分批",
        expected_return_3_6m: {
          bull: "+25%", base: "+10%", bear: "-8%",
          assumptions: ["息差企稳"], invalid_signals: ["不良率跳升"],
        },
        key_logic: ["高股息 + 低估值"], risks: ["区域经济下行"],
      },
      {
        code: "601088", name: "中国神华", stance: "持有",
        position_advice: null,
        expected_return_3_6m: { bull: "+18%", base: "+6%", bear: "-12%" },
        key_logic: ["长协煤价稳定"], risks: ["煤价中枢下移"],
      },
    ],
  }),
];

// ── ① 解析层：缺席 / 读得出 / 读不出 三态 ──────────────────────────────
console.log("== ① 字段读取三态 ==");
check(readValuationByCode(SINGLE).kind === "absent",
  "单标的：valuation_calc_by_code 缺席（走原 valuation_calc）");
check(readPerSubject(SINGLE).kind === "absent", "单标的：per_subject 缺席");
check(readSentimentMetricsByGroup(SINGLE).kind === "absent",
  "单标的：sentiment_metrics_by_group 缺席");
check(readGenericIndustrySections(SINGLE).kind === "absent",
  "单标的：generic_industry_sections 缺席");
const singleCompliance = readCompliancePerCode(SINGLE);
check(singleCompliance.kind === "ok" && singleCompliance.value.length === 1,
  "单标的：compliance_per_code **在场且只有 1 条**（不是缺席）");
check(readValuationByCode(MULTI).kind === "ok", "多标的：逐只估值读得出");
const multiRecommend = readPerSubject(MULTI);
check(multiRecommend.kind === "ok" && multiRecommend.value.length === 2,
  "多标的：per_subject 两条", multiRecommend.kind === "ok" ? `${multiRecommend.value.length}` : "");

// ── ② ★★ 单标的：**一个节点都不许多**（渲染必须与改动前逐字一致）────────
console.log("== ② 单标的渲染（逐字一致） ==");
check(buildMultiSubjectView(SINGLE).length === 0,
  "单标的：展示模型里一个小节都没有");
check(buildMultiSubjectView(null).length === 0, "outputs 为 null 不炸");
check(buildMultiSubjectView([]).length === 0, "outputs 为空数组不炸");
const singleHtml = render(SINGLE);
check(singleHtml === "", "★ 单标的渲染结果是**空串**（连容器都没有）", JSON.stringify(singleHtml).slice(0, 80));
check(!singleHtml.includes("逐只结论"), "单标的：没有「逐只结论」标题");
check(!singleHtml.includes("ms-section"), "单标的：没有小节容器");
check(!singleHtml.includes("（未提供）"), "单标的：不冒出「（未提供）」占位");
check(!singleHtml.includes("multi-subject"), "单标的：没有跨列容器（网格布局不受影响）");

// ── ③ 多标的：5 个字段各自成节 ────────────────────────────────────────
console.log("== ③ 多标的渲染（5 个字段） ==");
const multi = render(MULTI);
check(multi.includes("multi-subject"), "跨两列的多标的容器渲染");
check(multi.includes("逐只结论（多标的）"), "总标题");
check(multi.includes("宁波银行（002142）") && multi.includes("中国神华（601088）"),
  "两只票**都在**（逐只结论的核心诉求）");
// ① A17 per_subject
check(multi.includes("逐只建议") && multi.includes("per_subject"),
  "① A17 逐只建议小节 + 字段出处");
check(multi.includes("立场 增持") && multi.includes("立场 持有"), "① 逐只立场");
check(multi.includes("底仓 5%~8%"), "① 逐只仓位建议");
check(multi.includes("乐观情景（3–6 个月）") && multi.includes("悲观情景（3–6 个月）"),
  "① 逐只三档情景");
check(multi.includes("长协煤价稳定"), "① 逐只核心逻辑");
// ② A10 valuation_calc_by_code
check(multi.includes("逐只估值") && multi.includes("valuation_calc_by_code"),
  "② A10 逐只估值小节");
check(multi.includes("估值 低估") && multi.includes("估值 历史区间内"), "② 逐只估值判定");
check(multi.includes("PE 历史分位") && multi.includes("46%（0% = 历史最便宜）"),
  "② 历史分位（有才显示）");
check(count(multi, "PE 历史分位") === 1, "② 没有分位的那只票不显示空分位行",
  `出现 ${count(multi, "PE 历史分位")} 次`);
// ③ A07 sentiment_metrics_by_group
check(multi.includes("逐只舆情") && multi.includes("sentiment_metrics_by_group"),
  "③ A07 逐只舆情小节");
check(multi.includes("情绪分 0.42") && multi.includes("情绪分 -0.31"), "③ 逐只情绪分（含负分）");
check(multi.includes("周期定位 乐观") && multi.includes("周期定位 谨慎"),
  "③ 逐只 phase（伴随字段 sentiment_phase_by_group）");
check(multi.includes("热点主体") && multi.includes("宁波银行（净分 0.8）"),
  "③ 逐只热点主体");
// ④ A12 compliance_per_code
check(multi.includes("逐只合规") && multi.includes("compliance_per_code"),
  "④ A12 逐只合规小节");
check(multi.includes("合规等级 中") && multi.includes("合规等级 无"), "④ 逐只合规等级");
check(multi.includes("对外担保占净资产 38%（超 30% 阈值）"), "④ 逐只风险旗标");
check(count(multi, "对外担保占净资产 38%（超 30% 阈值）") === 1,
  "④ 同一句旗标只出现一次（risk_flags 与 flags 不重复渲染）",
  `出现 ${count(multi, "对外担保占净资产 38%（超 30% 阈值）")} 次`);
check(multi.includes("未量到的族"), "④ 未量到的族如实列出");
// ⑤ A20 generic_industry_sections
check(multi.includes("逐行业结论") && multi.includes("generic_industry_sections"),
  "⑤ A20 逐行业小节");
check(multi.includes("息差企稳、资产质量平稳"), "⑤ 银行的结论");
check(multi.includes("价格中枢下移风险未消"), "⑤ 煤炭的结论");
check(multi.includes("行业判定依据"), "⑤ 行业判定依据（为什么算一个行业）");
check(multi.includes("另有 1 个行业本次未分析（券商）"),
  "⑤ 截断如实声明（industries_truncated，不许静默少写）");

// 行数守恒：5 节 × 2 条 = 10 条，一条不多一条不少
const rowCount = count(multi, '"ms-row"');
check(rowCount === 10, "多标的：10 条逐只行（2 票 × 5 个字段）", `${rowCount}`);
const sectionCount = count(multi, '"ms-section"');
check(sectionCount === 5, "多标的：5 个小节", `${sectionCount}`);

// ── ④ 只有一条的字段**不许**让整节消失，也不许多渲染 ────────────────────
console.log("== ④ 部分字段只有 1 条 ==");
const partial = render([
  out("A10_micro", { valuation_calc: { valuation: "合理", basis: "无", detail: "" } }),
  out("A17_recommend", {
    per_subject: [{ code: "002142", name: "宁波银行", stance: "增持" }],
  }),
  out("A07_sentiment", { sentiment_metrics_by_group: { "002142": { weighted_sentiment: 0, event_count: 1, distribution: {}, top_subjects: [] } } }),
  out("A12_compliance", {
    compliance_per_code: [{
      code: "002142", label: "002142", level: "无", flags: ["未见异常"],
      risk_flags: [], families_measured: [], families_unmeasured: [],
      measured: true, severe_flag_count: 0, event_flags: 0,
    }],
  }),
  out("A20_generic_industry", {
    generic_industry_sections: [{ industry: "银行", conclusion: "…" }],
  }),
]);
check(partial === "", "5 个字段**都只有 1 条** ⇒ 仍然一个节点都不加",
  JSON.stringify(partial).slice(0, 80));

const onlyOne = render([
  out("A17_recommend", {
    per_subject: [
      { code: "002142", name: "宁波银行", stance: "增持" },
      { code: "601088", name: "中国神华", stance: "持有" },
    ],
  }),
]);
check(count(onlyOne, '"ms-section"') === 1,
  "只有 A17 多标的时**只**渲染 1 节（缺席的 4 个字段不留空壳）",
  `${count(onlyOne, '"ms-section"')}`);
check(!onlyOne.includes("逐只估值") && !onlyOne.includes("逐只合规"),
  "缺席字段没有标题");

// ── ⑤ ★ 在场但读不出：如实"（未提供）"，不崩、不整块消失 ─────────────────
console.log("== ⑤ 在场但读不出 ==");
const broken = render([
  out("A17_recommend", { per_subject: "本次没有逐只表态" }),
  out("A12_compliance", { compliance_per_code: [] }),
  out("A10_micro", { valuation_calc_by_code: { "002142": "低估" } }),
  out("A07_sentiment", { sentiment_metrics_by_group: 42 }),
  out("A20_generic_industry", { generic_industry_sections: [{ industry: "银行" }, null] }),
]);
check(broken.includes("（未提供）"), "形状不对时显示「（未提供）」");
check(broken.includes("逐只建议") && broken.includes("不是数组"),
  "① A17：字符串 ⇒ 说清「不是数组」");
check(broken.includes("一条逐只结论都没有"),
  "④ A12：空数组 ⇒ 说清「一条都没有」（不是静默消失）");
check(broken.includes("代码 002142 的取值是字符串"),
  "② A10：dict 的值不是对象 ⇒ 指到具体代码");
check(broken.includes("逐只舆情") && broken.includes("是数字"),
  "③ A07：整体不是对象 ⇒ 说清它是什么类型");
check(broken.includes("第 2 条是null"), "⑤ A20：数组里混进 null ⇒ 指到具体条目");
check(count(broken, '"ms-section"') === 5, "5 节**都还在**（读不出 ≠ 整块消失）",
  `${count(broken, '"ms-section"')}`);
check(count(broken, '"ms-row"') === 0, "读不出的节没有行（不留半截行）");

// ── ⑥ ★ "没量到" ≠ "量到 0" ────────────────────────────────────────────
console.log("== ⑥ 没量到 ≠ 量到 0 ==");
const unknownMeasured = render([
  out("A12_compliance", {
    compliance_per_code: [
      // `measured` 根本没给：不许说成"未量到"（那是替它签合格证）
      { code: "002142", label: "002142", level: "未量到" },
      // 明确量到了、且真的没有风险旗标 → 这才是"无"
      {
        code: "601088", label: "601088", level: "无", flags: ["未见异常"],
        risk_flags: [], families_measured: ["质押比例"], families_unmeasured: [],
        measured: true, severe_flag_count: 0, event_flags: 0,
      },
    ],
  }),
]);
check(unknownMeasured.includes("是否量到"), "合规行有「是否量到」这一格");
// ⚠️ 断言精确到标签/值的一对（`level` 那一格本来就可能是"未量到"，不能拿全文计数）
check(unknownMeasured.includes("<dt>是否量到</dt><dd>（未提供）</dd>"),
  "① `measured` 读不到 ⇒ 那一格写「（未提供）」，**不写**「未量到」");
check(unknownMeasured.includes("<dt>是否量到</dt><dd>已量到</dd>"),
  "① 真的量到了才写「已量到」");
check(!unknownMeasured.includes("<dt>是否量到</dt><dd>未量到</dd>"),
  "① 读不到**绝不**降级成「未量到」（那等于替它签合格证）");
check(unknownMeasured.includes("合规等级 未量到"),
  "① 后端自己给的 level=未量到 照原样显示（不是我们猜的）");

// 情绪分 0 是**有效读数**（中性），不许当成"没有值"
const zeroSentiment = render([
  out("A07_sentiment", {
    sentiment_metrics_by_group: {
      "002142": { weighted_sentiment: 0, event_count: 0, distribution: {}, top_subjects: [] },
      "601088": { weighted_sentiment: 0.5, event_count: 2, distribution: { positive: 2 }, top_subjects: [] },
    },
  }),
]);
check(zeroSentiment.includes("情绪分 0") && zeroSentiment.includes("情绪分 0.5"),
  "③ 情绪分 0 照原样显示（0 ≠ 没量到）");
check(zeroSentiment.includes("方向分布"), "③ 空分布也有一格（不静默留空）");

// ── ⑦ 同一节里的两条说明互不顶替 ──────────────────────────────────────
console.log("== ⑦ 读不出的条目 与 被截断的行业 并存 ==");
const bothNotes = render([
  out("A20_generic_industry", {
    generic_industry_sections: [
      { industry: "银行", conclusion: "…" },
      { industry: "", conclusion: "…" },
    ],
    industries_truncated: ["券商", "地产"],
  }),
]);
check(bothNotes.includes("第 2 节没有行业名"), "读不出的条目如实报出");
check(bothNotes.includes("另有 2 个行业本次未分析（券商、地产）"),
  "截断说明也留着（两条 note 不许用 `??` 互相顶替）");

console.log(failures === 0
  ? "\n✅ 多标的渲染自检全部通过"
  : `\n❌ ${failures} 项失败`);

// ⚠️ 与 `intelRenderCheck.tsx` 同款：`web` 的 tsconfig 没有 `@types/node`
// （前端代码本来用不到 node API），这里按需取 `process`；
// 拿不到就把失败**抛出去** —— 绝不能"静默以 0 退出"，那等于检查白跑。
const proc = (globalThis as { process?: { exitCode?: number } }).process;
if (proc) {
  proc.exitCode = failures === 0 ? 0 : 1;
} else if (failures > 0) {
  throw new Error(`多标的渲染自检失败 ${failures} 项`);
}
