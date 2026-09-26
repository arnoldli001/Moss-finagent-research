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
 * 原文倾向（后端 `tone_store` 的读取侧形状，按 `content_hash` join 到条目上）。
 *
 * ⚠️ 这里**只描述第三方原文自己的语气**，不是平台判断，也不构成投资建议。
 * 前端靠 `has_tone` + `tone` 两个值决定要不要在标题/摘要最前面加
 * 【多】/【空】标记（见 `directionMarker`）。
 *
 * `events` / `bullish` / `bearish` 是本地模型抽出的"相关个股/板块 + 关键事件"。
 * 它们与 `highlights` 不是一回事：后者是规则层扫出来、必须逐字在展示文本里的词。
 *
 * ⚠️ 2026-10-01 起**已经在界面上渲染**（情报详情面板的「多空分析」）——
 * 这句注释原来写的是"目前只在 payload 里、界面上没渲染"，那是当时的事实，
 * 现在改掉是为了不让下一个人照着旧注释以为这几个字段没人用。
 */
export interface IntelTone {
  /** `偏多` | `偏空` | `中性` | `未定` */
  tone?: string;
  has_tone?: boolean;
  /** **已判定为中性**（后端据此在情报流里隐藏这一条） */
  neutral?: boolean;
  phrases?: string[];
  codes?: string[];
  confidence?: number | null;
  /**
   * 判定**来源**：`rules` = 纯词表（零模型）；`rules+llm` = 模型参与；
   * `skipped` = 可信度太低不做倾向分析。
   *
   * ⚠️ 用户口径（2026-10-01）："本地模型提取的信息，要求输出文字不能超过200个"。
   * 这句话隐含一个区别：**词表给的**与**模型给的**不是一回事，界面要让用户
   * 分得出来（`DirectionTag` 的 tooltip 里写明），不能把词表猜测当模型判定。
   *
   * 后端在情报流里由**两条路**产生：`tone_store`（抽取任务落的）与
   * `rule_tone_verdict`（请求路径上现算的兜底）。两条都如实标 `rules`。
   */
  source?: string;
  explain?: string;
  events?: string[];
  bullish?: IntelSide;
  bearish?: IntelSide;
}

/**
 * 方向桶（`bullish` / `bearish`）—— **利好侧 / 利空侧**里原文点名的板块与个股。
 *
 * ## 字段的白名单由后端保证
 *
 * `tone._public_side()` 逐个键拷贝（只放行 `industries` / `stocks` / `count`
 * / `boards`），所以这里的形状是**契约**而不是"大概长这样"。原来它被标成
 * `Record<string, unknown>`，于是界面上要用它就必须先做一堆类型断言 ——
 * 断言写错不会有任何报错，只会表现为"这块偶尔空着"。
 *
 * ## 空是**正常状态**，不是缺陷
 *
 * `tone._rule_entities()` 只在**原文有明确方向**（偏多/偏空）时才把名字放进桶里
 * （"没有方向依据时两侧都不放"）。而实测绝大多数快讯的方向是"未定/中性"，
 * 所以界面上经常两侧都空 —— 这时要说"本条原文没有点名可归因的板块/个股"，
 * **不要**编一个方向出来。
 */
export interface IntelSide {
  /** 板块名（与 `boards` 同一批，这里只有名字） */
  industries: string[];
  stocks: IntelSideStock[];
  /** `industries.length + stocks.length`（后端算好的，前端别自己加） */
  count: number;
  /** 与 `industries` 同一批，多了主线挖掘要用的板块代码 */
  boards: IntelSideBoard[];
}

/** 方向桶里的个股。`code` 只从词表或原文明确配对取，模型编的代码一律不采用。 */
export interface IntelSideStock {
  name: string;
  code: string;
  /** 同一条里被提到几次（>=1） */
  count: number;
}

/** 方向桶里的板块（带代码）。 */
export interface IntelSideBoard {
  name: string;
  code: string;
}

/**
 * 服务端算好的**展示摘要**（`service._summary_of`）。
 *
 * 用户口径（2026-10-01）：
 *
 * > "本地模型提取的信息，要求输出文字不能超过200个"
 * > "精简200字以内，用户没时间看全文，要效率"
 * > "前端信息不要换行"
 *
 * 所以它已经**折叠成单行**、**≤200 字**，且带来源标记：
 *
 *   `model`    模型压出来的一句话 —— 这是**摘要**
 *   `excerpt`  原文在句边界截断 —— 这是**摘录**，`truncated=true` 时界面
 *              必须标出来（用户抱怨的正是"看到的是被截断的内容"，
 *              把摘录当摘要显示等于把同一个缺陷藏起来）
 *
 * 缺省（老数据 / 后端没给）时前端退回渲染 `summary`，
 * 并用 `oneLine()` + `SUMMARY_MAX_CHARS` 兜底 —— 不为一个展示细节
 * 让整页渲染不出来。
 */
export interface IntelSummaryText {
  /** 折叠成单行、已截到 ≤ `SUMMARY_MAX_CHARS` 的展示文本 */
  text: string;
  /** `model` | `excerpt` | `""`（空串 = 没有可显示的摘要） */
  kind: string;
  /** 是不是**丢了内容**的摘录（据此决定要不要标"（摘录）"） */
  truncated: boolean;
}

/** 单条情报（`IntelItem.to_public()` 的白名单输出）。 */
export interface IntelItem {
  kind: string;
  kind_label: string;
  title: string;
  /** 已由后端按类型截断（快讯 160 / 研报 200 / 笔记 260 / 政策 300 字） */
  summary: string;
  /**
   * 服务端算好的**展示摘要**（单行 + ≤200 字）。**优先渲染它**。
   *
   * 与 `summary` 的分工：`summary` 是原始的、可能带换行、上限 260 字的
   * 原文截断（`body_store` 的"摘要即正文"、`_group_row` 的透传都还用它）；
   * `summary_text` 才是**给人看的那一份**。
   * 缺省时退回 `summary` + 前端 `oneLine()` 兜底。
   */
  summary_text?: IntelSummaryText;
  published_at: string;
  /**
   * 来源**假名**（`src-xxxxxxxx`）。
   * 只用于同源判断；**不要渲染到界面上**。
   */
  source_alias: string;
  /**
   * **可公开**的平台名（东方财富/同花顺/财联社/…）。
   *
   * 与 `source_alias`（稳定假名）的分工：假名用于"按源去重/分组"，
   * 平台名用于"告诉用户这条来自哪个公开网站"。研报机构与私域来源
   * 这里给空串 —— 界面不要用假名去顶替平台名。
   */
  platform?: string;
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
   * 摘要来源：`"model"` = 本地模型生成的一句话摘要；缺省 = **原文截断**。
   *
   * 界面要能区分两者 —— 用户有权知道这段文字是压缩过的还是原文切片
   * （切片可能正好把结论截掉，而模型摘要承诺"保留数字、只压缩"）。
   */
  summary_source?: string;
  /**
   * 要在标题/摘要里**标绿加粗**的词（概念板块 / 个股 / 外部关键标的 / AI）。
   *
   * ⚠️ 每个词都**逐字出现在 `title` 或 `summary` 里**（服务端保证），
   * 且已按**长度降序**排好 —— 前端照顺序做一次替换即可，
   * **不要在前端重新判定**哪些词算命中：两套规则必然漂移，
   * 用户看到的绿字就会与"为什么这条没被折叠"对不上。
   *
   * 可能缺省（老数据 / 没有命中），按空数组处理。
   */
  highlights?: string[];
  /**
   * 内容里**逐字出现**的券商 / 机构名（如 `["中泰证券"]`），由服务端扫出。
   *
   * ⚠️ 用户口径（2026-10-01）："券商名不一定要告警，但是一定要前端输出信息。"
   * 所以它是**必须上屏**的展示字段 —— 不告警也要让用户看见"谁在说"。
   *
   * ⚠️ **不要与个股名混同**：这里是机构（中泰证券 / 国金证券），不是标的。
   * 渲染时必须带"机构："这类前缀，且**不能**套用 `highlights` 的绿色大字
   * —— 那是"哪只票在被打"的信号，机构是**背景信息**。
   *
   * 只有裸名字，没有标签；可能缺省（老数据 / 没扫到），按空数组处理。
   */
  institutions?: string[];
  /**
   * 内容里**逐字出现**的分析师姓名（用户点名的六人：孙潇雅、赵宇阳、武超则、
   * 陈果、刘晨明、洪灏），由服务端扫出。
   *
   * ⚠️ 用户口径（2026-10-01）："如果有以下内容必须要输出：股票名 板块名 券商
   * 孙潇雅、赵宇阳、武超则、陈果、刘晨明、洪灏 …… 推送到前端展示。"
   * 这六个人原先**只用于告警触发**，界面上一个字都没有 —— 现在必须上屏。
   *
   * ⚠️ 与 `institutions` 完全平行（同一个扫描实现、同一条纪律），
   * 但**标签不同**：渲染成"分析师：孙潇雅"。机构与分析师**都不是个股**，
   * 绝不能混进 `highlights`（那是"哪些票在被打"的答案）。
   *
   * 可能缺省（老数据 / 没命中），按空数组处理。
   */
  analysts?: string[];
  /**
   * 原文倾向（可能缺省：尚未抽取 / 老数据）。
   *
   * ⚠️ 只有 `has_tone === true` 且 `tone` 是 `偏多`/`偏空` 时才加方向标记；
   * 未定 / 未抽取 / 中性一律**什么都不加**（不是加一个空占位）。
   */
  tone?: IntelTone | null;
  /**
   * 这是"无明确倾向"条目的**收容组**（服务端合成，不是真实条目）。
   *
   * 用户口径（2026-09-25）："无明确倾向的，可以聚合成一条，点开可查看。"
   * 组里包含三种"给不出方向"的状态：未定 / 尚未抽取 / 低可信不做倾向分析。
   * 渲染时要**走单独的分支** —— 它没有 `credibility`、没有 `source_alias`，
   * 按普通条目渲染会得到一排"—"和空分数环。
   */
  is_group?: boolean;
  group_count?: number;
  /** 展开后要显示的逐条内容（只含渲染必需的字段） */
  group_items?: GroupRow[];
  /**
   * **来自留存**（`item_store` 里 3 天内的条目，本轮没取到它）。
   *
   * 为什么这个标记要出接口：知识星球那条链路每轮只取"最新 N 条帖子"，
   * 窗口一滑，早先取到的帖子就会从页面上消失。留存把它们存下来、补回页面 ——
   * 于是"这条不是本轮刚取的"是**正常且需要能说清**的一件事：
   * 页脚据此说明，排查时也靠它区分"这条怎么来的"。
   */
  retained?: boolean;
}

/** 收容组里的一行（`IntelGroup` 的子条目）。 */
export interface GroupRow {
  title: string;
  summary: string;
  kind_label: string;
  published_at: string;
  /** 可公开平台名；空串 = 私域/研报来源，不要显示 */
  platform: string;
  credibility_score: number | null;
  content_hash: string;
  /** 高亮词（同 `IntelItem.highlights`）；缺省按空数组处理 */
  highlights?: string[];
  /**
   * 内容里的机构名（同 `IntelItem.institutions`）；缺省按空数组处理。
   *
   * ⚠️ 这一层是**白名单投影**（服务端 `_group_row()`）：漏一个键的表现是
   * "抽到了、接口永远看不到"，不会有任何报错。而用户口径是"**一定要**
   * 前端输出信息" —— 组内条目展开后也得看得到"机构：中泰证券"。
   */
  institutions?: string[];
  /** 分析师名（同 `IntelItem.analysts`）；缺省按空数组处理 */
  analysts?: string[];
  /**
   * 方向标记（【多】/【空】）要的**最小投影**：`{tone, has_tone, source}`。
   *
   * ⚠️ 服务端在收容组这一层刻意**只给这三个值**（整份 `tone` 字典带着
   * `explain`/`bullish` 等大字段，几十条一起发会让响应体膨胀）。
   * `source` 让 tooltip 能说明"这是词表给的还是模型给的"。
   * 缺省 / 空对象 = 没有倾向字段（老数据）→ 不加标记。
   */
  tone?: Pick<IntelTone, "tone" | "has_tone" | "source"> | null;
  /** 展示摘要（同 `IntelItem.summary_text`）；缺省退回 `summary` */
  summary_text?: IntelSummaryText;
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
  /**
   * 多/空筛选器的角标 `{bull: n, bear: n}`（用户口径 2026-09-26）。
   *
   * ## 为什么不复用 `tone_dist`
   *
   * `tone_dist` 是**全量分布**（服务端在可信度筛选之前统计，热度页要用），
   * 而这个选择器工作在**当前可信度档位之内** —— 用户切到「高可信」时，
   * 「【多】N」必须跟着变小，否则点进去会发现"说好的 30 条只有 4 条"。
   *
   * ⚠️ 它与**当前多空档位无关**（同样统计在筛选之前）：选中【多】之后
   * 【空】的角标不能变成 0，否则用户没法切回去。
   */
  direction_dist?: { bull: number; bear: number };
  /** 被多空筛选挡掉的条数（只在真的筛了时非零，用于"为什么少了"的解释）。 */
  direction_hidden?: number;
  /**
   * 服务端回显的原文倾向档（`all` / `bull` / `bear`）。
   *
   * ⚠️ 以**本地 state 为准**渲染选中态：服务端回显的是"这一份数据按哪个档
   * 建的"，而切档位的瞬间请求还在飞 —— 拿它当选中态会让按钮"弹回去"。
   */
  direction?: string;
  /**
   * **平台热议**（用户口径 2026-09-25："舆情热度监控没做好，不如直接融入到
   * 情报流里"）。原来是一个独立子页签，现在归到情报流顶部。
   */
  heat?: IntelHeat;
  /**
   * 服务端返回的这一份是不是**后台建好的缓存**（冷启动时是 false）。
   *
   * ⚠️ 它**不表示"是不是发起了新的采集"** —— 那件事看 `refreshing`。
   */
  cached?: boolean;
  /**
   * ★ 服务端**正在后台重建**这一份（用户口径 2026-10-01 第七轮）。
   *
   * 后端现在**永远不等待采集**：冷启动 / 缓存过期时它把上一次的结果
   * （或一个空壳）立刻返回，同时起一个后台任务去拉六个源。前端据此：
   *
   *   ① 先画出来（骨架 + "正在采集…"），而不是盯着空屏；
   *   ② 启动"等待就绪"的监听（WS 通知为主 / `/feed/status` 轮询兜底），
   *      建好之后**自动**再拉一次 —— 用户不需要再点刷新。
   *
   * ⚠️ 不要拿它当"数据不可信"用：`false` 只说明后台没在跑，
   * 手里的数据仍然可能是 60 秒内的缓存（看 `age_seconds`）。
   */
  refreshing?: boolean;
  /**
   * 这一份的生成时刻（ISO，服务端本地时区）。给界面显示"数据截至 HH:MM"。
   *
   * ⚠️ 冷启动（空壳）时是**空串**，不是"现在" —— 把空串当时间渲染出
   * `Invalid Date` 或 `1970` 都是假信息。
   */
  built_at?: string;
  /** 这一份多旧（秒）。冷启动（还没有任何一份）时是 `null`，**不是 0**。 */
  age_seconds?: number | null;
  /**
   * 全局递增序号（服务端每次成功重建 +1）。
   *
   * 前端**只比较它**来判断"通知说的那份我拿到没有"：比时间戳字符串可靠
   * （没有格式/时区/精度三个坑），也不需要复刻后端的缓存键。
   */
  seq?: number;
}

/** 平台热议区块。三块内容都是**具体标的与事件**，不是统计量。 */
export interface IntelHeat {
  at: string;
  /** 确定性扫出来的"被平台快讯提到的 A 股"（名称精确匹配，可核对） */
  stocks: HeatStock[];
  /** 模型聚合出的"讨论最集中的事件" */
  topics: HeatTopic[];
  /** 各平台人气/热搜排名（含中文名与关注指数） */
  rank: HeatRankRow[];
  /** 本次扫描的条目数（让"扫了多少"可见） */
  stocks_scanned: number;
  gaps: IntelGap[];
  note: string;
}

/**
 * 一只被平台快讯提到的 A 股。
 *
 * ⚠️ `evidence` 是**给人核对的原文标题** —— 界面必须显示它。
 * 这一页上一版被投诉"作为一个投资者没获得任何有用信息"，
 * 而只给一个名字加一个数字，本质上是同一类问题：无法核对。
 */
export interface HeatStock {
  name: string;
  code: string;
  mention_count: number;
  /** 提到它的平台（**可公开**的财经平台名，可能为空数组） */
  platforms: string[];
  first_seen: string;
  last_seen: string;
  evidence: string[];
  kinds: string[];
}

/** 热议事件（模型聚合，标题已校验"必须在原文出现"）。 */
export interface HeatTopic {
  title: string;
  related: string[];
  detail: string;
  /** 利好 / 利空 / 中性 */
  sentiment: string;
}

/** 平台人气/热搜榜一行。字段随来源不同而部分为空。 */
export interface HeatRankRow {
  name: string;
  code: string;
  rank: number;
  rank_change: number;
  pct_change: number | null;
  heat: number | null;
  /** 关注指数（东方财富千股千评，0-100） */
  focus: number | null;
  /** 综合得分（东方财富千股千评） */
  score: number | null;
  /** 机构参与度（0-1） */
  inst: number | null;
  /** 来源平台（**可公开**） */
  platform: string;
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
    /**
     * 与 `codes` **一一对应**的中文股票名。
     *
     * 预览优先显示它 —— 用户口径（2026-09-25）："当前解禁股右侧未展开前的
     * 预览都是 6 位编码，必须转换成中文股票名。"
     * 可能为空（老数据 / 上游没给名字），此时前端退回显示编码。
     */
    names?: string[];
    /**
     * **限售解禁的个股明细**（只有 `kind === "unlock"` 才有）。
     * 按解禁市值倒序。
     */
    stocks?: UnlockStock[];
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

/**
 * 情报流。
 *
 * ## `refresh` 是**显式刷新路径**（用户口径 2026-10-01 第七轮）
 *
 * ⚠️ 服务端有 60 秒的结果缓存，所以"点刷新"如果不带 `refresh=true`，
 * 拿回来的还是同一份 —— 界面上表现为"点了刷新什么都没变"，
 * 而按钮还会先显示一次"刷新中…"。那种"假刷新"比没有按钮更糟：
 * 用户会以为新内容真的还没有。
 *
 * 所以：**面板上的刷新按钮必须传 `refresh=true`**；后台自动补拉
 * （收到就绪通知 / 切档位）一律不传 —— 那些场景要的正是"吃缓存"。
 *
 * ⚠️ 即使传了 `refresh=true`，**请求也不会等待采集**：服务端立刻返回，
 * 同时起后台任务；建好后通过 WebSocket（`intel_feed`）通知前端自动补拉。
 *
 * @param opts.codes 关注标的（用于额外拉取对应研报）
 */
export function fetchIntelFeed(
  opts: {
    limit?: number; codes?: string[]; sort?: string; filter?: string;
    /** 原文倾向档：`all` / `bull`（【多】）/ `bear`（【空】）。 */
    direction?: string;
    refresh?: boolean;
  } = {},
  signal?: AbortSignal,
): Promise<IntelFeed> {
  const q = new URLSearchParams();
  if (opts.limit) q.set("limit", String(opts.limit));
  if (opts.codes?.length) q.set("codes", opts.codes.join(","));
  if (opts.sort) q.set("sort", opts.sort);
  if (opts.filter && opts.filter !== "all") q.set("filter", opts.filter);
  // 与 `filter` 同一套约定：`all` 不写进 query（让"默认档"的 URL 与
  // **服务端默认值产生的缓存键**保持一致，避免多出一个等价但不同的键）。
  if (opts.direction && opts.direction !== "all") {
    q.set("direction", opts.direction);
  }
  if (opts.refresh) q.set("refresh", "true");
  const qs = q.toString();
  return getJson<IntelFeed>(`/feed${qs ? `?${qs}` : ""}`, signal);
}

/** 后台重建状态（WebSocket 通知的**兜底**，见 `IntelPanel` 的就绪监听）。 */
export interface IntelFeedStatus {
  /** 服务端此刻有没有后台任务在重建（**不是**"数据新不新"） */
  building: boolean;
  /** 全局递增序号：任何一次成功重建都会 +1（前端只比较它） */
  seq: number;
  built_at: string;
  age_seconds: number | null;
  ttl: number;
}

/**
 * 情报流后台重建状态。
 *
 * ## 为什么需要它（不是多余的第三条路）
 *
 * 主路是既有的告警 WebSocket（App 层常驻连接，消息 `intel_feed`）。
 * 但那条通道会断（隧道抖动、代理回收、浏览器挂起），而本条接口
 * **只读服务端进程内计数器**：零 I/O、零网络、零采集，毫秒级。
 *
 * ⚠️ 只在 `refreshing === true` 期间短轮询，拿到数据立刻停 ——
 * 它**不是**第二条数据通路（它连一条情报都不返回，只给序号）。
 */
export function fetchIntelFeedStatus(
  signal?: AbortSignal,
): Promise<IntelFeedStatus> {
  return getJson<IntelFeedStatus>("/feed/status", signal);
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

/**
 * 单条**全文**（点击标题/摘要时按需取，`GET /intel/item/{content_hash}`）。
 *
 * ## 为什么单独一个接口（而不是塞进 feed）
 *
 * `summary` 在契约层被截到 260 字（移动端一条 3400 字占满十屏），
 * 而这条接口回答的是"**我点了这一条**，把原文给我"。
 *
 * ## ⚠️ 响应里**不存在**来源标识（这是后端构造方式决定的）
 *
 * 只有六个键：`content_hash` / `title` / `text` / `published_at` /
 * `kind_label` / `tone`。没有 `source_alias` / `platform` / URL ——
 * 所以前端**不要**去猜、也不要用别处的字段给它补一个"来源"。
 *
 * ⚠️ `text` 是**第三方原文**：必须当纯文本渲染（`{text}`），
 * 绝不能用 `dangerouslySetInnerHTML` —— 那等于在情报流上开一个 XSS 口子。
 */
export interface IntelItemDetail {
  content_hash: string;
  title: string;
  /** 清洗后的全文（第三方原文，按纯文本渲染） */
  text: string;
  published_at: string;
  /** 由服务端按 `KIND_LABELS` **现算**（改名能作用于老数据） */
  kind_label: string;
  /** 原文倾向；未抽取时为 `未定`（**不编**） */
  tone: string;
}

/** 取单条全文。未知或超出留存窗口（3 天）时后端给 404。 */
export function fetchIntelItem(
  contentHash: string,
  signal?: AbortSignal,
): Promise<IntelItemDetail> {
  return getJson<IntelItemDetail>(
    `/item/${encodeURIComponent(contentHash)}`, signal);
}

/**
 * 方向标记表：**原文倾向 → 标题/摘要最前面的那个字**。
 *
 * ## ⚠️ 绿色 = 利多、红色 = 利空，这是**用户明确要求**的配色
 *
 * 用户口径（2026-10-01）：偏多（利多）→【多】显示**绿色**；
 * 偏空（利空）→【空】显示**红色**。
 *
 * 而本页其余地方用的是 **A 股约定「红涨绿跌」**（`.up` 红 / `.down` 绿，
 * 见 `intel.css` 的 `--intel-up` / `--intel-down`）——
 * 也就是说这张表与它是**正好相反**的。这不是笔误，是用户点名要的：
 * 所以这里用**专用 class**（`intel-dir-bull` / `intel-dir-bear`）与
 * **专用配色 token**，绝不复用 `.up`/`.down`/`.intel-hl` 任何一个 ——
 * 复用会让"改一处涨跌配色"顺手把方向标记也改反，而且没有任何报错。
 */
export const DIRECTION_MARKERS: Record<string, { text: string; cls: string }> = {
  "偏多": { text: "【多】", cls: "intel-dir-bull" },
  "偏空": { text: "【空】", cls: "intel-dir-bear" },
};

/**
 * 原文倾向 → 方向标记（**没有方向时返回 `null`**）。
 *
 * 三种"没有方向"的状态都必须返回 `null`（= 什么都不渲染）：
 * 尚未抽取（老数据缺字段）、未定（两层判定冲突）、中性（已判定没有倾向）。
 * ⚠️ **不许**返回空串或空 span 占位：那会在标题前留一格空白，
 * 而用户口径是"没有方向就什么都不加"。
 *
 * ⚠️ 这里**不看 `source`**（用户报障 2026-10-01 的反面教训）：
 * 一条明显偏多的笔记曾经因为"抽取任务还没排到它"而完全没有标记。
 * 现在后端在请求路径上用**词表**兜了一层（`source === "rules"`），
 * 标记照样要显示 —— 只是 tooltip 里要说明它是词表给的。
 * 按来源过滤掉规则层判定，等于把用户报的缺陷原样留着。
 */
export function directionMarker(
  tone?: Pick<IntelTone, "tone" | "has_tone"> | null,
): { text: string; cls: string } | null {
  // `has_tone` 是"规则层与模型层判定一致"的结果；未抽取/未定都是 false。
  if (!tone || !tone.has_tone) return null;
  return DIRECTION_MARKERS[String(tone.tone ?? "")] ?? null;
}

/**
 * 方向标记的 **tooltip 文案**（来源说明 + **命中的词**）。
 *
 * 用户口径（2026-10-01）："本地模型提取的信息……"。
 * 这句话本身就把"词表给的"与"模型给的"分开了 —— 而这个区分在界面上
 * **必须可见**：把词表猜测说成模型判定，就是"把请求当保证"的同类错误
 * （用户没法核对，也就没法判断这个标签该不该信）。
 *
 * ## ★ 为什么必须把 `phrases` 一起写进 tooltip（2026-10-01 第七轮）
 *
 * 词表判定有**已知的出错形态**，而且它没法靠调词表解决：一句
 * "多家云厂商上调资本开支计划，产业链订单能见度提升" 会同时命中
 * `订单` + `上调` + `提升` 三个弱档词，在"有市场语境"那一支上被认成偏多 ——
 * 而它其实是行业观察。要把它与"业绩下滑 + 减持"（同样两个弱档，确实该判偏空）
 * 分开，需要读懂"计划/能见度"与"经营事实"的区别，那是语义不是词表，
 * 而请求路径**不许**调模型。
 *
 * 所以兜底只能落在**可核对**上：把命中的词逐字印出来，用户一眼就能看见
 * "它是靠订单/上调/提升判的"，从而自己打折。这正是本项目对
 * "看起来完全合理的错"的一贯解药（与 `highlights` 必须逐字出现在原文里、
 * `phrases` 必须配得上标签是同一条纪律）。
 *
 * ⚠️ 收容组那一行取不到 `phrases`（`_tone_marker` 只投最小三键），
 * 所以这里是**可选**拼接，缺了就不写 —— 不编。
 */
export function directionTitle(tone?: IntelTone | null): string {
  const words = (tone?.phrases ?? []).filter(Boolean);
  const evidence = words.length
    ? `；判定依据（原文中命中的词）：${words.join("、")}`
    : "";
  if (tone?.source === "rules") {
    return "第三方原文自身的倾向，由**词表规则**判定（本地模型尚未处理这一条），"
      + "不是平台判断，不构成投资建议"
      + evidence;
  }
  return "第三方原文自身的倾向（不是平台判断，不构成投资建议）" + evidence;
}

/**
 * 判定**来源**的界面文案（`rules` / `rules+llm` / `skipped` / 缺省）。
 *
 * ## 为什么这个词必须上屏，不能只放 tooltip
 *
 * 用户口径（2026-10-01）："本地模型提取的信息……" —— 这句话本身就把
 * **词表给的**与**模型给的**分开了。词表判定有已知的出错形态（见
 * `directionTitle` 里"订单/上调/提升"那个例子），用户要能据此自己打折。
 * 把两者显示成同一个样子，等于把"这条是怎么判的"藏起来。
 *
 * ⚠️ `skipped` 是"可信度低于门槛，**没做**倾向分析"，与"做了但未定"
 * 完全是两件事 —— 前者我们根本没看，后者我们看了但两个判据不一致。
 * 措辞必须区分，否则用户会以为"系统看过这条并认为它中性"。
 */
export function directionSourceLabel(tone?: IntelTone | null): string {
  switch (String(tone?.source ?? "")) {
    case "rules": return "词表判定（本地模型尚未处理这一条）";
    case "rules+llm": return "词表 + 本地模型（两层一致才给方向）";
    case "skipped": return "未做倾向分析（可信度低于门槛）";
    default: return "尚未抽取";
  }
}

/**
 * 从一条情报里取某个方向桶（`bullish` / `bearish`），**永远返回可安全渲染的形状**。
 *
 * ## 为什么要有这个函数（而不是各处写 `item.tone?.bullish?.stocks ?? []`）
 *
 * 两种形状都要认，而且这不是"防御性编程"，是**实测的两种真实来路**：
 *
 *   · 情报流的普通条目   桶挂在 `tone` 里面（`service.build_feed` 逐条 join）；
 *   · 收容组的子条目     服务端**只投最小三键**（`{tone, has_tone, source}`），
 *                        整份 `tone` 字典带着 `explain`/`bullish` 一起发会让
 *                        响应体膨胀（见 `GroupRow.tone` 的说明）；
 *   · 老数据（第二轮之前）行里**根本没有这几个键** —— 正常状态，不是损坏。
 *
 * 三种情况都该显示"这块没有"，而不是抛异常或显示 `undefined`。
 * 把兜底收敛到一个函数里，是为了让"到底有几种空法"只有一个答案。
 */
export function sideOf(tone: IntelTone | null | undefined, key: "bullish" | "bearish"): IntelSide {
  const raw = tone?.[key];
  const empty: IntelSide = { industries: [], stocks: [], count: 0, boards: [] };
  if (!raw || typeof raw !== "object") return empty;
  const stocks = Array.isArray(raw.stocks) ? raw.stocks : [];
  return {
    industries: (Array.isArray(raw.industries) ? raw.industries : []).filter(Boolean),
    stocks: stocks.filter((s) => s && (s.name || s.code)),
    count: typeof raw.count === "number" ? raw.count : 0,
    boards: Array.isArray(raw.boards) ? raw.boards : [],
  };
}

/**
 * 方向桶里有东西可显示吗（板块或个股任一非空）。 */
export function sideHasContent(side: IntelSide): boolean {
  return side.industries.length > 0 || side.stocks.length > 0;
}

/**
 * 摘掉摘要里**与标题重复的开头**。
 *
 * ## 为什么必须做（实测）
 *
 * 知识星球的帖子没有标题，服务端取的是**正文前 60 字**当标题
 * （`zsxq_source`：`title = row.title or text[:60]`），而 `summary` 就是正文本身。
 * 于是"标题 + 摘要"并排显示时，同一句话会出现两遍：
 *
 *     摩根大通——建滔积层板（188…      ← 标题
 *     摩根大通——建滔积层板（1888.HK）：景旺、利升…   ← 摘要，开头一模一样
 *
 * 用户在列表行、手机卡片、详情面板三处都会看到这个重复（这是个"看出来的瑕疵"，
 * 不会有任何报错）。
 *
 * ## 判据与两条纪律
 *
 *   · **空白不参与比较**：服务端在标题里把 `\n` 换成了空格、摘要里可能还是
 *     换行，逐字比会漏判 —— 比对用"去掉所有空白"的形态，但**切片切原文**
 *     （否则会连标点一起切歪）。
 *   · 只剥**前缀**，且剥完顺手去掉紧跟的分隔标点（`：`、`，`、`。`…），
 *     否则剩下的正文会以一个孤零零的冒号开头。
 *   · 剥完太短（< `MIN_TAIL`）就返回空串：那说明摘要本来就是标题，
 *     再显示一小截"…维持买入。"只会显得断头。
 */
export function trimRepeatedHead(head: string, body: string): string {
  const MIN_TAIL = 10;
  const h = (head ?? "").replace(/\s+/g, "");
  const b = body ?? "";
  if (!h || !b) return b;
  let seen = 0;
  let i = 0;
  for (; i < b.length && seen < h.length; i += 1) {
    if (/\s/.test(b[i])) continue;          // 空白跳过：不计数、不比对
    if (b[i] !== h[seen]) return b;         // 不完全一致就**原样返回**（不猜）
    seen += 1;
  }
  if (seen < h.length) return b;            // body 比 head 还短 → 没得剥
  const tail = b.slice(i).replace(/^[\s：:，,、。.·—–\-]+/, "");
  return tail.length >= MIN_TAIL ? tail : "";
}

/**
 * **没有方向标记时，"判定"那一格该写什么**（详情面板用）。
 *
 * ## 为什么必须补这一格
 *
 * `DirectionTag` 在没有方向时**什么都不渲染**（用户口径："没有方向就什么都不加"）
 * —— 那条纪律针对的是**列表行**（标题前面留一格空白会让人以为标记丢了）。
 * 但详情面板的"判定"那一行是**一个带标签的字段**，空着就变成：
 *
 *     判定    词表 + 本地模型（两层一致才给方向）
 *
 * 用户读到的是"这里本来有个结论但没显示出来"。所以详情里要**如实写出结论是什么**：
 * 是"未定"（两层判定不一致）、"中性"（原文确实没有倾向），还是"尚未抽取"。
 *
 * ## 三种"没有方向"必须分开说
 *
 *   中性      看过了，原文没有倾向 —— 这是**结论**
 *   未定      看过了，但规则层与模型意见相反 —— 这也是**结论**（我们不做归类）
 *   尚未抽取  还没看 —— 这是**状态**，不是结论
 *
 * 把三者写成同一句话，就是把"我们没看"说成"我们看过并认为它中性"。
 */
export function noDirectionLabel(tone?: IntelTone | null): string {
  const t = String(tone?.tone ?? "");
  if (t === "中性") return "中性（原文自身没有明确倾向）";
  if (t === "未定") return "未定（规则层与模型判定不一致，不做倾向归类）";
  if (t === "偏多" || t === "偏空") return "尚未给出标记";  // 理论上不该出现，如实说
  return "尚未抽取（本地模型还没处理这一条）";
}

/**
 * 一条情报的**展示摘要**（单行 + ≤200 字）。
 *
 * ## 用户口径（2026-10-01）
 *
 * > "本地模型提取的信息，要求输出文字不能超过200个"
 * > "精简200字以内，用户没时间看全文，要效率"
 * > "前端信息不要换行"
 *
 * ## 优先级（**先摘要、后摘录**，顺序不能反）
 *
 *   ① `summary_text`（服务端算好的，带 `kind`/`truncated`）—— 直接用
 *   ② 没有它（老数据 / 后端版本旧）→ 在这里折叠单行并截到 200 字
 *
 * ⚠️ 第 ② 档是**兜底**，不是主路：它在服务端那份实现之外又写了一遍折叠
 * 逻辑，而两份实现必然漂移。所以主路必须走 ①；②只在"后端还没升级"
 * 时生效，且**不标 `（摘录）`**（前端无权判断"我们是不是藏了内容"，
 * 那条判断需要原始长度，只有服务端有）。
 */
export const SUMMARY_MAX_CHARS = 200;

/** 折叠成单行：所有空白（含全角空格 U+3000、不换行空格 U+00A0）→ 一个空格。 */
export function oneLine(text: string): string {
  return (text || "").replace(/[\s\u00a0\u3000]+/g, " ").trim();
}

/** 从 item（或收容组的一行）取展示摘要 + 是不是带省略的摘录。 */
export function displaySummary(src: {
  summary?: string;
  summary_text?: IntelSummaryText;
}): { text: string; excerpt: boolean } {
  const st = src.summary_text;
  if (st && typeof st.text === "string" && st.text) {
    return { text: st.text, excerpt: Boolean(st.truncated) };
  }
  const flat = oneLine(src.summary ?? "");
  if (flat.length <= SUMMARY_MAX_CHARS) return { text: flat, excerpt: false };
  return { text: flat.slice(0, SUMMARY_MAX_CHARS).trimEnd(), excerpt: true };
}

/** 盘前简报（盘前新闻 / 研报热度 / 政策 / 券商作文 四段）。 */
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

/** 情报类型 → 中文标签（前端只认这个，不认内部源名）
 *
 * ⚠️ `research_note` 的展示名是**用户口径**（2026-10-01）：「券商作文」。
 * 内部 `kind` 值**一个字都不改**（它被落库的行、告警闸门、前端筛选共用），
 * 变的只是给人看的那一面 —— 所以这里与后端 `intel_sources.SOURCE_KINDS`
 * 和 `service.KIND_LABELS` **三处必须同时改**（有测试把三处钉在一起）。
 */
export const KIND_FALLBACK_LABELS: Record<string, string> = {
  newswire: "财经快讯",
  broker_report: "券商研报",
  policy: "政策信号",
  research_note: "券商作文",
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
/**
 * 金额 → 中文单位（亿 / 万）。
 *
 * 财经数据里"59107601989.11 元"没人读得出来，界面必须给"591.08 亿"。
 * 阈值用 1e8/1e4 —— A 股语境下亿是最自然的量级。
 */
export function fmtMoney(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  // 去掉多余的尾随 0（"591.00 亿" → "591 亿"）
  const trim = (x: number) => x.toFixed(digits).replace(/\.?0+$/, "");
  if (abs >= 1e8) return `${trim(n / 1e8)} 亿`;
  if (abs >= 1e4) return `${trim(n / 1e4)} 万`;
  return fmtNum(n, digits);
}

/** 百分数（输入已经是 % 值，不是小数）。 */
export function fmtPct(v: unknown, digits = 1): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(digits).replace(/\.?0+$/, "")}%`;
}

/**
 * 限售解禁的个股明细（scope.stocks[]）。
 *
 * 来源是东财的**个股明细**接口（stock_restricted_release_detail_em），
 * 按市值倒序 —— 解禁影响最大的是最大的那几只，展开先看到它们。
 */
export interface UnlockStock {
  code: string;
  name: string;
  /** 实际解禁市值（元） */
  market_cap: number | null;
  /**
   * 占**解禁前流通市值**比例（%）。
   *
   * ⚠️ 可能大于 100 —— 那不是错：解禁量可以超过原流通盘
   * （次新股首发原股东解禁时常见）。界面不要做 0~100 钳制。
   */
  pct_of_float: number | null;
  /** 限售股类型（首发原股东 / 股权激励 / 追加承诺…） */
  share_type: string;
}

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
