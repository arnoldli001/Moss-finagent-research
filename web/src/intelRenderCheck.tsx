/**
 * 情报中心**渲染回归检查**（`npm run check:intel`）。
 *
 * ## 为什么需要一个"跑真组件"的检查
 *
 * 这一页的形态很容易**静默退化**，而 `tsc` 与 `vite build` 都拦不住：
 *
 *   · `tone.explain` / `phrases` / `events` / `bullish` / `bearish` /
 *     `credibility` **每一个都可能缺席**，缺席的表现是"那块空着"；
 *   · 详情面板是**一份实现三个位置**（桌面右栏 / 就地展开 / 手机卡片），
 *     少渲染一处只表现为"某个尺寸下少一块东西"；
 *   · 列表行是"标题 + 摘要"拼出来的，摘要里可能整段重复标题；
 *   · 收容组走的是**另一份投影**（服务端 `_group_row()`）。
 *
 * 今天真出过两次：① 收容组展开后组内 `<li>` 渲染了两遍（240 = 120×2）；
 * ② 星球帖的标题就是正文前 60 字，列表里同一句话连着出现两遍。
 * 两个都是"看得出来但不会报错"的形态。
 *
 * ## 它不联网、不读数据
 *
 * 全部用固定 fixture 渲染（`renderToStaticMarkup`），所以能进 CI：
 * 没有后端、没有 cookie、没有数据库。
 *
 * 用法：`cd web && npm run check:intel`
 */
import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import IntelFeedTab, {
  IntelDirectionAnalysis,
  IntelItemDetail,
} from "./components/intel/IntelFeedTab";
import {
  trimRepeatedHead,
  type IntelCredibility,
  type IntelFeed,
  type IntelItem,
} from "./intelApi";

// `useIntelLayout()` 的首帧初值读 `window.innerWidth`（纯浏览器应用），
// SSR 里没有 window，补一个最小替身。1180 以上 → 走"右栏"那一支。
(globalThis as unknown as { window: unknown }).window = {
  innerWidth: 1440, addEventListener() {}, removeEventListener() {},
};

let failures = 0;
function check(ok: boolean, label: string, extra = "") {
  if (!ok) failures += 1;
  console.log(`  ${ok ? "OK  " : "FAIL"} ${label}${extra ? `  ${extra}` : ""}`);
}
const render = (el: unknown) =>
  renderToStaticMarkup(el as Parameters<typeof renderToStaticMarkup>[0]);

// ── fixture ────────────────────────────────────────────────────────────
const CRED: IntelCredibility = {
  score: 84, source_base: 84, content_base: 78, source_reason: "持牌机构研报",
  content_reason: "事实要素齐全", corroboration: null, tone_allowed: true,
  explain: "来源轴 84 + 内容轴 78",
};

function item(over: Partial<IntelItem>): IntelItem {
  return {
    kind: "newswire", kind_label: "财经快讯", title: "标题", summary: "",
    published_at: "2026-09-25T21:33:10+0800", source_alias: "src-abc",
    codes: [], industry: "", rating_origin: "", agency: "",
    content_hash: "h1", extra: {},
    ...over,
  } as IntelItem;
}

// ★ 真实形态：知识星球的 `title` 就是正文前 60 字，`summary` 是同一段正文。
//   这正是"同一句话在列表里出现两遍"的那个案例，必须常驻在 fixture 里。
const LONG = "摩根大通——建滔积层板（1888.HK）：景旺、利升、份额扩张三重催化，"
  + "2028年净利润预测上调，首次覆盖予「增持」评级，目标价62.7港元。";
const note = item({
  content_hash: "h-note", kind: "research_note", kind_label: "券商作文",
  title: LONG.slice(0, 60),
  summary: LONG,
  summary_text: { text: LONG, kind: "excerpt", truncated: true },
  credibility: { ...CRED, score: 58, source_base: 54, content_base: 78 },
  tone: {
    tone: "偏多", has_tone: true, neutral: false, confidence: 0.9,
    source: "rules", explain: "词表规则判定（本地模型尚未处理这一条）",
    phrases: ["增持", "上调"],
    events: ["首次覆盖予增持评级"],
    bullish: {
      industries: ["PCB"],
      stocks: [{ name: "景旺电子", code: "603228", count: 1 }],
      count: 2, boards: [],
    },
    bearish: { industries: [], stocks: [], count: 0, boards: [] },
  },
});

const undetermined = item({
  content_hash: "h-none", title: "美国天然气期货跌5.19%",
  summary: "财联社9月25日电，美国天然气期货跌5.19%。",
  tone: {
    tone: "未定", has_tone: false, neutral: false, confidence: null,
    source: "rules+llm", explain: "两层判定不一致，不给倾向",
    phrases: [], events: [],
  },
});

const bare = item({ content_hash: "h-bare", title: "没有任何抽取字段的一条" });

// ★ 留存补回来的条目（`item_store` 里 3 天内的，本轮没取到）。
//   页脚必须**如实说明** —— 不说的话用户会以为这些都是刚抓到的。
const restored = item({
  content_hash: "h-restored", kind: "research_note", kind_label: "券商作文",
  title: "摩根大通——中国AI价值链：不改", summary: "大摩：看好中国AI价值链，首选阿里腾讯智谱。",
  summary_text: { text: "大摩：看好中国AI价值链，首选阿里腾讯智谱。",
                  kind: "model", truncated: false },
  published_at: "2026-09-25T20:38:00+0800",
  retained: true,
});

const group = item({
  content_hash: "h-group", title: "其他公开信息 12 条", is_group: true,
  group_count: 12, summary: "这一组里不涉及个股、概念板块、美股或 AI。",
  group_items: [{
    title: "组内一条", summary: "组内摘要", kind_label: "财经快讯",
    published_at: "2026-09-25T10:00:00+0800", platform: "",
    credibility_score: 54, content_hash: "h-sub",
    tone: { tone: "未定", has_tone: false, source: "rules" },
  }],
} as Partial<IntelItem>);

const feed = {
  items: [note, undetermined, bare, restored, group],
  gaps: [], counts: { broker_report: 1 },
  fetched_at: "2026-09-25T23:00:00+08:00", degraded: false,
  credibility_dist: { high: 1, low: 3 },
  filters: { all: "全部", high: "高可信" },
  tone_dist: { 偏多: 1 }, cluster_stats: { folded: 0, clusters: 0 },
} as unknown as IntelFeed;

// ── ① 摘要去重（纯函数）───────────────────────────────────────────────
console.log("== ① 摘要里重复的标题开头 ==");
check(trimRepeatedHead("摩根大通——建滔", "摩根大通——建滔积层板：景旺、利升。")
  === "积层板：景旺、利升。", "逐字重复的开头被剥掉");
check(trimRepeatedHead("摩根 大通", "摩根大通：景旺电子首次覆盖予增持评级。") === "景旺电子首次覆盖予增持评级。",
  "空白不参与比较（换行/空格差异也算重复）");
check(trimRepeatedHead("标题", "完全不相干的一段正文内容") === "完全不相干的一段正文内容",
  "不重复就**原样返回**，不猜");
check(trimRepeatedHead("一整句就是标题本身", "一整句就是标题本身") === "",
  "摘要本来就是标题 → 返回空串（不给一截断头话）");
check(trimRepeatedHead("短", "短。") === "", "剥完太短也返回空串");
check(trimRepeatedHead("", "正文") === "正文", "没有标题时原样返回");

// ── ② 列表（主从结构左栏）─────────────────────────────────────────────
console.log("== ② 列表行 ==");
let list = "";
try {
  list = render(h(IntelFeedTab, {
    feed, sort: "credibility", onSort: () => {}, filter: "all",
    onFilter: () => {}, busy: false,
  }));
} catch (e) {
  check(false, "渲染抛异常", String(e));
}
check(list.includes("intel-split"), "主从容器 .intel-split 渲染");
check(list.includes("intel-row-text"), "行内是一整块可读文字");
// ★ 三轮口径的最终形态：列表行**直接显示摘要/摘录**，不再"标题 + 摘要"拼接。
//   标题是正文前 60 字的残句（"瑞银——美光科技（MU.US）…"），
//   用户口径："直接显示摘录就行了，标题被裁剪严重"。
check(!list.includes("intel-row-sum") || !list.includes("h-note"),
  "普通条目的行内没有「标题 + 摘要」两份文字");
check(list.includes("摘录"), "摘录带徽标（不把「截断的」当成「全部的」）");
check(list.includes("intel-pane-empty"), "右栏空态");
check(!list.includes("intel-analysis"), "未选中时右栏没有多空分析面板");
check(list.includes("intel-row-group"), "收容组走单独行形态");
// ★ 行内**只有摘要**：普通条目不再单独渲染标题（标题是正文前 60 字的残句，
//   用户口径："直接显示摘录就行了，标题被裁剪严重"）。收容组例外 ——
//   它的标题是服务端合成的短标题（"其他公开信息（120 条）"），不是残句。
check(!list.includes("intel-row-title"),
  "普通条目行内不再单独渲染标题（避免「残句 + 整句」两份文字）");
check(list.includes("intel-row-group-title"), "收容组行保留标题 + 组说明");
check(list.includes("景旺、利升"), "行内显示的正是那段摘要");
// 每一条都要渲染出来（曾经出过"隐藏分支又渲染一份"的重复，计数要精确）
const rowCount = list.split("intel-row-btn").length - 1;
check(rowCount === feed.items.length, "列表行数 == payload 条数",
  `${rowCount} / ${feed.items.length}`);
// ★ 留存补回来的条目要在页脚**如实说明**（用户口径"落库最多保留 3 天"）。
//   不说的话用户会以为这些都是本轮刚抓到的 —— 那是"把请求当保证"的同类错误。
check(list.includes("来自留存"), "页脚说明了「其中 N 条来自留存」",
  `retained=${feed.items.filter((i) => i.retained).length}`);

// ── ③ 多空分析 ────────────────────────────────────────────────────────
console.log("== ③ 多空分析 ==");
const a1 = render(h(IntelDirectionAnalysis, { item: note }));
check(a1.includes("多空分析") && a1.includes("触发依据"), "标题与触发依据");
check(a1.includes("利好") && a1.includes("景旺电子") && a1.includes("603228"),
  "利多个股（名 + 代码）");
check(!a1.includes("利空"), "空的一侧不渲染（不留空标题）");
check(a1.includes("词表判定"), "判定来源如实标注（词表 ≠ 模型）");
check(!a1.includes("影响路径"), "不出现『影响路径』（本链路没有这个字段）");

const a2 = render(h(IntelDirectionAnalysis, { item: undetermined }));
check(a2.includes("未定（规则层与模型判定不一致"), "判定格写出『未定』而不是留空");
check(a2.includes("没有点名可归因的板块或个股"), "两侧空时给人话，不留空壳");
check(a2.includes("不是功能故障"), "明说这不是故障");

check(render(h(IntelDirectionAnalysis, { item: bare })).includes("尚未抽取"),
  "没抽取过说『尚未抽取』（状态 ≠ 结论）");

// ── ④ 详情面板 ────────────────────────────────────────────────────────
console.log("== ④ 详情面板 ==");
const TODAY = "2026-09-25";
const d1 = render(h(IntelItemDetail, {
  item: note, today: TODAY, detailHash: "",
  onToggleText: () => {}, onClose: () => {}, renderDetail: () => null,
}));
check(d1.includes("intel-detail-title"), "标题");
check(d1.includes("机构") || d1.includes("可信度"), "机构 / 可信度");
check(d1.includes("查看原文"), "全文入口");
check(d1.includes("intel-detail-close"), "给了 onClose 才有关闭按钮");
const dNote = d1.split("景旺、利升").length - 1;
check(dNote <= 1, "详情里标题与摘要也不重复", `出现 ${dNote} 次`);

check(!render(h(IntelItemDetail, {
  item: note, today: TODAY, detailHash: "",
  onToggleText: () => {}, renderDetail: () => null,
})).includes("intel-detail-close"), "就地展开形态没有关闭按钮");

const d3 = render(h(IntelItemDetail, {
  item: group, today: TODAY, detailHash: "",
  onToggleText: () => {}, onClose: () => {}, renderDetail: () => null,
}));
check(d3.includes("intel-group-list") && d3.includes("组内一条"),
  "收容组的详情渲染组内条目列表");
check(!d3.includes("多空分析"), "收容组容器不做多空分析（它没有方向）");

check(render(h(IntelItemDetail, {
  item: bare, today: TODAY, detailHash: "",
  onToggleText: () => {}, onClose: () => {}, renderDetail: () => null,
})).includes("多空分析"), "字段全缺的详情照样渲染");

console.log(failures === 0
  ? "\n✅ 情报中心渲染检查全部通过"
  : `\n❌ ${failures} 项失败`);

// ⚠️ 这个检查跑在 node 里（`npm run check:intel`），但 `web` 的 tsconfig **没有**
// `@types/node`（前端代码本来用不到 node API）。为了一个检查文件往
// devDependencies 里塞 `@types/node` 不划算，这里按需取一下：
// 拿不到就把失败**抛出去** —— 绝不能"静默以 0 退出"，那等于检查白跑。
const proc = (globalThis as { process?: { exitCode?: number } }).process;
if (proc) {
  proc.exitCode = failures === 0 ? 0 : 1;
} else if (failures > 0) {
  throw new Error(`情报中心渲染检查失败 ${failures} 项`);
}
