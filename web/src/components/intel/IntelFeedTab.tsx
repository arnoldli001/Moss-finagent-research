/**
 * 情报流：按类型筛选 + 时间倒序展示。
 *
 * ## 两个移动端相关的设计决定
 *
 * **① 桌面用表格，手机用卡片 —— 两套 DOM，用 CSS 切换。**
 * 表格列多（时间/类型/摘要/标的/倾向），390px 里无论怎么压都会把表头
 * 折成竖排单字。横向滚动是可行方案，但那样"摘要"列也一起被挤到屏外，
 * 等于要用户左右拉才能读完一条。卡片则天然纵向堆叠。
 * 两套 DOM 的代价是重复渲染，但避免了在同一个 DOM 上打两套样式补丁
 * （那种做法一旦加列就会两边不一致）。
 *
 * **② `source_alias` 只用于同源判断，不上屏。**
 * 它是稳定假名（`src-3f9a2c1b`），不是给人看的。界面上显示的是
 * `kind_label`（"财经快讯"/"券商研报"…）。把假名印出来等于告诉用户
 * "这几条同源" —— 那是我们自己的聚合口径，没必要交付。
 */

import { useMemo, useState } from "react";
import {
  dayKey, fmtNum, formatTime, IntelFeed, IntelItem, todayKey,
} from "../../intelApi";

/** 筛选页签。`pred` 为 `null` 表示"全部"。 */
type FilterKey = "all" | "newswire" | "broker_report" | "policy" | "research_note";

const FILTERS: Array<{ key: FilterKey; label: string }> = [
  { key: "all", label: "全部" },
  { key: "newswire", label: "财经快讯" },
  { key: "broker_report", label: "券商研报" },
  { key: "research_note", label: "研究笔记" },
  { key: "policy", label: "政策信号" },
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

/** 关联标的的展示文本。研报/笔记常常没有标的。 */
function targetsOf(it: IntelItem): string {
  if (it.industry) return it.industry;
  if (it.codes?.length) return it.codes.slice(0, 3).join(" · ")
    + (it.codes.length > 3 ? ` 等${it.codes.length}只` : "");
  return "";
}

export default function IntelFeedTab({ feed }: { feed: IntelFeed }) {
  const [filter, setFilter] = useState<FilterKey>("all");
  const [openHash, setOpenHash] = useState<string>("");

  const items = useMemo(() => {
    const all = feed.items ?? [];
    return filter === "all" ? all : all.filter((it) => it.kind === filter);
  }, [feed.items, filter]);

  // 计数用后端给的**全量去重计数**（`counts`），不是当前页条数 ——
  // `items` 是限量后的，拿它当"共 N 条"会少报。
  const countOf = (k: FilterKey) =>
    k === "all"
      ? Object.values(feed.counts ?? {}).reduce((a, b) => a + b, 0)
      : (feed.counts?.[k] ?? 0);

  /**
   * 按天分组。
   *
   * 为什么要分组：源的时间戳精度不统一（快讯只有日期、笔记到毫秒），
   * 混在一起排会让人分不清"刚刚"和"昨天"。分组标题给出锚点，
   * 组内仍按原顺序（后端已按时间倒序，这里**不再重排** —— 重排会把
   * 后端"同一天内按类型轮流取样"的均衡顺序打乱）。
   */
  const groups = useMemo(() => {
    const today = todayKey();
    const out: Array<{ day: string; label: string; rows: IntelItem[] }> = [];
    for (const it of items) {
      const d = dayKey(it.published_at);
      const last = out[out.length - 1];
      const label = d === today ? "今天" : d;
      if (last && last.day === d) last.rows.push(it);
      else out.push({ day: d, label, rows: [it] });
    }
    return out;
  }, [items]);

  return (
    <div className="intel-feed">
      {/* ── 筛选页签：手机上横向滑动，不折行 ── */}
      <div className="intel-filterbar" role="tablist" aria-label="情报类型筛选">
        {FILTERS.map((f) => {
          const n = countOf(f.key);
          // 该类型一条都没有时不显示页签（点进去是空列表，纯干扰）。
          // "全部"始终显示。
          if (f.key !== "all" && n === 0) return null;
          return (
            <button
              key={f.key}
              role="tab"
              aria-selected={filter === f.key}
              className={`intel-filter${filter === f.key ? " on" : ""}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
              <span className="intel-filter-n">{n}</span>
            </button>
          );
        })}
      </div>

      {groups.length === 0 && (
        <div className="empty-tip muted-text">该类型当前没有条目</div>
      )}

      {groups.map((g) => (
        <div className="intel-day" key={g.day}>
          <div className="intel-day-head">
            <span className="intel-day-label">{g.label}</span>
            <span className="muted-text">{g.rows.length} 条</span>
          </div>

          {/* ── 桌面：表格 ── */}
          <div className="intel-tablewrap">
            <table className="intel-table">
              <thead>
                <tr>
                  <th className="c-time">时间</th>
                  <th className="c-kind">来源类型</th>
                  <th>标题 / 摘要</th>
                  <th className="c-target">关联标的</th>
                </tr>
              </thead>
              <tbody>
                {g.rows.map((it) => (
                  <tr key={it.content_hash || it.title}>
                    <td className="c-time code">{formatTime(it.published_at)}</td>
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
                    </td>
                    <td className="c-target">
                      {targetsOf(it) || <span className="muted-text">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* ── 手机：卡片 ── */}
          <div className="intel-cards">
            {g.rows.map((it) => {
              const key = it.content_hash || it.title;
              const open = openHash === key;
              const tgt = targetsOf(it);
              return (
                <article
                  className={`intel-card${open ? " open" : ""}`}
                  key={key}
                  onClick={() => setOpenHash(open ? "" : key)}
                  // 可点击才给 button 语义；这里用 role + tabIndex 让键盘也能展开
                  role="button"
                  tabIndex={0}
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
                      {formatTime(it.published_at)}
                    </span>
                  </div>
                  <h3 className="intel-card-title">{it.title}</h3>
                  {/* 折叠时 2 行、展开显示全文。摘要已在后端截断到
                      ≤300 字，展开也不会撑爆。 */}
                  {it.summary && it.summary !== it.title && (
                    <p className="intel-card-sum">{it.summary}</p>
                  )}
                  {tgt && <div className="intel-card-target">{tgt}</div>}
                </article>
              );
            })}
          </div>
        </div>
      ))}

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
        共 {fmtNum(countOf("all"), 0)} 条 · 抓取于 {formatTime(feed.fetched_at, { withDate: true })}
        {feed.degraded && " · 数据不完整（部分来源暂无更新）"}
      </div>
    </div>
  );
}
