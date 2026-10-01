import { AgentOutputSummary } from "../api";
import { agentLabel, confidenceLabel } from "../agentMeta";
import { visibleAnomalies } from "../collectionAnomaly";

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
  // ★★ 2026-09-30 用户口径：「前端显示"部分节点异常、查询超过 10 秒防撞钟"，
  // 这类信息异常信息，**不要显示在用户界面**。要记录并显示到管理员界面的
  // "运行指标"里」。所以这里只渲染**非技术性**的异常；采集缺口/防撞钟/
  // 连接器注册表 dump 属运维信息，已记到 `<run_dir>/collection_anomalies.jsonl`，
  // 在管理员「运行指标 → 数据采集异常」区看。
  // 判定在 `collectionAnomaly.ts`（**唯一一处**，`App.tsx` 也 import 它 ——
  // 同一判断两份实现必然漂移，本项目为此付过代价）。
  const shown = visibleAnomalies(errors);

  return (
    <section className="panel">
      <h2>Agent协作时间线</h2>
      {shown.length > 0 && (
        <div className="warn-box">
          部分节点异常：<ul>{shown.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </div>
      )}
      <ol className="timeline">
        {outputs.map((o) => (
          <li key={o.agent_id} className="timeline-item">
            <div className="timeline-head">
              <span className="agent-id" title={o.agent_id}>
                {o.agent_name ?? agentLabel(o.agent_id)}
              </span>
              <span className={`badge ${CONFIDENCE_CLASS[o.confidence] ?? "conf-low"}`}>
                置信度 {o.confidence_zh ?? confidenceLabel(o.confidence)}
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
