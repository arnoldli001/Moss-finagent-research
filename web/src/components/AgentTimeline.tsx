import { AgentOutputSummary } from "../api";

const CONFIDENCE_CLASS: Record<string, string> = {
  high: "conf-high",
  medium: "conf-medium",
  low: "conf-low",
};

function StructuredResult({ result }: { result: Record<string, unknown> }) {
  const entries = Object.entries(result).filter(
    ([k, v]) =>
      !["model_used", "tokens_in", "tokens_out"].includes(k) &&
      v !== null && v !== undefined &&
      !(Array.isArray(v) && v.length === 0)
  );
  if (entries.length === 0) return null;
  return (
    <ul className="structured">
      {entries.map(([k, v]) => (
        <li key={k}>
          <b>{k}</b>:{" "}
          {typeof v === "object" ? JSON.stringify(v, null, 0) : String(v)}
        </li>
      ))}
    </ul>
  );
}

export default function AgentTimeline({
  outputs,
  errors,
}: {
  outputs: AgentOutputSummary[];
  errors: string[];
}) {
  return (
    <section className="panel">
      <h2>Agent协作时间线</h2>
      {errors.length > 0 && (
        <div className="warn-box">
          部分节点异常：<ul>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </div>
      )}
      <ol className="timeline">
        {outputs.map((o) => (
          <li key={o.agent_id} className="timeline-item">
            <div className="timeline-head">
              <span className="agent-id">{o.agent_id}</span>
              <span className={`badge ${CONFIDENCE_CLASS[o.confidence] ?? "conf-low"}`}>
                置信度 {o.confidence}
              </span>
            </div>
            <p className="conclusion">{o.conclusion}</p>
            <details>
              <summary>结构化输出与数据引用</summary>
              <StructuredResult result={o.result} />
              {o.data_refs.length > 0 && (
                <p className="refs">溯源引用: {o.data_refs.join(", ")}</p>
              )}
            </details>
          </li>
        ))}
      </ol>
    </section>
  );
}
