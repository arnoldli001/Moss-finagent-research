/**
 * 平台热议（情报流顶部的区块）。
 *
 * ## 为什么它从"独立子页签"搬到了这里
 *
 * 用户口径（2026-09-25）：
 *
 *   "为什么不把情报流、舆情热度 有明显利空或利多的信息，都通过事件告警弹出。
 *    当前舆情热度监控没做好，不如直接融入到情报流里，把几大平台
 *    （雪球/东方财富股吧/同花顺/财联社/百度人气榜/韭研公社）的热点事件和个股，
 *    聚合汇总显示在情报流或事件告警里。"
 *
 * ## 这个区块与"统计块"的区别（上一版被投诉的根因）
 *
 * 上一版这一页是三个统计块：来源类型结构、按日分布、被提及标的（6 位代码）。
 * 它们的共同问题是**只回答"有多少"，不回答"发生了什么"**。用户的原话是
 * "作为一个投资者没获得任何有用信息"。
 *
 * 所以这里只放三类**具体内容**，每一类都要求"能说出是谁、什么事"：
 *
 *   热议个股   名称 + 代码 + 涨跌幅 + **提及它的平台** + **原文标题**
 *   热议事件   事件标题 + 关联个股 + 一句话说明
 *   平台人气榜 名称 + 关注指数/排名变动 + 来源平台
 *
 * ## 两条界面纪律
 *
 * 1. **必须显示原文标题（evidence）**。这一页的价值在于"可核对" ——
 *    只给一个名字和一个数字，用户无法判断该不该信，那和上一版没区别。
 * 2. **没有数据就明说"暂无"**，不留空壳、不用 0 占位。上一版用 0 和
 *    空列表撑起了整页，看起来有内容，实际什么都没说。
 */

import { useState } from "react";
import { HeatRankRow, HeatStock, HeatTopic, IntelHeat, fmtPct, fmtSigned } from "../../intelApi";

export default function IntelHeatBlock({ heat, loading }: {
  heat?: IntelHeat;
  loading?: boolean;
}) {
  const [open, setOpen] = useState(true);

  if (loading && !heat) {
    return (
      <div className="intel-heat">
        <div className="intel-heat-head">
          <b>平台热议</b>
          <span className="muted-text">正在扫描各平台快讯…</span>
        </div>
      </div>
    );
  }
  if (!heat) return null;

  const stocks = heat.stocks ?? [];
  const topics = heat.topics ?? [];
  const rank = heat.rank ?? [];
  const empty = stocks.length === 0 && topics.length === 0 && rank.length === 0;

  return (
    <div className="intel-heat">
      <div className="intel-heat-head">
        <button className="intel-heat-toggle" onClick={() => setOpen((v) => !v)}
          aria-expanded={open}>
          <b>平台热议</b>
          <span className="intel-heat-counts">
            {stocks.length > 0 && <span>{stocks.length} 只个股</span>}
            {topics.length > 0 && <span>{topics.length} 个事件</span>}
            {rank.length > 0 && <span>{rank.length} 条人气榜</span>}
          </span>
          <span className="intel-heat-caret">{open ? "▾" : "▸"}</span>
        </button>
        <span className="muted-text intel-heat-src">
          扫描 {heat.stocks_scanned} 条平台快讯
          {heat.at ? ` · 事件聚合于 ${heat.at.slice(5, 16).replace("T", " ")}` : ""}
        </span>
      </div>

      {open && (
        <div className="intel-heat-body">
          {empty && (
            <div className="intel-heat-none">
              当前窗口内没有扫到被多个平台提到的 A 股，也没有聚合出热议事件。
              <span className="muted-text">
                （这不是"今天没事发生"，而是"这一批公开快讯里没有可归因到个股的内容"。）
              </span>
            </div>
          )}

          {stocks.length > 0 && (
            <section className="intel-heat-sec">
              <h4>热议个股<span className="muted-text">按被提及的条数排序 · 名称与代码来自本地个股名录精确匹配</span></h4>
              <div className="heat-stocks">
                {stocks.map((s) => <StockCard key={s.code || s.name} s={s} />)}
              </div>
            </section>
          )}

          {topics.length > 0 && (
            <section className="intel-heat-sec">
              <h4>热议事件<span className="muted-text">本地模型聚合 · 标题须在原文出现才保留</span></h4>
              <ul className="heat-topics">
                {topics.map((t, i) => <TopicRow key={i} t={t} />)}
              </ul>
            </section>
          )}

          {rank.length > 0 && (
            <section className="intel-heat-sec">
              <h4>平台人气榜<span className="muted-text">公开人气/关注度排名，非推荐</span></h4>
              <div className="heat-rank">
                {rank.map((r, i) => <RankItem key={`${r.code || r.name}-${i}`} r={r} />)}
              </div>
            </section>
          )}

          <div className="intel-heat-note">{heat.note}</div>
          {(heat.gaps?.length ?? 0) > 0 && (
            <ul className="intel-heat-gaps">
              {heat.gaps.map((g, i) => <li key={i}>{g.message}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** 一只热议个股：名字 + 代码 + 提及条数 + 平台 + **可核对的原文标题**。 */
function StockCard({ s }: { s: HeatStock }) {
  const [open, setOpen] = useState(false);
  const ev = s.evidence ?? [];
  return (
    <div className={`heat-stock${open ? " open" : ""}`}>
      <button className="heat-stock-main" onClick={() => setOpen((v) => !v)}
        aria-expanded={open}>
        <span className="heat-stock-name">{s.name}</span>
        <span className="heat-stock-code">{s.code}</span>
        <span className="heat-stock-n" title="被多少条不同快讯提到">
          提及 {s.mention_count}
        </span>
        <span className="heat-stock-plats">
          {(s.platforms ?? []).length > 0
            ? s.platforms.join(" · ")
            : <span className="muted-text">公开财经信息</span>}
        </span>
      </button>
      {open && (
        <div className="heat-stock-ev">
          {ev.length === 0 ? (
            <div className="muted-text">没有可展开的原文标题。</div>
          ) : (
            <ul>
              {ev.map((t, i) => <li key={i}>{t}</li>)}
            </ul>
          )}
          <div className="muted-text heat-stock-time">
            最近一次出现：{(s.last_seen || "").slice(0, 16) || "—"}
          </div>
        </div>
      )}
    </div>
  );
}

function TopicRow({ t }: { t: HeatTopic }) {
  return (
    <li className="heat-topic">
      <div className="heat-topic-title">
        {t.title}
      </div>
      {t.detail && <div className="heat-topic-detail">{t.detail}</div>}
      {(t.related ?? []).length > 0 && (
        <div className="heat-topic-rel">关联：{t.related.join("、")}</div>
      )}
    </li>
  );
}

/**
 * 一只人气榜个股 —— **行内紧凑排布**，一行放多只，行数取最少。
 *
 * ## 为什么和上面的 `StockCard` 长得完全不一样
 *
 * 热议个股要能展开**可核对的原文标题**（那是它的证据），所以一张卡片一行、
 * 可以很占地方；人气榜回答的只是"现在哪些票在被看"，上榜动辄十几二十只，
 * 一只一行要滚好几屏才能看完。所以这里：
 *
 * * 整条压成一个不可展开的短项，靠 CSS `flex-wrap` 自动折行 ——
 *   一屏看完全部上榜股；
 * * **保留名次号**（`#3`）。行内流式排布丢掉了"一只一行"自上而下的顺序感，
 *   名次必须自己写出来，否则"人气榜"就不成榜了；
 * * 热度指标按来源二选一（`heat` 来自百度热搜、`focus` 来自千股千评，
 *   实际不会同时有），避免同一件事显示两个数把行撑宽。
 *
 * ## 来源平台（`platform`）为什么不显示了，但也没丢
 *
 * 用户要求"不用注明来源"，所以行内**不再渲染** `.heat-rank-plat`。
 * 但它没有被删除，而是移进 `title` —— 项目的**数据溯源规范**要求每个数据点
 * 都能追到来源，把一个字段从界面上拿掉、同时从数据里删掉，等于让这个数
 * 变成不可核对的。放进 tooltip 既满足"不注明来源"，又保住了可追溯性。
 * tooltip 里同时给出 `score` / `inst` 这两个原先也没在行内展示的字段。
 */
function RankItem({ r }: { r: HeatRankRow }) {
  const heatText = r.heat !== null && r.heat !== undefined
    ? `热${Math.round(r.heat / 1000)}k`
    : r.focus !== null && r.focus !== undefined ? `关${r.focus}` : "";
  const tips = [
    r.rank > 0 ? `名次：第 ${r.rank} 名` : "",
    r.platform ? `来源平台：${r.platform}` : "",
    r.pct_change !== null && r.pct_change !== undefined
      ? `当日涨跌：${fmtPct(r.pct_change)}` : "",
    r.rank_change !== 0
      ? `排名较昨日：${fmtSigned(r.rank_change)}（正数 = 名次上升）` : "",
    r.heat !== null && r.heat !== undefined ? `综合热度：${r.heat}` : "",
    r.focus !== null && r.focus !== undefined
      ? `关注指数（千股千评 0-100）：${r.focus}` : "",
    r.score !== null && r.score !== undefined ? `综合得分：${r.score}` : "",
    r.inst !== null && r.inst !== undefined ? `机构参与度：${r.inst}` : "",
  ].filter(Boolean).join("\n");

  return (
    <span className="heat-rank-item" title={tips}>
      {r.rank > 0 && <span className="heat-rank-no">{r.rank}</span>}
      <span className="heat-rank-name">{r.name}</span>
      {r.code && <span className="heat-rank-code">{r.code}</span>}
      {r.pct_change !== null && r.pct_change !== undefined && (
        <span className={`heat-rank-pct ${r.pct_change >= 0 ? "up" : "down"}`}>
          {fmtPct(r.pct_change)}
        </span>
      )}
      {r.rank_change !== 0 && (
        <span className={`heat-rank-chg ${r.rank_change > 0 ? "up" : "down"}`}>
          {r.rank_change > 0 ? "↑" : "↓"}{Math.abs(r.rank_change)}
        </span>
      )}
      {heatText && <span className="heat-rank-heat">{heatText}</span>}
    </span>
  );
}
