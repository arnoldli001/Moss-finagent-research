import { useCallback, useMemo, useState } from "react";
import { formatDate, formatRatioPct, toneOf } from "../mainlineApi";
import {
  ETF_KIND_ORDER,
  ETF_KIND_TEXT,
  ETF_LEVEL_TEXT,
  ETF_REGIME_TEXT,
  etfFlowApi,
  etfGateReason,
  formatPercentile,
  type EtfFlowSignal,
  type EtfFlowSignalRow,
  type EtfFlowSnapshot,
} from "../etfFlowApi";

/**
 * ETF 份额监控 · 信号列表（按 机会 / 风险 / 行业反转 分组）。
 *
 * ## 「已门控」为什么必须长得跟正常告警不一样
 *
 * 后端在非放行环境（非熊市）会把机会信号 `gated=true` 降级成**观察项**：它照样
 * 出现在列表里，但**不构成仓位建议**。如果两种情况渲染成同一个样子，用户会以为
 * 牛市里也该按"低位 + 大额申购"建仓 —— 而回测里这正是亏钱的那一格。
 * 所以这里做了三重区分：整行灰化 + 虚线边框 + 明确的「已门控」标签。
 *
 * ## 但默认**不显示**被门控项（2026-09-22 改）
 *
 * 早先默认全量显示，理由是"藏起来就没法复盘门控本身"。实测证明这个取舍错了：
 * 1253 条信号里会真正告警的只有约 66 条（5%），默认全量 → 打开列表是一屏
 * "风险信号/已门控"噪声，真正该行动的被淹掉；更糟的是**被门控的机会信号
 * 也跟着一起被无视**（2026-04-07 中证1000 那条之后 34 日 +18.16%）。
 *
 * 现在默认只看「要行动的」，看全量是**主动切换**的动作 —— 数据一条没删，
 * 只是不再默认糊在用户脸上。门控的语义（灰化 + 标签）仍然保留，
 * 因为"观察与门控"这一档里它们照样要和弱信号区分开。
 *
 * ## 等级与"会不会真的告警"不是一回事
 *
 * `level` 表示信号强度，`gated` 表示环境是否放行。真正会告警的是
 * **强/中等级 且 未被门控**（与后端 `alerts_only=true` 的口径一致）——
 * 弱信号与已门控信号都只是观察项。筛选项就按这个口径写，不另外发明规则。
 *
 * ## 历史信号为什么单独一节
 *
 * 快照里的 `signals` 只反映**当前交易日**（今天很可能一条都没有），
 * 复盘"过去这些信号后来涨没涨"要靠 `/etf-flow/signals` 读本地落库。
 * 落库是显式动作（`POST /save`），所以这里给一个按钮 —— 否则这一节永远是空的，
 * 与 MainlineBacktest 里"首次回测没有入口"是同一类问题。
 */

/** 等级 → 行 class（强/中/弱三色 + none 兜底）。 */
function levelClass(level: string): string {
  const key = (level || "").toLowerCase();
  if (key === "strong" || key === "medium" || key === "weak") return key;
  return "none";
}

/** 是否会真正告警：强/中等级且未被环境门控（= 后端 `alerts_only=true` 的口径）。 */
function isAlertSignal(signal: EtfFlowSignal): boolean {
  return !signal.gated
    && (signal.level === "strong" || signal.level === "medium");
}

type Props = {
  snapshot: EtfFlowSnapshot | null;
  loading: boolean;
};

export default function EtfFlowSignals({ snapshot, loading }: Props) {
  /**
   * 只看会真正告警的（强/中等级 且 未被环境门控）。
   *
   * ## 为什么**默认打开**（2026-09-22 改）
   *
   * 早先默认 `false`（全量显示），理由写在模块头："门控挡掉的信号不隐藏，
   * 是复盘参数最该看的部分"。那条理由本身没错，但**放错了位置**：
   * 实测 1253 条信号里会真正告警的只有约 66 条（5%），其余全是弱信号观察项
   * 与被门控项。默认展示全量 → 用户打开列表看到的是一屏"风险信号/已门控"
   * 噪声，而真正该行动的那几条被淹在里面。实测事故：2026-04-07 中证1000
   * 出现过机会信号（被门控，之后 34 日 +18.16%），用户在同一个列表里
   * 完全没注意到它，只看到一串风险信号。
   */
  const [onlyAlerts, setOnlyAlerts] = useState(true);

  /** 历史落库信号（null = 还没读过） */
  const [history, setHistory] = useState<EtfFlowSignalRow[] | null>(null);
  const [historyGaps, setHistoryGaps] = useState<string[]>([]);
  const [historyLatest, setHistoryLatest] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  /**
   * 历史表默认只显示"要行动的"。
   *
   * 这是用户实际截图反映的那个问题：默认全量时，历史表是一屏
   * "风险信号 / 放行"重复行（实测某段时间窗口里 21 行中 20 行是同一类），
   * 而被门控的机会信号（真正值钱的那 5%）混在里面看不出来。
   * 落库时仍然**全量写入**（`alertsOnly: false`），这里只是显示层过滤 ——
   * 复盘门控随时可以切回来。
   */
  /**
   * 历史表默认只保留**未被门控**的行。
   *
   * 与当日信号同一口径（见下方 `showGated`）：被门控的行不进默认视图。
   * 落库时仍然**全量写入**（`alertsOnly: false`），数据库里一条不少。
   */
  const [historyAlertsOnly, setHistoryAlertsOnly] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveNote, setSaveNote] = useState("");

  const signals = snapshot?.signals ?? [];

  /** 已落库行里被门控的条数（用于空状态与开关标注）。 */
  const historyGated = useMemo(
    () => (history ?? []).filter((row) => row.gated).length, [history]);

  /**
   * 是否显示**被门控的行**（默认否）。
   *
   * ## 为什么默认隐藏（2026-09-22，用户明确要求）
   *
   * 门控行是"环境不支持、只作观察"的项，`gated=true` 且等级降为 weak ——
   * 它既不是多方触发也不是空方触发，放在默认列表里就是噪声：实测某段时间窗口的
   * 历史表 21 行里 20 行是同一类重复行，反而把真正该行动的那条淹掉了。
   *
   * ⚠️ 但**数据一条都没删**：`gated=true` 的行仍然照常落库、接口照常返回、
   * 后端回测的放行/降级对照照常统计。这里只是显示层开关 ——
   * 关掉这个开关就回到"能复盘门控挡住了什么"。做成开关而不是永久删除，
   * 是因为"门控挡对了没有"只能从这批行里看（例如 2026-04-07 中证1000
   * 那条被门控的机会信号，之后 34 日 +18.16%）。
   */
  const [showGated, setShowGated] = useState(false);

  /** 历史表的显示行：默认排除已门控行，且只留"要行动的"。 */
  const historyRows = useMemo(() => {
    const rows = history ?? [];
    if (showGated && !historyAlertsOnly) return rows;
    return rows.filter((row) => !row.gated
      && (!historyAlertsOnly
        || row.level === "strong" || row.level === "medium"));
  }, [history, historyAlertsOnly, showGated]);

  const counts = useMemo(() => {
    const rows = signals.filter((row) => !showGated || !row.gated);
    return {
      all: rows.length,
      alerts: rows.filter(isAlertSignal).length,
      observations: rows.filter((row) => !isAlertSignal(row)).length,
      gated: signals.filter((row) => row.gated).length,
    };
  }, [signals, showGated]);

  /** 按类型分组；未知类型排在已知类型之后（不丢行）。 */
  const groups = useMemo(() => {
    let rows = signals;
    // 门控行不进默认视图（见 `showGated` 的说明）
    if (!showGated) rows = rows.filter((row) => !row.gated);
    if (onlyAlerts) rows = rows.filter(isAlertSignal);
    const kinds = [
      ...ETF_KIND_ORDER,
      ...Array.from(new Set(rows.map((row) => row.kind)))
        .filter((kind) => !ETF_KIND_ORDER.includes(kind)),
    ];
    return kinds
      .map((kind) => ({ kind, rows: rows.filter((row) => row.kind === kind) }))
      .filter((group) => group.rows.length > 0);
  }, [signals, onlyAlerts, showGated]);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    setHistoryError("");
    try {
      // alerts_only=false：历史里要能看见"被门控的机会信号"，
      // 否则没法复盘门控挡住了什么。
      const payload = await etfFlowApi.signals({ alertsOnly: false, limit: 200 });
      setHistory(payload.signals ?? []);
      setHistoryGaps(payload.gaps ?? []);
      setHistoryLatest(payload.latest_trade_date ?? "");
    } catch (exc) {
      setHistory(null);
      setHistoryError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const saveToday = useCallback(async () => {
    setSaving(true);
    setSaveNote("");
    try {
      const result = await etfFlowApi.save(snapshot?.trade_date ?? "");
      setSaveNote(`已写入 ${result.written} 条（当日信号 ${result.signal_count} 条`
        + ` · 交易日 ${formatDate(result.trade_date)}）`);
      await loadHistory();
    } catch (exc) {
      setSaveNote(`保存失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setSaving(false);
    }
  }, [snapshot?.trade_date, loadHistory]);

  if (!snapshot) {
    return (
      <div className="etf-flow-empty">
        {loading
          ? "正在读取信号…"
          : "还没有数据。点右上「立即刷新」按最新份额重算一次快照。"}
      </div>
    );
  }

  return (
    <div className="etf-flow-signals">
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>当日信号</h4>
          <span className="muted-text">
            {onlyAlerts
              ? `只看要行动的：${counts.alerts} 条（强/中等级且环境放行）`
              : `观察项：${counts.observations} 条（份额有异动，指数分位不在极端区）`}
            {!showGated && counts.gated > 0
              && ` · 另有 ${counts.gated} 条已门控（默认不显示）`}
          </span>
        </div>

        <div className="etf-flow-toolbar">
          {/* 视图互斥（而不是"等级 × 是否告警"两个正交筛选）：
              正交筛选会出现"只看弱信号 + 只看会告警"= 空集的组合，
              用户要自己推出"为什么没数据"。 */}
          <div className="mode-switch">
            <button className={onlyAlerts ? "mode-btn active" : "mode-btn"}
                    title="强/中等级且未被环境门控 —— 这些才是会推送给你的"
                    onClick={() => setOnlyAlerts(true)}>
              要行动的（{counts.alerts}）
            </button>
            <button className={!onlyAlerts ? "mode-btn active" : "mode-btn"}
                    title="弱信号观察项 —— 份额有异动但指数分位不在极端区"
                    onClick={() => setOnlyAlerts(false)}>
              观察项（{counts.observations}）
            </button>
          </div>
          <span style={{ flex: 1 }} />
          <label className="chart-toggle"
                 title={"被门控 = 环境不支持该信号（机会信号只在熊市放行），"
                   + "不构成仓位建议。默认隐藏；勾上可以看到门控挡住了什么"}>
            <input type="checkbox" checked={showGated}
                   onChange={(event) => setShowGated(event.target.checked)} />
            显示已门控（{counts.gated}）
          </label>
        </div>

        {signals.length === 0 ? (
          <div className="etf-flow-empty">
            当日没有触发任何信号（这是正常状态：机会信号要求"指数分位 ≤ 30% 且份额
            日环比 &gt; +3%"同时成立，风险信号要求"分位 ≥ 70% 且份额 &lt; -3%"）。
            下方「历史信号」可以看过去的触发记录。
          </div>
        ) : groups.length === 0 ? (
          <div className="etf-flow-empty">
            {counts.gated > 0 && !showGated
              ? `当日 ${counts.gated} 条信号全部被环境门控（仅观察，不构成仓位建议），`
                + "默认不显示。勾选「显示已门控」可以查看。"
              : "当前筛选下没有信号（换一个视图或等级试试）。"}
          </div>
        ) : (
          <div className="etf-flow-signal-scroll">
            {groups.map((group) => (
              <div key={group.kind} className="etf-flow-signal-group">
                <div className="etf-flow-signal-group-head">
                  <b>{group.rows[0].kind_label
                    || ETF_KIND_TEXT[group.kind] || group.kind}</b>
                  <span className="muted-text">
                    共 {group.rows.length} 条
                    {group.kind === "industry_reversal"
                      && "（行业 ETF 只出反转警示：资金大量流入后短期反转效应显著）"}
                  </span>
                </div>

                <div className="etf-flow-signal-head">
                  <span>日期</span>
                  <span>ETF</span>
                  <span>等级</span>
                  <span className="num">1日</span>
                  <span className="num">5日</span>
                  <span>环境</span>
                  <span>共振</span>
                  <span className="num">指数分位</span>
                  <span>是否告警</span>
                </div>

                <ul className="etf-flow-signal-list">
                  {group.rows.map((signal) => (
                    <li key={signal.alert_id
                      || `${signal.date}-${signal.code}-${signal.kind}`}
                        className={`etf-flow-signal level-${levelClass(signal.level)}`
                          + (signal.gated ? " gated" : "")}>
                      <span className="mono">{formatDate(signal.date)}</span>
                      <span className="etf-flow-signal-name"
                            title={`${signal.name || signal.code}（${signal.code}）`
                              + `\n系列：${signal.group_label || signal.group || "—"}`}>
                        {signal.name || signal.code}
                        <i className="mono muted-text">{signal.code}</i>
                      </span>
                      <span className={`etf-flow-level level-${levelClass(signal.level)}`}>
                        {signal.level_label
                          || ETF_LEVEL_TEXT[levelClass(signal.level)] || "—"}
                      </span>
                      <span className={"num mono " + toneOf(signal.change_1d)}>
                        {formatRatioPct(signal.change_1d)}
                      </span>
                      <span className={"num mono " + toneOf(signal.change_5d)}>
                        {formatRatioPct(signal.change_5d)}
                      </span>
                      <span className="muted-text">
                        {signal.regime_label
                          || ETF_REGIME_TEXT[signal.regime] || signal.regime || "—"}
                      </span>
                      <span className="muted-text mono"
                            title={`同系列同向 ${signal.resonance_count}`
                              + ` / ${signal.resonance_total} 只`}>
                        {signal.resonance
                          ? `共振 ${signal.resonance_count}/${signal.resonance_total}`
                          : "—"}
                      </span>
                      <span className="num mono"
                            title={`${signal.index_name || signal.index_code || "—"}`
                              + " 的位置分位（越大越接近窗口高位）"}>
                        {formatPercentile(signal.index_percentile)}
                      </span>
                      <span className="etf-flow-signal-tags">
                        {signal.gated ? (
                          <i className="etf-flow-gated-tag"
                             title={`${etfGateReason(signal.kind)}`
                              + "；被门控的信号不构成仓位建议"}>
                            已门控
                          </i>
                        ) : isAlertSignal(signal) ? (
                          <i className="etf-flow-alert-tag" title="强/中等级且环境放行">
                            会告警
                          </i>
                        ) : (
                          <i className="etf-flow-weak-tag"
                             title="等级为弱：份额异动但指数分位不在极端区，属观察项">
                            观察项
                          </i>
                        )}
                      </span>
                      {/* 理由逐条列出：合并成一段会丢掉"哪一条成立、哪一条只是增强" */}
                      <ul className="etf-flow-reasons">
                        {signal.reasons.length === 0
                          ? <li className="muted-text">后端未给触发理由</li>
                          : signal.reasons.map((reason, index) => (
                            <li key={index}>{reason}</li>
                          ))}
                      </ul>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 历史落库信号：复盘"门控挡住了什么 / 放行的后来怎么样" */}
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>历史信号（本地落库）</h4>
          <span className="muted-text">
            读 /etf-flow/signals（近 200 条，新的在前）
            {historyLatest ? ` · 最新 ${formatDate(historyLatest)}` : ""}
          </span>
        </div>

        <div className="etf-flow-toolbar">
          <button className="btn-ghost tiny" disabled={historyLoading}
                  title="读本地已落库的信号（含被门控的，便于复盘门控本身）"
                  onClick={() => void loadHistory()}>
            {historyLoading ? "读取中…" : "读取历史"}
          </button>
          <button className="btn-ghost tiny" disabled={saving}
                  title="把当前快照的信号写入本地库（幂等 upsert）——
                          不落库就没有历史可复盘"
                  onClick={() => void saveToday()}>
            {saving ? "保存中…" : "保存当日信号"}
          </button>
          {history !== null && history.length > 0 && (
            <div className="mode-switch">
              <button className={historyAlertsOnly ? "mode-btn active" : "mode-btn"}
                      title="强/中等级且未被环境门控 —— 会真正推送给你的那些"
                      onClick={() => setHistoryAlertsOnly(true)}>
                要行动的
              </button>
              <button className={!historyAlertsOnly ? "mode-btn active" : "mode-btn"}
                      title="弱信号观察项 —— 份额有异动但指数分位不在极端区"
                      onClick={() => setHistoryAlertsOnly(false)}>
                观察项
              </button>
            </div>
          )}
          <span style={{ flex: 1 }} />
          <label className="chart-toggle"
                 title={"被门控 = 环境不支持该信号（机会信号只在熊市放行），"
                   + "不构成仓位建议。默认隐藏；勾上可以看到门控挡住了什么"}>
            <input type="checkbox" checked={showGated}
                   onChange={(event) => setShowGated(event.target.checked)} />
            显示已门控{historyGated > 0 ? `（${historyGated}）` : ""}
          </label>
          {saveNote && <span className="muted-text">{saveNote}</span>}
        </div>

        {historyError && (
          <div className="error-box">读取历史信号失败：{historyError}</div>
        )}
        {historyGaps.length > 0 && (
          <div className="etf-flow-gaps">⚠️ {historyGaps.join("；")}</div>
        )}
        {history === null && !historyLoading && !historyError && (
          <div className="etf-flow-empty">
            还没读过历史。点「读取历史」看已落库的信号 ——
            若一直为空，先点「保存当日信号」。
          </div>
        )}
        {history !== null && history.length === 0 && (
          <div className="etf-flow-empty">
            本地库里还没有 ETF 份额信号。点「保存当日信号」写入，之后这里会逐日累积。
          </div>
        )}
        {history !== null && history.length > 0 && historyRows.length === 0 && (
          <div className="etf-flow-empty">
            近 {history.length} 条落库信号里没有需要行动的
            {historyGated > 0 && `（其中 ${historyGated} 条已门控，默认不显示）`}。
            想要复盘就把「显示已门控」勾上 —— 那批行记录的是"低位有大额申购、
            但当时环境不放行"的时点，是复核门控参数唯一的依据。
          </div>
        )}

        {historyRows.length > 0 && (
          <div className="etf-flow-table-scroll">
            <table className="etf-flow-table">
              <thead>
                <tr>
                  <th>交易日</th>
                  <th>ETF</th>
                  <th>类型</th>
                  <th>等级</th>
                  <th className="num">1日</th>
                  <th className="num">5日</th>
                  <th>环境</th>
                  <th className="num">指数分位</th>
                  <th>是否告警</th>
                  <th>写入时间</th>
                </tr>
              </thead>
              <tbody>
                {historyRows.map((row) => (
                  <tr key={row.alert_id || `${row.trade_date}-${row.code}`}
                      className={row.gated ? "row-gated" : ""}>
                    <td className="mono">{formatDate(row.trade_date)}</td>
                    <td title={`${row.name}（${row.code}）`}>
                      {row.name || row.code}
                    </td>
                    <td>{ETF_KIND_TEXT[row.kind] ?? row.kind}</td>
                    <td className={`etf-flow-level level-${levelClass(row.level)}`}>
                      {ETF_LEVEL_TEXT[levelClass(row.level)] ?? row.level}
                    </td>
                    <td className={"num mono " + toneOf(row.change_1d)}>
                      {formatRatioPct(row.change_1d)}
                    </td>
                    <td className={"num mono " + toneOf(row.change_5d)}>
                      {formatRatioPct(row.change_5d)}
                    </td>
                    <td className="muted-text">
                      {ETF_REGIME_TEXT[row.regime] || row.regime || "—"}
                    </td>
                    <td className="num mono">
                      {formatPercentile(row.index_percentile)}
                    </td>
                    <td>
                      {row.gated
                        ? <i className="etf-flow-gated-tag"
                             title={etfGateReason(row.kind)}>已门控</i>
                        : (row.level === "strong" || row.level === "medium")
                          ? <i className="etf-flow-alert-tag">会告警</i>
                          : <i className="etf-flow-weak-tag">观察项</i>}
                    </td>
                    <td className="mono muted-text">
                      {(row.updated_at || "").replace("T", " ").slice(0, 16) || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="etf-flow-note muted-text">
          「要行动的」= 强/中等级且未被门控（与后端 alerts_only 口径一致）；
          <b>已门控的行默认不显示</b>（环境不支持该信号，不构成仓位建议），
          勾选「显示已门控」可查看 —— 数据一条没删，只是不进默认视图。
          <b>「指数分位」是理解每一条的关键</b>：机会信号要求它 ≤ 30%（低位），
          风险信号要求 ≥ 70%（高位）—— 同一只 ETF 短期内先报机会、后报风险
          并不矛盾，那是分位在区间内来回穿越造成的。
        </div>
      </section>
    </div>
  );
}
