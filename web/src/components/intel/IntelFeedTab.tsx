/**
 * 情报流：按类型筛选 + 时间倒序展示。
 *
 * ## 版式：**主从结构**（左列表 + 右详情），照「事件告警」
 *
 * 用户口径（2026-10-01）：
 *
 * > "我在想情报中心的热点&研报小作文，为什么不能做成事件告警里那样的效果呢，
 * >  感觉那种更整齐，还有多空分析"
 *
 * 具体分工见 `IntelListRow` 与 `IntelItemDetail` 的说明。一句话：
 * **列表只留"方向标 + 标题 + 一行 meta"，其余全部搬进详情**。
 * 这是"整齐"的真正来源 —— 原来那种 5 列表格每行塞八样东西，
 * 列宽被内容牵着走，行高就没节奏了。
 *
 * ## 分栏由 `useWidePane()` 决定（≥1180px 右栏，更窄就地展开）
 *
 * ⚠️ 这一处与下面第 ① 条**不冲突，是两件事**：单条**条目内容**的
 * 折叠/展开仍然是 CSS 断点（两套 DOM，CSS 选一个）；而**详情面板**
 * 只能渲染一次，它在右栏与"行下面"是两个不同的**位置**，
 * CSS 挪不动一个 DOM（`position: fixed` 会把滚动/层级/宽度全绑死）。
 *
 * ## 三个仍然成立的移动端设计决定
 *
 * **① 桌面用列表，手机用卡片 —— 两套 DOM，用 CSS 切换。**
 * 一行列表在 390px 里放不下"方向标 + 标题 + 一行 meta"（标题会被压成
 * 每行两三个字）。卡片天然纵向堆叠。
 * 两套 DOM 的代价是重复渲染，但避免了在同一个 DOM 上打两套样式补丁
 * （那种做法一旦加列就会两边不一致）。
 * ⚠️ 两套 DOM 里**显示的字段**必须一致 —— 所以 meta 行收敛成了
 * `RowMeta` 一个组件，详情收敛成了 `IntelItemDetail` 一个组件。
 * 各写一份的表现是"同一条在手机上有可信度、到桌面反而没有"，而且不报错。
 *
 * **② `source_alias` 只用于同源判断，不上屏。**
 * 它是稳定假名（`src-3f9a2c1b`），不是给人看的。界面上显示的是
 * `kind_label`（"财经快讯"/"券商研报"…）。把假名印出来等于告诉用户
 * "这几条同源" —— 那是我们自己的聚合口径，没必要交付。
 *
 * **③ 不做按天分组标题 —— 这是踩过坑改回来的。**
 *
 * 第一版按 `published_at` 分天、每天一个标题。看着合理，但实测数据是：
 *
 *     2026-09-25  快讯 ×140
 *     2026-09-24  政策 ×15
 *     2026-09-22  笔记 ×30
 *
 * **每一天只有一种类型**（快讯天天有，政策按日发，笔记断续来）。
 * 而后端取样必须保证类型多样（否则 140 条快讯会把政策与笔记全挤掉），
 * 两者一叠加，分组标题就变成：
 *
 *     今天 / 09-24 / 今天 / 09-24 / 今天 …
 *
 * 同一天被切得粉碎，iPad 上一屏能出现好几组，"今天到底有什么"根本读不出来。
 * 而"天分组 / 类型多样 / 严格时间序"在这份数据上**不可能同时满足**。
 *
 * 取舍：**时序信息改由每行承载**（时间 + 今天/昨天/日期），不靠分组标题。
 * 于是既保住了类型多样，也没有"同一天被切碎"的问题。
 *
 * **④ 点击查看全文（用户口径 2026-10-01）。**
 *
 * 展示契约把 `summary` 截到 260 字（移动端一条 3400 字占满十屏），
 * 所以"截断展示"必须配一条按需取全文的路：详情面板里的「查看原文」
 * → `GET /intel/item/{hash}` → 原地摊开（再点收起）。
 *
 *   · 全文**按 `content_hash` 懒加载并缓存**：一页几十条全取既慢又没必要；
 *   · 三态齐全（读取中 / 失败可重试 / 成功），404 单独给一句人话
 *     （没留存 or 超出 3 天留存窗口 —— 对用户是同一件事）；
 *   · **纯文本渲染**（`{text}`），绝不用 `dangerouslySetInnerHTML`：
 *     那是第三方原文，注 HTML 等于在情报流上开一个 XSS 口子；
 *   · 收容组容器（`is_group`）没有指纹与原文，**不给**这个按钮 ——
 *     给一个点了永远报错的按钮比不给按钮更糟。
 *
 * **⑤ 方向标记【多】/【空】：绿=利多、红=利空（用户明确要求）。**
 *
 * ⚠️ 这与本页其余地方的 **A 股约定「红涨绿跌」正好相反**，不是笔误：
 * 所以标记走**独立 class 与独立 CSS token**（见 `DirectionTag` 与
 * `intel.css` 里那一段），绝不复用 `.up` / `.down` / `.intel-hl` ——
 * 复用会让"调一下涨跌配色"顺手把方向标记也调反，而颜色不会报错。
 * 没有方向（未抽取 / 未定 / 中性）时**什么都不加**，不留空占位。
 *
 * **⑥ 多空分析（2026-10-01 新增）。**
 *
 * 详情面板里的 `IntelDirectionAnalysis` 把后端**早就在发、界面上一直没渲染**
 * 的那几个字段（`tone.explain` / `phrases` / `events` / `bullish` / `bearish`）
 * 摆出来。它与事件告警的"多空分析"**不是同一种东西**（那边含模型判断），
 * 理由与"影响路径这一项怎么填"见 `IntelDirectionAnalysis` 的说明。
 */

import { Fragment, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import {
  credLevel, credLevelLabel, dayKey, daysBetween, directionMarker,
  directionSourceLabel, directionTitle, displaySummary, fetchIntelItem,
  fmtNum, formatTime, GroupRow, IntelCredibility, IntelFeed, IntelItem,
  IntelSide, IntelTone, noDirectionLabel, sideHasContent, sideOf, todayKey,
  trimRepeatedHead,
} from "../../intelApi";
import { ApiError } from "../../errors";
import IntelHeatBlock from "./IntelHeatBlock";

/** 排序模式。服务端决定"谁能进这一页"，前端只切它。 */
type SortKey = "credibility" | "time";

const SORTS: Array<{ key: SortKey; label: string; hint: string }> = [
  { key: "credibility", label: "优先高可信", hint: "取样时优先纳入可信度更高的条目" },
  { key: "time", label: "纯时间", hint: "不做可信度加权，按时间取最近的一批" },
];

/**
 * 原文倾向（多/空）档位。用户口径（2026-09-26）：
 *
 * > "intel-controls 表头要加一个 多/空 的过滤选择器（多还是空 是模型
 * >   分析出来的 每条信息第一个字【空】【多】）"
 *
 * ## 为什么文案直接用【多】/【空】
 *
 * 那是**列表标题前的同一个标记**（`DirectionTag` / `directionMarker`）。
 * 筛选项与它逐字一致，用户一眼就能把"我筛的是什么"和"我在列表里看到什么"
 * 对上 —— 写成"看多/看空"或"利多/利空"都会让人怀疑是不是同一件事。
 *
 * ## 为什么与可信度档位分开
 *
 * 多空与可信度是**两个正交的轴**。并进同一个 tab 组的话，用户在
 * 「高可信」档下切到【多】就会丢掉可信度档位，"高可信 + 偏多"表达不出来。
 *
 * `countKey` 指向 `feed.direction_dist` 里的计数（**当前可信度档位之内**
 * 的条数，不是全量 `tone_dist`）。
 */
const DIRECTIONS: Array<{
  key: string; label: string; hint: string;
  countKey?: "bull" | "bear"; cls?: string;
}> = [
  { key: "all", label: "全部方向",
    hint: "不做倾向筛选，含未判定方向的条目" },
  { key: "bull", label: "【多】", countKey: "bull", cls: "dir-bull",
    hint: "只看模型判定为偏多（利多）的条目 —— 与列表标题前的【多】同一判据" },
  { key: "bear", label: "【空】", countKey: "bear", cls: "dir-bear",
    hint: "只看模型判定为偏空（利空）的条目 —— 与列表标题前的【空】同一判据" },
];

/**
 * 把标题/摘要里命中的词标成**绿色大字**（`<mark class="intel-hl">`）。
 *
 * 用户口径（2026-09-25）："涉及股票、概念板块、美股、AI 的…把命中的词
 * 突出显示。" 命中的词由**后端**给出（`item.highlights`）。
 *
 * ## 两条硬约束
 *
 * 1. **必须用 React 节点拼装，绝不能用 `dangerouslySetInnerHTML`。**
 *    标题与摘要都是第三方原文（研报、快讯、星球笔记），把 HTML 注进去
 *    等于在情报流上开一个 XSS 口子 —— 而"原文照原样显示"正是这一页的
 *    前提。拼节点还有个附带好处：不会有标签被原文吃掉的问题。
 * 2. **不在这里重新判定"哪些词算命中"。** 后端给的就是逐字出现在这段
 *    文字里的词（长度降序）。前端再写一套匹配规则必然与后端漂移，
 *    用户看到的绿字就会与"为什么这条没被折叠"对不上。
 *
 * 长词优先（后端已排好序，这里再按长度排一次兜底）：`存储芯片` 必须先被
 * 吃掉，否则 `芯片` 会把它切成"存储"+绿"芯片"两截。
 */
function highlight(text: string, terms?: string[]): ReactNode {
  if (!text) return text;
  const hits = (terms ?? []).filter((t) => t && text.includes(t));
  if (hits.length === 0) return text;
  const ordered = [...hits].sort((a, b) => b.length - a.length);
  const out: ReactNode[] = [];
  let buf = "";
  let i = 0;
  let key = 0;
  while (i < text.length) {
    const hit = ordered.find((t) => text.startsWith(t, i));
    if (hit) {
      if (buf) { out.push(buf); buf = ""; }
      out.push(<mark className="intel-hl" key={`hl-${key++}`}>{hit}</mark>);
      i += hit.length;
    } else {
      buf += text[i];
      i += 1;
    }
  }
  if (buf) out.push(buf);
  return out;
}

/**
 * **带标签的名字 chip 行**（`.intel-inst-row`）—— 机构名与分析师名**共用**这一份。
 *
 * ## 为什么合成一个（而不是复制一份 AnalystChips）
 *
 * 两者的形状、纪律、上限、`title` 全量提示完全一样，只有**前缀文字**与
 * 一个配色修饰类不同。抄一份出去的表现是"改了一处（比如把上限从 3 改成 4）
 * 另一处没改"，而两条 chip 行看起来一模一样 —— 没人会发现它们不一致。
 *
 * 四条纪律（原 `InstChips` 的，逐条保留）：
 *
 * 1. **绝不复用 `.intel-hl`（绿色大字）**：那个绿在这一页的语义是
 *    "命中的个股 / 板块 / 美股 / AI"，是**信号**；机构与分析师回答的是
 *    "谁在说"，是**背景**。同一种绿等于把"谁在吹"和"吹的是谁"混成一条。
 * 2. **每个 chip 自带前缀**：只靠颜色区分不够（色弱、11px 小字下更不够），
 *    前缀是灰度下也成立的区分。
 * 3. **最多露 3 个，其余折成 `+N`**，`title` 里给**全量** —— 名字是附加信息，
 *    不能把摘要挤掉。
 * 4. **空数组 / 缺字段一律不渲染**（老数据没有这个字段）：不留空 chip，
 *    也不留空行 —— 所以整行包在组件里，由组件自己决定要不要出现。
 *
 * 前端**不自己从正文里找名字**：判定只有服务端一份（同 `highlight` 的理由）。
 */
function NameChips({ names, label, tone }: {
  names?: string[]; label: string; tone: "inst" | "analyst";
}) {
  const list = (names ?? []).filter((n) => n && n.trim());
  if (list.length === 0) return null;
  const MAX = 3;
  const shown = list.slice(0, MAX);
  const rest = list.length - shown.length;
  const full = `原文提到的${label}（${list.length} 位）：${list.join("、")}`;
  return (
    <div className="intel-inst-row">
      {shown.map((n, i) => (
        <span className={`intel-inst intel-${tone}`} key={`${tone}-${i}-${n}`}
          title={full}>
          <span className="intel-inst-k">{label}：</span>
          {n}
        </span>
      ))}
      {rest > 0 && (
        <span className={`intel-inst intel-${tone} intel-inst-more`}
          title={full}>+{rest}</span>
      )}
    </div>
  );
}

/** 机构名 chip（数据源 `item.institutions`）。 */
function InstChips({ names }: { names?: string[] }) {
  return <NameChips names={names} label="机构" tone="inst" />;
}

/**
 * 分析师名 chip（数据源 `item.analysts`）。
 *
 * 用户口径（2026-10-01）："如果有以下内容必须要输出：股票名 板块名 券商
 * 孙潇雅、赵宇阳、武超则、陈果、刘晨明、洪灏 …… 推送到前端展示。"
 *
 * ⚠️ 与机构名**必须分得开**（前缀 + 配色两重区分）：两个字段回答的问题不同
 * （"哪家机构发的" vs "谁署的名"），混在一起用户没法核对。
 */
function AnalystChips({ names }: { names?: string[] }) {
  return <NameChips names={names} label="分析师" tone="analyst" />;
}

/**
 * **方向标记**：`【多】` / `【空】`，加在标题/摘要**最前面**。
 *
 * ## ⚠️ 颜色是用户明确指定的，而且是本页"红涨绿跌"的**反例**
 *
 * 用户口径（2026-10-01）：偏多（利多）→【多】用**绿色**；
 * 偏空（利空）→【空】用**红色**。
 * 而本页其余地方（热度榜涨跌、`.up`/`.down`）用的是 **A 股约定红涨绿跌**。
 * 两者**正好相反**，这不是笔误 —— 所以这里用专用 class
 * （`intel-dir-bull` / `intel-dir-bear`）与专用配色 token，
 * 绝不复用 `.up`/`.down`/`.intel-hl`：复用会让"改一处涨跌配色"
 * 顺手把方向标记也改反，而且没有任何报错。
 *
 * ## 没有方向 → **什么都不加**
 *
 * 未抽取 / 未定 / 中性一律返回 `null`（不是空 span、不是占位符）：
 * 标题前面留一格空白会让人以为"这里本来有个标记但没显示出来"。
 *
 * ## ⚠️ 来源要说清楚：词表判定 ≠ 模型判定（2026-10-01 第六轮）
 *
 * 用户报障「一条明显偏多的笔记没有【多】标记」的成因是**抽取任务还没排到
 * 那一条**（每 2 小时一班），所以后端在请求路径上用**纯词表**兜了一层
 * （`tone.source === "rules"`）。标记照显，但 tooltip 必须说明它是词表给的 ——
 * 把词表猜测说成模型判定，用户就没法判断这个标签该不该信。
 */
function DirectionTag({ tone }: { tone?: IntelTone | null }) {
  const mark = directionMarker(tone);
  if (!mark) return null;
  return (
    <span className={`intel-dir ${mark.cls}`} title={directionTitle(tone)}>
      {mark.text}
    </span>
  );
}

/**
 * **一行摘要**（单行 + ≤200 字，用户口径 2026-10-01）。
 *
 * > "本地模型提取的信息，要求输出文字不能超过200个"
 * > "精简200字以内，用户没时间看全文，要效率"
 * > "前端信息不要换行"
 *
 * ## 三条纪律
 *
 * 1. **换行在渲染前就被折叠掉**（后端 `one_line()` 已经做过一次，
 *    这里 `displaySummary()` 再兜一次）：CSS 只能改变"怎么画"，
 *    字符串里的 `\n` 一旦漏过去，就会出现"某几条仍然换行"这种
 *    看起来像样式没生效的问题。
 * 2. **摘录要标出来**：用户抱怨的正是"看到的是被截断的内容"。
 *    把摘录当摘要显示 = 把同一个缺陷藏起来，所以 `truncated` 时
 *    补一个「摘录」小徽标（纯文字前缀，不额外占一行）。
 * 3. **不在这里重新判定高亮词**：`highlights` 由服务端给出且逐字在
 *    文本里（见 `highlight`）—— 前端再判一套必然漂移。
 */
function SummaryLine({ src, terms, className = "intel-sum" }: {
  src: { title?: string; summary?: string;
         summary_text?: IntelItem["summary_text"] };
  terms?: string[];
  className?: string;
}) {
  const { text, excerpt } = displaySummary(src);
  // ⚠️ 剥掉与标题重复的开头（知识星球的标题就是正文前 60 字，见
  // `trimRepeatedHead` 的说明）。**放在这里**而不是各调用点：这条摘要
  // 在手机卡片、右侧详情、组内条目三处都会紧跟在标题下面显示 ——
  // 各改一遍必然漏一处，而漏了只表现为"某处同一句话出现两遍"。
  const body = trimRepeatedHead(src.title ?? "", text);
  if (!body) return null;
  return (
    <div className={className}>
      {excerpt && (
        <span className="intel-excerpt-tag"
          title="这段是原文的**摘录**（在句子边界截断），不是模型压缩的摘要">
          摘录
        </span>
      )}
      {highlight(body, terms)}
    </div>
  );
}

/** 全文面板的状态（父组件持有，子组件只渲染）。 */
interface DetailState {
  loading: boolean;
  text: string;
  error: string;
}

/** 全文取不到时给用户的一句话（**不透传响应体原文**，见 `errors.ts`）。 */
function detailErrorText(e: unknown): string {
  const status = e instanceof ApiError ? e.status : 0;
  const code = e instanceof ApiError ? e.code : "";
  // ── 404：**分两种成因**（用户口径 2026-10-01 第六轮）──
  //
  // 用户报障时看到的是"全文读取失败，请稍后重试"，而真实情况是 404。
  // 两件事同时错了：
  //   ① 后端把"编号无效"与"没留存/已过期"混成同一个 404（现在拆成两个码）；
  //   ② 前端把 404 归进"稍后重试"那一类 —— 而"超出 3 天留存窗口"
  //      **重试一万次也不会好**。让用户对着一个永远不会成功的按钮点下去，
  //      是最坏的一种提示（他会一直以为是网络/后端的问题）。
  //
  // 文案主表在 `errors.ts` 的 `COPY`（那是全站唯一的用户可见报错层）；
  // 这里再判一次是为了**不依赖 `ApiError.message` 的拼接**：本机开发时
  // `message` 后面会附上报错码与 trace_id（见 `apiErrorFromResponse`），
  // 直接透传会把 `（item_not_retained trace:…）` 印给用户。
  if (code === "item_not_retained") {
    return "这条全文没有留存（已超出 3 天留存窗口，或该来源未保存正文）";
  }
  if (code === "item_bad_hash") return "这条记录的编号无效，读不到全文";
  if (status === 404) {
    // `code` 没给（后端版本旧 / 中间层吞了 detail）：退回旧文案，
    // **不猜**是哪一种 —— 猜错会把"太老了"说成"后端坏了"。
    return "该条全文未留存（或已超出 3 天留存窗口）";
  }
  if (status === 401) return "登录已过期，请重新登录后再试";
  // 401/404 之外才是"稍后重试有意义"的那一类（5xx / 网络）
  return "全文读取失败，请稍后重试";
}

/**
 * 可点击的标题：点一下在**原地**展开全文（再点收起）。
 *
 * ## 它现在只服务收容组的**子条目**（`GroupList`）
 *
 * 2026-10-01 改成主从结构之后，普通条目的全文入口搬进了详情面板的
 * 「查看原文」按钮（`IntelItemDetail`），列表行本身只负责"选中"。
 * 而收容组展开后的那几十条子条目**没有**各自的行与详情，
 * 所以它们仍然用"点标题就地展开全文"。
 *
 * ## `stopPropagation` 仍然必须留着
 *
 * 手机卡片整卡可点（点它开/关详情），组内条目也在卡片里 ——
 * 不挡一下，"点标题看全文"会连带把卡片折起来，看起来像点击没生效。
 *
 * ## 没有 `content_hash` 时**不可点**
 *
 * 收容组容器（`is_group`）是服务端合成的条目，既没有指纹也没有原文；
 * 老数据也可能缺指纹。这时原样渲染文字（不套 button）——
 * 给一个点了永远报错的按钮比不给按钮更糟。
 */
function TextToggle({ hash, open, onToggle, children, className = "" }: {
  hash: string; open: boolean; onToggle: () => void;
  children: ReactNode; className?: string;
}) {
  if (!hash) return <div className={className}>{children}</div>;
  return (
    <div
      className={`${className} intel-textbtn${open ? " open" : ""}`}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      title={open ? "收起全文" : "点击查看全文"}
      onClick={(e) => {
        // 卡片整体也是可点的（开/关详情），别让这个点击冒泡上去
        e.stopPropagation();
        onToggle();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          onToggle();
        }
      }}
    >
      {children}
    </div>
  );
}

/**
 * **全文面板**（懒加载 + 加载中 / 失败 / 成功三态）。
 *
 * ⚠️ 全文是**第三方原文**，必须当纯文本渲染（`{text}` + CSS `pre-wrap`）。
 * 绝不使用 `dangerouslySetInnerHTML` —— 那等于在情报流上开一个 XSS 口子，
 * 而"原文照原样显示"正是这一页的前提。
 *
 * ⚠️ 全文里**不做高亮**：`highlights` 的词只保证逐字出现在标题或摘要里，
 * 不保证出现在全文的每一段中（全文是清洗后的正文，标题不总在里面）。
 * 标一个用户看不到出处的绿字，就是本项目最忌讳的"核不到的证据"。
 */
function ItemFullText({ hash, open, state, onRetry }: {
  hash: string; open: boolean; state?: DetailState; onRetry: () => void;
}) {
  if (!hash || !open) return null;
  let body: ReactNode;
  if (!state || state.loading) {
    body = <div className="intel-fulltext muted-text">正在读取全文…</div>;
  } else if (state.error) {
    body = (
      <div className="intel-fulltext intel-fulltext-err">
        <span>{state.error}</span>
        <button className="intel-fulltext-retry" onClick={(e) => {
          e.stopPropagation();
          onRetry();
        }}>重试</button>
      </div>
    );
  } else if (!state.text) {
    body = <div className="intel-fulltext muted-text">这条没有可显示的全文。</div>;
  } else {
    body = <div className="intel-fulltext">{state.text}</div>;
  }
  // ⚠️ 整块**吞掉点击**：手机卡片整卡可点（折叠/展开摘要），而全文面板是
  // 可滚动的（`max-height` + `overflow-y`）—— 手指在面板里滑动时很容易
  // 落成一次 click，冒泡上去会把卡片折起来，看起来像"点一下全文就没了"。
  return (
    <div onClick={(e) => e.stopPropagation()}>{body}</div>
  );
}

/** 类型 → 徽标配色 key（与 `.intel-kind-*` 对应）。 */
function kindTone(kind: string): string {
  switch (kind) {
    case "broker_report": return "blue";
    case "policy": return "purple";
    case "research_note": return "green";
    case "newswire": return "gray";
    default: return "gray";
  }
}

/**
 * 每行的时间文案：`{ 时间, 相对日 }`。
 *
 * 相对日解决了"分组标题被拿掉之后，怎么一眼看出这是不是今天的"。
 */
function timeLabel(raw: string, today: string): { text: string; sub: string } {
  const t = formatTime(raw);
  const d = dayKey(raw);
  if (d === today) return { text: t, sub: "今天" };
  const gap = daysBetween(d, today);
  if (gap === 1) return { text: t, sub: "昨天" };
  return { text: t, sub: d.slice(5) };
}

/** 关联标的的展示文本。研报/笔记常常没有标的。 */
function targetsOf(it: IntelItem): string {
  if (it.industry) return it.industry;
  if (it.codes?.length) return it.codes.slice(0, 3).join(" · ")
    + (it.codes.length > 3 ? ` 等${it.codes.length}只` : "");
  return "";
}

/**
 * 可信度环 + 可展开的构成。
 *
 * ## 为什么必须可展开
 *
 * 一个说不出理由的分数，用户只能选择信或不信 —— 两者都不合适。
 * 展开后给出**两轴的分与中文理由**，用户能自己判断这个分合不合理，
 * 也能看出"哦，它低是因为来源是自媒体，不是内容写得差"。
 *
 * ## 合规要点
 *
 * 展开面板里**只有"多可核实"**，没有方向、没有目标价、没有评级。
 * 佐证数第一步是 `null`，显示"暂未统计"而**不是 0**
 * （0 的意思是"查过了没有第二条来源"，与"还没做这个判断"是两件事）。
 */
function CredRing({ cred }: { cred?: IntelCredibility }) {
  const [open, setOpen] = useState(false);
  if (!cred) return <span className="muted-text">—</span>;
  const lv = credLevel(cred.score);
  return (
    <div className={`cred${open ? " open" : ""}`}>
      <button
        className={`cred-ring ${lv}`}
        aria-expanded={open}
        title={cred.explain}
        // ⚠️ `stopPropagation` 是**必须的**：手机卡片整卡可点（点它开/关详情），
        // 分数环也在卡片里 —— 不挡一下，用户点开可信度构成的同时卡片会被折起来，
        // 表现就是"点这个按钮没反应"（面板开了又立刻关掉）。
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
      >
        {cred.score}
      </button>
      <div className="cred-meta">
        <b>{credLevelLabel(cred.score)}</b>
        来源{cred.source_base}+内容{cred.content_base}
      </div>
      {open && (
        <dl className="cred-detail">
          <div>
            <dt>来源轴</dt>
            <dd>
              {cred.source_base} · {cred.source_reason}
            </dd>
          </div>
          <div>
            <dt>内容轴</dt>
            <dd>
              {cred.content_base} · {cred.content_reason}
            </dd>
          </div>
          <div>
            <dt>独立佐证</dt>
            <dd className="muted-text">
              {cred.corroboration === null
                ? "暂未统计（需多源共振聚类）"
                : `${cred.corroboration} 个来源`}
            </dd>
          </div>
          <div className="cred-formula">
            综合 = 0.85 × min(两轴) + 0.15 × max(两轴)
            <br />
            <span className="muted-text">
              低分那一轴占主导 —— 这是"来源分即上限"的实现方式：
              内容写得再好也抬不动低权威来源。
            </span>
          </div>
        </dl>
      )}
    </div>
  );
}

/**
 * **关联数字**：同一件事还有几条别的来源，点开才展开。
 *
 * ## 为什么是"数字 + 点开"而不是直接并列
 *
 * 用户口径（2026-09-25）："相似相同观点的可以聚合成一条的，把信息源
 * 在关联数字里，做好记录即可，点开数字可以展开看。"
 *
 * 所以后端已经把同一簇的非代表条**收起**（情报流里一件事只占一行，
 * 而不是刷五遍），这里只把条数露出来当入口。不默认展开是有意的：
 * 展开态会让每条卡片高度差一倍，一屏能看到的信息少一半。
 *
 * ## 为什么展开里不显示来源名
 *
 * `related` 刻意不带 `source_alias`（假名不上屏）。用户需要判断的是
 * "这几条是不是同一件事、分别多可核实" —— 标题 + 类型 + 分数就够了。
 * 渠道名是我们要保护的资产，不是他要的信息。
 */
function RelatedBadge({ item }: { item: IntelItem }) {
  const [open, setOpen] = useState(false);
  const n = item.related_count ?? 0;
  if (n <= 0) return null;
  const rows = item.related ?? [];
  return (
    <div className={`rel${open ? " open" : ""}`}>
      <button
        className="rel-btn"
        aria-expanded={open}
        title="同一件事的其他来源报道"
        onClick={(e) => {
          // 卡片整体也是可点的（展开摘要），别让这个点击冒泡上去
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        关联 {n} 条{open ? " ▲" : " ▼"}
      </button>
      {open && (
        <ul className="rel-list">
          {rows.map((r, i) => (
            <li key={`${r.title}-${i}`}>
              <span className="rel-score code">
                {r.credibility_score ?? "—"}
              </span>
              <span className="rel-kind">{r.kind_label}</span>
              <span className="rel-title">{r.title}</span>
              <span className="rel-time code">
                {formatTime(r.published_at)}
              </span>
            </li>
          ))}
          {n > rows.length && (
            <li className="muted-text rel-more">
              另有 {n - rows.length} 条未列出
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

/**
 * 「无明确倾向」收容组的**列表行**。
 *
 * 用户口径："无明确倾向的，可以聚合成一条，点开可查看。"
 * 折叠时只占一行（这是重点：不折叠的话它是几十上百行），
 * 选中后**右侧详情栏**逐条列出标题、时间、平台、可信度（见 `IntelItemDetail`
 * 里 `is_group` 那一支）。
 *
 * ⚠️ 它原来是一张 5 列表格里的 `<tr>`。改成列表行不是为了好看：
 * 表格里这一行要往 5 个格子里塞东西，列宽被别的行牵着走，
 * 于是"只占一行"这个唯一的诉求反而不成立（列一挤就换行）。
 */
function IntelGroupRow({ item, selected, onSelect, timeText }: {
  item: IntelItem; selected: boolean; onSelect: () => void; timeText: string;
}) {
  return (
    <li className={`intel-row intel-row-group${selected ? " on" : ""}`}>
      <button className="intel-row-btn" onClick={onSelect}
        aria-expanded={selected} title={item.title}>
        <span className="intel-row-text">
          {/* 收容组容器**今天不带倾向**（成组的前提就是"给不出方向"），
              所以这个标记现在恒为空渲染。照样写全：分流规则将来一变，
              没有它就会静默少一个标记（同 `_group_row` 里那段的理由）。 */}
          <DirectionTag tone={item.tone} />
          {/* 组容器是服务端合成的条目：`title` 就是那一行标题（"其他公开信息
              （120 条）"这种，**不是被截断的正文**），`summary` 是"这一组为什么
              被收在这里"。两者都要，而且它们不重复 —— 所以组行与普通条目
              不同，**保留标题**（普通条目的标题是正文前 60 字的残句，
              已经被摘要取代，见 `IntelListRow`）。 */}
          <span className="intel-row-group-title">{item.title}</span>
          {item.summary && (
            <span className="intel-row-sum">{item.summary}</span>
          )}
        </span>
        {/* 组容器的 meta 与普通行**同一个组件**，只把"AI 小标"换成条数 ——
            组容器是服务端合成的条目，没有可信度也没有指纹，它唯一的信息是
            "这里装了几条"。用同一个组件是为了两边的类型徽标/时间不会漂。 */}
        <RowMeta item={item} timeText={timeText} note={`${item.group_count} 条`} />
      </button>
    </li>
  );
}

/**
 * **手机卡片**（≤720px）。
 *
 * ## 它现在是"收起的列表行 + 点开是详情"
 *
 * 卡片收起时只有三样东西：方向标记 + 标题 + 一行 meta + 一行摘要。
 * 点开后挂的是 `IntelItemDetail` —— **与桌面右栏同一个组件**。
 *
 * 为什么不在卡片里再写一份详情：这一页的详情有五个字段可能缺席，
 * 每加一个字段就要改多处，而漏一处不会有任何报错（只会表现为"手机上这块没有"）。
 * 把"详情长什么样"收敛成一个组件之后，手机与桌面看到的东西
 * **结构上不可能不一致**。
 *
 * ## ⚠️ 展开后**卡片自己不再可点**（第一条改版实测出来的）
 *
 * 第一版是"整卡始终可点开/关"，结果有两个问题：
 *   ① 卡片自己那份标题/时间还在，详情里又渲染一遍 —— 同一个标题连着出现两遍；
 *   ② 用户在详情里读文字时手指落在卡片上就把卡片折起来了
 *      （只有按钮做了 `stopPropagation`，正文没有）—— 表现是"看着看着内容没了"。
 * 所以：**收起**时整卡是入口（`onClick` 开），**展开**后整卡不再响应点击，
 * 由详情右上角的 `×` 关闭。
 */
function IntelCard({ item, open, active, onToggle, today, detailHash,
  onToggleText, renderDetail }: {
  item: IntelItem; open: boolean;
  /** 卡片分支当前是否是**生效的那一个**版式（≤720px）。见 `useIntelLayout`。 */
  active: boolean;
  onToggle: () => void; today: string;
  detailHash: string; onToggleText: (hash: string) => void;
  renderDetail: (hash: string) => ReactNode;
}) {
  const tl = timeLabel(item.published_at, today);
  // 版式没生效时**当它是收起的**：详情由生效的那一支（右栏 / 就地展开）渲染。
  // 不这么做的话，卡片会渲染成一个"展开态但内容为空"的壳 —— 看不见，但没意义。
  const expanded = open && active;
  return (
    <article
      className={`intel-card${expanded ? " open" : ""}`
        + (item.is_group ? " intel-group-card" : "")}
      onClick={expanded ? undefined : onToggle}
      role={expanded ? undefined : "button"}
      tabIndex={expanded ? undefined : 0}
      aria-expanded={expanded}
      onKeyDown={expanded ? undefined : (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle();
        }
      }}
    >
      {/* 收起态才渲染卡片自己那份标题/meta/摘要；展开后由详情面板统一渲染
          （详情里的标题不限行数，卡片那份限 2 行 —— 留详情那份更完整）。 */}
      {!expanded && (
        <>
          <h3 className="intel-card-title">
            <DirectionTag tone={item.tone} />
            {highlight(item.title, item.highlights)}
          </h3>
          {/* meta 与桌面列表行**同一个组件**（`RowMeta`）：两边字段必须一致，
              否则"同一条情报在手机上有可信度、到桌面反而没有"。 */}
          <RowMeta item={item} timeText={`${tl.sub} ${tl.text}`}
            note={item.is_group ? `${item.group_count} 条` : undefined} />
          {/* 摘要同样铺开（用户口径 2026-10-01 第二轮："把信息目录的空间利用完"）。
              手机上宽度只有 390px，给**两行**（桌面上列表行给三行）——
              行高与可读性在窄屏是另一套取舍，但"不用点开就能读懂"这条是一样的。 */}
          <SummaryLine src={item} terms={item.highlights}
            className="intel-card-sum" />
        </>
      )}
      {expanded && (
        <IntelItemDetail item={item} today={today} detailHash={detailHash}
          onToggleText={onToggleText} onClose={onToggle}
          renderDetail={renderDetail} />
      )}
    </article>
  );
}

/**
 * 展开后的逐条列表。
 *
 * ⚠️ **摘要要显示**：用户点开就是为了读内容。只给标题等于让他再点一次，
 * 而这一组里绝大多数条目点开也没有更多东西（宏观数据快讯本来就只有一句话）。
 *
 * ⚠️ 这里是**第 3 条渲染路径**（另外两条是桌面行与手机卡片）：方向标记、
 * 机构/分析师 chip、点击看全文**都要有** —— 组内条目是用户真正读到内容的地方，
 * 而它们走的是另一份投影（`_group_row()`），漏一处不会有任何报错。
 */
function GroupList({ rows, openHash, onToggle, renderDetail }: {
  rows: GroupRow[]; openHash: string; onToggle: (hash: string) => void;
  renderDetail: (hash: string) => ReactNode;
}) {
  if (rows.length === 0) {
    return <div className="muted-text intel-group-empty">没有可显示的条目。</div>;
  }
  return (
    <ul className="intel-group-list">
      {rows.map((r, i) => {
        const hash = r.content_hash || "";
        return (
          <li key={hash || `${i}-${r.title}`}>
            <div className="intel-group-li-head">
              <span className="intel-daytag">
                {(r.published_at || "").slice(5, 16).replace("T", " ")}
              </span>
              <span className="intel-kind small">{r.kind_label}</span>
              {r.platform && <span className="intel-group-plat">{r.platform}</span>}
              {r.credibility_score !== null && r.credibility_score !== undefined && (
                <span className="intel-group-cred" title="可信度（规则层打分）">
                  {r.credibility_score}
                </span>
              )}
            </div>
            <TextToggle hash={hash} open={!!hash && openHash === hash}
              onToggle={() => onToggle(hash)}
              className="intel-group-li-title">
              <DirectionTag tone={r.tone} />
              {highlight(r.title, r.highlights)}
            </TextToggle>
            {r.summary && r.summary !== r.title && (
              <SummaryLine src={r} terms={r.highlights}
                className="intel-group-li-sum" />
            )}
            {/* 组内条目是**另一个投影**（服务端 `_group_row()`），机构名与
                分析师名要单独透传；展开后的这一条才是用户真正读到内容的地方，
                所以这一处**必须**有（同 `highlights` 的理由）。 */}
            <div className="intel-tags-row">
              <InstChips names={r.institutions} />
              <AnalystChips names={r.analysts} />
            </div>
            {renderDetail(hash)}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * 列表行与手机卡片**共用**的那一行 meta。
 *
 * ## 为什么必须是同一个组件
 *
 * 桌面的列表行与手机的卡片是**两套 DOM**（CSS 切换，见模块 docstring ①），
 * 而 meta 的内容是同一份判断："类型 / 时间 / 可信度 / 关联标的 / 有没有 AI 摘要"。
 * 各写一份的表现是"同一条情报在两处显示的字段不一样" —— 用户在手机上看到
 * 有可信度、到桌面反而没有，而**两边都不会报错**。
 *
 * ## 每一件都问过"扫列表时要不要看它"
 *
 *   类型        要看 —— 「仅研报」这类筛选靠它，且一眼能分辨快讯/笔记
 *   时间        要看 —— 判断新旧
 *   可信度      要一个**数字**（不是环）—— 它决定这条值不值得信
 *   关联标的    要看 —— 但只在采集层给了行业/代码时才有
 *   `AI` 小标   要看 —— 摘要搬进详情之后，列表上必须有个东西告诉用户
 *              "这条点开还有一句模型压的摘要"，否则他不会去点
 *
 * 摘要本身、机构/分析师、命中词、多空分析都在**详情**里，不在这里
 * （它们是行高不一致的主因，而列表要的是"能扫"）。
 */
function RowMeta({ item, timeText, note }: {
  item: IntelItem; timeText: string; note?: string;
}) {
  const tgt = targetsOf(item);
  const score = item.credibility?.score;
  // 收容组的类型徽标**不是**按 `kind` 上色：它是服务端合成的条目，
  // 用虚线灰底（`.intel-kind.group`）告诉用户"这是一组，不是一条"。
  const kindCls = item.is_group ? "group" : kindTone(item.kind);
  return (
    <span className="intel-row-meta">
      <span className={`intel-kind ${kindCls}`}>
        {item.kind_label}
      </span>
      {note
        ? <span className="intel-row-n">{note}</span>
        : item.summary_source === "model" && (
          <span className="intel-ai intel-ai-mini"
            title="这条有本地模型压出的一句话摘要（点开看）">AI</span>
        )}
      <span>{timeText}</span>
      {typeof score === "number" && (
        <span className="intel-row-cred"
          title="可信度：来源轴 + 内容轴的可复算分数，只描述『有多可核实』，不是方向">
          可信 {score}
        </span>
      )}
      {tgt && <span className="intel-row-target">{tgt}</span>}
    </span>
  );
}

/**
 * **列表行**（桌面左栏）—— 一行只有三样东西：方向标记 + 标题 + 一行 meta。
 *
 * ## 为什么从 5 列表格改成一行（用户口径 2026-10-01）
 *
 * > "我在想情报中心的热点&研报小作文，为什么不能做成事件告警里那样的效果呢，
 * >  感觉那种更整齐，还有多空分析"
 *
 * 原来是一张 5 列表格，每行里同时塞了类型徽标、方向标记、AI 摘要、机构 chips、
 * 分析师 chips、相关徽标、可信度环、关联标的，点开全文还**在行内**展开。
 * 列的宽度被内容牵着走（标题长短、chips 几条都在变），行高就没节奏了 ——
 * 这就是"不整齐"的来源，不是配色问题。
 *
 * 事件告警之所以整齐：左列表一行只留"方向·级别 + 标题 + 一行 meta"，
 * 其余全部搬到右边的详情面板。这里照搬这个分工。
 *
 * ⚠️ `title` 属性给**完整摘要**（鼠标悬停可看全）：行内是 CSS 截断的。
 *
 * ## ★ 行内**直接显示摘要/摘录**，不再"标题 + 摘要"拼（用户口径 2026-10-01，第三轮）
 *
 * > "目录里有这么大空白空间……请把信息目录的空间利用完"
 * > "直接显示摘录就行了，标题被裁剪严重"
 *
 * 第二轮我把它做成了"标题（加粗）+ 摘要（次要色）"拼在一起。实测下来**标题
 * 这一截本身就不该出现**：星球帖的 `title` 是服务端从正文里截的前 60 字，
 * 再经 `content_filter` 修剪，落到界面上常常是一句带省略号的残句
 * （`瑞银——美光科技（MU.US）…`），而紧接着的摘要开头又是同一句话的前半段
 * —— 用户看到的是"半句 + 整句"，还是重复的。
 *
 * 所以行内**只留一段文字：服务端算好的展示摘要**（`summary_text`，单行、
 * ≤200 字、句边界截断）。它才是"这条讲了什么"。
 *
 * ## 那"这叫摘录不是摘要"要不要标出来
 *
 * 要 —— 用户此前明确抱怨过"看到的是被截断的内容"，所以 `truncated` 时行首
 * 带一个 `摘录` 小徽标（纯文字前缀，不占额外一行）。**不标**等于把
 * "这段是截的"当成"这段就是全部"，那是同一个缺陷换个地方藏。
 */
function IntelListRow({ item, selected, onSelect, today }: {
  item: IntelItem; selected: boolean; onSelect: () => void; today: string;
}) {
  const tl = timeLabel(item.published_at, today);
  const { text, excerpt } = displaySummary(item);
  // 没有摘要时退回标题（老数据 / 后端没给）。**不编**。
  const main = text || item.title;
  return (
    <li className={`intel-row${selected ? " on" : ""}`}>
      <button className="intel-row-btn" onClick={onSelect}
        aria-expanded={selected} title={main}>
        <span className="intel-row-text">
          <DirectionTag tone={item.tone} />
          {excerpt && (
            <span className="intel-excerpt-tag"
              title="这段是原文的**摘录**（在句子边界截断），不是本地模型压的摘要">
              摘录
            </span>
          )}
          {/* 命中词照标（用户规则：涉及股票/板块/美股/AI 的词要突出显示）——
              那条规则针对的是**他看得见的那段文字**，与它在列表还是详情无关。 */}
          {highlight(main, item.highlights)}
        </span>
        <RowMeta item={item} timeText={`${tl.sub} ${tl.text}`} />
      </button>
    </li>
  );
}

/**
 * 方向桶的一块（利好 / 利空）—— 板块 chips + 个股（名称 + 代码）。
 *
 * 两侧都空时**不渲染**（由调用方给一句人话说明），不留一个空标题。
 */
function SideBlock({ label, side, cls }: {
  label: string; side: IntelSide; cls: "bull" | "bear";
}) {
  if (!sideHasContent(side)) return null;
  return (
    <div className={`intel-side intel-side-${cls}`}>
      <div className="intel-side-h">{label}</div>
      {side.industries.length > 0 && (
        <div className="intel-side-row">
          <span className="intel-side-k">板块</span>
          <span className="intel-side-tags">
            {side.industries.map((n) => (
              <span className="intel-side-tag" key={n}>{n}</span>
            ))}
          </span>
        </div>
      )}
      {side.stocks.length > 0 && (
        <div className="intel-side-row">
          <span className="intel-side-k">个股</span>
          <span className="intel-side-tags">
            {side.stocks.map((s, i) => (
              <span className="intel-side-stock" key={`${s.code}-${s.name}-${i}`}>
                <b>{s.name || "（未命名）"}</b>
                {/* 代码留空是正常状态（词表里查不到就留空，见
                    `alert_bridge._bucket_stocks` 的"代码解析不做兜底"）——
                    留空比安一个可能错的代码诚实。 */}
                {s.code && <span className="code">{s.code}</span>}
              </span>
            ))}
          </span>
        </div>
      )}
    </div>
  );
}

/**
 * **多空分析** —— 详情面板里回答"这条原文说了什么多、什么空"的那一块。
 *
 * ## ⚠️ 它与「事件告警」的多空分析**不是同一种东西**（用户 2026-10-01 问过）
 *
 * 事件告警那块（`AlertDetail.tsx`）的 `利空分 / 利多分 / 影响路径 / 受影响个股
 * （影响 + 原因）` 是**模型判断层**的产物：`src/domain/alerts/prompts.py` 让模型
 * 给出"风险分 0-100""影响路径：政策补贴→需求放量→龙头业绩"这类**判断**，
 * 在 `alerts/analyzer.py` 里落到 `EventAssessment`。
 *
 * 情报流**故意没有这一层**：`tone.py` 的抽取提示词明写"tone：**这段第三方原文
 * 自己**的语气（偏多/偏空/中性），不是你的观点、不预测涨跌"，而且实体必须
 * 逐字出现在原文里（`validate_entities`）。所以这里**不能**照抄"影响路径" ——
 * 抄了就得由我们编一条因果链出来，而那正是本项目最忌讳的
 * "看起来完全合理的错"（用户拿去原文核不出来）。
 *
 * ## 那"影响路径"这一项到底怎么填（用户直接问过）
 *
 * 答案：**这条链路上不存在"影响路径"这个东西**，别给它编内容。
 * 本项目里已经有先例 —— 情报 → 告警的桥把 `impact_path` 复用来装**触发依据**：
 * `alert_bridge.build_assessment()` 的 `impact_path=reason.describe()`
 * （"命中了哪位分析师 / 模型判了什么方向 / 有没有点名个股"）。
 * 情报中心这一栏与它**同口径**，只是不再借用那个字段名，
 * 免得读的人以为它是模型算的因果链。
 *
 * ## 于是这一栏只装已有的、可核对的东西
 *
 *   判定        方向 + **判定来源**（词表 / 词表+模型 / 未做）+ 置信度
 *   触发依据    `tone.explain`（后端写的判定说明）
 *   命中词      `tone.phrases`（逐字来自原文，用户拿去原文一定能核到）
 *   利好/利空    方向桶里的板块与个股（原文点名的）
 *   关键事件    `tone.events`
 *
 * ⚠️ 空是**正常状态**：`_rule_entities()` 只在原文有明确方向时才把名字放进桶里
 * （"没有方向依据时两侧都不放"），而多数快讯的方向本来就是未定/中性。
 * 所以两侧都空时给一句人话，**不编**一个方向出来。
 *
 * ⚠️ 这个组件**导出**了（本文件其余子组件都是模块私有的）：它是这一页最容易
 * 静默退化的那一块 —— 里面每一个字段（`explain` / `phrases` / `events` /
 * `bullish` / `bearish` / `confidence`）都可能缺席，而缺席的表现是"这块空着"，
 * 不会有任何报错。导出它是为了能拿真实 fixture 单独渲染它跑一遍回归。
 */
export function IntelDirectionAnalysis({ item }: { item: IntelItem }) {  const tone = item.tone ?? null;
  const bull = sideOf(tone, "bullish");
  const bear = sideOf(tone, "bearish");
  const phrases = (tone?.phrases ?? []).filter(Boolean);
  const events = (tone?.events ?? []).filter(Boolean);
  const hasSides = sideHasContent(bull) || sideHasContent(bear);
  const conf = typeof tone?.confidence === "number" ? tone.confidence : null;
  return (
    <section className="intel-analysis">
      <h4 className="intel-analysis-h">多空分析</h4>

      <div className="intel-analysis-line">
        <span className="intel-analysis-k">判定</span>
        {/* 有方向就出【多】/【空】；**没有方向时不能留空** ——
            `DirectionTag` 在无方向时什么都不渲染（那是**列表行**的纪律：
            标题前留一格空白会让人以为标记丢了），但详情里这一格是
            **带标签的字段**，空着会被读成"结论没显示出来"。
            所以补一句如实的结论，且三态分开说（见 `noDirectionLabel`）。 */}
        {directionMarker(tone)
          ? <DirectionTag tone={tone} />
          : <span className="intel-dir-none">{noDirectionLabel(tone)}</span>}
        <span className="muted-text">{directionSourceLabel(tone)}</span>
        {conf !== null && (
          <span className="intel-row-cred"
            title="本地模型对这条方向判定的置信度（不是涨跌概率）">
            置信 {Math.round(conf * 100)}%
          </span>
        )}
      </div>

      <div className="intel-analysis-line">
        <span className="intel-analysis-k">触发依据</span>
        <span className="intel-analysis-explain">
          {tone?.explain || "这条还没有判定依据（尚未抽取，或后端没给这一项）"}
        </span>
      </div>

      {phrases.length > 0 && (
        <div className="intel-analysis-line">
          <span className="intel-analysis-k">命中词</span>
          <span className="intel-side-tags">
            {phrases.map((p) => (
              <span className="intel-side-tag" key={p}
                title="逐字出现在原文里的词 —— 方向就是靠它判出来的，可自行打折">
                {p}
              </span>
            ))}
          </span>
        </div>
      )}

      <SideBlock label="利好" side={bull} cls="bull" />
      <SideBlock label="利空" side={bear} cls="bear" />

      {!hasSides && (
        <p className="muted-text intel-analysis-empty">
          这条原文没有点名可归因的板块或个股 —— 宏观数据、海外行情这类快讯
          本来就不提 A 股，<b>不是功能故障</b>。方向只代表原文自己的语气。
        </p>
      )}

      {events.length > 0 && (
        <div className="intel-analysis-line">
          <span className="intel-analysis-k">关键事件</span>
          <ul className="intel-analysis-events">
            {events.map((e, i) => <li key={`${i}-${e}`}>{e}</li>)}
          </ul>
        </div>
      )}
    </section>
  );
}

/**
 * **情报详情面板** —— 主从结构里"右边那一栏"，也是手机卡片点开后的内容。
 *
 * ## 一份实现、三个位置
 *
 *   桌面 ≥1180px   右栏（给了 `onClose`，带展开/收起按钮）
 *   桌面 <1180px   就地展开在选中行**下面**（不给 `onClose` —— 行本身管开关）
 *   手机 ≤720px    卡片展开后挂在卡片里（给了 `onClose`）
 *
 * 不为窄屏另写一份的理由见 `IntelCard` 的说明（漏一处的表现是静默的）。
 *
 * ## 收容组（`is_group`）走单独一支
 *
 * 组容器是服务端合成的条目：没有可信度、没有方向、没有指纹，
 * 它唯一的内容是 `group_items`。按普通条目渲染会得到一堆空栏
 * （看起来像数据坏了），所以这里直接换成 `GroupList`。
 *
 * ⚠️ 组内子条目走的是**另一份投影**（服务端 `_group_row()` 只投
 * `{tone, has_tone, source}` 三键，见 `GroupRow.tone` 的说明），
 * 所以它们的"多空分析"**拿不到** `explain`/`bullish`/`bearish`/`events`
 * —— 那不是缺陷，是刻意的响应体瘦身；组内条目本来就都是"未判定倾向"的。
 *
 * 导出理由同 `IntelDirectionAnalysis`：详情面板是三个位置共用的一份实现，
 * 出问题时表现为"某一处少了一块东西"，需要能单独渲染它验一把。
 */
export function IntelItemDetail({ item, today, detailHash, onToggleText,
  onClose, renderDetail }: {
  item: IntelItem; today: string;
  detailHash: string; onToggleText: (hash: string) => void;
  onClose?: () => void;
  renderDetail: (hash: string) => ReactNode;
}) {
  const hash = item.content_hash || "";
  const tl = timeLabel(item.published_at, today);
  const terms = (item.highlights ?? []).filter(Boolean);
  const tgt = targetsOf(item);
  const open = !!hash && detailHash === hash;
  const isGroup = !!item.is_group;
  return (
    <section className="intel-detail" aria-label="情报详情">
      <header className="intel-detail-head">
        <span className={`intel-kind ${isGroup ? "group" : kindTone(item.kind)}`}>
          {item.kind_label}
        </span>
        <span className="intel-detail-time code">{tl.sub} {tl.text}</span>
        {/* 关闭按钮只在**调用方给了 onClose** 时渲染：
            宽屏右栏、手机卡片展开态各有一个；而"就地展开在列表行下面"
            那一支不给 —— 那里点行本身就是开关，多一个 × 只会让人犹豫该点哪个。 */}
        {onClose && (
          <button className="intel-detail-close" title="收起详情"
            onClick={(e) => { e.stopPropagation(); onClose(); }}>×</button>
        )}
      </header>

      <h3 className="intel-detail-title">{highlight(item.title, terms)}</h3>

      {/* 摘要：模型压的一句话优先（它才是真摘要），没有时退回原文摘录，
          且摘录会带「摘录」徽标 —— 把摘录当摘要显示等于把"你看到的是被截断的
          内容"这个缺陷藏起来（见 `SummaryLine`）。 */}
      {item.summary_source === "model" && item.summary ? (
        <p className="intel-detail-sum ai">
          <span className="intel-ai"
            title="本地模型压缩的一句话摘要（保留原文数字，未引入原文没有的内容）">
            AI摘要
          </span>
          {highlight(displaySummary(item).text || item.summary, terms)}
        </p>
      ) : (
        <SummaryLine src={item} terms={terms} className="intel-detail-sum" />
      )}

      {isGroup ? (
        <GroupList rows={item.group_items ?? []} openHash={detailHash}
          onToggle={onToggleText} renderDetail={renderDetail} />
      ) : (
        <>
          <IntelDirectionAnalysis item={item} />

          {/* 机构名 / 分析师名 / 关联来源。机构与分析师回答的是"谁在说"，
              是**背景**；所以留在详情里、用与绿色命中词不同的专用配色
              （见 `NameChips` 的四条纪律）。 */}
          <div className="intel-tags-row">
            <InstChips names={item.institutions} />
            <AnalystChips names={item.analysts} />
            <RelatedBadge item={item} />
          </div>

          <dl className="intel-detail-facts">
            <div>
              <dt>可信度</dt>
              <dd><CredRing cred={item.credibility} /></dd>
            </div>
            <div>
              <dt>关联标的</dt>
              <dd>{tgt || <span className="muted-text">未给出</span>}</dd>
            </div>
            <div>
              <dt>命中词</dt>
              <dd>{terms.length
                ? terms.join("、")
                : <span className="muted-text">—</span>}</dd>
            </div>
          </dl>

          {hash && (
            <button className={`intel-detail-open${open ? " on" : ""}`}
              onClick={(e) => { e.stopPropagation(); onToggleText(hash); }}>
              {open ? "收起原文" : "查看原文"}
            </button>
          )}
          {renderDetail(hash)}
        </>
      )}
    </section>
  );
}

/**
 * 版式判定：右栏（≥1180px）与手机卡片（≤720px）**各自**是否生效。
 *
 * ## 为什么需要两个布尔值（一处改版实测出来的）
 *
 * 详情面板"一份实现三个位置"：桌面右栏 / 就地展开 / 手机卡片。但它**同时
 * 只能出现在一个位置**——第一版只判了 `wide`，于是选中一条时：
 *
 *     ≥1180px   右栏渲染详情；`.intel-cards`（`display:none`）里那份**也**渲染了
 *     721~1179  列表里就地展开一份；隐藏的卡片里**又**一份
 *
 * DOM 里于是有两份同样的内容。它**看得见**吗？看不见（卡片被 CSS 藏了）——
 * 但代价是实打实的：一条 3800 字的全文、展开的可信度构成、组内 120 条子条目
 * 全都渲染两遍（实测收容组展开后 `.intel-group-list > li` 数是 240 = 120×2）。
 * 更糟的是这类"看不见的第二份"会在以后被人当成幽灵 bug 追。
 *
 * ## 两个断点为什么不同（不是笔误）
 *
 * 1180 是**详情放哪**（右栏 vs 就地展开），720 是**列表还是卡片**
 * （与 `intel.css` 里 `.intel-split{display:none}` / `.intel-cards{display:block}`
 * 那条媒体查询必须一致）。两者服务不同的取舍，所以不能合成一个。
 *
 * ⚠️ 与 CSS 断点重复是**有意的**：CSS 负责"画不画"，这里负责"渲不渲染"。
 * 只留 CSS 的话，隐藏分支里的子树照样会被 React 构建出来。
 */
function useIntelLayout(): { wide: boolean; cards: boolean } {
  const read = () => ({
    wide: window.innerWidth >= 1180,
    cards: window.innerWidth <= 720,
  });
  const [layout, setLayout] = useState(read);
  useEffect(() => {
    const onResize = () => setLayout(read());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return layout;
}

export default function IntelFeedTab({
  feed, sort, onSort, filter, onFilter, direction = "all", onDirection,
  busy = false,
}: {
  feed: IntelFeed;
  sort: SortKey;
  onSort: (s: SortKey) => void;
  filter: string;
  onFilter: (f: string) => void;
  /** 原文倾向档：`all` / `bull`（【多】）/ `bear`（【空】）。 */
  direction?: string;
  onDirection?: (d: string) => void;
  busy?: boolean;
}) {
  const [openHash, setOpenHash] = useState<string>("");
  /**
   * "点击看全文"的**当前展开项**与**已取回的全文**（按 `content_hash` 缓存）。
   *
   * ## 为什么与 `openHash` 分开
   *
   * `openHash` 是卡片的**折叠态**（点卡片展开摘要），而这里是"这条的**全文**
   * 是否摊开"。两者共用会互相打架：手机卡片整卡可点，一个状态既表示
   * "展开摘要"又表示"显示全文"，点一次就同时触发两个动作。
   *
   * ## 为什么懒加载 + 缓存
   *
   * 全文一条上千字，一页几十条全取一遍既慢又没必要（用户多半只看一两条）。
   * 取回来的按 hash 存住：来回点不再发请求，也**不会**因为重渲染丢失。
   */
  const [detailHash, setDetailHash] = useState<string>("");
  const [details, setDetails] = useState<Record<string, DetailState>>({});
  //: 重试用的"再叫醒一次"计数器。**不能**靠"清掉缓存条目"来触发重取 ——
  //: 那要求 effect 依赖 `details`，而那正是下面那个死循环的成因。
  const [detailTick, setDetailTick] = useState(0);
  //: 已经发起过请求的 hash。用 ref 而不是读 `details`：effect 里读自己写的
  //: state 会把"依赖"和"副作用"缠在一起（见下面那段长注释）。
  const requested = useRef<Set<string>>(new Set());
  const today = todayKey();
  const items = feed.items ?? [];
  const { wide, cards } = useIntelLayout();

  /**
   * 右栏要显示的条目（`openHash` 选中的那条）。
   *
   * 用 `find` 而不是把整个条目存进 state：列表每 2 秒可能因为"构建中"的
   * 轮询重渲染，而**存下来的对象会是旧引用** —— 表现是"点开的那条
   * 摘要不更新了"，或者更糟：切换筛选档后右栏还留着上一档的条目
   * （它已经不在 `items` 里了，但 state 里还指着它）。
   * 从当前 `items` 现算，这两类不一致在结构上不可能出现。
   *
   * 找不到时返回 `null` → 右栏显示"点左侧任意一条"。
   * ⚠️ 这**不是**异常状态：切换筛选档、刷新拿到新的一页，都会让原来的
   * 选中项消失，那是正常的。
   */
  const selectedItem = useMemo(
    () => items.find((it) => (it.content_hash || it.title) === openHash) ?? null,
    [items, openHash]);

  /** 本页有多少条是**从留存补回来的**（页脚要说清，见那段注释）。 */
  const retainedCount = useMemo(
    () => items.filter((it) => it.retained).length, [items]);

  const toggleDetail = (hash: string) => {
    if (!hash) return;
    setDetailHash((cur) => (cur === hash ? "" : hash));
  };
  const retryDetail = (hash: string) => {
    // 撤回"已请求"标记并清掉失败记录，再**显式**叫醒 effect 一次。
    // ⚠️ 早先靠"`details` 变了 effect 自然重跑"来触发重试，那是下面那个
    // 死循环的另一半成因 —— 重试必须是一个显式动作，不能让 effect 依赖
    // 自己写的 state。
    requested.current.delete(hash);
    setDetails((d) => {
      const next = { ...d };
      delete next[hash];
      return next;
    });
    setDetailTick((t) => t + 1);
  };

  /**
   * 按需取全文（点「查看原文」时）。
   *
   * ## ★ 依赖数组里**绝不能**放 `details`（这里踩过一个很贵的坑）
   *
   * 实测（2026-09-26）：点一次「查看原文」，`/intel/item/{hash}` 被请求了
   * **2248 次**，而界面上永远停在"正在读取全文…"。链条是这样的：
   *
   *     ① effect 跑 → `setDetails(loading)` → 请求发出
   *     ② `details` 变了 → 而它是依赖 → React 先跑 cleanup
   *     ③ cleanup 把 `alive = false`（**这次响应注定被丢弃**）
   *        并把那条 `loading` 记录删掉
   *     ④ 新 effect 看到 `details` 里还有记录 → `return`（不发请求）
   *     ⑤ cleanup 那次 `setDetails` 生效 → `details` 又变了 → 回到 ①
   *
   * 于是每一轮的响应都被 `if (!alive) return` 丢掉，界面卡在加载态；
   * 更要紧的是**服务端被打**（一个用户点一下就是每秒几百个请求）。
   *
   * 修法：依赖只留 `[detailHash, detailTick]`；"这条取过没有"用 ref 记；
   * 重试由 `retryDetail` 显式 bump `detailTick`。
   *
   * ## 第二条纪律：**没有 cleanup**（见下面那段）
   *
   * 结果按 `content_hash` 存，与"当前选中哪一条"无关，所以切条时**不该丢**。
   * 丢掉只会让"切走再回来"重新打一次请求。
   */
  useEffect(() => {
    if (!detailHash) return;
    if (requested.current.has(detailHash)) return;   // 已取过（含失败）→ 等重试
    requested.current.add(detailHash);
    setDetails((d) => ({
      ...d, [detailHash]: { loading: true, text: "", error: "" },
    }));
    // ⚠️ **没有 cleanup**，这是有意的（第二条踩过的坑）：
    //
    // 第一版在 cleanup 里 `alive = false` + 删掉"读取中"，为的是"切条后不要
    // setState"。但这条响应本来就该写进 `details[detailHash]` —— 它**按 hash
    // 存**，与"现在选中的是哪一条"无关。丢掉它反而制造两个新问题：
    //   · 切走再回来要重取（明明已经拿到了）；
    //   · 请求还在飞时切走 → 标记与记录被清掉，回来时重取，来回点就是来回打请求。
    // 组件卸载后 `setDetails` 落空在 React 18 是无害的（不再有 setState 警告），
    // 而"结果按 hash 缓存住"正是这一块要的语义。
    fetchIntelItem(detailHash)
      .then((d) => {
        setDetails((prev) => ({
          ...prev,
          [detailHash]: { loading: false, text: d.text, error: "" },
        }));
      })
      .catch((e: unknown) => {
        setDetails((prev) => ({
          ...prev,
          [detailHash]: { loading: false, text: "", error: detailErrorText(e) },
        }));
      });
  }, [detailHash, detailTick]);

  /** 一条全文面板（子组件只调用它，状态与请求都留在父组件里）。 */
  const renderDetail = (hash: string) => (
    <ItemFullText hash={hash} open={!!hash && detailHash === hash}
      state={details[hash]} onRetry={() => retryDetail(hash)} />
  );

  /**
   * 筛选档的**角标数**用服务端给的分层计数（`credibility_dist`），
   * 而不是 `items` —— `items` 是**已按当前档位过滤后**的一页，
   * 拿它当角标会让"切一次 tab 所有角标都变"，用户会以为数据在动。
   */
  const badges = useMemo(() => {
    const d = feed.credibility_dist ?? {};
    const sum = (...keys: string[]) =>
      keys.reduce((n, k) => n + (d[k] ?? 0), 0);
    return {
      all: Object.values(d).reduce((a, b) => a + b, 0),
      high: sum("high"),
      mid_up: sum("high", "upper", "mid"),
      low: sum("low", "doubt"),
      official: 0,   // 服务端不出「仅官方」的分层计数（它按来源档判，不是分数档）
      broker: feed.counts?.broker_report ?? 0,
    } as Record<string, number>;
  }, [feed.credibility_dist, feed.counts]);

  const filterKeys = useMemo(() => {
    const defs = feed.filters ?? {};
    const order = ["all", "high", "mid_up", "low", "official", "broker"];
    return order.filter((k) => {
      if (!(k in defs)) return false;
      if (k === "all") return true;         // 始终显示
      // 该档**当前一条都没有**时不显示页签 —— 点进去是空列表，纯干扰。
      // 实测：「高可信 ≥80」经常为空，而那不是故障，是**数据事实**：
      // 研报的来源档是 84，内容分若为 78，两轴一混合是 79 分，差 1 分。
      // 也就是说"这一批信息里确实没有 80 分以上的"，而不是"功能坏了"。
      // 藏掉它，用户就不会以为坏了。
      if (k === "official") return (badges.official ?? 0) > 0;
      return (badges[k] ?? 0) > 0;
    });
  }, [feed.filters, badges]);

  return (
    <div className="intel-feed">
      {/* ── 平台热议（原「舆情热度」子页签，已并入情报流）──
          放最上面是有意的：用户打开情报中心，第一个要回答的问题是
          "现在哪些票、哪些事在被讨论"，而不是"一共有多少条信息"。 */}
      <IntelHeatBlock heat={feed.heat} loading={busy} />

      {/* ── 筛选 + 排序 ── */}
      <div className="intel-controls">
        <div className="intel-filterbar" role="tablist" aria-label="可信度筛选">
          {filterKeys.map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={filter === k}
              className={`intel-filter${filter === k ? " on" : ""}`}
              disabled={busy}
              onClick={() => onFilter(k)}
            >
              {feed.filters?.[k] ?? k}
              {badges[k] > 0 && (
                <span className="intel-filter-n">{badges[k]}</span>
              )}
            </button>
          ))}
        </div>
        {/* ── 多/空 过滤（用户口径 2026-09-26）──
            用户原话：

              "intel-controls 表头要加一个 多/空 的过滤选择器（多还是空 是
               模型分析出来的 每条信息第一个字【空】【多】）"

            所以文案直接用【多】/【空】——与列表标题前那个标记**逐字一致**，
            用户一眼就能把"筛的东西"和"看到的标记"对上。

            ⚠️ **不并进上面的可信度 tab**：那是两个正交的轴。并进去的话，
            用户在「高可信」档下切到【多】就会丢掉可信度档位，
            "高可信 + 偏多"这个组合就表达不出来了。

            ⚠️ 角标取 `direction_dist`（**当前可信度档位之内**的计数），
            不是 `tone_dist`（全量）。用全量会让用户点进去发现"说好的 30 条
            只有 4 条"。
            ⚠️ 选中态用**本地 `direction`**，不用服务端回显 —— 回显是
            "这一份数据按哪个档建的"，切档位时请求还在飞，拿它当选中态
            会让按钮"弹回去"。 */}
        <div className="intel-sortbar" role="group" aria-label="原文倾向筛选">
          <span className="muted-text">方向</span>
          {DIRECTIONS.map((d) => (
            <button
              key={d.key}
              className={`intel-sort intel-dirfilter${direction === d.key
                ? " on" : ""}${d.cls ? ` ${d.cls}` : ""}`}
              title={d.hint}
              disabled={busy}
              onClick={() => onDirection?.(d.key)}
            >
              {d.label}
              {(d.countKey && (feed.direction_dist?.[d.countKey] ?? 0) > 0) && (
                <span className="intel-filter-n">
                  {feed.direction_dist?.[d.countKey]}
                </span>
              )}
            </button>
          ))}
        </div>
        <div className="intel-sortbar" role="group" aria-label="取样顺序">
          <span className="muted-text">取样</span>
          {SORTS.map((s) => (
            <button
              key={s.key}
              className={`intel-sort${sort === s.key ? " on" : ""}`}
              title={s.hint}
              disabled={busy}
              onClick={() => onSort(s.key)}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {items.length === 0 && (
        <div className="empty-tip muted-text">
          该筛选档下当前没有条目。
          {filter === "high" && (
            <>
              {" "}这与「高可信」的口径有关：来源轴最高只到 <b>84</b>（持牌机构研报），
              两轴按 <b>0.85 × 低 + 0.15 × 高</b> 混合后，
              内容轴要 ≥ <b>78</b> 才可能越过 80。
              当前这批信息里没有这样的条目 —— <b>不是功能故障，是数据事实</b>。
            </>
          )}
          {filter !== "all" && filter !== "high" && "（换个档位试试）"}
        </div>
      )}

      {/* ── 桌面：**左列表 + 右详情**（主从结构，照「事件告警」）──
          ≥1180px：详情常驻右栏（点列表不跳版式）；
          更窄：详情就地展开在选中行**下面**（见 `useWidePane` 里
          为什么这个位置只能由 JS 决定，CSS 挪不动一个 DOM）。 */}
      {items.length > 0 && (
        <div className={`intel-split${wide ? " wide" : ""}`}>
          <ul className="intel-rows">
            {items.map((it) => {
              const key = it.content_hash || it.title;
              const selected = openHash === key;
              const toggle = () => setOpenHash(selected ? "" : key);
              const tl = timeLabel(it.published_at, today);
              return (
                <Fragment key={key}>
                  {/* 收容组走单独分支：它没有可信度、没有方向、没有指纹，
                      按普通行渲染会得到一排空栏（看起来像数据坏了）。 */}
                  {it.is_group ? (
                    <IntelGroupRow item={it} selected={selected}
                      onSelect={toggle} timeText={`${tl.sub} ${tl.text}`} />
                  ) : (
                    <IntelListRow item={it} selected={selected}
                      onSelect={toggle} today={today} />
                  )}
                  {selected && !wide && (
                    <li className="intel-row-detail">
                      <IntelItemDetail item={it} today={today}
                        detailHash={detailHash} onToggleText={toggleDetail}
                        renderDetail={renderDetail} />
                    </li>
                  )}
                </Fragment>
              );
            })}
          </ul>

          {wide && (
            <div className="intel-pane">
              {selectedItem ? (
                <IntelItemDetail item={selectedItem} today={today}
                  detailHash={detailHash} onToggleText={toggleDetail}
                  onClose={() => setOpenHash("")}
                  renderDetail={renderDetail} />
              ) : (
                <div className="intel-pane-empty muted-text">
                  点左侧任意一条，这里显示它的摘要、<b>多空分析</b>、
                  机构/分析师与可信度构成。
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── 手机：卡片（一份详情组件，点开在卡片里）──
          ⚠️ `active={cards}`：卡片分支**只在 ≤720px 生效**，别的时候整个
          `.intel-cards` 被 CSS 藏着 —— 但藏着不等于没渲染。不传这个布尔值
          的话，选中一条会同时渲染"右栏/就地展开"与"隐藏卡片"两份详情
          （实测收容组展开后组内 `<li>` 是 240 = 120×2）。 */}
      <div className="intel-cards">
        {items.map((it) => {
          const key = it.content_hash || it.title;
          const open = openHash === key;
          return (
            <IntelCard key={key} item={it} open={open} active={cards}
              onToggle={() => setOpenHash(open ? "" : key)}
              today={today} detailHash={detailHash}
              onToggleText={toggleDetail} renderDetail={renderDetail} />
          );
        })}
      </div>

      {/* 数据缺口：只显示"某类暂时没有"，**不说是哪个源坏了** */}
      {feed.gaps?.length > 0 && (
        <div className="intel-gaps">
          {feed.gaps.map((g, i) => (
            <span className="intel-gap" key={`${g.kind}-${i}`}>
              {g.message}
            </span>
          ))}
        </div>
      )}

      <div className="intel-foot muted-text">
        本页 {items.length} 条 / 全量 {fmtNum(badges.all, 0)} 条
        {" · "}抓取于 {formatTime(feed.fetched_at, { withDate: true })}
        {/* 留存的条数要**明说**：知识星球那条链路每轮只取"最新 N 条帖子"，
            早先取到的靠 `item_store`（3 天）补回来。不说的话用户会以为
            "这些是刚抓到的"，而它们其实来自更早的一轮 ——
            与本页其余"数据缺口也要如实报"是同一条纪律。 */}
        {retainedCount > 0 && (
          <>{" · "}其中 {retainedCount} 条来自留存（本轮未取到，3 天内保留）</>
        )}
        {" · "}
        <span title="可信度只描述『有多可核实』，不构成投资建议">
          可信度 = 规则层可复算分数（来源轴 + 内容轴 + 多来源印证），非平台判断
        </span>
        {(feed.cluster_stats?.folded ?? 0) > 0 && (
          <>
            {" · "}相似新闻已折叠 {feed.cluster_stats?.folded} 条
            （{feed.cluster_stats?.clusters} 组）
          </>
        )}
        {feed.degraded && " · 数据不完整（部分来源暂无更新）"}
      </div>
    </div>
  );
}
