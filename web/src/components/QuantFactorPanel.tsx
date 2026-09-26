import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  QuantDataStatus,
  QuantFactorList,
  QuantScreenResult,
} from "../api";
import { QuantHelpModal } from "./QuantHelpModal";
import { StrategyCasesPanel } from "./StrategyCasesPanel";

const pct = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
const fixed = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : v.toFixed(digits);
const signed = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;

const CATEGORY_TONE: Record<string, string> = {
  value: "var(--accent)",
  growth: "var(--medium)",
  quality: "var(--low)",
  momentum: "var(--high)",
  volatility: "var(--muted)",
  liquidity: "#8b5cf6",
  size: "#0ea5e9",
};

/** IC 表的横向条形图：一眼看出哪些因子有效（|ICIR| 越长越稳）。 */
function IcirBars({ rows }: { rows: QuantScreenResult["ic_table"] }) {
  const data = rows.filter((row) => row.ICIR !== null).slice(0, 18);
  if (!data.length) return <p className="muted-text">暂无 ICIR 结果</p>;
  const maxAbs = Math.max(...data.map((row) => Math.abs(row.ICIR ?? 0)), 0.1);
  return (
    <div className="icir-bars">
      {data.map((row) => {
        const value = row.ICIR ?? 0;
        const width = (Math.abs(value) / maxAbs) * 48;
        const positive = value >= 0;
        return (
          <div className="icir-row" key={row.factor}>
            <span className="icir-label mono" title={row.label}>
              {row.factor}
            </span>
            <span className="icir-track">
              <span
                className="icir-fill"
                style={{
                  width: `${width}%`,
                  left: positive ? "50%" : `${50 - width}%`,
                  background: positive ? "var(--low)" : "var(--high)",
                }}
              />
              <span className="icir-axis" />
            </span>
            <span className="icir-value mono">{signed(value)}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 净值/多空曲线（分层回测的分组年化收益柱状）。 */
function GroupBars({ groups }: { groups: QuantScreenResult["quantile"]["group_stats"] }) {
  if (!groups?.length) return null;
  const values = groups.map((g) => Number(g.annual_return ?? 0));
  const maxAbs = Math.max(...values.map(Math.abs), 0.01);
  return (
    <div className="group-bars">
      {groups.map((group, index) => {
        const value = values[index];
        const height = (Math.abs(value) / maxAbs) * 100;
        return (
          <div className="group-bar" key={String(group.index)}>
            <span className="group-bar-value mono">{pct(value, 1)}</span>
            <span
              className="group-bar-fill"
              style={{
                height: `${height}%`,
                background: value >= 0 ? "var(--low)" : "var(--high)",
              }}
            />
            <span className="muted-text">第{Number(group.index) + 1}组</span>
          </div>
        );
      })}
    </div>
  );
}

/** 判断服务进程是不是旧版本（响应里缺少本会话新增的仓库字段）。 */
function isStaleServer(status: QuantDataStatus): boolean {
  return status.warehouse === undefined;
}

/** `20260104` → `2026-01-04`（数据状态接口返回的是紧凑日期）。 */
const fmtDay = (value: string | undefined) =>
  value && value.length === 8
    ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6)}`
    : value ?? "";

export default function QuantFactorPanel({
  onSwitchToSingle,
}: {
  /** 切到「单股票策略回测」（由 BacktestPanel 注入；缺省时只显示提示文案）。 */
  onSwitchToSingle?: () => void;
}) {
  const [library, setLibrary] = useState<QuantFactorList | null>(null);
  const [status, setStatus] = useState<QuantDataStatus | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  // 默认 2024-01-01：本地数据已回补到 2006 年，而样本外可信度取决于区间长度 ——
  // 只筛 2026 年时样本外仅 3 个非重叠持有期（年化是噪声）；2024 起约 663 个
  // 交易日（实测 7.6 分钟 / 峰值 5.4 GB / 样本外约 10 期，面板已按需装配），
  // 是默认 8 GB 内存预算下的稳妥选择。再往前会被服务端的内存护栏拦下。
  const [start, setStart] = useState("2024-01-01");
  const [end, setEnd] = useState("");
  const [horizon, setHorizon] = useState(20);
  const [minIc, setMinIc] = useState(0.02);
  const [minIcir, setMinIcir] = useState(0.3);
  const [corrThreshold, setCorrThreshold] = useState(0.7);
  const [trainRatio, setTrainRatio] = useState(0.7);
  const [targetCount, setTargetCount] = useState(20);
  const [neutralize, setNeutralize] = useState(true);
  const [excludeSt, setExcludeSt] = useState(false);
  const [liquidityFilter, setLiquidityFilter] = useState(false);
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [result, setResult] = useState<QuantScreenResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [factors, dataStatus] = await Promise.all([
        api.quantFactors(),
        api.quantDataStatus(),
      ]);
      setLibrary(factors);
      setStatus(dataStatus);
    } catch (exc) {
      setError(`因子库加载失败：${String(exc)}`);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, [load]);

  const toggle = (key: string) =>
    setSelected((prev) =>
      prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key],
    );

  const toggleCategory = (keys: string[], on: boolean) =>
    setSelected((prev) =>
      on
        ? Array.from(new Set([...prev, ...keys]))
        : prev.filter((key) => !keys.includes(key)),
    );

  const runScreen = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    setStage("提交任务…");
    try {
      const { job_id } = await api.quantScreen({
        start,
        end,
        factors: selected,
        horizon,
        min_ic: minIc,
        min_icir: minIcir,
        corr_threshold: corrThreshold,
        train_ratio: trainRatio,
        target_count: targetCount,
        neutralize,
        exclude_st: excludeSt,
        liquidity_filter: liquidityFilter,
      });
      const tick = async () => {
        try {
          const state = await api.quantScreenStatus(job_id);
          setStage(`${state.stage}（${state.elapsed}s）`);
          if (state.status === "done") {
            setResult(state.result ?? null);
            setRunning(false);
            if (pollRef.current !== null) window.clearInterval(pollRef.current);
            pollRef.current = null;
          } else if (state.status === "failed") {
            setError(state.error ?? "筛选失败");
            setRunning(false);
            if (pollRef.current !== null) window.clearInterval(pollRef.current);
            pollRef.current = null;
          }
        } catch (exc) {
          setError(`轮询失败：${String(exc)}`);
          setRunning(false);
        }
      };
      pollRef.current = window.setInterval(() => void tick(), 1500);
      await tick();
    } catch (exc) {
      setError(`筛选启动失败：${String(exc)}`);
      setRunning(false);
    }
  };

  // 本地到底有多少历史，直接写在参数边上。
  // 实测的误解来源：数据其实已回补到 2006 年（仓库 8573 万行），
  // 但默认区间是 2026-01-01 起 —— 用户只看到 171 个交易日的结论，
  // 会以为"这个功能没意义"。把可用区间摊开，误判就没了。
  const coverage = status?.datasets.find((item) => item.dataset === "daily_basic");

  return (
    <div className="quant-panel">
      {/*
        这个页面是**截面研究**页，不是"给某只票做回测"。
        实测用户的原话："每个因子不应该设置参数、在指定的个股上去回测吗？"——
        所以把"本页做什么 / 不做什么 / 看个股去哪"写在最上面，
        而不是只藏在「使用说明书」里。
      */}
      <div className="quant-scope">
        <div>
          <b>本页是「横截面因子研究」，不是个股回测。</b>
          <span className="muted-text">
            它把全市场 5000 多只票按因子<b>逐日排序分组</b>，回答"哪些因子有效、
            哪些因子彼此重复、合成后分组是否单调"。
            它<b>不给任何一只个股的买卖点</b>，也不预测涨跌。
          </span>
        </div>
        <div className="quant-scope-next">
          <span className="muted-text">
            要看某只票怎么买卖（入场/出场条件、止损止盈、仓位、T+1、涨跌停、交易成本）
          </span>
          {onSwitchToSingle ? (
            <button className="btn-ghost tiny" type="button"
                    onClick={onSwitchToSingle}
                    title="指定一只票，用因子条件写进出场规则，做时序回测">
              → 切到「单股票策略回测」
            </button>
          ) : (
            <span className="muted-text">→ 请用上方「单股票策略回测」标签页</span>
          )}
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>多因子库（{library?.count ?? 0} 个）</h2>
          <span className="muted-text">
            七大类 · 全部按公告日对齐（PIT）· 无未来函数
          </span>
          <button className="btn-ghost tiny" type="button"
                  onClick={() => setHelpOpen(true)}
                  title="这个页面怎么用、结果怎么读、数据为 0 时怎么排查">
            ? 使用说明书
          </button>
        </div>

        {/*
          "本地数据 0 万行" 的自诊断。
          实测用户遇到过：数据其实全在（8579 万行），但页面显示 0 ——
          因为 web 服务进程还是修复前启动的旧代码，响应里连 `warehouse`
          字段都没有。以前这里只会静静显示 0，让人以为数据没下载；
          现在直接把"为什么是 0、怎么修"写在界面上。
        */}
        {status && isStaleServer(status) && (
          <div className="warn-box">
            <b>⚠ 本地数据读不出来，但**不是**数据没下载 —— 是服务进程太旧。</b>
            <div style={{ marginTop: 4 }}>
              这个响应里没有「仓库」字段，说明当前跑的服务是修复之前启动的版本。
              数据本身可以在命令行核对：
              <code>uv run python scripts/quant_warehouse.py status</code>
              （应有约 8550 万行）与
              <code>uv run python -m src.quant.dataset_store</code>。
            </div>
            <div style={{ marginTop: 4 }}>
              修复方式：重启服务
              <code>C:\veighna_studio\python.exe manage.py start --replace</code>
            </div>
          </div>
        )}

        {status && (
          <div className={`quant-status ${status.ready ? "" : "warn"}`}>
            <span>
              本地数据：{status.datasets.length} 个数据集 /{" "}
              {(status.total_rows / 10000).toFixed(0)} 万行
            </span>
            {status.datasets
              .filter((item) => ["daily", "daily_basic", "fina_indicator_vip"].includes(item.dataset))
              .map((item) => (
                <span key={item.dataset} className="mono">
                  {item.dataset} {item.partitions}期 {item.first}~{item.last}
                </span>
              ))}
            {status.warehouse?.available ? (
              <span className="mono" title={status.warehouse.description}>
                仓库 {status.warehouse.dialect} ·{" "}
                {(status.warehouse.total_rows / 10000).toFixed(0)} 万行 ·{" "}
                {status.warehouse.tables.length} 张表（回测优先读这里）
              </span>
            ) : (
              <span className="mono muted-text" title={status.warehouse?.error}>
                仓库未启用（回测走 CSV 分区）
              </span>
            )}
            {!status.ready && <span className="error-text">{status.hint}</span>}
          </div>
        )}

        {library?.categories.map((category) => {
          const keys = category.factors.map((factor) => factor.key);
          const allOn = keys.every((key) => selected.includes(key));
          return (
            <div className="factor-category" key={category.key}>
              <div className="factor-category-head">
                <span
                  className="factor-category-dot"
                  style={{ background: CATEGORY_TONE[category.key] ?? "var(--muted)" }}
                />
                <b>{category.label}</b>
                <span className="muted-text">{category.count} 个</span>
                <button className="btn-ghost tiny"
                        onClick={() => toggleCategory(keys, !allOn)}>
                  {allOn ? "取消全选" : "全选"}
                </button>
              </div>
              <div className="factor-grid">
                {category.factors.map((factor) => (
                  <label
                    key={factor.key}
                    className={`factor-chip ${selected.includes(factor.key) ? "on" : ""}`}
                    title={`${factor.formula}${factor.direction < 0 ? "（已取反，值越大越看好）" : ""}`}
                  >
                    <input
                      type="checkbox"
                      checked={selected.includes(factor.key)}
                      onChange={() => toggle(factor.key)}
                    />
                    <span className="factor-chip-label">{factor.label}</span>
                    <span className="muted-text mono">{factor.key}</span>
                    {factor.direction < 0 && <span className="factor-rev">↺</span>}
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>因子筛选与分层回测</h2>
          <span className="muted-text">
            中性化 → IC/ICIR → 相关性去重 → <b>样本外复核</b>
          </span>
        </div>

        {/*
          参数分两组显示，因为它们是**两种东西**（这是第二个误读来源）：
          ① 样本区间：用哪一段历史做检验；
          ② 检验口径：多严格才算"有效因子"—— 它**不改因子怎么算**。
          因子自身的参数（20 日动量 / 60 日波动 / 120 日动量）写在因子定义里，
          是不可调、也不做网格寻优的（否则 35 个因子一起调参＝过拟合机器）。
        */}
        <div className="quant-controls-group">
          <div className="quant-controls-title">
            样本区间
            <span className="muted-text">
              {coverage
                ? `本地可用 ${fmtDay(coverage.first)} ~ ${fmtDay(coverage.last)}（${coverage.partitions} 个交易日）——区间越长样本外越可信；实测 663 个交易日约 7.6 分钟 / 5.4 GB，默认 8 GB 内存预算下约 4 年封顶`
                : "决定用哪一段历史做检验（越长越可信）"}
            </span>
          </div>
          <div className="quant-controls">
            <label title="筛选用的起始日期；本地数据已回补至 2006 年，不必只填今年">
              开始
              <input value={start} onChange={(e) => setStart(e.target.value)}
                     placeholder="2024-01-01" />
            </label>
            <label title="留空 = 到今天">
              结束（空=今天）
              <input value={end} onChange={(e) => setEnd(e.target.value)}
                     placeholder="2026-09-15" />
            </label>
          </div>
        </div>

        <div className="quant-controls-group">
          <div className="quant-controls-title">
            检验口径
            <span className="muted-text">
              只决定"多严格算有效"，<b>不改变因子怎么算</b>；因子自身的参数
              （如 momentum_20 的 20 日）写死在因子定义里，不可调、也不做参数寻优
            </span>
          </div>
          <div className="quant-controls">
            <label title="算 IC 时看未来多少个交易日的收益（≈1 个月）；也决定分层回测的持有期">
              IC 前瞻（交易日）
              <input type="number" min={1} max={120} value={horizon}
                     onChange={(e) => setHorizon(Number(e.target.value))} />
            </label>
            <label title="因子值与未来收益的截面相关性下限；低于它视为无效因子">
              |IC| 门槛
              <input type="number" step={0.01} min={0} value={minIc}
                     onChange={(e) => setMinIc(Number(e.target.value))} />
            </label>
            <label title="IC 均值/IC 标准差的下限，衡量稳定性；|ICIR| < 0.3 通常不值得用">
              |ICIR| 门槛
              <input type="number" step={0.1} min={0} value={minIcir}
                     onChange={(e) => setMinIcir(Number(e.target.value))} />
            </label>
            <label title="两个因子平均秩相关超过它就算同一簇，每簇只留 |ICIR| 最高的一个">
              相关性阈值 ρ
              <input type="number" step={0.05} min={0} max={1} value={corrThreshold}
                     onChange={(e) => setCorrThreshold(Number(e.target.value))} />
            </label>
            <label title="前 N% 交易日用于挑因子，后 (1-N)% 只用于检验（walk-forward）">
              训练集比例
              <input type="number" step={0.05} min={0.1} max={0.9} value={trainRatio}
                     onChange={(e) => setTrainRatio(Number(e.target.value))} />
            </label>
            <label title="去重后最多保留多少个因子（按 |ICIR| 截断）">
              目标因子数
              <input type="number" min={1} max={35} value={targetCount}
                     onChange={(e) => setTargetCount(Number(e.target.value))} />
            </label>
            <label className="inline"
                   title="剔除 ST/*ST（按**当时的历史名称**判定，不是今天的名字）：ST 涨跌停 5%、退市风险，且会污染基本面因子。默认关闭 —— 它会改变截面构成">
              <input type="checkbox" checked={excludeSt}
                     onChange={(e) => setExcludeSt(e.target.checked)} />
              剔除 ST
            </label>
            <label className="inline"
                   title="股票池过滤：按**过去 20 日平均成交额**排序，每日剔除最差的 30%（业界常规做法）。默认关闭 —— 它会改变截面构成。打开后僵尸股不再进截面，面板的列也同比例变少，长区间才跑得动">
              <input type="checkbox" checked={liquidityFilter}
                     onChange={(e) => setLiquidityFilter(e.target.checked)} />
              剔除流动性最差 30%
            </label>
            <label className="inline"
                   title="剔除市值这个共同暴露；开了之后规模类因子与它共线，其 IC 仅供参考">
              <input type="checkbox" checked={neutralize}
                     onChange={(e) => setNeutralize(e.target.checked)} />
              市值中性化
            </label>
          </div>
        </div>

        <div className="quant-controls">
          <button onClick={() => void runScreen()} disabled={running}
                  title={
                    "点一次 = 跑完整条流水线（子进程执行，约 1~4 分钟）：\n"
                    + "① 中性化（去极值→市值中性化→标准化）\n"
                    + "② 训练集 IC/ICIR  ③ 相关性聚类去重  ④ 门槛过滤\n"
                    + "⑤ 样本外复核（后 30% 只用于检验）  ⑥ 分层回测\n\n"
                    + "产出三样东西：\n"
                    + "· 过门槛的因子清单（35 个里剩几个）\n"
                    + "· 去重淘汰明细（谁被谁替代、因为 |ρ| 多少）\n"
                    + "· 样本内 → 样本外的 |ICIR| 衰减幅度\n\n"
                    + "它不产生个股买卖点，也不代表可交易收益。"
                  }>
            {running ? "计算中…" : `开始筛选（${selected.length || library?.count || 35} 个因子）`}
          </button>
          {running && stage && <span className="loading-pill">{stage}</span>}
        </div>

        {error && <div className="error-box">{error}</div>}

        {result && (
          <>
            <div className="quant-summary">
              <span>
                样本：{result.start}~{result.end}（{result.trading_days} 个交易日 /{" "}
                {result.universe_size} 只股票）
              </span>
              <span>
                选中 <b>{result.selected.length}</b> 个因子：
                <span className="mono">{result.selected.join(", ") || "无"}</span>
              </span>
            </div>

            {result.notes.length > 0 && (
              <ul className="quant-notes">
                {result.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}

            <h3>IC / ICIR（含样本外复核）</h3>
            <div className="quant-tables">
              <div>
                <h4 className="muted-text">样本内（前 {Math.round(trainRatio * 100)}%）</h4>
                <IcirBars rows={result.ic_table} />
              </div>
              <div>
                <h4 className="muted-text">样本外（后 {Math.round((1 - trainRatio) * 100)}%）</h4>
                {result.oos_table.length ? (
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>因子</th>
                        <th>IC</th>
                        <th>ICIR</th>
                        <th>t</th>
                        <th>期数</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.oos_table.map((row) => (
                        <tr key={row.factor}>
                          <td className="mono">{row.factor}</td>
                          <td className="mono">{signed(row.IC, 4)}</td>
                          <td className="mono">{signed(row.ICIR)}</td>
                          <td className="mono">{signed(row.t, 1)}</td>
                          <td className="mono">{row.periods}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="muted-text">样本外无因子通过门槛</p>
                )}
              </div>
            </div>

            {(result.quantile?.group_stats?.length ?? 0) > 0 && (
              <>
                <h3>分层回测（等权合成，{result.quantile.n_groups} 组）</h3>
                <div className="quant-summary">
                  <span>多空年化 <b>{pct(result.quantile.hl_return)}</b></span>
                  <span>多空夏普 <b>{fixed(result.quantile.hl_sharpe)}</b></span>
                  <span>多空最大回撤 <b>{pct(result.quantile.hl_max_dd)}</b></span>
                </div>
                <GroupBars groups={result.quantile.group_stats} />
              </>
            )}

            {result.dropped.length > 0 && (
              <>
                <h3>去重与淘汰明细</h3>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>因子</th>
                      <th>保留者</th>
                      <th>原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.dropped.map((row) => (
                      <tr key={`${row.factor}-${row.reason}`}>
                        <td className="mono">{row.factor}</td>
                        <td className="mono">{row.kept_by || "—"}</td>
                        <td className="muted-text">{row.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            <p className="muted-text quant-disclaimer">{result.disclaimer}</p>
          </>
        )}
      </section>

      <StrategyCasesPanel
        title="最新开源回测策略"
        note="每周可从 GitHub / arXiv / CSDN 增量抓取因子筛选与分层回测类案例"
        defaultThemes={["因子研究", "回测方法论", "组合优化", "动量/趋势"]}
      />

      {helpOpen && (
        <QuantHelpModal topic="factors" onClose={() => setHelpOpen(false)} />
      )}
    </div>
  );
}
