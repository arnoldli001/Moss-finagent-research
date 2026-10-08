/**
 * 投研分析 · 「逐只结论」面板（多标的）。
 *
 * ## 它解决什么
 *
 * 2026-10-08 之前，问句里点名两只票（"…能否持有高股息的宁波银行和中国神华？"）
 * 时，后端各 Agent 的**逐只**结果在界面上读不到：要么被合并成一句话，
 * 要么只能展开 `<details>` 看一坨原始 JSON。这个面板把 5 个逐只字段
 * （估值 / 建议 / 舆情 / 合规 / 行业）摊成"一只票一块"。
 *
 * ## 单标的时**一个节点都不会多**（硬要求）
 *
 * 判据全在 `multiSubject.ts`（纯函数，**已自检**：`multiSubjectRenderCheck.tsx`）：
 * 新字段在单标的下**缺席或只有 1 条** ⇒ `buildMultiSubjectView()` 返回空数组
 * ⇒ 这里 `return null`。React 对 `null` **不产生任何 DOM 节点**（连占位元素都没有），
 * 所以单标的下的页面与改动前**逐字一致** —— 这不是靠"看起来没变"，
 * 而是靠"没有东西被渲染"+自检里 `renderToStaticMarkup(...) === ""` 这条断言。
 *
 * ⚠️ 别把它改成"先渲染容器、里面再判空"：那样单标的下会多出一个空面板。
 */

import type { AgentOutputSummary } from "../api";
import {
  buildMultiSubjectView,
  NOT_PROVIDED,
  type MultiSubjectRow,
  type MultiSubjectSection,
} from "../multiSubject";

function RowView({ row }: { row: MultiSubjectRow }) {
  return (
    <li className="ms-row">
      <div className="ms-row-head">
        <span className="ms-row-title">{row.title}</span>
        {row.badges.map((b, i) => (
          <span className={`badge ms-badge ms-tone-${b.tone}`} key={i}>{b.text}</span>
        ))}
      </div>
      {row.fields.length > 0 && (
        <dl className="ms-fields">
          {row.fields.map((f, i) => (
            <div className="ms-field" key={i}>
              <dt>{f.label}</dt>
              <dd>{f.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {row.lists.map((l, i) => (
        <p className="ms-list" key={i}>
          <b>{l.label}</b>：{l.items.join("；")}
        </p>
      ))}
      {row.body && (
        <p className="ms-body"><b>{row.body.label}</b>：{row.body.text}</p>
      )}
    </li>
  );
}

function SectionView({ section }: { section: MultiSubjectSection }) {
  return (
    <div className="ms-section">
      <h3>
        {section.title}
        <span className="ms-source">{section.source}</span>
      </h3>
      {/* 本节成立、但有句必须先说的话（行业被截断 / 个别条目读不出） */}
      {section.note && <p className="ms-note">{section.note}</p>}
      {/*
        ★ 字段在场却读不出 ⇒ **如实说**"（未提供）"+原因。
        既不留一个空壳，也不把整节吞掉（"未提供"与"没有这一节"是两件事）。
      */}
      {section.unreadable !== null ? (
        <p className="ms-unreadable">
          {section.title}：{NOT_PROVIDED}—— {section.unreadable}
        </p>
      ) : (
        <ul className="ms-rows">
          {section.rows.map((row) => <RowView row={row} key={row.key} />)}
        </ul>
      )}
    </div>
  );
}

export default function MultiSubjectPanel({
  outputs,
}: {
  outputs: AgentOutputSummary[];
}) {
  const sections = buildMultiSubjectView(outputs);
  // ★★ 单标的（或没有任何逐只字段）⇒ 整块不渲染。见文件头。
  if (sections.length === 0) return null;
  return (
    <section className="panel multi-subject">
      <h2>逐只结论（多标的）</h2>
      {sections.map((s) => <SectionView section={s} key={s.key} />)}
    </section>
  );
}
