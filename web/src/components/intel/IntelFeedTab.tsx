/**
 * 情报流：按类型筛选 + 时间倒序展示。
 *
 * ## 三个移动端相关的设计决定
 *
 * **① 桌面用表格，手机用卡片 —— 两套 DOM，用 CSS 切换。**
 * 表格列多（时间/类型/摘要/标的），390px 里无论怎么压都会把表头
 * 折成竖排单字。横向滚动是可行方案，但那样"摘要"列也一起被挤到屏外，
 * 等于要用户左右拉才能读完一条。卡片则天然纵向堆叠。
 * 两套 DOM 的代价是重复渲染，但避免了在同一个 DOM 上打两套样式补丁
 * （那种做法一旦加列就会两边不一致）。
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
 */

import { useMemo, useState } from "react";
import {
  credLevel, credLevelLabel, dayKey, daysBetween, fmtNum, formatTime,
  IntelCredibility, IntelFeed, IntelItem, todayKey,
} from "../../intelApi";

/** 排序模式。服务端决定"谁能进这一页"，前端只切它。 */
type SortKey = "credibility" | "time";

const SORTS: Array<{ key: SortKey; label: string; hint: string }> = [
  { key: "credibility", label: "优先高可信", hint: "取样时优先纳入可信度更高的条目" },
  { key: "time", label: "纯时间", hint: "不做可信度加权，按时间取最近的一批" },
];

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
        onClick={() => setOpen((v) => !v)}
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
          <div>
            <dt>倾向分析</dt>
            <dd className="muted-text">
              {cred.tone_allowed
                ? "可参与（≥50 分）"
                : "不参与（低于 50 分不做倾向分析）"}
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

export default function IntelFeedTab({
  feed, sort, onSort, filter, onFilter, busy = false,
}: {
  feed: IntelFeed;
  sort: SortKey;
  onSort: (s: SortKey) => void;
  filter: string;
  onFilter: (f: string) => void;
  busy?: boolean;
}) {
  const [openHash, setOpenHash] = useState<string>("");
  const today = todayKey();
  const items = feed.items ?? [];

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

      {/* ── 桌面：表格（每行自带日期与可信度环）── */}
      {items.length > 0 && (
        <div className="intel-tablewrap">
          <table className="intel-table">
            <thead>
              <tr>
                <th className="c-time">时间</th>
                <th className="c-kind">来源类型</th>
                <th>标题 / 摘要</th>
                <th className="c-cred">可信度</th>
                <th className="c-target">关联标的</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => {
                const key = it.content_hash || it.title;
                const tl = timeLabel(it.published_at, today);
                return (
                  <tr key={key}>
                    <td className="c-time">
                      <div className="code">{tl.text}</div>
                      <div className="intel-daytag">{tl.sub}</div>
                    </td>
                    <td className="c-kind">
                      <span className={`intel-kind ${kindTone(it.kind)}`}>
                        {it.kind_label}
                      </span>
                    </td>
                    <td className="intel-sum-cell">
                      <div className="intel-title">{it.title}</div>
                      {it.summary && it.summary !== it.title && (
                        <div className="intel-sum">{it.summary}</div>
                      )}
                      <RelatedBadge item={it} />
                    </td>
                    <td className="c-cred">
                      <CredRing cred={it.credibility} />
                    </td>
                    <td className="c-target">
                      {targetsOf(it) || <span className="muted-text">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* ── 手机：卡片 ── */}
      <div className="intel-cards">
        {items.map((it) => {
          const key = it.content_hash || it.title;
          const open = openHash === key;
          const tgt = targetsOf(it);
          const tl = timeLabel(it.published_at, today);
          return (
            <article
              className={`intel-card${open ? " open" : ""}`}
              key={key}
              onClick={() => setOpenHash(open ? "" : key)}
              // 可点击才给 button 语义；用 role + tabIndex 让键盘也能展开
              role="button"
              tabIndex={0}
              aria-expanded={open}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setOpenHash(open ? "" : key);
                }
              }}
            >
              <div className="intel-card-top">
                <span className={`intel-kind ${kindTone(it.kind)}`}>
                  {it.kind_label}
                </span>
                <span className="intel-card-time code">
                  {tl.sub} {tl.text}
                </span>
              </div>
              <h3 className="intel-card-title">{it.title}</h3>
              {/* 折叠时 2 行、展开显示全文。摘要已在后端按类型截断到
                  ≤300 字，展开也不会撑爆。 */}
              {it.summary && it.summary !== it.title && (
                <p className="intel-card-sum">{it.summary}</p>
              )}
              <RelatedBadge item={it} />
              <div className="intel-card-foot">
                <CredRing cred={it.credibility} />
                {tgt && <div className="intel-card-target">{tgt}</div>}
              </div>
            </article>
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
