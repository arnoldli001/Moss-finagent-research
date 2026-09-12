import { useCallback, useEffect, useState } from "react";
import { api, Health, LlmMetrics } from "../api";

const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(1)}%`);
const ms = (v: number | null) => (v === null ? "—" : `${v}ms`);

function HealthStrip({ health }: { health: Health }) {
  const chainOk = health.audit_chain.valid;
  return (
    <section className="panel">
      <h2>数据源与依赖健康</h2>
      <table className="audit-table sched-table">
        <thead>
          <tr><th>组件</th><th>状态</th><th>说明</th></tr>
        </thead>
        <tbody>
          {health.data_sources.connectors.map((c) => (
            <tr key={c.name}>
              <td className="agent-id">{c.name}</td>
              <td>
                {c.simulated
                  ? <span className="badge run-skipped">模拟数据</span>
                  : <span className="badge run-success">{c.status}</span>}
              </td>
              <td>{c.indicators.join("、")}</td>
            </tr>
          ))}
          <tr>
            <td className="agent-id">存储({health.data_sources.storage.backend})</td>
            <td>
              <span className={`badge ${health.data_sources.storage.status === "ok" ? "run-success" : "run-failed"}`}>
                {health.data_sources.storage.status}
              </span>
            </td>
            <td>
              {health.data_sources.storage.indicators !== undefined &&
                `${health.data_sources.storage.indicators} 个指标 · ${health.data_sources.storage.points} 个数据点`}
            </td>
          </tr>
          <tr>
            <td className="agent-id">Redis缓存</td>
            <td>
              <span className={`badge ${health.data_sources.redis_cache === "ok" ? "run-success" : "run-skipped"}`}>
                {health.data_sources.redis_cache}
              </span>
            </td>
            <td>未启用时直连存储，不影响功能</td>
          </tr>
          <tr>
            <td className="agent-id">Ollama</td>
            <td>
              <span className={`badge ${health.model_gateway.ollama === "connected" ? "run-success" : "run-failed"}`}>
                {health.model_gateway.ollama}
              </span>
            </td>
            <td>本地模型网关</td>
          </tr>
          <tr>
            <td className="agent-id">DeepSeek</td>
            <td>
              <span className={`badge ${health.model_gateway.deepseek === "configured" ? "run-success" : "run-skipped"}`}>
                {health.model_gateway.deepseek}
              </span>
            </td>
            <td>云端降级链路</td>
          </tr>
          <tr>
            <td className="agent-id">审计哈希链</td>
            <td>
              <span className={`badge ${chainOk ? "run-success" : "run-failed"}`}>
                {chainOk ? "valid" : "broken"}
              </span>
            </td>
            <td>{health.audit_chain.records} 条Trace记录</td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

export default function MetricsPanel() {
  const [metrics, setMetrics] = useState<LlmMetrics | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [m, h] = await Promise.all([api.llmMetrics(1000), api.health()]);
      setMetrics(m.metrics);
      setHealth(h);
      setError(null);
    } catch (e) {
      setError(`加载运行指标失败：${String(e)}`);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 15000);
    return () => window.clearInterval(t);
  }, [refresh]);

  if (error) return <div className="error-box">{error}</div>;
  if (!metrics) return <div className="progress-box"><span className="spinner" />加载指标中…</div>;

  const lat = metrics.latency_ms;

  return (
    <div>
      {health && <HealthStrip health={health} />}
      <div className="sched-head">
        <div className="warn-box" style={{ margin: 0 }}>
          最近 {metrics.window_calls} 次LLM调用窗口（审计JSONL实时聚合，15秒自动刷新；慢调用阈值3秒）。
        </div>
        <button className="btn-ghost" onClick={refresh}>刷新</button>
      </div>

      <div className="summary-row">
        <div className="stat-card">
          <span className="stat-label">窗口调用</span>
          <span className="stat-value">{metrics.window_calls}</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">缓存命中率</span>
          <span className="stat-value">{pct(metrics.cache_hit_rate)}</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">降级率</span>
          <span className="stat-value">{pct(metrics.fallback_rate)}</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">P95 延迟</span>
          <span className="stat-value">{ms(lat.p95)}</span>
        </div>
      </div>

      <section className="panel">
        <h2>延迟分布</h2>
        <table className="audit-table sched-table">
          <thead>
            <tr><th>P50</th><th>P95</th><th>P99</th><th>平均</th><th>最大</th><th>慢调用(≥3s)</th><th>错误率</th></tr>
          </thead>
          <tbody>
            <tr>
              <td>{ms(lat.p50)}</td>
              <td>{ms(lat.p95)}</td>
              <td>{ms(lat.p99)}</td>
              <td>{ms(lat.avg)}</td>
              <td>{ms(lat.max)}</td>
              <td className={metrics.slow_calls_over_3s > 0 ? "run-error" : ""}>
                {metrics.slow_calls_over_3s}
              </td>
              <td>{pct(metrics.error_rate)}</td>
            </tr>
          </tbody>
        </table>
        <h3>Token用量（窗口累计）</h3>
        <p className="conclusion">
          输入 {metrics.tokens.in.toLocaleString()} · 输出 {metrics.tokens.out.toLocaleString()}
          {" "}· 合计 {metrics.tokens.total.toLocaleString()}
        </p>
      </section>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <section className="panel" style={{ marginBottom: 0 }}>
          <h2>按模型提供方</h2>
          <table className="audit-table">
            <thead>
              <tr><th>提供方</th><th>调用</th><th>缓存命中</th><th>错误率</th><th>P95</th></tr>
            </thead>
            <tbody>
              {metrics.by_provider.map((p) => (
                <tr key={p.provider}>
                  <td className="agent-id">{p.provider}</td>
                  <td>{p.calls}</td>
                  <td>{pct(p.cache_hit_rate)}</td>
                  <td>{pct(p.error_rate)}</td>
                  <td>{ms(p.p95_latency_ms)}</td>
                </tr>
              ))}
              {metrics.by_provider.length === 0 && (
                <tr><td colSpan={5} style={{ color: "var(--muted)" }}>暂无调用</td></tr>
              )}
            </tbody>
          </table>
        </section>

        <section className="panel" style={{ marginBottom: 0 }}>
          <h2>按Agent</h2>
          <table className="audit-table">
            <thead><tr><th>Agent</th><th>调用次数</th></tr></thead>
            <tbody>
              {metrics.by_agent.map((a) => (
                <tr key={a.agent_id}>
                  <td className="agent-id">{a.agent_id}</td>
                  <td>{a.calls}</td>
                </tr>
              ))}
              {metrics.by_agent.length === 0 && (
                <tr><td colSpan={2} style={{ color: "var(--muted)" }}>暂无调用</td></tr>
              )}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}
