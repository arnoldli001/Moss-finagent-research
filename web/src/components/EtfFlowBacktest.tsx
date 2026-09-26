import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  formatDate,
  formatRatioPct,
  mainlineApi,
  resolveTaskState,
  toneOf,
} from "../mainlineApi";
import {
  ETF_KIND_TEXT,
  ETF_REGIME_TEXT,
  etfFlowApi,
  etfGroupText,
  type EtfFlowBacktestRecord,
  type EtfFlowBacktestReport,
  type EtfFlowBacktestRun,
  type EtfFlowHorizonStats,
  type EtfFlowHorizonTable,
} from "../etfFlowApi";

/**
 * ETF 份额监控 · 回测结果。
 *
 * ## 这个视图要回答的问题
 *
 * 不是"信号多准"，而是 **"哪一类信号、在哪种环境下才准"**。因为门控规则
 * （机会信号只在熊市放行）本身就是回测结论，用户必须能看到分桶之后的差异：
 * 拆开看某个桶很好、合并看只剩噪声，正是这个模块最反直觉的地方。
 * 所以三张分组表（类型 → 环境 → 类型×环境）是核心，明细表放在最后做证据。
 *
 * ## 为什么中位数/胜率走不同的格式化函数
 *
 * `median` 是**收益**（小数），带正负号才有意义 → `formatRatioPct()`（+9.18%）。
 * `win_rate` 是**比率**（0.833）而不是涨跌：`formatPct()` 与 `formatRatioPct()`
 * 都会给正数补 "+"（项目里其余面板的胜率列显示成 "+83.3%"），这里单独用
 * `rateText()` 输出不带符号的 "83.3%" —— 胜率没有方向，加号会让人误以为
 * 它在跟 0 比。
 *
 * ## 后端说的和需求书说的有一处不同（已按后端实现写）
 *
 * 需求书写 records 是 `{signal: {...}, regime, ...}`（嵌套），而后端
 * `SignalRecord.to_dict()` 是**平铺**的：`signal.to_dict()` 的结果直接 update 上
 * regime/forward/max_gain。所以 `EtfFlowBacktestRecord` 声明成
 * `EtfFlowSignal & {后续表现}`，读的是 `record.forward["5"]` 而不是
 * `record.signal.forward`。类型跟着**后端实际返回**走，不跟着需求书的示意走。
 *
 * ## 报告正文（Markdown）暂不渲染
 *
 * 接口支持 `with_markdown=true`，但项目里只有 MainlineBacktest 内部那个轻量
 * 解析器，且它是私有组件。为一个"锦上添花"的正文再抄一份解析器不划算，
 * 需要时把那个解析器提出来共用即可 —— 现在只在概要里指路。
 */

/** 分组展示顺序：按语义排，未知 key 沉底（再按字母序）。 */
const GROUP_ORDER: string[] = [
  "opportunity", "risk", "industry_reversal",
  "level:strong", "level:medium", "level:weak",
  "opportunity_live", "opportunity_gated", "risk_live", "risk_gated",
  "bull", "bear", "range",
];

function groupRank(key: string): number {
  const index = GROUP_ORDER.indexOf(key);
  return index >= 0 ? index : GROUP_ORDER.length;
}

/** `{组: {观察期: 统计}}` → 摊平成行，组内按观察期升序（对象键顺序不可靠）。 */
function flatten(
  table: EtfFlowHorizonTable,
): { key: string; stats: EtfFlowHorizonStats }[] {
  const rows: { key: string; stats: EtfFlowHorizonStats }[] = [];
  for (const [key, buckets] of Object.entries(table ?? {})) {
    for (const stats of Object.values(buckets ?? {})) rows.push({ key, stats });
  }
  return rows.sort((left, right) => {
    const rank = groupRank(left.key) - groupRank(right.key);
    if (rank !== 0) return rank;
    if (left.key !== right.key) return left.key.localeCompare(right.key);
    return left.stats.horizon - right.stats.horizon;
  });
}

/**
 * 胜率（0~1）→ "83.3%"；null → "—"（不显示 0%）。
 *
 * 刻意不走 `formatPct()` / `formatRatioPct()`：那两个都会给正数补 "+"，
 * 而胜率没有方向（见文件头的注释）。
 */
function rateText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/**
 * 胜率的 Wilson 95% 区间 → `[72.6–90.4]`；没有区间时返回空串（调用方不渲染）。
 *
 * 缩写成"下限–上限"而不是 `[72.6%, 90.4%]`：它紧跟在胜率后面同一格里，
 * 省掉两个百分号和逗号能让这一列不折行。
 *
 * 它存在的意义是**防止把一个点估计读成事实** —— 样本越少区间越宽，
 * 而这张表里 n=3 的格子和 n=419 的格子看起来一样自信。
 */
function ciText(stats: EtfFlowHorizonStats): string {
  const low = stats.win_rate_low;
  const high = stats.win_rate_high;
  if (low === null || low === undefined || high === null
    || high === undefined || !Number.isFinite(low) || !Number.isFinite(high)) {
    return "";
  }
  return `[${(low * 100).toFixed(1)}–${(high * 100).toFixed(1)}]`;
}

/** 目标线文案（放在达标单元格的 title 里，正文不占列宽）。 */
function targetText(stats: EtfFlowHorizonStats): string {
  const parts: string[] = [];
  if (stats.target_median !== null && stats.target_median !== undefined) {
    parts.push(`T 中位数 ≥ ${formatRatioPct(stats.target_median)}`);
  }
  if (stats.target_win_rate !== null && stats.target_win_rate !== undefined) {
    parts.push(`胜率 ≥ ${rateText(stats.target_win_rate)}`);
  }
  return parts.length > 0
    ? `目标：${parts.join("，")}（configs/etf_flow.yaml）`
    : "未配置目标线";
}

type Props = {
  /** 面板顶部的 trade_date，用于在概要里说明回测区间取自配置 */
  tradeDate?: string;
};

export default function EtfFlowBacktest({ tradeDate }: Props) {
  const [report, setReport] = useState<EtfFlowBacktestReport | null>(null);
  const [runs, setRuns] = useState<EtfFlowBacktestRun[]>([]);
  const [runId, setRunId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [task, setTask] = useState<{ id: string; state: string;
                                    message: string } | null>(null);
  const taskPollRef = useRef<number | null>(null);

  const load = useCallback(async (id: string) => {
    setLoading(true);
    setError("");
    try {
      setReport(await etfFlowApi.backtest(id));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      const payload = await etfFlowApi.backtestList(20);
      setRuns(payload.runs ?? []);
    } catch {
      // 历史列表只是导航，失败不该挡住报告本身
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

  /** 轮询回测任务（与主线挖掘共用 `/refresh_status`），done 后按 run_id 载入。 */
  const pollTask = useCallback(async (taskId: string) => {
    try {
      const status = await mainlineApi.refreshStatus(taskId);
      const state = resolveTaskState(status);
      setTask({ id: status.task_id || taskId, state,
                message: status.error || status.message });
      if (state !== "running" && state !== "queued") {
        stopTaskPoll();
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
    setTask({ id: "", state: "running", message: "正在提交回测任务…" });
    try {
      const started = await etfFlowApi.runBacktest({});
      setTask({ id: started.task_id, state: started.state || "running",
                message: "回测已开始（后台任务，跑完自动载入）…" });
      stopTaskPoll();
      taskPollRef.current = window.setInterval(
        () => void pollTask(started.task_id), 3000);
    } catch (exc) {
      setTask(null);
      setError(`触发回测失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }, [pollTask, stopTaskPoll]);

  const result = report?.result ?? null;
  const records = result?.records ?? [];

  /**
   * 是否显示**被门控的行**（默认否，用户明确要求）。
   *
   * 被门控的行是"环境不支持、只作观察"的项，`gated=true` 且等级降为 weak ——
   * 既不是多方触发也不是空方触发，直接铺在明细表里就是噪声（实测某段窗口 21 行里
   * 20 行是同一类重复行）。数据本身照常返回、照常落库，只是不进默认视图。
   */
  const [showGated, setShowGated] = useState(false);

  const visible = records.slice(0, 200);
  /**
   * 「风险信号 **且** 弱信号」这一组合**永久不显示**（2026-09-22，用户决定）。
   *
   * ## 为什么可以直接去掉而不是做成开关
   *
   * 这一块同时具备两个"没有操作含义"的属性，一条都不占理：
   * * **弱信号** = 指数分位**不在**极端区（机会要 ≤30%、风险要 ≥70%），
   *   系统自己都不告警，只放进观察列表；
   * * **风险信号** = 它想说的"高位赎回"里的"高位"根本不成立 —— 分位没到 70%。
   *
   * 也就是说这 66 行是"份额有异动、但位置不极端"，既不是多方触发也不是有效警示。
   * 用户看过明细后确认这类只制造"误报很多"的错觉，决定不显示。
   *
   * ## 去掉的范围（重要）
   *
   * **只去掉明细表里的这些行**，不动任何数据与统计：
   * * 后端照常返回、`ml_etf_backtest` 照常落库（想看随时能把过滤去掉）；
   * * `by_kind` / `by_etf` / `by_regime` 的样本数**仍按全量计** ——
   *   否则"风险信号整体 T+34 -2.00%"这个结论会被悄悄改掉，
   *   而那正是判断风险信号有没有用的唯一依据。
   *
   * 另外两类**不受影响**，仍在明细里：中/强等级的风险信号（会告警，有方向意义）、
   * 弱等级的机会/行业信号（观察项，但不是风险信号）。
   */
  const isRiskWeak = useCallback((row: EtfFlowBacktestRecord) =>
    row.kind === "risk" && row.level !== "strong" && row.level !== "medium",
  []);
  const rows = useMemo(() => {
    const picked = visible.filter((row) => !isRiskWeak(row));
    return showGated ? picked : picked.filter((row) => !row.gated);
  }, [visible, showGated, isRiskWeak]);
  const gatedCount = visible.filter(
    (row) => row.gated && !isRiskWeak(row)).length;
  const riskWeakCount = visible.filter(isRiskWeak).length;

  /**
   * 从未跑过回测时后端只回 `{run_id: "", gaps: ["还没有回测记录…"]}` ——
   * 结构齐全但一个数都没有。照常渲染会是一屏空表，所以显式识别成空状态。
   */
  const emptyReport = report !== null && !report.run_id && result === null;
  const taskRunning = task !== null
    && (task.state === "running" || task.state === "queued");

  const byKind = useMemo(() => result?.by_kind ?? {}, [result]);
  const byRegime = useMemo(() => result?.by_regime ?? {}, [result]);
  const byKindRegime = useMemo(() => result?.by_kind_regime ?? {}, [result]);
  /**
   * 样本内/外对照表。旧回测记录（本次改动之前落库的）没有这张表，
   * `?? {}` 让卡片落到"没有切分"的空状态 —— 而不是让整页崩掉。
   */
  const validation = useMemo(() => result?.validation ?? {}, [result]);

  /**
   * 一句话结论：这套信号能不能当系统用。
   *
   * 原来这个判断被摊在每张表的「达标」列上（44 个桶里 42 个 ⚠️），
   * 既淹没了真正的结论，又让人误以为"整张表都没意义"。
   * 现在收敛成这里一句 + 门控对照的对比 —— 该下判决的地方只此一处。
   */
  const verdict = useMemo(() => {
    const live = byKind["opportunity_live"]?.["34"];
    const gated = byKind["opportunity_gated"]?.["34"];
    if (!live) return null;
    return { live, gated, passed: live.passed === true };
  }, [byKind]);

  return (
    <div className="etf-flow-backtest">
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>回测与胜率</h4>
          <span className="muted-text">
            {report?.run_id
              ? `区间 ${formatDate(report.range_start)} → ${formatDate(report.range_end)}`
                + ` · ${report.days ?? "—"} 个交易日`
                + ` · 信号 ${report.signals ?? "—"} 次`
                + ` · 耗时 ${report.seconds === null
                  || report.seconds === undefined
                  ? "—" : `${report.seconds.toFixed(0)}s`}`
                + ` · run ${report.run_id}`
              : "正在读取…"}
          </span>
        </div>

        <div className="etf-flow-toolbar">
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
          <button className="btn-ghost tiny primary" disabled={taskRunning}
                  title={"跑一次全区间回测（后台任务，分钟级）。区间与目标线取自"
                    + " configs/etf_flow.yaml 的 backtest 段；结果会落库，"
                    + "之后在「历史回测」里随时可读"}
                  onClick={() => void startBacktest()}>
            {taskRunning ? "回测中…" : "▶ 运行回测"}
          </button>
          <span style={{ flex: 1 }} />
          <span className="muted-text">
            达标线来自后端 targets，前端只画 ✅/⚠️
            {tradeDate ? ` · 面板交易日 ${formatDate(tradeDate)}` : ""}
          </span>
        </div>

        {verdict && (
          <div className={verdict.passed ? "mainline-done" : "info-box"}>
            <b>
              {verdict.passed
                ? "✅ 门控后的机会信号达到目标线"
                : "⚠️ 门控后的机会信号未达目标线"}
            </b>
            <div className="muted-text">
              T+34：环境放行（{verdict.live.samples} 条）中位数{" "}
              <b className={toneOf(verdict.live.median)}>
                {formatRatioPct(verdict.live.median)}
              </b>
              {" "}· 胜率 <b>{rateText(verdict.live.win_rate)}</b>
              {verdict.gated && (
                <>
                  ；同口径下被门控的（{verdict.gated.samples} 条）
                  中位数 {formatRatioPct(verdict.gated.median)} · 胜率{" "}
                  {rateText(verdict.gated.win_rate)}
                </>
              )}
              <br />
              目标线（{targetText(verdict.live)}）来自 configs/etf_flow.yaml 的
              backtest 段。下面各表的切片桶大多不会达标 ——
              风险信号结构上就是负期望，按等级/环境/等级×环境切出来的桶
              本来就不是"独立可用"的东西，看数字、不必逐行对照目标线。
            </div>
          </div>
        )}

        {task && (
          <div className={task.state === "failed" ? "error-box"
            : taskRunning ? "mainline-task" : "mainline-done"}>
            {taskRunning && <span className="spinner" />}
            {task.state === "failed" ? `回测失败：${task.message || "未知原因"}`
              : taskRunning ? (task.message || "回测中…")
                : `✅ ${task.message || "回测完成"}`}
          </div>
        )}
        {error && (
          <div className="error-box">
            读取回测结果失败：{error}
            <div className="muted-text">
              （该回测要手动触发一次才有数据；从未跑过时后端返回空报告 ——
              点「▶ 运行回测」跑第一轮）
            </div>
          </div>
        )}
        {emptyReport && (
          <div className="etf-flow-empty">
            <b>还没有回测记录</b>
            <div>
              点「▶ 运行回测」跑第一轮（后台任务，跑完自动载入并落库，
              之后可在「历史回测」里对比）。
            </div>
            {/* `report.gaps` 可能是 undefined（空壳返回只给了子集键）——
                直接读 `.length` 会抛 `Cannot read properties of undefined`，
                和主线挖掘「回测报告」白屏是同一个 bug（见 mainlineApi.ts 的
                `normalizeBacktestReport` 注释）。 */}
            {(report?.gaps?.length ?? 0) > 0 && (
              <div>⚠️ {report?.gaps?.join("；")}</div>
            )}
          </div>
        )}
        {report?.error && (
          <div className="error-box">本次回测有错误：{report.error}</div>
        )}
      </section>

      {/* ① 按信号类型 / 等级 / 门控对照 */}
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>分类型胜率</h4>
          <span className="muted-text">
            「环境放行」与「已门控」是<b>后端分的两个桶</b>，
            两桶的差异就是门控的价值；只有强/中等级才进"放行"桶（弱信号从不告警）
          </span>
        </div>
        <HorizonTables table={byKind} empty="还没有回测结果。" />
      </section>

      {/* ② 按市场环境 */}
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>分市场环境胜率</h4>
          <span className="muted-text">
            环境按触发时的近 120 日收益判定（牛市 &gt; +15%、熊市 &lt; -15%，
            其余震荡市）；这里只看环境，会把不同类型信号混在一起
          </span>
        </div>
        <HorizonTables table={byRegime} empty="还没有回测结果。" />
      </section>

      {/* ③ 类型 × 环境（这才是门控规则的依据） */}
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>分类型 × 环境胜率</h4>
          <span className="muted-text">
            门控规则就是这张表的结论：机会信号只在熊市那一格为正，
            因此其余环境被降级为观察
          </span>
        </div>
        <HorizonTables table={byKindRegime} empty="还没有回测结果。" />
      </section>

      {/* ④ 样本内 / 样本外（唯一能证明"这些阈值不是拟合出来的"的东西） */}
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>样本内 / 样本外对照</h4>
          <span className="muted-text">
            {result?.split
              ? `切分点 ${formatDate(result.split)}：之前的信号用来定阈值（`
                + "好看是必然的），之后那段才是没看过的"
              : "未配置切分点（configs/etf_flow.yaml 的 backtest.split）"}
          </span>
        </div>
        <ValidationTables table={validation} />
        <div className="etf-flow-note muted-text">
          所有阈值都是在<b>全区间</b>上看着回测结果调出来的，所以拿同一份全区间
          结果当"验证"是循环论证。切分之后只看右半边。
          <br />
          ⚠️ <b>「独立」≤2 时不要读成"结论很好"</b>：样本外只剩一两个独立时段时，
          "结论仍然成立"和"结论根本无法验证"在胜率和中位数上完全一样 ——
          实测机会信号的样本外就只剩 <b>1 个</b>独立时段。
        </div>
      </section>

      {/* ⑤ 明细（证据；失败样本不隐藏） */}
      {result && records.length > 0 && (
        <section className="etf-flow-card">
          <div className="etf-flow-card-head">
            <h4>信号明细</h4>
            <span className="muted-text">
              共 {result.record_count} 条，明细保留<b>最近</b> {records.length} 条
              （后端上限 300，新的在前）；当前显示 {rows.length} 行
              {riskWeakCount > 0
                && `（已隐去「风险+弱信号」${riskWeakCount} 行）`}
              {!showGated && gatedCount > 0 && `（已门控 ${gatedCount} 行默认不显示）`}
            </span>
          </div>
          {/* ⚠️ 这段说明必须留着：明细天然在末尾断档，不解释就会被当成 bug。
              回测循环是 `while cursor < len(calendar) - max_horizon`
              （见 src/mainline/etf_flow_backtest.py），最近 34 个交易日
              （T+34 观察期）还没走完，算不出后续收益 ——
              实测 20260922 那天明细只到 20260804，最后可评估日是 20260806，
              而 8/5~8/6 恰好没触发信号。 */}
          <div className="etf-flow-note muted-text">
            ⚠️ <b>明细最后一天必然早于今天</b>：每条明细都要记录 T+5/10/20/34 的
            <b>实际</b>收益，而最近 34 个交易日"未来还没发生"，所以回测只评估到
            「最新交易日往前 34 个交易日」为止。要看最近几周<b>有没有新信号</b>，
            请用「信号列表」页签（那里读实时快照）；本页回答的是
            "这些信号后来涨没涨"。
            <br />
            <b>「份额1日」是份额变化率，不是价格涨跌</b> —— 它和右边的
            T+5~T+34（价格收益）口径完全不同。左边 -18.91% 配右边 +7.00%
            并不矛盾：那是"当天有大量份额赎回，而 34 天后价格反而涨了"。
            <br />
            带 <b>×N日</b> 的行表示同一事件**连续报了 N 天**（判据是份额 5 日累计，
            一次流入会让它连着超标）。明细只留首日；表里的样本数仍按日计，
            所以 300 行明细对应的独立事件远少于 300。
            <br />
            <b>「风险+弱信号」这一组合已不再显示</b>：它既不是多方触发也不是有效警示
            （分位没到 70% 的"高位"+ 从不告警的弱等级），只制造"误报很多"的错觉。
            中/强等级的风险信号与弱等级的机会/行业信号**都还在**。
            上面统计表的样本数仍按全量计，不受此显示调整影响。
          </div>
          <div className="etf-flow-toolbar">
            <span style={{ flex: 1 }} />
            <label className="chart-toggle"
                   title={"被门控 = 环境不支持该信号，不构成仓位建议。"
                     + "默认隐藏；勾上可以看门控挡掉的样本后来实际涨跌如何"}>
              <input type="checkbox" checked={showGated}
                     onChange={(event) => setShowGated(event.target.checked)} />
              显示已门控（{gatedCount}）
            </label>
          </div>
          <div className="etf-flow-table-scroll">
            <table className="etf-flow-table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>ETF</th>
                  <th>类型</th>
                  <th>等级</th>
                  <th>环境</th>
                  <th className="num" title="份额的日环比（**不是**价格涨跌）">份额1日</th>
                  <th className="num">T+5</th>
                  <th className="num">T+10</th>
                  <th className="num">T+20</th>
                  <th className="num">T+34</th>
                  <th className="num" title="观察窗口内标的的最大浮盈（价格）">价格最大浮盈</th>
                  <th className="num" title="观察窗口内标的的最大浮亏（价格）">价格最大浮亏</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((record) => (
                  <RecordRow key={record.alert_id
                    || `${record.date}-${record.code}-${record.kind}`}
                             record={record} />
                ))}
              </tbody>
            </table>
          </div>
          {rows.length === 0 && (
            <div className="etf-flow-empty">
              当前过滤下没有明细行。最近 {visible.length} 条信号全部落在被你
              排除的类别里 —— 取消上面的勾选即可看到它们。
            </div>
          )}
          {records.length > visible.length && (
            <div className="muted-text">
              只显示最近 {visible.length} 条（接口带回 {records.length} 条，
              回测全样本共 {result.record_count} 条）；当前过滤后显示 {rows.length} 行。
              看更早的样本请提高后端 `record_limit` 或跑一次窄区间回测。
            </div>
          )}
        </section>
      )}

      {(report?.gaps?.length ?? 0) > 0 && (
        <div className="etf-flow-gaps">⚠️ {report?.gaps?.join("；")}</div>
      )}
      <div className="etf-flow-note muted-text">
        「T+1」口径 = 信号次日收盘买入（更保守），「T」= 信号当日收盘买入；
        观察期与目标线来自 configs/etf_flow.yaml 的 backtest 段；
        「—」= 该桶没有样本，不是 0。
      </div>
    </div>
  );
}

/** 一组分组表（按 need 展示多个 block）。
 *
 * ## 为什么这里**不显示「达标」列**（2026-09-22 删）
 *
 * `passed` 回答的是"这套信号能不能当独立系统用"，**不是**"这一格好不好"。
 * 实测 44 个分桶里只有 2 个能过 —— 于是每张诊断表都变成一列 ⚠️。
 * 一行行刷 ⚠️ 不是信息，是噪声，还会让用户以为"这表没意义"。
 *
 * 目标线只在两处有意义，且都已经显示：顶部结论区与「按信号类型」里的门控对照。
 * 这里改成把目标线写进表头 tooltip —— 数字留着让人自己比，不下判决。
 *
 * ## 为什么加了「独立」列（2026-09-22 加）
 *
 * `样本` 与 `独立` 必须并排看，这是这两列存在的**全部理由**：判据用"份额
 * 5 日累计"而份额是存量，一次资金流入会让同一事件连续多日超标；同系列 ETF
 * 又常在同一天一起触发。于是"66 条信号"可能只对应 **3 个独立时段** ——
 * 只看样本列会把三条样本的结论读成统计事实。
 * `独立 ≤ 2` 时那个 ⚠️ 就是这个意思：**这一格无法验证**，不是"很好"。
 *
 * ## 为什么胜率后面挂着置信区间
 *
 * 一个孤零零的 "83.3%" 会被读成确定的事实。n=66 时 Wilson 区间是
 * [72.6%, 90.4%]，看着还行；换成真实的独立样本数就完全没有统计意义。
 * 区间与点估计**同时**呈现，读者自己就能判断该信多少。
 */
function HorizonTables({ table, empty }: { table: EtfFlowHorizonTable;
                                           empty: string }) {
  const rows = useMemo(() => flatten(table), [table]);
  if (rows.length === 0) return <div className="etf-flow-empty">{empty}</div>;
  return (
    <div className="etf-flow-table-scroll">
      <table className="etf-flow-table">
        <thead>
          <tr>
            <th>分组</th>
            <th className="num">观察期</th>
            <th className="num" title="信号条数（不等于独立事件数，见「独立」列）">
              样本
            </th>
            <th className="num"
                title={"前瞻窗口互不重叠的独立时段个数（按观察期长度去重）。\n"
                  + "样本 66 / 独立 3 意味着那个胜率只由 3 个事件撑着；\n"
                  + "≤2 时标 ⚠️ —— 那一格是「无法验证」，不是「很好」。"}>
              独立
            </th>
            <th className="num" title="T 收盘买入，持有到观察期末的收益中位数">
              T中位数
            </th>
            <th className="num"
                title={"T 口径下收益为正的比例；括号里是 Wilson 95% 置信区间。\n"
                  + "小样本必须连区间一起看：区间跨过 50% 就等于没结论。"}>
              T胜率
            </th>
            <th className="num" title="T+1 收盘买入（更保守口径）的收益中位数">
              T+1中位数
            </th>
            <th className="num" title="T+1 口径下收益为正的比例">T+1胜率</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.key}-${row.stats.horizon}`}>
              <td>
                {etfGroupText(row.key)}
                <span className="mono muted-text"> {row.key}</span>
              </td>
              <td className="num mono muted-text">{row.stats.horizon}日</td>
              <td className="num mono muted-text">{row.stats.samples}</td>
              <td className="num mono">
                <IndependentCell stats={row.stats} />
              </td>
              <td className={"num mono " + toneOf(row.stats.median)}>
                {formatRatioPct(row.stats.median)}
              </td>
              <td className="num mono">
                {rateText(row.stats.win_rate)}
                {ciText(row.stats) && (
                  <span className="muted-text"> {ciText(row.stats)}</span>
                )}
              </td>
              <td className={"num mono " + toneOf(row.stats.median_t1)}>
                {formatRatioPct(row.stats.median_t1)}
              </td>
              <td className="num mono">{rateText(row.stats.win_rate_t1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 独立时段数（旧回测记录没有这个字段 → `—`）。
 *
 * `≤2` 时挂一个 ⚠️：那一格的胜率只由一两个事件撑着，"结论成立"和
 * "无法验证"在数字上长得一模一样，这个标记是唯一的区分。
 */
const THIN_EPISODES = 2;

function IndependentCell({ stats }: { stats: EtfFlowHorizonStats }) {
  const value = stats.independent;
  if (value === null || value === undefined) {
    return <span className="muted-text"
                 title="这次回测落库时还没有「独立时段」这个口径（旧记录），重跑一次回测就有了">
      —
    </span>;
  }
  return (
    <>
      {value}
      {value <= THIN_EPISODES && (
        <span title={`独立时段只有 ${value} 个：这一格不是「结论很好」，`
          + "而是「无法验证」—— 换一段行情结论可能完全不同。"}>
          {" "}⚠️
        </span>
      )}
    </>
  );
}

/**
 * 样本内 / 样本外对照（并排，而不是两张独立的表）。
 *
 * ## 为什么必须并排
 *
 * 这张表的用法是"左右对照"，不是"分别看两遍"：阈值全是看着**全区间**结果
 * 调出来的，所以样本内那一半好看是**必然**的，它不构成任何证据；
 * 唯一的证据是右边那一半还能不能保持同样的方向。
 * 分成上下两张表就会让人（包括我自己）只看上半张。
 *
 * ## 为什么样本外那一半要特别看「独立」
 *
 * 样本外最常见的结局不是"结论翻转"，而是**只剩一两个独立时段**。
 * 那时候"结论仍然成立"（+9.42%、胜率 100%）和"结论根本无法验证"
 * 在胜率与中位数上长得一模一样 —— 只有独立时段数能把它们区分开。
 * 所以 `≤2` 时在这一格上挂 ⚠️，而不是等读者自己去数。
 *
 * ## 覆盖范围
 *
 * 后端只给**门控结论所依赖的那些分组**（机会/风险 × 三个环境、放行/降级两桶、
 * 行业反转），不是把四张表整体再算一遍 —— `by_etf` 与等级维度在样本外
 * 只剩个位数样本，铺开来看全是噪声。键形如 `"in:opportunity@bear"`。
 */
function ValidationTables({ table }: { table: EtfFlowHorizonTable }) {
  /** 去掉 `in:` / `out:` 前缀后的分组键（`level:` 里也有冒号，不能直接 split）。 */
  const groups = useMemo(() => {
    const keys = new Set<string>();
    for (const key of Object.keys(table ?? {})) {
      if (key.startsWith("in:")) keys.add(key.slice(3));
      else if (key.startsWith("out:")) keys.add(key.slice(4));
      else keys.add(key);
    }
    return [...keys].sort((left, right) => {
      const rank = groupRank(left) - groupRank(right);
      return rank !== 0 ? rank : left.localeCompare(right);
    });
  }, [table]);

  if (groups.length === 0) {
    return (
      <div className="etf-flow-empty">
        这次回测没有切分样本（`backtest.split` 为空），或切分后两侧都没有样本。
        在 configs/etf_flow.yaml 的 backtest 段配一个切分点即可。
      </div>
    );
  }
  return (
    <div className="etf-flow-table-scroll">
      <table className="etf-flow-table">
        <thead>
          <tr>
            <th rowSpan={2}>分组</th>
            <th className="num" rowSpan={2}>观察期</th>
            <th className="num" colSpan={4}>样本内（用来定阈值，好看是必然的）</th>
            <th className="num" colSpan={4}>样本外（唯一的证据）</th>
          </tr>
          <tr>
            <th className="num">样本</th>
            <th className="num" title="≤2 时这一格是「无法验证」，不是「很好」">
              独立
            </th>
            <th className="num">T中位数</th>
            <th className="num">T胜率</th>
            <th className="num">样本</th>
            <th className="num" title="≤2 时这一格是「无法验证」，不是「很好」">
              独立
            </th>
            <th className="num">T中位数</th>
            <th className="num">T胜率</th>
          </tr>
        </thead>
        <tbody>
          {groups.flatMap((group) => {
            const inside = table[`in:${group}`] ?? {};
            const outside = table[`out:${group}`] ?? {};
            const horizons = [...new Set([...Object.keys(inside),
                                          ...Object.keys(outside)])]
              .sort((left, right) => Number(left) - Number(right));
            return horizons.map((horizon) => (
              <tr key={`${group}-${horizon}`}>
                <td>
                  {etfGroupText(group)}
                  <span className="mono muted-text"> {group}</span>
                </td>
                <td className="num mono muted-text">{horizon}日</td>
                <SampleCells stats={inside[horizon]} />
                <SampleCells stats={outside[horizon]} />
              </tr>
            ));
          })}
        </tbody>
      </table>
    </div>
  );
}

/** 对照表的一半（样本内 或 样本外）：样本 / 独立 / T中位数 / T胜率 四个格。
 *
 * 整块缺失时四格一起给 `—` —— 留空会让人以为那一侧还没算，
 * 而实际含义是"那条切分线上这一组没有信号"。
 */
function SampleCells({ stats }: { stats?: EtfFlowHorizonStats }) {
  if (!stats) {
    return (
      <>
        <td className="num mono muted-text">—</td>
        <td className="num mono muted-text">—</td>
        <td className="num mono muted-text">—</td>
        <td className="num mono muted-text">—</td>
      </>
    );
  }
  return (
    <>
      <td className="num mono muted-text">{stats.samples}</td>
      <td className="num mono"><IndependentCell stats={stats} /></td>
      <td className={"num mono " + toneOf(stats.median)}>
        {formatRatioPct(stats.median)}
      </td>
      <td className="num mono">
        {rateText(stats.win_rate)}
        {ciText(stats) && <span className="muted-text"> {ciText(stats)}</span>}
      </td>
    </>
  );
}

/** 明细行。
 *
 * ## 为什么这里**不再标门控**（2026-09-22 删）
 *
 * 这张表是**已实现收益的复盘**（每条都带 T+5/10/20/34 的实际结果），不是
 * 待办清单 —— 一行灰着"已门控"只让人以为"这行不用看"，而门控挡对了没有
 * 恰恰只能在这里看。门控的整体效果已经由顶部结论区与「分类型胜率」的
 * 放行/降级两桶给出，逐行再刷一遍标签是重复信息。
 */
function RecordRow({ record }: { record: EtfFlowBacktestRecord }) {
  const repeat = record.repeat_days ?? 1;
  return (
    <tr>
      <td className="mono">
        {formatDate(record.date)}
        {repeat > 1 && (
          <i className="muted-text"
             title={`同一事件连续报 ${repeat} 个交易日`
               + `（${formatDate(record.date)} ~ ${formatDate(record.repeat_until || "")}）`
               + "；判据用份额 5 日累计，一次流入会让它连续多日超标，"
               + "这里只保留首日，避免同一事件占好几行把样本数虚增"}>
            {" "}×{repeat}日
          </i>
        )}
      </td>
      <td title={`${record.name}（${record.code}）`}>{record.name || record.code}</td>
      <td>{record.kind_label || ETF_KIND_TEXT[record.kind] || record.kind}</td>
      <td className={`etf-flow-level level-${record.level}`}>
        {record.level_label || record.level}
      </td>
      <td className="muted-text">
        {record.regime_label || ETF_REGIME_TEXT[record.regime] || record.regime}
      </td>
      <td className={"num mono " + toneOf(record.change_1d)}>
        {formatRatioPct(record.change_1d)}
      </td>
      {[5, 10, 20, 34].map((horizon) => (
        <td key={horizon}
            className={"num mono " + toneOf(record.forward[String(horizon)])}>
          {formatRatioPct(record.forward[String(horizon)])}
        </td>
      ))}
      <td className={"num mono " + toneOf(record.max_gain)}>
        {formatRatioPct(record.max_gain)}
      </td>
      <td className={"num mono " + toneOf(record.max_drawdown)}>
        {formatRatioPct(record.max_drawdown)}
      </td>
    </tr>
  );
}
