import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import MainlineAlertReturns from "./MainlineAlertReturns";
import {
  formatDate,
  formatPct,
  formatScore,
  mainlineApi,
  resolveTaskState,
  toneOf,
  type BacktestFold,
  type BacktestMetrics,
  type BacktestRunSummary,
  type BacktestScene,
  type BacktestTarget,
  type MainlineBacktestReport,
} from "../mainlineApi";

/**
 * 主线挖掘 · 回测与报告视图。
 *
 * ## 这个视图要回答的唯一问题："这套评分到底能不能用"
 *
 * 所以版面的顺序是**从结论到证据**：
 * ① 指标卡（一眼看达标情况）→ ② 分场景验证（历史上真的行情有没有提前抓到）
 * → ③ 因子相关性（是不是几个因子在说同一件事、分数被虚高）
 * → ④ 回测历史（这套参数是变好了还是变差了）→ ⑤ 报告正文（细节与口径）。
 *
 * ## 指标卡上的 ✅/⚠️ 来自后端 targets，不是前端拍的阈值
 *
 * `metrics.targets` 里每项都带 `{value, target, passed}` —— 达标线属于**策略口径**，
 * 只能由后端定义（改了阈值不该动前端）。前端只负责把 `passed` 画成 ✅/⚠️，
 * 并在 `target` 缺失时**不显示达标标记**（宁可不表态，也不编一条线出来）。
 *
 * ## 为什么 markdown 自己解析而不是用 marked
 *
 * 项目里虽然装了 `marked`，但它是给投研报告主视图用的通用渲染器。这里的报告是后端
 * 用固定模板拼的（标题 + 表格 + 列表），只需要三种块级元素；自己解析可以**逐行拿到
 * 结构**（比如把"未达标"行高亮、给表格单元格加涨跌色），通用渲染器反而要再做一次
 * DOM 后处理。取舍：解析器只认它认识的语法，认不出的行原样输出成段落 —— 宁可不渲染，
 * 也不要因为不认识就把内容吞掉。
 */

/** 指标卡定义：key 对应 `metrics.targets` 的键，取值器从 metrics 里读值。 */
const CARDS: {
  key: string;
  label: string;
  pick: (metrics: BacktestMetrics) => number | null;
  /** 展示格式：percent = 已经是百分数；ratio = 小数要 ×100；plain = 原值 */
  kind: "percent" | "ratio" | "plain";
  hint: string;
}[] = [
  { key: "ic_mean", label: "IC 均值", pick: (m) => m.ic_mean, kind: "plain",
    hint: "每日评分与次日收益的秩相关均值；> 0.05 通常算有效" },
  { key: "icir", label: "ICIR", pick: (m) => m.icir, kind: "plain",
    hint: "IC 均值 / IC 标准差，衡量选股能力的稳定性；> 0.3 算稳" },
  { key: "ic_win_rate", label: "IC 胜率", pick: (m) => m.ic_win_rate,
    kind: "ratio", hint: "IC 为正的交易日占比" },
  { key: "long_short_annual", label: "多空年化", pick: (m) => m.long_short_annual,
    kind: "ratio", hint: "多头组合减空头组合的年化收益" },
  { key: "long_excess", label: "多头超额", pick: (m) => m.long_excess,
    kind: "ratio", hint: "多头组合相对基准的年化超额" },
  { key: "signal_hit_rate", label: "信号兑现率", pick: (m) => m.signal_hit_rate,
    kind: "ratio", hint: "告警后确实上涨的比例" },
  { key: "false_positive_rate", label: "假阳性率", pick: (m) => m.false_positive_rate,
    kind: "ratio", hint: "告警后未兑现的比例（越低越好）" },
  { key: "avg_max_gain", label: "平均最大涨幅", pick: (m) => m.avg_max_gain,
    kind: "ratio", hint: "告警后区间最大涨幅的平均值（理论可捕获空间）" },
];

/** 目标值 → 展示文本（口径跟数值本身一致）。 */
function targetText(target: BacktestTarget | null | undefined,
                    kind: "percent" | "ratio" | "plain"): string {
  if (!target || target.target === null || target.target === undefined
      || !Number.isFinite(target.target)) return "";
  const value = target.target;
  if (kind === "ratio") return `${(value * 100).toFixed(1)}%`;
  if (kind === "percent") return `${value.toFixed(1)}%`;
  return String(value);
}

/** 指标值 → 展示文本。 */
function valueText(value: number | null, kind: "percent" | "ratio" | "plain"): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (kind === "ratio") return `${(value * 100).toFixed(2)}%`;
  if (kind === "percent") return `${value.toFixed(2)}%`;
  return value.toFixed(3);
}

type Props = {
  /** 点信号明细里的板块名 → 打开下钻（由 MainlinePanel 提供） */
  onPickBoard?: (code: string, name: string) => void;
};

export default function MainlineBacktest({ onPickBoard }: Props) {
  const [report, setReport] = useState<MainlineBacktestReport | null>(null);
  const [runs, setRuns] = useState<BacktestRunSummary[]>([]);
  const [runId, setRunId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showMarkdown, setShowMarkdown] = useState(false);
  /** 正在跑的回测任务（首次回测没有任何入口，只能在这里触发） */
  const [task, setTask] = useState<{ id: string; state: string;
                                    message: string; progress: string } | null>(null);
  const taskPollRef = useRef<number | null>(null);

  const load = useCallback(async (id: string) => {
    setLoading(true);
    setError("");
    try {
      setReport(await mainlineApi.backtest(id));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      const payload = await mainlineApi.backtestList(20);
      setRuns(payload.runs ?? []);
    } catch {
      // 历史列表失败不阻断主报告的展示（列表只是导航，报告才是内容）
      setRuns([]);
    }
  }, []);

  useEffect(() => { void load(runId); }, [load, runId]);
  useEffect(() => { void loadRuns(); }, [loadRuns]);

  const stopTaskPoll = useCallback(() => {
    if (taskPollRef.current !== null) {
      window.clearInterval(taskPollRef.current);
      taskPollRef.current = null;
    }
  }, []);
  useEffect(() => stopTaskPoll, [stopTaskPoll]);

  /**
   * 轮询回测任务（与 /refresh_status 共用），done 后载入这次的结果。
   *
   * ⚠️ 两个曾经让用户"点了没反应"的点：
   *
   * ① **必须显示 `progress`。** 后端 `_score_grid` 每 20 天回调一次
   *    （"已评估 120/700 天（20250101）"），但这里以前只显示提交时写死的
   *    `message`，所以一个跑几十分钟的回测全程只有一句"回测已开始…"——
   *    用户看到的就是"没输出什么信息"。进度是唯一能证明它还在跑的东西。
   * ② **`unknown` 不等于成功。** 任务表是**进程内** dict，后端一重启
   *    （实测那天 16:23 重启过一次）任务记录就没了，`refresh_status` 回
   *    `state="unknown"`。原来这里落到最后一个分支渲染成 `✅ 任务不存在或已过期`
   *    —— 给一条"东西丢了"的消息打勾，比不显示更误导。
   */
  const pollTask = useCallback(async (taskId: string) => {
    try {
      const status = await mainlineApi.refreshStatus(taskId);
      const state = resolveTaskState(status);
      setTask({ id: status.task_id || taskId, state,
                message: status.error || status.message,
                progress: status.progress || "" });
      if (state !== "running" && state !== "queued") {
        stopTaskPoll();
        // 回测结果结构 ≠ snapshot，所以不直接吃 result，而是按 run_id 重新读一次
        const nextRunId = status.result?.run_id ?? "";
        if (nextRunId) setRunId(nextRunId);
        else { await load(""); await loadRuns(); }
      }
    } catch (exc) {
      stopTaskPoll();
      setTask(null);
      setError(`查询回测进度失败：${exc instanceof Error
        ? exc.message : String(exc)}`);
    }
  }, [stopTaskPoll, load, loadRuns]);

  const startBacktest = useCallback(async () => {
    setError("");
    setTask({ id: "", state: "running", message: "正在提交回测任务…",
              progress: "" });
    try {
      const started = await mainlineApi.runBacktest({});
      setTask({ id: started.task_id, state: "running",
                message: "回测已开始（Walk-Forward 全区间，可能需要几十分钟）…",
                progress: "等待后端返回进度…" });
      stopTaskPoll();
      taskPollRef.current = window.setInterval(
        () => void pollTask(started.task_id), 3000);
    } catch (exc) {
      setTask(null);
      setError(`触发回测失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }, [pollTask, stopTaskPoll]);

  const metrics = report?.metrics ?? null;
  const scenes = report?.scenes ?? [];
  const folds = report?.folds ?? [];
  const correlation = report?.correlation ?? null;
  const markdown = report?.markdown ?? "";

  /**
   * 是否"还有没跑过回测"。
   *
   * 后端的空返回是 `{run_id: "", metrics: {}, gaps: ["还没有回测记录"]}` —— 结构齐全
   * 但一个数都没有。如果照常渲染，用户看到的是 8 张写着"—"的指标卡 + 一张空表，
   * 像是页面坏了；所以这里显式识别空报告，改成一个带"开始回测"按钮的空状态。
   */
  const emptyReport = report !== null && !report.run_id
    && (!metrics || Object.keys(metrics).length === 0);
  const taskRunning = task !== null
    && (task.state === "running" || task.state === "queued");

  /** 按维度拆的 IC：把 `by_dim` 的对象转成可排序数组（对象键顺序不可靠）。 */
  const dimRows = useMemo(() => {
    if (!metrics) return [];
    return Object.entries(metrics.by_dim ?? {}).map(([key, row]) => ({
      key, ...row,
    })).sort((left, right) => (right.ic_mean ?? -Infinity)
      - (left.ic_mean ?? -Infinity));
  }, [metrics]);

  /** 分持有期：键是字符串天数，排序前先转数字。 */
  const holdingRows = useMemo(() => {
    if (!metrics) return [];
    return Object.entries(metrics.by_holding ?? {}).map(([key, row]) => ({
      days: Number(key), ...row,
    })).sort((left, right) => left.days - right.days);
  }, [metrics]);

  const missedScenes = scenes.filter((row) => !row.passed).length;

  if (error) {
    return (
      <section className="mainline-card">
        <div className="error-box">
          读取回测报告失败：{error}
          <div className="muted-text">
            （该模块的回测要手动触发一次才有数据；没有 run_id 时后端返回最近一次，
            从未跑过就会是空或报错）
          </div>
        </div>
      </section>
    );
  }

  return (
    <div className="mainline-backtest">
      {/* 顶部：run 选择 + 概要 */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>回测与胜率报告</h4>
          <span className="muted-text">
            {report
              ? `区间 ${formatDate(report.range_start)} → ${formatDate(report.range_end)}`
                + ` · 耗时 ${report.seconds === null
                  || report.seconds === undefined
                  ? "—" : `${report.seconds.toFixed(0)}s`}`
                + ` · run ${report.run_id}`
              : "正在读取…"}
          </span>
        </div>
        <div className="mainline-alert-bar">
          <label className="muted-text">
            历史回测
            <select className="mono" value={runId}
                    onChange={(event) => setRunId(event.target.value)}>
              <option value="">最近一次</option>
              {runs.map((row) => (
                <option key={row.run_id} value={row.run_id}>
                  {`${row.run_id}（${formatDate(row.range_start)}~${formatDate(row.range_end)}`
                    + `${row.error ? " 失败" : ""}）`}
                </option>
              ))}
            </select>
          </label>
          <button className="btn-ghost tiny" disabled={loading}
                  onClick={() => { void load(runId); void loadRuns(); }}>
            {loading ? "读取中…" : "↻ 重新读取"}
          </button>
          <button className="btn-ghost tiny primary"
                  disabled={taskRunning}
                  title="跑一次 Walk-Forward 回测（全区间，可能需要几分钟）。
                          结果会落库，之后在「历史回测」里随时可读"
                  onClick={() => void startBacktest()}>
            {taskRunning ? "回测中…" : "▶ 开始回测"}
          </button>
          <span style={{ flex: 1 }} />
          {markdown && (
            <button className="btn-ghost tiny"
                    onClick={() => setShowMarkdown((current) => !current)}>
              {showMarkdown ? "收起报告正文" : "展开报告正文（Markdown）"}
            </button>
          )}
        </div>
        {task && (
          <div className={task.state === "failed" || task.state === "unknown"
            ? "error-box" : taskRunning ? "mainline-task" : "mainline-done"}>
            {taskRunning && <span className="spinner" />}
            {task.state === "failed"
              ? `回测失败：${task.message || "未知原因"}`
              : task.state === "unknown"
                // 任务表在进程内，后端重启就没了 —— 这是"结果丢了"，不是"跑成功了"
                ? `⚠️ 回测任务已中断：${task.message || "任务状态已丢失"}`
                  + "（后台任务状态存在内存里，服务重启会丢。"
                  + "若那次已跑完，结果会出现在下方「回测历史」里）"
                : taskRunning ? (task.message || "回测中…")
                  : `✅ ${task.message || "回测完成"}`}
            {taskRunning && task.progress && (
              <span className="muted-text"> · {task.progress}</span>
            )}
          </div>
        )}
        {emptyReport && (
          <div className="mainline-empty mainline-empty-lg">
            <b>还没有回测记录</b>
            <div>
              点右上「▶ 开始回测」跑第一轮（后台任务，跑完自动载入。
              结果会落库，之后可在下方「历史回测」里对比）。
            </div>
            {(report?.gaps?.length ?? 0) > 0 && (
              <div>⚠️ {report?.gaps?.join("；")}</div>
            )}
          </div>
        )}
        {report?.config_note && (
          <div className="muted-text">配置说明：{report.config_note}</div>
        )}
        {report?.error && (
          <div className="error-box">本次回测有错误：{report.error}</div>
        )}
      </section>

      {/*
        ⓪ 回测收益展示面板 —— 放在最前面是**故意的**。
        它不依赖 `mainline_backtest` 记录（那份表经常是空的，"还没有回测记录"
        的空状态会占满首屏），数据直接来自告警表 + 板块指数日线，
        所以即使一次回测都没跑过，用户打开这个页签也立刻能看到"哪些概念的钱好赚"。
        指标卡 / 分场景 / 相关性那些是"这套评分作为因子有没有效"的结论，
        排在它后面当证据。
      */}
      <MainlineAlertReturns onPickBoard={onPickBoard} />

      {/* ① 指标卡（空报告时不渲染，否则是一屏写着"—"的卡片，看着像坏了） */}
      {!emptyReport && (
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>核心指标</h4>
          <span className="muted-text">
            ✅/⚠️ 来自后端 `metrics.targets` 的 `passed`；没有配目标值的项不表态
            {metrics?.ic_samples !== null && metrics?.ic_samples !== undefined
              ? ` · IC 样本 ${metrics.ic_samples} 天` : ""}
            {metrics?.signal_count !== null && metrics?.signal_count !== undefined
              ? ` · 信号 ${metrics.signal_count} 次` : ""}
          </span>
        </div>
        {!metrics ? (
          <div className="mainline-empty">没有回测指标（还没有跑过回测）。</div>
        ) : (
          <div className="mainline-metric-grid">
            {CARDS.map((card) => {
              const value = card.pick(metrics);
              const target = metrics.targets?.[card.key];
              const passed = target?.passed;
              const hasTarget = target !== undefined && target !== null
                && target.target !== null && target.target !== undefined;
              return (
                <div key={card.key}
                     className={"mainline-metric-card"
                       + (hasTarget ? (passed ? " pass" : " fail") : "")}
                     title={card.hint}>
                  <span className="muted-text">{card.label}</span>
                  <b className="mainline-metric-value">{valueText(value, card.kind)}</b>
                  <span className="mainline-metric-target muted-text">
                    {hasTarget
                      ? `${passed ? "✅ 达标" : "⚠️ 未达标"}（目标 ${
                        targetText(target, card.kind)}）`
                      : "未设目标"}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        {metrics && (dimRows.length > 0 || holdingRows.length > 0) && (
          <div className="mainline-drill-grid">
            {dimRows.length > 0 && (
              <div>
                <h5 className="mainline-subhead">按维度拆解</h5>
                <table className="mainline-table">
                  <thead>
                    <tr><th>维度</th><th className="num">IC 均值</th>
                      <th className="num">ICIR</th><th className="num">IC 胜率</th>
                      <th className="num">样本</th></tr>
                  </thead>
                  <tbody>
                    {dimRows.map((row) => (
                      <tr key={row.key}>
                        <td className="mono">{row.key}</td>
                        <td className={"num " + toneOf(row.ic_mean)}>
                          {formatScore(row.ic_mean, 3)}
                        </td>
                        <td className="num">{formatScore(row.icir, 2)}</td>
                        <td className="num">{formatPct(
                          row.ic_win_rate === null ? null : row.ic_win_rate * 100, 1)}</td>
                        <td className="num muted-text">{row.samples ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {holdingRows.length > 0 && (
              <div>
                <h5 className="mainline-subhead">分持有期表现</h5>
                <table className="mainline-table">
                  <thead>
                    <tr><th>持有天数</th><th className="num">多头年化</th>
                      <th className="num">超额</th><th className="num">胜率</th></tr>
                  </thead>
                  <tbody>
                    {holdingRows.map((row) => (
                      <tr key={row.days}>
                        <td className="mono">{row.days} 日</td>
                        <td className={"num " + toneOf(row.long_annual)}>
                          {formatPct(row.long_annual === null
                            ? null : row.long_annual * 100, 1)}
                        </td>
                        <td className={"num " + toneOf(row.long_excess)}>
                          {formatPct(row.long_excess === null
                            ? null : row.long_excess * 100, 1)}
                        </td>
                        <td className="num">{formatPct(row.win_rate === null
                          ? null : row.win_rate * 100, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ② 分场景验证 */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>分场景验证</h4>
          <span className="muted-text">
            共 {scenes.length} 个历史行情场景
            {missedScenes > 0
              ? <> · <b className="mainline-fail-text">未通过 {missedScenes} 个</b></>
              : scenes.length > 0 ? " · 全部通过" : ""}
            · 「领先天数」= 首次告警日距场景起点的交易日数（正数才算提前预警）
          </span>
        </div>
        {scenes.length === 0 ? (
          <div className="mainline-empty">没有场景验证结果。</div>
        ) : (
          <div className="mainline-table-scroll">
            <table className="mainline-table">
              <thead>
                <tr>
                  <th>场景</th><th>起点</th><th>检查窗口</th><th>关键词</th>
                  <th>是否触发</th><th>首次告警</th><th className="num">领先天数</th>
                  <th className="num">最大涨幅</th><th className="num">60日收益</th>
                  <th>结论</th><th>备注</th>
                </tr>
              </thead>
              <tbody>
                {scenes.map((row) => <SceneRow key={row.key} row={row} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ③ 因子相关性 */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>因子相关性验证</h4>
          <span className="muted-text">
            {correlation
              ? `跨层平均 |相关| ${formatScore(correlation.cross_layer_mean_abs, 3)}`
                + ` · 最大 ${formatScore(correlation.cross_layer_max_abs, 3)}`
                + ` · 阈值 ${formatScore(correlation.limit, 2)}`
                + ` · 样本 ${correlation.samples ?? "—"}`
                + (correlation.passed ? " · ✅ 通过" : " · ⚠️ 存在高相关因子对")
              : "无相关性验证结果"}
          </span>
        </div>
        {correlation?.note && (
          <div className="muted-text">{correlation.note}</div>
        )}
        {!correlation || (correlation.factors?.length ?? 0) === 0 ? (
          <div className="mainline-empty">没有可用的因子相关性数据。</div>
        ) : (
          <>
            {(correlation.high_pairs?.length ?? 0) > 0 && (
              <div className="mainline-gaps">
                ⚠️ 高相关因子对（|相关| &gt; {formatScore(correlation.limit, 2)}
                ，权重叠加会重复计分）：
                {correlation.high_pairs.map((pair) => (
                  <b key={`${pair.a}-${pair.b}`} className="mainline-high-pair">
                    {pair.a} × {pair.b} = {pair.corr.toFixed(3)}
                  </b>
                ))}
              </div>
            )}
            <div className="mainline-table-scroll">
              <table className="mainline-table mainline-corr-table">
                <thead>
                  <tr>
                    <th>因子</th>
                    {correlation.factors.map((factor) => (
                      <th key={factor} className="num mainline-corr-th"
                          title={factor}>
                        {factor.length > 10 ? `${factor.slice(0, 10)}…` : factor}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {correlation.factors.map((rowFactor) => (
                    <tr key={rowFactor}>
                      <td className="mono mainline-corr-rowhead-cell"
                          title={rowFactor}>
                        {rowFactor.length > 18 ? `${rowFactor.slice(0, 18)}…` : rowFactor}
                      </td>
                      {correlation.factors.map((colFactor) => {
                        if (rowFactor === colFactor) {
                          return <td key={colFactor}
                                     className="num mainline-corr-self">1</td>;
                        }
                        const value = correlation.matrix?.[rowFactor]?.[colFactor];
                        const usable = value !== null && value !== undefined
                          && Number.isFinite(value);
                        // 矩阵只给下三角，上三角要取对称项 —— 显示 "—" 会让人以为没算
                        const mirrored = usable
                          ? value : correlation.matrix?.[colFactor]?.[rowFactor];
                        const safe = mirrored !== null && mirrored !== undefined
                          && Number.isFinite(mirrored) ? mirrored : null;
                        const high = safe !== null && correlation.limit !== null
                          && correlation.limit !== undefined
                          && Math.abs(safe) > correlation.limit;
                        return (
                          <td key={colFactor}
                              className={"num" + (high ? " mainline-corr-high" : "")}>
                            {safe === null ? "—" : safe.toFixed(2)}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      {/* ③b walk-forward 折 */}
      {folds.length > 0 && (
        <section className="mainline-card">
          <div className="mainline-card-head">
            <h4>Walk-Forward 分折</h4>
            <span className="muted-text">
              purge 段是为了防止训练/验证之间用同一天的信号泄漏未来收益
            </span>
          </div>
          <div className="mainline-table-scroll">
            <table className="mainline-table">
              <thead>
                <tr>
                  <th className="num">#</th><th>训练段</th><th>purge</th>
                  <th>验证段</th><th className="num">IC(六维)</th>
                  <th className="num">IC(建仓)</th><th className="num">IC(龙头)</th>
                  <th className="num">IC(总分)</th><th className="num">IC(等权)</th>
                  <th className="num">板块</th><th className="num">告警</th><th>备注</th>
                </tr>
              </thead>
              <tbody>
                {folds.map((fold) => <FoldRow key={fold.index} row={fold} />)}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* ④ 回测历史 */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>回测历史</h4>
          <span className="muted-text">
            共 {runs.length} 次（最近 20 次）；点任意行的「查看」载入该次报告
          </span>
        </div>
        {runs.length === 0 ? (
          <div className="mainline-empty">还没有回测记录。</div>
        ) : (
          <div className="mainline-table-scroll">
            <table className="mainline-table">
              <thead>
                <tr>
                  <th>run_id</th><th>区间</th><th>开始时间</th>
                  <th className="num">耗时</th><th className="num">IC 均值</th>
                  <th className="num">ICIR</th><th className="num">多空年化</th>
                  <th className="num">假阳性率</th><th>状态</th><th /></tr>
              </thead>
              <tbody>
                {runs.map((row) => (
                  <tr key={row.run_id}
                      className={row.run_id === report?.run_id ? "row-active" : ""}>
                    <td className="mono">{row.run_id}</td>
                    <td className="mono">
                      {formatDate(row.range_start)}~{formatDate(row.range_end)}
                    </td>
                    <td className="mono muted-text">
                      {(row.started_at || "").replace("T", " ").slice(0, 16) || "—"}
                    </td>
                    <td className="num">
                      {row.seconds === null || row.seconds === undefined
                        ? "—" : `${row.seconds.toFixed(0)}s`}
                    </td>
                    <td className="num">{formatScore(row.metrics?.ic_mean ?? null, 3)}</td>
                    <td className="num">{formatScore(row.metrics?.icir ?? null, 2)}</td>
                    <td className={"num " + toneOf(row.metrics?.long_short_annual ?? null)}>
                      {formatPct(row.metrics?.long_short_annual === null
                        || row.metrics?.long_short_annual === undefined
                        ? null : row.metrics.long_short_annual * 100, 1)}
                    </td>
                    <td className="num">
                      {formatPct(row.metrics?.false_positive_rate === null
                        || row.metrics?.false_positive_rate === undefined
                        ? null : row.metrics.false_positive_rate * 100, 1)}
                    </td>
                    <td>{row.error
                      ? <span className="mainline-fail-text">失败</span>
                      : <span className="mainline-pass-text">成功</span>}
                      {row.gaps?.length > 0
                        ? <span className="muted-text">（{row.gaps.length} 项缺数据）</span>
                        : null}
                    </td>
                    <td>
                      <button className="btn-ghost tiny"
                              onClick={() => setRunId(row.run_id)}>查看</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ⑤ 信号明细（点板块可下钻） */}
      {(report?.signals?.length ?? 0) > 0 && (
        <section className="mainline-card">
          <div className="mainline-card-head">
            <h4>回测信号明细</h4>
            <span className="muted-text">
              共 {report?.signals?.length ?? 0} 条；假阳性样本
              {" "}{report?.false_positives?.length ?? 0} 条（失败样本不隐藏，
              是调参最该看的）
            </span>
          </div>
          <div className="mainline-table-scroll">
            <table className="mainline-table">
              <thead>
                <tr>
                  <th>告警日</th><th>板块</th><th className="num">预警分</th>
                  <th>等级</th><th className="num">5日</th><th className="num">10日</th>
                  <th className="num">20日</th><th className="num">60日</th>
                  <th className="num">最大涨幅</th><th>确认</th></tr>
              </thead>
              <tbody>
                {report?.signals.slice(0, 200).map((row) => (
                  <tr key={row.alert_id}>
                    <td className="mono">{formatDate(row.trade_date)}</td>
                    <td>
                      <button className="mainline-link"
                              onClick={() => onPickBoard?.(
                                row.board_code, row.board_name)}>
                        {row.board_name || row.board_code}
                      </button>
                    </td>
                    <td className="num mono">{formatScore(row.score, 1)}</td>
                    <td>{row.level_label || row.level}</td>
                    <td className={"num " + toneOf(row.ret_5d)}>
                      {formatPct(row.ret_5d, 1)}</td>
                    <td className={"num " + toneOf(row.ret_10d)}>
                      {formatPct(row.ret_10d, 1)}</td>
                    <td className={"num " + toneOf(row.ret_20d)}>
                      {formatPct(row.ret_20d, 1)}</td>
                    <td className={"num " + toneOf(row.ret_60d)}>
                      {formatPct(row.ret_60d, 1)}</td>
                    <td className={"num " + toneOf(row.max_gain_pct)}>
                      {formatPct(row.max_gain_pct, 1)}</td>
                    <td className={row.confirmed ? "mainline-confirmed" : "muted-text"}>
                      {row.confirmed ? "✅" : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(report?.signals?.length ?? 0) > 200 && (
            <div className="muted-text">
              只显示前 200 条（共 {report?.signals?.length ?? 0} 条），完整清单见报告正文或 CSV 导出。
            </div>
          )}
        </section>
      )}

      {/* ⑥ 报告正文 */}
      {showMarkdown && markdown && (
        <section className="mainline-card">
          <div className="mainline-card-head">
            <h4>监控胜率报告（正文）</h4>
            <span className="muted-text">后端生成的 Markdown，前端做轻量解析渲染</span>
          </div>
          <MainlineMarkdown text={markdown} />
        </section>
      )}

      {(report?.gaps?.length ?? 0) > 0 && (
        <div className="mainline-gaps">⚠️ 缺数据说明：{report?.gaps?.join("；")}</div>
      )}
      {report?.disclaimer && (
        <div className="mainline-foot muted-text">{report.disclaimer}</div>
      )}
    </div>
  );
}

/** 场景行（未通过整行标红底，通过的标绿文字）。 */
function SceneRow({ row }: { row: BacktestScene }) {
  // 场景行里 `check_window` / `keywords` 是后端字段：老报告可能没有这两个键，
  // 直接读 `.length` 会抛 `Cannot read properties of undefined`，
  // 连累整张分场景表白屏 —— 所以逐处兜底而不是信任类型声明。
  const window_ = row.check_window ?? [];
  const keywords = row.keywords ?? [];
  return (
    <tr className={row.passed ? "" : "mainline-scene-fail"}>
      <td>{row.label || row.key}</td>
      <td className="mono">{formatDate(row.start_date)}</td>
      <td className="mono muted-text">
        {window_.length >= 2
          ? `${formatDate(window_[0])}~${formatDate(window_[1])}`
          : "—"}
      </td>
      <td className="muted-text">
        {keywords.length > 0 ? keywords.join("/") : "—"}
      </td>
      <td>{row.triggered
        ? <span className="mainline-pass-text">是</span>
        : <span className="mainline-fail-text">否</span>}</td>
      <td className="mono muted-text">
        {row.first_alert_date
          ? `${formatDate(row.first_alert_date)}${row.first_alert_board
            ? ` ${row.first_alert_board}` : ""}`
          : "—"}
      </td>
      <td className={"num " + toneOf(row.lead_days)}>
        {row.lead_days ?? "—"}
        {row.min_lead_days !== null && row.min_lead_days !== undefined
          ? <span className="muted-text"> / 要求 {row.min_lead_days}</span> : null}
      </td>
      <td className={"num " + toneOf(row.max_gain_pct)}>
        {formatPct(row.max_gain_pct, 1)}
      </td>
      <td className={"num " + toneOf(row.ret_60d)}>
        {formatPct(row.ret_60d, 1)}
      </td>
      <td>{row.passed
        ? <span className="mainline-pass-text">✅ 通过</span>
        : <span className="mainline-fail-text">⚠️ 未通过</span>}</td>
      <td className="muted-text mainline-logic">{row.note || "—"}</td>
    </tr>
  );
}

/** 分折行。 */
function FoldRow({ row }: { row: BacktestFold }) {
  return (
    <tr>
      <td className="num">{row.index}</td>
      <td className="mono muted-text">
        {formatDate(row.train_start)}~{formatDate(row.train_end)}
      </td>
      <td className="mono muted-text">{formatDate(row.purge_end)}</td>
      <td className="mono muted-text">
        {formatDate(row.validate_start)}~{formatDate(row.validate_end)}
      </td>
      <td className={"num " + toneOf(row.ic_six)}>{formatScore(row.ic_six, 3)}</td>
      <td className={"num " + toneOf(row.ic_accumulation)}>
        {formatScore(row.ic_accumulation, 3)}</td>
      <td className="num muted-text">{formatScore(row.ic_leader, 3)}</td>
      <td className={"num " + toneOf(row.ic_total)}>{formatScore(row.ic_total, 3)}</td>
      <td className="num muted-text">{formatScore(row.ic_equal, 3)}</td>
      <td className="num">{row.boards ?? "—"}</td>
      <td className="num">{row.alerts ?? "—"}</td>
      <td className="muted-text mainline-logic">{row.note || "—"}</td>
    </tr>
  );
}

/**
 * 轻量 Markdown 渲染器（只认三种块：标题 / 表格 / 列表）。
 *
 * 设计取舍：**按行状态机**而不是递归解析。后端报告是模板拼出来的，层级只有
 * `#`/`##`/`###`，表格用 `|`，列表用 `-`；状态机足以覆盖，且遇到不认识的语法
 * （引用块、代码块、链接）就整段当普通段落输出 —— 宁可显示原始文本，
 * 也不要因为"不认识"而丢内容。链接/加粗不做解析：报告里几乎用不到，
 * 引入行内解析反而要处理转义与嵌套。
 */
function MainlineMarkdown({ text }: { text: string }) {
  const blocks = useMemo(() => {
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    const nodes: { kind: "h" | "p" | "ul" | "table";
                   level: number;
                   lines: string[] }[] = [];
    let current: { kind: "ul" | "table"; lines: string[] } | null = null;

    const flush = () => {
      if (current && current.lines.length > 0) {
        nodes.push({ kind: current.kind, level: 0, lines: current.lines });
      }
      current = null;
    };

    for (const raw of lines) {
      const line = raw.trimEnd();
      const trimmed = line.trim();
      if (!trimmed) { flush(); continue; }

      const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (heading) {
        flush();
        nodes.push({ kind: "h", level: heading[1].length,
                     lines: [heading[2].trim()] });
        continue;
      }
      if (trimmed.startsWith("|")) {
        if (!current || current.kind !== "table") {
          flush();
          current = { kind: "table", lines: [] };
        }
        current.lines.push(trimmed);
        continue;
      }
      if (/^[-*+]\s+/.test(trimmed)) {
        if (!current || current.kind !== "ul") {
          flush();
          current = { kind: "ul", lines: [] };
        }
        current.lines.push(trimmed.replace(/^[-*+]\s+/, ""));
        continue;
      }
      flush();
      nodes.push({ kind: "p", level: 0, lines: [trimmed] });
    }
    flush();
    return nodes;
  }, [text]);

  return (
    <div className="markdown mainline-markdown">
      {blocks.map((block, index) => {
        const key = `${block.kind}-${index}`;
        if (block.kind === "h") {
          const title = block.lines[0];
          // 按层级显式分支（不用动态 Tag 变量）：动态标签名在 TS 里会退化成
          // 联合类型，`<Tag>` 的 props 推断会失败，这里显式写三种最省事。
          if (block.level === 1) return <h1 key={key}>{title}</h1>;
          if (block.level === 2) return <h2 key={key}>{title}</h2>;
          return <h3 key={key}>{title}</h3>;
        }
        if (block.kind === "ul") {
          return (
            <ul key={key}>
              {block.lines.map((line, lineIndex) => (
                <li key={lineIndex}>{line}</li>
              ))}
            </ul>
          );
        }
        if (block.kind === "table") {
          // 第二行是 `| --- | --- |` 分隔行：只要它全是 - 和 :，就当表头分隔
          const isSeparator = (line: string) => /^\|[\s:|-]+\|$/.test(line);
          const cells = (line: string) => line
            .replace(/^\|/, "").replace(/\|$/, "")
            .split("|").map((cell) => cell.trim());
          const header = cells(block.lines[0]);
          const bodyStart = block.lines.length > 1 && isSeparator(block.lines[1])
            ? 2 : 1;
          return (
            <table key={key} className="mainline-table">
              <thead>
                <tr>{header.map((cell, cellIndex) => (
                  <th key={cellIndex}>{cell}</th>
                ))}</tr>
              </thead>
              <tbody>
                {block.lines.slice(bodyStart).map((line, lineIndex) => (
                  <tr key={lineIndex}>
                    {cells(line).map((cell, cellIndex) => (
                      <td key={cellIndex}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          );
        }
        return <p key={key}>{block.lines[0]}</p>;
      })}
    </div>
  );
}
