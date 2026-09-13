import { useState } from "react";
import { api, BacktestResponse } from "../api";

const pct = (v: number | null, digits = 2) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
const num = (v: number | null) => (v === null || v === undefined ? "—" : v.toFixed(2));

function EquityChart({ data }: { data: BacktestResponse["equity_curve"] }) {
  const W = 880;
  const H = 260;
  const PAD = 32;
  const values = data.flatMap((p) => [p.strategy, p.buy_and_hold]);
  const min = Math.min(...values, 1);
  const max = Math.max(...values, 1);
  const span = max - min || 1;
  const x = (i: number) =>
    PAD + (i / Math.max(data.length - 1, 1)) * (W - PAD * 2);
  const y = (v: number) =>
    H - PAD - ((v - min) / span) * (H - PAD * 2);
  const line = (key: "strategy" | "buy_and_hold") =>
    data.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const ticks = [max, (max + min) / 2, min].map((v) => v.toFixed(2));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="equity-svg" role="img">
      {ticks.map((t, i) => (
        <g key={t}>
          <line x1={PAD} x2={W - PAD}
                y1={y([max, (max + min) / 2, min][i])}
                y2={y([max, (max + min) / 2, min][i])}
                stroke="var(--border)" strokeDasharray="3 3" />
          <text x={4} y={y([max, (max + min) / 2, min][i]) + 4}
                fontSize="10" fill="var(--muted)">{t}</text>
        </g>
      ))}
      <path d={line("buy_and_hold")} fill="none"
            stroke="var(--muted)" strokeWidth="1.8" />
      <path d={line("strategy")} fill="none"
            stroke="var(--accent)" strokeWidth="2.2" />
      <line x1={PAD} x2={W - PAD} y1={y(1)} y2={y(1)}
            stroke="var(--medium)" strokeDasharray="2 4" />
    </svg>
  );
}

export default function BacktestPanel() {
  const [indicator, setIndicator] = useState("PPI");
  const [code, setCode] = useState("601088");
  const [eps, setEps] = useState(1.0);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      setResult(await api.backtestRun({ indicator, code, eps_pct: eps }));
    } catch (e) {
      setError(`回测失败（全历史行情拉取可能耗时较长）：${String(e)}`);
    } finally {
      setRunning(false);
    }
  };

  const s = result?.strategy;
  const bh = s?.buy_and_hold;

  return (
    <div>
      <div className="warn-box">
        纯本地规则回测（无LLM）：{indicator}同比序列环比变动超过阈值时次月持有，
        否则空仓。按指标发布月对齐，信号不使用未来数据。
      </div>

      <section className="panel">
        <div className="submit-bar" style={{ marginBottom: 0 }}>
          <select value={indicator} onChange={(e) => setIndicator(e.target.value)}
                  disabled={running}>
            <option value="PPI">PPI（工业出厂价同比）</option>
            <option value="CPI">CPI（居民消费价同比）</option>
          </select>
          <input className="target-input" value={code}
                 onChange={(e) => setCode(e.target.value.trim())}
                 placeholder="6位股票代码" maxLength={6} disabled={running} />
          <label className="eps-label">
            环比阈值 ±
            <input type="number" min={0} max={10} step={0.5} value={eps}
                   onChange={(e) => setEps(Number(e.target.value))}
                   style={{ width: 70, marginLeft: 6 }} disabled={running} />
            %
          </label>
          <button onClick={run} disabled={running || code.length !== 6}>
            {running ? "取数回测中…（可能数十秒）" : "运行回测"}
          </button>
        </div>
      </section>

      {error && <div className="error-box">{error}</div>}

      {result && s && bh && (
        <>
          <div className="summary-row">
            <div className="stat-card">
              <span className="stat-label">
                {result.asset} · {result.range.start}~{result.range.end}（{result.periods}月）
                {result.cache.price_hit && (
                  <span className="cache-badge">
                    行情缓存命中（{Math.round(result.cache.ttl_seconds / 60)}分钟TTL）
                  </span>
                )}
              </span>
              <span className="stat-value">
                多{result.signals.long} / 中{result.signals.neutral} / 空{result.signals.avoid}
              </span>
            </div>
            <div className="stat-card">
              <span className="stat-label">策略累计 / 年化</span>
              <span className="stat-value">
                {pct(s.cumulative_return)} / {pct(s.cagr)}
              </span>
            </div>
            <div className="stat-card">
              <span className="stat-label">买入持有累计 / 年化</span>
              <span className="stat-value">
                {pct(bh.cumulative_return)} / {pct(bh.cagr)}
              </span>
            </div>
            <div className="stat-card">
              <span className="stat-label">超额（累计）</span>
              <span className="stat-value"
                    style={{ color: s.excess_cumulative_return >= 0 ? "var(--high)" : "var(--low)" }}>
                {pct(s.excess_cumulative_return)}
              </span>
            </div>
          </div>

          <section className="panel">
            <h2>净值曲线（蓝=规则策略，灰=买入持有，黄虚线=净值1）</h2>
            <EquityChart data={result.equity_curve} />
          </section>

          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <section className="panel" style={{ marginBottom: 0 }}>
              <h2>风险指标对比</h2>
              <table className="audit-table">
                <thead>
                  <tr><th>指标</th><th>规则策略</th><th>买入持有</th></tr>
                </thead>
                <tbody>
                  <tr><td>最大回撤</td><td>{pct(s.max_drawdown)}</td><td>{pct(bh.max_drawdown)}</td></tr>
                  <tr><td>年化波动</td><td>{pct(s.annualized_volatility)}</td><td>{pct(bh.annualized_volatility)}</td></tr>
                  <tr><td>夏普(rf=0)</td><td>{num(s.sharpe_rf0)}</td><td>{num(bh.sharpe_rf0)}</td></tr>
                  <tr><td>持仓月数</td><td>{s.invested_months}/{s.total_months}</td><td>{s.total_months}/{s.total_months}</td></tr>
                </tbody>
              </table>
            </section>

            <section className="panel" style={{ marginBottom: 0 }}>
              <h2>分持有期表现</h2>
              <table className="audit-table">
                <thead>
                  <tr><th>持有期</th><th>看多次数</th><th>命中率</th><th>平均前瞻</th><th>基准</th></tr>
                </thead>
                <tbody>
                  {Object.entries(result.directional).map(([h, b]) => (
                    <tr key={h}>
                      <td>{h}</td>
                      <td>{b.long.n}</td>
                      <td>{pct(b.long.hit_rate, 1)}</td>
                      <td>{pct(b.long.avg_forward_return)}</td>
                      <td>{pct(b.always_long_baseline)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </div>

          <div className="warn-box" style={{ marginTop: 16 }}>{result.disclaimer}</div>
        </>
      )}
    </div>
  );
}
