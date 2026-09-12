import { marked } from "marked";

export default function ReportView({ markdown }: { markdown: string }) {
  const html = marked.parse(markdown, { async: false });
  return (
    <section className="panel report">
      <h2>最终报告</h2>
      <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
    </section>
  );
}
