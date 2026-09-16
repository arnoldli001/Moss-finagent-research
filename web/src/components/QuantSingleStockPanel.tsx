import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type QuantSingleJob,
  type QuantSingleRequest,
  type QuantSingleResult,
  type QuantStrategyList,
  type QuantStrategySummary,
} from "../api";
import { ConditionInput } from "./ConditionInput";
import { QuantHelpModal } from "./QuantHelpModal";
import { StockPicker } from "./StockPicker";
import { StrategyCasesPanel } from "./StrategyCasesPanel";

/**
 * 单股票多因子条件策略回测 + 策略库。
 *
 * 与「多因子」模式的分工：
 *   多因子模式回答「按因子排序分组，哪一组更好」（截面/组合视角）；
 *   这里回答「我就交易这一只票，什么时候买、什么时候卖、能赚多少」（时序视角）。
 *
 * 界面上刻意把三件事摆在显眼位置，因为它们是这类回测最容易骗人的地方：
 *  1. **样本外**（默认展开的是样本外，不是全样本）；
 *  2. **交易笔数**（少于 10 笔的胜率/盈亏比基本是噪声）；
 *  3. **买入持有基准**（跑不赢躺着不动，策略就没有意义）。
 */

const TEMPLATES: { label: string; entry: string; exit: string; hint: string }[] = [
  {
    label: "均线多头",
    entry: "close > MA(close, 20) AND MA(close, 20) > MA(close, 60)",
    exit: "close < MA(close, 20)",
    hint: "20 日线上穿且均线多头排列时持有，跌破 20 日线离场",
  },
  {
    label: "超跌反弹",
    entry: "PCTL_TS(close, 60) < 20 AND DELTA(close, 1) > 0",
    exit: "PCTL_TS(close, 60) > 70",
    hint: "价格处于近 60 日低位且当日转涨时买入，回到高位区离场",
  },
  {
    label: "低估值+动量",
    entry: "pe_ttm < 20 AND momentum_20 > 0 AND close > MA(close, 10)",
    exit: "momentum_20 < 0",
    hint: "估值与动量双条件（因子名见「多因子」页的因子库）",
  },
  {
    label: "波动收缩突破",
    entry: "close > REF(MAX_TS(close, 20), 1) AND atr_20 / close < 0.03",
    exit: "close < MA(close, 10)",
    hint: "突破前 20 日高点、且单位波动率仍低（ATR/价格 < 3%）时买入",
  },
];

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)}%`;
}

function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(digits);
}

/** 净值曲线：策略 vs 买入持有 vs 指数（同步归一，便于直接比较）。 */
function EquityChart({ result }: { result: QuantSingleResult }) {
  const width = 780;
  const height = 200;
  const equity = result.equity;
  const benchmark = result.benchmark;
  const index = result.benchmark_index ?? [];
  if (!equity?.length || !benchmark?.length) return null;
  const series = [
    { values: equity.map((v) => v / (equity[0] || 1)),
      color: "var(--accent)", label: "策略", width: 2 },
    { values: benchmark.map((v) => v / (benchmark[0] || 1)),
      color: "var(--muted)", label: "买入持有（同一只票）", width: 1.5 },
    ...(index.length
      ? [{ values: index.map((v) => v / (index[0] || 1)),
           color: "var(--medium, #d9a24a)",
           label: `指数 ${result.index_code}`, width: 1.5 }]
      : []),
  ];
  const all = series.flatMap((item) => item.values)
    .filter((value) => Number.isFinite(value));
  const low = Math.min(...all, 1);
  const high = Math.max(...all, 1);
  const span = high - low || 1;
  const toPath = (values: number[]) =>
    values
      .map((value, i) => {
        const x = (i / Math.max(values.length - 1, 1)) * width;
        const y = height - ((value - low) / span) * height;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  return (
    <div className="equity-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img"
           aria-label="策略净值与基准对比">
        <line x1="0" y1={height - ((1 - low) / span) * height}
              x2={width} y2={height - ((1 - low) / span) * height}
              stroke="var(--border)" strokeDasharray="4 4" />
        {series.slice().reverse().map((item) => (
          <path key={item.label} d={toPath(item.values)} fill="none"
                stroke={item.color} strokeWidth={item.width} />
        ))}
      </svg>
      <div className="muted-text" style={{ fontSize: 12 }}>
        {series.map((item) => (
          <span key={item.label} style={{ color: item.color, marginRight: 12 }}>
            ━ {item.label}
          </span>
        ))}
        · 均以首日归一 · {result.start} ~ {result.end}（
        {result.trading_days} 个交易日）
      </div>
    </div>
  );
}

/** 价值判定：四个维度各自独立显示，不合成一句口号。 */
function VerdictBlock({ result }: { result: QuantSingleResult }) {
  const verdict = result.verdict;
  if (!verdict?.checks?.length) return null;
  return (
    <div className={`verdict-block ${verdict.has_value ? "positive" : "neutral"}`}>
      <div className="verdict-head">
        <b>{verdict.has_value ? "✓ " : "✗ "}{verdict.summary}</b>
        <span className="muted-text">
          {" "}（{verdict.passed_count}/{verdict.total_count} 个维度成立）
        </span>
      </div>
      <ul className="verdict-checks">
        {verdict.checks.map((item) => (
          <li key={item.key}>
            <span className={item.passed === true ? "ok" : "no"}>
              {item.passed === true ? "✓" : item.passed === false ? "✗" : "—"}
            </span>{" "}
            {item.label}
            <span className="muted-text"> · {item.detail}</span>
          </li>
        ))}
      </ul>
      <div className="muted-text" style={{ fontSize: 11 }}>
        四个维度都来自本次回测的实测数字，没有任何一项被写死；
        判定口径见 docs/QUANT_M4_SINGLE_BACKTEST.md
      </div>
    </div>
  );
}

export type QuantSingleVerdict = {
  has_value: boolean; all_dimensions?: boolean;
  passed_count: number; total_count: number; summary: string;
  checks: { key: string; label: string; passed: boolean | null;
            detail: string }[];
};

export function QuantSingleStockPanel() {
  const [code, setCode] = useState("000001");
  const [name, setName] = useState("");
  const [entry, setEntry] = useState(TEMPLATES[0].entry);
  const [exit, setExit] = useState(TEMPLATES[0].exit);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [cash, setCash] = useState(100_000);
  const [positionPct, setPositionPct] = useState(1.0);
  const [stopLoss, setStopLoss] = useState(8);
  const [takeProfit, setTakeProfit] = useState(0);
  const [maxHold, setMaxHold] = useState(20);
  const [trainRatio, setTrainRatio] = useState(0.7);
  const [tPlus1, setTPlus1] = useState(true);
  const [limits, setLimits] = useState(true);
  const [suspension, setSuspension] = useState(true);
  const [slippage, setSlippage] = useState(5);
  const [commission, setCommission] = useState(0.03);   // %
  const [stampTax, setStampTax] = useState(0.05);        // %
  const [autoSave, setAutoSave] = useState(true);
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [result, setResult] = useState<QuantSingleResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [strategies, setStrategies] = useState<QuantStrategySummary[]>([]);
  const [library, setLibrary] = useState<QuantStrategyList | null>(null);
  const [minExcess, setMinExcess] = useState(10);
  const [saveNote, setSaveNote] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const pollRef = useRef<number | null>(null);

  const refreshStrategies = useCallback(async (threshold?: number) => {
    const bar = threshold ?? minExcess;
    try {
      const data = await api.quantStrategies({ minExcess: bar || undefined });
      setLibrary(data);
      setStrategies(data.strategies);
    } catch {
      /* 策略档案不可用不影响回测本身 */
    }
  }, [minExcess]);

  const reload = useCallback(async (threshold: number) => {
    setMinExcess(threshold);
    await refreshStrategies(threshold);
  }, [refreshStrategies]);

  useEffect(() => {
    void refreshStrategies();
    return () => {
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const poll = useCallback(async (jobId: string) => {
    const job: QuantSingleJob = await api.quantSingleStatus(jobId);
    setStage(job.stage || null);
    if (job.status === "running") {
      pollRef.current = window.setTimeout(() => void poll(jobId), 1500);
      return;
    }
    setRunning(false);
    if (job.status === "done" && job.result) {
      setResult(job.result);
      if (job.result.saved_strategy?.auto_saved) {
        setSaveNote(`已自动存入策略库：${job.result.saved_strategy.name ?? ""}`);
      } else if (job.result.saved_strategy?.reason) {
        setSaveNote(`未自动保存 —— ${job.result.saved_strategy.reason}`);
      } else {
        setSaveNote(null);
      }
      void refreshStrategies();
    } else {
      setError(job.error ?? "回测失败");
    }
  }, [refreshStrategies]);

  const run = async () => {
    setRunning(true);
    setError(null);
    setSaveNote(null);
    setResult(null);
    try {
      const body: QuantSingleRequest = {
        code, entry, exit, start, end, name,
        initial_cash: cash, position_pct: positionPct,
        stop_loss_pct: stopLoss / 100, take_profit_pct: takeProfit / 100,
        max_hold_days: maxHold, train_ratio: trainRatio,
        t_plus_1: tPlus1, respect_price_limits: limits,
        respect_suspension: suspension,
        slippage_bps: slippage,
        commission_rate: commission / 100,
        stamp_tax_rate: stampTax / 100,
        auto_save: autoSave,
      };
      const { job_id } = await api.quantSingleBacktest(body);
      pollRef.current = window.setTimeout(() => void poll(job_id), 1200);
    } catch (exc) {
      setRunning(false);
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const saveNow = async () => {
    if (!result) return;
    setSaveNote(null);
    try {
      const payload = await api.quantSaveStrategy({
        code: result.code, entry: result.config.entry as string,
        exit: (result.config.exit as string) ?? "",
        name: name || `${result.code} 策略`, config: result.config,
        result,
      });
      setSaveNote(`已保存：${payload.strategy.id}（${payload.directory}）`);
      void refreshStrategies();
    } catch (exc) {
      setSaveNote(`保存失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  };

  const removeStrategy = async (id: string) => {
    try {
      await api.quantDeleteStrategy(id);
      void refreshStrategies();
    } catch (exc) {
      setSaveNote(`删除失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  };

  const loadStrategy = (item: QuantStrategySummary) => {
    setCode(item.code);
    setEntry(item.entry_condition ?? item.entry ?? "");
    setExit(item.exit_condition ?? item.exit ?? "");
    setName(item.name);
    setSaveNote(`已载入「${item.name}」—— 点「开始回测」用当前数据重跑一遍`);
  };

  const metrics = result?.metrics;
  const oos = result?.segments?.oos;
  const bhFinal = result?.benchmark?.[result.benchmark.length - 1];
  const strategyFinal = metrics?.final_equity;

  return (
    <div className="quant-single-panel">
      <div className="warn-box">
        单股票多因子条件策略回测（纯本地规则，无 LLM）：用 DSL 条件描述买卖点，
        逐日按 <b>t 日收盘信号 → t+1 日开盘成交</b>撮合，含 T+1、涨停不买/跌停不卖、
        停牌不交易、整手（100 股）、佣金/印花税/过户费/滑点。
        因子<b>按需计算</b>（只算条件里用到的）。
      </div>

      <section className="panel">
        <div className="submit-bar" style={{ flexWrap: "wrap" }}>
          <StockPicker
            value={code}
            disabled={running}
            onChange={setCode}
            onPick={(entry) => {
              if (entry.name && (!name || name.endsWith("策略"))) {
                setName(`${entry.name} 策略`);
              }
            }}
            placeholder="代码 / 拼音首字母 / 中文名"
            width={260}
          />
          <input value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="策略名（保存时用）" disabled={running}
                 style={{ width: 180 }} />
          <input value={start} onChange={(e) => setStart(e.target.value)}
                 placeholder="开始 20260101（空=全部）" disabled={running}
                 style={{ width: 190 }} />
          <input value={end} onChange={(e) => setEnd(e.target.value)}
                 placeholder="结束 空=今天" disabled={running}
                 style={{ width: 150 }} />
          <button className="btn-primary" onClick={run} disabled={running}>
            {running ? "回测中…" : "开始回测"}
          </button>
          {running && <span className="muted-text">{stage}</span>}
        </div>

        <div className="factor-category" style={{ marginTop: 10 }}>
          <div className="factor-category-head">
            <b>买卖条件（DSL，时序模式）</b>
            <button className="btn-ghost tiny" type="button"
                    onClick={() => setHelpOpen(true)}
                    title="查看全部函数、字段、因子与参数说明">
              ? 使用说明书
            </button>
          </div>
          <p className="muted-text" style={{ fontSize: 11 }}>
            时序函数：MA / STD_TS / PCTL_TS / MAX_TS / MIN_TS / ZSCORE_TS /
            REF / DELTA / COUNT_TS；截面函数（RANK/AVG…）在单股票下会被拒绝
            —— 它们会拿未来交易日一起排名。
          </p>
          <div style={{ display: "grid", gap: 8 }}>
            <label className="muted-text">
              入场条件
              <ConditionInput
                value={entry} disabled={running} rows={2}
                topic="single"
                placeholder="例：close > MA(close,20) AND momentum_20 > 0"
                onChange={setEntry}
                onOpenHelp={() => setHelpOpen(true)}
              />
            </label>
            <label className="muted-text">
              出场条件（可留空，只靠止损/止盈/最长持有）
              <ConditionInput
                value={exit} disabled={running} rows={2}
                topic="single"
                placeholder="例：close < MA(close,20) 或 PCTL_TS(close,250) > 80"
                onChange={setExit}
                onOpenHelp={() => setHelpOpen(true)}
              />
            </label>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
            {TEMPLATES.map((item) => (
              <button key={item.label} className="btn-ghost tiny"
                      title={item.hint} disabled={running}
                      onClick={() => { setEntry(item.entry); setExit(item.exit); }}>
                {item.label}
              </button>
            ))}
          </div>
        </div>

        <div className="submit-bar" style={{ flexWrap: "wrap", marginTop: 10 }}>
          <label className="eps-label">
            初始资金
            <input type="number" min={10000} step={10000} value={cash}
                   onChange={(e) => setCash(Number(e.target.value))}
                   style={{ width: 100, marginLeft: 6 }} disabled={running} />
            元
          </label>
          <label className="eps-label">
            仓位
            <input type="number" min={5} max={100} step={5}
                   value={positionPct * 100}
                   onChange={(e) => setPositionPct(Number(e.target.value) / 100)}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
            %
          </label>
          <label className="eps-label">
            止损
            <input type="number" min={0} max={50} step={1} value={stopLoss}
                   onChange={(e) => setStopLoss(Number(e.target.value))}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
            %（0=不启用）
          </label>
          <label className="eps-label">
            止盈
            <input type="number" min={0} max={200} step={5} value={takeProfit}
                   onChange={(e) => setTakeProfit(Number(e.target.value))}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
            %
          </label>
          <label className="eps-label">
            最长持有
            <input type="number" min={0} max={500} step={1} value={maxHold}
                   onChange={(e) => setMaxHold(Number(e.target.value))}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
            交易日
          </label>
        </div>

        <div className="submit-bar" style={{ flexWrap: "wrap" }}>
          <label className="eps-label">
            滑点
            <input type="number" min={0} max={100} step={1} value={slippage}
                   onChange={(e) => setSlippage(Number(e.target.value))}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
            bp
          </label>
          <label className="eps-label">
            佣金
            <input type="number" min={0} max={1} step={0.01} value={commission}
                   onChange={(e) => setCommission(Number(e.target.value))}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
            %
          </label>
          <label className="eps-label">
            印花税(卖出)
            <input type="number" min={0} max={1} step={0.01} value={stampTax}
                   onChange={(e) => setStampTax(Number(e.target.value))}
                   style={{ width: 60, marginLeft: 6 }} disabled={running} />
            %
          </label>
          <label className="eps-label">
            训练集占比
            <input type="number" min={0.3} max={0.9} step={0.05}
                   value={trainRatio}
                   onChange={(e) => setTrainRatio(Number(e.target.value))}
                   style={{ width: 70, marginLeft: 6 }} disabled={running} />
          </label>
          <label className="muted-text">
            <input type="checkbox" checked={tPlus1}
                   onChange={(e) => setTPlus1(e.target.checked)}
                   disabled={running} /> T+1
          </label>
          <label className="muted-text">
            <input type="checkbox" checked={limits}
                   onChange={(e) => setLimits(e.target.checked)}
                   disabled={running} /> 涨跌停限制
          </label>
          <label className="muted-text">
            <input type="checkbox" checked={suspension}
                   onChange={(e) => setSuspension(e.target.checked)}
                   disabled={running} /> 停牌不交易
          </label>
          <label className="muted-text"
                 title="达标（样本外为正、交易笔数足够、自检通过）时自动存入策略库">
            <input type="checkbox" checked={autoSave}
                   onChange={(e) => setAutoSave(e.target.checked)}
                   disabled={running} /> 达标自动保存
          </label>
        </div>

        {error && <div className="error-text" style={{ marginTop: 10 }}>{error}</div>}
      </section>

      {result && metrics && (
        <section className="panel">
          <div className="panel-head">
            <h2>
              {result.name || result.code} 回测结果
            </h2>
            <span className="muted-text">
              面板装配 + 回测 {result.elapsed_seconds}s · 峰值内存{" "}
              {result.peak_memory_mb} MB · 因子来源{" "}
              {result.panel_origins?.length ?? 0} 项
            </span>
          </div>

          {result.warnings.length > 0 && (
            <div className="warn-box">
              {result.warnings.map((item) => <div key={item}>⚠ {item}</div>)}
            </div>
          )}
          <VerdictBlock result={result} />
          {saveNote && <div className="muted-text">{saveNote}</div>}

          <div className="metric-grid">
            <div className="metric-card">
              <span className="muted-text">全样本收益</span>
              <b>{pct(metrics.total_return_pct)}</b>
            </div>
            <div className="metric-card">
              <span className="muted-text">
                近一年收益（{result.segments.recent?.trading_days ?? "—"} 天）
              </span>
              <b>{pct(result.segments.recent?.return_pct)}</b>
              <span className="muted-text">
                基准 {pct(result.segments.recent?.benchmark_return_pct)} ·
                超额 {pct(result.segments.recent?.excess_vs_benchmark_pct)}
              </span>
            </div>
            <div className="metric-card">
              <span className="muted-text">样本外收益</span>
              <b>{pct(oos?.return_pct)}</b>
              <span className="muted-text">
                {oos ? `${oos.trades} 笔` : "—"}
              </span>
            </div>
            <div className="metric-card">
              <span className="muted-text">最大回撤</span>
              <b>{pct(metrics.max_drawdown_pct)}</b>
              <span className="muted-text">
                基准 {pct(metrics.benchmark_max_drawdown_pct)}
              </span>
            </div>
            <div className="metric-card">
              <span className="muted-text">夏普 / Calmar</span>
              <b>{num(metrics.sharpe)} / {num(metrics.calmar)}</b>
            </div>
            <div className="metric-card">
              <span className="muted-text">交易笔数</span>
              <b>{metrics.trade_count}</b>
            </div>
            <div className="metric-card">
              <span className="muted-text">胜率</span>
              <b>{pct(metrics.win_rate_pct, 1)}</b>
            </div>
            <div className="metric-card">
              <span className="muted-text">盈亏比</span>
              <b>{num(metrics.profit_factor)}</b>
            </div>
            <div className="metric-card">
              <span className="muted-text">持仓占比</span>
              <b>{pct(metrics.exposure_pct, 1)}</b>
            </div>
            <div className="metric-card">
              <span className="muted-text">交易成本</span>
              <b>{metrics.total_cost.toLocaleString()}</b>
            </div>
          </div>

          <p className="muted-text">
            期末：策略 {strategyFinal?.toLocaleString()} 元 · 买入持有{" "}
            {bhFinal?.toLocaleString()} 元 · 指数{" "}
            {metrics.index_final?.toLocaleString()} 元
            {" "}（同一区间、同样含费用；指数按点位计，实盘需用 ETF 且会有跟踪偏差）
          </p>

          <EquityChart result={result} />

          <div className="submit-bar" style={{ marginTop: 10 }}>
            <button className="btn-ghost" onClick={saveNow}>
              一键保存到策略库
            </button>
            <span className="muted-text">
              保存的是完整参数 + 当时的指标快照（含样本外与自检提示），
              同一套参数重复保存只更新不新建
            </span>
          </div>

          {result.trades.length > 0 && (
            <>
              <h3>成交明细（最近 60 笔）</h3>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>买入</th><th>价格</th><th>股数</th><th>卖出</th>
                    <th>价格</th><th>持有</th><th>盈亏</th><th>收益</th><th>原因</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.slice(-60).reverse().map((trade, index) => (
                    <tr key={`${trade.entry_date}-${trade.exit_date}-${index}`}>
                      <td className="mono">{trade.entry_date}</td>
                      <td className="mono">{trade.entry_price}</td>
                      <td className="mono">{trade.shares}</td>
                      <td className="mono">{trade.exit_date}</td>
                      <td className="mono">{trade.exit_price}</td>
                      <td className="mono">{trade.hold_days}天</td>
                      <td className="mono"
                          style={{ color: trade.pnl >= 0
                            ? "var(--low)" : "var(--high)" }}>
                        {trade.pnl.toLocaleString()}
                      </td>
                      <td className="mono">{pct(trade.return_pct)}</td>
                      <td className="muted-text">{trade.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <details style={{ marginTop: 12 }}>
            <summary className="muted-text">撮合口径与说明</summary>
            <ul className="health-notes">
              {result.notes.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </details>
        </section>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>策略档案（{strategies.length}）</h2>
          <span className="muted-text">
            {library
              ? (library.source.startsWith("db")
                ? `数据库 ${library.stats.dialect} · 表 ${library.stats.table}`
                : "文件档案（数据库不可用）")
              : "档案未加载"}
            {library?.stats.best_recent_excess_pct !== undefined
              && library?.stats.best_recent_excess_pct !== null && (
              <> · 最优近一年超额 {library.stats.best_recent_excess_pct}pp</>
            )}
          </span>
        </div>

        <div className="submit-bar" style={{ flexWrap: "wrap" }}>
          <label className="muted-text">
            近一年超额门槛 ≥
            <input type="number" step={1} min={0} value={minExcess}
                   onChange={(e) => setMinExcess(Number(e.target.value))}
                   style={{ width: 70, marginLeft: 6 }} />
            pp
          </label>
          <button className="btn-ghost tiny" onClick={() => void reload(10)}>
            只看跑赢 10pp+
          </button>
          <button className="btn-ghost tiny" onClick={() => void reload(0)}>
            显示全部
          </button>
          <span className="muted-text">
            共 {strategies.length} 条 · 门槛生效于服务端（跨策略查询）
          </span>
        </div>

        {library?.strategies?.some(
          (item: QuantStrategySummary) => item.search_space) && (
          <div className="warn-box" style={{ fontSize: 11 }}>
            这批策略是从{" "}
            <b>{library.strategies[0]?.search_space}</b> 个「策略 × 标的」组合里
            按近一年超额选出来的；同批全体的中位超额是{" "}
            <b>{library.strategies[0]?.scan_median_excess_pct}pp</b>、
            跑赢比例仅 <b>
              {((library.strategies[0]?.scan_beat_ratio ?? 0) * 100).toFixed(0)}%
            </b>
            。也就是说：<b>这类策略的典型结果是跑输</b>，下面是筛选后的尾部。
            标了「低样本」的只有 3~5 笔交易，单笔运气就能主导结论。
          </div>
        )}

        {strategies.length === 0 ? (
          <p className="muted-text">
            还没有达标策略。跑一次回测点「一键保存」，或用扫描器批量找：
            <code>
              python scripts/quant_strategy_scan.py --preset all --codes 30 --min-excess 10
            </code>
          </p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>策略</th><th>标的</th><th>近一年</th><th>基准</th>
                <th>超额</th><th>笔数</th><th>回撤</th><th>夏普</th><th></th>
              </tr>
            </thead>
            <tbody>
              {(library?.strategies ?? strategies).map(
                (item: QuantStrategySummary) => {
                const excess = item.recent_excess_pct;
                const entry = item.entry_condition ?? item.entry ?? "";
                const exitText = item.exit_condition ?? item.exit ?? "";
                return (
                  <tr key={item.id}>
                    <td>
                      <b>{item.name}</b>
                      {item.low_sample ? (
                        <span className="badge run-failed" title="只有几笔交易，统计上不可靠">
                          低样本
                        </span>
                      ) : null}
                      {excess !== null && excess !== undefined && excess > 0 && (
                        <span className="badge run-success">
                          跑赢 {excess.toFixed(1)}pp
                        </span>
                      )}
                      <div className="muted-text mono" style={{ fontSize: 11 }}>
                        {entry}
                        {exitText ? `  ⏹ ${exitText}` : ""}
                      </div>
                    </td>
                    <td className="mono">{item.code}</td>
                    <td className="mono">{pct(item.recent_return_pct)}</td>
                    <td className="mono muted-text">
                      {pct(item.recent_benchmark_pct)}
                    </td>
                    <td className="mono"
                        style={{ color: (excess ?? 0) > 0
                          ? "var(--low)" : "var(--high)" }}>
                      {pct(excess)}
                    </td>
                    <td className="mono">{item.recent_trades ?? item.trade_count ?? "—"}</td>
                    <td className="mono">{pct(item.max_drawdown_pct)}</td>
                    <td className="mono">{num(item.sharpe)}</td>
                    <td>
                      <button className="btn-ghost tiny"
                              onClick={() => loadStrategy(item)}>载入</button>
                      <button className="btn-ghost tiny"
                              onClick={() => void removeStrategy(item.id)}>删除</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <p className="muted-text" style={{ fontSize: 11 }}>
          {library?.disclaimer ?? ""}
        </p>
      </section>

      <StrategyCasesPanel
        title="开源策略库"
        note="每天可从 GitHub / arXiv / CSDN 增量抓取；仅为线索，未经本项目验证"
        defaultThemes={["动量/趋势", "因子研究", "回测方法论", "统计套利"]}
      />

      {helpOpen && (
        <QuantHelpModal topic="single" onClose={() => setHelpOpen(false)} />
      )}
    </div>
  );
}
