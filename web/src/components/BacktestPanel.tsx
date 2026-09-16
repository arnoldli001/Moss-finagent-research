import { useState } from "react";
import { api, BacktestRequest, BacktestResponse } from "../api";
import QuantFactorPanel from "./QuantFactorPanel";
import { QuantSingleStockPanel } from "./QuantSingleStockPanel";
import { StockPicker } from "./StockPicker";

const pct = (v: number | null, digits = 2) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
const num = (v: number | null) => (v === null || v === undefined ? "—" : v.toFixed(2));
const money = (v: number | null | undefined) =>
  v === null || v === undefined
    ? "—"
    : v >= 10000
      ? `${(v / 10000).toFixed(2)}万元`
      : `${v.toFixed(0)}元`;

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
  const gridVals = [max, (max + min) / 2, min];
  const ticks = gridVals.map((v) => v.toFixed(2));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="equity-svg" role="img">
      {ticks.map((t, i) => (
        <g key={t}>
          <line x1={PAD} x2={W - PAD} y1={y(gridVals[i])} y2={y(gridVals[i])}
                stroke="var(--border)" strokeDasharray="3 3" />
          <text x={4} y={y(gridVals[i]) + 4}
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

const INDICATOR_OPTIONS: [string, string][] = [
  ["PPI", "PPI（工业出厂价同比）"],
  ["CPI", "CPI（居民消费价同比）"],
  ["M2", "M2（货币供应同比）"],
  ["社融", "社融（社融增量，亿元）"],
];

export default function BacktestPanel() {
  // 模式：宏观择时（原有单一宏观因子）/ 多因子（35 个核心因子）
  const [mode, setMode] = useState<"macro" | "factor" | "single">("macro");
  const [indicator, setIndicator] = useState("PPI");
  const [assetType, setAssetType] =
    useState<"stock" | "index" | "etf">("stock");
  const [code, setCode] = useState("601088");
  const [eps, setEps] = useState(1.0);
  const [startMonth, setStartMonth] = useState("");
  const [endMonth, setEndMonth] = useState("");
  const [capital, setCapital] = useState(1_000_000);
  const [commissionPct, setCommissionPct] = useState(0.025); // 万2.5
  const [stampPct, setStampPct] = useState(0.05);             // 千0.5
  const [slippagePct, setSlippagePct] = useState(0.05);
  const [cashYieldPct, setCashYieldPct] = useState(1.5);
  const [peWatermark, setPeWatermark] = useState("");
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const switchAsset = (t: "stock" | "index" | "etf") => {
    setAssetType(t);
    // 印花税仅个股卖出收取；切换类型时给合理默认（用户仍可手改）
    setStampPct(t === "stock" ? 0.05 : 0);
    setCode(t === "index" ? "000300" : t === "etf" ? "510300" : "601088");
    if (t !== "stock") setPeWatermark("");
  };

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    setProgress("提交回测任务…");
    const body: BacktestRequest = {
      indicator,
      code,
      asset_type: assetType,
      eps_pct: eps,
      start_date: startMonth || null,
      end_date: endMonth || null,
      initial_capital: capital,
      commission_rate: commissionPct / 100,
      stamp_tax_rate: stampPct / 100,
      slippage_rate: slippagePct / 100,
      cash_annual_yield: cashYieldPct / 100,
      pe_watermark:
        assetType === "stock" && peWatermark !== ""
          ? Number(peWatermark)
          : null,
    };
    try {
      // 异步任务模式：POST立即返回job_id，轮询进度——避免全历史拉取
      // 数十秒的长请求被浏览器/代理掐断（此前表现为Failed to fetch）
      const started = await api.backtestRun(body);
      const interval = Math.max(started.poll_interval_seconds, 1) * 1000;
      for (;;) {
        await new Promise((r) => setTimeout(r, interval));
        const job = await api.backtestJob(started.job_id);
        if (job.status === "done" && job.result) {
          setResult(job.result);
          break;
        }
        if (job.status === "error") {
          throw new Error(job.error || "回测任务失败");
        }
        const remain = Math.max(
          (job.estimated_wait_seconds || 0) - Math.round(job.elapsed_seconds),
          0,
        );
        setProgress(
          `数据下载中（${job.stage_label}）：已等待 ${Math.round(job.elapsed_seconds)} 秒`
          + (remain > 0 ? `，预计还需约 ${remain} 秒` : "，仍在拉取请稍候"),
        );
      }
    } catch (e) {
      setError(`回测失败：${String(e)}`);
    } finally {
      setRunning(false);
      setProgress(null);
    }
  };

  const s = result?.strategy;
  const bh = s?.buy_and_hold;
  const codePlaceholder =
    assetType === "stock" ? "6位股票代码" : assetType === "index"
      ? "6位指数代码（如000300）" : "6位ETF代码（如510300）";

  return (
    <div>
      <nav className="mode-switch" style={{ marginBottom: 12 }}>
        <button className={mode === "macro" ? "mode-btn active" : "mode-btn"}
                onClick={() => setMode("macro")}
                title="宏观择时：单一宏观指标（CPI/PPI/M2/社融）环比阈值择时">
          宏观择时
        </button>
        <button className={mode === "factor" ? "mode-btn active" : "mode-btn"}
                onClick={() => setMode("factor")}
                title="多因子：35 个核心因子（价值/成长/质量/动量/波动/流动性/规模）">
          多因子（35 个因子）
        </button>
        <button className={mode === "single" ? "mode-btn active" : "mode-btn"}
                onClick={() => setMode("single")}
                title="单股票策略：指定一只票，用因子条件描述买卖点做时序回测，可一键保存策略">
          单股票策略回测
        </button>
      </nav>

      {mode === "single" ? (
        <QuantSingleStockPanel />
      ) : mode === "factor" ? (
        <QuantFactorPanel />
      ) : (
      <>
      <div className="warn-box">
        纯本地规则回测（无LLM）：{indicator}月度序列环比变动超过阈值时次月持有，
        否则空仓（空仓按货基计息）；含佣金/印花税/滑点。按指标发布月对齐，
        信号不使用未来数据。数据链路：QMT → 本地CSV → AkShare，绝不使用模拟数据。
      </div>

      <section className="panel">
        <div className="submit-bar" style={{ marginBottom: 10 }}>
          <select value={assetType}
                  onChange={(e) => switchAsset(e.target.value as typeof assetType)}
                  disabled={running}>
            <option value="stock">个股</option>
            <option value="index">指数</option>
            <option value="etf">ETF</option>
          </select>
          <select value={indicator} onChange={(e) => setIndicator(e.target.value)}
                  disabled={running}>
            {INDICATOR_OPTIONS.map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
          </select>
          <StockPicker
            value={code}
            disabled={running}
            onChange={setCode}
            placeholder={codePlaceholder}
            width={260}
          />
          <label className="eps-label">
            环比阈值 ±
            <input type="number" min={0} max={10} step={0.5} value={eps}
                   onChange={(e) => setEps(Number(e.target.value))}
                   style={{ width: 70, marginLeft: 6 }} disabled={running} />
            %
          </label>
        </div>
        <div className="submit-bar" style={{ marginBottom: 0, gap: 12, flexWrap: "wrap" }}>
          <label className="eps-label">
            起 <input type="month" value={startMonth}
                      onChange={(e) => setStartMonth(e.target.value)}
                      style={{ width: 130, marginLeft: 4 }} disabled={running} />
          </label>
          <label className="eps-label">
            止 <input type="month" value={endMonth}
                      onChange={(e) => setEndMonth(e.target.value)}
                      style={{ width: 130 }} disabled={running} />
          </label>
          <label className="eps-label">
            初始资金
            <input type="number" min={10000} step={10000} value={capital}
                   onChange={(e) => setCapital(Number(e.target.value))}
                   style={{ width: 110, marginLeft: 6 }} disabled={running} />
          </label>
          <label className="eps-label">
            佣金单边%
            <input type="number" min={0} step={0.005} value={commissionPct}
                   onChange={(e) => setCommissionPct(Number(e.target.value))}
                   style={{ width: 70, marginLeft: 6 }} disabled={running} />
          </label>
          <label className="eps-label">
            印花税卖出%
            <input type="number" min={0} step={0.01} value={stampPct}
                   onChange={(e) => setStampPct(Number(e.target.value))}
                   style={{ width: 70, marginLeft: 6 }}
                   disabled={running || assetType !== "stock"} />
          </label>
          <label className="eps-label">
            滑点单边%
            <input type="number" min={0} step={0.01} value={slippagePct}
                   onChange={(e) => setSlippagePct(Number(e.target.value))}
                   style={{ width: 70, marginLeft: 6 }} disabled={running} />
          </label>
          <label className="eps-label">
            空仓年化%
            <input type="number" min={0} step={0.1} value={cashYieldPct}
                   onChange={(e) => setCashYieldPct(Number(e.target.value))}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
          </label>
          {assetType === "stock" && (
            <label className="eps-label">
              PE(TTM)≤
              <input type="number" min={0} step={1} value={peWatermark}
                     onChange={(e) => setPeWatermark(e.target.value)}
                     placeholder="不启用"
                     style={{ width: 80, marginLeft: 6 }} disabled={running} />
            </label>
          )}
          <button onClick={run} disabled={running || code.length !== 6}>
            {running ? "数据下载/回测中…" : "运行回测"}
          </button>
        </div>
      </section>

      {progress && (
        <div className="warn-box">
          ⏳ {progress}。首次回测需拉取全历史行情（QMT → 本地CSV → AkShare），
          数据一次拉取后缓存10分钟，再次运行秒回。
        </div>
      )}

      {error && <div className="error-box">{error}</div>}

      {result && s && bh && (
        <>
          {result.pe_gate.enabled && result.pe_gate.coverage < 0.8 && (
            <div className="warn-box">
              PE数据覆盖率仅 {pct(result.pe_gate.coverage, 1)}
              （{result.pe_gate.months_covered}/{result.periods}月），
              缺失月份闸门自动放行，结果可能高估。
            </div>
          )}
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
              <span className="stat-label">期末资产 / 交易费用</span>
              <span className="stat-value">
                {money(s.final_equity)}
                <span className="cache-badge">
                  {s.trades}次换手 · {money(s.total_transaction_cost)}
                </span>
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
                  <tr><td>换手次数</td><td>{s.trades}</td><td>0</td></tr>
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
      </>
      )}
    </div>
  );
}
