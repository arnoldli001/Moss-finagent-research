import { TraceDetail } from "../api";

export default function TracePanel({ trace }: { trace: TraceDetail }) {
  const chain = trace.audit_chain;
  return (
    <section className="panel">
      <h2>推理路径与审计</h2>
      <div className="chain-info">
        哈希链{" "}
        <span className={`badge ${chain.valid ? "conf-high" : "conf-low"}`}>
          {chain.valid ? "完整" : "已篡改"}
        </span>{" "}
        共 {chain.records} 条 · 链头{" "}
        <code>{(chain.head ?? "").slice(0, 16)}</code>
      </div>
      <h3>LLM调用审计</h3>
      {trace.llm_calls.length === 0 ? (
        <p className="muted">本任务无LLM调用记录</p>
      ) : (
        <table className="audit-table">
          <thead>
            <tr>
              <th>模型</th><th>tokens</th><th>延迟</th><th>缓存</th><th>降级</th>
            </tr>
          </thead>
          <tbody>
            {trace.llm_calls.map((c, i) => (
              <tr key={i}>
                <td>{c.model}</td>
                <td>{c.tokens_in}/{c.tokens_out}</td>
                <td>{c.latency_ms}ms</td>
                <td>{c.cache_hit ? c.cache_kind : "-"}</td>
                <td>{c.fallback_used ? "是" : "否"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h3>Prompt哈希（前16位）</h3>
      <ul className="hash-list">
        {trace.llm_calls.map((c, i) => (
          <li key={i}><code>{c.prompt_hash.slice(0, 16)}</code> → <code>{c.response_hash.slice(0, 16)}</code></li>
        ))}
      </ul>
    </section>
  );
}
