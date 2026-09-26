/**
 * 盘前简报：盘前新闻 / 研报热度 / 政策信号 / 券商作文 四段聚合视图。
 *
 * ## ⚠️ 名称与实质的差距，必须如实说明
 *
 * 设计稿里这一页叫「盘前简报」，包含三项**定时任务产物**：
 * 盘前预测（08:10）、盘前新闻（08:15）、盘前研报热度（08:28）。
 * 当前后端 `/intel/brief` **只做了四段聚合**（把情报流按类型分组），
 * **没有做预测、也没有算研报热度评分** —— 那属于 P1/P2 的分层打分。
 *
 * 所以这一页的标题写「盘前信息汇总」而不是「盘前简报」，
 * 并明确写出"本页是聚合视图，不含预测"。把没做的事说成做了，
 * 比少做一个功能严重得多 —— 用户会据此以为看到了"盘前结论"。
 *
 * ## 四段的排列顺序有理由，不是随意的
 *
 *   政策信号 > 盘前新闻 > 研报 > 券商作文
 *
 * 政策影响面最大且最不可预期（一条政策能改变整条主线），
 * 研究笔记是第三方观点里最"软"的一类，排最后。
 */

import { useState } from "react";
import { IntelItem, PremarketBrief, formatTime } from "../../intelApi";

function kindTone(kind: string): string {
  switch (kind) {
    case "broker_report": return "blue";
    case "policy": return "purple";
    case "research_note": return "green";
    default: return "gray";
  }
}

function Section({
  title, sub, items, empty, openHash, onToggle,
}: {
  title: string;
  sub: string;
  items: IntelItem[];
  empty: string;
  openHash: string;
  onToggle: (key: string) => void;
}) {
  return (
    <section className="brief-sec">
      <div className="brief-sec-head">
        <h3>{title}</h3>
        <span className="muted-text">{sub}</span>
        <span className="brief-sec-n code">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="empty-tip muted-text" style={{ padding: "12px 0" }}>
          {empty}
        </div>
      ) : (
        <ul className="brief-list">
          {items.map((it) => {
            const key = it.content_hash || it.title;
            const open = openHash === key;
            return (
              // 可点开看全文。挂 role+tabIndex+键盘处理，让**键盘与读屏**
              // 也能展开 —— 只挂 onClick 的话键盘用户完全打不开。
              <li
                key={key}
                className={open ? "open" : ""}
                role="button"
                tabIndex={0}
                aria-expanded={open}
                onClick={() => onToggle(key)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onToggle(key);
                  }
                }}
              >
                <div className="brief-item-top">
                  <span className={`intel-kind ${kindTone(it.kind)}`}>
                    {it.kind_label}
                  </span>
                  <span className="code brief-time">
                    {formatTime(it.published_at)}
                  </span>
                  {/* 研报署名是公开信息，可以给；其它类型的 `agency`
                      后端已置空，不会漏出渠道 */}
                  {it.agency && <span className="brief-agency">{it.agency}</span>}
                </div>
                <div className="brief-title">{it.title}</div>
                {it.summary && it.summary !== it.title && (
                  <div className="brief-sum">{it.summary}</div>
                )}
                {(it.codes?.length > 0 || it.industry) && (
                  <div className="brief-targets">
                    {it.industry && <span className="heat-tag">{it.industry}</span>}
                    {(it.codes ?? []).slice(0, 4).map((c) => (
                      <span className="heat-tag code" key={c}>{c}</span>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export default function IntelBriefTab({ brief }: { brief: PremarketBrief }) {
  const s = brief.sections;
  const total = Object.values(brief.counts ?? {}).reduce((a, b) => a + b, 0);
  // 展开哪一条（点一下看全文）。摘要默认 3 行封顶，手机上不展开只能看个开头。
  const [openHash, setOpenHash] = useState("");
  const toggle = (k: string) => setOpenHash((prev) => (prev === k ? "" : k));

  return (
    <div className="intel-brief">
      <div className="brief-banner">
        <b>本页是公开信息的聚合视图，不含预测、不含方向判断。</b>
        按类型分组展示当前窗口内的条目（共 {total} 条）。
        涉及第三方观点的内容一律按来源类型标注归属，平台不加工其倾向。
      </div>

      <Section
        title="政策信号"
        sub="影响面最大、最不可预期"
        items={s.policy ?? []}
        empty="当前窗口没有政策类条目"
        openHash={openHash} onToggle={toggle}
      />
      <Section
        title="盘前新闻"
        sub="财经快讯聚合"
        items={s.premarket_news ?? []}
        empty="当前窗口没有快讯条目"
        openHash={openHash} onToggle={toggle}
      />
      <Section
        title="券商研报"
        sub="持牌机构署名"
        items={s.broker_heat ?? []}
        empty="当前窗口没有研报（需在「热点&研报小作文」里配置关注标的）"
        openHash={openHash} onToggle={toggle}
      />
      <Section
        title="券商作文"
        sub="第三方笔记，倾向属原文"
        items={s.research_notes ?? []}
        empty="当前窗口没有券商作文（该来源需要有效授权）"
        openHash={openHash} onToggle={toggle}
      />

      {brief.degraded && (
        <div className="intel-gaps">
          <span className="intel-gap">
            数据不完整：部分来源当前暂无更新（下次采集会补齐）
          </span>
        </div>
      )}

      <div className="intel-foot muted-text">
        汇总于 {formatTime(brief.fetched_at, { withDate: true })}
      </div>

      {brief.disclaimer && <p className="disclaimer">{brief.disclaimer}</p>}
    </div>
  );
}
